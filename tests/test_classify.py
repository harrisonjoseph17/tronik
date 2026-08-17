from app.analysis.classify import Classification, classify
from app.analysis.edge import EdgeResult
from app.analysis.quality_gate import QualityGateResult
from app.analysis.risk_gate import RiskGateResult
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState

CONFIG = AnalysisSettings(compound_min_score=60.0, watch_min_score=35.0, asymmetric_edge_threshold=0.25)


def _edge(adjusted_edge=0.1) -> EdgeResult:
    return EdgeResult(
        raw_edge=adjusted_edge, adjusted_edge=adjusted_edge, expected_value=adjusted_edge, category_reliability=0.8
    )


def test_quality_gate_failure_is_avoid_regardless_of_everything_else():
    quality = QualityGateResult(passed=False, data_quality=DataQualityState.INSUFFICIENT, reason="insufficient_data")
    result = classify(quality, None, None, None, CONFIG)
    assert result == Classification.AVOID


def test_quality_gate_failure_is_avoid_even_with_a_hypothetically_high_score():
    # There's no legitimate way to reach this state (the pipeline never
    # computes a score after a quality-gate failure), but classify() must
    # not be fooled by one even if a caller passed one in.
    quality = QualityGateResult(passed=False, data_quality=DataQualityState.INSUFFICIENT, reason="insufficient_data")
    risk = RiskGateResult(passed=True, reasons=[])
    result = classify(quality, risk, 99.0, _edge(0.5), CONFIG)
    assert result == Classification.AVOID


def test_risk_gate_failure_is_no_trade_regardless_of_score():
    quality = QualityGateResult(passed=True, data_quality=DataQualityState.SUFFICIENT, reason=None)
    risk = RiskGateResult(passed=False, reasons=["wide_spread"])
    result = classify(quality, risk, 95.0, _edge(0.5), CONFIG)
    assert result == Classification.NO_TRADE


def test_high_edge_classifies_asymmetric():
    quality = QualityGateResult(passed=True, data_quality=DataQualityState.SUFFICIENT, reason=None)
    risk = RiskGateResult(passed=True, reasons=[])
    result = classify(quality, risk, 50.0, _edge(0.30), CONFIG)
    assert result == Classification.ASYMMETRIC


def test_high_score_below_asymmetric_threshold_classifies_compound():
    quality = QualityGateResult(passed=True, data_quality=DataQualityState.SUFFICIENT, reason=None)
    risk = RiskGateResult(passed=True, reasons=[])
    result = classify(quality, risk, 70.0, _edge(0.10), CONFIG)
    assert result == Classification.COMPOUND


def test_moderate_score_classifies_watch():
    quality = QualityGateResult(passed=True, data_quality=DataQualityState.SUFFICIENT, reason=None)
    risk = RiskGateResult(passed=True, reasons=[])
    result = classify(quality, risk, 40.0, _edge(0.06), CONFIG)
    assert result == Classification.WATCH


def test_low_score_classifies_no_trade():
    quality = QualityGateResult(passed=True, data_quality=DataQualityState.SUFFICIENT, reason=None)
    risk = RiskGateResult(passed=True, reasons=[])
    result = classify(quality, risk, 10.0, _edge(0.06), CONFIG)
    assert result == Classification.NO_TRADE
