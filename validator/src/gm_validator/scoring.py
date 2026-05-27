"""Per-miner score derivation from `aggregated.jsonl`.

A miner's epoch score is the sum of `earnings_ndollars +
surcharge_ndollars` across every product they served. The float weight
submitted to bittensor is derived from that score after applying the
bm-style miner-emission cap:

    miner_pool_alpha   = emissions_alpha × MINER_EMISSION_PCT
    miner_pool_usd     = miner_pool_alpha × alpha_price_usd
    total_consumed_usd = Σ (earnings + surcharge) / 1e9
    scale              = min(1.0, miner_pool_usd / total_consumed_usd)
    payout_usd_i       = consumed_usd_i × scale
    payout_alpha_i     = payout_usd_i / alpha_price_usd
    weight_i           = payout_alpha_i / miner_pool_alpha

Under-subscription (total_consumed < miner_pool_usd) keeps scale=1 so the
sum of miner weights stays below 1 — the unburned remainder is the
fraction PR 2 will route to the subnet-owner UID. Over-subscription
caps each miner's payout at their proportional share of the pool.

`validator.md §15` and `research.md §H.3` describe the v1 scoring rule
("for each (miner, product) tuple, miner_price × tokens_served"); the
cap-and-burn layer is the bm-validator pattern documented in
`gm/docs/research/bm-validator-deep-dive.md`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

LOGGER = logging.getLogger(__name__)

# Protocol constant: miner share of full-epoch alpha emission. Matches
# bm-validator's whitepaper split (41% miners / 41% validators / 18% owner).
# Exposed via env (MINER_EMISSION_PCT) only for testbed override; do not
# change at runtime.
MINER_EMISSION_PCT_DEFAULT = Decimal("0.41")

# 1 USD = 10^9 ndollars. Mirrors the gateway/registry convention.
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
class EpochCapSummary:
    """Audit row for the emission-cap math.

    Surfaced by ``apply_emission_cap`` so the orchestrator can log and
    eventually route the burn fraction to the subnet-owner UID (PR 2).
    """

    alpha_price_usd: Decimal
    emissions_alpha: Decimal
    miner_emission_pct: Decimal
    miner_pool_alpha: Decimal
    miner_pool_usd: Decimal
    total_consumed_usd: Decimal
    scale: Decimal
    submitted_weight_sum: float
    burn_fraction: float


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
    """Aggregate `aggregated.jsonl` rows into per-miner scores.

    Returns a dict keyed by miner hotkey. `weight` is populated by
    ``apply_emission_cap``.
    """
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


def apply_emission_cap(
    scores: dict[str, MinerScore],
    *,
    alpha_price_usd: Decimal,
    emissions_alpha: Decimal,
    miner_emission_pct: Decimal = MINER_EMISSION_PCT_DEFAULT,
) -> EpochCapSummary:
    """Compute per-miner float weights using the bm-validator cap pattern.

    Mutates each ``MinerScore.weight`` in place; returns an
    ``EpochCapSummary`` for logging and metric emission.

    When ``total_consumed_usd < miner_pool_usd`` the sum of returned
    weights is strictly less than 1.0 — the missing fraction is the burn
    target that PR 2 will route to the subnet-owner UID.

    Args:
        scores: Per-miner score buckets as produced by ``score``.
        alpha_price_usd: Current USD-per-alpha (oracle output). Must be
            > 0.
        emissions_alpha: Full-epoch alpha emission (chain/finalizer
            output). Must be > 0.
        miner_emission_pct: Miner share of emissions, defaulting to the
            bm-validator constant 0.41.

    Raises:
        ValueError: ``alpha_price_usd`` or ``emissions_alpha`` is not
            strictly positive.
    """
    if alpha_price_usd <= 0:
        raise ValueError(f"alpha_price_usd must be positive, got {alpha_price_usd}")
    if emissions_alpha <= 0:
        raise ValueError(f"emissions_alpha must be positive, got {emissions_alpha}")

    miner_pool_alpha = emissions_alpha * miner_emission_pct
    miner_pool_usd = miner_pool_alpha * alpha_price_usd

    total_consumed_usd = Decimal(0)
    consumed_usd_by_miner: dict[str, Decimal] = {}
    for miner_id, s in scores.items():
        consumed_ndollars = Decimal(s.earnings_ndollars + s.surcharge_ndollars)
        consumed_usd = consumed_ndollars / NDOLLARS_PER_USD
        consumed_usd_by_miner[miner_id] = consumed_usd
        total_consumed_usd += consumed_usd

    if total_consumed_usd > 0:
        scale = min(Decimal(1), miner_pool_usd / total_consumed_usd)
    else:
        scale = Decimal(1)

    submitted_weight_sum = 0.0
    for miner_id, s in scores.items():
        consumed_usd = consumed_usd_by_miner[miner_id]
        payout_usd = consumed_usd * scale
        payout_alpha = payout_usd / alpha_price_usd
        weight = payout_alpha / miner_pool_alpha if miner_pool_alpha > 0 else Decimal(0)
        s.weight = float(weight)
        submitted_weight_sum += s.weight

    burn_fraction = max(0.0, 1.0 - submitted_weight_sum)

    summary = EpochCapSummary(
        alpha_price_usd=alpha_price_usd,
        emissions_alpha=emissions_alpha,
        miner_emission_pct=miner_emission_pct,
        miner_pool_alpha=miner_pool_alpha,
        miner_pool_usd=miner_pool_usd,
        total_consumed_usd=total_consumed_usd,
        scale=scale,
        submitted_weight_sum=submitted_weight_sum,
        burn_fraction=burn_fraction,
    )
    LOGGER.info(
        "emission cap applied: alpha_price=$%s emissions=%s miner_pool=$%s "
        "consumed=$%s scale=%s submitted_weight_sum=%.6f burn_fraction=%.6f",
        alpha_price_usd,
        emissions_alpha,
        miner_pool_usd,
        total_consumed_usd,
        scale,
        submitted_weight_sum,
        burn_fraction,
    )
    return summary


def aggregated_path(mirror_dir: str) -> str:
    """Convenience: path-join helper for the canonical filename."""
    return os.path.join(mirror_dir, "aggregated.jsonl")
