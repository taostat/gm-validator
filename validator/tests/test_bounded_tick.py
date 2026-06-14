"""Bounded per-tick work + stale-backlog retirement.

A fresh deploy rediscovers every finalized epoch still in S3. Submitting
weights for all of them in one tick is what OOM-kills the pod: each
``wait_for_inclusion`` leaks websocket state, and a backlog of hundreds
never drains before the memory limit is hit. ``process_once`` must
therefore submit only the newest ``max_epochs_per_tick`` epochs and
retire the older backlog to processed-state without a chain round-trip,
persisting progress as it goes so a restart never redoes the backlog.
"""

from __future__ import annotations

import pathlib

import boto3
from moto import mock_aws

from gm_validator.bittensor_adapter import MockSubmitter
from gm_validator.processed_state import ProcessedState
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator
from tests.test_validator_integration import BUCKET, PREFIX, _config, _populate_epoch, _record


def _bucket() -> object:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return s3


def test_tick_bounds_submissions_to_newest_n(tmp_path: pathlib.Path) -> None:
    """Only the newest max_epochs_per_tick epochs get a submit; the rest
    are retired without a chain call."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        for epoch_id in range(100, 110):  # 10 finalized epochs
            _populate_epoch(
                s3,
                epoch_id=epoch_id,
                records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
                emissions_alpha="0.0001",
            )

        config = _config(tmp_path)
        config.max_epochs_per_tick = 3
        submitter = MockSubmitter()
        validator = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            miner_uid_lookup={miner_a: 0},
        )

        outcomes = validator.process_once()

        # Only the newest 3 epochs submit.
        assert len(submitter.calls) == 3
        assert sorted(c["epoch_id"] for c in submitter.calls) == [107, 108, 109]
        assert sorted(o.epoch_id for o in outcomes) == [107, 108, 109]

        # All 10 are marked processed — the 7 stale ones without a submit.
        assert validator._processed.epochs == set(range(100, 110))


def test_stale_backlog_persists_before_submit(tmp_path: pathlib.Path) -> None:
    """The stale backlog is persisted to disk in the same tick, so a
    restart mid-tick never reprocesses it."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        for epoch_id in range(200, 206):
            _populate_epoch(
                s3,
                epoch_id=epoch_id,
                records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
                emissions_alpha="0.0001",
            )

        config = _config(tmp_path)
        config.max_epochs_per_tick = 2
        validator = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            MockSubmitter(),
            miner_uid_lookup={miner_a: 0},
        )
        validator.process_once()

        # A fresh ProcessedState reading the same file sees every epoch —
        # the stale ones were persisted, not held only in memory.
        reloaded = ProcessedState(config.processed_state_path)
        assert reloaded.epochs == set(range(200, 206))


def test_restart_after_bound_does_not_resubmit(tmp_path: pathlib.Path) -> None:
    """A second validator (simulating a restart) reads the persisted state
    and re-submits nothing — the backlog is gone and the newest epochs
    are already processed."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        for epoch_id in range(300, 308):
            _populate_epoch(
                s3,
                epoch_id=epoch_id,
                records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
                emissions_alpha="0.0001",
            )

        config = _config(tmp_path)
        config.max_epochs_per_tick = 3
        first = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            MockSubmitter(),
            miner_uid_lookup={miner_a: 0},
        )
        first.process_once()

        restarted_submitter = MockSubmitter()
        restarted = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            restarted_submitter,
            miner_uid_lookup={miner_a: 0},
        )
        outcomes = restarted.process_once()

        assert outcomes == []
        assert restarted_submitter.calls == []


def test_single_epoch_still_submits(tmp_path: pathlib.Path) -> None:
    """No backlog: a lone finalized epoch is submitted normally (the
    bound never strands the only epoch)."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(
            s3,
            epoch_id=42,
            records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
            emissions_alpha="0.0001",
        )

        config = _config(tmp_path)
        config.max_epochs_per_tick = 3
        submitter = MockSubmitter()
        validator = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            miner_uid_lookup={miner_a: 0},
        )
        outcomes = validator.process_once()

        assert len(outcomes) == 1
        assert len(submitter.calls) == 1
        assert submitter.calls[0]["epoch_id"] == 42


def test_mark_many_persists_in_one_write(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "processed.json")
    state = ProcessedState(path)
    state.mark_many({1, 2, 3})
    state.mark_many({2, 3})  # no new ids -> still readable, idempotent

    assert ProcessedState(path).epochs == {1, 2, 3}
