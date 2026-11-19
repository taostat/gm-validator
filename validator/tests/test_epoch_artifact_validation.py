"""Artifact identity, strict geometry, and recovery through the real validator tick."""

from __future__ import annotations

import json
import pathlib

import pytest
from moto import mock_aws

from tests.test_chain_cursor import (
    _add_epoch,
    _bucket,
    _geometry_mismatches,
    _pointer_errors,
    _put_summary,
    _summary,
    _validator,
)
from tests.test_validator_integration import (
    BUCKET,
    PREFIX,
    _head_after,
    _latest_pointer_key,
    _summary_key,
)


@pytest.mark.parametrize("field", ["start_block", "close_block", "next_close_block", "epoch_id"])
@pytest.mark.parametrize("kind", ["string", "float", "bool", "invalid"])
def test_summary_rejects_coerced_integers_and_recovers(
    tmp_path: pathlib.Path, field: str, kind: str
) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 0)
        valid = _summary(s3, 0)
        value = valid[field]
        bad_value = {"string": str(value), "float": float(value), "bool": False, "invalid": []}
        _put_summary(s3, 0, {**valid, field: bad_value[kind]})
        validator, submitter = _validator(tmp_path, s3, head=_head_after(0))
        before = _geometry_mismatches()

        assert validator.process_once() == []
        assert submitter.calls == []
        assert validator._last_submitted_epoch is None
        assert _geometry_mismatches() == before + 1

        _put_summary(s3, 0, valid)
        assert [item.epoch_id for item in validator.process_once()] == [0]
        assert [call["epoch_id"] for call in submitter.calls] == [0]


def test_summary_epoch_must_match_pointer(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 20)
        valid = _summary(s3, 20)
        _put_summary(s3, 20, {**valid, "epoch_id": 19})
        validator, submitter = _validator(tmp_path, s3, head=_head_after(20))
        before = _geometry_mismatches()

        assert validator.process_once() == []
        assert submitter.calls == []
        assert _geometry_mismatches() == before + 1

        _put_summary(s3, 20, valid)
        assert [item.epoch_id for item in validator.process_once()] == [20]


@pytest.mark.parametrize("epoch_id", [19, "20", True, None])
def test_aggregated_rows_must_belong_to_target_epoch(
    tmp_path: pathlib.Path, epoch_id: object
) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 20)
        key = f"{PREFIX}/finalized/epoch=20/aggregated.jsonl"
        rows = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().splitlines()
        row = json.loads(rows[0])
        row["epoch_id"] = epoch_id
        s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(row).encode())
        validator, submitter = _validator(tmp_path, s3, head=_head_after(20))

        assert validator.process_once() == []
        assert submitter.calls == []
        assert validator._last_submitted_epoch is None


@pytest.mark.parametrize("body", [b"{", b"{}"])
def test_invalid_summary_recovers_after_republish(tmp_path: pathlib.Path, body: bytes) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 20)
        valid = _summary(s3, 20)
        key = _summary_key(20)
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)
        validator, submitter = _validator(tmp_path, s3, head=_head_after(20))

        assert validator.process_once() == []
        assert submitter.calls == []

        _put_summary(s3, 20, valid)
        assert [item.epoch_id for item in validator.process_once()] == [20]


def test_cold_start_waits_for_finalizer_upgrade(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 20)
        s3.delete_object(Bucket=BUCKET, Key=_latest_pointer_key())
        local_root = tmp_path / "new" / "mirror"
        validator, submitter = _validator(local_root, s3, head=_head_after(20))
        before = _pointer_errors("missing")

        assert validator.process_once() == []
        assert not local_root.exists()
        assert submitter.calls == []
        assert _pointer_errors("missing") == before + 1

        _add_epoch(s3, 20)
        assert [item.epoch_id for item in validator.process_once()] == [20]
        assert (local_root / "epoch=20" / "_FINALIZED").is_file()


def test_pointer_without_finalized_marker_waits(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 20)
        key = f"{PREFIX}/finalized/epoch=20/_FINALIZED"
        s3.delete_object(Bucket=BUCKET, Key=key)
        validator, submitter = _validator(tmp_path, s3, head=_head_after(20))

        assert validator.process_once() == []
        assert submitter.calls == []
        assert validator._last_submitted_epoch is None

        s3.put_object(Bucket=BUCKET, Key=key, Body=b"")
        assert [item.epoch_id for item in validator.process_once()] == [20]
