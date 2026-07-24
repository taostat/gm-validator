"""Mech-1 routing through the mechanism-aware extrinsic.

The mech-1 constant-weight submit must NOT ride the public
``subtensor.set_weights`` wrapper. On the repo-pinned bittensor 10.5.0 that
wrapper's rate-limit precheck keys on the BARE netuid (ignores mechid); since
mech-0 submits first each epoch it reports "just submitted" and silently
drops the mech-1 extrinsic. So mech-1 goes through the module-level function
``set_weights_extrinsic(subtensor, wallet, netuid, mechid=1, uids, weights,
version_key, …)`` (``bittensor.core.extrinsics.weights``), which composes the
``SubtensorModule.set_mechanism_weights(netuid, mecid=1, …)`` chain extrinsic
and sign-and-sends it with NO bare-netuid precheck — returning the same
``(success, message)`` shape so the failure/reconnect contract is preserved.
mech-0 stays on the public ``subtensor.set_weights`` wrapper, unchanged
(dgm-00 §7).

There is NO ``set_mechanism_weights`` *method* on 10.5.0's ``Subtensor`` — the
seam is the module function. This suite patches it where ``RealSubmitter``
imports it (a module-level patch on ``gm_validator.bittensor_real``) AND fakes
the ``bittensor.core.extrinsics.weights`` submodule, so the spy is hit whether
the builder binds the symbol at module top level or imports it lazily inside
``submit``. ``bittensor`` itself is faked at the module boundary for
construction, like ``test_bittensor_real.py``.
"""

from __future__ import annotations

import sys
import types

import pytest

import gm_validator.bittensor_real as bittensor_real
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
    """Records public ``set_weights`` wrapper calls (the mech-0 path)."""

    def __init__(self, network: str | None = None) -> None:
        self.network = network
        self.calls: list[dict] = []  # subtensor.set_weights wrapper (mech-0)
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


def _install_fake_bittensor(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeSubtensor, list[dict]]:
    """Fake ``bittensor`` for construction and intercept ``set_weights_extrinsic``.

    Returns the fake subtensor (records wrapper ``set_weights`` calls) and a
    list capturing every ``set_weights_extrinsic`` call (the mech-1 path). The
    spy is bound both as a module-level symbol on ``gm_validator.bittensor_real``
    and on a faked ``bittensor.core.extrinsics.weights`` submodule, so it is hit
    regardless of the import style the builder uses.
    """
    subtensor = _FakeSubtensor()
    extrinsic_calls: list[dict] = []

    def _set_weights_extrinsic(*args: object, **kwargs: object) -> tuple[bool, str | None]:
        extrinsic_calls.append({"args": args, "kwargs": dict(kwargs)})
        return (True, "included")

    bittensor_mod = types.ModuleType("bittensor")
    bittensor_mod.Keypair = _FakeKeypair  # type: ignore[attr-defined]
    bittensor_mod.Subtensor = lambda network=None: subtensor  # type: ignore[attr-defined]
    core_mod = types.ModuleType("bittensor.core")
    extrinsics_mod = types.ModuleType("bittensor.core.extrinsics")
    weights_mod = types.ModuleType("bittensor.core.extrinsics.weights")
    weights_mod.set_weights_extrinsic = _set_weights_extrinsic  # type: ignore[attr-defined]

    for name, module in (
        ("bittensor", bittensor_mod),
        ("bittensor.core", core_mod),
        ("bittensor.core.extrinsics", extrinsics_mod),
        ("bittensor.core.extrinsics.weights", weights_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    # Module-level patch on the seam where RealSubmitter references the symbol.
    monkeypatch.setattr(
        bittensor_real, "set_weights_extrinsic", _set_weights_extrinsic, raising=False
    )
    return subtensor, extrinsic_calls


def _mechid_of(call: dict) -> object:
    """Extract the mechid from a ``set_weights_extrinsic`` call.

    Signature is ``(subtensor, wallet, netuid, mechid, uids, weights,
    version_key, …)`` — mechid is keyword or positional index 3.
    """
    if "mechid" in call["kwargs"]:
        return call["kwargs"]["mechid"]
    args = call["args"]
    return args[3] if len(args) > 3 else None


def test_mock_submitter_records_mechid() -> None:
    """The mock records the mechid so tests can assert the mech-1 target."""
    submitter = MockSubmitter()
    submitter.submit(netuid=42, uids=[250], weights=[MAX_WEIGHT], epoch_id=3, mechid=1)
    assert submitter.calls[0]["mechid"] == 1


def test_mech1_submit_uses_set_weights_extrinsic(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``mechid=1`` submit goes through the ``set_weights_extrinsic`` module
    function (mechid=1), bypassing the bare-netuid ``subtensor.set_weights``
    wrapper that would silently drop it after the mech-0 submit."""
    subtensor, extrinsic_calls = _install_fake_bittensor(monkeypatch)

    submitter = bittensor_real.RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[250], weights=[MAX_WEIGHT], epoch_id=9, mechid=1)

    # The public wrapper is bypassed entirely for mech-1.
    assert subtensor.calls == []
    # The mechanism-aware extrinsic carries the mech-1 mechanism id and vector.
    assert len(extrinsic_calls) == 1
    call = extrinsic_calls[0]
    assert _mechid_of(call) == 1
    values = list(call["args"]) + list(call["kwargs"].values())
    assert [250] in values
    assert [MAX_WEIGHT] in values


def test_mech0_submit_uses_set_weights_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default (mech-0) submit stays on the public ``subtensor.set_weights``
    wrapper unchanged and never touches ``set_weights_extrinsic``."""
    subtensor, extrinsic_calls = _install_fake_bittensor(monkeypatch)

    submitter = bittensor_real.RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter.submit(netuid=42, uids=[0, 1], weights=[100, MAX_WEIGHT - 100], epoch_id=9)

    assert len(subtensor.calls) == 1
    assert subtensor.calls[0]["uids"] == [0, 1]
    assert extrinsic_calls == []
