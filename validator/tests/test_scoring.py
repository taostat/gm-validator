"""Tests for the per-miner score aggregation."""

from __future__ import annotations

import pytest

from gm_validator.scoring import MalformedArtifactError, MinerScore, score


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


def test_accepts_int_money_fields() -> None:
    """Money may arrive as JSON ints, not only strings."""
    row = _row("A", 100, surcharge=10)
    row["earnings_ndollars"] = 100
    row["surcharge_ndollars"] = 10
    s = score([row])
    assert s["A"].earnings_ndollars == 100
    assert s["A"].surcharge_ndollars == 10


def test_missing_earnings_field_raises() -> None:
    row = _row("A", 100)
    del row["earnings_ndollars"]
    with pytest.raises(MalformedArtifactError, match="earnings_ndollars"):
        score([row])


def test_missing_surcharge_field_raises() -> None:
    row = _row("A", 100)
    del row["surcharge_ndollars"]
    with pytest.raises(MalformedArtifactError, match="surcharge_ndollars"):
        score([row])


def test_null_money_field_raises() -> None:
    row = _row("A", 100)
    row["earnings_ndollars"] = None
    with pytest.raises(MalformedArtifactError, match="earnings_ndollars"):
        score([row])


def test_non_int_money_string_raises() -> None:
    row = _row("A", 100)
    row["earnings_ndollars"] = "not-a-number"
    with pytest.raises(MalformedArtifactError, match="earnings_ndollars"):
        score([row])


def test_float_money_field_raises() -> None:
    row = _row("A", 100)
    row["surcharge_ndollars"] = 1.5
    with pytest.raises(MalformedArtifactError, match="surcharge_ndollars"):
        score([row])


def test_bool_money_field_raises() -> None:
    """bool is an int subclass in Python; it must still be rejected."""
    row = _row("A", 100)
    row["earnings_ndollars"] = True
    with pytest.raises(MalformedArtifactError, match="earnings_ndollars"):
        score([row])


def test_null_count_raises() -> None:
    row = _row("A", 100)
    row["successful_requests"] = None
    with pytest.raises(MalformedArtifactError, match="successful_requests"):
        score([row])


def test_non_int_count_raises() -> None:
    row = _row("A", 100)
    row["failed_requests"] = "oops"
    with pytest.raises(MalformedArtifactError, match="failed_requests"):
        score([row])


def test_absent_count_defaults_to_zero() -> None:
    """An absent count key is permitted and defaults to 0."""
    row = _row("A", 100)
    del row["successful_requests"]
    del row["failed_requests"]
    s = score([row])
    assert s["A"].successful_requests == 0
    assert s["A"].failed_requests == 0


def test_non_string_miner_id_raises() -> None:
    """A numeric miner_id would never match the hotkey lookup."""
    row = _row("A", 100)
    row["miner_id"] = 12345
    with pytest.raises(MalformedArtifactError, match="miner_id"):
        score([row])


def _no_miner_row() -> dict:
    """A gateway no-miner failure row: empty miner_id, zero earnings, failure."""
    row = _row("", 0, surcharge=0)
    row["successful_requests"] = 0
    row["failed_requests"] = 1
    return row


def test_empty_miner_id_row_skipped_not_raised() -> None:
    """A no-miner row (empty miner_id) is skipped, not fatal to the epoch."""
    row = _no_miner_row()
    s = score([row])
    assert s == {}


def test_missing_miner_id_row_skipped() -> None:
    row = _no_miner_row()
    del row["miner_id"]
    s = score([row])
    assert s == {}


def test_null_miner_id_row_skipped() -> None:
    row = _no_miner_row()
    row["miner_id"] = None
    s = score([row])
    assert s == {}


def test_empty_miner_row_does_not_stall_valid_rows() -> None:
    """The benign empty-miner row is skipped while the valid rows still score."""
    rows = [
        _row("A", 100),
        _no_miner_row(),
        _row("B", 25),
    ]
    s = score(rows)
    assert set(s) == {"A", "B"}
    assert s["A"].earnings_ndollars == 100
    assert s["B"].earnings_ndollars == 25


def test_empty_miner_id_warns(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="gm_validator.scoring"):
        score([_no_miner_row()])
    assert any("no-miner row" in r.message for r in caplog.records)


def test_empty_miner_row_with_nonzero_earnings_raises() -> None:
    """A no-miner row carrying money is corruption, not a benign skip."""
    row = _no_miner_row()
    row["earnings_ndollars"] = "100"
    with pytest.raises(MalformedArtifactError, match="non-zero"):
        score([row])
