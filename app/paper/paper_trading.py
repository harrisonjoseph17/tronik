"""Paper trading (requirement #19).

Sits strictly after the signal journal in the pipeline:

    SIGNAL JOURNAL -> PAPER TRADE -> RESOLUTION -> PAPER P&L

Read-only with respect to every upstream table - this module never writes
to markets/analyses/resolutions/signal_journal, and never calls
app/analysis/* or app/polymarket/client.py (no HTTP, no CLOB, no wallet,
no order submission - pure simulation over data already collected).

Two independent, purely synchronous steps (no network I/O is needed for
either, unlike scan/analyze/resolve):

- create_paper_trades_for_new_signals(): opens exactly one paper trade per
  signal_journal row that doesn't have one yet (signal_journal's own
  market_id-PK uniqueness is the natural dedup point - this never hooks
  into pipeline.py, it just reads what pipeline.py already wrote).
- settle_open_paper_trades(): closes any OPEN trade whose market now has a
  resolutions row that isn't 'unresolved'.

Entry price convention (see the module-level design note in the approved
plan): entry_probability is signal_journal's market_implied_probability -
the market's own price at the moment of the first actionable signal, never
the model's estimated_probability and never a later/better price. This
answers "if we had actually taken this signal when the system generated
it, what would have happened?" per the master requirement.

Fixed 1-unit notional per trade, no bankroll/position sizing (explicitly
out of scope). WON pays 1.0, LOST pays 0.0, VOID (resolutions.
resolution_status == 'invalid') refunds the entry cost (pnl = 0.0) - never
a win or a loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config.loader import AppConfig
from app.storage.database import Database
from app.storage.paper_trade_models import PaperTrade, PaperTradeStatus
from app.storage.repositories import (
    AnalysisRepository,
    MarketRepository,
    PaperTradeRepository,
    ResolutionRepository,
    SignalJournalRepository,
)
from app.storage.resolution_models import ResolutionStatus

logger = logging.getLogger(__name__)

BINARY_OUTCOMES = {"Yes", "No"}


def derive_selected_outcome(estimated_probability: float) -> str:
    """Same >=0.5 threshold used everywhere else in this project (e.g.
    scripts/evaluate_resolutions.py::derive_predicted_side) - kept as its
    own small pure function here rather than imported from scripts/, since
    app/ code should not depend on the scripts/ layer."""
    return "Yes" if estimated_probability >= 0.5 else "No"


@dataclass
class CreationSummary:
    created: int = 0
    skipped_non_binary_market: int = 0
    skipped_market_not_found: int = 0
    skipped_estimate_missing: int = 0
    # The exact trades created this run, in case a caller (e.g.
    # app/notify/telegram.py) needs to know which without a second query -
    # requirement #20. Purely additive - existing count-based assertions
    # are unaffected.
    created_trades: list[PaperTrade] = field(default_factory=list)


def create_paper_trades_for_new_signals(
    config: AppConfig, db: Database, *, limit: int = 500
) -> CreationSummary:
    summary = CreationSummary()

    market_repo = MarketRepository(db)
    analysis_repo = AnalysisRepository(db)
    signal_journal_repo = SignalJournalRepository(db)
    paper_trade_repo = PaperTradeRepository(db)
    try:
        market_ids = paper_trade_repo.list_pending_signal_market_ids(limit=limit)
        for market_id in market_ids:
            entry = signal_journal_repo.get(market_id)
            if entry is None:
                continue  # defensive - list_pending_signal_market_ids guarantees a row exists

            market = market_repo.get_market(market_id)
            if market is None:
                summary.skipped_market_not_found += 1
                continue

            if set(market.outcomes) != BINARY_OUTCOMES:
                # Never guess a "Yes"/"No" position on a market that isn't
                # actually framed that way - same discipline as
                # app/polymarket/resolutions.py::determine_outcome and
                # scripts/evaluate_resolutions.py's binary-outcome checks.
                summary.skipped_non_binary_market += 1
                continue

            if entry.estimated_probability is None or entry.market_implied_probability is None:
                # PaperTrade.entry_probability is a required float - a
                # market_implied_probability of None would fail pydantic
                # validation, so this is checked defensively even though
                # it should be rare in practice (both are core inputs to
                # the edge calculation that gated this market to
                # COMPOUND/WATCH in the first place).
                summary.skipped_estimate_missing += 1
                continue

            analysis_at_signal = analysis_repo.get_at(market_id, entry.first_signal_at)
            raw_edge = analysis_at_signal.raw_edge if analysis_at_signal is not None else None

            trade = PaperTrade(
                market_id=market_id,
                classification=entry.classification,
                category=entry.category,
                subcategory=entry.subcategory,
                signal_at=entry.first_signal_at,
                selected_outcome=derive_selected_outcome(entry.estimated_probability),
                entry_probability=entry.market_implied_probability,
                estimated_probability=entry.estimated_probability,
                raw_edge=raw_edge,
                adjusted_edge=entry.adjusted_edge,
                score=entry.score,
            )
            if paper_trade_repo.create(trade):
                summary.created += 1
                summary.created_trades.append(trade)
    finally:
        market_repo.close()
        analysis_repo.close()
        signal_journal_repo.close()
        paper_trade_repo.close()

    return summary


@dataclass
class SettlementSummary:
    won: int = 0
    lost: int = 0
    void: int = 0
    still_open: int = 0
    # The exact trades settled this run, with their final status/pnl/
    # winning_outcome already applied - requirement #20, same rationale as
    # CreationSummary.created_trades above.
    settled_trades: list[PaperTrade] = field(default_factory=list)


def settle_open_paper_trades(config: AppConfig, db: Database, *, limit: int = 500) -> SettlementSummary:
    summary = SettlementSummary()
    now = datetime.now(timezone.utc)

    resolution_repo = ResolutionRepository(db)
    paper_trade_repo = PaperTradeRepository(db)
    try:
        for trade in paper_trade_repo.list_open(limit=limit):
            resolution = resolution_repo.get(trade.market_id)
            if resolution is None or resolution.resolution_status == ResolutionStatus.UNRESOLVED:
                summary.still_open += 1
                continue

            if resolution.resolution_status == ResolutionStatus.INVALID:
                if paper_trade_repo.settle(
                    trade.market_id,
                    status=PaperTradeStatus.VOID,
                    settled_at=now,
                    winning_outcome=None,
                    pnl=0.0,
                ):
                    summary.void += 1
                    summary.settled_trades.append(
                        trade.model_copy(
                            update={
                                "status": PaperTradeStatus.VOID,
                                "settled_at": now,
                                "winning_outcome": None,
                                "pnl": 0.0,
                            }
                        )
                    )
                continue

            # RESOLVED - compare selected_outcome to the actual winner.
            entry_price = (
                trade.entry_probability
                if trade.selected_outcome == "Yes"
                else 1.0 - trade.entry_probability
            )
            won = trade.selected_outcome == resolution.winning_outcome
            pnl = (1.0 - entry_price) if won else -entry_price
            status = PaperTradeStatus.WON if won else PaperTradeStatus.LOST
            if paper_trade_repo.settle(
                trade.market_id,
                status=status,
                settled_at=now,
                winning_outcome=resolution.winning_outcome,
                pnl=pnl,
            ):
                if won:
                    summary.won += 1
                else:
                    summary.lost += 1
                summary.settled_trades.append(
                    trade.model_copy(
                        update={
                            "status": status,
                            "settled_at": now,
                            "winning_outcome": resolution.winning_outcome,
                            "pnl": pnl,
                        }
                    )
                )
    finally:
        resolution_repo.close()
        paper_trade_repo.close()

    return summary


@dataclass
class PaperTradingSummary:
    creation: CreationSummary
    settlement: SettlementSummary


def run_paper_trading_once(config: AppConfig, db: Database) -> PaperTradingSummary:
    creation = create_paper_trades_for_new_signals(config, db)
    settlement = settle_open_paper_trades(config, db)

    logger.info(
        "paper trading complete: created=%d skipped_non_binary=%d skipped_not_found=%d "
        "won=%d lost=%d void=%d still_open=%d",
        creation.created,
        creation.skipped_non_binary_market,
        creation.skipped_market_not_found,
        settlement.won,
        settlement.lost,
        settlement.void,
        settlement.still_open,
    )
    return PaperTradingSummary(creation=creation, settlement=settlement)
