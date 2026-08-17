"""Storage-layer models for Stage 2 (features + analyses).

Mirrors Market's pattern in models.py: these are the models repositories.py
persists, kept in the storage layer so app/analysis/ (the business logic)
depends on storage, not the other way around.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class DataQualityState(str, Enum):
    SUFFICIENT = "DATA_SUFFICIENT"
    PARTIAL = "DATA_PARTIAL"
    INSUFFICIENT = "DATA_INSUFFICIENT"


class Features(BaseModel):
    market_id: str
    computed_at: datetime

    # Raw market-implied probability (from Market.mid_price) - preserved
    # here untouched; estimated_probability (in AnalysisRecord) is derived
    # from this but never overwrites it.
    market_implied_probability: float | None = None

    volume: float | None = None
    volume_24hr: float | None = None
    liquidity: float | None = None
    spread: float | None = None
    time_to_resolution_hours: float | None = None
    market_age_hours: float | None = None

    # Derived from our own accumulated market_snapshots history - NOT from
    # CLOB /prices-history (unverified for active markets as of Stage 2).
    snapshot_count: int = 0
    history_span_hours: float | None = None
    price_change_recent: float | None = None
    volume_change_recent: float | None = None

    # CLOB-dependent - not populated in Stage 2's first pass (pending live
    # verification of /book's response shape). Always None/False until that
    # follow-up lands; never guessed at.
    order_book_imbalance: float | None = None
    clob_data_available: bool = False

    data_quality: DataQualityState
    data_quality_reasons: list[str] = Field(default_factory=list)


class AnalysisRecord(BaseModel):
    market_id: str
    feature_id: int | None = None
    computed_at: datetime
    category: str | None = None

    # Preserved separately per the approved plan - estimated_probability
    # must never overwrite the raw market-implied value.
    market_implied_probability: float | None = None
    estimated_probability: float | None = None
    confidence: float | None = None
    adjustments: list[dict] = Field(default_factory=list)

    raw_edge: float | None = None
    adjusted_edge: float | None = None
    expected_value: float | None = None
    category_reliability: float | None = None

    data_quality: DataQualityState
    quality_gate_passed: bool
    quality_gate_reason: str | None = None

    # None when the quality gate failed before the risk gate ever ran.
    risk_gate_passed: bool | None = None
    risk_gate_reasons: list[str] = Field(default_factory=list)

    # None whenever either gate failed - scoring never runs for a gated-out
    # market, and a score can never override a gate failure.
    score: float | None = None
    classification: str
