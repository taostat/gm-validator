"""Chain-cursor work discovery.

The validator targets the newest closed epoch off the chain head and
probes a single targeted ``_FINALIZED`` head (with a bounded walk-back
when the finalizer lags) instead of scanning the whole epoch history in
S3. These tests cover the bounded walk-back in ``S3Mirror`` and the
end-to-end cursor behaviour in ``Validator``: nothing-finalized-yet,
lagging-finalizer walk-back, the once-per-epoch guard, advancing the
cursor on the next epoch, and the early-chain genesis guard.
"""

from __future__ import annotations

import pathlib
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from gm_validator.bittensor_adapter import MockChainCursor, MockSubmitter
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator
from tests.test_validator_integration import BUCKET, PREFIX, _config, _populate_epoch, _record

_MINER_A = "5Ehm" + "A" * 44


def _bucket() -> object:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return s3


# ---------------------------------------------------------------------------
# S3Mirror.latest_finalized_epoch — bounded targeted walk-back
# ---------------------------------------------------------------------------


def test_latest_finalized_returns_target_when_finalized(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _populate_epoch(s3, epoch_id=42, records=[_record("01A" + "A" * 23, _MINER_A)])
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        assert mirror.latest_finalized_epoch(42, lookback=3) == 42


def test_latest_finalized_walks_back_when_target_lags(tmp_path: pathlib.Path) -> None:
    """When E-1 is not finalized yet, the newest finalized epoch within the
    walk-back window is returned."""
    with mock_aws():
        s3 = _bucket()
        # Only epoch 40 is finalized; 41 and 42 are not.
        _populate_epoch(s3, epoch_id=40, records=[_record("01A" + "A" * 23, _MINER_A)])
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        assert mirror.latest_finalized_epoch(42, lookback=3) == 40


def test_latest_finalized_returns_none_when_window_unfinalized(tmp_path: pathlib.Path) -> None:
    """Nothing finalized within the window -> None (retry next tick)."""
    with mock_aws():
        s3 = _bucket()
        # Finalized epoch is outside the [target-lookback, target] window.
        _populate_epoch(s3, epoch_id=30, records=[_record("01A" + "A" * 23, _MINER_A)])
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        assert mirror.latest_finalized_epoch(42, lookback=3) is None


def test_latest_finalized_prefers_newest_in_window(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        for epoch_id in (40, 41):
            _populate_epoch(s3, epoch_id=epoch_id, records=[_record("01A" + "A" * 23, _MINER_A)])
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        # 42 unfinalized; walk back finds 41 before 40.
        assert mirror.latest_finalized_epoch(42, lookback=3) == 41


def test_latest_finalized_clamps_window_at_genesis(tmp_path: pathlib.Path) -> None:
    """The walk-back floor never goes below epoch 0."""
    with mock_aws():
        s3 = _bucket()
        _populate_epoch(s3, epoch_id=0, records=[_record("01A" + "A" * 23, _MINER_A)])
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        assert mirror.latest_finalized_epoch(2, lookback=10) == 0


class _ForbiddenOnMissing:
    """Fake S3 client modelling an anonymous bucket without ListBucket.

    HEAD on a present ``_FINALIZED`` marker succeeds; HEAD on a missing one
    raises 403 Forbidden (not 404) — the case a public-read bucket returns.
    """

    def __init__(self, finalized: set[int]) -> None:
        self._finalized = finalized
        self.exceptions = type("Ex", (), {"ClientError": ClientError})

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        epoch_id = int(Key.rsplit("epoch=", 1)[1].split("/", 1)[0])
        if epoch_id in self._finalized:
            return {}
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")


def test_latest_finalized_treats_forbidden_as_absent_when_anonymous(
    tmp_path: pathlib.Path,
) -> None:
    """On an anonymous bucket a 403 on a missing marker (no ListBucket) is
    walked past like a 404, not raised — the walk-back still finds the
    newest finalized epoch."""
    s3 = _ForbiddenOnMissing(finalized={40})
    mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path), anonymous=True)
    # 42 and 41 raise 403 (missing); 40 is finalized -> returned.
    assert mirror.latest_finalized_epoch(42, lookback=3) == 40


def test_latest_finalized_forbidden_everywhere_returns_none_when_anonymous(
    tmp_path: pathlib.Path,
) -> None:
    """On an anonymous bucket, no marker anywhere in the window (all 403) ->
    None, not a raise."""
    s3 = _ForbiddenOnMissing(finalized=set())
    mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path), anonymous=True)
    assert mirror.latest_finalized_epoch(42, lookback=3) is None


def test_latest_finalized_forbidden_propagates_on_private_bucket(
    tmp_path: pathlib.Path,
) -> None:
    """On a signed/private bucket (the gm prod config), a 403 is a real
    permission misconfiguration on an existing key — it must propagate, not
    be swallowed as a missing marker leaving the validator silently idle."""
    s3 = _ForbiddenOnMissing(finalized={40})
    mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path), anonymous=False)
    with pytest.raises(ClientError):
        mirror.latest_finalized_epoch(42, lookback=3)


def test_marker_exists_reraises_unexpected_error(tmp_path: pathlib.Path) -> None:
    """A non-absent ClientError (e.g. 500) propagates — it is not silently
    swallowed as 'no marker'."""

    class _BoomClient:
        exceptions = type("Ex", (), {"ClientError": ClientError})

        def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "500", "Message": "Internal"}}, "HeadObject")

    mirror = S3Mirror(_BoomClient(), BUCKET, PREFIX, str(tmp_path))
    with pytest.raises(ClientError):
        mirror.latest_finalized_epoch(10, lookback=2)


# ---------------------------------------------------------------------------
# Validator — end-to-end chain-cursor discovery
# ---------------------------------------------------------------------------


def _validator(
    tmp_path: pathlib.Path, s3: object, cursor: MockChainCursor, submitter: MockSubmitter
) -> Validator:
    return Validator(
        _config(tmp_path),
        S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
        submitter,
        cursor,
        miner_uid_lookup={_MINER_A: 0},
    )


def test_targets_newest_closed_epoch(tmp_path: pathlib.Path) -> None:
    """Open epoch E -> the validator submits the newest closed epoch E-1."""
    with mock_aws():
        s3 = _bucket()
        _populate_epoch(
            s3, epoch_id=50, records=[_record("01A" + "A" * 23, _MINER_A)], emissions_alpha="0.0001"
        )
        submitter = MockSubmitter()
        # Open epoch 51 -> newest closed is 50.
        validator = _validator(tmp_path, s3, MockChainCursor(epoch=51), submitter)

        outcomes = validator.process_once()
        assert [o.epoch_id for o in outcomes] == [50]
        assert [c["epoch_id"] for c in submitter.calls] == [50]


def test_no_submit_when_nothing_finalized(tmp_path: pathlib.Path) -> None:
    """The closed epoch is not finalized yet -> nothing submitted."""
    with mock_aws():
        s3 = _bucket()  # no finalized epochs at all
        submitter = MockSubmitter()
        validator = _validator(tmp_path, s3, MockChainCursor(epoch=51), submitter)

        assert validator.process_once() == []
        assert submitter.calls == []


def test_validator_waits_for_exact_epoch_when_finalizer_lags(tmp_path: pathlib.Path) -> None:
    """An older marker must not consume the current epoch's submission window."""
    with mock_aws():
        s3 = _bucket()
        # Open epoch 53 -> newest closed 52; only 50 is finalized (within
        # the default lookback of 3: 52, 51, 50).
        _populate_epoch(
            s3, epoch_id=50, records=[_record("01A" + "A" * 23, _MINER_A)], emissions_alpha="0.0001"
        )
        submitter = MockSubmitter()
        validator = _validator(tmp_path, s3, MockChainCursor(epoch=53), submitter)

        assert validator.process_once() == []
        assert submitter.calls == []


def test_epoch_window_guard_blocks_resubmit(tmp_path: pathlib.Path) -> None:
    """Within the same epoch the guard prevents a second submit."""
    with mock_aws():
        s3 = _bucket()
        _populate_epoch(
            s3, epoch_id=60, records=[_record("01A" + "A" * 23, _MINER_A)], emissions_alpha="0.0001"
        )
        submitter = MockSubmitter()
        cursor = MockChainCursor(epoch=61)
        validator = _validator(tmp_path, s3, cursor, submitter)

        assert len(validator.process_once()) == 1
        # Same chain head -> same target -> guard blocks the resubmit.
        assert validator.process_once() == []
        assert len(submitter.calls) == 1


def test_finalizer_catchup_waits_then_submits_exact_epoch(tmp_path: pathlib.Path) -> None:
    """Waiting preserves the window for the exact newest closed artifact."""
    with mock_aws():
        s3 = _bucket()
        # Open epoch stays at 54 across ticks; newest closed is 53.
        cursor = MockChainCursor(epoch=54)
        submitter = MockSubmitter()
        validator = _validator(tmp_path, s3, cursor, submitter)

        # Tick 1: only epoch 51 finalized so far -> wait for exact epoch 53.
        _populate_epoch(
            s3, epoch_id=51, records=[_record("01A" + "A" * 23, _MINER_A)], emissions_alpha="0.0001"
        )
        assert validator.process_once() == []

        # Tick 2: catch-up publishes 52 and 53 while the chain remains open 54;
        # the still-unused window submits 53, never the stale intermediate.
        for epoch_id in (52, 53):
            _populate_epoch(
                s3,
                epoch_id=epoch_id,
                records=[_record("01A" + "A" * 23, _MINER_A)],
                emissions_alpha="0.0001",
            )
        assert [o.epoch_id for o in validator.process_once()] == [53]
        assert len(submitter.calls) == 1


def test_stalled_finalizer_never_submits_an_older_target(tmp_path: pathlib.Path) -> None:
    """A stale finalized marker remains ineligible as chain epochs advance."""
    with mock_aws():
        s3 = _bucket()
        # Only epoch 50 is ever finalized; the finalizer is stalled.
        _populate_epoch(
            s3, epoch_id=50, records=[_record("01A" + "A" * 23, _MINER_A)], emissions_alpha="0.0001"
        )
        submitter = MockSubmitter()
        cursor = MockChainCursor(epoch=52)
        validator = _validator(tmp_path, s3, cursor, submitter)

        assert validator.process_once() == []

        # Chain advances, but exact epoch 52 is also absent.
        cursor.epoch = 53
        assert validator.process_once() == []
        assert submitter.calls == []


def test_advancing_epoch_triggers_next_submit(tmp_path: pathlib.Path) -> None:
    """When the chain advances, the next closed epoch is submitted."""
    with mock_aws():
        s3 = _bucket()
        for epoch_id in (60, 61):
            _populate_epoch(
                s3,
                epoch_id=epoch_id,
                records=[_record("01A" + "A" * 23, _MINER_A)],
                emissions_alpha="0.0001",
            )
        submitter = MockSubmitter()
        cursor = MockChainCursor(epoch=61)  # newest closed 60
        validator = _validator(tmp_path, s3, cursor, submitter)

        assert [o.epoch_id for o in validator.process_once()] == [60]
        # Chain advances one epoch: newest closed is now 61.
        cursor.epoch = 62
        assert [o.epoch_id for o in validator.process_once()] == [61]
        assert [c["epoch_id"] for c in submitter.calls] == [60, 61]


def test_unreadable_chain_head_does_nothing(tmp_path: pathlib.Path) -> None:
    """A None chain head (transient read failure) skips the tick."""
    with mock_aws():
        s3 = _bucket()
        _populate_epoch(
            s3, epoch_id=60, records=[_record("01A" + "A" * 23, _MINER_A)], emissions_alpha="0.0001"
        )
        submitter = MockSubmitter()
        validator = _validator(tmp_path, s3, MockChainCursor(epoch=None), submitter)

        assert validator.process_once() == []
        assert submitter.calls == []


def test_genesis_no_closed_epoch(tmp_path: pathlib.Path) -> None:
    """At open epoch 0 there is no closed epoch -> nothing to submit, and no
    negative-epoch probe is attempted."""
    with mock_aws():
        s3 = _bucket()
        submitter = MockSubmitter()
        validator = _validator(tmp_path, s3, MockChainCursor(epoch=0), submitter)

        assert validator.process_once() == []
        assert submitter.calls == []
