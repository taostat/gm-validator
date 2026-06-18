"""Tests for the scoring → u16 weight vector pipeline."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.epoch_summary import EpochSummary
from gm_validator.scoring import (
    StaleEpochSummaryError,
    StaleMetagraphError,
    compute_weights,
    score,
)


def _summary(
    alpha_price_usd: str = "0.50",
    *,
    emissions_alpha: str | None = "50",
) -> EpochSummary:
    payload: dict[str, object] = {
        "epoch_id": 1,
        "finalized_at": "2026-05-27T12:00:00Z",
        "alpha_price_in_tao": "0.05",
        "tao_price_usd": "10",
        "alpha_price_usd": alpha_price_usd,
        "price_block_height": 1,
        "price_alpha_source": "chain",
        "price_tao_usd_source": "taostats",
        "finalizer_version": "test",
    }
    if emissions_alpha is not None:
        payload["emissions_alpha"] = emissions_alpha
        payload["emissions_alpha_source"] = "chain"
    return EpochSummary.model_validate(payload)


def _row(miner_id: str, earnings_ndollars: int) -> dict:
    return {
        "epoch_id": 1,
        "miner_id": miner_id,
        "product": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "totals": {"input_tokens": 0, "output_tokens": 0},
        "earnings_ndollars": str(earnings_ndollars),
        "surcharge_ndollars": "0",
        "successful_requests": 1,
        "failed_requests": 0,
        "raw_record_count": 1,
        "raw_hash": "0" * 64,
    }


def test_undersubscribed_routes_residue_to_burn_uid() -> None:
    # 3 miners, $9 of demand against a $10.25 pool — under-subscribed.
    # ndollars per miner: 5, 3, 1 (×10^9).
    rows = [
        _row("A", 5_000_000_000),
        _row("B", 3_000_000_000),
        _row("C", 1_000_000_000),
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2, "C": 3},
        epoch_summary=_summary("0.50", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    assert vector.burn_uid == 99
    assert 99 in vector.uids
    assert sum(vector.weights) == MAX_WEIGHT
    # Burn slice ~ (1 - 9/10.25) of MAX_WEIGHT ≈ 0.122 * 65535 ≈ 7992.
    burn_w = dict(zip(vector.uids, vector.weights, strict=True))[99]
    assert burn_w > 6000
    assert burn_w < 10000


def test_oversubscribed_renorms_no_burn_slice() -> None:
    """Demand > pool → normalize_weights renorms; burn drops to
    floor-rounding dust."""
    rows = [
        _row("A", 10_000_000_000),  # $10
        _row("B", 5_000_000_000),  # $5
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        epoch_summary=_summary("0.50", emissions_alpha="50"),  # pool = $10.25 < $15
        subnet_owner_uid=99,
    )
    assert sum(vector.weights) == MAX_WEIGHT
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    burn_w = weights_by_uid.get(99, 0)
    assert burn_w < 5  # only dust, if any


def test_zero_consumption_all_to_burn() -> None:
    rows = [_row("A", 0), _row("B", 0)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        epoch_summary=_summary("0.50", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    assert vector.uids == [99]
    assert vector.weights == [MAX_WEIGHT]


def test_miner_outside_lookup_dropped() -> None:
    rows = [_row("A", 700_000_000), _row("UNKNOWN", 300_000_000)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1},
        epoch_summary=_summary("0.50", emissions_alpha="100"),
        subnet_owner_uid=99,
    )
    assert "UNKNOWN" not in (str(u) for u in vector.uids)
    assert sum(vector.weights) == MAX_WEIGHT


def test_all_miners_missing_uid_raises_stale_metagraph() -> None:
    """All scored miners absent from the uid lookup => StaleMetagraphError.

    process_once() catches this and defers the epoch without marking it
    processed — a silent empty-vector return would get marked processed
    and the epoch would be lost.
    """
    rows = [_row("A", 1_000_000_000), _row("B", 2_000_000_000)]
    scores = score(rows)
    with pytest.raises(StaleMetagraphError) as exc_info:
        compute_weights(
            scores,
            miner_uid_lookup={},  # neither miner known
            epoch_summary=_summary("0.50", emissions_alpha="100"),
            subnet_owner_uid=99,
        )
    message = str(exc_info.value)
    assert "2 scored miners missing" in message
    assert "A" in message
    assert "B" in message


def test_partial_missing_uid_still_counted_against_pool() -> None:
    """Unknown miners' demand suppresses known miners' submitted share.

    2 known miners ($5, $3) + 1 unknown ($2). Total demand = $10 against
    a $5.125 pool (50 alpha × 0.41 × $0.25), so demand over-subscribes the
    pool. Per-miner weight = consumed / pool_usd; the renorm denominator is
    the full demand sum (10 / 5.125), unknown included. A and B therefore
    receive exactly their share of total demand — A = 5/10, B = 3/10 of
    MAX_WEIGHT — and the unknown's 2/10 is left in the burn slot rather than
    redistributed to the known miners.
    """
    rows = [
        _row("A", 5_000_000_000),  # $5
        _row("B", 3_000_000_000),  # $3
        _row("UNKNOWN", 2_000_000_000),  # $2
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},  # UNKNOWN absent
        # pool = 50 * 0.41 * 0.25 = $5.125
        epoch_summary=_summary("0.25", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    assert vector.burn_uid == 99
    assert sum(vector.weights) == MAX_WEIGHT
    assert "UNKNOWN" not in {str(u) for u in vector.uids}

    # All three miners contribute to total_consumed_usd; the missing one
    # is silently dropped from the submission but counted in the pool
    # math.
    assert vector.epoch_result.total_consumed_usd == Decimal("10")

    # Known miners get exactly their floored share of total demand:
    # A = floor(5/10 * MAX_WEIGHT), B = floor(3/10 * MAX_WEIGHT). The
    # unknown's 2/10 stays in the burn slot rather than inflating A and B.
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    assert weights_by_uid[1] == int(MAX_WEIGHT * Decimal(5) / Decimal(10))
    assert weights_by_uid[2] == int(MAX_WEIGHT * Decimal(3) / Decimal(10))
    assert weights_by_uid[99] == MAX_WEIGHT - weights_by_uid[1] - weights_by_uid[2]


def test_oversubscribed_unknown_miner_not_redistributed_to_known() -> None:
    """An unknown miner's share is not handed to the remaining known miners.

    A ($3) is known; UNKNOWN ($4) is absent from the lookup. Total demand
    $7 over-subscribes the $5.125 pool. A's true share is its slice of total
    demand, 3/7 of MAX_WEIGHT. The buggy normalization renormed only over the
    submitted miners: with A's solo weight 3/5.125 < 1 the renorm denominator
    collapsed to 1, paying A 3/5.125 ≈ 0.585 of MAX_WEIGHT — its full
    unscaled pool share — and silently transferring UNKNOWN's emission to A.
    """
    rows = [
        _row("A", 3_000_000_000),  # $3, known
        _row("UNKNOWN", 4_000_000_000),  # $4, absent from lookup
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1},  # UNKNOWN absent
        # pool = 50 * 0.41 * 0.25 = $5.125
        epoch_summary=_summary("0.25", emissions_alpha="50"),
        subnet_owner_uid=99,
    )
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    assert sum(vector.weights) == MAX_WEIGHT
    # UNKNOWN is dropped from the submitted vector: only A's uid and burn remain.
    assert set(vector.uids) == {1, 99}
    assert vector.epoch_result.total_consumed_usd == Decimal("7")

    # A gets floor(3/7) of total demand, not its full unscaled 3/5.125
    # pool share (which the buggy code paid: floor(3/5.125 * MAX_WEIGHT)).
    assert weights_by_uid[1] == int(MAX_WEIGHT * Decimal(3) / Decimal(7))
    assert weights_by_uid[1] < int(MAX_WEIGHT * Decimal(3) / Decimal("5.125"))
    # The remaining 4/7 (UNKNOWN's share) lands in burn, not on A.
    assert weights_by_uid[99] == MAX_WEIGHT - weights_by_uid[1]


def test_multiplier_one_is_exact_no_op() -> None:
    """Default multiplier (1) produces byte-identical weights to no knob."""
    rows = [
        _row("A", 5_000_000_000),
        _row("B", 3_000_000_000),
        _row("C", 1_000_000_000),
    ]
    lookup = {"A": 1, "B": 2, "C": 3}
    summary = _summary("0.50", emissions_alpha="50")

    baseline = compute_weights(
        score(rows), miner_uid_lookup=lookup, epoch_summary=summary, subnet_owner_uid=99
    )
    explicit_one = compute_weights(
        score(rows),
        miner_uid_lookup=lookup,
        epoch_summary=summary,
        subnet_owner_uid=99,
        earnings_multiplier=Decimal(1),
    )
    assert explicit_one.uids == baseline.uids
    assert explicit_one.weights == baseline.weights


def test_multiplier_one_no_op_for_large_earnings() -> None:
    """Multiplier 1 is exact even for totals exceeding Decimal's default
    precision — the value bypasses any Decimal-context rounding."""
    big = 12_345_678_901_234_567_890_123_456_789  # 29 digits > default prec 28
    rows = [_row("A", big)]
    lookup = {"A": 1}
    summary = _summary("0.50", emissions_alpha="50")

    baseline = compute_weights(
        score(rows), miner_uid_lookup=lookup, epoch_summary=summary, subnet_owner_uid=99
    )
    explicit_one = compute_weights(
        score(rows),
        miner_uid_lookup=lookup,
        epoch_summary=summary,
        subnet_owner_uid=99,
        earnings_multiplier=Decimal(1),
    )
    assert explicit_one.weights == baseline.weights
    # consumed_usd reflects the exact 29-digit total, not a rounded one.
    assert explicit_one.epoch_result.total_consumed_usd == Decimal(big) / Decimal(10**9)


def test_fractional_multiplier_floors_to_integer_ndollars() -> None:
    """A fractional multiplier floors the scaled earnings to whole nano-dollars
    with no float drift."""
    rows = [_row("A", 1_000_000_001)]  # odd value so 1.5x has a fractional tail
    lookup = {"A": 1}
    summary = _summary("0.50", emissions_alpha="50")

    vector = compute_weights(
        score(rows),
        miner_uid_lookup=lookup,
        epoch_summary=summary,
        subnet_owner_uid=99,
        earnings_multiplier=Decimal("1.5"),
    )
    # 1_000_000_001 * 1.5 = 1_500_000_001.5 -> floored to 1_500_000_001 nd.
    expected_usd = Decimal(1_500_000_001) / Decimal(10**9)
    assert vector.epoch_result.total_consumed_usd == expected_usd


def test_multiplier_scales_pre_cap_earnings_input() -> None:
    """A multiplier of N scales the consumed_usd that feeds the pool math by N."""
    rows = [_row("A", 50_000)]  # well under the pool — sub-floor at multiplier 1
    lookup = {"A": 1}
    summary = _summary("0.50", emissions_alpha="50")

    base = compute_weights(
        score(rows), miner_uid_lookup=lookup, epoch_summary=summary, subnet_owner_uid=99
    )
    scaled = compute_weights(
        score(rows),
        miner_uid_lookup=lookup,
        epoch_summary=summary,
        subnet_owner_uid=99,
        earnings_multiplier=Decimal(1000),
    )
    base_consumed = base.epoch_result.total_consumed_usd
    scaled_consumed = scaled.epoch_result.total_consumed_usd
    assert scaled_consumed == base_consumed * 1000


def test_multiplier_lifts_sub_floor_miner_to_proportional_weight() -> None:
    """Representative testnet case: ~50k nd earnings against a large pool sits at
    the 1-unit dust floor at multiplier 1, but a 100000x knob lifts the miner to
    a clearly-visible proportional u16 well above the floor."""
    rows = [_row("A", 50_000)]  # $5e-5 of demand
    lookup = {"A": 1}
    # emissions_alpha=10000, price=$1 -> pool = 10000*0.41*1 = $4100.
    # share = 5e-5 / 4100 ~= 1.2e-8 -> *65535 ~= 8e-4, which the floor-to-1
    # patch pins at a single dust unit — invisible against the 65534 burn.
    summary = _summary("1.0", emissions_alpha="10000")

    base = compute_weights(
        score(rows), miner_uid_lookup=lookup, epoch_summary=summary, subnet_owner_uid=99
    )
    base_by_uid = dict(zip(base.uids, base.weights, strict=True))
    assert base_by_uid[1] == 1  # pinned to the dust floor — effectively burns
    assert base_by_uid[99] == MAX_WEIGHT - 1

    amplified = compute_weights(
        score(rows),
        miner_uid_lookup=lookup,
        epoch_summary=summary,
        subnet_owner_uid=99,
        earnings_multiplier=Decimal(100_000),
    )
    amp_by_uid = dict(zip(amplified.uids, amplified.weights, strict=True))
    assert amp_by_uid[1] > 1  # lifted off the dust floor into a visible share
    assert sum(amplified.weights) == MAX_WEIGHT


def test_missing_emissions_alpha_raises_stale_epoch_summary() -> None:
    """epoch_summary.json without emissions_alpha => StaleEpochSummaryError.

    A pre-chain-read finalizer wrote the artifact; the validator defers
    rather than inventing a pool denominator. process_once() catches this
    and skips the processed-state mark so the next tick retries once
    the finalizer republishes.
    """
    rows = [_row("A", 1_000_000_000)]
    scores = score(rows)
    with pytest.raises(StaleEpochSummaryError) as exc_info:
        compute_weights(
            scores,
            miner_uid_lookup={"A": 1},
            epoch_summary=_summary("0.50", emissions_alpha=None),
            subnet_owner_uid=99,
        )
    message = str(exc_info.value)
    assert "emissions_alpha" in message
    assert "epoch 1" in message
