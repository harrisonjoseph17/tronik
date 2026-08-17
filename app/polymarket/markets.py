"""Discovery orchestration: pagination, market extraction, categorization,
filtering, and duplicate detection over the Gamma API.

Filtering never discards a market - `filter_markets` only annotates each
Market with a `filter_reason` (None means it currently belongs in the
"included" analysis universe). Every discovered market is still persisted so
later stages can revisit these decisions and so nothing found by the scanner
silently disappears.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
from typing import Any

from app.polymarket.categorize import CategoryRule, categorize_market
from app.polymarket.client import GammaClient
from app.storage.models import Market

logger = logging.getLogger(__name__)

FILTER_REASON_CLOSED = "closed"
FILTER_REASON_LOW_LIQUIDITY = "low_liquidity"
FILTER_REASON_WIDE_SPREAD = "wide_spread"
FILTER_REASON_NEAR_RESOLUTION = "near_resolution"
FILTER_REASON_DUPLICATE = "duplicate"


def _extract_page(response: Any) -> tuple[list[dict], bool | None]:
    """Handle both plausible Gamma /events response shapes defensively.

    The exact shape is unverified pending a live smoke test. A bare JSON
    list (has_more inferred by caller from page size) and a
    {"data": [...], "has_more": bool} envelope are both supported.
    """
    if isinstance(response, list):
        return response, None
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            return data, bool(response.get("has_more", False))
        logger.warning(
            "unexpected Gamma /events response shape (dict without list 'data'): keys=%s",
            list(response.keys()),
        )
        return [], False
    logger.warning("unexpected Gamma /events response type: %r", type(response))
    return [], False


async def fetch_active_events(
    client: GammaClient,
    *,
    order: str = "volume24hr",
    ascending: bool = False,
    page_size: int = 100,
    max_pages: int = 20,
) -> AsyncIterator[dict]:
    offset = 0
    for _ in range(max_pages):
        response = await client.get_events(
            active=True,
            closed=False,
            limit=page_size,
            offset=offset,
            order=order,
            ascending=ascending,
        )
        page, has_more = _extract_page(response)
        if not page:
            return
        for event in page:
            yield event

        if has_more is False:
            return
        if has_more is None and len(page) < page_size:
            return
        offset += page_size
    logger.warning("hit max_pages=%d safety cap while fetching events", max_pages)


def extract_markets_from_events(events: Iterable[dict]) -> list[dict]:
    """Flatten each event's nested markets, attaching parent event context."""
    raw_markets: list[dict] = []
    for event in events:
        nested = event.get("markets")
        if nested is None:
            continue
        if not isinstance(nested, list):
            logger.warning(
                "event %s has a non-list 'markets' field: %r", event.get("id"), type(nested)
            )
            continue
        event_tags = [
            tag.get("label") or tag.get("name") or ""
            for tag in (event.get("tags") or [])
            if isinstance(tag, dict)
        ]
        for nested_market in nested:
            raw_market = dict(nested_market)  # avoid mutating the caller's event dict
            raw_market.setdefault("event_id", event.get("id"))
            raw_market["_event_title"] = event.get("title")
            raw_market["_event_tags"] = event_tags
            raw_markets.append(raw_market)
    return raw_markets


def categorize_and_normalize(raw_markets: Iterable[dict], rules: list[CategoryRule]) -> list[Market]:
    markets: list[Market] = []
    for raw in raw_markets:
        event_title = raw.get("_event_title")
        event_tags = raw.get("_event_tags") or []
        category, subcategory = categorize_market(
            raw.get("question") or raw.get("title") or "", event_title, event_tags, rules
        )
        markets.append(Market.from_gamma(raw, category=category, subcategory=subcategory))
    return markets


def filter_markets(
    markets: list[Market],
    *,
    min_liquidity: float,
    max_spread: float,
    min_hours_to_resolution: float,
    now: datetime | None = None,
) -> tuple[list[Market], dict[str, int]]:
    """Annotate each market's filter_reason in place; nothing is removed.

    Returns the same list plus a {reason: count} dict for scan-summary
    logging. Reasons are mutually exclusive, checked in priority order.
    """
    now = now or datetime.now(timezone.utc)
    reason_counts: dict[str, int] = defaultdict(int)

    for market in markets:
        if market.status in ("closed", "archived", "invalid"):
            market.filter_reason = FILTER_REASON_CLOSED
        elif market.liquidity is not None and market.liquidity < min_liquidity:
            market.filter_reason = FILTER_REASON_LOW_LIQUIDITY
        elif market.spread is not None and market.spread > max_spread:
            market.filter_reason = FILTER_REASON_WIDE_SPREAD
        elif market.end_date is not None and _hours_until(market.end_date, now) < min_hours_to_resolution:
            market.filter_reason = FILTER_REASON_NEAR_RESOLUTION
        else:
            market.filter_reason = None

        if market.filter_reason:
            reason_counts[market.filter_reason] += 1

    return markets, dict(reason_counts)


def _hours_until(target: datetime, now: datetime) -> float:
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - now).total_seconds() / 3600.0


def flag_duplicates(markets: list[Market]) -> list[Market]:
    """Hard dedup by id; soft-flag markets sharing an event_id + identical
    clob_token_ids set as likely duplicates. Duplicates are flagged (and get
    a filter_reason if they don't already have a higher-priority one from
    filter_markets), never removed - the highest-volume market in a
    duplicate group is kept as canonical.
    """
    seen_ids: set[str] = set()
    deduped: list[Market] = []
    for market in markets:
        if market.id in seen_ids:
            continue
        seen_ids.add(market.id)
        deduped.append(market)

    groups: dict[tuple[str, frozenset[str]], list[Market]] = defaultdict(list)
    for market in deduped:
        if market.event_id and market.clob_token_ids:
            key = (market.event_id, frozenset(market.clob_token_ids))
            groups[key].append(market)

    for group in groups.values():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda m: m.volume or 0.0, reverse=True)
        canonical = group_sorted[0]
        for duplicate in group_sorted[1:]:
            duplicate.is_duplicate = True
            duplicate.duplicate_of = canonical.id
            if duplicate.filter_reason is None:
                duplicate.filter_reason = FILTER_REASON_DUPLICATE

    return deduped
