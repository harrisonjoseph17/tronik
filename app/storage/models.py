"""Normalization layer: turns raw Gamma API dicts into a validated Market.

Every field is read defensively (raw.get(...), never direct indexing) since
Gamma does not guarantee every field is present on every market. Malformed
or missing data degrades to a safe default plus a WARNING log rather than
raising - a single bad market must never abort a whole scan.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.polymarket.categorize import OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY

logger = logging.getLogger(__name__)


def _parse_stringified_json_list(value: Any, field_name: str) -> list:
    """Gamma sends outcomes/outcomePrices/clobTokenIds as JSON-stringified arrays."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            logger.warning("could not parse stringified JSON for %s: %r", field_name, value)
            return []
        if isinstance(parsed, list):
            return parsed
        logger.warning("expected a JSON list for %s, got %r", field_name, parsed)
        return []
    logger.warning("unexpected type for %s: %r", field_name, type(value))
    return []


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _safe_datetime(value: Any) -> datetime | None:
    """Python 3.11+ datetime.fromisoformat handles trailing 'Z' natively."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning("could not parse datetime: %r", value)
        return None


def _compute_spread(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return round(best_ask - best_bid, 6)


def _compute_mid_price(
    best_bid: float | None, best_ask: float | None, outcome_prices: list[float]
) -> float | None:
    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2, 6)
    if outcome_prices:
        return outcome_prices[0]
    return None


def _derive_status(*, active: bool, closed: bool, archived: bool) -> str:
    if archived:
        return "archived"
    if closed:
        return "closed"
    if active:
        return "active"
    return "invalid"


class Market(BaseModel):
    id: str
    event_id: str | None = None
    question: str = ""
    slug: str | None = None
    category: str = OTHER_CATEGORY
    subcategory: str = UNCATEGORIZED_SUBCATEGORY

    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[float] = Field(default_factory=list)
    clob_token_ids: list[str] = Field(default_factory=list)

    volume: float | None = None
    volume_24hr: float | None = None
    liquidity: float | None = None

    # Discovery-time price metadata sourced from Gamma only - NOT an
    # executable/tradable price. Later stages must read CLOB (/book, /price)
    # for anything used in edge/spread calculations or order placement.
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    mid_price: float | None = None

    active: bool = False
    closed: bool = False
    archived: bool = False
    enable_order_book: bool = False

    start_date: datetime | None = None
    end_date: datetime | None = None

    status: str = "unknown"

    is_duplicate: bool = False
    duplicate_of: str | None = None

    # Why this market is excluded from the "included" analysis universe
    # (e.g. "closed", "low_liquidity", "wide_spread", "near_resolution",
    # "duplicate"). None means it currently passes all filters. Markets are
    # never discarded for having a filter_reason - they're still persisted.
    filter_reason: str | None = None

    @field_validator("outcomes", mode="before")
    @classmethod
    def _validate_outcomes(cls, value: Any) -> list:
        return _parse_stringified_json_list(value, "outcomes")

    @field_validator("outcome_prices", mode="before")
    @classmethod
    def _validate_outcome_prices(cls, value: Any) -> list:
        return _parse_stringified_json_list(value, "outcome_prices")

    @field_validator("clob_token_ids", mode="before")
    @classmethod
    def _validate_clob_token_ids(cls, value: Any) -> list:
        return _parse_stringified_json_list(value, "clob_token_ids")

    @classmethod
    def from_gamma(
        cls,
        raw: dict[str, Any],
        *,
        event: dict[str, Any] | None = None,
        category: str = OTHER_CATEGORY,
        subcategory: str = UNCATEGORIZED_SUBCATEGORY,
    ) -> "Market":
        event = event or {}

        best_bid = _safe_float(raw.get("bestBid"))
        best_ask = _safe_float(raw.get("bestAsk"))
        active = bool(raw.get("active", False))
        closed = bool(raw.get("closed", False))
        archived = bool(raw.get("archived", False))

        market = cls(
            id=_safe_str(raw.get("id")) or "",
            event_id=_safe_str(raw.get("event_id") or event.get("id")),
            question=raw.get("question") or raw.get("title") or "",
            slug=raw.get("slug"),
            category=category,
            subcategory=subcategory,
            outcomes=raw.get("outcomes"),
            outcome_prices=raw.get("outcomePrices"),
            clob_token_ids=raw.get("clobTokenIds"),
            volume=_safe_float(raw.get("volume")),
            volume_24hr=_safe_float(raw.get("volume24hr")),
            liquidity=_safe_float(raw.get("liquidity")),
            best_bid=best_bid,
            best_ask=best_ask,
            active=active,
            closed=closed,
            archived=archived,
            enable_order_book=bool(raw.get("enableOrderBook", False)),
            start_date=_safe_datetime(raw.get("startDate")),
            end_date=_safe_datetime(raw.get("endDate")),
            status=_derive_status(active=active, closed=closed, archived=archived),
        )
        market.spread = _compute_spread(best_bid, best_ask)
        market.mid_price = _compute_mid_price(best_bid, best_ask, market.outcome_prices)
        return market
