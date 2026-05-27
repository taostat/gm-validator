"""Environment-driven config for the Validator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from gm_validator.scoring import MINER_EMISSION_PCT_DEFAULT


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


def _decimal_env(name: str) -> Decimal | None:
    """Parse an env var as ``Decimal``; return ``None`` when unset.

    Raises:
        ValueError: The variable is set to a string that does not parse
            as a decimal.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name}={raw!r} is not a valid decimal") from exc


@dataclass
class ValidatorConfig:
    """All knobs the validator service needs at runtime."""

    # S3
    s3_bucket: str
    s3_prefix: str
    s3_endpoint_url: str | None
    aws_region: str
    # When True, skip request signing and read S3 as an anonymous principal —
    # required for OVH public-read buckets or any AWS bucket without IAM creds
    # available. Env: GM_VALIDATOR_S3_ANONYMOUS (default false).
    s3_anonymous: bool

    # Local mirror for the gm-verifier subprocess.
    local_mirror_dir: str
    # How many of the most-recent epoch mirrors to keep on disk; older
    # ones are pruned each tick. Env: MIRROR_RETENTION_EPOCHS.
    mirror_retention_epochs: int
    # Path to the JSON file recording processed epoch ids, so a restart
    # does not re-submit weights for epochs already finalized in S3.
    # Env: PROCESSED_STATE_PATH.
    processed_state_path: str

    # Bittensor.
    bittensor_netuid: int
    bittensor_endpoint: str | None
    bittensor_wallet_name: str | None
    bittensor_wallet_hotkey: str | None
    bittensor_mock: bool

    # Verifier subprocess.
    verifier_bin: str
    verifier_sample_per_tuple: int

    # Polling / timing.
    poll_interval_secs: int

    # Observability.
    metrics_port: int

    # Alpha-economics cap (bm-pattern PR 1). See scoring.apply_emission_cap.
    # MINER_EMISSION_PCT — protocol miner share. Default 0.41 mirrors bm.
    miner_emission_pct: Decimal
    # ALPHA_PRICE_OVERRIDE_USD — short-circuit the live oracle. None means
    # "fetch from taostats". Set in tests/dev when the API is unreachable.
    alpha_price_override_usd: Decimal | None
    # TAOSTATS_API_KEY — Authorization header value. Required when the
    # override is unset; we surface the empty case at the call site so
    # mock-mode tests run without credentials.
    taostats_api_key: str | None
    # TAOSTATS_API_URL — override the API root. Default points at prod.
    taostats_api_url: str
    # EPOCH_ALPHA_EMISSION_OVERRIDE — full-epoch alpha emission. PR 1 reads
    # this from config because the finalizer does not yet emit it and the
    # chain-state pull lives in PR 2. None forces validator to skip
    # cap-aware submission until the override is set.
    epoch_alpha_emission_override: Decimal | None

    @classmethod
    def from_env(cls) -> ValidatorConfig:
        """Build from environment variables.

        Raises:
            ValueError: A required environment variable is missing.
        """
        miner_emission_pct_raw = _decimal_env("MINER_EMISSION_PCT")
        miner_emission_pct = (
            miner_emission_pct_raw
            if miner_emission_pct_raw is not None
            else MINER_EMISSION_PCT_DEFAULT
        )
        return cls(
            s3_bucket=_require_env("S3_BUCKET"),
            s3_prefix=os.environ.get("S3_PREFIX", "v1").strip("/"),
            s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            s3_anonymous=os.environ.get("GM_VALIDATOR_S3_ANONYMOUS", "0") in {"1", "true", "True"},
            local_mirror_dir=os.environ.get("LOCAL_MIRROR_DIR", "/var/cache/gm-validator"),
            mirror_retention_epochs=_int_env("MIRROR_RETENTION_EPOCHS", 10),
            processed_state_path=os.environ.get(
                "PROCESSED_STATE_PATH", "/var/cache/gm-validator/processed.json"
            ),
            bittensor_netuid=_int_env("BITTENSOR_NETUID", 0),
            bittensor_endpoint=os.environ.get("BITTENSOR_ENDPOINT"),
            bittensor_wallet_name=os.environ.get("BITTENSOR_WALLET_NAME"),
            bittensor_wallet_hotkey=os.environ.get("BITTENSOR_WALLET_HOTKEY"),
            bittensor_mock=os.environ.get("BITTENSOR_MOCK", "0") in {"1", "true", "True"},
            verifier_bin=os.environ.get("GM_VERIFIER_BIN", "gm-verifier"),
            verifier_sample_per_tuple=_int_env("VERIFIER_SAMPLE_PER_TUPLE", 16),
            poll_interval_secs=_int_env("POLL_INTERVAL_SECS", 60),
            metrics_port=_int_env("METRICS_PORT", 9092),
            miner_emission_pct=miner_emission_pct,
            alpha_price_override_usd=_decimal_env("ALPHA_PRICE_OVERRIDE_USD"),
            taostats_api_key=os.environ.get("TAOSTATS_API_KEY"),
            taostats_api_url=os.environ.get("TAOSTATS_API_URL", "https://api.taostats.io"),
            epoch_alpha_emission_override=_decimal_env("EPOCH_ALPHA_EMISSION_OVERRIDE"),
        )

    def finalized_prefix(self, epoch_id: int) -> str:
        """S3 key prefix for finalized artifacts of one epoch."""
        return f"{self.s3_prefix}/finalized/epoch={epoch_id}/"
