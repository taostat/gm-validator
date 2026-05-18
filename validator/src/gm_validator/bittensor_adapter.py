"""Bittensor adapter — submits weights via `subtensor.set_weights`.

In production this wraps `bittensor-py`. During build we run against
the `MockSubmitter`, which records calls in memory and lets tests
assert the expected vector was emitted. The two implementations share a
small `Submitter` protocol so the validator's hot-path code never
imports `bittensor` directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

LOGGER = logging.getLogger(__name__)


class Submitter(Protocol):
    """Minimal interface the validator uses for weight submission."""

    def submit(self, *, netuid: int, uids: list[int], weights: list[float], epoch_id: int) -> None:
        """Submit one epoch's weights to the subnet."""
        ...


@dataclass
class MockSubmitter:
    """In-memory submitter for tests and Phase 1 build.

    The real bittensor-py submitter lands in Phase 2 alongside the
    testnet deploy.
    """

    calls: list[dict] = field(default_factory=list)

    def submit(self, *, netuid: int, uids: list[int], weights: list[float], epoch_id: int) -> None:
        LOGGER.info(
            "mock submit: netuid=%d epoch=%d n_uids=%d sum=%.4f",
            netuid,
            epoch_id,
            len(uids),
            sum(weights),
        )
        self.calls.append(
            {
                "netuid": netuid,
                "uids": list(uids),
                "weights": list(weights),
                "epoch_id": epoch_id,
            }
        )
