"""Real bittensor submitter — submits weights to the subtensor chain.

This wraps the ``bittensor`` SDK (v10). It opens a wallet, connects to a
subtensor endpoint, and calls ``subtensor.set_weights`` once per epoch.
The validator's hot-path code never imports ``bittensor`` directly — it
talks to the ``Submitter`` protocol in ``bittensor_adapter`` — so the
test suite (which uses ``MockSubmitter``) does not pull in the heavy
dependency.

The ``bittensor`` import is deferred to the methods that need it so that
merely importing this module is cheap; only constructing ``RealSubmitter``
or calling ``submit`` requires the SDK to be installed.

Config (all env-driven via ``ValidatorConfig``):

- ``BITTENSOR_NETUID`` — subnet id the validator scores.
- ``BITTENSOR_ENDPOINT`` — subtensor websocket URL (``wss://...``). When
  unset the SDK default network ("finney" mainnet) is used.
- ``BITTENSOR_WALLET_NAME`` — coldkey wallet name on disk.
- ``BITTENSOR_WALLET_HOTKEY`` — hotkey within that wallet; this is the
  validator's signing key for ``set_weights`` extrinsics.

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


class RealSubmitter:
    """Real bittensor weight submitter.

    Opens the wallet and a subtensor connection at construction time so
    that a misconfigured wallet fails fast on startup rather than on the
    first finalized epoch. The subtensor connection is recreated lazily
    after any submission error so a dropped websocket does not poison
    subsequent epochs.
    """

    def __init__(
        self,
        netuid: int,
        endpoint: str | None,
        wallet_name: str,
        wallet_hotkey: str,
    ) -> None:
        """Open the wallet and connect to subtensor.

        Args:
            netuid: Subnet id the validator submits weights for.
            endpoint: Subtensor websocket URL; ``None`` selects the SDK
                default network.
            wallet_name: Coldkey wallet name on disk.
            wallet_hotkey: Hotkey name within the wallet; the validator's
                signing key.

        Raises:
            WeightSubmissionError: The wallet could not be opened or the
                subtensor endpoint could not be reached.
        """
        import bittensor

        self._netuid = netuid
        self._endpoint = endpoint
        self._subtensor: Any | None = None
        try:
            self._wallet: Any = bittensor.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            # Touch the hotkey so a missing/locked keyfile fails here.
            hotkey_ss58 = self._wallet.hotkey.ss58_address
            self._subtensor = self._open_subtensor()
        except Exception as exc:
            raise WeightSubmissionError(
                f"failed to initialise bittensor wallet/subtensor "
                f"(wallet={wallet_name}/{wallet_hotkey}, endpoint={endpoint}): {exc}"
            ) from exc
        LOGGER.info(
            "RealSubmitter ready: netuid=%d hotkey=%s endpoint=%s",
            netuid,
            hotkey_ss58,
            endpoint or "<default>",
        )

    def _open_subtensor(self) -> Any:
        """Open a fresh subtensor connection at the configured endpoint."""
        import bittensor

        if self._endpoint:
            return bittensor.Subtensor(network=self._endpoint)
        return bittensor.Subtensor()

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
            success, message = self._subtensor.set_weights(
                wallet=self._wallet,
                netuid=netuid,
                uids=uids,
                weights=weights,
                wait_for_inclusion=True,
                wait_for_finalization=False,
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
