"""Tests for the subtensor connection retry helper.

``connect_subtensor`` wraps ``bittensor.Subtensor()`` with bounded
exponential backoff. The tests inject a fake factory so no bittensor SDK
or websocket is touched, and inject a fake sleep so the suite stays
fast.
"""

from __future__ import annotations

from typing import Any

import pytest

from gm_validator.subtensor_connect import connect_subtensor


class _FakeSubtensor:
    def __init__(self, endpoint: str | None) -> None:
        self.endpoint = endpoint


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
