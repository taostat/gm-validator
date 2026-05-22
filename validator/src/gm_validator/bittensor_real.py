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

The hotkey is loaded entirely in memory — no wallet keyfile is read from
disk. ``BITTENSOR_HOTKEY_FILE`` carries the bittensor unencrypted-hotkey
JSON document; its ``secretSeed`` field seeds ``bt.Keypair.create_from_seed``.

Config (all env-driven via ``ValidatorConfig``):

- ``BITTENSOR_NETUID`` — subnet id the validator scores.
- ``BITTENSOR_ENDPOINT`` — subtensor websocket URL (``wss://...``). When
  unset the SDK default network ("finney" mainnet) is used.
- ``BITTENSOR_HOTKEY_FILE`` — the validator hotkey keyfile contents (the
  bittensor unencrypted-hotkey JSON document); the ``secretSeed`` field
  seeds the in-memory signing keypair.
"""

from __future__ import annotations

import json
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class WeightSubmissionError(RuntimeError):
    """The subtensor rejected a ``set_weights`` extrinsic."""


class HotkeyConfigError(RuntimeError):
    """The hotkey keyfile blob could not be parsed into a seed."""


def _seed_from_hotkey_file(hotkey_file: str) -> str:
    """Extract the ``secretSeed`` from a bittensor hotkey keyfile blob.

    Args:
        hotkey_file: Contents of a bittensor unencrypted-hotkey keyfile —
            a JSON document with an ``accountId`` / ``publicKey`` /
            ``privateKey`` / ``secretPhrase`` / ``secretSeed`` /
            ``ss58Address`` shape.

    Returns:
        The ``secretSeed`` value, a ``0x``-prefixed hex string suitable
        for ``bittensor.Keypair.create_from_seed``.

    Raises:
        HotkeyConfigError: The blob is not valid JSON, or it carries no
            usable ``secretSeed``.
    """
    try:
        keyfile = json.loads(hotkey_file)
    except json.JSONDecodeError as exc:
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_FILE is not valid JSON; it must be the "
            "bittensor unencrypted-hotkey keyfile document"
        ) from exc

    if not isinstance(keyfile, dict):
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_FILE must be a JSON object (the bittensor hotkey keyfile document)"
        )

    seed = keyfile.get("secretSeed")
    if not seed or not isinstance(seed, str):
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_FILE has no 'secretSeed' string; the "
            "keyfile must be UNENCRYPTED (an encrypted keyfile carries "
            "no plaintext seed)"
        )
    return seed


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
        hotkey_file: str,
    ) -> None:
        """Build the in-memory hotkey and connect to subtensor.

        Args:
            netuid: Subnet id the validator submits weights for.
            endpoint: Subtensor websocket URL; ``None`` selects the SDK
                default network.
            hotkey_file: Contents of the bittensor unencrypted-hotkey
                keyfile; its ``secretSeed`` seeds the signing keypair.

        Raises:
            HotkeyConfigError: The hotkey keyfile blob carries no usable
                seed.
            WeightSubmissionError: The keypair could not be built or the
                subtensor endpoint could not be reached.
        """
        import bittensor

        self._netuid = netuid
        self._endpoint = endpoint
        seed = _seed_from_hotkey_file(hotkey_file)
        try:
            hotkey: Any = bittensor.Keypair.create_from_seed(seed)
            self._wallet: Any = _KeypairWallet(hotkey)
            hotkey_ss58 = hotkey.ss58_address
            self._subtensor: Any = (
                bittensor.Subtensor(network=endpoint) if endpoint else bittensor.Subtensor()
            )
        except Exception as exc:
            raise WeightSubmissionError(
                f"failed to build bittensor hotkey/subtensor from seed (endpoint={endpoint}): {exc}"
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
