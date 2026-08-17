"""Edge = the divergence between estimated_probability and the raw
market-implied probability.

`raw_edge` is a plain difference. `adjusted_edge` additionally discounts by
a configurable per-category reliability factor (Stage 2 has thinner
evidence for some categories than others) and subtracts a slippage
estimate, preserving sign - it never overshoots raw_edge in magnitude and
never flips its sign. `expected_value` is currently just `adjusted_edge`:
a full EV/bet-sizing calculation (payout structure, Kelly-style sizing,
etc.) is out of scope for Stage 2 - the master spec explicitly warns
against blindly using full Kelly, and real position sizing needs
paper-trading history to validate against, which doesn't exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.probability import EstimateResult
from app.config.loader import AnalysisSettings


@dataclass(frozen=True)
class EdgeResult:
    raw_edge: float
    adjusted_edge: float
    expected_value: float
    category_reliability: float


def compute_edge(estimate: EstimateResult, category: str, config: AnalysisSettings) -> EdgeResult:
    raw_edge = estimate.estimated_probability - estimate.market_implied_probability

    reliability = config.category_reliability.get(
        category, config.category_reliability.get("other", 0.3)
    )
    discounted = raw_edge * reliability

    if discounted > 0:
        adjusted_edge = max(0.0, discounted - config.slippage_estimate)
    elif discounted < 0:
        adjusted_edge = min(0.0, discounted + config.slippage_estimate)
    else:
        adjusted_edge = 0.0

    return EdgeResult(
        raw_edge=raw_edge,
        adjusted_edge=adjusted_edge,
        expected_value=adjusted_edge,
        category_reliability=reliability,
    )
