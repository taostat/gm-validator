"""Property tests on the scoring + weight normalisation."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from gm_validator.scoring import MinerScore, normalise_weights, score


def _row(miner_id: str, earnings: int, surcharge: int = 0) -> dict:
    return {
        "epoch_id": 1,
        "miner_id": miner_id,
        "product": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "totals": {"input_tokens": 0, "output_tokens": 0},
        "earnings_pdollars": str(earnings),
        "surcharge_pdollars": str(surcharge),
        "successful_requests": 1,
        "failed_requests": 0,
        "raw_record_count": 1,
        "raw_hash": "0" * 64,
    }


def test_sums_per_miner_across_products() -> None:
    rows = [
        _row("A", 100),
        _row("A", 50),
        _row("B", 25),
    ]
    s = score(rows)
    assert s["A"].earnings_pdollars == 150
    assert s["B"].earnings_pdollars == 25


def test_normalise_weights_sum_to_one_when_total_positive() -> None:
    rows = [_row("A", 700), _row("B", 300)]
    s = normalise_weights(score(rows))
    assert abs(s["A"].weight + s["B"].weight - 1.0) < 1e-12
    assert abs(s["A"].weight - 0.7) < 1e-12


def test_normalise_weights_all_zero_when_no_earnings() -> None:
    rows = [_row("A", 0), _row("B", 0)]
    s = normalise_weights(score(rows))
    assert s["A"].weight == 0.0
    assert s["B"].weight == 0.0


def test_surcharges_included_in_weight() -> None:
    rows = [
        _row("A", 100, surcharge=0),
        _row("B", 0, surcharge=100),
    ]
    s = normalise_weights(score(rows))
    assert abs(s["A"].weight - 0.5) < 1e-12
    assert abs(s["B"].weight - 0.5) < 1e-12


@given(
    earnings=st.lists(
        st.integers(min_value=0, max_value=10**18),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=80)
def test_normalised_weights_sum_to_one_or_zero(earnings: list[int]) -> None:
    rows = [_row(f"M{i:02d}", e) for i, e in enumerate(earnings)]
    s = normalise_weights(score(rows))
    total = sum(score_.weight for score_ in s.values())
    if sum(earnings) > 0:
        assert abs(total - 1.0) < 1e-9
    else:
        assert total == 0.0


def test_dataclass_defaults_are_independent_instances() -> None:
    """MinerScore.per_product must use default_factory so instances do not share state."""
    a = MinerScore(miner_id="A")
    b = MinerScore(miner_id="B")
    a.per_product[("anthropic", "x")] = 1
    assert b.per_product == {}
