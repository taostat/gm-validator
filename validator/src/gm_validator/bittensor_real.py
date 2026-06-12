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
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


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

    Builds the hotkey keypair in memory and opens a subtensor connection
    at construction time so that a malformed seed or unreachable endpoint
    fails fast on startup rather than on the first finalized epoch.
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
        from gm_validator.subtensor_connect import connect_subtensor

        self._netuid = netuid
        self._endpoint = endpoint
        hotkey = _keypair_from_seed(hotkey_seed)
        self._wallet: Any = _KeypairWallet(hotkey)
        hotkey_ss58 = hotkey.ss58_address
        try:
            # connect_subtensor retries transient endpoint failures (HTTP
            # 429 from public testnet RPCs in particular) so a brief
            # rate-limit window does not crash the pod into a tight
            # restart loop.
            self._subtensor: Any = connect_subtensor(endpoint)
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
            raise WeightSubmissionError(
                f"epoch {epoch_id}: set_weights call failed: {exc}"
            ) from exc

        if not success:
            raise WeightSubmissionError(
                f"epoch {epoch_id}: subtensor rejected set_weights: {message}"
            )
        LOGGER.info("epoch %d: weights accepted by subtensor (%s)", epoch_id, message)
