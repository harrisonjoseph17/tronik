from datetime import datetime, timedelta, timezone

from app.analysis.features import compute_features
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState
from app.storage.models import Market

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
CONFIG = AnalysisSettings(min_snapshots_for_history=3, min_history_hours=1.0)


def _market(**overrides) -> Market:
    defaults = dict(
        id="m1",
        question="Will BTC be up?",
        category="crypto",
        subcategory="btc_updown",
        mid_price=0.55,
        liquidity=5000.0,
        volume=10000.0,
        volume_24hr=2000.0,
        spread=0.02,
        start_date=NOW - timedelta(days=5),
        end_date=NOW + timedelta(hours=48),
    )
    defaults.update(overrides)
    return Market(**defaults)


def _snapshots(count: int, span_hours: float, *, mid_start=0.50, mid_end=0.55) -> list[dict]:
    """Most-recent-first, matching MarketRepository.list_snapshots ordering."""
    if count == 0:
        return []
    step = span_hours / max(count - 1, 1)
    snapshots = []
    for i in range(count):
        hours_ago = span_hours - i * step
        ts = NOW - timedelta(hours=hours_ago)
        frac = i / max(count - 1, 1)
        mid = mid_start + (mid_end - mid_start) * frac
        snapshots.append({"captured_at": ts.isoformat(), "mid_price": mid, "volume": 1000.0 + i * 10})
    return list(reversed(snapshots))  # newest first


def test_sufficient_data_quality_with_enough_history():
    features = compute_features(_market(), _snapshots(5, 3.0), now=NOW, config=CONFIG)
    assert features.data_quality == DataQualityState.SUFFICIENT
    assert features.data_quality_reasons == []


def test_insufficient_when_no_market_price():
    features = compute_features(_market(mid_price=None), _snapshots(5, 3.0), now=NOW, config=CONFIG)
    assert features.data_quality == DataQualityState.INSUFFICIENT
    assert "no_market_price" in features.data_quality_reasons


def test_insufficient_when_no_liquidity():
    features = compute_features(_market(liquidity=None), _snapshots(5, 3.0), now=NOW, config=CONFIG)
    assert features.data_quality == DataQualityState.INSUFFICIENT
    assert "no_liquidity_data" in features.data_quality_reasons


def test_insufficient_when_too_few_snapshots():
    features = compute_features(_market(), _snapshots(1, 3.0), now=NOW, config=CONFIG)
    assert features.data_quality == DataQualityState.INSUFFICIENT
    assert "insufficient_snapshot_count" in features.data_quality_reasons


def test_insufficient_when_history_span_too_short():
    features = compute_features(_market(), _snapshots(5, 0.2), now=NOW, config=CONFIG)
    assert features.data_quality == DataQualityState.INSUFFICIENT
    assert "insufficient_history_span" in features.data_quality_reasons


def test_insufficient_when_no_snapshots_at_all():
    features = compute_features(_market(), [], now=NOW, config=CONFIG)
    assert features.data_quality == DataQualityState.INSUFFICIENT
    assert features.snapshot_count == 0
    assert features.history_span_hours is None


def test_partial_when_optional_fields_missing():
    features = compute_features(
        _market(volume_24hr=None), _snapshots(5, 3.0), now=NOW, config=CONFIG
    )
    assert features.data_quality == DataQualityState.PARTIAL
    assert "no_24h_volume" in features.data_quality_reasons


def test_price_change_recent_computed_from_snapshot_span():
    features = compute_features(
        _market(), _snapshots(5, 3.0, mid_start=0.40, mid_end=0.55), now=NOW, config=CONFIG
    )
    assert features.price_change_recent is not None
    assert round(features.price_change_recent, 4) == 0.15


def test_time_to_resolution_and_market_age():
    features = compute_features(_market(), _snapshots(5, 3.0), now=NOW, config=CONFIG)
    assert features.time_to_resolution_hours == 48.0
    assert features.market_age_hours == 120.0  # 5 days


def test_missing_dates_yield_none_time_fields():
    features = compute_features(
        _market(start_date=None, end_date=None), _snapshots(5, 3.0), now=NOW, config=CONFIG
    )
    assert features.time_to_resolution_hours is None
    assert features.market_age_hours is None


def test_order_book_fields_are_not_populated_yet():
    features = compute_features(_market(), _snapshots(5, 3.0), now=NOW, config=CONFIG)
    assert features.order_book_imbalance is None
    assert features.clob_data_available is False
