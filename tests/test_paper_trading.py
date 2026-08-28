"""Tests for app/paper/paper_trading.py - paper-trade creation (from
signal_journal) and settlement (from resolutions). Both are read-driven:
these tests assert they never touch signal_journal/resolutions/analyses,
only paper_trades."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config.loader import load_config
from app.paper.paper_trading import (
    create_paper_trades_for_new_signals,
    derive_selected_outcome,
    run_paper_trading_once,
    settle_open_paper_trades,
)
from app.storage.analysis_models import AnalysisRecord, DataQualityState
from app.storage.database import Database
from app.storage.models import Market
from app.storage.paper_trade_models import PaperTradeStatus
from app.storage.repositories import (
    AnalysisRepository,
    MarketRepository,
    PaperTradeRepository,
    ResolutionRepository,
    SignalJournalRepository,
)
from app.storage.resolution_models import Resolution, ResolutionStatus
from app.storage.signal_journal_models import SignalJournalEntry

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def config():
    return load_config(profile_path=Path("config/profile.yaml"), markets_path=Path("config/markets.yaml"))


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "paper_trading_test.db")
    database.init_schema()
    return database


def _seed_market(db: Database, market_id: str, *, outcomes=("Yes", "No"), category="crypto") -> None:
    market_repo = MarketRepository(db)
    try:
        market_repo.upsert_market(
            Market(
                id=market_id,
                question="Q",
                category=category,
                subcategory="btc_up_down",
                outcomes=list(outcomes),
            )
        )
    finally:
        market_repo.close()


def _seed_signal(db: Database, market_id: str, **overrides) -> None:
    defaults = dict(
        market_id=market_id,
        first_signal_at=T1,
        classification="COMPOUND",
        category="crypto",
        subcategory="btc_up_down",
        market_implied_probability=0.55,
        estimated_probability=0.75,
        adjusted_edge=0.11,
        score=62.83,
    )
    defaults.update(overrides)
    repo = SignalJournalRepository(db)
    try:
        repo.record_first_signal(SignalJournalEntry(**defaults))
    finally:
        repo.close()


def _seed_analysis_with_raw_edge(db: Database, market_id: str, computed_at: datetime, raw_edge: float) -> None:
    repo = AnalysisRepository(db)
    try:
        repo.insert(
            AnalysisRecord(
                market_id=market_id,
                computed_at=computed_at,
                category="crypto",
                market_implied_probability=0.55,
                estimated_probability=0.75,
                raw_edge=raw_edge,
                adjusted_edge=0.11,
                data_quality=DataQualityState.SUFFICIENT,
                quality_gate_passed=True,
                risk_gate_passed=True,
                score=62.83,
                classification="COMPOUND",
            )
        )
    finally:
        repo.close()


def _seed_resolution(db: Database, market_id: str, **overrides) -> None:
    defaults = dict(
        market_id=market_id,
        resolution_status=ResolutionStatus.RESOLVED,
        resolved_at=T1 + timedelta(hours=2),
        winning_outcome_index=0,
        winning_outcome="Yes",
        raw_resolution_json="{}",
        checked_at=T1 + timedelta(hours=2),
    )
    defaults.update(overrides)
    repo = ResolutionRepository(db)
    try:
        repo.upsert(Resolution(**defaults))
    finally:
        repo.close()


# --------------------------------------------------------------------------
# derive_selected_outcome
# --------------------------------------------------------------------------


def test_derive_selected_outcome_boundary_is_yes():
    assert derive_selected_outcome(0.5) == "Yes"
    assert derive_selected_outcome(0.5000001) == "Yes"
    assert derive_selected_outcome(0.4999999) == "No"


# --------------------------------------------------------------------------
# create_paper_trades_for_new_signals
# --------------------------------------------------------------------------


def test_creates_one_trade_per_pending_signal(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1")
    _seed_analysis_with_raw_edge(db, "m1", T1, raw_edge=0.15)

    summary = create_paper_trades_for_new_signals(config, db)
    assert summary.created == 1

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()

    assert trade is not None
    assert trade.status == PaperTradeStatus.OPEN
    assert trade.classification == "COMPOUND"
    assert trade.selected_outcome == "Yes"  # estimated_probability 0.75 >= 0.5
    assert trade.entry_probability == pytest.approx(0.55)  # market_implied_probability at signal time
    assert trade.raw_edge == pytest.approx(0.15)  # recovered from the matching analyses row


def test_running_creation_twice_never_double_creates(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1")
    _seed_analysis_with_raw_edge(db, "m1", T1, raw_edge=0.15)

    first = create_paper_trades_for_new_signals(config, db)
    second = create_paper_trades_for_new_signals(config, db)

    assert first.created == 1
    assert second.created == 0

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trades = paper_trade_repo.list_all()
    finally:
        paper_trade_repo.close()
    assert len(trades) == 1


def test_skips_non_binary_market(config, db):
    _seed_market(db, "m1", outcomes=("Lakers", "Celtics"))
    _seed_signal(db, "m1")

    summary = create_paper_trades_for_new_signals(config, db)
    assert summary.created == 0
    assert summary.skipped_non_binary_market == 1

    paper_trade_repo = PaperTradeRepository(db)
    try:
        assert paper_trade_repo.get("m1") is None
    finally:
        paper_trade_repo.close()


def test_selected_outcome_is_no_when_estimated_probability_below_half(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1", estimated_probability=0.3, classification="WATCH")

    create_paper_trades_for_new_signals(config, db)

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()
    assert trade.selected_outcome == "No"
    assert trade.classification == "WATCH"  # copied verbatim, never reinterpreted/upgraded


def test_raw_edge_is_none_when_no_matching_analyses_row_exists(config, db):
    """Defensive: creation must still succeed even if the exact-match
    analyses lookup misses (e.g. data inconsistency) - raw_edge just stays
    None rather than raising."""
    _seed_market(db, "m1")
    _seed_signal(db, "m1")
    # No analyses row seeded at all.

    summary = create_paper_trades_for_new_signals(config, db)
    assert summary.created == 1

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()
    assert trade.raw_edge is None


# --------------------------------------------------------------------------
# settle_open_paper_trades
# --------------------------------------------------------------------------


def test_settles_won_when_selected_outcome_matches_winner(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1", estimated_probability=0.75)  # selected "Yes"
    create_paper_trades_for_new_signals(config, db)
    _seed_resolution(db, "m1", winning_outcome="Yes")

    summary = settle_open_paper_trades(config, db)
    assert summary.won == 1
    assert summary.lost == 0

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()

    assert trade.status == PaperTradeStatus.WON
    assert trade.pnl == pytest.approx(1.0 - 0.55)  # 1.0 - entry_price(Yes) = 1.0 - market_implied_probability


def test_settles_lost_when_selected_outcome_does_not_match_winner(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1", estimated_probability=0.75)  # selected "Yes"
    create_paper_trades_for_new_signals(config, db)
    _seed_resolution(db, "m1", winning_outcome="No")

    summary = settle_open_paper_trades(config, db)
    assert summary.lost == 1
    assert summary.won == 0

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()

    assert trade.status == PaperTradeStatus.LOST
    assert trade.pnl == pytest.approx(-0.55)  # -entry_price(Yes) = -market_implied_probability


def test_settles_void_on_invalid_resolution_never_a_win_or_loss(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1")
    create_paper_trades_for_new_signals(config, db)
    _seed_resolution(db, "m1", resolution_status=ResolutionStatus.INVALID, winning_outcome=None)

    summary = settle_open_paper_trades(config, db)
    assert summary.void == 1
    assert summary.won == 0
    assert summary.lost == 0

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()

    assert trade.status == PaperTradeStatus.VOID
    assert trade.pnl == pytest.approx(0.0)


def test_unresolved_market_leaves_trade_open(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1")
    create_paper_trades_for_new_signals(config, db)
    _seed_resolution(db, "m1", resolution_status=ResolutionStatus.UNRESOLVED, resolved_at=None, winning_outcome=None)

    summary = settle_open_paper_trades(config, db)
    assert summary.still_open == 1

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()
    assert trade.status == PaperTradeStatus.OPEN


def test_trade_with_no_resolution_row_at_all_leaves_trade_open(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1")
    create_paper_trades_for_new_signals(config, db)
    # No resolutions row seeded.

    summary = settle_open_paper_trades(config, db)
    assert summary.still_open == 1


def test_settlement_never_re_settles_an_already_settled_trade(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1", estimated_probability=0.75)
    create_paper_trades_for_new_signals(config, db)
    _seed_resolution(db, "m1", winning_outcome="Yes")
    settle_open_paper_trades(config, db)

    paper_trade_repo = PaperTradeRepository(db)
    try:
        first_pnl = paper_trade_repo.get("m1").pnl
    finally:
        paper_trade_repo.close()

    # Running settlement again must be a pure no-op - no OPEN trades left.
    summary = settle_open_paper_trades(config, db)
    assert summary.won == 0
    assert summary.lost == 0
    assert summary.void == 0
    assert summary.still_open == 0

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()
    assert trade.pnl == pytest.approx(first_pnl)


# --------------------------------------------------------------------------
# run_paper_trading_once - end to end
# --------------------------------------------------------------------------


def test_run_paper_trading_once_creates_and_settles_in_one_call(config, db):
    _seed_market(db, "m1")
    _seed_signal(db, "m1", estimated_probability=0.75)
    _seed_analysis_with_raw_edge(db, "m1", T1, raw_edge=0.15)
    _seed_resolution(db, "m1", winning_outcome="Yes")

    summary = run_paper_trading_once(config, db)

    assert summary.creation.created == 1
    assert summary.settlement.won == 1

    paper_trade_repo = PaperTradeRepository(db)
    try:
        trade = paper_trade_repo.get("m1")
    finally:
        paper_trade_repo.close()
    assert trade.status == PaperTradeStatus.WON
    assert trade.raw_edge == pytest.approx(0.15)
