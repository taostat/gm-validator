"""Real bittensor submitter — submits weights to the subtensor chain.

This wraps the ``bittensor`` SDK (v10). It builds the validator hotkey
keypair in memory from a seed, connects to a subtensor endpoint, and
calls ``subtensor.set_weights`` once per epoch. The validator's hot-path
code never imports ``bittensor`` directly — it talks to the ``Submitter``
protocol in ``bittensor_adapter`` — so the test suite (which uses
``MockSubmitter``) does not pull in the heavy dependency.

The ``bittensor`` import is deferred to the methods that need it so that
merely importing this module is cheap; only constructing ``RealSubmitter``
or calling ``submit`` requires the SDK to be installed.

The hotkey is loaded entirely in memory — no wallet keyfile is ever read
from or written to disk. ``BITTENSOR_HOTKEY_SEED`` carries only the
secret seed material: either a BIP-39 mnemonic or a ``0x``-prefixed
hex seed. No coldkey, no wallet directory, no keyfile JSON blob.

Connection lifecycle:

The submitter holds ONE long-lived ``Subtensor`` for the life of the
process — the same websocket the blockmachine validator runs unchanged
for 9+ days. A submission failure does NOT drop the connection: dropping
and reopening the websocket on every failed or rejected submit leaks
subscriptions inside the SDK and OOMs the pod in minutes. Instead a
``_consecutive_failures`` counter reconnects only after three consecutive
connection-level (raised-exception) failures, mirroring the bm validator's
``_maybe_reconnect``; a clean success resets it to zero.

Submit contract:

``set_weights`` is called fire-and-forget — ``wait_for_inclusion=False,
wait_for_finalization=False`` — so the SDK signs, submits to the mempool,
and returns at once without opening a block-event subscription. Waiting
for inclusion or finalization holds a per-submit subscription on the
long-lived socket until the extrinsic lands; over multi-hour runs those
accumulate and stall payouts, and a ``run_with_timeout`` that fires
abandons (does not cancel) the worker thread, so a wedged wait keeps its
subscription alive on the shared socket. This trades the SDK's per-submit
inclusion receipt for a non-leaking submit: a mempool accept is not a
chain-inclusion proof, so a submitted vector the pool later drops is not
retried within its epoch. That loss is bounded — the validator only ever
targets the newest finalized epoch and re-derives it from the chain head
each tick, so a dropped vector is superseded by the next epoch's submit
(on-chain weights are a snapshot, not a per-epoch ledger), and a
re-submitted identical vector is idempotent so the path never
double-submits.

The submit is bounded by ``run_with_timeout`` and uses the SDK default
``raise_error=False``, so substrate errors come back as an
``ExtrinsicResponse`` (unpacks as ``(success, message)``) rather than a
raised exception. Two outcomes drive the lifecycle:

- A raised exception is a connection-level failure (dead websocket, RPC
  timeout, or the run_with_timeout wall-clock bound firing). It increments
  the failure counter so the connection is recreated once three pile up,
  and surfaces as ``WeightSubmissionError``.
- A ``(False, message)`` return is a chain answer, so the socket is
  healthy: it neither resets nor increments the counter, and surfaces as
  ``WeightSubmissionError`` so the validator loop retries the epoch on the
  next tick. With the waits off this covers a mempool-submission rejection
  (bad nonce, rate-limit short-circuit, hotkey not registered) as well as
  the chain's pre-flight checks; the rate-limit gate runs before the
  extrinsic is built, so dropping the waits does not weaken it.

This counts only raised exceptions toward reconnect, exactly like the bm
validator that has run this contract for 9+ days. The SDK can also wrap a
transport failure into a ``(False, message)`` return, and its ``.error``
field is populated for ordinary chain rejections as well as transport
errors — so there is no reliable way to tell the two apart from the
response without coupling to SDK-internal exception types. We accept the
bm trade-off rather than reintroduce that coupling: the websocket-idle
close that actually occurs in practice raises, and a persistently dead
endpoint still surfaces through the startup connect on the kubelet
restart.

Like the bm validator, there is no "already submitted" short-circuit. A
``(False, "...Already Imported...")`` return carries no inclusion receipt —
it can be the transaction pool rejecting a still-pending duplicate that
may yet be dropped — so it is treated as a plain failure and retried.
Re-submitting the identical weight vector is idempotent, and a genuinely
landed epoch is retired by the superseded-epoch sweep once newer epochs
finalize, so retrying never double-submits or loops forever.
"""

from __future__ import annotations

import logging
from typing import Any

from gm_validator.subtensor_connect import (
    DEFAULT_CONNECT_TIMEOUT_SECS,
    DEFAULT_RPC_TIMEOUT_SECS,
    run_with_timeout,
)

LOGGER = logging.getLogger(__name__)


# Connection-level failures tolerated before recreating the websocket.
# A single failure never reconnects; only sustained failure does, which
# avoids the per-error reconnect churn that leaked subscriptions.
_RECONNECT_AFTER_FAILURES = 3


class WeightSubmissionError(RuntimeError):
    """The subtensor rejected a ``set_weights`` extrinsic."""


class HotkeyConfigError(RuntimeError):
    """The hotkey seed was missing or could not be parsed into a keypair."""


def _keypair_from_seed(seed: str) -> Any:
    """Build a ``bittensor.Keypair`` in memory from a seed.

    Accepts either a BIP-39 mnemonic (space-separated words) or a
    ``0x``-prefixed hex seed. Nothing is read from or written to disk.

    Args:
        seed: ``BITTENSOR_HOTKEY_SEED`` — a mnemonic or ``0x`` hex seed.

    Returns:
        A ``bittensor.Keypair`` carrying the signing key.

    Raises:
        HotkeyConfigError: ``seed`` is empty, or neither a valid mnemonic
            nor a valid ``0x`` hex seed.
    """
    import bittensor

    candidate = seed.strip()
    if not candidate:
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_SEED is empty; provide the validator hotkey "
            "seed as a BIP-39 mnemonic or a 0x-prefixed hex seed"
        )
    try:
        if candidate.startswith("0x"):
            return bittensor.Keypair.create_from_seed(candidate)
        return bittensor.Keypair.create_from_mnemonic(candidate)
    except Exception as exc:
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_SEED is not a valid BIP-39 mnemonic or 0x-prefixed hex seed"
        ) from exc


class _KeypairWallet:
    """Shim wrapping a bare ``bittensor.Keypair`` as a wallet.

    ``subtensor.set_weights`` expects a wallet object exposing ``.hotkey``
    and (on newer bittensor versions) ``unlock_hotkey()``. The hotkey is
    already in memory, so ``unlock_hotkey`` is a no-op and no keyfile is
    ever read from disk.
    """

    def __init__(self, hotkey: Any) -> None:
        self.hotkey = hotkey

    def unlock_hotkey(self) -> None:
        """No-op: the hotkey is already loaded in memory."""


class RealSubmitter:
    """Real bittensor weight submitter.

    Builds the hotkey keypair in memory and opens a single subtensor
    connection at construction time so that a malformed seed or
    unreachable endpoint fails fast on startup rather than on the first
    finalized epoch. That connection is held for the life of the process
    and recreated only after sustained connection failures — a single
    failed submit reuses the existing websocket.
    """

    def __init__(
        self,
        netuid: int,
        endpoint: str | None,
        hotkey_seed: str,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECS,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECS,
    ) -> None:
        """Build the in-memory hotkey and connect to subtensor.

        Args:
            netuid: Subnet id the validator submits weights for.
            endpoint: Subtensor websocket URL; ``None`` selects the SDK
                default network.
            hotkey_seed: The validator hotkey seed — a BIP-39 mnemonic or
                a ``0x``-prefixed hex seed. The signing keypair is built
                in memory from it; no keyfile is read from disk.
            connect_timeout: Per-attempt wall-clock budget for opening the
                subtensor websocket; a hung connect becomes a retryable
                timeout instead of freezing startup.
            rpc_timeout: Per-call wall-clock budget for a chain RPC over
                the open socket (head read, submit); a hung call becomes a
                ``TimeoutError`` that counts toward reconnect instead of
                freezing the loop.

        Raises:
            HotkeyConfigError: The seed is missing or malformed.
            WeightSubmissionError: The subtensor endpoint could not be
                reached.
        """
        self._netuid = netuid
        self._endpoint = endpoint
        self._connect_timeout = connect_timeout
        self._rpc_timeout = rpc_timeout
        self._subtensor: Any | None = None
        self._consecutive_failures = 0
        hotkey = _keypair_from_seed(hotkey_seed)
        self._wallet: Any = _KeypairWallet(hotkey)
        hotkey_ss58 = hotkey.ss58_address
        try:
            self._subtensor = self._open_subtensor()
        except Exception as exc:
            raise WeightSubmissionError(
                f"failed to connect to subtensor (endpoint={endpoint}): {exc}"
            ) from exc
        LOGGER.info(
            "RealSubmitter ready: netuid=%d hotkey=%s endpoint=%s (in-memory keypair)",
            netuid,
            hotkey_ss58,
            endpoint or "<default>",
        )

    def _open_subtensor(self) -> Any:
        """Open a fresh subtensor connection at the configured endpoint.

        Routes through ``connect_subtensor`` so both the construction-time
        connect and the rare post-failure reconnect retry transient
        endpoint failures (HTTP 429 from public testnet RPCs in
        particular) instead of crash-looping the pod.
        """
        from gm_validator.subtensor_connect import connect_subtensor

        return connect_subtensor(self._endpoint, connect_timeout=self._connect_timeout)

    def _maybe_reconnect(self) -> None:
        """Recreate the websocket after sustained connection failures.

        Reconnects only once ``_consecutive_failures`` reaches
        ``_RECONNECT_AFTER_FAILURES`` — a single failed submit reuses the
        existing connection. Dropping and reopening on every failure leaks
        SDK subscriptions and OOMs the pod, so reconnect is the rare
        recovery path, not the per-error default. The old connection is
        closed before the fresh one replaces it so the recovery path does
        not itself leak websockets. A failed reconnect keeps the last
        connection and leaves the counter tripped so the next submit
        retries the reconnect.
        """
        if self._consecutive_failures < _RECONNECT_AFTER_FAILURES:
            return
        LOGGER.warning(
            "reconnecting subtensor after %d consecutive failures",
            self._consecutive_failures,
        )
        try:
            fresh = self._open_subtensor()
        except Exception as exc:  # noqa: BLE001 — keep the last connection on a failed reconnect; the counter stays tripped so the next submit retries.
            LOGGER.error("subtensor reconnect failed: %s", exc)
            return
        self._close_subtensor(self._subtensor)
        self._subtensor = fresh
        self._consecutive_failures = 0
        LOGGER.info("subtensor reconnected")

    @staticmethod
    def _close_subtensor(subtensor: Any | None) -> None:
        """Best-effort close of a retired subtensor's websocket.

        Called only when a fresh connection has already replaced it, so a
        ``close()`` failure is logged and swallowed — the connection is
        being discarded either way.
        """
        if subtensor is None:
            return
        close = getattr(subtensor, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup; the retired connection is discarded regardless.
            LOGGER.debug("retired subtensor close failed: %s", exc)

    def head_block(self) -> int:
        """Return the current chain head block from the long-lived connection.

        Reuses the same websocket the submitter holds — the validator's
        per-tick epoch cursor reads the head here rather than opening a
        second connection. A read failure counts toward reconnect exactly
        like a failed submit (only raised exceptions drive reconnect), so a
        dead socket is recovered on a later tick.

        A clean head read deliberately does NOT reset ``_consecutive_failures``:
        the head poll runs every tick before the submit, and a chain that
        can still serve ``get_current_block()`` while ``set_weights`` keeps
        raising must not have its accumulated submit failures wiped — only a
        clean ``set_weights`` proves the submit path healthy and resets the
        counter. The reset asymmetry keeps the leak-fix's
        reconnect-after-three-submit-failures recovery intact.

        The read is bounded by ``_rpc_timeout``: after a successful
        ``set_weights`` the SDK can leave the websocket wedged so the next
        ``get_current_block`` blocks forever, freezing the whole loop.
        A timeout is a raised exception like any other connection failure —
        it counts toward reconnect so the socket self-heals on a later tick.

        Raises:
            WeightSubmissionError: No connection is available, the head
                read failed at the connection level, or it timed out.
        """
        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            raise WeightSubmissionError("no subtensor connection available for head-block read")
        try:
            block = run_with_timeout(
                "subtensor get_current_block",
                subtensor.get_current_block,
                self._rpc_timeout,
            )
        except Exception as exc:
            self._consecutive_failures += 1
            raise WeightSubmissionError(f"head-block read failed: {exc}") from exc
        return int(block)

    def metagraph_hotkeys(self, netuid: int) -> dict[str, int]:
        """Return the subnet's hotkey ss58 -> uid mapping over the held socket.

        Reads the metagraph through the submitter's long-lived connection so
        startup and per-tick refreshes use exactly one websocket. A pending
        reconnect (failure threshold already tripped by prior submits/head
        reads) is honoured first so the refresh reads from the fresh socket
        in the same tick. Unlike a submit or head read, a failure here is not
        part of the reconnect accounting: callers keep their last-good lookup
        and the following head read or submit still drives socket health.

        Raises:
            WeightSubmissionError: No connection is available, or the
                metagraph read failed.
        """
        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            raise WeightSubmissionError("no subtensor connection available for metagraph read")
        try:
            metagraph = run_with_timeout(
                "subtensor metagraph",
                lambda: subtensor.metagraph(netuid),
                self._rpc_timeout,
            )
        except Exception as exc:
            raise WeightSubmissionError(f"metagraph read failed: {exc}") from exc
        return {hotkey: uid for uid, hotkey in enumerate(metagraph.hotkeys)}

    def submit(
        self,
        *,
        netuid: int,
        uids: list[int],
        weights: list[int],
        epoch_id: int,
    ) -> None:
        """Submit one epoch's weight vector to the subnet.

        Args:
            netuid: Subnet id; must match the configured netuid.
            uids: Miner uids to set weights for.
            weights: Per-uid u16 weights summing to ``MAX_WEIGHT``.
            epoch_id: Finalized epoch id, for logging only.

        Raises:
            WeightSubmissionError: ``netuid`` mismatch, malformed input, a
                connection-level failure, or the chain rejecting the
                extrinsic for any reason. The validator loop retries the
                epoch on the next tick.
        """
        if netuid != self._netuid:
            raise WeightSubmissionError(
                f"netuid mismatch: submitter configured for {self._netuid}, "
                f"epoch {epoch_id} requested {netuid}"
            )
        if len(uids) != len(weights):
            raise WeightSubmissionError(
                f"uids/weights length mismatch: {len(uids)} != {len(weights)}"
            )
        if not uids:
            LOGGER.info("epoch %d: empty weight vector, nothing to submit", epoch_id)
            return

        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            raise WeightSubmissionError(f"epoch {epoch_id}: no subtensor connection available")

        LOGGER.info(
            "submitting weights: netuid=%d epoch=%d n_uids=%d sum=%d",
            netuid,
            epoch_id,
            len(uids),
            sum(weights),
        )

        def _set_weights() -> tuple[bool, str | None]:
            # Fire-and-forget into the mempool: wait_for_inclusion /
            # wait_for_finalization each open a per-submit block-event
            # subscription on the long-lived socket and block until the
            # extrinsic lands. Over multi-hour runs those subscriptions
            # accumulate and stall payouts; a run_with_timeout abandons
            # (does not cancel) the worker thread, so a submit wedged
            # inside the wait keeps its subscription alive on the shared
            # socket. With both False the SDK submits and returns at once,
            # holding no subscription. The trade is the SDK's inclusion
            # receipt: a mempool accept is not chain-inclusion proof, but a
            # dropped vector is superseded by the next epoch's submit and a
            # re-submitted identical vector is idempotent — so the loss is
            # bounded and the path never double-submits.
            # mev_protection=False forces the plain author_submitExtrinsic
            # path. The SDK default reads BT_MEV_PROTECTION from the env;
            # under MEV mode the SDK rejects wait_for_revealed_execution
            # (its own default True) combined with wait_for_inclusion=False,
            # so pinning it off keeps the fire-and-forget submit valid
            # regardless of a stray env var.
            return subtensor.set_weights(
                wallet=self._wallet,
                netuid=netuid,
                uids=uids,
                weights=weights,
                mev_protection=False,
                wait_for_inclusion=False,
                wait_for_finalization=False,
            )

        try:
            success, message = run_with_timeout(
                f"subtensor set_weights (epoch {epoch_id})",
                _set_weights,
                self._rpc_timeout,
            )
        except Exception as exc:
            # A raised exception is a connection-level failure (dead
            # websocket, RPC timeout). Count it toward reconnect but keep
            # the connection — the next submit reconnects once the counter
            # trips. Do NOT drop the socket here; per-error drops leak.
            self._consecutive_failures += 1
            raise WeightSubmissionError(
                f"epoch {epoch_id}: set_weights call failed: {exc}"
            ) from exc

        if not success:
            # A (False, message) return is a chain answer, so the socket is
            # healthy: it neither resets nor increments the failure counter
            # (only raised exceptions drive reconnect, mirroring the bm
            # validator). The validator loop retries the epoch next tick;
            # an "Already Imported" pool duplicate carries no inclusion
            # receipt, so it is retried like any other rejection rather
            # than silently marking the epoch submitted.
            raise WeightSubmissionError(
                f"epoch {epoch_id}: subtensor rejected set_weights: {message or ''}"
            )

        # A clean success proves the websocket is healthy — reset the
        # connection-failure counter.
        self._consecutive_failures = 0
        LOGGER.info("epoch %d: weights accepted by subtensor (%s)", epoch_id, message)


class RealChainCursor:
    """Derives the open epoch id from the chain head over the submitter's socket.

    Reads the head block through the ``RealSubmitter``'s long-lived
    connection so the validator holds exactly one websocket for both the
    head poll and weight submission. ``current_epoch`` returns the open
    epoch ``head_block // blocks_per_epoch`` — the same derivation the
    epoch-finalizer uses — or ``None`` when the head read fails, so a
    transient chain hiccup skips the tick instead of crashing the loop.
    """

    def __init__(self, submitter: RealSubmitter, blocks_per_epoch: int) -> None:
        if blocks_per_epoch <= 0:
            raise ValueError(f"blocks_per_epoch must be positive, got {blocks_per_epoch}")
        self._submitter = submitter
        self._blocks_per_epoch = blocks_per_epoch

    def current_epoch(self) -> int | None:
        try:
            block = self._submitter.head_block()
        except WeightSubmissionError as exc:
            LOGGER.warning("chain head read failed: %s — skipping this tick", exc)
            return None
        return block // self._blocks_per_epoch


class RealMetagraphSource:
    """Reads subnet hotkeys through the submitter's long-lived socket.

    The source is intentionally a tiny wrapper around ``RealSubmitter`` so
    validator ticks can refresh hotkey -> uid state without opening a second
    subtensor connection.
    """

    def __init__(self, submitter: RealSubmitter, netuid: int) -> None:
        self._submitter = submitter
        self._netuid = netuid

    def hotkeys(self) -> dict[str, int]:
        """Return the current hotkey ss58 -> uid mapping."""
        return self._submitter.metagraph_hotkeys(self._netuid)
