"""Tests for the finalizer-written epoch_summary.json reader."""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest
from pydantic import ValidationError

from gm_validator.epoch_summary import EpochSummary, epoch_summary_path, load_epoch_summary


def _sample_summary(epoch_id: int = 7) -> dict[str, object]:
    return {
        "epoch_id": epoch_id,
        "finalized_at": "2026-05-27T12:00:00Z",
        "alpha_price_in_tao": "0.0500000000",
        "tao_price_usd": "450.123456",
        "alpha_price_usd": "22.5061728000",
        "price_block_height": 12345,
        "price_alpha_source": "chain",
        "price_tao_usd_source": "taostats",
        "finalizer_version": "0.2.0",
    }


def test_load_decimal_strings_parsed_with_full_precision(tmp_path: pathlib.Path) -> None:
    payload = _sample_summary()
    path = tmp_path / "epoch_summary.json"
    path.write_text(json.dumps(payload))
    summary = load_epoch_summary(str(path))
    assert summary is not None
    assert summary.epoch_id == 7
    assert summary.alpha_price_usd == Decimal("22.5061728000")
    assert summary.tao_price_usd == Decimal("450.123456")
    assert summary.price_block_height == 12345
    assert summary.price_alpha_source == "chain"


def test_missing_file_returns_none(tmp_path: pathlib.Path) -> None:
    """The legacy-epoch path: no file means fall back to naive scoring."""
    summary = load_epoch_summary(str(tmp_path / "absent.json"))
    assert summary is None


def test_summary_is_frozen() -> None:
    summary = EpochSummary.model_validate(_sample_summary())
    with pytest.raises(ValidationError):
        summary.epoch_id = 999  # type: ignore[misc]


def test_path_helper(tmp_path: pathlib.Path) -> None:
    assert epoch_summary_path(str(tmp_path)).endswith("/epoch_summary.json")
