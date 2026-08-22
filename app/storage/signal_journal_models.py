"""Storage-layer model for the signal journal (requirement #18).

Records each market's FIRST actionable (COMPOUND/WATCH) analysis snapshot,
written once and never overwritten by later analyze cycles. Mirrors
Resolution's pattern: `market_id` is the primary key (exactly one row per
market), keeping this joinable with the `resolutions` table by market_id
without any FK between the two - the same non-FK-join precedent already
used between `analyses` and `resolutions`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SignalJournalEntry(BaseModel):
    market_id: str
    first_signal_at: datetime
    classification: str
    category: str | None = None
    subcategory: str | None = None
    market_implied_probability: float | None = None
    estimated_probability: float | None = None
    adjusted_edge: float | None = None
    score: float | None = None
