"""Storage-layer model for market resolution/outcome tracking.

Mirrors Market/Features/AnalysisRecord's pattern: this is the model
repositories.py persists, kept in the storage layer so
app/polymarket/resolutions.py (the fetch/parse logic) depends on storage,
not the other way around.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    # Gamma confirmed the market resolved (closed=true,
    # umaResolutionStatus="resolved") but the settlement record itself was
    # inconclusive (e.g. a 50-50 "invalid" resolution for a canceled or
    # postponed event) - a winning outcome is never guessed in this case.
    INVALID = "invalid"


class Resolution(BaseModel):
    market_id: str
    resolution_status: ResolutionStatus
    resolved_at: datetime | None = None
    winning_outcome_index: int | None = None
    winning_outcome: str | None = None
    # Full raw Gamma market dict at the time this was checked, verbatim -
    # the audit trail back to the authoritative source.
    raw_resolution_json: str
    checked_at: datetime
