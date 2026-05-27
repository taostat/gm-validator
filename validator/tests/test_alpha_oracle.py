"""Tests for the Taostats-backed alpha-price oracle."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from gm_validator.alpha_oracle import (
    AlphaOracleError,
    AlphaPriceOracle,
)

NETUID = 482

# pytest-asyncio is configured `asyncio_mode = "auto"` — every async def
# test function is treated as an asyncio test.


def _stub_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _ok_handler(
    *, alpha_price: str = "0.012717", tao_price: str = "350.00"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/dtao/pool/latest/v1":
            assert request.url.params["netuid"] == str(NETUID)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"netuid": NETUID, "price": alpha_price, "symbol": "x"},
                    ]
                },
            )
        if path == "/api/price/latest/v1":
            assert request.url.params["asset"] == "tao"
            return httpx.Response(
                200,
                json={"data": [{"price": tao_price}]},
            )
        return httpx.Response(404)

    return handler


async def test_override_short_circuits_http() -> None:
    """When ``override_usd`` is set, the oracle never hits the network."""

    def explode(_: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called when override is set")

    async with _stub_transport(explode) as client:
        oracle = AlphaPriceOracle(
            netuid=NETUID,
            api_key=None,
            override_usd=Decimal("0.42"),
            http_client=client,
        )
        assert await oracle.get_alpha_price_usd() == Decimal("0.42")


async def test_fetch_multiplies_alpha_in_tao_by_tao_usd() -> None:
    async with _stub_transport(_ok_handler(alpha_price="0.0125", tao_price="400.00")) as client:
        oracle = AlphaPriceOracle(netuid=NETUID, api_key="key", http_client=client)
        price = await oracle.get_alpha_price_usd()
        assert price == Decimal("0.0125") * Decimal("400.00")


async def test_cache_serves_repeat_calls_within_ttl() -> None:
    calls = {"dtao": 0, "tao": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/dtao/pool/latest/v1":
            calls["dtao"] += 1
            return httpx.Response(200, json={"data": [{"price": "0.01"}]})
        if request.url.path == "/api/price/latest/v1":
            calls["tao"] += 1
            return httpx.Response(200, json={"data": [{"price": "300"}]})
        return httpx.Response(404)

    async with _stub_transport(handler) as client:
        oracle = AlphaPriceOracle(
            netuid=NETUID, api_key="k", http_client=client, cache_ttl_seconds=60.0
        )
        first = await oracle.get_alpha_price_usd()
        second = await oracle.get_alpha_price_usd()
        assert first == second
        assert calls == {"dtao": 1, "tao": 1}


async def test_fetch_failure_falls_back_to_cached() -> None:
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503, text="boom")
        if request.url.path == "/api/dtao/pool/latest/v1":
            return httpx.Response(200, json={"data": [{"price": "0.01"}]})
        if request.url.path == "/api/price/latest/v1":
            return httpx.Response(200, json={"data": [{"price": "300"}]})
        return httpx.Response(404)

    async with _stub_transport(handler) as client:
        oracle = AlphaPriceOracle(
            netuid=NETUID, api_key="k", http_client=client, cache_ttl_seconds=0.0
        )
        cached = await oracle.get_alpha_price_usd()
        assert cached > 0
        state["fail"] = True
        # Cache TTL is 0, so the next call must hit the network — and we
        # want the fallback to kick in (returns the prior cached value).
        again = await oracle.get_alpha_price_usd()
        assert again == cached


async def test_fetch_failure_with_no_cache_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="taostats down")

    async with _stub_transport(handler) as client:
        oracle = AlphaPriceOracle(netuid=NETUID, api_key="k", http_client=client)
        with pytest.raises(AlphaOracleError):
            await oracle.get_alpha_price_usd()


async def test_empty_pool_payload_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/dtao/pool/latest/v1":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"price": "300"}]})

    async with _stub_transport(handler) as client:
        oracle = AlphaPriceOracle(netuid=NETUID, api_key="k", http_client=client)
        with pytest.raises(AlphaOracleError, match="no dTAO pool row"):
            await oracle.get_alpha_price_usd()


async def test_authorization_header_sent_when_key_configured() -> None:
    seen: dict[str, str | None] = {"auth": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        if request.url.path == "/api/dtao/pool/latest/v1":
            return httpx.Response(200, json={"data": [{"price": "0.01"}]})
        return httpx.Response(200, json={"data": [{"price": "300"}]})

    async with _stub_transport(handler) as client:
        oracle = AlphaPriceOracle(netuid=NETUID, api_key="secret-key", http_client=client)
        await oracle.get_alpha_price_usd()
        assert seen["auth"] == "secret-key"
