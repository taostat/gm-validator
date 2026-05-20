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
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class WeightSubmissionError(RuntimeError):
    """The subtensor rejected a ``set_weights`` extrinsic."""


class RealSubmitter:
    """Real bittensor weight submitter.

    Opens the wallet and a subtensor connection at construction time so
    that a misconfigured wallet fails fast on startup rather than on the
    first finalized epoch.
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
        try:
            self._wallet: Any = bittensor.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            # Touch the hotkey so a missing/locked keyfile fails here.
            hotkey_ss58 = self._wallet.hotkey.ss58_address
            self._subtensor: Any = (
                bittensor.Subtensor(network=endpoint) if endpoint else bittensor.Subtensor()
            )
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

    def submit(
        self,
        *,
        netuid: int,
        uids: list[int],
        weights: list[float],
        epoch_id: int,
    ) -> None:
        """Submit one epoch's weight vector to the subnet.

        Args:
            netuid: Subnet id; must match the configured netuid.
            uids: Miner uids to set weights for.
            weights: Per-uid weights, aligned with ``uids``.
            epoch_id: Finalized epoch id, for logging only.

        Raises:
            WeightSubmissionError: ``netuid`` mismatch, malformed input,
                or the chain rejected the extrinsic.
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

        LOGGER.info(
            "submitting weights: netuid=%d epoch=%d n_uids=%d sum=%.4f",
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
            raise WeightSubmissionError(
                f"epoch {epoch_id}: set_weights call failed: {exc}"
            ) from exc

        if not success:
            raise WeightSubmissionError(
                f"epoch {epoch_id}: subtensor rejected set_weights: {message}"
            )
        LOGGER.info("epoch %d: weights accepted by subtensor (%s)", epoch_id, message)
