"""Final classification - COMPOUND / ASYMMETRIC / WATCH / NO_TRADE / AVOID.

A gate failure is always final: quality_gate.py and risk_gate.py results are
checked first, and no score can override them. AVOID is reserved for
structural/data-quality failures (the quality gate); NO_TRADE covers every
other gate failure and any market that passes both gates but simply doesn't
score high enough. COMPOUND/ASYMMETRIC/WATCH only ever apply to markets that
passed both gates.
"""

from __future__ import annotations

from enum import Enum

from app.analysis.edge import EdgeResult
from app.analysis.quality_gate import QualityGateResult
from app.analysis.risk_gate import RiskGateResult
from app.config.loader import AnalysisSettings


class Classification(str, Enum):
    COMPOUND = "COMPOUND"
    ASYMMETRIC = "ASYMMETRIC"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"
    AVOID = "AVOID"


def classify(
    quality_gate: QualityGateResult,
    risk_gate: RiskGateResult | None,
    score: float | None,
    edge: EdgeResult | None,
    config: AnalysisSettings,
) -> Classification:
    if not quality_gate.passed:
        return Classification.AVOID

    if risk_gate is None or not risk_gate.passed:
        return Classification.NO_TRADE

    if score is None or edge is None:
        # Defensive only - the pipeline always computes both once the risk
        # gate has passed, so this branch should be unreachable in practice.
        return Classification.NO_TRADE

    if edge.adjusted_edge >= config.asymmetric_edge_threshold:
        return Classification.ASYMMETRIC
    if score >= config.compound_min_score:
        return Classification.COMPOUND
    if score >= config.watch_min_score:
        return Classification.WATCH
    return Classification.NO_TRADE
