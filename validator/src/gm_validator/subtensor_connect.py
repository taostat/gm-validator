"""Resilient subtensor connection at startup.

``bittensor.Subtensor()`` opens a websocket synchronously and raises on
the first failure. Public testnet endpoints rate-limit aggressively
(HTTP 429), and the validator runs under a kubelet that restarts the
container on exit. A naked construction therefore turns a brief 429
window into a tight crash-loop: the pod is recreated in seconds, dials
the same endpoint, gets rejected again, exits, repeat. In practice this
has produced 139 restarts inside 10 hours before the endpoint cleared.

This module retries the construction with exponential backoff so a
transient outage at startup costs minutes, not container restarts. The
loop only retries — the kubelet still restarts the container if every
attempt fails, so a misconfigured endpoint still surfaces as a crash.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

# Bounded exponential backoff: 8 attempts, doubling from 1s, capped at
# 60s. Total budget is ~4 minutes — long enough to ride out a brief
# rate-limit window, short enough that a real misconfig still fails the
# pod before the kubelet's CrashLoopBackOff cap kicks in.
_MAX_ATTEMPTS = 8
_INITIAL_BACKOFF_SECS = 1.0
_MAX_BACKOFF_SECS = 60.0


def connect_subtensor(
    endpoint: str | None,
    *,
    factory: Callable[[str | None], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Construct a ``bittensor.Subtensor`` with retry on transient failure.

    Args:
        endpoint: Subtensor websocket URL; ``None`` selects the SDK
            default network.
        factory: Constructor invoked with ``endpoint``; defaults to
            ``bittensor.Subtensor``. Tests inject a fake to avoid
            importing the SDK.
        sleep: Sleep function; tests inject a no-op to keep the suite
            fast.

    Returns:
        The constructed ``Subtensor`` instance.

    Raises:
        The last exception from the factory after ``_MAX_ATTEMPTS``
        failed attempts.
    """
    if factory is None:
        import bittensor

        def factory(ep: str | None) -> Any:
            return bittensor.Subtensor(network=ep) if ep else bittensor.Subtensor()

    backoff = _INITIAL_BACKOFF_SECS
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return factory(endpoint)
        except Exception as exc:  # noqa: BLE001 — bittensor surfaces transient failures (HTTP 429, websocket close, DNS) as a varied bag of exception types; the retry loop is endpoint-construction-only so a blind catch is the correct surface here.
            last_exc = exc
            if attempt == _MAX_ATTEMPTS:
                break
            LOGGER.warning(
                "subtensor connect attempt %d/%d failed (endpoint=%s): %s; retrying in %.1fs",
                attempt,
                _MAX_ATTEMPTS,
                endpoint or "<default>",
                exc,
                backoff,
            )
            sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECS)
    assert last_exc is not None
    raise last_exc
