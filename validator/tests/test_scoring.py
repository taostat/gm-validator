"""Tests on score aggregation + the bm-pattern emission cap.

The cap math mirrors the deep-dive at
``gm/docs/research/bm-validator-deep-dive.md`` §3 (under-/over-
subscription and the worked example) and the bm-validator reference at
``Blockmachine/blockmachine_playground/validator/common/scoring/weights.py``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gm_validator.scoring import (
    MINER_EMISSION_PCT_DEFAULT,
    EpochCapSummary,
    MinerScore,
    apply_emission_cap,
    score,
)


def _row(miner_id: str, earnings: int, surcharge: int = 0) -> dict:
    return {
        "epoch_id": 1,
        "miner_id": miner_id,
        "product": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "totals": {"input_tokens": 0, "output_tokens": 0},
        "earnings_ndollars": str(earnings),
        "surcharge_ndollars": str(surcharge),
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
    assert s["A"].earnings_ndollars == 150
    assert s["B"].earnings_ndollars == 25


def test_dataclass_defaults_are_independent_instances() -> None:
    """MinerScore.per_product must use default_factory so instances do not share state."""
    a = MinerScore(miner_id="A")
    b = MinerScore(miner_id="B")
    a.per_product[("anthropic", "x")] = 1
    assert b.per_product == {}


# --- apply_emission_cap ---------------------------------------------------


def _cap(
    scores: dict[str, MinerScore],
    *,
    alpha_price_usd: str = "0.50",
    emissions_alpha: str = "50",
    miner_emission_pct: str | None = None,
) -> EpochCapSummary:
    return apply_emission_cap(
        scores,
        alpha_price_usd=Decimal(alpha_price_usd),
        emissions_alpha=Decimal(emissions_alpha),
        miner_emission_pct=(
            Decimal(miner_emission_pct)
            if miner_emission_pct is not None
            else MINER_EMISSION_PCT_DEFAULT
        ),
    )


def test_cap_under_subscribed_leaves_burn_fraction() -> None:
    """Deep-dive §3.2 worked example A: $9 consumed, $10.25 pool → scale=1, burn>0."""
    # 1 USD = 1e9 ndollars. $5, $3, $1.
    scores = {
        "A": MinerScore(miner_id="A", earnings_ndollars=5 * 10**9),
        "B": MinerScore(miner_id="B", earnings_ndollars=3 * 10**9),
        "C": MinerScore(miner_id="C", earnings_ndollars=1 * 10**9),
    }
    summary = _cap(scores)

    # miner_pool = 50 × 0.41 = 20.5α, $0.50/α → $10.25.
    assert summary.miner_pool_alpha == Decimal("20.5")
    assert summary.miner_pool_usd == Decimal("10.25")
    assert summary.total_consumed_usd == Decimal("9")
    # Under-subscribed → scale stays at 1.
    assert summary.scale == Decimal(1)
    # Per-miner weights: payout_alpha = consumed / 0.50 → 10, 6, 2 α.
    # weight = payout_alpha / 20.5 → 0.4878..., 0.2927..., 0.0976...
    assert scores["A"].weight == pytest.approx(10 / 20.5, abs=1e-12)
    assert scores["B"].weight == pytest.approx(6 / 20.5, abs=1e-12)
    assert scores["C"].weight == pytest.approx(2 / 20.5, abs=1e-12)
    # Σ weights = 18 / 20.5 ≈ 0.878; burn = 2.5/20.5 ≈ 0.122.
    assert summary.submitted_weight_sum == pytest.approx(18 / 20.5, abs=1e-12)
    assert summary.burn_fraction == pytest.approx(2.5 / 20.5, abs=1e-12)


def test_cap_over_subscribed_pays_pool_exactly() -> None:
    """Deep-dive §3.3 worked example B: $15 consumed, $10.25 pool → scale<1, burn≈0."""
    scores = {
        "A": MinerScore(miner_id="A", earnings_ndollars=10 * 10**9),
        "B": MinerScore(miner_id="B", earnings_ndollars=5 * 10**9),
    }
    summary = _cap(scores)

    assert summary.miner_pool_usd == Decimal("10.25")
    assert summary.total_consumed_usd == Decimal("15")
    assert summary.scale == Decimal("10.25") / Decimal("15")
    # Σ payout_alpha = miner_pool_alpha exactly (scale clamps to pool).
    assert summary.submitted_weight_sum == pytest.approx(1.0, abs=1e-12)
    assert summary.burn_fraction == pytest.approx(0.0, abs=1e-12)


def test_cap_zero_consumed_burns_everything() -> None:
    """No miner activity → every weight is zero, burn = 1.0."""
    scores = {
        "A": MinerScore(miner_id="A"),
        "B": MinerScore(miner_id="B"),
    }
    summary = _cap(scores)
    assert summary.total_consumed_usd == Decimal(0)
    assert summary.scale == Decimal(1)
    assert scores["A"].weight == 0.0
    assert scores["B"].weight == 0.0
    assert summary.submitted_weight_sum == pytest.approx(0.0, abs=1e-12)
    assert summary.burn_fraction == pytest.approx(1.0, abs=1e-12)


def test_cap_invariant_weight_sum_plus_burn_equals_one() -> None:
    """Σ weight_i + burn_fraction ≈ 1.0 in all subscription regimes."""
    scores = {
        "A": MinerScore(miner_id="A", earnings_ndollars=2 * 10**9),
        "B": MinerScore(miner_id="B", earnings_ndollars=4 * 10**9),
    }
    summary = _cap(scores)
    total = sum(s.weight for s in scores.values()) + summary.burn_fraction
    assert total == pytest.approx(1.0, abs=1e-12)


def test_cap_rejects_zero_alpha_price() -> None:
    with pytest.raises(ValueError, match="alpha_price_usd"):
        apply_emission_cap(
            {"A": MinerScore(miner_id="A", earnings_ndollars=1)},
            alpha_price_usd=Decimal(0),
            emissions_alpha=Decimal(1),
        )


def test_cap_rejects_zero_emissions() -> None:
    with pytest.raises(ValueError, match="emissions_alpha"):
        apply_emission_cap(
            {"A": MinerScore(miner_id="A", earnings_ndollars=1)},
            alpha_price_usd=Decimal(1),
            emissions_alpha=Decimal(0),
        )


def test_cap_surcharges_included_in_consumed() -> None:
    """A miner with surcharge but no earnings still consumes USD."""
    scores = {
        "A": MinerScore(miner_id="A", earnings_ndollars=100 * 10**9),
        "B": MinerScore(miner_id="B", earnings_ndollars=0, surcharge_ndollars=100 * 10**9),
    }
    summary = _cap(scores)
    # Over-subscribed (total $200, pool $10.25) → both miners get equal weight,
    # since they have equal consumed USD.
    assert summary.total_consumed_usd == Decimal("200")
    assert scores["A"].weight == pytest.approx(scores["B"].weight, abs=1e-12)


@given(
    earnings=st.lists(
        st.integers(min_value=0, max_value=10**18),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=80)
def test_property_weight_sum_plus_burn_equals_one(earnings: list[int]) -> None:
    """For any input distribution, Σ weight + burn_fraction ≈ 1.0."""
    scores = {
        f"M{i:02d}": MinerScore(miner_id=f"M{i:02d}", earnings_ndollars=e)
        for i, e in enumerate(earnings)
    }
    summary = apply_emission_cap(
        scores,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
    )
    total = sum(s.weight for s in scores.values()) + summary.burn_fraction
    assert total == pytest.approx(1.0, abs=1e-9)


def test_default_miner_emission_pct_matches_bm() -> None:
    """Anchor the constant — accidental drift here changes economics."""
    assert Decimal("0.41") == MINER_EMISSION_PCT_DEFAULT


# --- regression fixture: known epoch input → expected weight output -------


def test_regression_known_epoch_weights() -> None:
    """Three miners, known earnings, fixed price/emissions → fixed weights.

    Locks the formula direction (deep-dive §3.1):

        scale = min(1.0, miner_pool_usd / total_consumed_usd)

    so future refactors of the cap math don't silently reverse it.
    """
    scores = {
        "miner_alpha": MinerScore(miner_id="miner_alpha", earnings_ndollars=4_000_000_000),
        "miner_beta": MinerScore(miner_id="miner_beta", earnings_ndollars=3_000_000_000),
        "miner_gamma": MinerScore(
            miner_id="miner_gamma",
            earnings_ndollars=2_000_000_000,
            surcharge_ndollars=1_000_000_000,
        ),
    }
    summary = apply_emission_cap(
        scores,
        alpha_price_usd=Decimal("0.50"),
        emissions_alpha=Decimal("50"),
        miner_emission_pct=Decimal("0.41"),
    )

    # $10 consumed, $10.25 pool → still under-subscribed, scale=1.
    assert summary.miner_pool_usd == Decimal("10.25")
    assert summary.total_consumed_usd == Decimal("10")
    assert summary.scale == Decimal(1)
    # payout_alpha = consumed / 0.50 → 8, 6, 6α; weight = payout/20.5.
    assert scores["miner_alpha"].weight == pytest.approx(8 / 20.5, abs=1e-12)
    assert scores["miner_beta"].weight == pytest.approx(6 / 20.5, abs=1e-12)
    assert scores["miner_gamma"].weight == pytest.approx(6 / 20.5, abs=1e-12)
    # 20α paid out, 0.5α burned.
    assert summary.burn_fraction == pytest.approx(0.5 / 20.5, abs=1e-12)
