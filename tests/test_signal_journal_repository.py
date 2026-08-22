from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import MarketRepository, SignalJournalRepository
from app.storage.signal_journal_models import SignalJournalEntry

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    # signal_journal has a FK to markets(market_id) - seed m1.
    market_repo = MarketRepository(database)
    try:
        market_repo.upsert_market(Market(id="m1", question="Q1", category="crypto", subcategory="btc_up_down"))
    finally:
        market_repo.close()
    return database


@pytest.fixture
def repo(db):
    repository = SignalJournalRepository(db)
    yield repository
    repository.close()


def _entry(**overrides) -> SignalJournalEntry:
    defaults = dict(
        market_id="m1",
        first_signal_at=T1,
        classification="COMPOUND",
        category="crypto",
        subcategory="btc_up_down",
        market_implied_probability=0.55,
        estimated_probability=0.68,
        adjusted_edge=0.11,
        score=62.83,
    )
    defaults.update(overrides)
    return SignalJournalEntry(**defaults)


def test_record_first_signal_inserts_and_returns_true(repo):
    recorded = repo.record_first_signal(_entry())
    assert recorded is True

    fetched = repo.get("m1")
    assert fetched is not None
    assert fetched.classification == "COMPOUND"
    assert fetched.score == pytest.approx(62.83)
    assert fetched.first_signal_at == T1


def test_repeated_record_first_signal_is_a_no_op(repo):
    """Core idempotency requirement: a later analyze cycle producing
    different values must never overwrite the first-recorded signal."""
    repo.record_first_signal(_entry(first_signal_at=T1, classification="COMPOUND", score=62.83))
    recorded_again = repo.record_first_signal(
        _entry(
            first_signal_at=T2,
            classification="WATCH",
            score=40.0,
            estimated_probability=0.99,
            market_implied_probability=0.99,
            adjusted_edge=0.0,
        )
    )

    assert recorded_again is False
    fetched = repo.get("m1")
    assert fetched.first_signal_at == T1
    assert fetched.classification == "COMPOUND"
    assert fetched.score == pytest.approx(62.83)
    assert fetched.estimated_probability == pytest.approx(0.68)


def test_get_missing_market_returns_none(repo):
    assert repo.get("does-not-exist") is None


def test_list_by_classification_filters_correctly(db):
    market_repo = MarketRepository(db)
    try:
        market_repo.upsert_market(Market(id="m2", question="Q2", category="politics", subcategory="elon_musk_tweets"))
    finally:
        market_repo.close()

    repo = SignalJournalRepository(db)
    try:
        repo.record_first_signal(_entry(market_id="m1", classification="COMPOUND"))
        repo.record_first_signal(_entry(market_id="m2", classification="WATCH", category="politics", subcategory="elon_musk_tweets"))

        compound_rows = repo.list_by_classification("COMPOUND")
        watch_rows = repo.list_by_classification("WATCH")
        avoid_rows = repo.list_by_classification("AVOID")
    finally:
        repo.close()

    assert [r.market_id for r in compound_rows] == ["m1"]
    assert [r.market_id for r in watch_rows] == ["m2"]
    assert avoid_rows == []
