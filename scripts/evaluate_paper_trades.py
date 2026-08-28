"""Paper trading performance report (requirement #19).

Read-only: loads every row from `paper_trades` via `PaperTradeRepository.
list_all()` and prints the report. No writes, no calls to
app/paper/paper_trading.py's create/settle functions - this only reads
what those already wrote (mirrors scripts/evaluate_resolutions.py's
read-only reporting pattern).

VOID trades (resolutions.resolution_status == "invalid") count toward
"Settled" but never toward wins/losses/win rate - per requirement #19,
an invalid/cancelled outcome is never a win or a loss.

"ROI" and "Average P&L/trade" are reported as distinct numbers:
- Average P&L/trade = mean(pnl) over settled trades (WON+LOST+VOID).
- ROI = sum(pnl) / sum(entry_price) over the same settled set - total
  return over total capital actually staked (fixed 1-unit notional per
  trade, so entry_price is also each trade's "amount staked").

Run with: python scripts/evaluate_paper_trades.py [--db-path PATH]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.loader import load_config  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.paper_trade_models import PaperTrade, PaperTradeStatus  # noqa: E402
from app.storage.repositories import PaperTradeRepository  # noqa: E402

SETTLED_STATUSES = (PaperTradeStatus.WON, PaperTradeStatus.LOST, PaperTradeStatus.VOID)
DETAILED_CLASSIFICATIONS = ("COMPOUND", "WATCH")


def _mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.mean(clean) if clean else None


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def entry_price(trade: PaperTrade) -> float:
    """The simulated cost of the position - entry_probability if the
    selected side is "Yes" (buying at the market's own Yes price), else
    1 - entry_probability (the "No" side's price)."""
    return trade.entry_probability if trade.selected_outcome == "Yes" else 1.0 - trade.entry_probability


@dataclass
class GroupStats:
    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    voids: int = 0
    win_rate: float | None = None
    pnl: float = 0.0


def aggregate_group(label: str, trades: list[PaperTrade]) -> GroupStats:
    stats = GroupStats(label=label, trades=len(trades))
    stats.wins = sum(1 for t in trades if t.status == PaperTradeStatus.WON)
    stats.losses = sum(1 for t in trades if t.status == PaperTradeStatus.LOST)
    stats.voids = sum(1 for t in trades if t.status == PaperTradeStatus.VOID)
    decided = stats.wins + stats.losses
    stats.win_rate = (stats.wins / decided) if decided else None
    stats.pnl = sum(t.pnl for t in trades if t.pnl is not None)
    return stats


def _render_group(out, stats: GroupStats) -> None:
    out(f"{stats.label}:")
    out(f"  trades = {stats.trades}")
    out(f"  wins = {stats.wins}")
    out(f"  losses = {stats.losses}")
    out(f"  void = {stats.voids}")
    out(f"  win rate = {_fmt(stats.win_rate)}")
    out(f"  P&L = {_fmt(stats.pnl)}")
    out("")


def render_report(trades: list[PaperTrade]) -> str:
    lines: list[str] = []
    out = lines.append

    out("=" * 78)
    out("PAPER TRADING PERFORMANCE")
    out("=" * 78)
    out("")

    settled = [t for t in trades if t.status in SETTLED_STATUSES]
    open_trades = [t for t in trades if t.status == PaperTradeStatus.OPEN]
    wins = [t for t in trades if t.status == PaperTradeStatus.WON]
    losses = [t for t in trades if t.status == PaperTradeStatus.LOST]
    voids = [t for t in trades if t.status == PaperTradeStatus.VOID]
    decided = len(wins) + len(losses)
    win_rate = (len(wins) / decided) if decided else None

    out(f"Total paper trades: {len(trades)}")
    out(f"Open: {len(open_trades)}")
    out(f"Settled: {len(settled)}")
    out(f"Wins: {len(wins)}")
    out(f"Losses: {len(losses)}")
    out(f"Void: {len(voids)}")
    out(f"Win rate: {_fmt(win_rate)}")
    out("")

    total_pnl = sum(t.pnl for t in settled if t.pnl is not None)
    avg_pnl = _mean([t.pnl for t in settled])
    total_stake = sum(entry_price(t) for t in settled)
    roi = (total_pnl / total_stake) if total_stake else None

    out(f"Total simulated P&L: {_fmt(total_pnl)}")
    out(f"Average P&L/trade: {_fmt(avg_pnl)}")
    out(f"ROI: {_fmt(roi)}")
    out("")

    for classification in DETAILED_CLASSIFICATIONS:
        group = [t for t in trades if t.classification == classification]
        _render_group(out, aggregate_group(classification, group))

    other_classifications = sorted(
        {t.classification for t in trades} - set(DETAILED_CLASSIFICATIONS)
    )
    for classification in other_classifications:
        group = [t for t in trades if t.classification == classification]
        _render_group(out, aggregate_group(f"{classification} (unexpected value, flagged)", group))

    categories = sorted({t.category for t in trades if t.category})
    for category in categories:
        group = [t for t in trades if t.category == category]
        _render_group(out, aggregate_group(category.upper(), group))

    avg_entry_edge = _mean([t.adjusted_edge for t in trades])
    avg_realized_result = _mean([t.pnl for t in settled])
    holding_hours = [
        (t.settled_at - t.signal_at).total_seconds() / 3600.0
        for t in settled
        if t.settled_at is not None
    ]
    avg_holding_hours = _mean(holding_hours)

    out("-- summary --")
    out(f"Average entry edge: {_fmt(avg_entry_edge)}")
    out(f"Average realized result: {_fmt(avg_realized_result)}")
    out(f"Average holding time (hours): {_fmt(avg_holding_hours, 2)}")

    return "\n".join(lines)


def run_report(db: Database) -> str:
    repo = PaperTradeRepository(db)
    try:
        trades = repo.list_all()
    finally:
        repo.close()
    return render_report(trades)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None, help="Override the configured db_path")
    args = parser.parse_args()

    db_path = args.db_path or load_config().db_path
    db = Database(db_path)
    print(run_report(db))


if __name__ == "__main__":
    main()
