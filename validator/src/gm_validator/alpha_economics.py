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

from gm_validator import metrics

MAX_WEIGHT = 65535
MINER_EMISSION_PCT = Decimal("0.41")

_ZERO = Decimal(0)
_QUANTUM = Decimal(1) / MAX_WEIGHT


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
    total_consumed_usd: Decimal
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
        total_consumed_usd=total_consumed_usd,
        miners=results,
    )


def _report_payment_accuracy(
    renorm: Decimal,
    cleaned: list[tuple[int, Decimal]],
    miner_u16: list[int],
) -> None:
    """Compute and publish the observation-only payment-accuracy gauges.

    Compares each scored miner's intended pool share (``weight / renorm``)
    against its submitted share (``u16 / MAX_WEIGHT``). Pure observation: it
    derives nothing the caller reads back, so the returned weight vector is
    identical whether or not this runs.

    Args:
        renorm: The renormalization denominator applied to intended shares.
            Unused when ``cleaned`` is empty (the all-burn epoch).
        cleaned: ``(uid, weight)`` pairs with strictly-positive weight, in
            submission order.
        miner_u16: Submitted u16 weight per cleaned miner, index-aligned with
            ``cleaned``. Excludes the burn slot.
    """
    residual = _ZERO
    below_quantum = 0
    floored_total = _ZERO
    for (_, weight), u16 in zip(cleaned, miner_u16, strict=True):
        intended = weight / renorm
        submitted = Decimal(u16) / MAX_WEIGHT
        residual += abs(intended - submitted)
        if intended < _QUANTUM:
            below_quantum += 1
            floored_total += Decimal(u16) - intended * MAX_WEIGHT
    metrics.record_payment_accuracy(
        quantization_residual=float(residual),
        miners_below_quantum=below_quantum,
        floored_weight_total=float(floored_total),
    )


def normalize_weights(
    miner_weights: list[tuple[int, Decimal]],
    burn_uid: int,
    *,
    renorm_total: Decimal | None = None,
) -> list[tuple[int, int]]:
    """Convert Decimal-domain weights to u16, padding the burn slot with dust.

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
        renorm_total: Full demand total in pool units: the sum of every
            scored miner's weight, including miners dropped from the
            submitted vector. When supplied, this total is used as the
            renormalization denominator so dropped demand still
            suppresses the submitted shares.

    Returns:
        ``(uid, u16_weight)`` pairs summing to exactly ``MAX_WEIGHT``.

    Raises:
        ValueError: More than ``MAX_WEIGHT`` miners have positive weight, so
            they cannot all be floored to >= 1 within the u16 budget.
    """
    cleaned = [(uid, w) for uid, w in miner_weights if w > 0]
    if not cleaned:
        _report_payment_accuracy(_ZERO, [], [])
        return [(burn_uid, MAX_WEIGHT)]
    if len(cleaned) > MAX_WEIGHT:
        raise ValueError(
            f"cannot floor {len(cleaned)} positive miners to >=1 within MAX_WEIGHT={MAX_WEIGHT}"
        )

    total = sum((w for _, w in cleaned), _ZERO)
    if total <= 0:
        _report_payment_accuracy(_ZERO, [], [])
        return [(burn_uid, MAX_WEIGHT)]

    # Renorm over the full demand total when supplied so demand from miners
    # dropped from the submitted vector still suppresses the submitted shares;
    # absent that, the submitted total is the whole demand.
    demand_total = total if renorm_total is None else renorm_total
    renorm = max(demand_total, Decimal(1))

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
        _reclaim_overflow(result, [w for _, w in cleaned], -remainder)

    miner_count = len(cleaned)
    if remainder > 0:
        for i, (uid, w) in enumerate(result):
            if uid == burn_uid:
                result[i] = (uid, w + remainder)
                break
        else:
            result.append((burn_uid, remainder))

    # Report against the final submitted u16 of each miner — the first
    # ``miner_count`` entries stay index-aligned with ``cleaned`` even when the
    # burn dust was merged into a colliding miner uid above.
    _report_payment_accuracy(renorm, cleaned, [w for _, w in result[:miner_count]])

    return result


def _reclaim_overflow(
    result: list[tuple[int, int]],
    shares: list[Decimal],
    deficit: int,
) -> None:
    """Trim ``deficit`` units off the heaviest entries so the vector sums to
    exactly ``MAX_WEIGHT`` without dropping any floored miner below 1.

    Over-subscription floors many sub-unit shares up to 1, which can push the
    sum past ``MAX_WEIGHT``. Reclaim by water-filling: shave the deficit off
    the tallest weights uniformly so a larger share never falls below a smaller
    one. ``shares`` (the pre-quantization Decimal weights, index-aligned with
    ``result``) breaks ties so the shave lands on the genuinely smaller share
    when two miners quantize to the same u16 value. Floored dust entries
    (weight 1) are never touched — the count guard in the caller guarantees the
    taller entries hold enough surplus.
    """
    order = sorted(
        range(len(result)),
        key=lambda i: (result[i][1], shares[i]),
        reverse=True,
    )
    weights = [result[i][1] for i in order]

    level = _water_level(weights, deficit)
    for idx in order:
        uid, weight = result[idx]
        if weight <= level:
            break
        result[idx] = (uid, level)
        deficit -= weight - level

    # Residual units (deficit not divisible across the capped entries) come off
    # the bottom of the `level` plateau upward, so an originally-larger share
    # never drops below an originally-smaller one.
    plateau_end = next(
        (rank for rank, idx in enumerate(order) if result[idx][1] < level),
        len(order),
    )
    cursor = plateau_end - 1
    while deficit > 0 and cursor >= 0:
        uid, weight = result[order[cursor]]
        if weight > 1:
            result[order[cursor]] = (uid, weight - 1)
            deficit -= 1
        cursor -= 1


def _water_level(weights: list[int], deficit: int) -> int:
    """Highest integer level L such that capping ``weights`` (descending) at L
    removes at most ``deficit`` units. Capping the remainder above L is handled
    by the caller one unit at a time.
    """
    reclaimed = 0
    for rank in range(1, len(weights)):
        # Dropping the top `rank` entries from weights[rank-1] to weights[rank]
        # reclaims `rank * drop` units.
        drop = weights[rank - 1] - weights[rank]
        if reclaimed + rank * drop >= deficit:
            return weights[rank - 1] - (deficit - reclaimed) // rank
        reclaimed += rank * drop
    n = len(weights)
    return weights[-1] - (deficit - reclaimed) // n
