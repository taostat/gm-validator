"""Unit + property tests for the cap+burn math."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gm_validator.alpha_economics import (
    MAX_WEIGHT,
    MinerEpochData,
    compute_epoch_weights,
    normalize_weights,
)


def _miners(values: list[tuple[str, str]]) -> list[MinerEpochData]:
    return [MinerEpochData(hotkey=h, consumed_usd=Decimal(v)) for h, v in values]


def test_case_b_under_subscribed_burns_residue() -> None:
    """Worked example A from the deep-dive: demand $9, pool $10.25."""
    miners = _miners([("A", "5"), ("B", "3"), ("C", "1")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    assert result.scale == Decimal(1)
    assert result.total_consumed_usd == Decimal(9)
    assert result.pool_usd_total == Decimal("10.25")
    assert result.burn_alpha == Decimal("2.5")
    expected_burn_weight = Decimal("2.5") / Decimal("20.5")
    assert result.burn_weight == expected_burn_weight
    weights_sum = sum((m.weight for m in result.miners), Decimal(0)) + result.burn_weight
    assert weights_sum == Decimal(1)


def test_case_a_over_subscribed_no_burn() -> None:
    """Demand exceeds pool: scale < 1, burn weight is zero."""
    miners = _miners([("A", "10"), ("B", "5")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    assert result.total_consumed_usd == Decimal(15)
    assert result.pool_usd_total == Decimal("10.25")
    assert result.scale < Decimal(1)
    assert result.burn_alpha == Decimal(0)
    assert result.burn_weight == Decimal(0)
    weights_sum = sum((m.weight for m in result.miners), Decimal(0)) + result.burn_weight
    assert weights_sum == Decimal(1)


def test_case_c_no_consumption_all_to_burn() -> None:
    """Zero demand: scale = 1 by convention, all weight burns."""
    miners = _miners([("A", "0"), ("B", "0")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    assert result.total_consumed_usd == Decimal(0)
    assert result.scale == Decimal(1)
    assert result.burn_weight == Decimal(1)
    for m in result.miners:
        assert m.weight == Decimal(0)


def test_case_d_single_miner_takes_whole_pool_when_demand_exceeds() -> None:
    miners = _miners([("solo", "100")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    assert result.miners[0].weight == Decimal(1)
    assert result.burn_weight == Decimal(0)


def test_blacklisted_miner_contributes_zero() -> None:
    miners = [
        MinerEpochData(hotkey="A", consumed_usd=Decimal("5")),
        MinerEpochData(hotkey="banned", consumed_usd=Decimal("5"), is_blacklisted=True),
    ]
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("1"),
        emissions_alpha=Decimal("100"),
    )
    assert result.miners[0].weight > Decimal(0)
    assert result.miners[1].is_banned
    assert result.miners[1].weight == Decimal(0)
    assert result.miners[1].consumed_usd == Decimal(0)


def test_zero_alpha_price_raises() -> None:
    with pytest.raises(ValueError, match="alpha_price_usd"):
        compute_epoch_weights(
            _miners([("A", "5")]),
            alpha_price_usd=Decimal(0),
            emissions_alpha=Decimal(10),
        )


def test_zero_emissions_raises() -> None:
    with pytest.raises(ValueError, match="emissions_alpha"):
        compute_epoch_weights(
            _miners([("A", "5")]),
            alpha_price_usd=Decimal("1"),
            emissions_alpha=Decimal(0),
        )


def test_normalize_empty_routes_all_to_burn() -> None:
    out = normalize_weights([], burn_uid=42)
    assert out == [(42, MAX_WEIGHT)]


def test_normalize_zero_total_routes_all_to_burn() -> None:
    out = normalize_weights([(1, Decimal(0)), (2, Decimal(0))], burn_uid=42)
    assert out == [(42, MAX_WEIGHT)]


def test_normalize_burn_already_in_list_gets_remainder() -> None:
    pairs = [(0, Decimal("0.5")), (1, Decimal("0.5"))]
    out = normalize_weights(pairs, burn_uid=0)
    assert sum(w for _, w in out) == MAX_WEIGHT
    burn_entry = next(w for uid, w in out if uid == 0)
    other_entry = next(w for uid, w in out if uid == 1)
    assert burn_entry >= other_entry  # burn picks up the floor remainder


def test_normalize_floor_remainder_padded_into_burn_uid() -> None:
    # Seven equal sevenths: MAX_WEIGHT % 7 = 6, so 6 dust units land on burn.
    pairs = [(i, Decimal("1") / Decimal("7")) for i in range(1, 8)]
    out = normalize_weights(pairs, burn_uid=99)
    assert sum(w for _, w in out) == MAX_WEIGHT
    burn_entry = next(w for uid, w in out if uid == 99)
    assert burn_entry == MAX_WEIGHT % 7


def test_underflow_with_under_one_total_pads_burn() -> None:
    """Demand below pool: total miner weight < 1, burn absorbs the gap."""
    pairs = [(1, Decimal("0.4")), (2, Decimal("0.3"))]
    out = normalize_weights(pairs, burn_uid=99)
    assert sum(w for _, w in out) == MAX_WEIGHT
    burn_w = next(w for uid, w in out if uid == 99)
    # Should be roughly (1 - 0.7) of MAX_WEIGHT ≈ 19660 plus any dust.
    assert burn_w >= 19000


@given(
    consumed=st.lists(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("1000"),
            allow_nan=False,
            allow_infinity=False,
            places=2,
        ),
        min_size=0,
        max_size=10,
    ),
    alpha_price=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("100"),
        allow_nan=False,
        allow_infinity=False,
        places=4,
    ),
    emissions=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("10000"),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    ),
)
@settings(max_examples=80)
def test_property_u16_vector_sums_to_max_weight(
    consumed: list[Decimal],
    alpha_price: Decimal,
    emissions: Decimal,
) -> None:
    miners = [MinerEpochData(hotkey=f"M{i:02d}", consumed_usd=v) for i, v in enumerate(consumed)]
    if miners:
        result = compute_epoch_weights(
            miners, alpha_price_usd=alpha_price, emissions_alpha=emissions
        )
        pairs = [(i, m.weight) for i, m in enumerate(result.miners)]
    else:
        pairs = []
    u16 = normalize_weights(pairs, burn_uid=99)
    assert sum(w for _, w in u16) == MAX_WEIGHT
