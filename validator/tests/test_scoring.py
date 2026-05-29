"""Tests for the per-miner score aggregation."""

from __future__ import annotations

from gm_validator.scoring import MinerScore, score


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


def test_surcharges_summed_into_score() -> None:
    rows = [
        _row("A", 100, surcharge=10),
        _row("A", 0, surcharge=5),
    ]
    s = score(rows)
    assert s["A"].earnings_ndollars == 100
    assert s["A"].surcharge_ndollars == 15


def test_dataclass_defaults_are_independent_instances() -> None:
    """MinerScore.per_product must use default_factory so instances do not share state."""
    a = MinerScore(miner_id="A")
    b = MinerScore(miner_id="B")
    a.per_product[("anthropic", "x")] = 1
    assert b.per_product == {}
