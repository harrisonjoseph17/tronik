from app.analysis.edge import EdgeResult
from app.analysis.risk_gate import (
    REASON_CLOB_UNAVAILABLE,
    REASON_INSUFFICIENT_EDGE,
    REASON_LIQUIDITY_UNAVAILABLE,
    REASON_LOW_LIQUIDITY,
    REASON_NEAR_RESOLUTION,
    REASON_RESOLUTION_UNKNOWN,
    REASON_SPREAD_UNAVAILABLE,
    REASON_WIDE_SPREAD,
    evaluate_risk_gate,
)
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState, Features

CONFIG = AnalysisSettings(
    risk_gate_max_spread=0.08,
    risk_gate_min_liquidity=2000.0,
    risk_gate_min_hours_to_resolution=4.0,
    risk_gate_min_edge=0.05,
)


def _features(**overrides) -> Features:
    defaults = dict(
        market_id="m1",
        computed_at=None,
        spread=0.03,
        liquidity=5000.0,
        time_to_resolution_hours=24.0,
        data_quality=DataQualityState.SUFFICIENT,
        clob_data_available=True,
    )
    defaults.update(overrides)
    from datetime import datetime, timezone

    defaults["computed_at"] = defaults["computed_at"] or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Features(**defaults)


def _edge(adjusted_edge=0.10) -> EdgeResult:
    return EdgeResult(
        raw_edge=adjusted_edge, adjusted_edge=adjusted_edge, expected_value=adjusted_edge, category_reliability=0.8
    )


def test_passes_when_all_conditions_healthy():
    result = evaluate_risk_gate(_features(), _edge(), CONFIG)
    assert result.passed is True
    assert result.reasons == []


def test_fails_on_wide_spread():
    result = evaluate_risk_gate(_features(spread=0.15), _edge(), CONFIG)
    assert result.passed is False
    assert REASON_WIDE_SPREAD in result.reasons


def test_fails_on_missing_spread():
    result = evaluate_risk_gate(_features(spread=None), _edge(), CONFIG)
    assert REASON_SPREAD_UNAVAILABLE in result.reasons


def test_fails_on_low_liquidity():
    result = evaluate_risk_gate(_features(liquidity=100.0), _edge(), CONFIG)
    assert REASON_LOW_LIQUIDITY in result.reasons


def test_fails_on_missing_liquidity():
    result = evaluate_risk_gate(_features(liquidity=None), _edge(), CONFIG)
    assert REASON_LIQUIDITY_UNAVAILABLE in result.reasons


def test_fails_on_near_resolution():
    result = evaluate_risk_gate(_features(time_to_resolution_hours=1.0), _edge(), CONFIG)
    assert REASON_NEAR_RESOLUTION in result.reasons


def test_fails_on_unknown_resolution_date():
    result = evaluate_risk_gate(_features(time_to_resolution_hours=None), _edge(), CONFIG)
    assert REASON_RESOLUTION_UNKNOWN in result.reasons


def test_fails_on_insufficient_edge():
    result = evaluate_risk_gate(_features(), _edge(0.01), CONFIG)
    assert REASON_INSUFFICIENT_EDGE in result.reasons


def test_negative_edge_magnitude_also_checked():
    result = evaluate_risk_gate(_features(), _edge(-0.01), CONFIG)
    assert REASON_INSUFFICIENT_EDGE in result.reasons


def test_multiple_failures_all_reported():
    result = evaluate_risk_gate(_features(spread=0.5, liquidity=10.0), _edge(0.0), CONFIG)
    assert result.passed is False
    assert set(result.reasons) == {REASON_WIDE_SPREAD, REASON_LOW_LIQUIDITY, REASON_INSUFFICIENT_EDGE}


def test_fails_when_clob_data_unavailable():
    result = evaluate_risk_gate(_features(clob_data_available=False), _edge(), CONFIG)
    assert result.passed is False
    assert REASON_CLOB_UNAVAILABLE in result.reasons


def test_passes_when_clob_data_available_and_everything_else_healthy():
    result = evaluate_risk_gate(_features(clob_data_available=True), _edge(), CONFIG)
    assert result.passed is True


def test_high_score_cannot_be_represented_here_gate_is_score_independent():
    # The gate takes no score parameter at all - there is nothing a caller
    # could pass in to override a failing condition.
    import inspect

    params = inspect.signature(evaluate_risk_gate).parameters
    assert "score" not in params
