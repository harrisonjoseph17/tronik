import asyncio

import httpx
import pytest
import respx

from app.polymarket import client as client_module
from app.polymarket.client import CLOBClient, GammaClient, PolymarketAPIError, RateLimiter


@pytest.fixture(autouse=True)
def no_backoff_delay(monkeypatch):
    """Skip real sleeping between retries so tests run fast."""
    monkeypatch.setattr(client_module, "_backoff_delay", lambda attempt: 0)


@pytest.mark.asyncio
@respx.mock
async def test_get_events_success_path():
    respx.get("https://gamma-api.polymarket.com/events").mock(
        return_value=httpx.Response(200, json=[{"id": "1"}])
    )
    client = GammaClient()
    try:
        result = await client.get_events(active=True, closed=False)
    finally:
        await client.aclose()
    assert result == [{"id": "1"}]


@pytest.mark.asyncio
@respx.mock
async def test_query_params_passed_through():
    route = respx.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = GammaClient()
    try:
        await client.get_markets(slug="my-market", limit=5)
    finally:
        await client.aclose()
    request = route.calls.last.request
    assert request.url.params["slug"] == "my-market"
    assert request.url.params["limit"] == "5"


@pytest.mark.asyncio
@respx.mock
async def test_retries_on_500_then_succeeds():
    route = respx.get("https://gamma-api.polymarket.com/events").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json=[{"id": "ok"}]),
        ]
    )
    client = GammaClient(max_retries=4)
    try:
        result = await client.get_events()
    finally:
        await client.aclose()
    assert result == [{"id": "ok"}]
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_retries_on_429():
    route = respx.get("https://gamma-api.polymarket.com/events").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=[])]
    )
    client = GammaClient(max_retries=4)
    try:
        await client.get_events()
    finally:
        await client.aclose()
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_exhausts_retries_and_raises():
    respx.get("https://gamma-api.polymarket.com/events").mock(return_value=httpx.Response(500))
    client = GammaClient(max_retries=2)
    try:
        with pytest.raises(PolymarketAPIError) as exc_info:
            await client.get_events()
        assert exc_info.value.status_code == 500
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_does_not_retry_on_404():
    route = respx.get("https://gamma-api.polymarket.com/events").mock(
        return_value=httpx.Response(404)
    )
    client = GammaClient(max_retries=4)
    try:
        with pytest.raises(PolymarketAPIError) as exc_info:
            await client.get_events()
        assert exc_info.value.status_code == 404
    finally:
        await client.aclose()
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_clob_get_book_and_price():
    respx.get("https://clob.polymarket.com/book", params={"token_id": "abc"}).mock(
        return_value=httpx.Response(200, json={"bids": [], "asks": []})
    )
    respx.get("https://clob.polymarket.com/price", params={"token_id": "abc", "side": "BUY"}).mock(
        return_value=httpx.Response(200, json={"price": "0.5"})
    )
    client = CLOBClient()
    try:
        book = await client.get_book("abc")
        price = await client.get_price("abc")
    finally:
        await client.aclose()
    assert book == {"bids": [], "asks": []}
    assert price == {"price": "0.5"}


@pytest.mark.asyncio
async def test_rate_limiter_delays_when_bucket_full():
    limiter = RateLimiter(max_requests=2, period_seconds=0.2)
    start = asyncio.get_event_loop().time()
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()  # bucket full, should wait ~0.2s
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_rate_limiter_no_delay_under_limit():
    limiter = RateLimiter(max_requests=10, period_seconds=5.0)
    start = asyncio.get_event_loop().time()
    for _ in range(5):
        await limiter.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.1
