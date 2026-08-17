from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import MarketRepository


@pytest.fixture
def repo(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    repository = MarketRepository(db)
    yield repository
    repository.close()


def _market(id_="m1", **overrides):
    defaults = dict(id=id_, question="Will it happen?", category="crypto", subcategory="btc_updown")
    defaults.update(overrides)
    return Market(**defaults)


def test_upsert_then_get_market(repo):
    repo.upsert_market(_market())
    fetched = repo.get_market("m1")
    assert fetched is not None
    assert fetched.question == "Will it happen?"
    assert fetched.category == "crypto"


def test_upsert_preserves_first_seen_at_across_updates(repo):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    repo.upsert_market(_market(question="v1"), now=t1)
    repo.upsert_market(_market(question="v2"), now=t2)

    row = repo._conn.execute(
        "SELECT first_seen_at, last_seen_at, question FROM markets WHERE market_id = 'm1'"
    ).fetchone()
    assert row["first_seen_at"] == t1.isoformat()
    assert row["last_seen_at"] == t2.isoformat()
    assert row["question"] == "v2"


def test_bulk_upsert_markets_in_one_transaction(repo):
    markets = [_market(id_="m1"), _market(id_="m2"), _market(id_="m3")]
    count = repo.bulk_upsert_markets(markets)
    assert count == 3
    assert repo.get_market("m1") is not None
    assert repo.get_market("m2") is not None
    assert repo.get_market("m3") is not None


def test_insert_snapshot_is_append_only(repo):
    market = _market()
    repo.upsert_market(market)
    repo.insert_snapshot(market)
    repo.insert_snapshot(market)
    rows = repo._conn.execute(
        "SELECT COUNT(*) as cnt FROM market_snapshots WHERE market_id = 'm1'"
    ).fetchone()
    assert rows["cnt"] == 2


def test_record_scan_upserts_and_snapshots(repo):
    markets = [_market(id_="m1"), _market(id_="m2")]
    count = repo.record_scan(markets)
    assert count == 2
    snap_count = repo._conn.execute("SELECT COUNT(*) as cnt FROM market_snapshots").fetchone()
    assert snap_count["cnt"] == 2
    assert repo.get_market("m1") is not None


def test_list_markets_filters_by_category(repo):
    repo.bulk_upsert_markets(
        [
            _market(id_="m1", category="crypto", subcategory="btc_updown"),
            _market(id_="m2", category="sports", subcategory="football"),
        ]
    )
    crypto_markets = repo.list_markets(category="crypto")
    assert len(crypto_markets) == 1
    assert crypto_markets[0].id == "m1"


def test_list_markets_filters_by_status(repo):
    repo.bulk_upsert_markets(
        [
            _market(id_="m1", status="active"),
            _market(id_="m2", status="closed"),
        ]
    )
    active_markets = repo.list_markets(status="active")
    assert len(active_markets) == 1
    assert active_markets[0].id == "m1"


def test_count_by_category(repo):
    repo.bulk_upsert_markets(
        [
            _market(id_="m1", category="crypto"),
            _market(id_="m2", category="crypto"),
            _market(id_="m3", category="sports"),
        ]
    )
    counts = repo.count_by_category()
    assert counts == {"crypto": 2, "sports": 1}


def test_round_trip_preserves_lists_and_dates(repo):
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    market = _market(
        outcomes=["Yes", "No"],
        outcome_prices=[0.6, 0.4],
        clob_token_ids=["t1", "t2"],
        end_date=end,
    )
    repo.upsert_market(market)
    fetched = repo.get_market("m1")
    assert fetched.outcomes == ["Yes", "No"]
    assert fetched.outcome_prices == [0.6, 0.4]
    assert fetched.clob_token_ids == ["t1", "t2"]
    assert fetched.end_date == end
