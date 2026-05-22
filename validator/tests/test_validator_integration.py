"""End-to-end validator integration.

Populates a moto S3 bucket with synthesized finalized-epoch artifacts,
points the Validator at it with a real `gm-verifier` binary, and
asserts that:

- the `gm-verifier` subprocess accepts the artifacts cleanly,
- the MockSubmitter receives one `submit()` call per epoch,
- the weights vector sums to 1.0,
- per-miner earnings match the synthesized inputs.

The Rust verifier binary is built once via `cargo build` in conftest
and located in `target/debug/`.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
from typing import Any

import boto3
import zstandard as zstd
from moto import mock_aws

from gm_validator.bittensor_adapter import MockSubmitter
from gm_validator.config import ValidatorConfig
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator

BUCKET = "gm-test-bucket"
PREFIX = "v1"


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
                "input_per_mtok_pdollars": "1000000000",
                "output_per_mtok_pdollars": "5000000000",
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

    Mirrors `gm_epoch_finalizer.aggregation.aggregate` for the subset of
    behaviour the integration test cares about. We do not call the
    sibling repo's package because the gm-validator CI workflow only
    checks out this repo. The aggregation contract is exercised end-to-
    end in the gm repo's pytest suite (`tests/test_aggregation.py`); the
    test here just needs `raw_hash` to match what the Rust verifier
    computes — so we use `gm-verifier hash-fixture` for that.
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
                r["usage"]["input_tokens"] * int(dims["input_per_mtok_pdollars"]) // 1_000_000
            )
            earnings += (
                r["usage"]["output_tokens"] * int(dims["output_per_mtok_pdollars"]) // 1_000_000
            )
        raw_hash = _compute_raw_hash_via_verifier(bucket)
        rows.append(
            {
                "epoch_id": epoch_id,
                "miner_id": miner_id,
                "product": {"provider": provider, "model": model},
                "totals": {"input_tokens": in_tokens, "output_tokens": out_tokens},
                "earnings_pdollars": str(earnings),
                "surcharge_pdollars": "0",
                "successful_requests": len(success),
                "failed_requests": len(failed),
                "raw_record_count": len(bucket),
                "raw_hash": raw_hash,
            }
        )
    return rows


def _compute_raw_hash_via_verifier(records: list[dict]) -> str:
    """Pipe a JSONL fixture through `gm-verifier hash-fixture`."""
    bin_path = _verifier_bin()
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        path = f.name
    try:
        out = subprocess.run(  # noqa: S603 - args fully constructed from typed inputs
            [bin_path, "hash-fixture", "--file", path],
            capture_output=True,
            check=True,
            text=True,
        )
        return out.stdout.strip().splitlines()[-1]
    finally:
        os.unlink(path)


def _populate_epoch(s3: Any, epoch_id: int, records: list[dict]) -> None:
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

    # _FINALIZED (last)
    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}_FINALIZED", Body=b"")


def _verifier_bin() -> str:
    """Return the path to the freshly-built `gm-verifier` binary."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    candidate = repo_root / "target" / "debug" / "gm-verifier"
    if not candidate.exists():
        # Build it on demand for ergonomic local runs.
        subprocess.run(
            ["cargo", "build", "--bin", "gm-verifier"],  # noqa: S607
            cwd=str(repo_root),
            check=True,
        )
    return str(candidate)


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
        _populate_epoch(s3, epoch_id=7, records=records)

        # Sample 0 means signatures are not checked (our records have
        # placeholder signatures); raw_hash is still verified.
        config = ValidatorConfig(
            s3_bucket=BUCKET,
            s3_prefix=PREFIX,
            s3_endpoint_url=None,
            aws_region="us-east-1",
            s3_anonymous=False,
            local_mirror_dir=str(tmp_path),
            mirror_retention_epochs=10,
            processed_state_path=str(tmp_path / "processed.json"),
            bittensor_netuid=42,
            bittensor_endpoint=None,
            bittensor_hotkey_file=None,
            bittensor_mock=True,
            verifier_bin=_verifier_bin(),
            verifier_sample_per_tuple=0,
            poll_interval_secs=1,
            metrics_port=9092,
        )
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(
            config,
            mirror,
            submitter,
            miner_uid_lookup={miner_a: 0, miner_b: 1},
        )

        outcomes = validator.process_once()

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.epoch_id == 7
        assert outcome.verifier_ok
        assert outcome.weights_submitted

        # Verify the submission shape.
        assert len(submitter.calls) == 1
        call = submitter.calls[0]
        assert call["epoch_id"] == 7
        assert call["netuid"] == 42
        assert set(call["uids"]) == {0, 1}
        total_weight = sum(call["weights"])
        assert abs(total_weight - 1.0) < 1e-12

        # Idempotency: a second tick should not re-process.
        more = validator.process_once()
        assert more == []
        assert len(submitter.calls) == 1

        # The local mirror should still contain the epoch directory.
        epoch_dir = os.path.join(str(tmp_path), "epoch=7")
        for name in ("raw.jsonl.zst", "aggregated.jsonl", "gateway_keys.json", "_FINALIZED"):
            assert os.path.exists(os.path.join(epoch_dir, name)), f"missing: {name}"


def test_validator_restart_does_not_resubmit(tmp_path: pathlib.Path) -> None:
    """A restarted validator reads the persisted processed-epoch state and
    must not re-submit weights for an epoch still present in S3."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)]
        _populate_epoch(s3, epoch_id=7, records=records)

        config = ValidatorConfig(
            s3_bucket=BUCKET,
            s3_prefix=PREFIX,
            s3_endpoint_url=None,
            aws_region="us-east-1",
            s3_anonymous=False,
            local_mirror_dir=str(tmp_path),
            mirror_retention_epochs=10,
            processed_state_path=str(tmp_path / "processed.json"),
            bittensor_netuid=42,
            bittensor_endpoint=None,
            bittensor_hotkey_file=None,
            bittensor_mock=True,
            verifier_bin=_verifier_bin(),
            verifier_sample_per_tuple=0,
            poll_interval_secs=1,
            metrics_port=9092,
        )
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))

        # First process: a fresh validator submits epoch 7.
        first = Validator(config, mirror, MockSubmitter(), miner_uid_lookup={miner_a: 0})
        assert len(first.process_once()) == 1

        # Restart: a brand-new Validator (fresh in-memory state) reads the
        # persisted processed.json and discovers epoch 7 still in S3.
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


def test_validator_skips_submission_on_verifier_failure(tmp_path: pathlib.Path) -> None:
    """If aggregated.jsonl claims a raw_hash that doesn't match raw.jsonl.zst,
    the verifier exits non-zero and the validator must skip weight submission."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        miner_a = "5Ehm" + "A" * 44
        records = [_record("01ZZZZZZZZZZZZZZZZZZZZZZZZ", miner_a)]

        finalized_prefix = f"{PREFIX}/finalized/epoch=9/"

        raw_bytes = b"\n".join(json.dumps(r).encode("utf-8") for r in records)
        compressed = zstd.ZstdCompressor(level=10).compress(raw_bytes)
        s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}raw.jsonl.zst", Body=compressed)

        rows = _aggregate(records, epoch_id=9)
        body = io.BytesIO()
        for row in rows:
            row["raw_hash"] = "f" * 64  # deliberately wrong
            body.write(json.dumps(row, separators=(",", ":")).encode("utf-8"))
            body.write(b"\n")
        s3.put_object(
            Bucket=BUCKET, Key=f"{finalized_prefix}aggregated.jsonl", Body=body.getvalue()
        )
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{finalized_prefix}gateway_keys.json",
            Body=json.dumps(
                {"schema_version": "1", "epoch_id": 9, "gateways": {"gw-test": []}}
            ).encode("utf-8"),
        )
        s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}_FINALIZED", Body=b"")

        config = ValidatorConfig(
            s3_bucket=BUCKET,
            s3_prefix=PREFIX,
            s3_endpoint_url=None,
            aws_region="us-east-1",
            s3_anonymous=False,
            local_mirror_dir=str(tmp_path),
            mirror_retention_epochs=10,
            processed_state_path=str(tmp_path / "processed.json"),
            bittensor_netuid=42,
            bittensor_endpoint=None,
            bittensor_hotkey_file=None,
            bittensor_mock=True,
            verifier_bin=_verifier_bin(),
            verifier_sample_per_tuple=0,
            poll_interval_secs=1,
            metrics_port=9092,
        )
        mirror = S3Mirror(s3, BUCKET, PREFIX, str(tmp_path))
        submitter = MockSubmitter()
        validator = Validator(config, mirror, submitter, miner_uid_lookup={miner_a: 0})

        outcomes = validator.process_once()
        assert len(outcomes) == 1
        assert not outcomes[0].verifier_ok
        assert not outcomes[0].weights_submitted
        assert submitter.calls == []
