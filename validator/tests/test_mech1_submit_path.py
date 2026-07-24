"""Mech-1 routing through the mechanism-aware extrinsic.

The mech-1 constant-weight submit must NOT ride the ``set_weights`` wrapper.
On the repo-pinned bittensor 10.5.0 that wrapper's rate-limit precheck keys
on the BARE netuid (ignores mechid); since mech-0 submits first each epoch it
reports "just submitted" and silently drops the mech-1 extrinsic. So mech-1
goes through the mechanism-aware path (``set_mechanism_weights(netuid,
mecid=1, …)`` / a composed call) that bypasses the bare-netuid precheck —
our own per-mechanism gate does the correct rate-limiting. mech-0 stays on
``set_weights`` unchanged (dgm-00 §7).

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
    """Records ``set_weights`` and ``set_mechanism_weights`` calls separately."""

    def __init__(self, network: str | None = None) -> None:
        self.network = network
        self.calls: list[dict] = []  # set_weights (mech-0 path)
        self.mech_calls: list[dict] = []  # set_mechanism_weights (mech-1 path)
        self.result: tuple[bool, str | None] = (True, "included")
        self.weights_rate_limit_value = 100
        self.tempo_value = 360

    def set_weights(self, **kwargs: object) -> tuple[bool, str | None]:
        self.calls.append(dict(kwargs))
        return self.result

    def set_mechanism_weights(self, *args: object, **kwargs: object) -> tuple[bool, str | None]:
        self.mech_calls.append({"args": args, "kwargs": dict(kwargs)})
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


def test_mech1_submit_uses_mechanism_aware_extrinsic(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``mechid=1`` submit goes through ``set_mechanism_weights`` (mecid=1),
    NOT the bare-netuid ``set_weights`` wrapper that would silently drop it."""
    subtensor = _install_fake_bittensor(monkeypatch)
    from gm_validator.bittensor_real import RealSubmitter

    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[250], weights=[MAX_WEIGHT], epoch_id=9, mechid=1)

    # The wrapper is bypassed entirely for mech-1.
    assert subtensor.calls == []
    # The mechanism-aware extrinsic carries the mech-1 mechanism id and vector.
    assert len(subtensor.mech_calls) == 1
    call = subtensor.mech_calls[0]
    assert call["kwargs"].get("mecid") == 1
    values = list(call["args"]) + list(call["kwargs"].values())
    assert [250] in values
    assert [MAX_WEIGHT] in values


def test_mech0_submit_uses_set_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default (mech-0) submit stays on the ``set_weights`` wrapper unchanged
    and never touches the mechanism-aware extrinsic."""
    subtensor = _install_fake_bittensor(monkeypatch)
    from gm_validator.bittensor_real import RealSubmitter

    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[0, 1], weights=[100, MAX_WEIGHT - 100], epoch_id=9)

    assert len(subtensor.calls) == 1
    assert subtensor.calls[0]["uids"] == [0, 1]
    assert subtensor.mech_calls == []
