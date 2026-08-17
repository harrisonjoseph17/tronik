"""Order-book-derived feature - CLOB-dependent, fetched only for markets
that already passed the data quality gate (only worth spending a live CLOB
call on a market with a real chance of being tradeable).

Uses the confirmed live /book response shape:
    {"bids": [{"price": "0.01", "size": "19.39"}, ...], "asks": [...], ...}
price/size are strings (need float()); the arrays are NOT reliably sorted -
a live probe against a genuinely active market returned asks running
0.99->0.07 and bids running 0.01->0.02 - so best bid/ask are computed
explicitly via max()/min(), never by assuming index 0 is "best".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.polymarket.client import PolymarketAPIError


class SupportsGetBook(Protocol):
    async def get_book(self, token_id: str) -> Any: ...


@dataclass(frozen=True)
class OrderBookSnapshot:
    available: bool
    imbalance: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None


async def fetch_order_book_snapshot(clob: SupportsGetBook, token_id: str) -> OrderBookSnapshot:
    try:
        raw = await clob.get_book(token_id)
    except PolymarketAPIError:
        return OrderBookSnapshot(available=False)

    if not isinstance(raw, dict):
        return OrderBookSnapshot(available=False)

    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    if not bids and not asks:
        return OrderBookSnapshot(available=False)

    bid_prices = _safe_floats(entry.get("price") for entry in bids)
    ask_prices = _safe_floats(entry.get("price") for entry in asks)
    bid_sizes = _safe_floats(entry.get("size") for entry in bids)
    ask_sizes = _safe_floats(entry.get("size") for entry in asks)

    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None

    bid_depth = sum(bid_sizes)
    ask_depth = sum(ask_sizes)
    total_depth = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else None

    return OrderBookSnapshot(available=True, imbalance=imbalance, best_bid=best_bid, best_ask=best_ask)


def _safe_floats(values: Any) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            continue
    return result
