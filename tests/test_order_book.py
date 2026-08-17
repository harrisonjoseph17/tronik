import pytest

from app.analysis.order_book import fetch_order_book_snapshot
from app.polymarket.client import PolymarketAPIError


class _StubClob:
    def __init__(self, response=None, error: Exception | None = None):
        self._response = response
        self._error = error

    async def get_book(self, token_id: str):
        if self._error:
            raise self._error
        return self._response


REAL_BOOK_RESPONSE = {
    "market": "0xa45d...",
    "asset_id": "888...",
    "timestamp": "1787006636024",
    "hash": "b06438c5e349a05a24e866ff2671b83dbef795fe",
    "bids": [
        {"price": "0.01", "size": "19.39"},
        {"price": "0.02", "size": "13.97"},
    ],
    "asks": [
        {"price": "0.99", "size": "19.39"},
        {"price": "0.98", "size": "20"},
        {"price": "0.07", "size": "90"},
    ],
    "min_order_size": "5",
    "tick_size": "0.01",
    "neg_risk": False,
    "last_trade_price": "0.950",
}


@pytest.mark.asyncio
async def test_parses_real_confirmed_book_shape():
    result = await fetch_order_book_snapshot(_StubClob(REAL_BOOK_RESPONSE), "tok1")
    assert result.available is True
    assert result.best_bid == 0.02  # max of bid prices, not array[0]
    assert result.best_ask == 0.07  # min of ask prices, not array[0]


@pytest.mark.asyncio
async def test_imbalance_positive_when_more_bid_depth():
    book = {
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.60", "size": "10"}],
    }
    result = await fetch_order_book_snapshot(_StubClob(book), "tok1")
    assert result.imbalance == pytest.approx((100 - 10) / 110)
    assert result.imbalance > 0


@pytest.mark.asyncio
async def test_imbalance_negative_when_more_ask_depth():
    book = {
        "bids": [{"price": "0.40", "size": "5"}],
        "asks": [{"price": "0.60", "size": "95"}],
    }
    result = await fetch_order_book_snapshot(_StubClob(book), "tok1")
    assert result.imbalance < 0


@pytest.mark.asyncio
async def test_unavailable_on_404_error():
    result = await fetch_order_book_snapshot(
        _StubClob(error=PolymarketAPIError("not found", status_code=404)), "tok1"
    )
    assert result.available is False
    assert result.imbalance is None
    assert result.best_bid is None


@pytest.mark.asyncio
async def test_unavailable_when_both_bids_and_asks_empty():
    result = await fetch_order_book_snapshot(_StubClob({"bids": [], "asks": []}), "tok1")
    assert result.available is False


@pytest.mark.asyncio
async def test_available_with_only_bids_no_asks():
    book = {"bids": [{"price": "0.40", "size": "10"}], "asks": []}
    result = await fetch_order_book_snapshot(_StubClob(book), "tok1")
    assert result.available is True
    assert result.best_bid == 0.40
    assert result.best_ask is None
    assert result.imbalance == 1.0  # all bid depth, no ask depth


@pytest.mark.asyncio
async def test_malformed_price_strings_are_skipped_not_fatal():
    book = {
        "bids": [{"price": "not-a-number", "size": "10"}, {"price": "0.30", "size": "5"}],
        "asks": [{"price": "0.60", "size": "5"}],
    }
    result = await fetch_order_book_snapshot(_StubClob(book), "tok1")
    assert result.available is True
    assert result.best_bid == 0.30


@pytest.mark.asyncio
async def test_unavailable_on_unexpected_response_type():
    result = await fetch_order_book_snapshot(_StubClob("not-a-dict"), "tok1")
    assert result.available is False
