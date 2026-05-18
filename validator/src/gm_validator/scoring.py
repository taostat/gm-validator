"""Per-miner score derivation from `aggregated.jsonl`.

A miner's epoch score is the sum of `earnings_pdollars +
surcharge_pdollars` across every product they served. Normalized
across miners, this becomes the input to `subtensor.set_weights()`.

The scoring is intentionally simple in v1 — `validator.md §15` and
`research.md §H.3` confirm "for each (miner, product) tuple in the
summary, the miner's earned amount is miner_price * tokens_served
summed across all products" with no protocol-margin adjustment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class MinerScore:
    """Per-miner score components plus weight."""

    miner_id: str
    earnings_pdollars: int = 0
    surcharge_pdollars: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    weight: float = 0.0
    per_product: dict[tuple[str, str], int] = field(default_factory=dict)


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
    `normalise_weights`.
    """
    scores: dict[str, MinerScore] = {}
    for row in rows:
        miner_id = row["miner_id"]
        bucket = scores.setdefault(miner_id, MinerScore(miner_id=miner_id))
        earn = int(row.get("earnings_pdollars", "0") or 0)
        surch = int(row.get("surcharge_pdollars", "0") or 0)
        bucket.earnings_pdollars += earn
        bucket.surcharge_pdollars += surch
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

    Picodollar-precision input, float weight output suitable for
    `subtensor.set_weights()`.
    """
    total = sum(s.earnings_pdollars + s.surcharge_pdollars for s in scores.values())
    if total <= 0:
        # No earnings this epoch — every miner gets equal weight (or 0).
        # Bittensor's set_weights requires the vector to sum to ~1.0 if
        # we're emitting any weights at all, so we emit an all-zero vector
        # which corresponds to "no incentive this epoch."
        for s in scores.values():
            s.weight = 0.0
        return scores
    for s in scores.values():
        s.weight = (s.earnings_pdollars + s.surcharge_pdollars) / total
    return scores


def usd_for_alpha(scores: dict[str, MinerScore], alpha_per_usd: float) -> dict[str, float]:
    """Convert per-miner pdollar earnings to subnet alpha.

    Args:
        scores: Output of `normalise_weights`.
        alpha_per_usd: Spot exchange rate (alpha tokens per 1 USD).

    Returns:
        Mapping of miner hotkey -> alpha amount.
    """
    # picodollars -> dollars: divide by 1e12; -> alpha: multiply by rate.
    return {
        miner_id: (s.earnings_pdollars + s.surcharge_pdollars) / 1e12 * alpha_per_usd
        for miner_id, s in scores.items()
    }


def aggregated_path(mirror_dir: str) -> str:
    """Convenience: path-join helper for the canonical filename."""
    return os.path.join(mirror_dir, "aggregated.jsonl")
