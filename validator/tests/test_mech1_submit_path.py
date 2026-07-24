"""Mech-1 threading through the submit path.

The mech-1 constant-weight submit rides the same submit path as mech-0 but
targets a different mechanism id (``mecid=1`` per spike-1, dgm-10). The
``mechid`` argument added to ``submit`` must reach the chain
``set_weights`` call so the vector lands on the mech-1 mechanism's storage
index, while a default (mech-0) submit is unchanged.

``bittensor`` is faked at the module boundary exactly like
``test_bittensor_real.py``: ``RealSubmitter`` imports it inside its methods,
so a fake module in ``sys.modules`` exercises the full path without a chain.
"""

from __future__ import annotations

import sys
import types

import pytest

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.bittensor_adapter import MockSubmitter

_TEST_SEED_HEX = "0x" + "ab" * 32


class _FakeKeypair:
    """Stand-in for ``bittensor.Keypair``."""

    def __init__(self, ss58_address: str = "5SeedAddress") -> None:
        self.ss58_address = ss58_address
        self.public_key = bytes([1]) * 32

    @classmethod
    def create_from_seed(cls, seed: str) -> _FakeKeypair:
        return cls("5SeedAddress")

    @classmethod
    def create_from_mnemonic(cls, mnemonic: str) -> _FakeKeypair:
        return cls("5MnemonicAddress")


class _FakeSubtensor:
    """Records ``set_weights`` calls; returns a configurable result."""

    def __init__(self, network: str | None = None) -> None:
        self.network = network
        self.calls: list[dict] = []
        self.result: tuple[bool, str | None] = (True, "included")
        self.weights_rate_limit_value = 100
        self.tempo_value = 360

    def set_weights(self, **kwargs: object) -> tuple[bool, str | None]:
        self.calls.append(dict(kwargs))
        return self.result

    def weights_rate_limit(self, netuid: int) -> int:
        return self.weights_rate_limit_value

    def tempo(self, netuid: int) -> int:
        return self.tempo_value

    def close(self) -> None:
        """No-op close for the fake socket."""


def _install_fake_bittensor(monkeypatch: pytest.MonkeyPatch) -> _FakeSubtensor:
    subtensor = _FakeSubtensor()
    module = types.ModuleType("bittensor")
    module.Keypair = _FakeKeypair  # type: ignore[attr-defined]
    module.Subtensor = lambda network=None: subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)
    return subtensor


def test_mock_submitter_records_mechid() -> None:
    """The mock records the mechid so tests can assert the mech-1 target."""
    submitter = MockSubmitter()
    submitter.submit(netuid=42, uids=[250], weights=[MAX_WEIGHT], epoch_id=3, mechid=1)
    assert submitter.calls[0]["mechid"] == 1


def test_submit_threads_mechid_into_set_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``mechid=1`` submit lands on ``set_weights`` with ``mechid=1``.

    This is the mech-1 constant vector reaching its own mechanism. Without the
    threading it silently rides mech-0 and double-pays.
    """
    subtensor = _install_fake_bittensor(monkeypatch)
    from gm_validator.bittensor_real import RealSubmitter

    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[250], weights=[MAX_WEIGHT], epoch_id=9, mechid=1)

    assert len(subtensor.calls) == 1
    assert subtensor.calls[0].get("mechid") == 1


def test_mech0_submit_targets_mechid_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default submit stays on mech-0 (``mechid`` 0 / absent) — unchanged."""
    subtensor = _install_fake_bittensor(monkeypatch)
    from gm_validator.bittensor_real import RealSubmitter

    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[0, 1], weights=[100, MAX_WEIGHT - 100], epoch_id=9)

    assert len(subtensor.calls) == 1
    assert subtensor.calls[0].get("mechid", 0) == 0
