"""End-to-end validator integration.

Populates a moto S3 bucket with synthesized finalized-epoch artifacts,
points the Validator at them, and asserts that:

- the MockSubmitter receives one `submit()` call per epoch,
- the weights vector sums to ``MAX_WEIGHT``,
- per-miner earnings match the synthesized inputs.

The validator trusts the gm-operated finalizer's cost-derived rows;
the artifact set is treated as authoritative and no re-derivation or
hash/signature verification is performed.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
from decimal import Decimal
from typing import Any

import boto3
import pytest
import zstandard as zstd
from moto import mock_aws
from pydantic import ValidationError

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.bittensor_adapter import MockChainCursor, MockSubmitter
from gm_validator.bittensor_real import WeightSubmissionError
from gm_validator.config import ValidatorConfig
from gm_validator.epoch_summary import epoch_summary_path, load_epoch_summary
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator

BUCKET = "gm-test-bucket"
PREFIX = "v1"

# Schema-valid placeholder for the aggregated row's `raw_hash` field.
# The validator no longer verifies this value, so the contents are
# irrelevant — but the field is required by the artifact schema so the
# test fixture keeps emitting something well-formed (lower-case hex,
# 64 chars).
_PLACEHOLDER_RAW_HASH = "0" * 64


def _record(rid: str, miner: str, success: bool = True) -> dict[str, Any]:
    usage = (
        {"input_tokens": 100, "output_tokens": 200}
        if success
        else {"input_tokens": 0, "output_tokens": 0}
    )
    return {
        "schema_version": "1",
        "request_id": rid,
        "timestamp": "2026-05-17T18:34:21.451Z",
        "epoch_id": 7,
        "gateway_id": "gw-test",
        "miner_id": miner,
        "product": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "miner_price": {
            "price_id": "mp-v1-7-1",
            "dimensions": {
                "input_per_mtok_ndollars": 1_000_000_000,
                "output_per_mtok_ndollars": 5_000_000_000,
            },
        },
        "usage": usage,
        "modifiers": {},
        "surcharges": {},
        "success": success,
        "signature": "A" * 88,
    }


def _aggregate(records: list[dict], epoch_id: int) -> list[dict]:
    """Tiny aggregator for tests.

    Produces aggregated rows in the same shape the gm-finalizer emits —
    cost-derived `earnings_ndollars` per `(miner_id, product)` bucket.
    `raw_hash` is a schema placeholder; the validator no longer
    verifies it.
    """
    by_tuple: dict[tuple[str, str, str], list[dict]] = {}
    for r in records:
        key = (r["miner_id"], r["product"]["provider"], r["product"]["model"])
        by_tuple.setdefault(key, []).append(r)
    rows: list[dict] = []
    for (miner_id, provider, model), bucket in sorted(by_tuple.items()):
        success = [r for r in bucket if r.get("success")]
        failed = [r for r in bucket if not r.get("success")]
        in_tokens = sum(r["usage"].get("input_tokens", 0) for r in success)
        out_tokens = sum(r["usage"].get("output_tokens", 0) for r in success)
        # Per-record cost: tokens * miner_price / 1e6.
        earnings = 0
        for r in success:
            dims = r["miner_price"]["dimensions"]
            earnings += (
                r["usage"]["input_tokens"] * int(dims["input_per_mtok_ndollars"]) // 1_000_000
            )
            earnings += (
                r["usage"]["output_tokens"] * int(dims["output_per_mtok_ndollars"]) // 1_000_000
            )
        rows.append(
            {
                "epoch_id": epoch_id,
                "miner_id": miner_id,
                "product": {"provider": provider, "model": model},
                "totals": {"input_tokens": in_tokens, "output_tokens": out_tokens},
                "earnings_ndollars": str(earnings),
                "surcharge_ndollars": "0",
                "successful_requests": len(success),
                "failed_requests": len(failed),
                "raw_record_count": len(bucket),
                "raw_hash": _PLACEHOLDER_RAW_HASH,
            }
        )
    return rows


def _populate_epoch(
    s3: Any,
    epoch_id: int,
    records: list[dict],
    *,
    alpha_price_usd: str = "0.50",
    emissions_alpha: str | None = "100",
) -> None:
    """Populate an epoch with the full finalizer artifact set.

    The validator only reads ``aggregated.jsonl`` and
    ``epoch_summary.json`` (+ ``_FINALIZED`` as the readiness marker),
    but the fixture writes the same files the gm finalizer publishes
    so the moto bucket faithfully mirrors production.

    ``emissions_alpha`` is the chain-read field on
    ``epoch_summary.json``; pass ``None`` to simulate a pre-chain-read
    artifact for the deferral path.
    """
    finalized_prefix = f"{PREFIX}/finalized/epoch={epoch_id}/"

    # raw.jsonl.zst
    raw_bytes = b"\n".join(json.dumps(r).encode("utf-8") for r in records)
    cctx = zstd.ZstdCompressor(level=10)
    compressed = cctx.compress(raw_bytes)
    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}raw.jsonl.zst", Body=compressed)

    rows = _aggregate(records, epoch_id=epoch_id)
    body = io.BytesIO()
    for row in rows:
        body.write(json.dumps(row, separators=(",", ":")).encode("utf-8"))
        body.write(b"\n")
    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}aggregated.jsonl", Body=body.getvalue())

    # gateway_keys.json
    manifest = {"schema_version": "1", "epoch_id": epoch_id, "gateways": {"gw-test": []}}
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{finalized_prefix}gateway_keys.json",
        Body=json.dumps(manifest).encode("utf-8"),
    )

    summary: dict[str, object] = {
        "epoch_id": epoch_id,
        "finalized_at": "2026-05-27T12:00:00Z",
        "alpha_price_in_tao": "0.05",
        "tao_price_usd": "10",
        "alpha_price_usd": alpha_price_usd,
        "price_block_height": 1,
        "price_alpha_source": "chain",
        "price_tao_usd_source": "taostats",
        "finalizer_version": "test",
    }
    if emissions_alpha is not None:
        summary["emissions_alpha"] = emissions_alpha
        summary["emissions_alpha_source"] = "chain"
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{finalized_prefix}epoch_summary.json",
        Body=json.dumps(summary).encode("utf-8"),
    )

    # _FINALIZED (last)
    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}_FINALIZED", Body=b"")


def _cursor_targeting(epoch_id: int) -> MockChainCursor:
    """Chain cursor whose newest *closed* epoch is *epoch_id*.

    ``current_epoch`` reports the open epoch; the validator targets
    ``open - 1``, so the open epoch is ``epoch_id + 1``.
    """
    return MockChainCursor(epoch=epoch_id + 1)


def _config(tmp_path: pathlib.Path) -> ValidatorConfig:
    return ValidatorConfig(
        s3_bucket=BUCKET,
        s3_prefix=PREFIX,
        s3_endpoint_url=None,
        aws_region="us-east-1",
        s3_anonymous=False,
        local_mirror_dir=str(tmp_path),
        mirror_retention_epochs=10,
        blocks_per_epoch=360,
        finalized_lookback_epochs=3,
        bittensor_netuid=42,
        bittensor_endpoint=None,
        bittensor_hotkey_seed=None,
        bittensor_mock=True,
        subtensor_connect_timeout_secs=30,
        subtensor_rpc_timeout_secs=30,
        poll_interval_secs=1,
        metrics_port=9092,
        subnet_owner_uid=99,
        weight_earnings_multiplier=Decimal(1),
    )


class _SequenceMetagraphSource:
    """Metagraph source test double that returns or raises in sequence."""

    def __init__(self, results: list[dict[str, int] | Exception]) -> None:
        self._results = list(results)
        self.calls = 0

    def hotkeys(self) -> dict[str, int]:
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_validator_processes_epoch_end_to_end(tmp_path: pathlib.Path) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        miner_b = "5Ehm" + "B" * 44

        records = [
            _record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a),
            _record("01BBBBBBBBBBBBBBBBBBBBBBBB", miner_b),
            _record("01CCCCCCCCCCCCCCCCCCCCCCCC", miner_a),
        ]
        # Smaller pool so the tiny per-record demand is still visible in
        # the u16 weights — otherwise floor-rounding wipes the miner
        # slots entirely.
        _populate_epoch(s3, epoch_id=7, records=records, emissions_alpha="0.0001")

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config,
            mirror,
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0, miner_b: 1},
        )

        outcomes = validator.process_once()

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.epoch_id == 7
        assert outcome.weights_submitted

        # Verify the submission shape.
        assert len(submitter.calls) == 1
        call = submitter.calls[0]
        assert call["epoch_id"] == 7
        assert call["netuid"] == 42
        # u16 weight vector sums to exactly MAX_WEIGHT.
        assert sum(call["weights"]) == MAX_WEIGHT
        # Both miners present in the vector; demand far exceeds the
        # tiny pool so weights renorm and burn drops to dust.
        assert {0, 1} <= set(call["uids"])

        # Idempotency: a second tick should not re-process.
        more = validator.process_once()
        assert more == []
        assert len(submitter.calls) == 1

        # The local mirror should still contain the epoch directory.
        epoch_dir = os.path.join(str(tmp_path), "epoch=7")
        for name in (
            "aggregated.jsonl",
            "epoch_summary.json",
            "_FINALIZED",
        ):
            assert os.path.exists(os.path.join(epoch_dir, name)), f"missing: {name}"


def test_validator_refreshes_miner_uid_lookup_before_scoring(
    tmp_path: pathlib.Path,
) -> None:
    """A miner registered after startup is picked up before scoring the tick."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(
            s3,
            epoch_id=7,
            records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
            emissions_alpha="0.0001",
        )

        submitter = MockSubmitter()
        metagraph_source = _SequenceMetagraphSource([{miner_a: 0}])
        validator = Validator(
            _config(tmp_path),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={},
            metagraph_source=metagraph_source,
        )

        outcomes = validator.process_once()

        assert [o.epoch_id for o in outcomes] == [7]
        assert [c["epoch_id"] for c in submitter.calls] == [7]
        assert metagraph_source.calls == 1


def test_validator_self_heals_post_startup_registration_across_ticks(
    tmp_path: pathlib.Path,
) -> None:
    """The StaleMetagraphError self-heal fires from an automatic refresh.

    Tick 1 sees an empty lookup (the miner registered after startup),
    raises StaleMetagraphError, and defers without advancing the guards.
    Tick 2's automatic metagraph refresh picks the miner up, so the same
    epoch is scored and submitted — no process restart required.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(
            s3,
            epoch_id=7,
            records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
            emissions_alpha="0.0001",
        )

        submitter = MockSubmitter()
        metagraph_source = _SequenceMetagraphSource([{}, {miner_a: 0}])
        validator = Validator(
            _config(tmp_path),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            metagraph_source=metagraph_source,
        )

        assert validator.process_once() == []
        assert submitter.calls == []
        assert validator._last_submitted_open_epoch is None

        outcomes = validator.process_once()

        assert [o.epoch_id for o in outcomes] == [7]
        assert [c["epoch_id"] for c in submitter.calls] == [7]
        assert metagraph_source.calls == 2


def test_validator_keeps_last_good_lookup_when_refresh_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A transient metagraph read failure must not zero the uid lookup."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(
            s3,
            epoch_id=7,
            records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
            emissions_alpha="0.0001",
        )

        submitter = MockSubmitter()
        metagraph_source = _SequenceMetagraphSource([ConnectionError("metagraph unavailable")])
        validator = Validator(
            _config(tmp_path),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
            metagraph_source=metagraph_source,
        )

        outcomes = validator.process_once()

        assert [o.epoch_id for o in outcomes] == [7]
        assert [c["epoch_id"] for c in submitter.calls] == [7]
        assert metagraph_source.calls == 1


def test_successful_submit_advances_staleness_gauges(tmp_path: pathlib.Path) -> None:
    """A successful submit advances the last-weight epoch + timestamp gauges."""
    from gm_validator import metrics

    metrics._highest_submitted = -1
    metrics.LAST_WEIGHT_EPOCH.set(0)
    metrics.LAST_WEIGHT_TIMESTAMP.set(0)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(
            s3,
            epoch_id=7,
            records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)],
            emissions_alpha="0.0001",
        )
        validator = Validator(
            _config(tmp_path),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            MockSubmitter(),
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        assert validator.process_once()[0].weights_submitted

    assert metrics.LAST_WEIGHT_EPOCH._value.get() == 7
    assert metrics.LAST_WEIGHT_TIMESTAMP._value.get() > 0


def test_validator_restart_rescores_current_epoch_at_most_once(tmp_path: pathlib.Path) -> None:
    """A restart re-derives the cursor from the chain, so it re-scores the
    current epoch exactly once — the submit is idempotent (same weight
    vector) — then the in-memory guard blocks any further re-submit until
    the epoch advances. No persisted processed-state is involved."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        _populate_epoch(s3, epoch_id=7, records=records, emissions_alpha="0.0001")

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))

        # First process: a fresh validator submits epoch 7, then the guard
        # blocks a second submit within the same epoch window.
        first = Validator(
            config, mirror, MockSubmitter(), _cursor_targeting(7), miner_uid_lookup={miner_a: 0}
        )
        assert len(first.process_once()) == 1
        assert first.process_once() == []

        # Restart: a brand-new Validator with fresh in-memory state and the
        # same chain head re-scores epoch 7 once (idempotent), then its own
        # guard blocks the next submit.
        restarted_submitter = MockSubmitter()
        restarted = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            restarted_submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )
        assert len(restarted.process_once()) == 1
        assert len(restarted_submitter.calls) == 1
        assert restarted.process_once() == []
        assert len(restarted_submitter.calls) == 1


def test_restart_into_rate_limit_retries_until_accepted(tmp_path: pathlib.Path) -> None:
    """If a restart re-submits within the chain's weight-set rate-limit
    window, the duplicate is rejected and retried each tick (the guards stay
    unset on failure) until the chain accepts it — bounded, idempotent, and
    self-latching once accepted. No persisted dedup is involved."""

    class _RejectThenAccept:
        """Rejects the first N submits (rate-limit window), then accepts."""

        def __init__(self, reject_count: int) -> None:
            self._remaining = reject_count
            self.calls: list[dict] = []

        def submit(
            self, *, netuid: int, uids: list[int], weights: list[int], epoch_id: int
        ) -> None:
            self.calls.append({"epoch_id": epoch_id})
            if self._remaining > 0:
                self._remaining -= 1
                raise WeightSubmissionError(f"epoch {epoch_id}: rate limited")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        _populate_epoch(s3, epoch_id=7, records=records, emissions_alpha="0.0001")

        config = _config(tmp_path)
        submitter = _RejectThenAccept(reject_count=2)
        validator = Validator(
            config,
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        # Two rejected ticks (guards stay unset -> retry the same epoch).
        assert validator.process_once() == []
        assert validator._last_submitted_epoch is None
        assert validator.process_once() == []
        assert validator._last_submitted_epoch is None

        # Third tick: chain accepts -> guards latch, no further submits.
        assert len(validator.process_once()) == 1
        assert validator._last_submitted_epoch == 7
        assert validator.process_once() == []
        assert len(submitter.calls) == 3


class _FailingSubmitter:
    """Submitter that raises ``WeightSubmissionError`` on every call.

    Models a rejected or failed submit (chain rejection, an
    ``Already Imported`` pool duplicate, or a connection-level failure).
    The validator must defer the epoch — leave it unmarked so the next
    tick retries — rather than mark it processed.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit(self, *, netuid: int, uids: list[int], weights: list[int], epoch_id: int) -> None:
        self.calls.append(
            {"netuid": netuid, "uids": list(uids), "weights": list(weights), "epoch_id": epoch_id}
        )
        raise WeightSubmissionError(
            f"epoch {epoch_id}: subtensor rejected set_weights: Transaction Already Imported"
        )


def test_validator_defers_epoch_on_submit_failure(tmp_path: pathlib.Path) -> None:
    """A failed submit must NOT mark the epoch processed — the validator
    defers it so the next tick retries with the same idempotent weight
    vector. Mirrors the bm validator: there is no already-submitted
    short-circuit, so an ``Already Imported`` pool duplicate (which
    carries no inclusion receipt) is retried like any rejection."""
    from gm_validator import metrics

    failures_before = metrics.SUBMIT_FAILURES._value.get()
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        _populate_epoch(s3, epoch_id=17, records=records, emissions_alpha="0.0001")

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = _FailingSubmitter()
        validator = Validator(
            config, mirror, submitter, _cursor_targeting(17), miner_uid_lookup={miner_a: 0}
        )

        outcomes = validator.process_once()

        # Deferred: no outcome recorded, cursor not advanced.
        assert outcomes == []
        assert validator._last_submitted_open_epoch is None
        assert len(submitter.calls) == 1

        # The next tick retries the same epoch.
        again = validator.process_once()
        assert again == []
        assert validator._last_submitted_open_epoch is None
        assert len(submitter.calls) == 2

    # Both failed submits bumped the submit-failure counter.
    assert metrics.SUBMIT_FAILURES._value.get() == failures_before + 2


def test_validator_zero_revenue_epoch_burns_full_pool(tmp_path: pathlib.Path) -> None:
    """Zero billing => burn_uid gets the entire MAX_WEIGHT."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        miner_b = "5Ehm" + "B" * 44

        # success=False -> zero usage, zero earnings.
        records = [
            _record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a, success=False),
            _record("01BBBBBBBBBBBBBBBBBBBBBBBB", miner_b, success=False),
        ]
        _populate_epoch(s3, epoch_id=13, records=records, alpha_price_usd="0.50")

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config,
            mirror,
            submitter,
            _cursor_targeting(13),
            miner_uid_lookup={miner_a: 0, miner_b: 1},
        )

        outcomes = validator.process_once()
        assert len(outcomes) == 1
        assert outcomes[0].weights_submitted

        assert len(submitter.calls) == 1
        call = submitter.calls[0]
        assert call["uids"] == [99]
        assert call["weights"] == [MAX_WEIGHT]

        # The epoch-window guard blocks a second submit within the same epoch.
        again = validator.process_once()
        assert again == []
        assert len(submitter.calls) == 1


def test_validator_defers_stale_metagraph_epoch(tmp_path: pathlib.Path) -> None:
    """Cap-path epoch where every scored miner is unknown to the uid lookup
    must defer: no submission, cursor unadvanced, retry next tick.

    Simulates the metagraph refreshing between ticks by mutating the
    Validator's uid lookup before the second process_once().
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        miner_b = "5Ehm" + "B" * 44

        records = [
            _record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a),
            _record("01BBBBBBBBBBBBBBBBBBBBBBBB", miner_b),
        ]
        _populate_epoch(s3, epoch_id=23, records=records, alpha_price_usd="0.50")

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config,
            mirror,
            submitter,
            _cursor_targeting(23),
            miner_uid_lookup={},  # stale: neither miner present
        )

        # First tick: stale metagraph -> defer.
        outcomes = validator.process_once()
        assert outcomes == []
        assert submitter.calls == []
        assert validator._last_submitted_open_epoch is None

        # Metagraph refreshes; next tick must process the same epoch.
        validator._miner_uid_lookup = {miner_a: 0, miner_b: 1}
        outcomes = validator.process_once()
        assert len(outcomes) == 1
        assert outcomes[0].epoch_id == 23
        assert outcomes[0].weights_submitted is True
        assert len(submitter.calls) == 1
        assert submitter.calls[0]["epoch_id"] == 23
        assert validator._last_submitted_open_epoch == 24


def test_validator_defers_when_epoch_summary_missing_emissions_alpha(
    tmp_path: pathlib.Path,
) -> None:
    """A pre-chain-read epoch_summary.json must defer rather than score.

    Without emissions_alpha the cap+burn pool denominator is unknown;
    the validator must skip submission AND leave the cursor unadvanced so
    the next tick retries once the finalizer republishes. The deferral
    also invalidates the local cached copy of epoch_summary.json so the
    next tick re-downloads the corrected artifact instead of rereading
    the stale cache.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44

        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        # Simulate a pre-chain-read finalizer by omitting emissions_alpha.
        _populate_epoch(s3, epoch_id=31, records=records, emissions_alpha=None)

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config, mirror, submitter, _cursor_targeting(31), miner_uid_lookup={miner_a: 0}
        )

        # First tick: missing emissions_alpha -> defer.
        outcomes = validator.process_once()
        assert outcomes == []
        assert submitter.calls == []
        assert validator._last_submitted_open_epoch is None

        # Operator republishes epoch_summary.json with the chain-read field.
        # The stale cached copy must have been invalidated so the next
        # tick re-downloads the corrected artifact.
        _populate_epoch(s3, epoch_id=31, records=records, emissions_alpha="100")

        outcomes = validator.process_once()
        assert len(outcomes) == 1
        assert outcomes[0].epoch_id == 31
        assert outcomes[0].weights_submitted is True
        assert validator._last_submitted_open_epoch == 32


def test_validator_submits_cap_burn_weights(tmp_path: pathlib.Path) -> None:
    """End-to-end cap+burn: summary present -> burn slot in submission."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        miner_b = "5Ehm" + "B" * 44

        records = [
            _record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a),
            _record("01BBBBBBBBBBBBBBBBBBBBBBBB", miner_b),
        ]
        _populate_epoch(s3, epoch_id=11, records=records, alpha_price_usd="0.50")

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config,
            mirror,
            submitter,
            _cursor_targeting(11),
            miner_uid_lookup={miner_a: 0, miner_b: 1},
        )

        outcomes = validator.process_once()
        assert len(outcomes) == 1
        assert outcomes[0].weights_submitted

        call = submitter.calls[0]
        assert sum(call["weights"]) == MAX_WEIGHT
        # Big pool against tiny demand: burn slot should dominate.
        weights_by_uid = dict(zip(call["uids"], call["weights"], strict=True))
        assert weights_by_uid.get(99, 0) > MAX_WEIGHT // 2


def _populate_epoch_with_malformed_summary(s3: Any, epoch_id: int, records: list[dict]) -> None:
    """Populate an epoch but overwrite epoch_summary.json with garbage."""
    _populate_epoch(s3, epoch_id, records)
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/finalized/epoch={epoch_id}/epoch_summary.json",
        Body=b'{"epoch_id": "not-an-int", "finalized_at": null}',
    )


def test_propagates_malformed_epoch_summary(tmp_path: pathlib.Path) -> None:
    """A malformed epoch_summary.json must raise — the cap path can't
    proceed without a valid alpha_price_usd."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        _populate_epoch_with_malformed_summary(s3, epoch_id=22, records=records)

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config,
            mirror,
            submitter,
            _cursor_targeting(22),
            miner_uid_lookup={miner_a: 0},
        )

        # process_once() catches per-epoch exceptions internally and logs
        # them, so assert via the side effect: no submission, cursor
        # unadvanced (epoch will retry next tick).
        outcomes = validator.process_once()
        assert outcomes == []
        assert submitter.calls == []

        # And confirm the raw read itself raises so operators see schema
        # errors loudly at the function boundary.
        mirror_dir = mirror.mirror_epoch(22)
        with pytest.raises(ValidationError):
            load_epoch_summary(epoch_summary_path(mirror_dir))


def test_validator_defers_malformed_aggregated_row(tmp_path: pathlib.Path) -> None:
    """A money-field drift in aggregated.jsonl must not submit weights.

    A row missing earnings_ndollars is a permanent fault: score() raises
    MalformedArtifactError, process_once() catches it, and no weights are
    submitted. Without the fail-fast the missing field would silently zero
    the miner's earnings and misroute incentive.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        _populate_epoch(s3, epoch_id=44, records=records, emissions_alpha="0.0001")

        finalized_prefix = f"{PREFIX}/finalized/epoch=44/"
        bad_row = {
            "epoch_id": 44,
            "miner_id": miner_a,
            "product": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "totals": {"input_tokens": 100, "output_tokens": 200},
            "surcharge_ndollars": "0",
            "successful_requests": 1,
            "failed_requests": 0,
            "raw_record_count": 1,
            "raw_hash": _PLACEHOLDER_RAW_HASH,
        }
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{finalized_prefix}aggregated.jsonl",
            Body=(json.dumps(bad_row, separators=(",", ":")) + "\n").encode("utf-8"),
        )

        config = _config(tmp_path)
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config, mirror, submitter, _cursor_targeting(44), miner_uid_lookup={miner_a: 0}
        )

        outcomes = validator.process_once()
        assert outcomes == []
        assert submitter.calls == []
        # Cursor unadvanced — a malformed artifact is not silently marked
        # processed.
        assert validator._last_submitted_open_epoch is None
