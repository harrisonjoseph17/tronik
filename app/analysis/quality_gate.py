"""Data quality gate - the first of the pipeline's two gates.

Runs immediately after features.py, before any probability estimate is
computed. A market with DATA_INSUFFICIENT quality never reaches
probability.py/edge.py at all - there is no meaningful estimate to make
without enough price/liquidity/history data, so the pipeline short-circuits
straight to a gated-out result (classification AVOID) instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.storage.analysis_models import DataQualityState, Features


@dataclass(frozen=True)
class QualityGateResult:
    passed: bool
    data_quality: DataQualityState
    reason: str | None


def evaluate_quality_gate(features: Features) -> QualityGateResult:
    if features.data_quality == DataQualityState.INSUFFICIENT:
        return QualityGateResult(
            passed=False,
            data_quality=features.data_quality,
            reason="insufficient_data",
        )
    return QualityGateResult(passed=True, data_quality=features.data_quality, reason=None)
