"""Metagraph-driven miner hotkey -> uid lookup.

``Validator._uids_and_weights`` translates hotkey-keyed scores into the
``(uid, weight)`` lists ``subtensor.set_weights`` expects. The mapping
comes from the subnet metagraph: every registered neuron has a uid and a
hotkey ss58 address.

This module is lazy-imported by ``main.py`` only on the real-submission
path; the test suite passes an explicit dict instead, so it never pulls
in the ``bittensor`` SDK.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


def load_miner_uid_lookup(netuid: int, endpoint: str | None) -> dict[str, int]:
    """Query the subnet metagraph and return a hotkey -> uid mapping.

    Args:
        netuid: Subnet id to read the metagraph for.
        endpoint: Subtensor websocket URL; ``None`` selects the SDK
            default network.

    Returns:
        Mapping of neuron hotkey ss58 address to its uid.

    Raises:
        RuntimeError: The metagraph could not be fetched.
    """
    import bittensor

    try:
        subtensor = bittensor.Subtensor(network=endpoint) if endpoint else bittensor.Subtensor()
        metagraph = subtensor.metagraph(netuid)
    except Exception as exc:
        raise RuntimeError(
            f"failed to fetch metagraph for netuid={netuid} "
            f"(endpoint={endpoint or '<default>'}): {exc}"
        ) from exc

    lookup: dict[str, int] = {}
    for uid, hotkey in enumerate(metagraph.hotkeys):
        lookup[hotkey] = uid
    LOGGER.info("metagraph netuid=%d: loaded %d hotkey->uid entries", netuid, len(lookup))
    return lookup
