from datetime import datetime, timezone

from app.analysis.quality_gate import evaluate_quality_gate
from app.storage.analysis_models import DataQualityState, Features


def _features(data_quality: DataQualityState) -> Features:
    return Features(
        market_id="m1",
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        data_quality=data_quality,
    )


def test_passes_when_sufficient():
    result = evaluate_quality_gate(_features(DataQualityState.SUFFICIENT))
    assert result.passed is True
    assert result.reason is None


def test_passes_when_partial():
    result = evaluate_quality_gate(_features(DataQualityState.PARTIAL))
    assert result.passed is True
    assert result.reason is None


def test_fails_when_insufficient():
    result = evaluate_quality_gate(_features(DataQualityState.INSUFFICIENT))
    assert result.passed is False
    assert result.reason == "insufficient_data"
    assert result.data_quality == DataQualityState.INSUFFICIENT
