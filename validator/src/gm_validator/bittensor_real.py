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

``set_weights`` is called with ``wait_for_inclusion=True,
wait_for_finalization=True`` and the SDK default ``raise_error=False``, so
substrate errors come back as an ``ExtrinsicResponse`` (unpacks as
``(success, message)``) rather than a raised exception. Two outcomes drive
the lifecycle:

- A raised exception is a connection-level failure (dead websocket, RPC
  timeout). It increments the failure counter so the connection is
  recreated once three pile up, and surfaces as ``WeightSubmissionError``.
- A ``(False, message)`` return is a chain answer, so the socket is
  healthy: it neither resets nor increments the counter, and surfaces as
  ``WeightSubmissionError`` so the validator loop retries the epoch on the
  next tick.

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
    ) -> None:
        """Build the in-memory hotkey and connect to subtensor.

        Args:
            netuid: Subnet id the validator submits weights for.
            endpoint: Subtensor websocket URL; ``None`` selects the SDK
                default network.
            hotkey_seed: The validator hotkey seed — a BIP-39 mnemonic or
                a ``0x``-prefixed hex seed. The signing keypair is built
                in memory from it; no keyfile is read from disk.

        Raises:
            HotkeyConfigError: The seed is missing or malformed.
            WeightSubmissionError: The subtensor endpoint could not be
                reached.
        """
        self._netuid = netuid
        self._endpoint = endpoint
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

        return connect_subtensor(self._endpoint)

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

        Raises:
            WeightSubmissionError: No connection is available, or the head
                read failed at the connection level.
        """
        self._maybe_reconnect()
        if self._subtensor is None:
            raise WeightSubmissionError("no subtensor connection available for head-block read")
        try:
            block = self._subtensor.get_current_block()
        except Exception as exc:
            self._consecutive_failures += 1
            raise WeightSubmissionError(f"head-block read failed: {exc}") from exc
        return int(block)

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
        if self._subtensor is None:
            raise WeightSubmissionError(f"epoch {epoch_id}: no subtensor connection available")

        LOGGER.info(
            "submitting weights: netuid=%d epoch=%d n_uids=%d sum=%d",
            netuid,
            epoch_id,
            len(uids),
            sum(weights),
        )
        try:
            success, message = self._subtensor.set_weights(
                wallet=self._wallet,
                netuid=netuid,
                uids=uids,
                weights=weights,
                wait_for_inclusion=True,
                wait_for_finalization=True,
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
