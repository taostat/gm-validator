"""Tests for the scoring → u16 weight vector pipeline."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.epoch_summary import EpochSummary
from gm_validator.scoring import StaleMetagraphError, compute_weights, score


def _summary(alpha_price_usd: str = "0.50") -> EpochSummary:
    return EpochSummary.model_validate(
        {
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
    )


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


def test_legacy_path_when_flag_off_emits_u16_summing_to_max() -> None:
    rows = [_row("A", 700_000_000), _row("B", 300_000_000)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        use_emission_cap=False,
        epoch_summary=None,
        alpha_emission_per_epoch=Decimal("100"),
        subnet_owner_uid=0,
    )
    assert vector.used_emission_cap is False
    assert vector.burn_uid is None
    assert sum(vector.weights) == MAX_WEIGHT
    assert set(vector.uids) == {1, 2}


def test_flag_on_but_summary_missing_falls_back_to_legacy() -> None:
    rows = [_row("A", 700_000_000), _row("B", 300_000_000)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        use_emission_cap=True,
        epoch_summary=None,
        alpha_emission_per_epoch=Decimal("100"),
        subnet_owner_uid=0,
    )
    assert vector.used_emission_cap is False
    assert sum(vector.weights) == MAX_WEIGHT


def test_cap_path_routes_residue_to_burn_uid() -> None:
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
        use_emission_cap=True,
        epoch_summary=_summary("0.50"),
        alpha_emission_per_epoch=Decimal("50"),
        subnet_owner_uid=99,
    )
    assert vector.used_emission_cap is True
    assert vector.burn_uid == 99
    assert 99 in vector.uids
    assert sum(vector.weights) == MAX_WEIGHT
    # The burn uid should hold a non-trivial slice (~12.2% of MAX_WEIGHT).
    burn_w = dict(zip(vector.uids, vector.weights, strict=True))[99]
    assert burn_w > 6000  # well above mere dust
    assert burn_w < 10000  # but bounded under the expected 0.122 fraction


def test_cap_path_over_subscribed_no_burn_slice() -> None:
    """Demand > pool: scale < 1, burn weight ≈ 0 (only floor-rounding dust)."""
    rows = [
        _row("A", 10_000_000_000),  # $10
        _row("B", 5_000_000_000),  # $5
    ]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        use_emission_cap=True,
        epoch_summary=_summary("0.50"),
        alpha_emission_per_epoch=Decimal("50"),  # pool = $10.25 < $15
        subnet_owner_uid=99,
    )
    assert sum(vector.weights) == MAX_WEIGHT
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    burn_w = weights_by_uid.get(99, 0)
    assert burn_w < 5  # only dust, if any


def test_cap_path_zero_consumption_all_to_burn() -> None:
    rows = [_row("A", 0), _row("B", 0)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        use_emission_cap=True,
        epoch_summary=_summary("0.50"),
        alpha_emission_per_epoch=Decimal("50"),
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
        use_emission_cap=True,
        epoch_summary=_summary("0.50"),
        alpha_emission_per_epoch=Decimal("100"),
        subnet_owner_uid=99,
    )
    assert "UNKNOWN" not in (str(u) for u in vector.uids)
    assert sum(vector.weights) == MAX_WEIGHT


def test_cap_path_all_miners_missing_uid_raises_stale_metagraph() -> None:
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
            use_emission_cap=True,
            epoch_summary=_summary("0.50"),
            alpha_emission_per_epoch=Decimal("100"),
            subnet_owner_uid=99,
        )
    message = str(exc_info.value)
    assert "2 scored miners missing" in message
    assert "A" in message
    assert "B" in message


def test_cap_path_partial_missing_uid_uses_correct_cap() -> None:
    """Partial uid misses still submit; missing demand counts toward the cap.

    2 known miners ($5, $3) + 1 unknown ($2). Total demand = $10. Pool is
    over-subscribed at $5.125 (50 alpha * 0.41 * $0.25), so scale =
    $5.125 / $10 = 0.5125. Known miners get their proportional share of
    that scaled payout — the unknown miner's $2 still suppresses the
    others' share. The bug was the unknown's $2 being ignored, which
    inflated the known miners' weights.
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
        use_emission_cap=True,
        epoch_summary=_summary("0.25"),  # pool = 50 * 0.41 * 0.25 = $5.125
        alpha_emission_per_epoch=Decimal("50"),
        subnet_owner_uid=99,
    )
    assert vector.used_emission_cap is True
    assert vector.burn_uid == 99
    assert sum(vector.weights) == MAX_WEIGHT
    assert "UNKNOWN" not in {str(u) for u in vector.uids}

    # Cap denominator includes UNKNOWN. With scale = 0.5125, total
    # payout_alpha across all three = 0.5125 * $10 / $0.25 = 20.5 alpha
    # against a 20.5-alpha pool -> burn weight = 0. UNKNOWN's slice
    # (0.5125 * $2 / $0.25 / 20.5 = 0.2) is forfeited and lost.
    # A: (5 / 10) * scale -> 0.5 of the pool's payout fraction
    # B: (3 / 10) * scale -> 0.3 of the pool's payout fraction
    # As a fraction of (A+B+UNKNOWN) cleaned weights:
    # A_share = 0.5 / 0.8 = 0.625; B_share = 0.3 / 0.8 = 0.375
    # If UNKNOWN had been ignored, A and B would have split a higher cap
    # share (no demand suppression), but the renormalisation across cleaned
    # weights still yields 0.625/0.375. The key assertion: the
    # *result.scale* used the full demand.
    assert vector.epoch_result is not None
    assert vector.epoch_result.total_consumed_usd == Decimal("10")
    assert vector.epoch_result.scale == Decimal("5.125") / Decimal("10")

    # Per-miner ratio for known miners on the submitted vector.
    weights_by_uid = dict(zip(vector.uids, vector.weights, strict=True))
    # A / B ratio should match 5:3 to within floor-rounding dust.
    assert abs((weights_by_uid[1] / max(weights_by_uid[2], 1)) - (5 / 3)) < 0.01


def test_legacy_path_zero_revenue_skips_submission() -> None:
    """Legacy path with zero total revenue returns an empty vector — skip submit.

    bittensor.set_weights drops sum-zero vectors before building the
    extrinsic, so an all-zeros submission would NOT clear the prior
    epoch's weights as intended. Explicit skip + log makes the
    limitation visible; clearing the chain on the legacy path would
    require routing to a burn UID, which the legacy path doesn't know.
    """
    rows = [_row("A", 0), _row("B", 0)]
    scores = score(rows)
    vector = compute_weights(
        scores,
        miner_uid_lookup={"A": 1, "B": 2},
        use_emission_cap=False,
        epoch_summary=None,
        alpha_emission_per_epoch=Decimal("100"),
        subnet_owner_uid=0,
    )
    assert vector.uids == []
    assert vector.weights == []
    assert vector.burn_uid is None
    assert vector.used_emission_cap is False
