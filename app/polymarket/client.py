"""HTTP layer for Polymarket's public REST APIs.

Isolates all outbound HTTP so the rest of the app never touches httpx
directly: timeouts, retry/backoff, rate limiting, and structured logging
live here. Only read-only, keyless endpoints are used - Stage 1 (and all of
V1) never signs or places orders, so no wallet/API-key auth is implemented.

Response shapes, field names, and the rate-limit figures below are based on
Polymarket's own reference docs/repos cross-checked via secondary sources
(direct access to docs.polymarket.com was unavailable during planning) -
treat them as provisional until confirmed by scripts/smoke_test.py against
the live API.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class PolymarketAPIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class RateLimiter:
    """Sliding-window limiter: at most max_requests per period_seconds."""

    def __init__(self, max_requests: int, period_seconds: float):
        self.max_requests = max_requests
        self.period_seconds = period_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            self._evict_expired()
            if len(self._timestamps) >= self.max_requests:
                sleep_for = self.period_seconds - (time.monotonic() - self._timestamps[0])
                if sleep_for > 0:
                    logger.debug("rate limit reached, sleeping %.2fs", sleep_for)
                    await asyncio.sleep(sleep_for)
                self._evict_expired()
            self._timestamps.append(time.monotonic())

    def _evict_expired(self) -> None:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] >= self.period_seconds:
            self._timestamps.popleft()


def _backoff_delay(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 1)


class BaseAPIClient:
    def __init__(
        self,
        base_url: str,
        rate_limiter: RateLimiter,
        *,
        timeout: float = 10.0,
        max_retries: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    logger.error(
                        "giving up on GET %s%s after %d attempts: %s",
                        self.base_url, path, attempt + 1, exc,
                    )
                    raise PolymarketAPIError(
                        f"request failed: {exc}", url=f"{self.base_url}{path}"
                    ) from exc
                delay = _backoff_delay(attempt)
                logger.warning(
                    "transport error on GET %s%s (attempt %d/%d), retrying in %.2fs: %s",
                    self.base_url, path, attempt + 1, self.max_retries + 1, delay, exc,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code < 400:
                return response.json()

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = PolymarketAPIError(
                    f"HTTP {response.status_code} from {path}",
                    status_code=response.status_code,
                    url=f"{self.base_url}{path}",
                )
                if attempt >= self.max_retries:
                    logger.error(
                        "giving up on GET %s%s after %d attempts: HTTP %d",
                        self.base_url, path, attempt + 1, response.status_code,
                    )
                    raise last_error
                delay = _backoff_delay(attempt)
                logger.warning(
                    "HTTP %d on GET %s%s (attempt %d/%d), retrying in %.2fs",
                    response.status_code, self.base_url, path, attempt + 1,
                    self.max_retries + 1, delay,
                )
                await asyncio.sleep(delay)
                continue

            # Non-retryable 4xx - fail fast.
            logger.error("non-retryable HTTP %d on GET %s%s", response.status_code, self.base_url, path)
            raise PolymarketAPIError(
                f"HTTP {response.status_code} from {path}",
                status_code=response.status_code,
                url=f"{self.base_url}{path}",
            )

        raise PolymarketAPIError(f"request failed: {last_error}", url=f"{self.base_url}{path}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BaseAPIClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class GammaClient(BaseAPIClient):
    """Read-only client for https://gamma-api.polymarket.com (no auth)."""

    def __init__(self, *, timeout: float = 10.0, max_retries: int = 4, rate_limit_per_10s: int = 3500):
        super().__init__(
            "https://gamma-api.polymarket.com",
            RateLimiter(rate_limit_per_10s, 10.0),
            timeout=timeout,
            max_retries=max_retries,
        )

    async def get_events(self, **params: Any) -> Any:
        return await self.get("/events", params=params)

    async def get_markets(self, **params: Any) -> Any:
        return await self.get("/markets", params=params)

    async def get_tags(self, **params: Any) -> Any:
        return await self.get("/tags", params=params)


class CLOBClient(BaseAPIClient):
    """Read-only client for https://clob.polymarket.com (no auth).

    Stage 1 only uses this for a connectivity check in scripts/smoke_test.py.
    Order-book/price fetching as part of the discovery pipeline is a later
    stage. Gamma's outcomePrices/bestBid/bestAsk are discovery-time estimates
    only - CLOB is the authoritative source for tradable price/spread/depth.
    """

    def __init__(self, *, timeout: float = 10.0, max_retries: int = 4, rate_limit_per_10s: int = 1500):
        super().__init__(
            "https://clob.polymarket.com",
            RateLimiter(rate_limit_per_10s, 10.0),
            timeout=timeout,
            max_retries=max_retries,
        )

    async def get_book(self, token_id: str) -> Any:
        return await self.get("/book", params={"token_id": token_id})

    async def get_price(self, token_id: str, side: str = "BUY") -> Any:
        return await self.get("/price", params={"token_id": token_id, "side": side})
