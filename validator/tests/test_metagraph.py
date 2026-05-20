"""Tests for the metagraph-driven hotkey -> uid lookup.

``bittensor`` is mocked at the module boundary, the same way
``test_bittensor_real`` does it, so no chain is contacted.
"""

from __future__ import annotations

import sys
import types

import pytest

from gm_validator.metagraph import load_miner_uid_lookup


class _FakeMetagraph:
    def __init__(self, hotkeys: list[str]) -> None:
        self.hotkeys = hotkeys


class _FakeSubtensor:
    def __init__(self, network: str | None = None) -> None:
        self.network = network
        self.requested_netuid: int | None = None
        self.hotkeys = ["5HotkeyA", "5HotkeyB", "5HotkeyC"]
        self.raise_on_metagraph: Exception | None = None

    def metagraph(self, netuid: int) -> _FakeMetagraph:
        self.requested_netuid = netuid
        if self.raise_on_metagraph is not None:
            raise self.raise_on_metagraph
        return _FakeMetagraph(self.hotkeys)


def _install_fake_bittensor(monkeypatch: pytest.MonkeyPatch, subtensor: _FakeSubtensor) -> None:
    module = types.ModuleType("bittensor")
    module.Subtensor = lambda network=None: subtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", module)


def test_lookup_maps_hotkey_to_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _FakeSubtensor()
    _install_fake_bittensor(monkeypatch, subtensor)

    lookup = load_miner_uid_lookup(netuid=42, endpoint="wss://node.example")

    assert lookup == {"5HotkeyA": 0, "5HotkeyB": 1, "5HotkeyC": 2}
    assert subtensor.requested_netuid == 42


def test_lookup_empty_metagraph(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _FakeSubtensor()
    subtensor.hotkeys = []
    _install_fake_bittensor(monkeypatch, subtensor)

    lookup = load_miner_uid_lookup(netuid=1, endpoint=None)

    assert lookup == {}


def test_lookup_wraps_chain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    subtensor = _FakeSubtensor()
    subtensor.raise_on_metagraph = ConnectionError("subtensor unreachable")
    _install_fake_bittensor(monkeypatch, subtensor)

    with pytest.raises(RuntimeError, match="subtensor unreachable"):
        load_miner_uid_lookup(netuid=1, endpoint=None)
