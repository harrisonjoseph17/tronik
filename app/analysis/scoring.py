"""Transparent 0-100 trade score.

Only ever meaningful for a market that has already passed both gates -
scoring.py takes no gate-related inputs and has no way to force a
classification on its own; pipeline.py only calls it after risk_gate.py
passes, and classify.py treats a gate failure as final regardless of score.

Every component below is a simple, named, 0-1 normalized sub-score
multiplied by a configurable weight (weights sum to 100 by default), so the
final score is reproducible from the same Features/EstimateResult/EdgeResult
values stored in the analyses table.
"""

from __future__ import annotations

from app.analysis.edge import EdgeResult
from app.analysis.probability import EstimateResult
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState, Features

_QUALITY_COMPONENT = {
    DataQualityState.SUFFICIENT: 1.0,
    DataQualityState.PARTIAL: 0.5,
    DataQualityState.INSUFFICIENT: 0.0,
}


def compute_score(
    features: Features,
    estimate: EstimateResult,
    edge: EdgeResult,
    config: AnalysisSettings,
) -> float:
    edge_component = min(abs(edge.adjusted_edge) / config.score_edge_normalization, 1.0)
    confidence_component = max(0.0, min(estimate.confidence, 1.0))

    liquidity_anchor = config.risk_gate_min_liquidity * config.score_liquidity_normalization_multiplier
    liquidity_component = min((features.liquidity or 0.0) / liquidity_anchor, 1.0) if liquidity_anchor > 0 else 0.0

    quality_component = _QUALITY_COMPONENT[features.data_quality]
    asymmetric_bonus = 1.0 if edge.adjusted_edge >= config.asymmetric_edge_threshold else 0.0

    score = (
        config.score_weight_edge * edge_component
        + config.score_weight_confidence * confidence_component
        + config.score_weight_liquidity * liquidity_component
        + config.score_weight_data_quality * quality_component
        + config.score_weight_asymmetric_bonus * asymmetric_bonus
    )
    return round(max(0.0, min(100.0, score)), 2)
