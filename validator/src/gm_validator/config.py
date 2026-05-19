"""Environment-driven config for the Validator."""

from __future__ import annotations

import os
from dataclasses import dataclass


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

    @classmethod
    def from_env(cls) -> ValidatorConfig:
        """Build from environment variables.

        Raises:
            ValueError: A required environment variable is missing.
        """
        return cls(
            s3_bucket=_require_env("S3_BUCKET"),
            s3_prefix=os.environ.get("S3_PREFIX", "v1").strip("/"),
            s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            s3_anonymous=os.environ.get("GM_VALIDATOR_S3_ANONYMOUS", "0") in {"1", "true", "True"},
            local_mirror_dir=os.environ.get("LOCAL_MIRROR_DIR", "/var/cache/gm-validator"),
            bittensor_netuid=_int_env("BITTENSOR_NETUID", 0),
            bittensor_endpoint=os.environ.get("BITTENSOR_ENDPOINT"),
            bittensor_wallet_name=os.environ.get("BITTENSOR_WALLET_NAME"),
            bittensor_wallet_hotkey=os.environ.get("BITTENSOR_WALLET_HOTKEY"),
            bittensor_mock=os.environ.get("BITTENSOR_MOCK", "0") in {"1", "true", "True"},
            verifier_bin=os.environ.get("GM_VERIFIER_BIN", "gm-verifier"),
            verifier_sample_per_tuple=_int_env("VERIFIER_SAMPLE_PER_TUPLE", 16),
            poll_interval_secs=_int_env("POLL_INTERVAL_SECS", 60),
            metrics_port=_int_env("METRICS_PORT", 9092),
        )

    def finalized_prefix(self, epoch_id: int) -> str:
        """S3 key prefix for finalized artifacts of one epoch."""
        return f"{self.s3_prefix}/finalized/epoch={epoch_id}/"
