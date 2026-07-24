"""End-to-end Daily gm mech-1 validator leg.

The validator, after its normal mech-0 weight submit each epoch, must — when
``MECH1_CONTRACT_UID >= 0`` — issue a SECOND submit: a constant vector
``uids=[MECH1_CONTRACT_UID], weights=[MAX_WEIGHT]`` on the mech-1 mechanism
(``mechid=1``, spike-1/dgm-10). That vector reads no artifact; it is the
same every epoch regardless of ``aggregated.jsonl``. The mech-1 leg has its
own rate-limit gate and must be isolated from the mech-0 submit — either
failing must not break the other.

Grounding: gm-memory ``dgm-03-finalizer-validator.md`` §6,
``dgm-00-interfaces.md`` §7. The moto harness mirrors
``test_validator_integration.py``.
"""

from __future__ import annotations

import io
import json
import pathlib
from decimal import Decimal
from typing import Any

import boto3
import zstandard as zstd
from moto import mock_aws

from gm_validator.alpha_economics import MAX_WEIGHT
from gm_validator.bittensor_adapter import (
    MockChainCursor,
    MockSubmitter,
    ValidatorWeightStatus,
)
from gm_validator.bittensor_real import WeightSubmissionError
from gm_validator.config import ValidatorConfig
from gm_validator.s3_mirror import S3Mirror
from gm_validator.validator import Validator

BUCKET = "gm-test-bucket"
PREFIX = "v1"
OWNER_UID = 99
MECH1_UID = 250

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
    emissions_alpha: str | None = "0.0001",
) -> None:
    """Write the full finalizer artifact set the validator mirrors."""
    finalized_prefix = f"{PREFIX}/finalized/epoch={epoch_id}/"

    raw_bytes = b"\n".join(json.dumps(r).encode("utf-8") for r in records)
    compressed = zstd.ZstdCompressor(level=10).compress(raw_bytes)
    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}raw.jsonl.zst", Body=compressed)

    rows = _aggregate(records, epoch_id=epoch_id)
    body = io.BytesIO()
    for row in rows:
        body.write(json.dumps(row, separators=(",", ":")).encode("utf-8"))
        body.write(b"\n")
    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}aggregated.jsonl", Body=body.getvalue())

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

    s3.put_object(Bucket=BUCKET, Key=f"{finalized_prefix}_FINALIZED", Body=b"")


def _cursor_targeting(epoch_id: int) -> MockChainCursor:
    """Chain cursor whose newest closed epoch is *epoch_id*."""
    return MockChainCursor(epoch=epoch_id + 1)


def _config(tmp_path: pathlib.Path, *, mech1_contract_uid: int) -> ValidatorConfig:
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
        bittensor_wallet_name=None,
        bittensor_wallet_hotkey=None,
        bittensor_wallet_path=None,
        bittensor_mock=True,
        subtensor_connect_timeout_secs=30,
        subtensor_rpc_timeout_secs=30,
        poll_interval_secs=1,
        metrics_bind=None,
        subnet_owner_uid=OWNER_UID,
        weight_earnings_multiplier=Decimal(1),
        mech1_contract_uid=mech1_contract_uid,
    )


class _MechAwareSubmitter:
    """Submitter double that records the mechid and can fail a chosen mechanism.

    Lets a test drive mech-0 and mech-1 outcomes independently so failure
    isolation is observable.
    """

    def __init__(self, fail_mechids: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self._fail = set(fail_mechids or set())

    def weight_status(self, mechid: int = 0) -> ValidatorWeightStatus | None:
        return None

    def submit(
        self, *, netuid: int, uids: list[int], weights: list[int], epoch_id: int, mechid: int = 0
    ) -> None:
        self.calls.append(
            {
                "netuid": netuid,
                "uids": list(uids),
                "weights": list(weights),
                "epoch_id": epoch_id,
                "mechid": mechid,
            }
        )
        if mechid in self._fail:
            raise WeightSubmissionError(f"mech-{mechid} rejected")


def _bucket() -> Any:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return s3


# --- flag on / off -------------------------------------------------------


def test_mech1_enabled_submits_constant_vector_after_mech0(tmp_path: pathlib.Path) -> None:
    """Flag on: a second submit sends ``[MECH1_UID] -> MAX_WEIGHT`` on mechid 1,
    after the mech-0 submit, for the same epoch."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        miner_b = "5Ehm" + "B" * 44
        _populate_epoch(
            s3,
            epoch_id=7,
            records=[
                _record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a),
                _record("01BBBBBBBBBBBBBBBBBBBBBBBB", miner_b),
            ],
        )
        submitter = MockSubmitter()
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=MECH1_UID),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0, miner_b: 1},
        )

        validator.process_once()

        assert len(submitter.calls) == 2
        mech0, mech1 = submitter.calls[0], submitter.calls[1]
        assert mech0["mechid"] == 0
        assert mech1["mechid"] == 1
        assert mech1["uids"] == [MECH1_UID]
        assert mech1["weights"] == [MAX_WEIGHT]
        assert mech1["epoch_id"] == 7
        assert mech1["netuid"] == 42


def test_mech1_disabled_leaves_mech0_only(tmp_path: pathlib.Path) -> None:
    """Flag off (uid -1): exactly one mech-0 submit — byte-identical to today."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(s3, epoch_id=7, records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)])
        submitter = MockSubmitter()
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=-1),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        validator.process_once()

        assert len(submitter.calls) == 1
        assert submitter.calls[0]["mechid"] == 0
        assert all(c["mechid"] == 0 for c in submitter.calls)


def test_mech1_constant_vector_emitted_on_all_burn_epoch(tmp_path: pathlib.Path) -> None:
    """The mech-1 vector reads no artifact: even a zero-revenue (all-burn)
    epoch still emits the identical ``[MECH1_UID] -> MAX_WEIGHT`` on mechid 1."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        miner_b = "5Ehm" + "B" * 44
        _populate_epoch(
            s3,
            epoch_id=13,
            records=[
                _record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a, success=False),
                _record("01BBBBBBBBBBBBBBBBBBBBBBBB", miner_b, success=False),
            ],
            alpha_price_usd="0.50",
        )
        submitter = MockSubmitter()
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=MECH1_UID),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(13),
            miner_uid_lookup={miner_a: 0, miner_b: 1},
        )

        validator.process_once()

        assert len(submitter.calls) == 2
        mech0, mech1 = submitter.calls[0], submitter.calls[1]
        # mech-0 burns the whole pool to the owner uid.
        assert mech0["mechid"] == 0
        assert mech0["uids"] == [OWNER_UID]
        assert mech0["weights"] == [MAX_WEIGHT]
        # mech-1 is the same constant regardless of mech-0 content.
        assert mech1["mechid"] == 1
        assert mech1["uids"] == [MECH1_UID]
        assert mech1["weights"] == [MAX_WEIGHT]


# --- failure isolation ---------------------------------------------------


def test_mech1_submit_failure_does_not_break_mech0(tmp_path: pathlib.Path) -> None:
    """A failing mech-1 submit must not defer the epoch: the mech-0 submit is
    still recorded, the epoch outcome stands, and the epoch guard advances."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(s3, epoch_id=7, records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)])
        submitter = _MechAwareSubmitter(fail_mechids={1})
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=MECH1_UID),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        outcomes = validator.process_once()

        # mech-1 was attempted (and failed) but mech-0 is unaffected.
        assert any(c["mechid"] == 1 for c in submitter.calls)
        assert any(c["mechid"] == 0 for c in submitter.calls)
        assert len(outcomes) == 1
        assert outcomes[0].weights_submitted
        assert validator._last_submitted_epoch == 7


def test_mech0_submit_failure_skips_mech1(tmp_path: pathlib.Path) -> None:
    """A failing mech-0 submit defers the epoch and never reaches mech-1 —
    the mech-1 leg does not rescue or alter mech-0's existing failure path."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(s3, epoch_id=7, records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)])
        submitter = _MechAwareSubmitter(fail_mechids={0})
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=MECH1_UID),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        outcomes = validator.process_once()

        assert outcomes == []
        assert validator._last_submitted_epoch is None
        assert not any(c["mechid"] == 1 for c in submitter.calls)


# --- mech-1's own rate-limit gate ----------------------------------------


def test_mech1_submit_fires_outside_rate_limit_window(tmp_path: pathlib.Path) -> None:
    """Outside the mech-1 rate-limit window the mech-1 constant submit fires.

    mech-0 status is None (proceeds); the mech-1 status reports 200 blocks
    since last update against a 100-block limit, so its own gate lets the
    submit through.
    """
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(s3, epoch_id=7, records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)])
        submitter = MockSubmitter(
            status=None,
            mech1_status=ValidatorWeightStatus(
                registered=True,
                last_update_block=1000,
                current_block=1200,
                weights_rate_limit=100,
            ),
        )
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=MECH1_UID),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        validator.process_once()

        assert any(c["mechid"] == 1 and c["uids"] == [MECH1_UID] for c in submitter.calls)


def test_mech1_submit_skipped_inside_rate_limit_window(tmp_path: pathlib.Path) -> None:
    """Inside the mech-1 rate-limit window the mech-1 submit is deferred while
    the mech-0 submit still proceeds — the two gates are independent."""
    with mock_aws():
        s3 = _bucket()
        miner_a = "5Ehm" + "A" * 44
        _populate_epoch(s3, epoch_id=7, records=[_record("01AAAAAAAAAAAAAAAAAAAAAAAA", miner_a)])
        submitter = MockSubmitter(
            status=None,
            mech1_status=ValidatorWeightStatus(
                registered=True,
                last_update_block=1000,
                current_block=1040,
                weights_rate_limit=100,
            ),
        )
        validator = Validator(
            _config(tmp_path, mech1_contract_uid=MECH1_UID),
            S3Mirror(s3, BUCKET, PREFIX, str(tmp_path)),
            submitter,
            _cursor_targeting(7),
            miner_uid_lookup={miner_a: 0},
        )

        validator.process_once()

        assert [c for c in submitter.calls if c["mechid"] == 1] == []
        assert any(c["mechid"] == 0 for c in submitter.calls)
