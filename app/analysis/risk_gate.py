"""Hard risk/data gate - runs after edge calculation, before scoring.

Per the approved plan: failing ANY condition here forces the market to
NO_TRADE (or AVOID, decided by classify.py) and skips scoring.py entirely -
a high score can never override this gate, because scoring never runs for
a market that fails it.

"Inadequate data quality" and "insufficient historical data" are already
enforced one stage earlier by quality_gate.py (a DATA_INSUFFICIENT market
never reaches this point at all - see features.py) - this gate only
re-checks conditions that depend on values computed after that point (edge)
or that are independent of data quality (spread, liquidity,
time-to-resolution).

"Stale/unavailable CLOB data": pipeline.py fetches a live CLOB order book
(app/analysis/order_book.py) for every market that passes the quality gate
and sets Features.clob_data_available accordingly before this gate runs.
A live probe confirmed /book returns 404 for markets with no current live
orders even when Gamma reports them as active/order-book-enabled (e.g. a
paused esports sub-market) - so "unavailable" is a real, expected signal,
not a broken endpoint, and is checked below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.edge import EdgeResult
from app.config.loader import AnalysisSettings
from app.storage.analysis_models import Features

REASON_WIDE_SPREAD = "wide_spread"
REASON_SPREAD_UNAVAILABLE = "spread_unavailable"
REASON_LOW_LIQUIDITY = "low_liquidity"
REASON_LIQUIDITY_UNAVAILABLE = "liquidity_unavailable"
REASON_NEAR_RESOLUTION = "near_resolution"
REASON_RESOLUTION_UNKNOWN = "resolution_date_unknown"
REASON_INSUFFICIENT_EDGE = "insufficient_edge"
REASON_CLOB_UNAVAILABLE = "clob_unavailable"


@dataclass(frozen=True)
class RiskGateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_risk_gate(features: Features, edge: EdgeResult, config: AnalysisSettings) -> RiskGateResult:
    reasons: list[str] = []

    if features.spread is None:
        reasons.append(REASON_SPREAD_UNAVAILABLE)
    elif features.spread > config.risk_gate_max_spread:
        reasons.append(REASON_WIDE_SPREAD)

    if features.liquidity is None:
        reasons.append(REASON_LIQUIDITY_UNAVAILABLE)
    elif features.liquidity < config.risk_gate_min_liquidity:
        reasons.append(REASON_LOW_LIQUIDITY)

    if features.time_to_resolution_hours is None:
        reasons.append(REASON_RESOLUTION_UNKNOWN)
    elif features.time_to_resolution_hours < config.risk_gate_min_hours_to_resolution:
        reasons.append(REASON_NEAR_RESOLUTION)

    if abs(edge.adjusted_edge) < config.risk_gate_min_edge:
        reasons.append(REASON_INSUFFICIENT_EDGE)

    if not features.clob_data_available:
        reasons.append(REASON_CLOB_UNAVAILABLE)

    return RiskGateResult(passed=len(reasons) == 0, reasons=reasons)
