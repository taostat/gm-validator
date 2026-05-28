"""Per-miner score derivation from `aggregated.jsonl`.

A miner's epoch score is the sum of `earnings_ndollars +
surcharge_ndollars` across every product they served. The validator
turns those sums into u16 weights via one of two paths:

- **Naive** (legacy) — miner share of total earnings, normalised across
  miners to a u16 vector summing to ``MAX_WEIGHT``.
- **Emission cap + burn** (bm pattern) — miners' total demand is capped
  at ``alpha_emission_per_epoch * MINER_EMISSION_PCT * alpha_price_usd``;
  any unconsumed pool is routed to the subnet-owner uid as burn weight.
  See :mod:`gm_validator.alpha_economics`.

The selection is driven by ``ValidatorConfig.use_emission_cap`` and the
presence of ``epoch_summary.json`` on S3.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from gm_validator.alpha_economics import (
    MAX_WEIGHT,
    EpochWeightsResult,
    MinerEpochData,
    compute_epoch_weights,
    normalize_weights,
)
from gm_validator.epoch_summary import EpochSummary

LOGGER = logging.getLogger(__name__)

NDOLLARS_PER_USD = Decimal(10**9)


@dataclass
class MinerScore:
    """Per-miner score components plus weight."""

    miner_id: str
    earnings_ndollars: int = 0
    surcharge_ndollars: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    weight: float = 0.0
    per_product: dict[tuple[str, str], int] = field(default_factory=dict)


@dataclass
class WeightVector:
    """Result of the score → weights pipeline."""

    uids: list[int]
    weights: list[int]
    burn_uid: int | None
    used_emission_cap: bool
    epoch_result: EpochWeightsResult | None = None


def load_aggregated(path: str) -> list[dict]:
    """Read `aggregated.jsonl` from a local file. Skips blank lines."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def score(rows: Iterable[dict]) -> dict[str, MinerScore]:
    """Aggregate `aggregated.jsonl` rows into per-miner scores."""
    scores: dict[str, MinerScore] = {}
    for row in rows:
        miner_id = row["miner_id"]
        bucket = scores.setdefault(miner_id, MinerScore(miner_id=miner_id))
        earn = int(row.get("earnings_ndollars", "0") or 0)
        surch = int(row.get("surcharge_ndollars", "0") or 0)
        bucket.earnings_ndollars += earn
        bucket.surcharge_ndollars += surch
        bucket.successful_requests += int(row.get("successful_requests", 0))
        bucket.failed_requests += int(row.get("failed_requests", 0))
        product = row.get("product") or {}
        provider = product.get("provider", "")
        model = product.get("model", "")
        bucket.per_product[(provider, model)] = (
            bucket.per_product.get((provider, model), 0) + earn + surch
        )
    return scores


def normalise_weights(scores: dict[str, MinerScore]) -> dict[str, MinerScore]:
    """Compute per-miner weight as the fraction of total earnings.

    Float-domain output suitable for further conversion to u16. The
    weight vector sums to 1.0 when any miner earned, else 0.
    """
    total = sum(s.earnings_ndollars + s.surcharge_ndollars for s in scores.values())
    if total <= 0:
        for s in scores.values():
            s.weight = 0.0
        return scores
    for s in scores.values():
        s.weight = (s.earnings_ndollars + s.surcharge_ndollars) / total
    return scores


def aggregated_path(mirror_dir: str) -> str:
    """Convenience: path-join helper for the canonical filename."""
    return os.path.join(mirror_dir, "aggregated.jsonl")


def compute_weights(
    scores: dict[str, MinerScore],
    miner_uid_lookup: dict[str, int],
    *,
    use_emission_cap: bool,
    epoch_summary: EpochSummary | None,
    alpha_emission_per_epoch: Decimal,
    subnet_owner_uid: int,
) -> WeightVector:
    """Convert per-miner scores to a u16 weight vector for `set_weights`.

    Args:
        scores: Per-miner score totals (from :func:`score`).
        miner_uid_lookup: Mapping from miner hotkey to subnet uid. Miners
            absent here are dropped silently.
        use_emission_cap: When True, apply the bm-style cap+burn from
            :func:`alpha_economics.compute_epoch_weights`. Requires
            ``epoch_summary``; falls back to the naive path when None.
        epoch_summary: Per-epoch price snapshot written by the finalizer.
            ``None`` means a legacy epoch from before PR #161; the cap
            path is skipped and the legacy normalisation runs instead.
        alpha_emission_per_epoch: Full-epoch alpha emission (chain-level
            constant; a follow-up will pull this from substrate).
        subnet_owner_uid: Uid that absorbs the burn slot + floor-rounding
            dust under the cap path.

    Returns:
        WeightVector: aligned ``uids``, ``weights`` (u16, sum =
            ``MAX_WEIGHT``), plus the per-epoch result for audit
            logging when the cap path ran.
    """
    if use_emission_cap and epoch_summary is not None:
        return _compute_capped(
            scores,
            miner_uid_lookup,
            epoch_summary=epoch_summary,
            alpha_emission_per_epoch=alpha_emission_per_epoch,
            subnet_owner_uid=subnet_owner_uid,
        )

    if use_emission_cap and epoch_summary is None:
        LOGGER.warning(
            "USE_EMISSION_CAP=true but epoch_summary.json absent — "
            "falling back to legacy normalisation for this epoch."
        )

    return _compute_legacy(scores, miner_uid_lookup)


def _compute_legacy(
    scores: dict[str, MinerScore],
    miner_uid_lookup: dict[str, int],
) -> WeightVector:
    normalise_weights(scores)
    pairs: list[tuple[int, Decimal]] = []
    for miner_id, s in scores.items():
        uid = miner_uid_lookup.get(miner_id)
        if uid is None or s.weight <= 0:
            continue
        pairs.append((uid, Decimal(str(s.weight))))
    if not pairs:
        return WeightVector(uids=[], weights=[], burn_uid=None, used_emission_cap=False)
    # Floor-rounding into u16 with no burn slot — naive path doesn't
    # know a subnet owner uid. Any remainder lands on the highest-
    # weighted miner so the vector still sums to MAX_WEIGHT.
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    total = sum((w for _, w in pairs), Decimal(0))
    renorm = total if total > Decimal(1) else Decimal(1)
    u16: list[tuple[int, int]] = []
    running = 0
    for uid, w in pairs:
        v = int((w / renorm) * MAX_WEIGHT)
        if v <= 0:
            continue
        u16.append((uid, v))
        running += v
    remainder = MAX_WEIGHT - running
    if remainder > 0 and u16:
        head_uid, head_w = u16[0]
        u16[0] = (head_uid, head_w + remainder)
    uids = [uid for uid, _ in u16]
    weights = [w for _, w in u16]
    return WeightVector(uids=uids, weights=weights, burn_uid=None, used_emission_cap=False)


def _compute_capped(
    scores: dict[str, MinerScore],
    miner_uid_lookup: dict[str, int],
    *,
    epoch_summary: EpochSummary,
    alpha_emission_per_epoch: Decimal,
    subnet_owner_uid: int,
) -> WeightVector:
    miners_data: list[MinerEpochData] = []
    # Pair the MinerEpochData entries with their uids in lockstep so we
    # can map results back to uids without a second hotkey lookup.
    hotkeys_with_uid: list[tuple[str, int]] = []
    for miner_id, s in scores.items():
        uid = miner_uid_lookup.get(miner_id)
        if uid is None:
            continue
        total_ndollars = Decimal(s.earnings_ndollars + s.surcharge_ndollars)
        miners_data.append(
            MinerEpochData(
                hotkey=miner_id,
                consumed_usd=total_ndollars / NDOLLARS_PER_USD,
            )
        )
        hotkeys_with_uid.append((miner_id, uid))

    result = compute_epoch_weights(
        miners_data,
        alpha_price_usd=epoch_summary.alpha_price_usd,
        emissions_alpha=alpha_emission_per_epoch,
    )

    miner_pairs: list[tuple[int, Decimal]] = []
    for miner_weight, (_, uid) in zip(result.miners, hotkeys_with_uid, strict=True):
        miner_pairs.append((uid, miner_weight.weight))

    u16 = normalize_weights(miner_pairs, burn_uid=subnet_owner_uid)
    uids = [uid for uid, _ in u16]
    weights = [w for _, w in u16]
    return WeightVector(
        uids=uids,
        weights=weights,
        burn_uid=subnet_owner_uid,
        used_emission_cap=True,
        epoch_result=result,
    )
