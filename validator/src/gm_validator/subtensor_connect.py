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

Backoff alone only covers a connect that *raises*. The SDK construction
takes no connect timeout, so a connect that *hangs* — a half-open socket,
or the public endpoint stalling a second rapid websocket behind its
rate-limiter — would freeze startup forever, below the retry loop. The
construction therefore runs in a worker thread bounded by a wall-clock
``connect_timeout``; a thread that does not finish in time becomes a
``TimeoutError`` the retry loop catches like any other transient failure.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

# Default wall-clock budget for one connect attempt when no explicit
# timeout is passed; production callers thread ValidatorConfig's value
# through, this only backstops tests and direct calls.
DEFAULT_CONNECT_TIMEOUT_SECS = 30.0

# Bounded exponential backoff: 8 attempts, doubling from 1s, capped at
# 60s. Total budget is ~4 minutes — long enough to ride out a brief
# rate-limit window, short enough that a real misconfig still fails the
# pod before the kubelet's CrashLoopBackOff cap kicks in.
_MAX_ATTEMPTS = 8
_INITIAL_BACKOFF_SECS = 1.0
_MAX_BACKOFF_SECS = 60.0


def _call_with_timeout(
    factory: Callable[[str | None], Any],
    endpoint: str | None,
    timeout: float,
) -> Any:
    """Run ``factory(endpoint)`` in a worker thread, bounded by ``timeout``.

    The SDK construction is synchronous with no connect timeout, so a hung
    connect can only be unblocked from outside. The worker is a daemon
    thread: a connect that never finishes is abandoned rather than joined
    forever, and is never returned into the success path. A connect that
    finishes after the timeout (a late success) is likewise discarded —
    the result is only read when ``join`` proves the thread finished in
    time.

    Raises:
        TimeoutError: ``factory`` did not finish within ``timeout``.
        Exception: Whatever ``factory`` raised, re-raised in this thread.
    """
    result: list[Any] = []
    error: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(factory(endpoint))
        except BaseException as exc:  # noqa: BLE001 — re-raised below in the caller's thread; the worker must not let any failure escape unobserved.
            error.append(exc)

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(
            f"subtensor connect did not complete within {timeout:.1f}s "
            f"(endpoint={endpoint or '<default>'})"
        )
    if error:
        raise error[0]
    return result[0]


def connect_subtensor(
    endpoint: str | None,
    *,
    factory: Callable[[str | None], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECS,
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
        connect_timeout: Per-attempt wall-clock budget. A connect that
            hangs past this becomes a retryable ``TimeoutError`` instead
            of freezing startup.

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
            return _call_with_timeout(factory, endpoint, connect_timeout)
        except Exception as exc:  # noqa: BLE001 — bittensor surfaces transient failures (HTTP 429, websocket close, DNS, hung-connect TimeoutError) as a varied bag of exception types; the retry loop is endpoint-construction-only so a blind catch is the correct surface here.
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
