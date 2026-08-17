from datetime import datetime, timezone

import pytest

from app.analysis.probability import estimate_probability
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState, Features

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = AnalysisSettings(
    momentum_adjustment_weight=0.3,
    max_probability_adjustment=0.15,
    partial_data_max_adjustment=0.05,
    min_snapshots_for_history=3,
)


def _features(**overrides) -> Features:
    defaults = dict(
        market_id="m1",
        computed_at=NOW,
        market_implied_probability=0.5,
        snapshot_count=9,
        history_span_hours=6.0,
        price_change_recent=0.02,
        data_quality=DataQualityState.SUFFICIENT,
    )
    defaults.update(overrides)
    return Features(**defaults)


def test_raw_market_implied_probability_preserved_separately():
    result = estimate_probability(_features(), CONFIG)
    assert result.market_implied_probability == 0.5
    assert result.estimated_probability != result.market_implied_probability or result.applied_adjustment == 0


def test_momentum_adjustment_applied_and_bounded_from_market_price():
    result = estimate_probability(_features(price_change_recent=0.02), CONFIG)
    expected_raw = 0.02 * 0.3  # 0.006, well under the 0.15 cap
    assert result.applied_adjustment == pytest.approx(expected_raw)
    assert result.estimated_probability == pytest.approx(0.5 + expected_raw)
    assert result.adjustment_capped is False


def test_large_momentum_gets_capped_at_max_probability_adjustment():
    result = estimate_probability(_features(price_change_recent=0.9), CONFIG)
    assert result.adjustment_capped is True
    assert result.applied_adjustment == pytest.approx(0.15)
    assert result.estimated_probability == pytest.approx(0.65)


def test_negative_momentum_capped_in_negative_direction():
    result = estimate_probability(_features(price_change_recent=-0.9), CONFIG)
    assert result.adjustment_capped is True
    assert result.applied_adjustment == pytest.approx(-0.15)


def test_partial_data_uses_tighter_cap():
    result = estimate_probability(
        _features(price_change_recent=0.9, data_quality=DataQualityState.PARTIAL), CONFIG
    )
    assert result.applied_adjustment == pytest.approx(0.05)


def test_estimated_probability_bounded_to_zero_one():
    result = estimate_probability(
        _features(market_implied_probability=0.98, price_change_recent=0.9), CONFIG
    )
    assert result.estimated_probability <= 1.0


def test_no_momentum_data_means_no_adjustment():
    result = estimate_probability(_features(price_change_recent=None), CONFIG)
    assert result.adjustments == []
    assert result.applied_adjustment == 0.0
    assert result.estimated_probability == 0.5


def test_raises_when_market_implied_probability_missing():
    with pytest.raises(ValueError):
        estimate_probability(_features(market_implied_probability=None), CONFIG)


def test_adjustments_list_holds_raw_uncapped_named_value():
    result = estimate_probability(_features(price_change_recent=0.9), CONFIG)
    assert len(result.adjustments) == 1
    assert result.adjustments[0].name == "momentum"
    assert result.adjustments[0].value == pytest.approx(0.9 * 0.3)  # raw, not capped
    assert result.adjustments[0].as_dict()["name"] == "momentum"


def test_confidence_higher_for_sufficient_than_insufficient_quality():
    sufficient = estimate_probability(_features(data_quality=DataQualityState.SUFFICIENT), CONFIG)
    partial = estimate_probability(_features(data_quality=DataQualityState.PARTIAL), CONFIG)
    assert sufficient.confidence > partial.confidence


def test_confidence_scales_with_snapshot_count():
    few = estimate_probability(_features(snapshot_count=1), CONFIG)
    many = estimate_probability(_features(snapshot_count=100), CONFIG)
    assert many.confidence >= few.confidence


def test_confidence_bounded_zero_to_one():
    result = estimate_probability(_features(snapshot_count=100000), CONFIG)
    assert 0.0 <= result.confidence <= 1.0
