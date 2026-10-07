"""Config parsing for the Daily gm mech-1 leg (``MECH1_CONTRACT_UID``).

The validator sets a constant 100% weight to the Mech1Escrow contract's UID
on the mech-1 mechanism each epoch. ``MECH1_CONTRACT_UID`` selects that UID;
unset or negative disables the mech-1 leg (mech-0 behaviour byte-identical).
See gm-memory ``dgm-03-finalizer-validator.md`` §6 and ``dgm-00-interfaces.md``
§7.
"""

from __future__ import annotations

import pytest

from gm_validator.config import ValidatorConfig


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the two env vars ``from_env`` requires so it constructs cleanly."""
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SUBNET_OWNER_UID", "0")


def test_from_env_mech1_contract_uid_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset ``MECH1_CONTRACT_UID`` disables the mech-1 leg (sentinel -1)."""
    _set_required(monkeypatch)
    monkeypatch.delenv("MECH1_CONTRACT_UID", raising=False)
    config = ValidatorConfig.from_env()
    assert config.mech1_contract_uid == -1


def test_from_env_parses_mech1_contract_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set ``MECH1_CONTRACT_UID`` is parsed onto the config field."""
    _set_required(monkeypatch)
    monkeypatch.setenv("MECH1_CONTRACT_UID", "250")
    config = ValidatorConfig.from_env()
    assert config.mech1_contract_uid == 250


def test_from_env_parses_negative_mech1_contract_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit negative value round-trips (stays the disabled sentinel)."""
    _set_required(monkeypatch)
    monkeypatch.setenv("MECH1_CONTRACT_UID", "-1")
    config = ValidatorConfig.from_env()
    assert config.mech1_contract_uid == -1
