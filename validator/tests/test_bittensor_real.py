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
import threading
import types
from typing import Any

import pytest

from gm_validator.bittensor_real import (
    HotkeyConfigError,
    RealChainCursor,
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
        self.result: tuple[bool, str | None] = (True, "included")
        self.raise_on_set: Exception | None = None
        self.closes: int = 0
        self.block: int = 0
        self.raise_on_block: Exception | None = None
        self.hotkeys: list[str] = ["5HotkeyA", "5HotkeyB", "5HotkeyC"]
        self.requested_metagraph_netuid: int | None = None
        self.raise_on_metagraph: Exception | None = None
        # When set, the named chain RPC blocks on this event until the test
        # releases it (or the worker thread's wall-clock budget elapses),
        # simulating a wedged websocket that hangs rather than raises.
        self.hang_on_block: threading.Event | None = None
        self.hang_on_set: threading.Event | None = None

    def metagraph(self, netuid: int) -> Any:
        self.requested_metagraph_netuid = netuid
        if self.raise_on_metagraph is not None:
            raise self.raise_on_metagraph
        return types.SimpleNamespace(hotkeys=self.hotkeys)

    def set_weights(self, **kwargs: Any) -> tuple[bool, str | None]:
        self.calls.append(kwargs)
        if self.hang_on_set is not None:
            self.hang_on_set.wait(timeout=5.0)
        if self.raise_on_set is not None:
            raise self.raise_on_set
        return self.result

    def get_current_block(self) -> int:
        if self.hang_on_block is not None:
            self.hang_on_block.wait(timeout=5.0)
        if self.raise_on_block is not None:
            raise self.raise_on_block
        return self.block

    def close(self) -> None:
        self.closes += 1


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


# --- metagraph hotkey lookup --------------------------------------------


def test_metagraph_hotkeys_reuses_one_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hotkey lookup reads over the submitter's existing socket.

    Startup must open exactly one bittensor connection: the second rapid
    connect to the public testnet endpoint is what stalled the validator.
    The fake module hands out the SAME subtensor for every
    ``bittensor.Subtensor()`` call, so a second connect would be invisible
    here — what this asserts is that the read targets the held socket and
    no extra ``Subtensor`` construction is needed for the lookup.
    """
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.hotkeys = ["5HotkeyA", "5HotkeyB", "5HotkeyC"]
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)

    lookup = submitter.metagraph_hotkeys(42)

    assert lookup == {"5HotkeyA": 0, "5HotkeyB": 1, "5HotkeyC": 2}
    assert subtensor.requested_metagraph_netuid == 42


def test_metagraph_hotkeys_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.hotkeys = []
    submitter = RealSubmitter(netuid=1, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    assert submitter.metagraph_hotkeys(1) == {}


def test_metagraph_hotkeys_wraps_chain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_metagraph = ConnectionError("subtensor unreachable")
    submitter = RealSubmitter(netuid=1, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="subtensor unreachable"):
        submitter.metagraph_hotkeys(1)


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
    # Fire-and-forget into the mempool: waiting for inclusion or
    # finalization holds a per-submit block-event subscription on the
    # long-lived socket that accumulates over multi-hour runs and stalls
    # payouts. Both waits stay off; confirmation is the separate chain-head
    # poll plus the per-epoch dedup guards.
    assert call["wait_for_inclusion"] is False
    assert call["wait_for_finalization"] is False
    # Pin the plain author_submitExtrinsic path: under MEV mode the SDK
    # rejects its default wait_for_revealed_execution combined with
    # wait_for_inclusion=False, so the submit forces mev_protection off.
    assert call["mev_protection"] is False
    # The submitter relies on the SDK default raise_error=False so substrate
    # errors come back as a (success, message) tuple — a mempool-submission
    # rejection (bad nonce, rate-limit, not registered) surfaces as
    # (False, message). It must not opt into the raising contract.
    assert "raise_error" not in call
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


def test_submit_raises_when_chain_rejects_with_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bittensor.Subtensor.set_weights`` returns ``(False, None)`` on its
    own pre-flight rejections (e.g. the SDK's rate-limit short-circuit).
    The submitter must surface a plain ``WeightSubmissionError`` rather
    than crashing on ``None`` formatting."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, None)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)


def _install_reconnecting_fake_bittensor(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_FakeSubtensor]:
    """Inject a fake ``bittensor`` that returns a new subtensor each call.

    Records every ``Subtensor()`` construction so tests can assert when
    the submitter recreated the connection.
    """
    _FakeKeypair.last_seed = None
    _FakeKeypair.last_mnemonic = None
    subtensors: list[_FakeSubtensor] = []

    def _make_subtensor(network: str | None = None) -> _FakeSubtensor:
        subtensor = _FakeSubtensor(network=network)
        subtensors.append(subtensor)
        return subtensor

    module = types.ModuleType("bittensor")
    module.Keypair = _FakeKeypair  # type: ignore[attr-defined]
    module.Subtensor = _make_subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)
    return subtensors


@pytest.mark.parametrize(
    "fragment",
    [
        "Transaction Already Imported",
        "Invalid Transaction (bad signature)",
        "Transaction is outdated",
        "Stale extrinsic",
    ],
)
def test_submit_treats_every_false_return_as_retryable_failure(
    monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """A ``(False, message)`` return is a plain ``WeightSubmissionError`` so
    the validator loop retries the epoch on the next tick — including
    ``Already Imported``, which from a non-raising return carries no
    inclusion receipt and can be the pool rejecting a still-pending
    duplicate. Mirrors the bm validator: no already-submitted
    short-circuit. Re-submitting the identical vector is idempotent."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.result = (False, fragment)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="rejected set_weights"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)


# --- connection lifecycle ------------------------------------------------


def test_submit_does_not_reconnect_on_single_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single failed submit must reuse the SAME subtensor — dropping and
    reopening the websocket on every failure leaks subscriptions and OOMs
    the pod. Only sustained failure reconnects."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    assert len(subtensors) == 1
    subtensors[0].raise_on_set = ConnectionError("websocket blip")

    with pytest.raises(WeightSubmissionError, match="websocket blip"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)

    # The next submit still uses the original connection — no reconnect yet.
    assert submitter._subtensor is subtensors[0]
    assert len(subtensors) == 1


def test_submit_reconnects_only_after_three_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect fires once the third consecutive connection failure has
    tripped the counter — not before. The original connection is reused
    through failures one and two; the next submit opens a fresh one."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    subtensors[0].raise_on_set = ConnectionError("websocket closed")

    for epoch_id in (5, 6, 7):
        with pytest.raises(WeightSubmissionError, match="websocket closed"):
            submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=epoch_id)
        # Still the original connection while the counter climbs to 3.
        assert submitter._subtensor is subtensors[0]
        assert len(subtensors) == 1

    # Fourth submit: counter is at 3, so _maybe_reconnect opens a fresh
    # connection before submitting; the fresh one succeeds by default.
    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=8)
    assert len(subtensors) == 2
    assert submitter._subtensor is subtensors[1]
    assert subtensors[1].calls[0]["uids"] == [0]
    # The retired connection is closed so the recovery path does not leak.
    assert subtensors[0].closes == 1


def test_submit_success_resets_failure_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A success between failures resets the counter, so two failures then
    a success then two more failures never reconnects — only three
    CONSECUTIVE failures do."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)

    subtensors[0].raise_on_set = ConnectionError("blip")
    for epoch_id in (1, 2):
        with pytest.raises(WeightSubmissionError):
            submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=epoch_id)
    assert submitter._consecutive_failures == 2

    subtensors[0].raise_on_set = None
    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=3)
    assert submitter._consecutive_failures == 0

    subtensors[0].raise_on_set = ConnectionError("blip")
    for epoch_id in (4, 5):
        with pytest.raises(WeightSubmissionError):
            submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=epoch_id)

    # Counter only reached 2 after the reset — still the original socket.
    assert submitter._subtensor is subtensors[0]
    assert len(subtensors) == 1


def test_submit_rejection_does_not_count_toward_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``(False, message)`` rejection means the chain answered, so the
    socket is healthy — it must NOT increment the connection-failure
    counter. Repeated rejections never reconnect."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    subtensors[0].result = (False, "rate limited")

    for epoch_id in (1, 2, 3, 4):
        with pytest.raises(WeightSubmissionError, match="rate limited"):
            submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=epoch_id)

    assert submitter._consecutive_failures == 0
    assert submitter._subtensor is subtensors[0]
    assert len(subtensors) == 1


def test_rejection_does_not_reset_accumulated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With raise_error at the SDK default, bittensor can also funnel a
    transport error into a ``(False, message)`` return. A rejection must
    therefore NOT reset the failure counter — otherwise a poisoned
    connection that alternates raised errors and (False, msg) returns
    would never accumulate to the reconnect threshold. Only a clean
    success resets the counter."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)

    # Two raised transport failures bring the counter to 2.
    subtensors[0].raise_on_set = ConnectionError("websocket closed")
    for epoch_id in (1, 2):
        with pytest.raises(WeightSubmissionError):
            submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=epoch_id)
    assert submitter._consecutive_failures == 2

    # A (False, message) rejection must leave the counter at 2, not reset it.
    subtensors[0].raise_on_set = None
    subtensors[0].result = (False, "rate limited")
    with pytest.raises(WeightSubmissionError, match="rate limited"):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=3)
    assert submitter._consecutive_failures == 2

    # The third raised failure trips the threshold; the next submit reconnects.
    subtensors[0].result = (True, "included")
    subtensors[0].raise_on_set = ConnectionError("websocket closed")
    with pytest.raises(WeightSubmissionError):
        submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=4)
    assert submitter._consecutive_failures == 3

    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    assert len(subtensors) == 2
    assert submitter._subtensor is subtensors[1]


def test_submit_reuses_connection_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-to-back successful submissions reuse the one long-lived
    connection — no reconnect churn on the happy path."""
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)

    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=6)
    assert len(subtensors) == 1


# --- head-block read + chain cursor --------------------------------------


def test_head_block_reads_current_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """``head_block`` returns the long-lived connection's current block."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.block = 1234
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    assert submitter.head_block() == 1234


def test_head_block_wraps_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raised chain read surfaces as WeightSubmissionError and counts
    toward reconnect, exactly like a failed submit."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_block = ConnectionError("ws closed")
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(WeightSubmissionError, match="head-block read failed"):
        submitter.head_block()
    assert submitter._consecutive_failures == 1


def test_head_block_success_does_not_reset_failure_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean head read must NOT reset the submit-failure counter — the head
    poll runs every tick, and a chain that still serves get_current_block()
    while set_weights keeps raising must not have accumulated submit failures
    wiped, or the reconnect-after-three-submit-failures recovery never fires."""
    subtensor = _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    submitter._consecutive_failures = 2
    subtensor.block = 50
    assert submitter.head_block() == 50
    assert submitter._consecutive_failures == 2


# --- RPC timeout + reconnect (post-submit hang) --------------------------


def test_head_block_times_out_when_socket_wedges(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``get_current_block`` that hangs must raise, not freeze the loop.

    Reproduces the post-submit hang: the SDK leaves the websocket wedged
    after a successful ``set_weights`` so the next head read blocks forever.
    The bounded RPC timeout converts that hang into a
    ``WeightSubmissionError`` the validator loop catches, and counts it
    toward reconnect. Without the timeout this test would HANG rather than
    fail — that is the bug it guards. The 5s ``Event.wait`` in the fake is
    a teardown backstop, not the path under test (the 0.05s budget fires
    first).
    """
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.hang_on_block = threading.Event()
    submitter = RealSubmitter(
        netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX, rpc_timeout=0.05
    )
    try:
        with pytest.raises(WeightSubmissionError, match="head-block read failed"):
            submitter.head_block()
    finally:
        subtensor.hang_on_block.set()
    assert submitter._consecutive_failures == 1


def test_submit_times_out_when_socket_wedges(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``set_weights`` that hangs must raise and count toward reconnect."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.hang_on_set = threading.Event()
    submitter = RealSubmitter(
        netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX, rpc_timeout=0.05
    )
    try:
        with pytest.raises(WeightSubmissionError, match="set_weights call failed"):
            submitter.submit(netuid=42, uids=[0], weights=[1], epoch_id=5)
    finally:
        subtensor.hang_on_set.set()
    assert submitter._consecutive_failures == 1


def test_head_block_reconnects_after_three_hung_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three consecutive hung head reads trip the reconnect threshold; the
    fourth read opens a fresh socket and succeeds.

    This is the end-to-end recovery for the post-submit hang: a wedged
    socket times out, accumulates failures, and self-heals via reconnect
    rather than freezing the loop forever.
    """
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(
        netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX, rpc_timeout=0.05
    )
    hang = threading.Event()
    subtensors[0].hang_on_block = hang

    try:
        for _ in range(3):
            with pytest.raises(WeightSubmissionError, match="head-block read failed"):
                submitter.head_block()
            assert submitter._subtensor is subtensors[0]
            assert len(subtensors) == 1
    finally:
        hang.set()

    # Fourth read: the counter is at 3, so _maybe_reconnect opens a fresh
    # socket (a new _FakeSubtensor with no hang set) before the read, which
    # then returns that socket's default block instead of hanging.
    block = submitter.head_block()
    assert len(subtensors) == 2
    assert submitter._subtensor is subtensors[1]
    assert block == subtensors[1].block
    assert subtensors[0].closes == 1


def test_chain_cursor_derives_open_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``current_epoch`` is ``head_block // blocks_per_epoch`` — the same
    derivation the finalizer uses."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.block = 725  # 725 // 360 == 2
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    cursor = RealChainCursor(submitter, blocks_per_epoch=360)
    assert cursor.current_epoch() == 2


def test_chain_cursor_returns_none_on_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed head read yields None so the validator skips the tick."""
    subtensor = _install_fake_bittensor(monkeypatch)
    subtensor.raise_on_block = ConnectionError("ws closed")
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    cursor = RealChainCursor(submitter, blocks_per_epoch=360)
    assert cursor.current_epoch() is None


def test_chain_cursor_survives_hung_tick_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung head read skips the tick without freezing, and the cursor
    recovers once the wedged socket is reconnected.

    This is the loop-survival contract for the post-submit hang: a wedged
    ``get_current_block`` times out, ``current_epoch`` returns None so the
    validator loop's tick is a clean no-op (not an infinite freeze), and
    after the reconnect threshold trips the cursor resolves a real epoch
    again over the fresh socket.
    """
    subtensors = _install_reconnecting_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(
        netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX, rpc_timeout=0.05
    )
    cursor = RealChainCursor(submitter, blocks_per_epoch=360)
    hang = threading.Event()
    subtensors[0].hang_on_block = hang

    try:
        for _ in range(3):
            assert cursor.current_epoch() is None
    finally:
        hang.set()

    subtensors_before = len(subtensors)
    epoch = cursor.current_epoch()
    assert len(subtensors) == subtensors_before + 1
    assert epoch == subtensors[-1].block // 360


def test_chain_cursor_rejects_nonpositive_blocks_per_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_bittensor(monkeypatch)
    submitter = RealSubmitter(netuid=42, endpoint=None, hotkey_seed=_TEST_SEED_HEX)
    with pytest.raises(ValueError, match="blocks_per_epoch must be positive"):
        RealChainCursor(submitter, blocks_per_epoch=0)
