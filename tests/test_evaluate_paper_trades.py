from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.database import Database
from app.storage.models import Market
from app.storage.paper_trade_models import PaperTrade, PaperTradeStatus
from app.storage.repositories import MarketRepository, PaperTradeRepository
from scripts.evaluate_paper_trades import (
    aggregate_group,
    entry_price,
    render_report,
    run_report,
)

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


# --------------------------------------------------------------------------
# entry_price
# --------------------------------------------------------------------------


def test_entry_price_for_yes_is_entry_probability():
    trade = _trade(selected_outcome="Yes", entry_probability=0.6)
    assert entry_price(trade) == pytest.approx(0.6)


def test_entry_price_for_no_is_one_minus_entry_probability():
    trade = _trade(selected_outcome="No", entry_probability=0.6)
    assert entry_price(trade) == pytest.approx(0.4)


# --------------------------------------------------------------------------
# aggregate_group
# --------------------------------------------------------------------------


def test_aggregate_group_computes_win_rate_from_decided_trades_only():
    trades = [
        _trade(market_id="m1", status=PaperTradeStatus.WON, pnl=0.4),
        _trade(market_id="m2", status=PaperTradeStatus.LOST, pnl=-0.6),
        _trade(market_id="m3", status=PaperTradeStatus.VOID, pnl=0.0),
        _trade(market_id="m4", status=PaperTradeStatus.OPEN),
    ]
    stats = aggregate_group("ALL", trades)

    assert stats.trades == 4
    assert stats.wins == 1
    assert stats.losses == 1
    assert stats.voids == 1
    # win rate excludes void and open - only decided (won+lost) trades count
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.pnl == pytest.approx(-0.2)  # 0.4 + -0.6 + 0.0, open trades contribute nothing (pnl=None)


def test_aggregate_group_win_rate_is_none_with_no_decided_trades():
    trades = [_trade(market_id="m1", status=PaperTradeStatus.OPEN)]
    stats = aggregate_group("ALL", trades)
    assert stats.win_rate is None


# --------------------------------------------------------------------------
# render_report
# --------------------------------------------------------------------------


def test_render_report_totals_and_roi():
    trades = [
        _trade(
            market_id="m1",
            selected_outcome="Yes",
            entry_probability=0.6,
            status=PaperTradeStatus.WON,
            settled_at=T1 + timedelta(hours=5),
            winning_outcome="Yes",
            pnl=0.4,  # 1.0 - 0.6
        ),
        _trade(
            market_id="m2",
            selected_outcome="Yes",
            entry_probability=0.5,
            status=PaperTradeStatus.LOST,
            settled_at=T1 + timedelta(hours=3),
            winning_outcome="No",
            pnl=-0.5,  # -0.5
        ),
    ]
    report = render_report(trades)

    assert "Total paper trades: 2" in report
    assert "Settled: 2" in report
    assert "Wins: 1" in report
    assert "Losses: 1" in report
    assert "Win rate: 0.5000" in report

    # Total P&L = 0.4 + -0.5 = -0.1; total stake = 0.6 + 0.5 = 1.1
    assert "Total simulated P&L: -0.1000" in report
    assert f"Average P&L/trade: {(-0.1 / 2):.4f}" in report
    assert f"ROI: {(-0.1 / 1.1):.4f}" in report


def test_render_report_void_counts_as_settled_but_not_win_or_loss():
    trades = [
        _trade(
            market_id="m1",
            status=PaperTradeStatus.VOID,
            settled_at=T1 + timedelta(hours=1),
            winning_outcome=None,
            pnl=0.0,
        )
    ]
    report = render_report(trades)

    assert "Total paper trades: 1" in report
    assert "Settled: 1" in report
    assert "Wins: 0" in report
    assert "Losses: 0" in report
    assert "Void: 1" in report
    assert "Win rate: n/a" in report  # no decided trades


def test_render_report_classification_and_category_sections():
    trades = [
        _trade(market_id="m1", classification="COMPOUND", category="crypto", status=PaperTradeStatus.WON, pnl=0.4),
        _trade(market_id="m2", classification="WATCH", category="politics", status=PaperTradeStatus.LOST, pnl=-0.5),
    ]
    report = render_report(trades)

    assert "COMPOUND:" in report
    assert "WATCH:" in report
    assert "CRYPTO:" in report
    assert "POLITICS:" in report


def test_render_report_holding_time_computed_from_signal_to_settled():
    trades = [
        _trade(
            market_id="m1",
            status=PaperTradeStatus.WON,
            signal_at=T1,
            settled_at=T1 + timedelta(hours=6),
            winning_outcome="Yes",
            pnl=0.4,
        )
    ]
    report = render_report(trades)
    assert "Average holding time (hours): 6.00" in report


def test_render_report_open_trades_excluded_from_pnl_and_holding_time():
    trades = [_trade(market_id="m1", status=PaperTradeStatus.OPEN)]
    report = render_report(trades)
    assert "Total simulated P&L: 0.0000" in report
    assert "Average holding time (hours): n/a" in report


# --------------------------------------------------------------------------
# End-to-end against a real (temp) SQLite DB
# --------------------------------------------------------------------------


def test_end_to_end_report_reflects_real_db(tmp_path: Path):
    db = Database(tmp_path / "eval_pt_test.db")
    db.init_schema()

    market_repo = MarketRepository(db)
    try:
        market_repo.upsert_market(Market(id="m1", question="Q", category="crypto", subcategory="btc_up_down"))
    finally:
        market_repo.close()

    paper_trade_repo = PaperTradeRepository(db)
    try:
        paper_trade_repo.create(_trade(market_id="m1"))
        paper_trade_repo.settle(
            "m1", status=PaperTradeStatus.WON, settled_at=T1 + timedelta(hours=2), winning_outcome="Yes", pnl=0.4
        )
    finally:
        paper_trade_repo.close()

    report = run_report(db)
    assert "Total paper trades: 1" in report
    assert "Wins: 1" in report
