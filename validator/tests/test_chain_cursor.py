"""Pointer-driven epoch selection and chain-head cursor behavior."""

from __future__ import annotations

import json
import logging
import pathlib
import random
from itertools import pairwise
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from prometheus_client import REGISTRY

from gm_validator.bittensor_adapter import MockChainCursor, MockSubmitter, ValidatorWeightStatus
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator
from tests.test_validator_integration import (
    BUCKET,
    PREFIX,
    _config,
    _head_after,
    _latest_pointer_key,
    _pointer_payload,
    _populate_epoch,
    _record,
    _summary_key,
    _write_latest_pointer,
)

_MINER_A = "5Ehm" + "A" * 44
_RECORD_ID = "01A" + "A" * 23


def _bucket() -> Any:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return s3


def _validator(
    tmp_path: pathlib.Path,
    s3: Any,
    head: int | None,
    submitter: MockSubmitter | None = None,
    *,
    anonymous: bool = False,
) -> tuple[Validator, MockSubmitter]:
    active_submitter = submitter or MockSubmitter()
    mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path), anonymous=anonymous)
    validator = Validator(
        _config(tmp_path),
        mirror,
        active_submitter,
        MockChainCursor(head=head),
        miner_uid_lookup={_MINER_A: 0},
    )
    return validator, active_submitter


def _counter(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _pointer_errors(reason: str) -> float:
    return _counter("gm_validator_latest_pointer_errors_total", {"reason": reason})


def _geometry_mismatches() -> float:
    return _counter("gm_validator_epoch_geometry_mismatch_total", {})


def _summary(s3: Any, epoch_id: int) -> dict[str, Any]:
    return json.loads(s3.get_object(Bucket=BUCKET, Key=_summary_key(epoch_id))["Body"].read())


def _put_summary(s3: Any, epoch_id: int, value: dict[str, Any]) -> None:
    s3.put_object(Bucket=BUCKET, Key=_summary_key(epoch_id), Body=json.dumps(value).encode("utf-8"))


def _add_epoch(s3: Any, epoch_id: int) -> None:
    _populate_epoch(
        s3,
        epoch_id=epoch_id,
        records=[_record(_RECORD_ID, _MINER_A)],
        emissions_alpha="0.0001",
    )


def _warning_count(caplog: pytest.LogCaptureFixture, text: str) -> int:
    return sum(
        record.levelno == logging.WARNING and text in record.getMessage()
        for record in caplog.records
    )


def test_missing_pointer_is_counted_and_skips_tick(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="gm_validator.validator")
    with mock_aws():
        s3 = _bucket()
        validator, submitter = _validator(tmp_path, s3, head=_head_after(0))
        before = _pointer_errors("missing")

        assert validator.process_once() == []

        assert submitter.calls == []
        assert _pointer_errors("missing") == before + 1
        assert _warning_count(caplog, "latest.json") == 1


class _S3GetError:
    def __init__(self, code: str | Exception) -> None:
        self.code = code
        self.exceptions = type("Ex", (), {"ClientError": ClientError})

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if isinstance(self.code, Exception):
            raise self.code
        raise ClientError({"Error": {"Code": self.code, "Message": "test"}}, "GetObject")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_pointer_not_found_codes_are_missing(
    tmp_path: pathlib.Path, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="gm_validator.validator")
    validator, submitter = _validator(tmp_path, _S3GetError(code), head=_head_after(0))
    before = _pointer_errors("missing")

    assert validator.process_once() == []

    assert submitter.calls == []
    assert _pointer_errors("missing") == before + 1
    assert _warning_count(caplog, "latest.json") == 1


@pytest.mark.parametrize("code", ["403", "AccessDenied", "Forbidden"])
def test_anonymous_forbidden_is_missing(tmp_path: pathlib.Path, code: str) -> None:
    validator, submitter = _validator(
        tmp_path, _S3GetError(code), head=_head_after(0), anonymous=True
    )
    before = _pointer_errors("missing")

    assert validator.process_once() == []

    assert submitter.calls == []
    assert _pointer_errors("missing") == before + 1


@pytest.mark.parametrize("code", ["403", "AccessDenied", "Forbidden", "500"])
def test_private_forbidden_and_other_client_errors_are_unreadable(
    tmp_path: pathlib.Path, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="gm_validator.validator")
    validator, submitter = _validator(tmp_path, _S3GetError(code), head=_head_after(0))
    before = _pointer_errors("unreadable")

    assert validator.process_once() == []

    assert submitter.calls == []
    assert _pointer_errors("unreadable") == before + 1
    assert _warning_count(caplog, "latest.json") == 1


def test_network_error_is_unreadable_without_escaping(tmp_path: pathlib.Path) -> None:
    validator, submitter = _validator(
        tmp_path, _S3GetError(ConnectionError("offline")), head=_head_after(0)
    )
    before = _pointer_errors("unreadable")

    assert validator.process_once() == []

    assert submitter.calls == []
    assert _pointer_errors("unreadable") == before + 1


_MALFORMED_MUTATIONS: list[tuple[str, object]] = [
    ("invalid_json", None),
    ("schema", 2),
    ("schema", True),
    ("schema", 1.0),
    ("missing_next_close_block", None),
    ("epoch_id", "1"),
    ("epoch_id", True),
    ("epoch_id", -1),
    ("start_block", "0"),
    ("close_block", False),
    ("next_close_block", None),
    ("start_block", -1),
    ("close_block", -1),
    ("next_close_block", -1),
    ("close_block", _pointer_payload(1)["next_close_block"]),
    ("close_after_next", None),
    ("start_after_close", None),
    ("finalized_at", 123),
    ("finalized_at", False),
    ("finalized_at", None),
]


@pytest.mark.parametrize(("mutation", "value"), _MALFORMED_MUTATIONS)
def test_malformed_latest_pointer_is_counted_and_skips(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
    mutation: str,
    value: object,
) -> None:
    caplog.set_level(logging.WARNING, logger="gm_validator.validator")
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 1)
        payload: Any = _pointer_payload(1)
        if mutation == "invalid_json":
            _write_latest_pointer(s3, b"{")
        else:
            if mutation == "missing_next_close_block":
                payload.pop("next_close_block")
            elif mutation == "close_after_next":
                payload["close_block"] = payload["next_close_block"] + 1
            elif mutation == "start_after_close":
                payload["start_block"] = payload["close_block"] + 1
            else:
                payload[mutation] = value
            _write_latest_pointer(s3, payload)
        validator, submitter = _validator(tmp_path, s3, head=722)
        before = _pointer_errors("malformed")
        caplog.clear()

        assert validator.process_once() == []

        assert submitter.calls == []
        assert _pointer_errors("malformed") == before + 1
        assert _warning_count(caplog, "latest.json") == 1


@pytest.mark.parametrize("finalized_at", ["", "informational, not a timestamp"])
def test_pointer_finalized_at_is_an_informational_string(
    tmp_path: pathlib.Path, finalized_at: str
) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 1)
        _write_latest_pointer(s3, {**_pointer_payload(1), "finalized_at": finalized_at})
        validator, submitter = _validator(tmp_path, s3, head=_head_after(1))
        before = _pointer_errors("malformed")

        assert [item.epoch_id for item in validator.process_once()] == [1]
        assert [call["epoch_id"] for call in submitter.calls] == [1]
        assert _pointer_errors("malformed") == before


class _CountingS3:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.pointer_reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key == _latest_pointer_key():
            self.pointer_reads += 1
        return self.client.get_object(Bucket=Bucket, Key=Key)


def test_latest_pointer_is_fetched_fresh_each_tick(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        client = _bucket()
        _add_epoch(client, 10)
        _add_epoch(client, 11)
        _write_latest_pointer(client, _pointer_payload(10))
        s3 = _CountingS3(client)
        validator, submitter = _validator(tmp_path, s3, head=_head_after(10))

        assert [item.epoch_id for item in validator.process_once()] == [10]
        _write_latest_pointer(client, _pointer_payload(11))
        validator._cursor.head = _head_after(11)
        assert [item.epoch_id for item in validator.process_once()] == [11]

        assert [call["epoch_id"] for call in submitter.calls] == [10, 11]
        assert s3.pointer_reads == 2


def test_stale_pointer_waits_then_submits_after_it_advances(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 10)
        _add_epoch(s3, 11)
        _write_latest_pointer(s3, _pointer_payload(10))
        validator, submitter = _validator(tmp_path, s3, head=_head_after(11))

        assert validator.process_once() == []
        assert submitter.calls == []
        _write_latest_pointer(s3, _pointer_payload(11))

        assert [item.epoch_id for item in validator.process_once()] == [11]
        assert [call["epoch_id"] for call in submitter.calls] == [11]


def test_head_behind_or_unreadable_waits_without_pointer_error(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 10)
        s3 = _CountingS3(s3)
        validator, submitter = _validator(tmp_path, s3, head=None)
        before = _pointer_errors("unreadable") + _pointer_errors("missing")

        assert validator.process_once() == []
        assert s3.pointer_reads == 0
        validator._cursor.head = _head_after(10) - 1
        assert validator.process_once() == []

        assert submitter.calls == []
        assert s3.pointer_reads == 1
        assert _pointer_errors("unreadable") + _pointer_errors("missing") == before


@pytest.mark.parametrize("head_kind", ["close_plus_one", "next_close"])
def test_head_boundary_blocks_are_eligible(tmp_path: pathlib.Path, head_kind: str) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 10)
        pointer = _pointer_payload(10)
        head = (
            pointer["close_block"] + 1
            if head_kind == "close_plus_one"
            else pointer["next_close_block"]
        )
        validator, submitter = _validator(tmp_path, s3, head=head)

        assert [item.epoch_id for item in validator.process_once()] == [10]
        assert [call["epoch_id"] for call in submitter.calls] == [10]


@pytest.mark.parametrize(
    ("field", "remove"),
    [
        ("start_block", False),
        ("close_block", False),
        ("next_close_block", False),
        ("start_block", True),
    ],
)
def test_geometry_mismatch_invalidates_summary_and_retries(
    tmp_path: pathlib.Path, field: str, remove: bool
) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 20)
        summary = _summary(s3, 20)
        if remove:
            for name in ("start_block", "close_block", "next_close_block"):
                summary.pop(name)
        else:
            summary[field] += 1
        _put_summary(s3, 20, summary)
        validator, submitter = _validator(tmp_path, s3, head=_head_after(20))
        before = _geometry_mismatches()

        assert validator.process_once() == []
        assert submitter.calls == []
        assert _geometry_mismatches() == before + 1
        assert validator._last_submitted_epoch is None
        assert not (tmp_path / "epoch=20" / "epoch_summary.json").exists()

        _add_epoch(s3, 20)
        assert [item.epoch_id for item in validator.process_once()] == [20]
        assert [call["epoch_id"] for call in submitter.calls] == [20]
        assert _geometry_mismatches() == before + 1


def test_target_guard_and_restart_rate_limit_behavior(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = _bucket()
        _add_epoch(s3, 30)
        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        cursor = MockChainCursor(head=_head_after(30))
        first_submitter = MockSubmitter()
        first = Validator(config, mirror, first_submitter, cursor, {_MINER_A: 0})

        assert first.process_once()[0].weights_submitted
        for _ in range(10):
            first.process_once()
        assert len(first_submitter.calls) == 1

        restarted_submitter = MockSubmitter()
        restarted = Validator(config, mirror, restarted_submitter, cursor, {_MINER_A: 0})
        assert restarted.process_once()[0].weights_submitted
        assert restarted_submitter.calls[0]["uids"] == first_submitter.calls[0]["uids"]
        assert restarted_submitter.calls[0]["weights"] == first_submitter.calls[0]["weights"]

        rate_limited = MockSubmitter(
            status=ValidatorWeightStatus(
                registered=True,
                last_update_block=1000,
                current_block=1050,
                weights_rate_limit=100,
            )
        )
        deferred = Validator(config, mirror, rate_limited, cursor, {_MINER_A: 0})
        assert deferred.process_once() == []
        assert rate_limited.calls == []


def _finalization_times(epoch_count: int, rng: random.Random) -> list[int]:
    delays = [1, 1, 5, 17, 500, 1, 361, 4]
    times: list[int] = []
    for epoch_id in range(epoch_count):
        close = (epoch_id + 1) * 361 - 1
        delay = delays[epoch_id] if epoch_id < len(delays) else rng.choice([1, 3, 9, 40, 361, 500])
        candidate = close + max(delay, 1)
        times.append(max(candidate, times[-1] + 1 if times else 0))
    return times


def _tick_heads(last_head: int, final_times: list[int], rng: random.Random) -> list[int]:
    heads = [0]
    while heads[-1] < last_head:
        step = rng.randint(4, 6)
        if rng.random() < 0.025:
            step += rng.randint(8, 25)
        heads.append(min(heads[-1] + step, last_head))
    boundaries = {
        block + offset
        for block in range(0, last_head + 1, 361)
        for offset in (-1, 0, 1)
        if 0 <= block + offset <= last_head
    }
    return sorted(set(heads) | boundaries | {time for time in final_times if time <= last_head})


@pytest.mark.parametrize("seed", [20261119, 482, 28])
def test_pointer_targeting_matches_old_rule_over_seeded_chain_history(
    tmp_path: pathlib.Path, seed: int
) -> None:
    rng = random.Random(seed)  # noqa: S311 — deterministic test schedule, not security use.
    epoch_count = 34
    final_times = _finalization_times(epoch_count, rng)
    last_head = min(final_times[-1], (epoch_count + 1) * 361 - 1)
    heads = _tick_heads(last_head, final_times, rng)
    assert all(earlier < later for earlier, later in pairwise(final_times))
    assert final_times[0] % 361 == final_times[1] % 361 == 0
    assert any(
        final_time - ((epoch_id + 1) * 361 - 1) > 361
        for epoch_id, final_time in enumerate(final_times)
    )

    with mock_aws():
        s3 = _bucket()
        cursor = MockChainCursor(head=heads[0])
        submitter = MockSubmitter()
        validator = Validator(
            _config(tmp_path),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            cursor,
            {_MINER_A: 0},
        )
        old_submissions = _old_rule_submissions(heads, final_times)
        new_submissions: list[tuple[int, int]] = []
        newest_finalized = -1

        for tick_index, head in enumerate(heads):
            while newest_finalized + 1 < epoch_count and final_times[newest_finalized + 1] <= head:
                newest_finalized += 1
                _add_epoch(s3, newest_finalized)
            cursor.head = head
            outcomes = validator.process_once()
            new_submissions.extend(
                (tick_index, outcome.epoch_id) for outcome in outcomes if outcome.weights_submitted
            )

    assert new_submissions == old_submissions
    assert len(new_submissions) >= 5
    assert any(right[1] - left[1] > 1 for left, right in pairwise(new_submissions))
    assert any(heads[tick] == (epoch_id + 2) * 361 - 1 for tick, epoch_id in new_submissions)


def _old_rule_submissions(heads: list[int], final_times: list[int]) -> list[tuple[int, int]]:
    submitted: list[tuple[int, int]] = []
    last_open: int | None = None
    last_target: int | None = None
    for tick_index, head in enumerate(heads):
        open_epoch = head // 361
        target = open_epoch - 1
        if target < 0 or target >= len(final_times) or final_times[target] > head:
            continue
        if last_open is not None and open_epoch <= last_open:
            continue
        if last_target is not None and target <= last_target:
            continue
        submitted.append((tick_index, target))
        last_open = open_epoch
        last_target = target
    return submitted
