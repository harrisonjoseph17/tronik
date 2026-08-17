"""Baseline probability estimate.

Called `estimated_probability`, never "model probability" - that name is
reserved until calibration against real outcomes has been demonstrated,
which needs paper-trading history that doesn't exist yet (Stage 4+).

Starts from the raw market-implied probability (Market.mid_price, via
Features.market_implied_probability) and applies a small set of named,
bounded, configurable adjustments. Every adjustment is logged with its
magnitude and reasoning so the final estimate is reproducible from stored
feature values alone. The raw market-implied probability is always
preserved separately in the result - estimate_probability() never
overwrites it, only derives from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.loader import AnalysisSettings
from app.storage.analysis_models import DataQualityState, Features


@dataclass(frozen=True)
class Adjustment:
    name: str
    value: float
    reason: str

    def as_dict(self) -> dict:
        return {"name": self.name, "value": round(self.value, 6), "reason": self.reason}


@dataclass(frozen=True)
class EstimateResult:
    market_implied_probability: float
    estimated_probability: float
    confidence: float
    adjustments: list[Adjustment] = field(default_factory=list)
    # The actual (capped) adjustment applied to reach estimated_probability -
    # may differ from sum(a.value for a in adjustments), which is the raw,
    # uncapped total, kept for auditability.
    applied_adjustment: float = 0.0
    adjustment_capped: bool = False


def estimate_probability(features: Features, config: AnalysisSettings) -> EstimateResult:
    if features.market_implied_probability is None:
        raise ValueError(
            "cannot estimate probability without market_implied_probability - "
            "the quality gate should have already excluded this market"
        )

    adjustments: list[Adjustment] = []

    if features.price_change_recent is not None and features.history_span_hours:
        raw_value = features.price_change_recent * config.momentum_adjustment_weight
        adjustments.append(
            Adjustment(
                name="momentum",
                value=raw_value,
                reason=(
                    f"recent price change {features.price_change_recent:+.4f} over "
                    f"{features.history_span_hours:.1f}h, weight={config.momentum_adjustment_weight}"
                ),
            )
        )

    raw_total = sum(a.value for a in adjustments)
    cap = (
        config.partial_data_max_adjustment
        if features.data_quality == DataQualityState.PARTIAL
        else config.max_probability_adjustment
    )
    capped = abs(raw_total) > cap
    applied_adjustment = max(-cap, min(cap, raw_total))

    estimated = features.market_implied_probability + applied_adjustment
    estimated = max(0.0, min(1.0, estimated))

    return EstimateResult(
        market_implied_probability=features.market_implied_probability,
        estimated_probability=estimated,
        confidence=_compute_confidence(features, config),
        adjustments=adjustments,
        applied_adjustment=applied_adjustment,
        adjustment_capped=capped,
    )


def _compute_confidence(features: Features, config: AnalysisSettings) -> float:
    quality_component = {
        DataQualityState.SUFFICIENT: 1.0,
        DataQualityState.PARTIAL: 0.5,
        DataQualityState.INSUFFICIENT: 0.0,
    }[features.data_quality]

    history_baseline = max(config.min_snapshots_for_history * 3, 1)
    history_component = min(features.snapshot_count / history_baseline, 1.0)

    confidence = 0.6 * quality_component + 0.4 * history_component
    return round(max(0.0, min(1.0, confidence)), 4)
