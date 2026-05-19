"""Tests for anonymous S3 client construction and GM_VALIDATOR_S3_ANONYMOUS config."""

from __future__ import annotations

import contextlib
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
    monkeypatch.delenv("GM_VALIDATOR_S3_ANONYMOUS", raising=False)
    config = ValidatorConfig.from_env()
    assert config.s3_anonymous is False


@pytest.mark.parametrize("value", ["1", "true", "True"])
def test_s3_anonymous_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("GM_VALIDATOR_S3_ANONYMOUS", value)
    config = ValidatorConfig.from_env()
    assert config.s3_anonymous is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", ""])
def test_s3_anonymous_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("GM_VALIDATOR_S3_ANONYMOUS", value)
    config = ValidatorConfig.from_env()
    assert config.s3_anonymous is False


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
        "bittensor_netuid": 0,
        "bittensor_endpoint": None,
        "bittensor_wallet_name": None,
        "bittensor_wallet_hotkey": None,
        "bittensor_mock": True,
        "verifier_bin": "gm-verifier",
        "verifier_sample_per_tuple": 0,
        "poll_interval_secs": 60,
        "metrics_port": 9092,
    }
    defaults.update(overrides)
    return ValidatorConfig(**defaults)  # type: ignore[arg-type]


def _run_and_capture_client_kwargs(config: ValidatorConfig) -> dict[str, object]:
    """Invoke _run, capture the kwargs passed to boto3.client, then exit early."""
    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> MagicMock:
        captured["service"] = service
        captured["kwargs"] = dict(kwargs)
        return MagicMock()

    with (
        patch.object(main_mod, "boto3") as mock_boto3,
        patch.object(main_mod, "S3Mirror"),
        patch.object(main_mod, "_build_submitter"),
        patch.object(main_mod, "Validator") as mock_validator_cls,
        patch.object(main_mod.time, "sleep", side_effect=StopIteration),
    ):
        mock_boto3.client.side_effect = fake_client
        mock_validator_cls.return_value.process_once.return_value = []
        with contextlib.suppress(StopIteration):
            main_mod._run(config)

    return captured


def test_anonymous_client_uses_unsigned_signature() -> None:
    """When s3_anonymous=True, boto3.client must receive Config(UNSIGNED)."""
    captured = _run_and_capture_client_kwargs(_make_config(s3_anonymous=True))
    kwargs: dict[str, object] = captured["kwargs"]  # type: ignore[assignment]
    assert "config" in kwargs, "Expected 'config' kwarg passed to boto3.client"
    client_config: Config = kwargs["config"]  # type: ignore[assignment]
    assert client_config.signature_version == botocore.UNSIGNED


def test_signed_client_has_no_config_kwarg() -> None:
    """When s3_anonymous=False (default), boto3.client must not pass a Config."""
    captured = _run_and_capture_client_kwargs(_make_config(s3_anonymous=False))
    kwargs: dict[str, object] = captured["kwargs"]  # type: ignore[assignment]
    assert "config" not in kwargs, (
        "boto3.client should not receive a 'config' kwarg when s3_anonymous=False"
    )


def test_anonymous_with_endpoint_url() -> None:
    """s3_anonymous=True with an endpoint_url passes both endpoint_url and Config(UNSIGNED)."""
    captured = _run_and_capture_client_kwargs(
        _make_config(s3_anonymous=True, s3_endpoint_url="https://s3.example.com")
    )
    kwargs: dict[str, object] = captured["kwargs"]  # type: ignore[assignment]
    assert kwargs.get("endpoint_url") == "https://s3.example.com"
    assert "config" in kwargs
    assert kwargs["config"].signature_version == botocore.UNSIGNED  # type: ignore[union-attr]
