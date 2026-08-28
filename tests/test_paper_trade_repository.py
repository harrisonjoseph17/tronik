from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.database import Database
from app.storage.models import Market
from app.storage.paper_trade_models import PaperTrade, PaperTradeStatus
from app.storage.repositories import MarketRepository, PaperTradeRepository, SignalJournalRepository
from app.storage.signal_journal_models import SignalJournalEntry

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    # paper_trades has a FK to markets(market_id) - seed m1.
    market_repo = MarketRepository(database)
    try:
        market_repo.upsert_market(Market(id="m1", question="Q1", category="crypto", subcategory="btc_up_down"))
    finally:
        market_repo.close()
    return database


@pytest.fixture
def repo(db):
    repository = PaperTradeRepository(db)
    yield repository
    repository.close()


def _trade(**overrides) -> PaperTrade:
    defaults = dict(
        market_id="m1",
        classification="COMPOUND",
        category="crypto",
        subcategory="btc_up_down",
        signal_at=T1,
        selected_outcome="Yes",
        entry_probability=0.6,
        estimated_probability=0.75,
        raw_edge=0.15,
        adjusted_edge=0.1,
        score=62.83,
    )
    defaults.update(overrides)
    return PaperTrade(**defaults)


def test_create_inserts_and_returns_true(repo):
    created = repo.create(_trade())
    assert created is True

    fetched = repo.get("m1")
    assert fetched is not None
    assert fetched.status == PaperTradeStatus.OPEN
    assert fetched.classification == "COMPOUND"
    assert fetched.entry_probability == pytest.approx(0.6)
    assert fetched.signal_at == T1


def test_repeated_create_is_a_no_op(repo):
    """Core dedup requirement: only ONE paper trade per market_id, ever -
    a second create() call for the same market must never overwrite it,
    even with very different values."""
    repo.create(_trade(selected_outcome="Yes", entry_probability=0.6, score=62.83))
    created_again = repo.create(
        _trade(selected_outcome="No", entry_probability=0.2, score=40.0, classification="WATCH")
    )

    assert created_again is False
    fetched = repo.get("m1")
    assert fetched.selected_outcome == "Yes"
    assert fetched.entry_probability == pytest.approx(0.6)
    assert fetched.classification == "COMPOUND"


def test_get_missing_market_returns_none(repo):
    assert repo.get("does-not-exist") is None


def test_list_pending_signal_market_ids_excludes_markets_with_a_paper_trade(db):
    market_repo = MarketRepository(db)
    signal_journal_repo = SignalJournalRepository(db)
    paper_trade_repo = PaperTradeRepository(db)
    try:
        market_repo.upsert_market(Market(id="m2", question="Q2", category="crypto", subcategory="btc_up_down"))
        signal_journal_repo.record_first_signal(
            SignalJournalEntry(
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
        )
        signal_journal_repo.record_first_signal(
            SignalJournalEntry(
                market_id="m2",
                first_signal_at=T1,
                classification="WATCH",
                category="crypto",
                subcategory="btc_up_down",
                market_implied_probability=0.55,
                estimated_probability=0.62,
                adjusted_edge=0.07,
                score=50.0,
            )
        )
        paper_trade_repo.create(_trade(market_id="m1"))

        pending = paper_trade_repo.list_pending_signal_market_ids()
    finally:
        market_repo.close()
        signal_journal_repo.close()
        paper_trade_repo.close()

    assert pending == ["m2"]


def test_list_open_returns_only_open_trades(repo):
    repo.create(_trade(market_id="m1"))
    open_trades = repo.list_open()
    assert [t.market_id for t in open_trades] == ["m1"]

    repo.settle(
        "m1", status=PaperTradeStatus.WON, settled_at=T2, winning_outcome="Yes", pnl=0.4
    )
    assert repo.list_open() == []


def test_settle_transitions_open_to_won_with_correct_fields(repo):
    repo.create(_trade())
    settled = repo.settle(
        "m1", status=PaperTradeStatus.WON, settled_at=T2, winning_outcome="Yes", pnl=0.4
    )
    assert settled is True

    fetched = repo.get("m1")
    assert fetched.status == PaperTradeStatus.WON
    assert fetched.settled_at == T2
    assert fetched.winning_outcome == "Yes"
    assert fetched.pnl == pytest.approx(0.4)


def test_settle_is_immutable_once_settled(repo):
    """A second settle() call must never change the original settlement -
    same guarantee as ResolutionRepository.upsert's immutability."""
    repo.create(_trade())
    repo.settle("m1", status=PaperTradeStatus.WON, settled_at=T1, winning_outcome="Yes", pnl=0.4)

    settled_again = repo.settle(
        "m1", status=PaperTradeStatus.LOST, settled_at=T2, winning_outcome="No", pnl=-0.6
    )

    assert settled_again is False
    fetched = repo.get("m1")
    assert fetched.status == PaperTradeStatus.WON
    assert fetched.settled_at == T1
    assert fetched.winning_outcome == "Yes"
    assert fetched.pnl == pytest.approx(0.4)


def test_settle_missing_market_returns_false(repo):
    assert repo.settle(
        "does-not-exist", status=PaperTradeStatus.WON, settled_at=T1, winning_outcome="Yes", pnl=0.4
    ) is False


def test_list_all_returns_every_trade_ordered_by_signal_at(db):
    market_repo = MarketRepository(db)
    try:
        market_repo.upsert_market(Market(id="m2", question="Q2", category="crypto", subcategory="btc_up_down"))
    finally:
        market_repo.close()

    repo = PaperTradeRepository(db)
    try:
        repo.create(_trade(market_id="m2", signal_at=T2))
        repo.create(_trade(market_id="m1", signal_at=T1))
        trades = repo.list_all()
    finally:
        repo.close()

    assert [t.market_id for t in trades] == ["m1", "m2"]
