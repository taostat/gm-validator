"""Tests for the scoring → u16 weight vector pipeline."""

from __future__ import annotations

from decimal import Decimal

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.epoch_summary import EpochSummary
from gm_validator.scoring import compute_weights, score


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
