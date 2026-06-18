"""Per-miner score derivation from `aggregated.jsonl`.

A miner's epoch score is the sum of ``earnings_ndollars +
surcharge_ndollars`` across every product they served. The validator
converts those sums to u16 weights via the cap+burn pipeline in
:mod:`gm_validator.alpha_economics`: miner i gets
``consumed_usd_i / pool_usd``, the residue routes to the subnet-owner
uid as burn weight, and oversubscribed demand renorms down inside
:func:`alpha_economics.normalize_weights`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from gm_validator.alpha_economics import (
    EpochWeightsResult,
    MinerEpochData,
    compute_epoch_weights,
    normalize_weights,
)
from gm_validator.epoch_summary import EpochSummary

LOGGER = logging.getLogger(__name__)

NDOLLARS_PER_USD = Decimal(10**9)


class StaleMetagraphError(Exception):
    """All scored miners missing from miner_uid_lookup — defer this epoch.

    Raised when the metagraph hotkey->uid lookup is stale (every scored
    miner is unknown). The next process_once() tick will retry; if the
    metagraph has refreshed by then, the epoch proceeds normally.
    """


class StaleEpochSummaryError(Exception):
    """`epoch_summary.json` predates the chain-read ``emissions_alpha`` field.

    Raised when the artifact was written by an older finalizer release
    (pre gm PR #176) and so does not carry ``emissions_alpha``. The
    validator defers the epoch — the cap+burn pool denominator cannot be
    invented without ground-truth — and the next ``process_once`` tick
    retries. Once every prod finalizer is at >= the chain-read release
    this branch is unreachable; the field stays optional in the parser
    only to surface this transient cleanly rather than fail-fast on
    parser load.
    """


class MalformedArtifactError(Exception):
    """An aggregated.jsonl row is structurally invalid — a permanent fault.

    Raised when a required field is missing, null, or non-integer where an
    integer is required. Distinct from the transient Stale* defers: a
    malformed artifact will not fix itself on the next tick, so the caller
    must surface it loudly rather than silently zero a miner earnings and
    misroute incentive.
    """


@dataclass
class MinerScore:
    """Per-miner score components aggregated from `aggregated.jsonl`."""

    miner_id: str
    earnings_ndollars: int = 0
    surcharge_ndollars: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    per_product: dict[tuple[str, str], int] = field(default_factory=dict)


@dataclass
class WeightVector:
    """Result of the score → weights pipeline."""

    uids: list[int]
    weights: list[int]
    burn_uid: int
    epoch_result: EpochWeightsResult


def load_aggregated(path: str) -> list[dict]:
    """Read `aggregated.jsonl` from a local file. Skips blank lines."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def _coerce_int(value: object, key: str, miner_id: str) -> int:
    # bool is an int subclass; the not-bool guard keeps a JSON true/false from
    # coercing to 1/0 money before the isinstance(int) accept.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            pass
    raise MalformedArtifactError(
        f"aggregated.jsonl row for miner_id={miner_id!r} field {key!r} "
        f"must be an integer; got {value!r}"
    )


def _require_int(row: dict, key: str, miner_id: str) -> int:
    if key not in row:
        raise MalformedArtifactError(
            f"aggregated.jsonl row for miner_id={miner_id!r} missing required field {key!r}"
        )
    return _coerce_int(row[key], key, miner_id)


def _optional_int(row: dict, key: str, miner_id: str) -> int:
    if key not in row:
        return 0
    return _coerce_int(row[key], key, miner_id)


def score(rows: Iterable[dict]) -> dict[str, MinerScore]:
    """Aggregate `aggregated.jsonl` rows into per-miner scores."""
    scores: dict[str, MinerScore] = {}
    for row in rows:
        if "miner_id" not in row:
            raise MalformedArtifactError("aggregated.jsonl row missing required field 'miner_id'")
        miner_id = row["miner_id"]
        # Must be a non-empty hotkey string: a numeric or empty miner_id would
        # never match the hotkey->uid lookup, so it would silently miss payout.
        if not isinstance(miner_id, str) or not miner_id:
            raise MalformedArtifactError(
                f"aggregated.jsonl row field 'miner_id' must be a non-empty string; "
                f"got {miner_id!r}"
            )
        earn = _require_int(row, "earnings_ndollars", miner_id)
        surch = _require_int(row, "surcharge_ndollars", miner_id)
        successful = _optional_int(row, "successful_requests", miner_id)
        failed = _optional_int(row, "failed_requests", miner_id)

        bucket = scores.setdefault(miner_id, MinerScore(miner_id=miner_id))
        bucket.earnings_ndollars += earn
        bucket.surcharge_ndollars += surch
        bucket.successful_requests += successful
        bucket.failed_requests += failed
        product = row.get("product") or {}
        provider = product.get("provider", "")
        model = product.get("model", "")
        bucket.per_product[(provider, model)] = (
            bucket.per_product.get((provider, model), 0) + earn + surch
        )
    return scores


def aggregated_path(mirror_dir: str) -> str:
    """Convenience: path-join helper for the canonical filename."""
    return os.path.join(mirror_dir, "aggregated.jsonl")


def _apply_earnings_multiplier(ndollars: int, multiplier: Decimal) -> int:
    """Scale integer nano-dollars by ``multiplier``, flooring to an integer.

    Multiplier ``1`` returns the input untouched — an exact no-op that never
    routes the value through Decimal-context rounding. Other values floor the
    product so no float precision leaks into the persisted money math. Used
    only by the TESTNET-ONLY demo knob; see :func:`compute_weights`.
    """
    if multiplier == 1:
        return ndollars
    # Multiply in exact integer arithmetic via the multiplier's numerator /
    # denominator, then floor — independent of the active Decimal precision, so
    # no float precision leaks into the money value.
    numerator, denominator = multiplier.as_integer_ratio()
    return ndollars * numerator // denominator


def compute_weights(
    scores: dict[str, MinerScore],
    miner_uid_lookup: dict[str, int],
    *,
    epoch_summary: EpochSummary,
    subnet_owner_uid: int,
    earnings_multiplier: Decimal = Decimal(1),
) -> WeightVector:
    """Convert per-miner scores to a u16 weight vector for `set_weights`.

    Args:
        scores: Per-miner score totals (from :func:`score`).
        miner_uid_lookup: Mapping from miner hotkey to subnet uid. Miners
            absent here still count toward the pool denominator but are
            dropped from the submitted vector.
        epoch_summary: Per-epoch price + emission snapshot written by the
            finalizer. Both ``alpha_price_usd`` and ``emissions_alpha``
            come from the same chain read pinned to the epoch-close
            block, so every validator sees identical pool inputs.
        subnet_owner_uid: Uid that absorbs the burn slot + floor-rounding
            dust.
        earnings_multiplier: TESTNET-ONLY demo knob. Scales every miner's
            aggregated nano-dollar earnings in memory before the alpha
            conversion so sub-floor test earnings can cross the
            ``1/MAX_WEIGHT`` weight floor and light up on-chain. The
            scaled value is floored back to an integer nano-dollar count
            (no float drift). MUST stay ``1`` on mainnet — any other
            value distorts real payouts. Defaults to ``1`` (exact
            no-op).

    Returns:
        WeightVector: aligned ``uids``, ``weights`` (u16, sum =
            ``MAX_WEIGHT``), plus the per-epoch result for audit
            logging.

    Raises:
        StaleMetagraphError: All scored miners are absent from
            ``miner_uid_lookup``. The caller should defer the epoch
            rather than mark it processed.
        StaleEpochSummaryError: ``epoch_summary.emissions_alpha`` is
            missing — the artifact was written by a pre-chain-read
            finalizer. The caller should defer the epoch rather than
            invent a pool denominator.
    """
    if epoch_summary.emissions_alpha is None:
        raise StaleEpochSummaryError(
            f"epoch {epoch_summary.epoch_id}: epoch_summary.json is missing "
            f"emissions_alpha (finalizer_version={epoch_summary.finalizer_version!r}); "
            f"refusing to score until the finalizer republishes with chain-read emission"
        )
    # All scored miners contribute to the pool denominator — missing-uid
    # miners are still real demand against it. The uid lookup only gates
    # whether we can route them in this epoch's submission.
    miners_data: list[MinerEpochData] = []
    uids_by_index: list[int | None] = []
    missing_hotkeys: list[str] = []
    for miner_id, s in scores.items():
        uid = miner_uid_lookup.get(miner_id)
        scaled_ndollars = _apply_earnings_multiplier(
            s.earnings_ndollars + s.surcharge_ndollars, earnings_multiplier
        )
        total_ndollars = Decimal(scaled_ndollars)
        miners_data.append(
            MinerEpochData(
                hotkey=miner_id,
                consumed_usd=total_ndollars / NDOLLARS_PER_USD,
            )
        )
        uids_by_index.append(uid)
        if uid is None:
            missing_hotkeys.append(miner_id)

    # All scored miners missing from the lookup means the metagraph is
    # stale. Raise so process_once() defers without marking the epoch
    # processed — a bare empty-vector return would get silently marked
    # processed and the epoch would be lost.
    if miners_data and all(uid is None for uid in uids_by_index):
        sample = missing_hotkeys[:10]
        ellipsis = "..." if len(missing_hotkeys) > 10 else ""
        raise StaleMetagraphError(
            f"all {len(missing_hotkeys)} scored miners missing from "
            f"miner_uid_lookup; hotkeys={sample}{ellipsis}"
        )

    if missing_hotkeys:
        LOGGER.warning(
            "epoch scoring: %d scored miner(s) missing from miner_uid_lookup; "
            "their demand counts toward the pool but they miss this epoch's payout. "
            "hotkeys=%s",
            len(missing_hotkeys),
            missing_hotkeys,
        )

    result = compute_epoch_weights(
        miners_data,
        alpha_price_usd=epoch_summary.alpha_price_usd,
        emissions_alpha=epoch_summary.emissions_alpha,
    )
    full_demand_total = sum((miner_weight.weight for miner_weight in result.miners), Decimal(0))

    miner_pairs: list[tuple[int, Decimal]] = []
    for miner_weight, uid in zip(result.miners, uids_by_index, strict=True):
        if uid is None:
            continue
        miner_pairs.append((uid, miner_weight.weight))

    u16 = normalize_weights(
        miner_pairs,
        burn_uid=subnet_owner_uid,
        renorm_total=full_demand_total,
    )
    uids = [uid for uid, _ in u16]
    weights = [w for _, w in u16]
    return WeightVector(
        uids=uids,
        weights=weights,
        burn_uid=subnet_owner_uid,
        epoch_result=result,
    )
