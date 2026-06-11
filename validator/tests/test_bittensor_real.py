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

from gm_validator.bittensor_real import (
    AlreadySubmittedError,
    RealSubmitter,
    WeightSubmissionError,
)


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
        self.result: tuple[bool, str | None] = (True, "included")
        self.raise_on_set: Exception | None = None
        self.closes: int = 0
        self.raise_on_close: Exception | None = None

    def set_weights(self, **kwargs: Any) -> tuple[bool, str | None]:
        self.calls.append(kwargs)
        if self.raise_on_set is not None:
            raise self.raise_on_set
        return self.result

    def close(self) -> None:
        self.closes += 1
        if self.raise_on_close is not None:
            raise self.raise_on_close


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
        submitter.submit(netuid=99, uids=[0], weights=[1], epoch_id=1)


def test_submit_rejects_length_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError, match="length mismatch"):
        submitter.submit(netuid=42, uids=[0, 1], weights=[1], epoch_id=1)


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
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)


def test_submit_wraps_set_weights_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_set = ConnectionError("subtensor down")
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError, match="subtensor down"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)


def test_submit_raises_when_chain_rejects_with_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bittensor.Subtensor.set_weights`` returns ``(False, None)`` on its
    own pre-flight rejections (e.g. the SDK's rate-limit short-circuit).
    The submitter must surface a plain ``WeightSubmissionError`` —
    crashing on ``None.lower()`` would obscure the rejection from
    callers that branch on exception type."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, None)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError) as exc_info:
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    assert not isinstance(exc_info.value, AlreadySubmittedError)


def _install_reconnecting_fake_bittensor(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_FakeSubtensor]:
    """Inject a fake ``bittensor`` that returns a new subtensor each call.

    Records every ``Subtensor()`` construction so tests can assert the
    submitter reopened the connection after a failure.
    """
    subtensors: list[_FakeSubtensor] = []

    def _make_subtensor(network: str | None = None) -> _FakeSubtensor:
        subtensor = _FakeSubtensor(network=network)
        subtensors.append(subtensor)
        return subtensor

    module = types.ModuleType("bittensor")
    module.Wallet = _FakeWallet  # type: ignore[attr-defined]
    module.Subtensor = _make_subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)
    return subtensors


def test_submit_raises_already_submitted_on_already_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substrate's ``Transaction Already Imported`` proves the chain holds
    this exact extrinsic. It must surface as ``AlreadySubmittedError`` (a
    ``WeightSubmissionError`` subclass) so the validator marks the epoch
    processed instead of looping."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_set = RuntimeError("Transaction Already Imported")
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(AlreadySubmittedError, match="already on chain"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)


@pytest.mark.parametrize(
    "fragment",
    [
        "Invalid Transaction (bad signature)",
        "Transaction is outdated",
        "Stale extrinsic",
    ],
)
def test_submit_does_not_treat_ambiguous_errors_as_submitted(
    monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """Substrate emits ``bad signature``, ``stale``, and ``outdated`` for
    cases that do NOT prove this extrinsic reached the chain (genuine
    signing failures; nonce advanced by another extrinsic). Treating them
    as already-submitted would silently skip an epoch's weights. They
    must propagate as plain ``WeightSubmissionError`` so the validator
    retries on the next tick."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_set = RuntimeError(fragment)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError) as exc_info:
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    assert not isinstance(exc_info.value, AlreadySubmittedError)


def test_submit_treats_explicit_failure_message_as_plain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``(False, message)`` return shape carries no inclusion receipt
    even when *message* mentions ``Already Imported`` — it can mean the
    transaction pool merely sees a still-pending duplicate that may later
    be dropped. The submitter must surface it as a plain failure so the
    next tick retries; only the raised-exception path under
    ``wait_for_inclusion=True`` is positive evidence of inclusion."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, "Transaction Already Imported")
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    with pytest.raises(WeightSubmissionError) as exc_info:
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    assert not isinstance(exc_info.value, AlreadySubmittedError)


def test_submit_reopens_subtensor_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submission that raised must drop the cached connection so the
    next submit opens a fresh subtensor — otherwise a dead websocket
    would poison every subsequent epoch."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    assert len(subtensors) == 1
    subtensors[0].raise_on_set = ConnectionError("websocket closed")

    with pytest.raises(WeightSubmissionError, match="websocket closed"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)

    # Next submit succeeds on a freshly-opened subtensor.
    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=6)
    assert len(subtensors) == 2
    assert subtensors[1].calls[0]["uids"] == [0]


def test_submit_reopens_subtensor_after_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same reconnect path applies when set_weights returns
    ``(False, message)`` rather than raising."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    subtensors[0].result = (False, "rate limited")

    with pytest.raises(WeightSubmissionError, match="rate limited"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)

    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=6)
    assert len(subtensors) == 2


def test_submit_does_not_reopen_subtensor_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful submission must reuse the existing connection — only
    the failure paths reset it."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")

    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=6)
    assert len(subtensors) == 1


def test_reset_closes_old_subtensor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated submission failures across a long-running validator would
    accumulate dead websockets if reset only dropped the reference. The
    failure path must call ``close()`` on the old subtensor before
    opening a fresh one."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    subtensors[0].raise_on_set = ConnectionError("websocket closed")

    with pytest.raises(WeightSubmissionError, match="websocket closed"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)

    assert subtensors[0].closes == 1


def test_reset_suppresses_close_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``close()`` that itself raises must not mask the underlying
    submission failure — we are already on the error path and the
    connection is being discarded anyway."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, wallet_name="v", wallet_hotkey="h")
    subtensors[0].raise_on_set = ConnectionError("websocket closed")
    subtensors[0].raise_on_close = RuntimeError("close exploded")

    with pytest.raises(WeightSubmissionError, match="websocket closed"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)

    # Next submit still succeeds on a freshly-opened subtensor.
    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=6)
    assert len(subtensors) == 2
