"""Alpha-price oracle — USD-per-alpha lookup for the subnet's alpha token.

The validator's emission-cap math needs ``alpha_price_usd`` (the dollar
value of one unit of the subnet's alpha token) to convert ndollar
earnings into the alpha-denominated weight space. We resolve that price
in two hops:

1. ``alpha_price_in_tao`` from Taostats' dTAO pool endpoint
   (``GET /api/dtao/pool/latest/v1?netuid=<N>``). The ``price`` field is
   a decimal string of TAO per alpha.
2. ``tao_price_usd`` from Taostats' price endpoint
   (``GET /api/price/latest/v1?asset=tao``). Returns USD per TAO as a
   decimal string.

Multiplying the two gives USD per alpha.

Caching: each successful fetch is held for ``cache_ttl_seconds`` (60s by
default). A miss within TTL serves the cached value without an HTTP
round-trip. If the live fetch fails outright we fall back to the most
recently cached value (if any) and log a warning. If there is no cached
value to fall back to we raise — the validator must not silently submit
zero-emission weights.

Override: setting ``ALPHA_PRICE_OVERRIDE_USD`` short-circuits both fetches
and returns the value verbatim. Useful for tests and dev runs where the
Taostats API is unavailable.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, cast

import httpx

LOGGER = logging.getLogger(__name__)

DEFAULT_TAOSTATS_BASE_URL = "https://api.taostats.io"
DEFAULT_CACHE_TTL_SECONDS = 60.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0


class AlphaOracleError(RuntimeError):
    """Failed to determine the current alpha price in USD."""


class AlphaPriceOracle:
    """Caching USD-per-alpha lookup against the Taostats API.

    Thread-unsafe by design — the validator's tick loop is single-
    threaded. If we ever fan out submissions across threads, wrap calls
    in an asyncio lock.
    """

    def __init__(
        self,
        netuid: int,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_TAOSTATS_BASE_URL,
        override_usd: Decimal | None = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build the oracle.

        Args:
            netuid: Subnet id whose alpha token we price.
            api_key: Taostats ``Authorization`` header value. ``None`` is
                accepted so an override-only oracle works without
                credentials.
            base_url: Taostats API root. Override for staging or local
                mocks.
            override_usd: Static USD-per-alpha that short-circuits all
                fetches. Mirrors ``ALPHA_PRICE_OVERRIDE_USD``.
            cache_ttl_seconds: Successful fetches are reused for this
                many seconds before re-hitting the API.
            http_client: Inject a pre-built ``httpx.AsyncClient`` (tests
                use ``MockTransport``); when omitted a fresh client is
                constructed and owned by this oracle.
        """
        self._netuid = netuid
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._override_usd = override_usd
        self._cache_ttl = cache_ttl_seconds
        self._owns_client = http_client is None
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        self._cached_price_usd: Decimal | None = None
        self._cached_at: float = 0.0

    async def close(self) -> None:
        """Release the underlying ``httpx.AsyncClient`` if we own it."""
        if self._owns_client:
            await self._http.aclose()

    async def get_alpha_price_usd(self) -> Decimal:
        """Return the current USD-per-alpha price.

        Order of precedence:

        1. ``override_usd`` if set.
        2. Live fetch (cached for ``cache_ttl_seconds`` between calls).
        3. Last successful cached value, with a warning log.

        Raises:
            AlphaOracleError: All paths failed and no cached value
                exists. The validator MUST NOT submit weights in this
                case.
        """
        if self._override_usd is not None:
            return self._override_usd

        now = time.monotonic()
        if self._cached_price_usd is not None and (now - self._cached_at) < self._cache_ttl:
            return self._cached_price_usd

        try:
            price = await self._fetch_alpha_price_usd()
        except Exception as exc:
            if self._cached_price_usd is not None:
                LOGGER.warning(
                    "alpha price fetch failed (%s); using last known $%s",
                    exc,
                    self._cached_price_usd,
                )
                return self._cached_price_usd
            raise AlphaOracleError(
                f"alpha price fetch failed and no cached value available: {exc}"
            ) from exc

        self._cached_price_usd = price
        self._cached_at = now
        return price

    async def _fetch_alpha_price_usd(self) -> Decimal:
        """Hit Taostats for alpha/TAO and TAO/USD, multiply, return USD/α."""
        alpha_in_tao = await self._fetch_alpha_price_in_tao()
        tao_price_usd = await self._fetch_tao_price_usd()
        if alpha_in_tao <= 0 or tao_price_usd <= 0:
            raise AlphaOracleError(
                f"non-positive price components: alpha_in_tao={alpha_in_tao}, "
                f"tao_price_usd={tao_price_usd}"
            )
        price = alpha_in_tao * tao_price_usd
        LOGGER.info(
            "alpha price refreshed: $%s/alpha (TAO=$%s, %s TAO/alpha, netuid=%d)",
            price,
            tao_price_usd,
            alpha_in_tao,
            self._netuid,
        )
        return price

    async def _fetch_alpha_price_in_tao(self) -> Decimal:
        url = f"{self._base_url}/api/dtao/pool/latest/v1"
        data = await self._get_json(url, params={"netuid": str(self._netuid)})
        return _extract_price(
            data,
            empty_msg=f"taostats returned no dTAO pool row for netuid={self._netuid}",
            missing_msg=f"taostats dTAO pool row missing 'price' field for netuid={self._netuid}",
        )

    async def _fetch_tao_price_usd(self) -> Decimal:
        url = f"{self._base_url}/api/price/latest/v1"
        data = await self._get_json(url, params={"asset": "tao"})
        return _extract_price(
            data,
            empty_msg="taostats returned no TAO price row",
            missing_msg="taostats TAO price row missing 'price' field",
        )

    async def _get_json(self, url: str, *, params: dict[str, str]) -> dict[str, object]:
        headers = {"Authorization": self._api_key} if self._api_key else {}
        response = await self._http.get(url, params=params, headers=headers)
        response.raise_for_status()
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise AlphaOracleError(
                f"unexpected taostats payload shape at {url}: {type(parsed).__name__}"
            )
        return parsed


def _extract_price(
    payload: dict[str, object],
    *,
    empty_msg: str,
    missing_msg: str,
) -> Decimal:
    """Pull ``data[0]['price']`` out of a taostats response as a Decimal.

    Centralises the narrowing dance — both endpoints return
    ``{"data": [{..., "price": "..."}]}`` and we want one place that
    validates the shape.
    """
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise AlphaOracleError(empty_msg)
    first: object = rows[0]
    if not isinstance(first, dict):
        raise AlphaOracleError(missing_msg)
    # ``isinstance(first, dict)`` narrows to ``dict[Unknown, Unknown]``; the
    # taostats payload is JSON so keys are always ``str`` and values are
    # ``object``. Cast through ``Any`` to query the ``"price"`` key.
    price: object = cast("Any", first).get("price")
    if price is None:
        raise AlphaOracleError(missing_msg)
    return Decimal(str(price))
