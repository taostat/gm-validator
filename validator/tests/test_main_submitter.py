"""Submitter-selection guard in ``main``.

A production pod that forgets to mount ``BITTENSOR_HOTKEY_SEED`` must
fail fast at startup rather than silently degrade to the in-memory
``MockSubmitter`` and process epochs forever without ever signing or
submitting on-chain weights. The mock path is selected only when
``BITTENSOR_MOCK`` is set explicitly; with mock off, a missing or blank
seed raises.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from gm_validator.bittensor_adapter import MockSubmitter
from gm_validator.config import ValidatorConfig
from gm_validator.main import HotkeyNotConfiguredError, _build_submitter, _use_mock_submitter


def _config(
    *,
    mock: bool,
    seed: str | None,
    wallet_name: str | None = None,
    wallet_hotkey: str | None = None,
) -> ValidatorConfig:
    return ValidatorConfig(
        s3_bucket="b",
        s3_prefix="v1",
        s3_endpoint_url=None,
        aws_region="us-east-1",
        s3_anonymous=False,
        local_mirror_dir="unused-mirror-dir",
        mirror_retention_epochs=10,
        blocks_per_epoch=360,
        finalized_lookback_epochs=3,
        bittensor_netuid=42,
        bittensor_endpoint=None,
        bittensor_hotkey_seed=seed,
        bittensor_wallet_name=wallet_name,
        bittensor_wallet_hotkey=wallet_hotkey,
        bittensor_wallet_path=None,
        bittensor_mock=mock,
        subtensor_connect_timeout_secs=30,
        subtensor_rpc_timeout_secs=30,
        poll_interval_secs=1,
        metrics_bind=None,
        subnet_owner_uid=99,
        weight_earnings_multiplier=Decimal(1),
    )


def test_mock_mode_selects_mock_submitter() -> None:
    config = _config(mock=True, seed=None)
    assert _use_mock_submitter(config) is True
    assert isinstance(_build_submitter(config), MockSubmitter)


def test_mock_mode_ignores_present_seed() -> None:
    """Explicit mock mode wins even if a seed happens to be set."""
    config = _config(mock=True, seed="0x" + "ab" * 32)
    assert _use_mock_submitter(config) is True
    assert isinstance(_build_submitter(config), MockSubmitter)


def test_real_mode_missing_seed_fails_fast() -> None:
    config = _config(mock=False, seed=None)
    with pytest.raises(HotkeyNotConfiguredError, match="BITTENSOR_HOTKEY_SEED"):
        _use_mock_submitter(config)
    with pytest.raises(HotkeyNotConfiguredError, match="BITTENSOR_HOTKEY_SEED"):
        _build_submitter(config)


def test_real_mode_blank_seed_fails_fast() -> None:
    config = _config(mock=False, seed="   ")
    with pytest.raises(HotkeyNotConfiguredError, match="BITTENSOR_HOTKEY_SEED"):
        _build_submitter(config)


def test_real_mode_with_seed_does_not_use_mock() -> None:
    config = _config(mock=False, seed="0x" + "ab" * 32)
    assert _use_mock_submitter(config) is False


def test_real_mode_with_wallet_and_no_seed_does_not_use_mock() -> None:
    """A configured on-disk wallet is a valid hotkey source on its own —
    no BITTENSOR_HOTKEY_SEED required."""
    config = _config(mock=False, seed=None, wallet_name="cold", wallet_hotkey="hot")
    assert _use_mock_submitter(config) is False


def test_real_mode_partial_wallet_without_seed_fails_fast() -> None:
    """A wallet name without a hotkey (and no seed) is not a usable source."""
    config = _config(mock=False, seed=None, wallet_name="cold", wallet_hotkey=None)
    with pytest.raises(HotkeyNotConfiguredError):
        _use_mock_submitter(config)


def test_default_config_without_mock_or_seed_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dangerous default — mock unset, seed unset — must raise, not
    silently mock. Guards against a regression to the old fallback."""
    config = dataclasses.replace(
        _config(mock=False, seed=None),
        bittensor_mock=False,
        bittensor_hotkey_seed=None,
    )
    with pytest.raises(HotkeyNotConfiguredError):
        _build_submitter(config)
