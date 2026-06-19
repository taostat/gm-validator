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
