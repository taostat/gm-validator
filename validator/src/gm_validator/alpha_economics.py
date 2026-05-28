"""Cap-and-burn weight math, ported from bm-validator.

The miner pool for one epoch is bounded by
``emissions_alpha * MINER_EMISSION_PCT * alpha_price_usd``. Demand above
the pool clamps proportionally; demand below the pool leaves a residue
that routes to the subnet owner uid as burn weight, padded with any
floor-rounding remainder so the submitted u16 vector sums to exactly
``MAX_WEIGHT``.

Reference: ``bm/validator/common/scoring/weights.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

MAX_WEIGHT = 65535
MINER_EMISSION_PCT = Decimal("0.41")

_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass
class MinerEpochData:
    """One miner's consumption for a single epoch."""

    hotkey: str
    consumed_usd: Decimal
    is_blacklisted: bool = False


@dataclass
class MinerWeight:
    """Per-miner result row from :func:`compute_epoch_weights`."""

    hotkey: str
    is_banned: bool
    consumed_usd: Decimal
    payout_usd: Decimal = _ZERO
    payout_alpha: Decimal = _ZERO
    weight: Decimal = _ZERO


@dataclass
class EpochWeightsResult:
    """Epoch-level summary plus the per-miner weight rows."""

    pool_usd_total: Decimal
    pool_alpha_total: Decimal
    total_consumed_usd: Decimal
    scale: Decimal
    burn_alpha: Decimal
    burn_weight: Decimal
    alpha_price_usd: Decimal
    emissions_alpha: Decimal
    miners: list[MinerWeight] = field(default_factory=list)


def compute_epoch_weights(
    miners_data: list[MinerEpochData],
    alpha_price_usd: Decimal,
    emissions_alpha: Decimal,
) -> EpochWeightsResult:
    """Apply bm-style cap+burn to one epoch's miner consumption.

    Args:
        miners_data: Per-miner USD consumption for the epoch.
        alpha_price_usd: Alpha price in USD at the epoch-close block.
        emissions_alpha: Total alpha issued in the epoch (chain-level).

    Returns:
        EpochWeightsResult: float-domain weights in [0, 1] plus the
            burn-weight residue. ``sum(miner.weight) + burn_weight`` is
            ``1`` modulo Decimal precision.

    Raises:
        ValueError: ``alpha_price_usd`` or ``emissions_alpha`` is not
            strictly positive.
    """
    if alpha_price_usd <= 0:
        raise ValueError("alpha_price_usd must be positive")
    if emissions_alpha <= 0:
        raise ValueError("emissions_alpha must be positive")

    miner_pool_alpha = emissions_alpha * MINER_EMISSION_PCT
    miner_pool_usd = miner_pool_alpha * alpha_price_usd

    results: list[MinerWeight] = []
    total_consumed_usd = _ZERO
    for miner in miners_data:
        consumed = _ZERO if miner.is_blacklisted else miner.consumed_usd
        total_consumed_usd += consumed
        results.append(
            MinerWeight(
                hotkey=miner.hotkey,
                is_banned=miner.is_blacklisted,
                consumed_usd=consumed,
            )
        )

    scale = min(_ONE, miner_pool_usd / total_consumed_usd) if total_consumed_usd > 0 else _ONE

    total_payout_alpha = _ZERO
    for r in results:
        r.payout_usd = r.consumed_usd * scale
        r.payout_alpha = r.payout_usd / alpha_price_usd
        total_payout_alpha += r.payout_alpha
        r.weight = r.payout_alpha / miner_pool_alpha

    burn_alpha = miner_pool_alpha - total_payout_alpha
    burn_weight = burn_alpha / miner_pool_alpha

    return EpochWeightsResult(
        pool_usd_total=miner_pool_usd,
        pool_alpha_total=miner_pool_alpha,
        total_consumed_usd=total_consumed_usd,
        scale=scale,
        burn_alpha=burn_alpha,
        burn_weight=burn_weight,
        alpha_price_usd=alpha_price_usd,
        emissions_alpha=miner_pool_alpha,
        miners=results,
    )


def normalize_weights(
    miner_weights: list[tuple[int, Decimal]],
    burn_uid: int,
) -> list[tuple[int, int]]:
    """Convert float-domain weights to u16, padding the burn slot with dust.

    Args:
        miner_weights: ``(uid, weight)`` pairs in the [0, 1] domain.
        burn_uid: Uid that absorbs the floor-rounding remainder. When
            ``miner_weights`` is empty or sums to zero, the burn uid gets
            the entire ``MAX_WEIGHT``.

    Returns:
        ``(uid, u16_weight)`` pairs summing to exactly ``MAX_WEIGHT``.
    """
    cleaned = [(uid, w) for uid, w in miner_weights if w > 0]
    if not cleaned:
        return [(burn_uid, MAX_WEIGHT)]

    total = sum((w for _, w in cleaned), _ZERO)
    if total <= 0:
        return [(burn_uid, MAX_WEIGHT)]

    renorm = total if total > _ONE else _ONE

    result: list[tuple[int, int]] = []
    running_total = 0
    for uid, weight in cleaned:
        u16_weight = int((weight / renorm) * MAX_WEIGHT)
        if u16_weight <= 0:
            continue
        result.append((uid, u16_weight))
        running_total += u16_weight

    remainder = MAX_WEIGHT - running_total
    if remainder <= 0:
        return result

    for i, (uid, w) in enumerate(result):
        if uid == burn_uid:
            result[i] = (uid, w + remainder)
            break
    else:
        result.append((burn_uid, remainder))

    return result
