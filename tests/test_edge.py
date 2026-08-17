import pytest

from app.analysis.edge import compute_edge
from app.analysis.probability import EstimateResult
from app.config.loader import AnalysisSettings

CONFIG = AnalysisSettings(
    category_reliability={"politics": 0.7, "crypto": 0.8, "sports": 0.6, "other": 0.3},
    slippage_estimate=0.01,
)


def _estimate(market_implied=0.5, estimated=0.6) -> EstimateResult:
    return EstimateResult(
        market_implied_probability=market_implied,
        estimated_probability=estimated,
        confidence=0.7,
    )


def test_raw_edge_is_plain_difference():
    result = compute_edge(_estimate(0.5, 0.6), "crypto", CONFIG)
    assert result.raw_edge == pytest.approx(0.1)


def test_category_reliability_applied():
    result = compute_edge(_estimate(0.5, 0.6), "crypto", CONFIG)
    # 0.1 * 0.8 = 0.08, minus slippage 0.01 = 0.07
    assert result.adjusted_edge == pytest.approx(0.07)
    assert result.category_reliability == 0.8


def test_unknown_category_falls_back_to_other():
    result = compute_edge(_estimate(0.5, 0.6), "esports", CONFIG)
    assert result.category_reliability == 0.3


def test_negative_edge_preserves_sign_after_slippage():
    result = compute_edge(_estimate(0.6, 0.5), "crypto", CONFIG)
    # raw edge -0.1 * 0.8 = -0.08, plus slippage 0.01 = -0.07 (still negative)
    assert result.adjusted_edge == pytest.approx(-0.07)
    assert result.adjusted_edge < 0


def test_slippage_cannot_flip_sign_when_it_exceeds_discounted_edge():
    tiny_edge_config = AnalysisSettings(
        category_reliability={"crypto": 0.1}, slippage_estimate=0.5
    )
    result = compute_edge(_estimate(0.5, 0.51), "crypto", tiny_edge_config)
    # discounted = 0.01 * 0.1 = 0.001, slippage 0.5 would overshoot past zero
    assert result.adjusted_edge == 0.0


def test_zero_edge_stays_zero():
    result = compute_edge(_estimate(0.5, 0.5), "crypto", CONFIG)
    assert result.raw_edge == 0.0
    assert result.adjusted_edge == 0.0


def test_expected_value_equals_adjusted_edge():
    result = compute_edge(_estimate(0.5, 0.6), "crypto", CONFIG)
    assert result.expected_value == result.adjusted_edge
