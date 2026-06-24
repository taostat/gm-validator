"""``ValidatorWeightStatus`` — derived properties used by the rate-limit
pre-gate and the reveal detector."""

from __future__ import annotations

from gm_validator.bittensor_adapter import ValidatorWeightStatus


def _status(last_update: int | None, head: int, rate_limit: int) -> ValidatorWeightStatus:
    return ValidatorWeightStatus(
        registered=last_update is not None,
        last_update_block=last_update,
        current_block=head,
        weights_rate_limit=rate_limit,
    )


def test_blocks_since_last_update_is_head_minus_last_update() -> None:
    assert _status(last_update=100, head=150, rate_limit=100).blocks_since_last_update == 50


def test_blocks_since_is_none_when_unregistered() -> None:
    status = _status(last_update=None, head=150, rate_limit=100)
    assert status.blocks_since_last_update is None
    assert status.within_rate_limit_window is False


def test_within_window_when_inside_rate_limit() -> None:
    # 50 <= 100 -> a submit now would be rejected by the chain's gate.
    assert _status(last_update=100, head=150, rate_limit=100).within_rate_limit_window is True


def test_outside_window_once_rate_limit_elapsed() -> None:
    # 101 > 100 -> the window has cleared, a submit is allowed.
    assert _status(last_update=100, head=201, rate_limit=100).within_rate_limit_window is False


def test_boundary_is_inclusive() -> None:
    # exactly at the limit still rejects (chain uses strict > rate_limit).
    assert _status(last_update=100, head=200, rate_limit=100).within_rate_limit_window is True


def test_zero_rate_limit_disables_pre_gate() -> None:
    # rate_limit 0 (unknown/unreadable) -> never pre-gate; chain decides.
    assert _status(last_update=100, head=101, rate_limit=0).within_rate_limit_window is False


def test_timelocked_queue_full_at_local_limit() -> None:
    status = ValidatorWeightStatus(
        registered=True,
        last_update_block=100,
        current_block=150,
        weights_rate_limit=1,
        pending_timelocked_commits=1,
        pending_timelocked_commit_limit=1,
    )
    assert status.timelocked_commit_queue_full is True


def test_unknown_timelocked_queue_does_not_pre_gate() -> None:
    assert _status(last_update=100, head=150, rate_limit=1).timelocked_commit_queue_full is False
