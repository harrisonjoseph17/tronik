"""One-shot scan orchestrator: fetch -> categorize -> normalize -> filter ->
dedup-flag -> persist. Every discovered market is persisted (see
markets.py's module docstring) - filtering only annotates filter_reason.

Runnable via `python main.py scan` or `python -m app.polymarket.scanner`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config.loader import AppConfig, load_config
from app.polymarket.categorize import OTHER_CATEGORY
from app.polymarket.client import GammaClient
from app.polymarket.markets import (
    categorize_and_normalize,
    extract_markets_from_events,
    fetch_active_events,
    filter_markets,
    flag_duplicates,
)
from app.storage.database import Database
from app.storage.repositories import MarketRepository

logger = logging.getLogger(__name__)


@dataclass
class ScanSummary:
    events_fetched: int = 0
    raw_markets_seen: int = 0
    categorized: int = 0
    filtered_out: dict[str, int] = field(default_factory=dict)
    duplicates_flagged: int = 0
    persisted: int = 0
    elapsed_seconds: float = 0.0
    per_category_counts: dict[str, int] = field(default_factory=dict)


async def run_scan_once(config: AppConfig, db: Database) -> ScanSummary:
    start = time.monotonic()
    summary = ScanSummary()

    client = GammaClient(
        timeout=config.filters.request_timeout_seconds,
        max_retries=config.filters.max_retries,
        rate_limit_per_10s=config.filters.gamma_rate_limit_per_10s,
    )
    try:
        events = [
            event
            async for event in fetch_active_events(
                client,
                order=config.filters.events_order,
                ascending=config.filters.events_ascending,
                page_size=config.filters.events_page_size,
                max_pages=config.filters.events_max_pages,
            )
        ]
    finally:
        await client.aclose()
    summary.events_fetched = len(events)

    raw_markets = extract_markets_from_events(events)
    summary.raw_markets_seen = len(raw_markets)

    markets = categorize_and_normalize(raw_markets, config.category_rules)
    summary.categorized = sum(1 for m in markets if m.category != OTHER_CATEGORY)

    markets, filtered_out = filter_markets(
        markets,
        min_liquidity=config.filters.min_liquidity,
        max_spread=config.filters.max_spread,
        min_hours_to_resolution=config.filters.min_hours_to_resolution,
    )
    summary.filtered_out = filtered_out

    markets = flag_duplicates(markets)
    summary.duplicates_flagged = sum(1 for m in markets if m.is_duplicate)

    for market in markets:
        summary.per_category_counts[market.category] = (
            summary.per_category_counts.get(market.category, 0) + 1
        )

    repo = MarketRepository(db)
    try:
        summary.persisted = repo.record_scan(markets, now=datetime.now(timezone.utc))
    finally:
        repo.close()

    summary.elapsed_seconds = round(time.monotonic() - start, 2)
    logger.info(
        "scan complete: events=%d raw_markets=%d categorized=%d filtered_out=%s "
        "duplicates=%d persisted=%d elapsed=%.2fs",
        summary.events_fetched,
        summary.raw_markets_seen,
        summary.categorized,
        summary.filtered_out,
        summary.duplicates_flagged,
        summary.persisted,
        summary.elapsed_seconds,
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    database = Database(cfg.db_path)
    database.init_schema()
    asyncio.run(run_scan_once(cfg, database))
