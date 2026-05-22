"""Tests for the real bittensor weight submitter.

The ``bittensor`` SDK is mocked at the module boundary: ``RealSubmitter``
does ``import bittensor`` inside its methods, so injecting a fake module
into ``sys.modules`` lets us exercise the full submission path without a
keypair on disk or a chain to talk to.

The hotkey is built in memory from the ``secretSeed`` of a bittensor
unencrypted-hotkey keyfile blob — no wallet keyfile is read from disk.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from gm_validator.bittensor_real import (
    HotkeyConfigError,
    RealSubmitter,
    WeightSubmissionError,
    _seed_from_hotkey_file,
)

# A bittensor unencrypted-hotkey keyfile blob. Only ``secretSeed`` is
# consumed; the other fields mirror the real document shape.
_HOTKEY_FILE = json.dumps(
    {
        "accountId": "0xaccount",
        "publicKey": "0xpublic",
        "privateKey": "0xprivate",
        "secretPhrase": "twelve word mnemonic goes right about here for the test",
        "secretSeed": "0x" + "ab" * 32,
        "ss58Address": "5HotKeyAddress",
    }
)


class _FakeKeypair:
    ss58_address = "5HotKeyAddress"

    def __init__(self, seed: str) -> None:
        self.seed = seed


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
    module.Keypair = types.SimpleNamespace(create_from_seed=_FakeKeypair)  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)
    return subtensor


def test_seed_from_hotkey_file_extracts_secret_seed() -> None:
    seed = _seed_from_hotkey_file(_HOTKEY_FILE)
    assert seed == "0x" + "ab" * 32


def test_seed_from_hotkey_file_rejects_non_json() -> None:
    with pytest.raises(HotkeyConfigError, match="not valid JSON"):
        _seed_from_hotkey_file("not-json")


def test_seed_from_hotkey_file_rejects_non_object() -> None:
    with pytest.raises(HotkeyConfigError, match="JSON object"):
        _seed_from_hotkey_file(json.dumps(["a", "b"]))


def test_seed_from_hotkey_file_rejects_missing_seed() -> None:
    with pytest.raises(HotkeyConfigError, match="no 'secretSeed'"):
        _seed_from_hotkey_file(json.dumps({"ss58Address": "5Addr"}))


def test_seed_from_hotkey_file_rejects_empty_seed() -> None:
    with pytest.raises(HotkeyConfigError, match="no 'secretSeed'"):
        _seed_from_hotkey_file(json.dumps({"secretSeed": ""}))


def test_constructor_builds_keypair_and_subtensor(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(
        netuid=42, endpoint="wss://entrypoint.example", hotkey_file=_HOTKEY_FILE
    )
    assert submitter._netuid == 42
    # Hotkey is wrapped in the _KeypairWallet shim with a no-op unlock.
    submitter._wallet.unlock_hotkey()
    assert submitter._wallet.hotkey.seed == "0x" + "ab" * 32


def test_constructor_rejects_bad_hotkey_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    with pytest.raises(HotkeyConfigError, match="no 'secretSeed'"):
        RealSubmitter(netuid=1, endpoint=None, hotkey_file=json.dumps({}))


def test_constructor_wraps_keypair_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("bittensor")

    def _boom(*_: object, **__: object) -> object:
        raise ValueError("invalid seed length")

    module.Keypair = types.SimpleNamespace(create_from_seed=_boom)  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: _FakeSubtensor()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)

    with pytest.raises(WeightSubmissionError, match="invalid seed length"):
        RealSubmitter(netuid=1, endpoint=None, hotkey_file=_HOTKEY_FILE)


def test_submit_calls_set_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_file=_HOTKEY_FILE)

    submitter.submit(netuid=42, uids=[0, 3], weights=[0.4, 0.6], epoch_id=7)

    assert len(subtensor.calls) == 1
    call = subtensor.calls[0]
    assert call["netuid"] == 42
    assert call["uids"] == [0, 3]
    assert call["weights"] == [0.4, 0.6]
    assert call["wait_for_inclusion"] is True
    # The shim wallet — not a disk-backed bt.Wallet — is passed through.
    assert call["wallet"] is submitter._wallet


def test_submit_rejects_netuid_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_file=_HOTKEY_FILE)
    with pytest.raises(WeightSubmissionError, match="netuid mismatch"):
        submitter.submit(netuid=99, uids=[0], weights=[1.0], epoch_id=1)


def test_submit_rejects_length_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_file=_HOTKEY_FILE)
    with pytest.raises(WeightSubmissionError, match="length mismatch"):
        submitter.submit(netuid=42, uids=[0, 1], weights=[1.0], epoch_id=1)


def test_submit_empty_vector_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_file=_HOTKEY_FILE)
    submitter.submit(netuid=42, uids=[], weights=[], epoch_id=1)
    assert subtensor.calls == []


def test_submit_raises_when_chain_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, "rate limited")
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_file=_HOTKEY_FILE)
    with pytest.raises(WeightSubmissionError, match="rate limited"):
        submitter.submit(netuid=42, uids=[0], weights=[1.0], epoch_id=5)


def test_submit_wraps_set_weights_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_set = ConnectionError("subtensor down")
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_file=_HOTKEY_FILE)
    with pytest.raises(WeightSubmissionError, match="subtensor down"):
        submitter.submit(netuid=42, uids=[0], weights=[1.0], epoch_id=5)
