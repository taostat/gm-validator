"""Tests for the real bittensor weight submitter.

The ``bittensor`` SDK is mocked at the module boundary: ``RealSubmitter``
does ``import bittensor`` inside its methods, so injecting a fake module
into ``sys.modules`` lets us exercise the full submission path without a
keypair on disk or a chain to talk to.

The hotkey is built in memory from ``BITTENSOR_HOTKEY_SEED`` — a BIP-39
mnemonic or a ``0x``-prefixed hex seed — never from a keyfile on disk.

``test_*_ground_truth_ss58`` cases are not mocked: they import the real
``bittensor`` SDK to assert that a known throwaway seed/mnemonic derives
the expected ss58 address. They are skipped when the SDK is not
installed (e.g. the lightweight unit-test environment).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from typing import Any

import pytest

from gm_validator.bittensor_real import (
    HotkeyConfigError,
    RealSubmitter,
    WeightSubmissionError,
    _keypair_from_seed,
)

# Throwaway test secrets — NOT used by any real validator. The expected
# ss58 addresses are the ground truth bittensor v10 derives from them.
_TEST_MNEMONIC = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"
_TEST_MNEMONIC_SS58 = "5DfhGyQdFobKM8NsWvEeAKk5EQQgYe9AydgJ7rMB6E1EqRzV"
_TEST_SEED_HEX = "0x" + "ab" * 32
_TEST_SEED_SS58 = "5ERxhxyG15TfeiZ27PFdTQRpJghy17kkffvLEYuVMtfZZTrn"


class _FakeKeypair:
    """Stand-in for ``bittensor.Keypair`` recording how it was built."""

    last_seed: str | None = None
    last_mnemonic: str | None = None

    def __init__(self, ss58_address: str = "5HotKeyAddress") -> None:
        self.ss58_address = ss58_address

    @classmethod
    def create_from_seed(cls, seed: str) -> _FakeKeypair:
        cls.last_seed = seed
        return cls("5SeedAddress")

    @classmethod
    def create_from_mnemonic(cls, mnemonic: str) -> _FakeKeypair:
        cls.last_mnemonic = mnemonic
        return cls("5MnemonicAddress")


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
    _FakeKeypair.last_seed = None
    _FakeKeypair.last_mnemonic = None
    subtensor = _FakeSubtensor()
    module = types.ModuleType("bittensor")
    module.Keypair = _FakeKeypair  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)
    return subtensor


def _install_failing_keypair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a ``bittensor`` whose Keypair builders always raise."""

    class _Boom:
        @staticmethod
        def create_from_mnemonic(_: str) -> object:
            raise ValueError("not a valid mnemonic")

        @staticmethod
        def create_from_seed(_: str) -> object:
            raise ValueError("not a valid seed")

    module = types.ModuleType("bittensor")
    module.Keypair = _Boom  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: _FakeSubtensor()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)


# --- seed parsing --------------------------------------------------------


def test_keypair_from_hex_seed_uses_create_from_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    keypair = _keypair_from_seed(_TEST_SEED_HEX)
    assert _FakeKeypair.last_seed == _TEST_SEED_HEX
    assert _FakeKeypair.last_mnemonic is None
    assert keypair.ss58_address == "5SeedAddress"


def test_keypair_from_mnemonic_uses_create_from_mnemonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_bittensor(monkeypatch)
    keypair = _keypair_from_seed(_TEST_MNEMONIC)
    assert _FakeKeypair.last_mnemonic == _TEST_MNEMONIC
    assert _FakeKeypair.last_seed is None
    assert keypair.ss58_address == "5MnemonicAddress"


def test_keypair_from_seed_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    _keypair_from_seed(f"  {_TEST_SEED_HEX}\n")
    assert _FakeKeypair.last_seed == _TEST_SEED_HEX


def test_keypair_from_empty_seed_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    with pytest.raises(HotkeyConfigError, match="empty"):
        _keypair_from_seed("   ")


def test_keypair_from_invalid_seed_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_failing_keypair(monkeypatch)
    with pytest.raises(HotkeyConfigError, match="not a valid BIP-39 mnemonic or"):
        _keypair_from_seed("garbage words that are not a mnemonic")


# --- ground truth (real SDK) --------------------------------------------

_HAS_BITTENSOR = importlib.util.find_spec("bittensor") is not None
_needs_sdk = pytest.mark.skipif(not _HAS_BITTENSOR, reason="bittensor SDK not installed")


@_needs_sdk
def test_mnemonic_ground_truth_ss58() -> None:
    keypair = _keypair_from_seed(_TEST_MNEMONIC)
    assert keypair.ss58_address == _TEST_MNEMONIC_SS58


@_needs_sdk
def test_hex_seed_ground_truth_ss58() -> None:
    keypair = _keypair_from_seed(_TEST_SEED_HEX)
    assert keypair.ss58_address == _TEST_SEED_SS58


# --- construction --------------------------------------------------------


def test_constructor_builds_in_memory_keypair(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(
        netuid=42,
        endpoint="wss://entrypoint.example",
        hotkey_seed=_TEST_SEED_HEX,
    )
    assert submitter._netuid == 42
    assert _FakeKeypair.last_seed == _TEST_SEED_HEX
    assert subtensor.network is None


def test_constructor_does_not_write_keyfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No keyfile or wallet directory is created on disk during startup."""
    _install_fake_bittensor(monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    RealSubmitter(netuid=1, endpoint=None, hotkey_seed=_TEST_MNEMONIC)
    assert list(fake_home.rglob("*")) == []


def test_constructor_rejects_bad_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_failing_keypair(monkeypatch)
    with pytest.raises(HotkeyConfigError):
        RealSubmitter(netuid=1, endpoint=None, hotkey_seed="not a real seed")


# --- submission ----------------------------------------------------------


def test_submit_passes_in_memory_wallet_to_set_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)

    submitter.submit(netuid=42, uids=[0, 3], weights=[400, 600], epoch_id=7)

    assert len(subtensor.calls) == 1
    call = subtensor.calls[0]
    assert call["netuid"] == 42
    assert call["uids"] == [0, 3]
    assert call["weights"] == [400, 600]
    assert call["wait_for_inclusion"] is True
    # The wallet handed to the chain is the in-memory shim, and its
    # no-op unlock_hotkey never touches the filesystem.
    wallet = call["wallet"]
    assert wallet.hotkey.ss58_address == "5SeedAddress"
    wallet.unlock_hotkey()


def test_submit_rejects_netuid_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="netuid mismatch"):
        submitter.submit(netuid=99, uids=[0], weights=[1], epoch_id=1)


def test_submit_rejects_length_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="length mismatch"):
        submitter.submit(netuid=42, uids=[0, 1], weights=[1], epoch_id=1)


def test_submit_empty_vector_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[], weights=[], epoch_id=1)
    assert subtensor.calls == []


def test_submit_raises_when_chain_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, "rate limited")
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="rate limited"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)


def test_submit_wraps_set_weights_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_set = ConnectionError("subtensor down")
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="subtensor down"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
