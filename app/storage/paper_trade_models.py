"""Storage-layer model for paper trading (requirement #19).

One simulated position per market, opened the first time signal_journal
records an actionable (COMPOUND/WATCH) signal for it, closed once
resolutions confirms an outcome. Mirrors Resolution/SignalJournalEntry's
pattern: this is the model repositories.py persists.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class PaperTradeStatus(str, Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    # resolutions.resolution_status == "invalid" (inconclusive settlement,
    # e.g. a 50-50 canceled/postponed market) - never a win or a loss,
    # modeled as a full refund of the entry cost (pnl = 0.0).
    VOID = "VOID"


class PaperTrade(BaseModel):
    market_id: str
    classification: str
    category: str | None = None
    subcategory: str | None = None
    signal_at: datetime
    selected_outcome: str
    # The market's own price at signal time (== signal_journal's
    # market_implied_probability) - the simulated fill price, never the
    # model's own estimated_probability and never a later/better price.
    entry_probability: float
    estimated_probability: float | None = None
    raw_edge: float | None = None
    adjusted_edge: float | None = None
    score: float | None = None
    status: PaperTradeStatus = PaperTradeStatus.OPEN
    settled_at: datetime | None = None
    winning_outcome: str | None = None
    pnl: float | None = None
