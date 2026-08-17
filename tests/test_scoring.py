from datetime import datetime, timezone

from app.analysis.edge import EdgeResult
from app.analysis.probability import EstimateResult
from app.analysis.scoring import compute_score
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState, Features

CONFIG = AnalysisSettings(
    score_weight_edge=35.0,
    score_weight_confidence=20.0,
    score_weight_liquidity=15.0,
    score_weight_data_quality=15.0,
    score_weight_asymmetric_bonus=15.0,
    score_edge_normalization=0.30,
    risk_gate_min_liquidity=2000.0,
    score_liquidity_normalization_multiplier=5.0,
    asymmetric_edge_threshold=0.25,
)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _features(**overrides) -> Features:
    defaults = dict(
        market_id="m1", computed_at=NOW, liquidity=10000.0, data_quality=DataQualityState.SUFFICIENT
    )
    defaults.update(overrides)
    return Features(**defaults)


def _estimate(confidence=0.8) -> EstimateResult:
    return EstimateResult(market_implied_probability=0.5, estimated_probability=0.6, confidence=confidence)


def _edge(adjusted_edge=0.1) -> EdgeResult:
    return EdgeResult(
        raw_edge=adjusted_edge, adjusted_edge=adjusted_edge, expected_value=adjusted_edge, category_reliability=0.8
    )


def test_max_score_when_everything_maxed_out():
    score = compute_score(
        _features(liquidity=1_000_000), _estimate(confidence=1.0), _edge(0.5), CONFIG
    )
    assert score == 100.0


def test_zero_score_when_everything_minimal():
    score = compute_score(
        _features(liquidity=0.0, data_quality=DataQualityState.INSUFFICIENT),
        _estimate(confidence=0.0),
        _edge(0.0),
        CONFIG,
    )
    assert score == 0.0


def test_score_bounded_to_100_even_with_extreme_inputs():
    score = compute_score(
        _features(liquidity=999_999_999), _estimate(confidence=5.0), _edge(10.0), CONFIG
    )
    assert score <= 100.0


def test_higher_edge_yields_higher_score_all_else_equal():
    low = compute_score(_features(), _estimate(), _edge(0.02), CONFIG)
    high = compute_score(_features(), _estimate(), _edge(0.20), CONFIG)
    assert high > low


def test_higher_liquidity_yields_higher_score_all_else_equal():
    low = compute_score(_features(liquidity=100.0), _estimate(), _edge(), CONFIG)
    high = compute_score(_features(liquidity=100000.0), _estimate(), _edge(), CONFIG)
    assert high > low


def test_partial_data_quality_scores_lower_than_sufficient():
    sufficient = compute_score(
        _features(data_quality=DataQualityState.SUFFICIENT), _estimate(), _edge(), CONFIG
    )
    partial = compute_score(
        _features(data_quality=DataQualityState.PARTIAL), _estimate(), _edge(), CONFIG
    )
    assert sufficient > partial


def test_asymmetric_bonus_applies_only_above_threshold():
    below = compute_score(_features(), _estimate(), _edge(0.24), CONFIG)
    above = compute_score(_features(), _estimate(), _edge(0.26), CONFIG)
    # crossing the threshold adds the full asymmetric-bonus weight on top of
    # the small edge-component difference between 0.24 and 0.26
    assert above - below > 10.0
