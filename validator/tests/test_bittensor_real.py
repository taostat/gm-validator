"""Tests for the real bittensor weight submitter.

The ``bittensor`` SDK is mocked at the module boundary: ``RealSubmitter``
does ``import bittensor`` inside its methods, so injecting a fake module
into ``sys.modules`` lets us exercise the full submission path without a
wallet on disk or a chain to talk to.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from gm_validator.bittensor_real import RealSubmitter, WeightSubmissionError


class _FakeHotkey:
    ss58_address = "5HotKeyAddress"


class _FakeWallet:
    def __init__(self, name: str, hotkey: str) -> None:
        self.name = name
        self.hotkey_name = hotkey
        self.hotkey = _FakeHotkey()


class _FakeSubtensor:
    """Records set_weights calls; returns a configurable result."""

    def __init__(self, network: str | None = None) -> None:
        self.network = network
        self.calls: list[dict[str, Any]] = []
        self.result: tuple[bool, str] = (True, "included")
        self.raise_on_set: Exception | None = None

    def set_weights(self, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append(kwargs)
        if self.raise_on_set is not None:
            raise self.raise_on_set
        return self.result


def _install_fake_bittensor(monkeypatch: pytest.MonkeyPatch) -> _FakeSubtensor:
    """Inject a fake ``bittensor`` module; return its shared subtensor."""
    subtensor = _FakeSubtensor()
    module = types.ModuleType("bittensor")
    module.Wallet = _FakeWallet  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)
    return subtensor


def test_constructor_opens_wallet_and_subtensor(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(
        netuid=42,
        endpoint="wss://entrypoint.example",
        wallet_name="validator",
        wallet_hotkey="default",
    )
    assert submitter._netuid == 42


def test_constructor_wraps_wallet_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("bittensor")

    def _boom(*_: object, **__: object) -> object:
        raise OSError("keyfile missing")

    module.Wallet = _boom  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: _FakeSubtensor()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)

    with pytest.raises(WeightSubmissionError, match="keyfile missing"):
        RealSubmitter(netuid=1, endpoint=None, wallet_name="v", wallet_hotkey="h")


def test_submit_calls_set_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")

    submitter.submit(netuid=42, uids=[0, 3], weights=[0.4, 0.6], epoch_id=7)

    assert len(subtensor.calls) == 1
    call = subtensor.calls[0]
    assert call["netuid"] == 42
    assert call["uids"] == [0, 3]
    assert call["weights"] == [0.4, 0.6]
    assert call["wait_for_inclusion"] is True


def test_submit_rejects_netuid_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError, match="netuid mismatch"):
        submitter.submit(netuid=99, uids=[0], weights=[1.0], epoch_id=1)


def test_submit_rejects_length_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError, match="length mismatch"):
        submitter.submit(netuid=42, uids=[0, 1], weights=[1.0], epoch_id=1)


def test_submit_empty_vector_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    submitter.submit(netuid=42, uids=[], weights=[], epoch_id=1)
    assert subtensor.calls == []


def test_submit_raises_when_chain_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, "rate limited")
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError, match="rate limited"):
        submitter.submit(netuid=42, uids=[0], weights=[1.0], epoch_id=5)


def test_submit_wraps_set_weights_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_set = ConnectionError("subtensor down")
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError, match="subtensor down"):
        submitter.submit(netuid=42, uids=[0], weights=[1.0], epoch_id=5)
