import json
from pathlib import Path

import httpx
import pytest
import respx

from app.config.loader import load_config
from app.polymarket.scanner import run_scan_once
from app.storage.database import Database
from app.storage.repositories import MarketRepository

FIXTURE = json.loads(Path("tests/fixtures/gamma_events_sample.json").read_text())


@pytest.mark.asyncio
@respx.mock
async def test_run_scan_once_end_to_end(tmp_path):
    respx.get("https://gamma-api.polymarket.com/events").mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    config = load_config(
        profile_path=Path("config/profile.yaml"), markets_path=Path("config/markets.yaml")
    )
    db = Database(tmp_path / "scan_test.db")
    db.init_schema()

    summary = await run_scan_once(config, db)

    assert summary.events_fetched == 3
    assert summary.raw_markets_seen == 5
    assert summary.categorized == 3
    assert summary.filtered_out == {"low_liquidity": 1, "closed": 1}
    assert summary.duplicates_flagged == 1
    assert summary.persisted == 5
    assert summary.per_category_counts == {"politics": 1, "crypto": 2, "other": 2}

    repo = MarketRepository(db)
    try:
        elon = repo.get_market("mkt-1")
        assert elon.category == "politics"
        assert elon.subcategory == "elon_musk"
        assert elon.filter_reason is None

        closed_market = repo.get_market("mkt-4")
        assert closed_market.filter_reason == "closed"
        assert closed_market.category == "other"

        illiquid = repo.get_market("mkt-3")
        assert illiquid.filter_reason == "low_liquidity"

        duplicate = repo.get_market("mkt-2-dup")
        assert duplicate.is_duplicate is True
        assert duplicate.duplicate_of == "mkt-2"
        assert duplicate.filter_reason == "duplicate"

        snapshot_count = repo._conn.execute(
            "SELECT COUNT(*) as cnt FROM market_snapshots"
        ).fetchone()
        assert snapshot_count["cnt"] == 5
    finally:
        repo.close()
