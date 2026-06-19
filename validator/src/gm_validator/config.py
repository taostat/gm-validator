"""Environment-driven config for the Validator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


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


def _decimal_env(name: str, default: str) -> Decimal:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return Decimal(default)
    return Decimal(value)


def _metrics_bind_env(name: str) -> tuple[str, int] | None:
    """Parse a ``host:port`` (or bare ``port``) metrics-bind spec.

    Returns ``None`` when the variable is unset or blank so the caller opens
    no metrics server at all. A bare port binds ``0.0.0.0``.

    Raises:
        ValueError: The value is set but is not a port or ``host:port``.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    value = value.strip()
    host, sep, port = value.rpartition(":")
    # A bare port or empty host binds all interfaces — the operator opted in by
    # setting the env var at all, matching prometheus start_http_server's own
    # 0.0.0.0 default; PR-2 still gates the whole server behind that opt-in.
    if not sep:
        host, port = "0.0.0.0", value  # noqa: S104
    if not host:
        host = "0.0.0.0"  # noqa: S104
    try:
        port_num = int(port)
    except ValueError as exc:
        raise ValueError(f"{name} must be a port or host:port; got {value!r}") from exc
    if not 1 <= port_num <= 65535:
        raise ValueError(f"{name} port must be in 1..65535; got {port_num}")
    return host, port_num


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

    # Local mirror for finalized epoch artifacts.
    local_mirror_dir: str
    # How many of the most-recent epoch mirrors to keep on disk; older
    # ones are pruned each tick. Env: MIRROR_RETENTION_EPOCHS.
    mirror_retention_epochs: int
    # Chain epoch length (`tempo + 1` = 361). A Bittensor subnet runs
    # consensus/emission every `tempo + 1` blocks, so the chain head block
    # divided by 361 gives the current epoch id — the same derivation the
    # finalizer uses (`block // blocks_per_epoch`). Must equal the gm
    # finalizer/registry divisor or the `finalized/epoch=<N>/` S3 paths
    # this validator probes desync. Env: BLOCKS_PER_EPOCH.
    blocks_per_epoch: int
    # How many epochs back from the newest closed epoch to probe for a
    # `_FINALIZED` marker before giving up for this tick. Tolerates the
    # finalizer lagging the chain by a few epochs without a full S3 scan.
    # Env: FINALIZED_LOOKBACK_EPOCHS.
    finalized_lookback_epochs: int

    # Bittensor.
    bittensor_netuid: int
    bittensor_endpoint: str | None
    # The validator hotkey's secret seed — a BIP-39 mnemonic or a
    # `0x`-prefixed hex seed. The signing keypair is built in memory from
    # it; no wallet keyfile is read from or written to disk. Env:
    # BITTENSOR_HOTKEY_SEED.
    bittensor_hotkey_seed: str | None
    bittensor_mock: bool
    # Wall-clock budget for a single subtensor connect attempt. The SDK
    # opens its websocket synchronously with no connect timeout, so a
    # connect that hangs (rather than raising) would freeze startup
    # forever. connect_subtensor runs the construction in a worker thread
    # and raises TimeoutError past this budget so the retry loop catches
    # it. Env: SUBTENSOR_CONNECT_TIMEOUT_SECS.
    subtensor_connect_timeout_secs: int
    # Wall-clock budget for a single chain RPC over the already-open
    # socket (get_current_block at the top of each tick, set_weights). The
    # SDK has no per-call timeout, so after a successful set_weights the
    # next get_current_block can hang forever on a wedged websocket and
    # freeze the loop. RealSubmitter bounds each RPC with this budget and
    # raises TimeoutError past it, which counts toward reconnect so the
    # socket self-heals. Env: SUBTENSOR_RPC_TIMEOUT_SECS.
    subtensor_rpc_timeout_secs: int

    # Polling / timing.
    poll_interval_secs: int

    # Observability. The Prometheus metrics server binds here only when
    # GM_VALIDATOR_METRICS_BIND is set; unset means no metrics endpoint is
    # opened — default-off so a third-party validator operator never exposes
    # an unexpected listening socket. Env value is "host:port" or a bare
    # "port" (host defaults to 0.0.0.0).
    metrics_bind: tuple[str, int] | None

    # Uid that absorbs the burn slot + floor-rounding dust. bm reads the
    # subnet-owner hotkey from the chain and resolves to a uid; the gm
    # port defers that lookup to a follow-up.
    subnet_owner_uid: int

    # TESTNET-ONLY demo knob. Multiplies each miner's aggregated earnings
    # in memory before the alpha/weight conversion so tiny test earnings
    # can cross the 1/65535 on-chain weight floor and produce a visible
    # non-zero miner incentive instead of burning 100% to the owner uid.
    # MUST stay 1 (unset) on mainnet — any other value distorts real
    # payouts. Env: GM_WEIGHT_EARNINGS_MULTIPLIER (default 1 = exact
    # no-op). See scoring.compute_weights.
    weight_earnings_multiplier: Decimal

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
            mirror_retention_epochs=_int_env("MIRROR_RETENTION_EPOCHS", 10),
            blocks_per_epoch=_int_env("BLOCKS_PER_EPOCH", 361),
            finalized_lookback_epochs=_int_env("FINALIZED_LOOKBACK_EPOCHS", 3),
            bittensor_netuid=_int_env("BITTENSOR_NETUID", 0),
            bittensor_endpoint=os.environ.get("BITTENSOR_ENDPOINT"),
            bittensor_hotkey_seed=os.environ.get("BITTENSOR_HOTKEY_SEED"),
            bittensor_mock=os.environ.get("BITTENSOR_MOCK", "0") in {"1", "true", "True"},
            subtensor_connect_timeout_secs=_int_env("SUBTENSOR_CONNECT_TIMEOUT_SECS", 30),
            subtensor_rpc_timeout_secs=_int_env("SUBTENSOR_RPC_TIMEOUT_SECS", 30),
            poll_interval_secs=_int_env("POLL_INTERVAL_SECS", 60),
            metrics_bind=_metrics_bind_env("GM_VALIDATOR_METRICS_BIND"),
            subnet_owner_uid=int(_require_env("SUBNET_OWNER_UID")),
            weight_earnings_multiplier=_decimal_env("GM_WEIGHT_EARNINGS_MULTIPLIER", "1"),
        )

    def finalized_prefix(self, epoch_id: int) -> str:
        """S3 key prefix for finalized artifacts of one epoch."""
        return f"{self.s3_prefix}/finalized/epoch={epoch_id}/"
