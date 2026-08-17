"""Polymarket-native feature computation.

Everything here is derived from a Market row (Stage 1's Gamma-sourced
normalization) plus its own market_snapshots history - no CLOB order-book
data and no external (non-Polymarket) data sources. order_book_imbalance and
clob_data_available are always None/False in this pass: CLOB's /book
response shape was unverified at the time this was written, and the
approved plan requires verifying live API assumptions before coding against
them rather than inventing an interface. They're wired into Features now so
the schema doesn't need to change when that follow-up lands.

Emits a DATA_SUFFICIENT/DATA_PARTIAL/DATA_INSUFFICIENT tri-state alongside
the raw values - quality_gate.py makes the actual gating decision from it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState, Features
from app.storage.models import Market

_INSUFFICIENT_TRIGGERS = frozenset(
    {"no_market_price", "no_liquidity_data", "insufficient_snapshot_count", "insufficient_history_span"}
)


def _hours_between(start: datetime, end: datetime) -> float:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - start).total_seconds() / 3600.0


def compute_features(
    market: Market,
    snapshots: list[dict],
    *,
    now: datetime,
    config: AnalysisSettings,
) -> Features:
    """snapshots must be most-recent-first (MarketRepository.list_snapshots' order)."""
    time_to_resolution_hours = _hours_between(now, market.end_date) if market.end_date else None
    market_age_hours = _hours_between(market.start_date, now) if market.start_date else None

    snapshot_count = len(snapshots)
    history_span_hours: float | None = None
    price_change_recent: float | None = None
    volume_change_recent: float | None = None

    if snapshots:
        newest, oldest = snapshots[0], snapshots[-1]
        newest_ts = datetime.fromisoformat(newest["captured_at"])
        oldest_ts = datetime.fromisoformat(oldest["captured_at"])
        history_span_hours = max(_hours_between(oldest_ts, newest_ts), 0.0)
        if newest.get("mid_price") is not None and oldest.get("mid_price") is not None:
            price_change_recent = newest["mid_price"] - oldest["mid_price"]
        if newest.get("volume") is not None and oldest.get("volume") is not None:
            volume_change_recent = newest["volume"] - oldest["volume"]

    reasons: list[str] = []
    if market.mid_price is None:
        reasons.append("no_market_price")
    if market.liquidity is None:
        reasons.append("no_liquidity_data")
    if snapshot_count < config.min_snapshots_for_history:
        reasons.append("insufficient_snapshot_count")
    if history_span_hours is None or history_span_hours < config.min_history_hours:
        reasons.append("insufficient_history_span")

    if any(reason in _INSUFFICIENT_TRIGGERS for reason in reasons):
        data_quality = DataQualityState.INSUFFICIENT
    else:
        if market.volume_24hr is None:
            reasons.append("no_24h_volume")
        if market.spread is None:
            reasons.append("no_computed_spread")
        data_quality = DataQualityState.PARTIAL if reasons else DataQualityState.SUFFICIENT

    return Features(
        market_id=market.id,
        computed_at=now,
        market_implied_probability=market.mid_price,
        volume=market.volume,
        volume_24hr=market.volume_24hr,
        liquidity=market.liquidity,
        spread=market.spread,
        time_to_resolution_hours=time_to_resolution_hours,
        market_age_hours=market_age_hours,
        snapshot_count=snapshot_count,
        history_span_hours=history_span_hours,
        price_change_recent=price_change_recent,
        volume_change_recent=volume_change_recent,
        order_book_imbalance=None,
        clob_data_available=False,
        data_quality=data_quality,
        data_quality_reasons=reasons,
    )
