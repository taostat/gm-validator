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


def aggregated_path(mirror_dir: str) -> str:
    """Convenience: path-join helper for the canonical filename."""
    return os.path.join(mirror_dir, "aggregated.jsonl")


def compute_weights(
    scores: dict[str, MinerScore],
    miner_uid_lookup: dict[str, int],
    *,
    epoch_summary: EpochSummary,
    alpha_emission_per_epoch: Decimal,
    subnet_owner_uid: int,
) -> WeightVector:
    """Convert per-miner scores to a u16 weight vector for `set_weights`.

    Args:
        scores: Per-miner score totals (from :func:`score`).
        miner_uid_lookup: Mapping from miner hotkey to subnet uid. Miners
            absent here still count toward the pool denominator but are
            dropped from the submitted vector.
        epoch_summary: Per-epoch price snapshot written by the finalizer.
        alpha_emission_per_epoch: Full-epoch alpha emission (chain-level
            constant; a follow-up will pull this from substrate).
        subnet_owner_uid: Uid that absorbs the burn slot + floor-rounding
            dust.

    Returns:
        WeightVector: aligned ``uids``, ``weights`` (u16, sum =
            ``MAX_WEIGHT``), plus the per-epoch result for audit
            logging.

    Raises:
        StaleMetagraphError: All scored miners are absent from
            ``miner_uid_lookup``. The caller should defer the epoch
            rather than mark it processed.
    """
    # All scored miners contribute to the pool denominator — missing-uid
    # miners are still real demand against it. The uid lookup only gates
    # whether we can route them in this epoch's submission.
    miners_data: list[MinerEpochData] = []
    uids_by_index: list[int | None] = []
    missing_hotkeys: list[str] = []
    for miner_id, s in scores.items():
        uid = miner_uid_lookup.get(miner_id)
        total_ndollars = Decimal(s.earnings_ndollars + s.surcharge_ndollars)
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
        emissions_alpha=alpha_emission_per_epoch,
    )

    miner_pairs: list[tuple[int, Decimal]] = []
    for miner_weight, uid in zip(result.miners, uids_by_index, strict=True):
        if uid is None:
            continue
        miner_pairs.append((uid, miner_weight.weight))

    u16 = normalize_weights(miner_pairs, burn_uid=subnet_owner_uid)
    uids = [uid for uid, _ in u16]
    weights = [w for _, w in u16]
    return WeightVector(
        uids=uids,
        weights=weights,
        burn_uid=subnet_owner_uid,
        epoch_result=result,
    )
