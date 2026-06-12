"""Cap-and-burn weight math, ported from bm-validator.

For each miner i:

    weight_i = consumed_usd_i / pool_usd

where ``pool_usd = emissions_alpha * MINER_EMISSION_PCT * alpha_price_usd``.

When total miner demand is under the pool, the leftover surfaces as
``1 - sum(weights)`` and routes to the subnet-owner uid as burn weight
in :func:`normalize_weights`. When demand exceeds the pool the per-miner
weights sum > 1 and ``normalize_weights`` renorms them down so the
submitted u16 vector sums to exactly ``MAX_WEIGHT``; burn drops to
floor-rounding dust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

MAX_WEIGHT = 65535
MINER_EMISSION_PCT = Decimal("0.41")

_ZERO = Decimal(0)


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
    weight: Decimal = _ZERO


@dataclass
class EpochWeightsResult:
    """Epoch-level summary plus the per-miner weight rows."""

    pool_usd_total: Decimal
    pool_alpha_total: Decimal
    total_consumed_usd: Decimal
    alpha_price_usd: Decimal
    emissions_alpha: Decimal
    miners: list[MinerWeight] = field(default_factory=list)


def compute_epoch_weights(
    miners_data: list[MinerEpochData],
    alpha_price_usd: Decimal,
    emissions_alpha: Decimal,
) -> EpochWeightsResult:
    """Compute per-miner weights as ``consumed_usd / pool_usd``.

    Weights may sum > 1 when total billing exceeds the pool;
    ``normalize_weights`` renorms downstream — miners share the pool,
    burn drops to floor-rounding dust.

    Args:
        miners_data: Per-miner USD consumption for the epoch.
        alpha_price_usd: Alpha price in USD at the epoch-close block.
        emissions_alpha: Total alpha issued in the epoch (chain-level).

    Returns:
        EpochWeightsResult: float-domain weights in the pool's units
            (``consumed_i / pool_usd``) plus the totals used to derive
            them.

    Raises:
        ValueError: ``alpha_price_usd`` or ``emissions_alpha`` is not
            strictly positive.
    """
    if alpha_price_usd <= 0:
        raise ValueError("alpha_price_usd must be positive")
    if emissions_alpha <= 0:
        raise ValueError("emissions_alpha must be positive")

    pool_alpha = emissions_alpha * MINER_EMISSION_PCT
    pool_usd = pool_alpha * alpha_price_usd

    results: list[MinerWeight] = []
    total_consumed_usd = _ZERO
    for miner in miners_data:
        consumed = _ZERO if miner.is_blacklisted else miner.consumed_usd
        total_consumed_usd += consumed
        weight = consumed / pool_usd if pool_usd > 0 else _ZERO
        results.append(
            MinerWeight(
                hotkey=miner.hotkey,
                is_banned=miner.is_blacklisted,
                consumed_usd=consumed,
                weight=weight,
            )
        )

    return EpochWeightsResult(
        pool_usd_total=pool_usd,
        pool_alpha_total=pool_alpha,
        total_consumed_usd=total_consumed_usd,
        alpha_price_usd=alpha_price_usd,
        emissions_alpha=pool_alpha,
        miners=results,
    )


def normalize_weights(
    miner_weights: list[tuple[int, Decimal]],
    burn_uid: int,
) -> list[tuple[int, int]]:
    """Convert float-domain weights to u16, padding the burn slot with dust.

    Every miner with a strictly positive weight is floored to at least 1 u16
    unit so a share below ``1/MAX_WEIGHT`` keeps its emission instead of
    truncating to 0 and falling through to burn. The floored units are drawn
    from the burn remainder; when demand over-subscribes the pool and the
    burn remainder cannot cover them, the surplus is reclaimed from the
    heaviest miners so the vector still sums to exactly ``MAX_WEIGHT``.

    Args:
        miner_weights: ``(uid, weight)`` pairs in pool units. When sum
            > 1 the inputs are renormed before u16 conversion; when sum
            < 1 the residue routes to ``burn_uid``.
        burn_uid: Uid that absorbs the floor-rounding remainder. When
            ``miner_weights`` is empty or sums to zero, the burn uid gets
            the entire ``MAX_WEIGHT``.

    Returns:
        ``(uid, u16_weight)`` pairs summing to exactly ``MAX_WEIGHT``.

    Raises:
        ValueError: More than ``MAX_WEIGHT`` miners have positive weight, so
            they cannot all be floored to >= 1 within the u16 budget.
    """
    cleaned = [(uid, w) for uid, w in miner_weights if w > 0]
    if not cleaned:
        return [(burn_uid, MAX_WEIGHT)]
    if len(cleaned) > MAX_WEIGHT:
        raise ValueError(
            f"cannot floor {len(cleaned)} positive miners to >=1 within MAX_WEIGHT={MAX_WEIGHT}"
        )

    total = sum((w for _, w in cleaned), _ZERO)
    if total <= 0:
        return [(burn_uid, MAX_WEIGHT)]

    renorm = total if total > Decimal(1) else Decimal(1)

    # Floor: every strictly-positive share earns at least one unit so a miner
    # below 1/MAX_WEIGHT keeps its emission instead of truncating to burn. The
    # floored units come out of the burn remainder below; cleaned filters to
    # w > 0, so max(1, ...) never pays a non-participant.
    result: list[tuple[int, int]] = []
    running_total = 0
    for uid, weight in cleaned:
        u16_weight = max(1, int((weight / renorm) * MAX_WEIGHT))
        result.append((uid, u16_weight))
        running_total += u16_weight

    remainder = MAX_WEIGHT - running_total
    if remainder < 0:
        _reclaim_overflow(result, -remainder)
        return result
    if remainder == 0:
        return result

    for i, (uid, w) in enumerate(result):
        if uid == burn_uid:
            result[i] = (uid, w + remainder)
            break
    else:
        result.append((burn_uid, remainder))

    return result


def _reclaim_overflow(result: list[tuple[int, int]], deficit: int) -> None:
    """Trim ``deficit`` units off the heaviest entries so the vector sums to
    exactly ``MAX_WEIGHT`` without dropping any floored miner below 1.

    Over-subscription floors many sub-unit shares up to 1, which can push the
    sum past ``MAX_WEIGHT``; the surplus is reclaimed from the largest weights
    (those that can spare it) rather than from the floored ones.
    """
    order = sorted(range(len(result)), key=lambda i: result[i][1], reverse=True)
    cursor = 0
    while deficit > 0:
        idx = order[cursor % len(order)]
        uid, weight = result[idx]
        if weight > 1:
            result[idx] = (uid, weight - 1)
            deficit -= 1
        cursor += 1
