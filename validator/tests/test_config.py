"""Tests for environment-driven config parsing."""

from __future__ import annotations

import pytest

from gm_validator.config import ValidatorConfig, _metrics_bind_env


def test_metrics_bind_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GM_VALIDATOR_METRICS_BIND", raising=False)
    assert _metrics_bind_env("GM_VALIDATOR_METRICS_BIND") is None


def test_metrics_bind_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", "   ")
    assert _metrics_bind_env("GM_VALIDATOR_METRICS_BIND") is None


def test_metrics_bind_bare_port_defaults_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", "9092")
    assert _metrics_bind_env("GM_VALIDATOR_METRICS_BIND") == ("0.0.0.0", 9092)


def test_metrics_bind_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", "127.0.0.1:9100")
    assert _metrics_bind_env("GM_VALIDATOR_METRICS_BIND") == ("127.0.0.1", 9100)


def test_metrics_bind_empty_host_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", ":9092")
    assert _metrics_bind_env("GM_VALIDATOR_METRICS_BIND") == ("0.0.0.0", 9092)


def test_metrics_bind_non_numeric_port_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", "localhost:abc")
    with pytest.raises(ValueError, match="port or host:port"):
        _metrics_bind_env("GM_VALIDATOR_METRICS_BIND")


@pytest.mark.parametrize("port", ["0", "-1", "65536", "99999"])
def test_metrics_bind_out_of_range_port_raises(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", f"127.0.0.1:{port}")
    with pytest.raises(ValueError, match=r"1\.\.65535"):
        _metrics_bind_env("GM_VALIDATOR_METRICS_BIND")


def test_from_env_defaults_metrics_bind_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config with no GM_VALIDATOR_METRICS_BIND opens no metrics server."""
    monkeypatch.delenv("GM_VALIDATOR_METRICS_BIND", raising=False)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    config = ValidatorConfig.from_env()
    assert config.metrics_bind is None


def test_from_env_parses_metrics_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GM_VALIDATOR_METRICS_BIND", "0.0.0.0:9092")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    config = ValidatorConfig.from_env()
    assert config.metrics_bind == ("0.0.0.0", 9092)


def test_from_env_defaults_wallet_fields_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No WALLET_* env → seed-based auth; wallet fields are None."""
    for var in ("WALLET_NAME", "WALLET_HOTKEY", "WALLET_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    config = ValidatorConfig.from_env()
    assert config.bittensor_wallet_name is None
    assert config.bittensor_wallet_hotkey is None
    assert config.bittensor_wallet_path is None


def test_from_env_parses_wallet_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLET_NAME", "my-coldkey")
    monkeypatch.setenv("WALLET_HOTKEY", "my-hotkey")
    monkeypatch.setenv("WALLET_PATH", "/wallets")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    config = ValidatorConfig.from_env()
    assert config.bittensor_wallet_name == "my-coldkey"
    assert config.bittensor_wallet_hotkey == "my-hotkey"
    assert config.bittensor_wallet_path == "/wallets"


def test_from_env_blank_wallet_fields_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only WALLET_* must not count as a configured wallet — it
    would otherwise override a valid seed and crash on a bogus wallet path."""
    monkeypatch.setenv("WALLET_NAME", "   ")
    monkeypatch.setenv("WALLET_HOTKEY", "")
    monkeypatch.setenv("WALLET_PATH", " ")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")
    config = ValidatorConfig.from_env()
    assert config.bittensor_wallet_name is None
    assert config.bittensor_wallet_hotkey is None
    assert config.bittensor_wallet_path is None
