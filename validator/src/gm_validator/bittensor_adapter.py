"""Bittensor adapter — submits weights via `subtensor.set_weights`.

In production this wraps `bittensor-py`. During build we run against
the `MockSubmitter`, which records calls in memory and lets tests
assert the expected vector was emitted. The two implementations share a
small `Submitter` protocol so the validator's hot-path code never
imports `bittensor` directly.

Weights are submitted as u16 ints summing to ``MAX_WEIGHT`` (65535) per
the bm-validator convention — the chain expects integer weight vectors,
and integer arithmetic side-steps the float-dust normalisation issues
``set_weights`` would otherwise perform.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidatorWeightStatus:
    """The validator hotkey's own on-chain weight-setting status.

    Read each tick to (a) log how long since this validator's weights last
    *landed* on-chain, (b) skip a submit the chain's weight-set rate limit
    would reject anyway, and (c) detect a commit-reveal *reveal* by watching
    ``last_update_block`` advance.

    ``last_update_block`` advances only when weights are actually applied
    on-chain — on a commit-reveal subnet that is the *reveal*, not the
    commit. ``registered`` is False when the validator hotkey has no uid on
    the subnet yet (so weights can't be set at all). The chain's rate limit
    gates the commit too, so a freshly-registered or just-revealed validator
    must wait ``weights_rate_limit`` blocks before its next submit is
    accepted — this mirrors bm's ``blocks_since_last_update`` visibility.
    """

    registered: bool
    last_update_block: int | None
    current_block: int
    weights_rate_limit: int

    @property
    def blocks_since_last_update(self) -> int | None:
        """Blocks since this validator's weights last landed, or None if unregistered."""
        if self.last_update_block is None:
            return None
        return self.current_block - self.last_update_block

    @property
    def within_rate_limit_window(self) -> bool:
        """True when a submit now would be rejected by the weight-set rate limit.

        Conservative: a zero/unknown rate limit reports False so the submit
        proceeds and the chain's own gate is the final authority.
        """
        bs = self.blocks_since_last_update
        return bs is not None and self.weights_rate_limit > 0 and bs <= self.weights_rate_limit


class Submitter(Protocol):
    """Minimal interface the validator uses for weight submission."""

    def submit(self, *, netuid: int, uids: list[int], weights: list[int], epoch_id: int) -> None:
        """Submit one epoch's weights to the subnet."""
        ...

    def weight_status(self) -> ValidatorWeightStatus | None:
        """Return the validator hotkey's on-chain weight status, or None.

        None means 'unknown' (mock mode, or a transient chain read failure) —
        callers fall back to submitting and let the chain's own rate-limit
        gate decide, so a status read failure never blocks a submit.
        """
        ...


class ChainCursor(Protocol):
    """Reads the chain head block and derives the current epoch id.

    The validator targets the newest *closed* epoch each tick by reading
    the chain head — `head_block // blocks_per_epoch` is the open epoch,
    so the newest closed one is that minus one. This is the same
    derivation the epoch-finalizer uses, and it replaces scanning S3 for
    finalized markers: the chain head is the discovery cursor.
    """

    def current_epoch(self) -> int | None:
        """Return the open epoch id from the chain head, or None if unreadable.

        A None return is transient (the chain read failed); the validator
        skips the tick and retries on the next one rather than crashing.
        """
        ...


class MetagraphSource(Protocol):
    """Reads the subnet metagraph hotkey mapping for miner uid resolution.

    The validator refreshes this lookup each tick before scoring so miners
    that registered after startup can receive weights without a process
    restart. Real implementations should reuse the submitter's existing
    subtensor connection rather than opening a second socket.
    """

    def hotkeys(self) -> dict[str, int]:
        """Return the current hotkey ss58 -> uid mapping."""
        ...


@dataclass
class MockChainCursor:
    """In-memory chain cursor for tests and the mock-submitter build.

    ``epoch`` is the *open* epoch reported by the (fake) chain head; the
    validator targets ``epoch - 1`` as the newest closed epoch. Set it to
    ``None`` to simulate an unreadable chain head.
    """

    epoch: int | None = 0

    def current_epoch(self) -> int | None:
        return self.epoch


@dataclass
class MockSubmitter:
    """In-memory submitter for tests and Phase 1 build."""

    calls: list[dict] = field(default_factory=list)
    status: ValidatorWeightStatus | None = None

    def weight_status(self) -> ValidatorWeightStatus | None:
        return self.status

    def submit(self, *, netuid: int, uids: list[int], weights: list[int], epoch_id: int) -> None:
        LOGGER.info(
            "mock submit: netuid=%d epoch=%d n_uids=%d sum=%d",
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
