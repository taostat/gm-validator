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

Config (all env-driven via ``ValidatorConfig``):

- ``BITTENSOR_NETUID`` — subnet id the validator scores.
- ``BITTENSOR_ENDPOINT`` — subtensor websocket URL (``wss://...``). When
  unset the SDK default network ("finney" mainnet) is used.
- ``BITTENSOR_HOTKEY_SEED`` — the validator hotkey's secret seed: a
  BIP-39 mnemonic (space-separated words) or a ``0x``-prefixed hex seed.
  The signing keypair is built in memory from it.

Reconnect handling:

The bittensor SDK holds a single websocket per ``Subtensor`` instance.
When that websocket churns (idle close, network blip, validator
endpoint failover) the SDK's internal retry can re-broadcast the same
signed extrinsic during a ``wait_for_inclusion=True`` call; the chain
then raises ``Transaction Already Imported``. Because the call was
waiting on inclusion, the raise is positive evidence that the chain
holds the extrinsic. We surface that as ``AlreadySubmittedError`` (a
subclass of ``WeightSubmissionError``) so the caller can mark the epoch
processed instead of looping.

Three deliberate restrictions on what counts as "already submitted":

- Only the raised-exception path with the ``Already Imported`` message
  classifies. The ``(success=False, message)`` return shape, even with
  the same text, carries no inclusion receipt — it can be a transaction
  pool that sees a still-pending duplicate which may later be dropped,
  so we treat it as a plain failure and retry next tick.
- ``Invalid Transaction (bad signature)`` is excluded. Substrate emits
  it for genuine signing failures (corrupt keyfile, wrong hotkey,
  runtime upgrade) as well as stale-nonce replays.
- ``Stale`` / ``Transaction is outdated`` are excluded. They mean the
  validator hotkey's nonce has been advanced by some extrinsic, not
  necessarily this one's weights vector.

When a duplicate broadcast trips one of the excluded shapes, the outer
validator loop logs and retries the epoch on the next tick; the retry
then either matches ``Already Imported`` cleanly or succeeds.

After any submission error we also drop the cached ``Subtensor`` so the
next epoch opens a fresh websocket; otherwise a dead connection would
poison every subsequent submission for the lifetime of the pod.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


# Substrate's pool-side dedup response, lower-cased for matching. This
# is the only message that unambiguously proves the chain already holds
# this exact extrinsic — see module docstring for what we deliberately
# do NOT classify here.
_ALREADY_IMPORTED_FRAGMENT = "already imported"


def _looks_already_submitted(message: str) -> bool:
    """True iff *message* is substrate's "extrinsic already imported" error."""
    return _ALREADY_IMPORTED_FRAGMENT in message.lower()


class WeightSubmissionError(RuntimeError):
    """The subtensor rejected a ``set_weights`` extrinsic."""


class AlreadySubmittedError(WeightSubmissionError):
    """The chain already has this extrinsic — submission is logically done.

    Raised when a substrate runtime error indicates the signed extrinsic
    is already known to the chain (duplicate broadcast after a websocket
    reconnect). The caller should treat the epoch as submitted and mark
    it processed; retrying would re-broadcast the same extrinsic and hit
    the same error.
    """


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

    Builds the hotkey keypair in memory and opens a subtensor connection
    at construction time so that a malformed seed or unreachable endpoint
    fails fast on startup rather than on the first finalized epoch. The
    subtensor connection is recreated lazily after any submission error
    so a dropped websocket does not poison subsequent epochs.
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
        connect and lazy reconnects after a submission error retry
        transient endpoint failures (HTTP 429 from public testnet RPCs in
        particular) instead of crash-looping the pod.
        """
        from gm_validator.subtensor_connect import connect_subtensor

        return connect_subtensor(self._endpoint)

    def _reset_subtensor(self) -> None:
        """Close and drop the cached subtensor so the next submit opens a fresh one.

        The bittensor SDK does not expose a stable "is the websocket
        alive" check, and replacing the connection is cheap relative to
        an epoch (60s+). After any submission error we close the cached
        websocket and drop the instance; the next ``submit`` calls
        ``_open_subtensor`` again. ``close()`` errors are suppressed
        because we are already on the failure path — leaking the
        connection to GC would also work but the explicit close prevents
        websocket accumulation across many failed epochs.
        """
        subtensor = self._subtensor
        self._subtensor = None
        if subtensor is None:
            return
        close = getattr(subtensor, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup; connection is discarded either way
            LOGGER.debug("subtensor close failed during reset: %s", exc)

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
            AlreadySubmittedError: The chain already has this extrinsic —
                a previous submission for this epoch (likely retried by
                the SDK after a websocket reconnect) has been accepted.
                Callers should treat the epoch as submitted.
            WeightSubmissionError: ``netuid`` mismatch, malformed input,
                or the chain rejected the extrinsic for any other reason.
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

        if self._subtensor is None:
            # Previous submission failed and reset the connection; open
            # a fresh one now.
            try:
                self._subtensor = self._open_subtensor()
            except Exception as exc:
                raise WeightSubmissionError(
                    f"epoch {epoch_id}: failed to reopen subtensor: {exc}"
                ) from exc

        LOGGER.info(
            "submitting weights: netuid=%d epoch=%d n_uids=%d sum=%d",
            netuid,
            epoch_id,
            len(uids),
            sum(weights),
        )
        try:
            # raise_error=True makes the SDK re-raise substrate errors
            # (notably "Transaction Already Imported" on a reconnect
            # rebroadcast) instead of swallowing them into a
            # (success=False, message) ExtrinsicResponse. The classifier
            # below only treats the raised-exception path under
            # wait_for_inclusion=True as positive evidence of inclusion;
            # without raise_error it would never see that exception.
            success, message = self._subtensor.set_weights(
                wallet=self._wallet,
                netuid=netuid,
                uids=uids,
                weights=weights,
                wait_for_inclusion=True,
                wait_for_finalization=False,
                raise_error=True,
            )
        except Exception as exc:
            # Drop the connection so the next epoch starts fresh; the
            # websocket that just errored may be dead.
            self._reset_subtensor()
            message = str(exc)
            if _looks_already_submitted(message):
                LOGGER.info(
                    "epoch %d: chain already has this extrinsic (%s); treating as submitted",
                    epoch_id,
                    message,
                )
                raise AlreadySubmittedError(
                    f"epoch {epoch_id}: extrinsic already on chain: {message}"
                ) from exc
            raise WeightSubmissionError(
                f"epoch {epoch_id}: set_weights call failed: {exc}"
            ) from exc

        if not success:
            # The ``(False, message)`` return shape carries no inclusion
            # receipt — even an "Already Imported" payload here only
            # means the pool sees a duplicate, which can be a still-
            # pending tx that may later be dropped. We treat it as a
            # plain failure so the next tick retries; the retry will
            # either succeed cleanly or raise ``Already Imported`` from
            # ``wait_for_inclusion=True``, which IS positive evidence.
            # Reset the connection regardless — another epoch is a
            # minute out.
            self._reset_subtensor()
            raise WeightSubmissionError(
                f"epoch {epoch_id}: subtensor rejected set_weights: {message}"
            )
        LOGGER.info("epoch %d: weights accepted by subtensor (%s)", epoch_id, message)
