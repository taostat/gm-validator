"""Tests for the subtensor connection retry helper.

``connect_subtensor`` wraps ``bittensor.Subtensor()`` with bounded
exponential backoff and a per-attempt connect timeout. The tests inject a
fake factory so no bittensor SDK or websocket is touched, and inject a
fake sleep so the suite stays fast.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from gm_validator.subtensor_connect import connect_subtensor, run_with_timeout


class _FakeSubtensor:
    def __init__(self, endpoint: str | None) -> None:
        self.endpoint = endpoint


def test_run_with_timeout_returns_result() -> None:
    assert run_with_timeout("noop", lambda: 7, timeout=1.0) == 7


def test_run_with_timeout_reraises_callable_error() -> None:
    def _boom() -> int:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_with_timeout("boom", _boom, timeout=1.0)


def test_run_with_timeout_raises_on_hang() -> None:
    """A callable that never finishes within the budget raises TimeoutError
    rather than blocking the caller forever. The 5s wait is a teardown
    backstop; the 0.05s budget fires first."""
    release = threading.Event()
    try:
        with pytest.raises(TimeoutError, match="wedged"):
            run_with_timeout("wedged", lambda: release.wait(timeout=5.0), timeout=0.05)
    finally:
        release.set()


def test_first_attempt_succeeds_no_sleep() -> None:
    sleeps: list[float] = []
    calls: list[str | None] = []

    def factory(endpoint: str | None) -> Any:
        calls.append(endpoint)
        return _FakeSubtensor(endpoint)

    result = connect_subtensor("wss://node", factory=factory, sleep=sleeps.append)

    assert isinstance(result, _FakeSubtensor)
    assert result.endpoint == "wss://node"
    assert calls == ["wss://node"]
    assert sleeps == []


def test_retries_then_succeeds() -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    def factory(_endpoint: str | None) -> Any:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise ConnectionError("HTTP 429")
        return _FakeSubtensor(_endpoint)

    result = connect_subtensor(None, factory=factory, sleep=sleeps.append)

    assert isinstance(result, _FakeSubtensor)
    assert len(attempts) == 3
    # Two sleeps before the successful third attempt; backoff doubles.
    assert sleeps == [1.0, 2.0]


def test_raises_last_exception_after_max_attempts() -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    def factory(_endpoint: str | None) -> Any:
        attempts.append(len(attempts) + 1)
        raise ConnectionError(f"attempt {len(attempts)} failed")

    with pytest.raises(ConnectionError, match="attempt 8 failed"):
        connect_subtensor("wss://node", factory=factory, sleep=sleeps.append)

    # 8 attempts → 7 sleeps; backoff doubles then caps at 60s.
    assert len(attempts) == 8
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


def test_passes_none_endpoint_to_factory() -> None:
    seen: list[str | None] = []

    def factory(endpoint: str | None) -> Any:
        seen.append(endpoint)
        return _FakeSubtensor(endpoint)

    connect_subtensor(None, factory=factory, sleep=lambda _s: None)

    assert seen == [None]


def test_hanging_factory_times_out_then_retries() -> None:
    """A factory that *hangs* (never raises) must not freeze startup.

    The first attempt blocks on an event the test never releases until
    teardown; the bounded connect timeout converts that hang into a
    TimeoutError the retry loop catches, and the second attempt returns.
    Without the timeout the call would block forever — this test would
    hang rather than fail, which is the bug it guards.
    """
    release = threading.Event()
    attempts: list[int] = []
    sleeps: list[float] = []

    def factory(endpoint: str | None) -> Any:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            release.wait(timeout=5.0)
            return _FakeSubtensor(endpoint)
        return _FakeSubtensor(endpoint)

    try:
        result = connect_subtensor(
            "wss://node",
            factory=factory,
            sleep=sleeps.append,
            connect_timeout=0.05,
        )
    finally:
        release.set()

    assert isinstance(result, _FakeSubtensor)
    assert len(attempts) == 2
    assert sleeps == [1.0]


def test_persistent_hang_exhausts_attempts_with_timeouterror() -> None:
    """Every attempt hanging surfaces a TimeoutError after the budget.

    The pod still crash-loops on a permanently dead endpoint, but it does
    so through a raised error the kubelet sees, not a silent freeze.
    """
    release = threading.Event()
    attempts: list[int] = []

    def factory(_endpoint: str | None) -> Any:
        attempts.append(len(attempts) + 1)
        release.wait(timeout=5.0)
        return _FakeSubtensor(_endpoint)

    try:
        with pytest.raises(TimeoutError):
            connect_subtensor(
                "wss://node",
                factory=factory,
                sleep=lambda _s: None,
                connect_timeout=0.05,
            )
    finally:
        release.set()

    assert len(attempts) == 8
