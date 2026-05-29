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
        "emissions_alpha": "295.999999920",
        "price_block_height": 12345,
        "price_alpha_source": "chain",
        "price_tao_usd_source": "taostats",
        "emissions_alpha_source": "chain",
        "finalizer_version": "0.3.0",
    }


def test_load_decimal_strings_parsed_with_full_precision(tmp_path: pathlib.Path) -> None:
    payload = _sample_summary()
    path = tmp_path / "epoch_summary.json"
    path.write_text(json.dumps(payload))
    summary = load_epoch_summary(str(path))
    assert summary.epoch_id == 7
    assert summary.alpha_price_usd == Decimal("22.5061728000")
    assert summary.tao_price_usd == Decimal("450.123456")
    assert summary.emissions_alpha == Decimal("295.999999920")
    assert summary.emissions_alpha_source == "chain"
    assert summary.price_block_height == 12345
    assert summary.price_alpha_source == "chain"


def test_load_pre_chain_read_summary_leaves_emissions_alpha_none(tmp_path: pathlib.Path) -> None:
    """A pre-chain-read artifact (no ``emissions_alpha``) parses cleanly.

    The parser leaves the field None; ``scoring.compute_weights`` raises
    ``StaleEpochSummaryError`` so ``Validator.process_once`` defers the
    epoch rather than fabricating a pool denominator.
    """
    payload = _sample_summary()
    del payload["emissions_alpha"]
    del payload["emissions_alpha_source"]
    path = tmp_path / "epoch_summary.json"
    path.write_text(json.dumps(payload))
    summary = load_epoch_summary(str(path))
    assert summary.emissions_alpha is None
    assert summary.emissions_alpha_source is None


def test_missing_file_raises(tmp_path: pathlib.Path) -> None:
    """Every finalizer emits epoch_summary.json — a missing file is an
    upstream regression, not a legacy-epoch signal."""
    with pytest.raises(FileNotFoundError):
        load_epoch_summary(str(tmp_path / "absent.json"))


def test_summary_is_frozen() -> None:
    summary = EpochSummary.model_validate(_sample_summary())
    with pytest.raises(ValidationError):
        summary.epoch_id = 999  # type: ignore[misc]


def test_path_helper(tmp_path: pathlib.Path) -> None:
    assert epoch_summary_path(str(tmp_path)).endswith("/epoch_summary.json")
