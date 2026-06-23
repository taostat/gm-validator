"""Tests for anonymous S3 client construction and GM_VALIDATOR_S3_ANONYMOUS config."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import botocore
import pytest
from botocore.config import Config

import gm_validator.main as main_mod
from gm_validator.config import ValidatorConfig

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_s3_anonymous_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    monkeypatch.delenv("GM_VALIDATOR_S3_ANONYMOUS", raising=False)
    config = ValidatorConfig.from_env()
    assert config.s3_anonymous is False


@pytest.mark.parametrize("value", ["1", "true", "True"])
def test_s3_anonymous_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    monkeypatch.setenv("GM_VALIDATOR_S3_ANONYMOUS", value)
    config = ValidatorConfig.from_env()
    assert config.s3_anonymous is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", ""])
def test_s3_anonymous_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    monkeypatch.setenv("GM_VALIDATOR_S3_ANONYMOUS", value)
    config = ValidatorConfig.from_env()
    assert config.s3_anonymous is False


# ---------------------------------------------------------------------------
# SUBNET_OWNER_UID is mandatory
# ---------------------------------------------------------------------------


def test_subnet_owner_uid_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUBNET_OWNER_UID is the burn target; without it weight would
    route to uid 0 (a real miner)."""
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.delenv("SUBNET_OWNER_UID", raising=False)
    with pytest.raises(ValueError, match="SUBNET_OWNER_UID"):
        ValidatorConfig.from_env()


def test_subnet_owner_uid_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "103")
    config = ValidatorConfig.from_env()
    assert config.subnet_owner_uid == 103


def test_blocks_per_epoch_defaults_to_chain_epoch_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset `BLOCKS_PER_EPOCH` defaults to 361 (`tempo + 1`), not 360 —
    must match the gm finalizer/registry divisor or the
    `finalized/epoch=<N>/` S3 paths this validator probes desync."""
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    monkeypatch.delenv("BLOCKS_PER_EPOCH", raising=False)
    config = ValidatorConfig.from_env()
    assert config.blocks_per_epoch == 361


# ---------------------------------------------------------------------------
# S3 client construction in _run
# ---------------------------------------------------------------------------


def _make_config(**overrides: object) -> ValidatorConfig:
    """Build a minimal ValidatorConfig for S3 client tests."""
    defaults: dict[str, object] = {
        "s3_bucket": "test-bucket",
        "s3_prefix": "v1",
        "s3_endpoint_url": None,
        "aws_region": "us-east-1",
        "s3_anonymous": False,
        "local_mirror_dir": "/var/cache/gm-test",
        "mirror_retention_epochs": 10,
        "blocks_per_epoch": 360,
        "finalized_lookback_epochs": 3,
        "bittensor_netuid": 0,
        "bittensor_endpoint": None,
        "bittensor_hotkey_seed": None,
        "bittensor_mock": True,
        "subtensor_connect_timeout_secs": 30,
        "subtensor_rpc_timeout_secs": 30,
        "poll_interval_secs": 60,
        "metrics_bind": None,
        "subnet_owner_uid": 0,
        "weight_earnings_multiplier": Decimal(1),
    }
    defaults.update(overrides)
    return ValidatorConfig(**defaults)  # type: ignore[arg-type]


class _CapturedClient:
    """Captured kwargs from a single ``boto3.client`` call in ``_build_s3_client``."""

    def __init__(self, kwargs: dict[str, object]) -> None:
        self._kwargs = kwargs

    @property
    def endpoint_url(self) -> object:
        return self._kwargs.get("endpoint_url")

    @property
    def config(self) -> Config | None:
        value = self._kwargs.get("config")
        assert value is None or isinstance(value, Config)
        return value


def _capture_client_call(config: ValidatorConfig) -> _CapturedClient:
    """Invoke _build_s3_client, capturing the kwargs passed to boto3.client."""
    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> MagicMock:
        captured["service"] = service
        captured.update(kwargs)
        return MagicMock()

    with patch.object(main_mod, "boto3") as mock_boto3:
        mock_boto3.client.side_effect = fake_client
        main_mod._build_s3_client(config)

    return _CapturedClient(captured)


def _signature_version(config: Config) -> object:
    """Read Config.signature_version (omitted from ty's stub)."""
    return getattr(config, "signature_version", None)


def _checksum_calculation(config: Config) -> object:
    """Read Config.request_checksum_calculation (omitted from ty's stub)."""
    return getattr(config, "request_checksum_calculation", None)


def _checksum_validation(config: Config) -> object:
    """Read Config.response_checksum_validation (omitted from ty's stub)."""
    return getattr(config, "response_checksum_validation", None)


def test_anonymous_client_uses_unsigned_signature() -> None:
    """When s3_anonymous=True, boto3.client must receive Config(UNSIGNED)."""
    captured = _capture_client_call(_make_config(s3_anonymous=True))
    assert captured.config is not None, "expected a 'config' kwarg on boto3.client"
    assert _signature_version(captured.config) == botocore.UNSIGNED


def test_signed_client_still_has_config_kwarg() -> None:
    """s3_anonymous=False still passes a Config (it carries the checksum knobs)."""
    captured = _capture_client_call(_make_config(s3_anonymous=False))
    assert captured.config is not None, "expected a 'config' kwarg on boto3.client"
    assert _signature_version(captured.config) is None


def test_anonymous_with_endpoint_url() -> None:
    """s3_anonymous=True with an endpoint_url passes both endpoint_url and Config(UNSIGNED)."""
    captured = _capture_client_call(
        _make_config(s3_anonymous=True, s3_endpoint_url="https://s3.example.com")
    )
    assert captured.endpoint_url == "https://s3.example.com"
    assert captured.config is not None
    assert _signature_version(captured.config) == botocore.UNSIGNED


@pytest.mark.parametrize("anonymous", [True, False])
@pytest.mark.parametrize("endpoint_url", [None, "https://s3.gra.io.cloud.ovh.net"])
def test_client_pins_checksums_to_when_required(anonymous: bool, endpoint_url: str | None) -> None:
    """Every S3 client pins request/response checksums to ``when_required``.

    botocore >=1.36 defaults these to ``when_supported``, which makes
    S3-compatible providers (OVH Object Storage) reject ``ListObjectsV2``
    with ``InvalidRequest``. This guards against a boto3 bump or a
    refactor of ``_build_s3_client`` silently dropping the override.
    """
    captured = _capture_client_call(
        _make_config(s3_anonymous=anonymous, s3_endpoint_url=endpoint_url)
    )
    assert captured.config is not None, "expected a 'config' kwarg on boto3.client"
    assert _checksum_calculation(captured.config) == "when_required"
    assert _checksum_validation(captured.config) == "when_required"


def _addressing_style(config: Config) -> object:
    """Read Config.s3['addressing_style'] (the OVH virtual-hosted switch)."""
    s3 = getattr(config, "s3", None) or {}
    return s3.get("addressing_style")


def test_anonymous_client_uses_virtual_addressing() -> None:
    """Anonymous mode must use virtual-hosted addressing. OVH serves
    public-read objects only at ``bucket.endpoint/key``; an unsigned
    path-style request (``endpoint/bucket/key``) is rejected with HTTP 400
    regardless of the object ACL."""
    captured = _capture_client_call(_make_config(s3_anonymous=True))
    assert captured.config is not None
    assert _addressing_style(captured.config) == "virtual"


def test_signed_client_keeps_default_addressing() -> None:
    """Signed mode keeps default (path) addressing — the keyed gm-bucket
    reads work path-style and must not be perturbed."""
    captured = _capture_client_call(_make_config(s3_anonymous=False))
    assert captured.config is not None
    assert _addressing_style(captured.config) is None
