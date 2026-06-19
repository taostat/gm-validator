"""Unit + property tests for the cap+burn math."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gm_validator import alpha_economics
from gm_validator.alpha_economics import (
    MAX_WEIGHT,
    MINER_EMISSION_PCT,
    MinerEpochData,
    compute_epoch_weights,
    normalize_weights,
)


def _capture_accuracy(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Capture the next ``record_payment_accuracy`` call's kwargs."""
    captured: dict[str, float] = {}

    def _record(**kwargs: float) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(alpha_economics.metrics, "record_payment_accuracy", _record)
    return captured


def _miners(values: list[tuple[str, str]]) -> list[MinerEpochData]:
    return [MinerEpochData(hotkey=h, consumed_usd=Decimal(v)) for h, v in values]


def _pool_usd(alpha_price: Decimal, emissions: Decimal) -> Decimal:
    return emissions * MINER_EMISSION_PCT * alpha_price


def test_under_subscribed_weights_match_consumed_over_pool() -> None:
    """Demand $9 against a $10.25 pool: weight_i = consumed_i / pool_usd."""
    miners = _miners([("A", "5"), ("B", "3"), ("C", "1")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    pool_usd = _pool_usd(Decimal("0.50"), Decimal("50"))
    assert result.total_consumed_usd == Decimal(9)
    assert result.pool_usd_total == pool_usd
    for miner in result.miners:
        assert miner.weight == miner.consumed_usd / pool_usd
    # Sum of miner weights < 1: residue lands on burn in normalize_weights.
    total_miner_weight = sum((m.weight for m in result.miners), Decimal(0))
    assert total_miner_weight < Decimal(1)


def test_over_subscribed_weights_sum_above_one() -> None:
    """Demand $15 against a $10.25 pool: per-miner weight = consumed/pool,
    aggregate sum > 1, normalize_weights renorms downstream."""
    miners = _miners([("A", "10"), ("B", "5")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    pool_usd = _pool_usd(Decimal("0.50"), Decimal("50"))
    assert result.total_consumed_usd == Decimal(15)
    assert result.pool_usd_total == pool_usd
    total_miner_weight = sum((m.weight for m in result.miners), Decimal(0))
    assert total_miner_weight > Decimal(1)
    for miner in result.miners:
        assert miner.weight == miner.consumed_usd / pool_usd


def test_zero_consumption_all_weights_zero() -> None:
    """Zero demand: every miner weight is 0; normalize_weights routes
    the full MAX_WEIGHT to burn."""
    miners = _miners([("A", "0"), ("B", "0")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    assert result.total_consumed_usd == Decimal(0)
    for m in result.miners:
        assert m.weight == Decimal(0)


def test_single_miner_demand_above_pool_renorms_to_full() -> None:
    """A single miner whose demand exceeds the pool ends up with the
    entire MAX_WEIGHT after normalize_weights renorms."""
    miners = _miners([("solo", "100")])
    result = compute_epoch_weights(
        miners,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    pairs = [(0, result.miners[0].weight)]
    u16 = normalize_weights(pairs, burn_uid=99)
    weights_by_uid = dict(u16)
    assert weights_by_uid[0] == MAX_WEIGHT
    assert 99 not in weights_by_uid


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


def test_tiny_positive_share_floors_to_at_least_one() -> None:
    """A miner whose share is below 1/MAX_WEIGHT must still get weight >= 1,
    not truncate to 0 and lose its whole emission to burn."""
    # Testnet shape: one miner earns ~$0.000064 against a ~$2415 pool.
    share = Decimal("0.000064") / Decimal("2415")
    assert share * MAX_WEIGHT < Decimal(1)  # would truncate to 0 under int()
    out = normalize_weights([(7, share)], burn_uid=99)
    weights_by_uid = dict(out)
    assert weights_by_uid[7] >= 1
    assert sum(w for _, w in out) == MAX_WEIGHT
    assert weights_by_uid[99] == MAX_WEIGHT - weights_by_uid[7]


def test_zero_score_miner_stays_at_weight_zero() -> None:
    """The floor applies only to strictly positive scores; a miner with a
    zero share is dropped, not paid a courtesy unit."""
    out = normalize_weights([(7, Decimal(0)), (8, Decimal("0.5"))], burn_uid=99)
    weights_by_uid = dict(out)
    assert 7 not in weights_by_uid
    assert weights_by_uid[8] >= 1
    assert sum(w for _, w in out) == MAX_WEIGHT


def test_many_tiny_miners_each_floor_to_one_and_sum_holds() -> None:
    """Many sub-threshold positive shares each get >= 1 and the vector
    still sums to exactly MAX_WEIGHT with no burn double-count."""
    pairs = [(uid, Decimal("0.000064") / Decimal("2415")) for uid in range(1, 21)]
    out = normalize_weights(pairs, burn_uid=99)
    weights_by_uid = dict(out)
    for uid in range(1, 21):
        assert weights_by_uid[uid] >= 1
    assert sum(w for _, w in out) == MAX_WEIGHT
    # Burn absorbs the rest exactly once.
    assert weights_by_uid[99] == MAX_WEIGHT - sum(weights_by_uid[uid] for uid in range(1, 21))


def test_oversubscribed_floor_reclaims_overflow_to_max_weight() -> None:
    """Over-subscription that floors many tiny shares up to 1 must reclaim the
    overflow from the heavy miners, not exceed MAX_WEIGHT."""
    pairs = [(0, Decimal("1000"))] + [(uid, Decimal("0.0000001")) for uid in range(1, 2000)]
    out = normalize_weights(pairs, burn_uid=99)
    weights_by_uid = dict(out)
    assert sum(w for _, w in out) == MAX_WEIGHT
    for uid in range(1, 2000):
        assert weights_by_uid[uid] >= 1
    # Reclaim drains only the overflow from the heavy miner, which keeps the
    # bulk of MAX_WEIGHT (it loses at most ~the dust count of units).
    assert weights_by_uid[0] > MAX_WEIGHT - 2000


def test_oversubscribed_reclaim_preserves_miner_ranking() -> None:
    """Reclaiming the overflow must not invert payout order: a larger share
    never ends up below a smaller one after the dust floor is funded."""
    pairs = [(0, Decimal("0.50")), (1, Decimal("0.49"))]
    pairs += [(uid, Decimal("0.0000001")) for uid in range(2, 4000)]
    out = dict(normalize_weights(pairs, burn_uid=99_999))
    assert out[0] >= out[1]  # 0.50 share >= 0.49 share
    for uid in range(2, 4000):
        assert out[uid] >= 1
        # Dust miners never out-earn a genuine large share.
        assert out[uid] <= out[1]
    assert sum(out.values()) == MAX_WEIGHT


def test_reclaim_tiebreak_shaves_the_genuinely_smaller_share() -> None:
    """Two miners that quantize to the same u16 value but differ in their
    Decimal share: the reclaim residual must come off the smaller share, never
    invert the pair by input order."""
    pairs = [
        (0, Decimal("0.5")),
        (1, Decimal("0.500001")),
        (2, Decimal("0.0000001")),
        (3, Decimal("0.0000001")),
    ]
    out = dict(normalize_weights(pairs, burn_uid=99))
    assert out[1] >= out[0]  # 0.500001 share never below the 0.5 share
    assert sum(out.values()) == MAX_WEIGHT
    assert out[2] >= 1
    assert out[3] >= 1


def test_more_positive_miners_than_max_weight_raises() -> None:
    pairs = [(uid, Decimal(1)) for uid in range(MAX_WEIGHT + 1)]
    with pytest.raises(ValueError, match="cannot floor"):
        normalize_weights(pairs, burn_uid=99)


def test_normal_magnitude_shares_unaffected_by_floor() -> None:
    """Regression: ordinary shares quantize exactly as before the floor."""
    pairs = [(1, Decimal("0.5")), (2, Decimal("0.3")), (3, Decimal("0.2"))]
    out = normalize_weights(pairs, burn_uid=99)
    weights_by_uid = dict(out)
    assert weights_by_uid[1] == int(Decimal("0.5") * MAX_WEIGHT)
    assert weights_by_uid[2] == int(Decimal("0.3") * MAX_WEIGHT)
    assert weights_by_uid[3] == int(Decimal("0.2") * MAX_WEIGHT)
    assert sum(w for _, w in out) == MAX_WEIGHT


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


@given(
    shares=st.lists(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("1000"),
            allow_nan=False,
            allow_infinity=False,
            places=8,
        ),
        min_size=1,
        max_size=300,
    ),
)
@settings(max_examples=120)
def test_property_floor_holds_and_sum_exact(shares: list[Decimal]) -> None:
    """Across mixed tiny/large share vectors: the u16 vector sums to exactly
    MAX_WEIGHT and every strictly-positive share keeps at least one unit."""
    pairs = [(i, s) for i, s in enumerate(shares)]
    out = normalize_weights(pairs, burn_uid=99_999)
    assert sum(w for _, w in out) == MAX_WEIGHT
    weights_by_uid = dict(out)
    for i, s in enumerate(shares):
        if s > 0:
            assert weights_by_uid.get(i, 0) >= 1


@given(
    shares=st.lists(
        st.decimals(
            min_value=Decimal("0.00000001"),
            max_value=Decimal("1000"),
            allow_nan=False,
            allow_infinity=False,
            places=8,
        ),
        min_size=1,
        max_size=400,
    ),
)
@settings(max_examples=120)
def test_property_reclaim_preserves_ranking(shares: list[Decimal]) -> None:
    """A larger original share never receives fewer u16 units than a smaller
    one, even when overflow reclaim shaves the heaviest weights and even when
    the inputs arrive in arbitrary order."""
    pairs = [(i, s) for i, s in enumerate(shares)]
    out = dict(normalize_weights(pairs, burn_uid=10_000_000))
    # Walk uids in descending true-share order; u16 weights must not increase.
    by_share = sorted(range(len(shares)), key=lambda i: shares[i], reverse=True)
    ranked = [out[i] for i in by_share]
    for a, b in itertools.pairwise(ranked):
        assert a >= b
    assert sum(out.values()) == MAX_WEIGHT


@pytest.mark.parametrize(
    "pairs",
    [
        [(1, Decimal("0.5")), (2, Decimal("0.2")), (3, Decimal("0.0000001"))],
        # Over-subscribed: drives the reclaim path with many sub-quantum miners.
        [(i, Decimal("1") / Decimal("7")) for i in range(1, 8)],
        [(i, Decimal("0.0000001")) for i in range(1, 50)],
        [],
        [(1, Decimal(0)), (2, Decimal(0))],
    ],
)
def test_payment_accuracy_gauges_do_not_alter_weights(
    monkeypatch: pytest.MonkeyPatch,
    pairs: list[tuple[int, Decimal]],
) -> None:
    """The submitted vector is byte-identical whether the live gauge recorder
    runs or is stubbed to a no-op — observation must never feed back."""
    live = normalize_weights(list(pairs), burn_uid=99_999)

    monkeypatch.setattr(alpha_economics.metrics, "record_payment_accuracy", lambda **_: None)
    stubbed = normalize_weights(list(pairs), burn_uid=99_999)

    assert live == stubbed


def test_payment_accuracy_values_on_crafted_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual, below-quantum count, and signed floored weight match the
    intended-vs-submitted comparison on a hand-built share vector."""
    captured = _capture_accuracy(monkeypatch)
    # total < 1 so renorm == 1 and intended_i == weight_i directly.
    pairs = [
        (1, Decimal("0.5")),
        (2, Decimal("0.2")),
        (3, Decimal("0.0000001")),  # below the 1/65535 quantum
    ]
    out = dict(normalize_weights(list(pairs), burn_uid=99_999))

    # Only miner 3 is sub-quantum; it is floored up to one u16 unit.
    assert captured["miners_below_quantum"] == 1
    assert out[3] == 1

    quantum = Decimal(1) / MAX_WEIGHT
    expected_floored = float(Decimal(1) - Decimal("0.0000001") * MAX_WEIGHT)
    assert captured["floored_weight_total"] == pytest.approx(expected_floored)
    assert captured["floored_weight_total"] > 0  # floored up == overpay

    expected_residual = float(
        sum(abs(intended - Decimal(out[uid]) / MAX_WEIGHT) for uid, intended in pairs)
    )
    assert captured["quantization_residual"] == pytest.approx(expected_residual)
    assert Decimal("0.0000001") < quantum  # sanity on the crafted sub-quantum miner


def test_payment_accuracy_measures_final_u16_when_burn_collides_with_miner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When burn_uid is also a scored miner, the under-subscribed burn dust is
    merged into that miner's u16. The gauge must observe the final (post-merge)
    submitted weight, not the pre-merge miner share."""
    captured = _capture_accuracy(monkeypatch)
    # Under-subscribed (total 0.7 < 1): burn dust merges into uid 1.
    pairs = [(1, Decimal("0.4")), (2, Decimal("0.3"))]
    out = dict(normalize_weights(list(pairs), burn_uid=1))

    # uid 1 carries its own share plus the whole burn remainder.
    assert out[1] > int(Decimal("0.4") * MAX_WEIGHT)
    expected_residual = float(
        sum(abs(intended - Decimal(out[uid]) / MAX_WEIGHT) for uid, intended in pairs)
    )
    assert captured["quantization_residual"] == pytest.approx(expected_residual)


def test_payment_accuracy_empty_epoch_is_zero_no_nan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The all-burn empty epoch reports residual 0 and no NaN."""
    import math

    captured = _capture_accuracy(monkeypatch)
    out = normalize_weights([], burn_uid=42)

    assert out == [(42, MAX_WEIGHT)]
    assert captured["quantization_residual"] == 0.0
    assert captured["miners_below_quantum"] == 0
    assert captured["floored_weight_total"] == 0.0
    assert not math.isnan(captured["quantization_residual"])


def test_payment_accuracy_all_zero_weight_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vector of only zero-weight miners burns the full pool and reports
    zero accuracy gauges without dividing by an empty denominator."""
    captured = _capture_accuracy(monkeypatch)
    out = normalize_weights([(1, Decimal(0)), (2, Decimal(0))], burn_uid=7)

    assert out == [(7, MAX_WEIGHT)]
    assert captured["quantization_residual"] == 0.0
    assert captured["miners_below_quantum"] == 0
    assert captured["floored_weight_total"] == 0.0
