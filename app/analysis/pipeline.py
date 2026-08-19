"""Stage 2 pipeline orchestrator.

DISCOVERY -> MARKET STATE -> FEATURES -> DATA QUALITY GATE -> BASELINE
ESTIMATE -> EDGE -> HARD RISK GATE -> TRADE SCORE -> CLASSIFICATION

Discovery and market-state persistence are Stage 1's scanner.py - this
module reads what's already in markets/market_snapshots (the "included"
subset: filter_reason IS NULL, i.e. markets Stage 1's filters didn't
exclude) for the configured (category, subcategory) target universe
(config.target_subcategories - see app/config/loader.py), and writes one
features row plus one analyses row per market analyzed. This target-universe
filter is a DB query (list_markets(category=, subcategory=, ...)), so a
market outside the target universe never gets loaded in the first place -
it costs nothing beyond what Stage 1 already spent on it.

A live CLOB order-book fetch (app/analysis/order_book.py) happens for every
market that passes the data quality gate - not before, so markets that
were never going to qualify don't cost a CLOB call - which is why this
module is async, unlike Stage 1's fully-synchronous storage-only modules.

Selective LLM review is the next stage after classification and is not
implemented here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.analysis.classify import classify
from app.analysis.edge import compute_edge
from app.analysis.features import compute_features
from app.analysis.order_book import SupportsGetBook, fetch_order_book_snapshot
from app.analysis.probability import estimate_probability
from app.analysis.quality_gate import evaluate_quality_gate
from app.analysis.risk_gate import evaluate_risk_gate
from app.analysis.scoring import compute_score
from app.config.loader import AppConfig
from app.polymarket.client import CLOBClient
from app.storage.analysis_models import AnalysisRecord
from app.storage.database import Database
from app.storage.repositories import AnalysisRepository, FeatureRepository, MarketRepository

logger = logging.getLogger(__name__)


@dataclass
class AnalysisSummary:
    markets_considered: int = 0
    by_target_category: dict[str, int] = field(default_factory=dict)
    quality_gate_failed: int = 0
    risk_gate_failed: int = 0
    scored: int = 0
    classification_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


async def run_analysis_once(
    config: AppConfig, db: Database, *, clob: SupportsGetBook | None = None
) -> AnalysisSummary:
    start = time.monotonic()
    summary = AnalysisSummary()
    now = datetime.now(timezone.utc)

    owns_clob = clob is None
    if clob is None:
        clob = CLOBClient(
            timeout=config.filters.request_timeout_seconds,
            max_retries=config.filters.max_retries,
            rate_limit_per_10s=config.filters.clob_rate_limit_per_10s,
        )

    market_repo = MarketRepository(db)
    feature_repo = FeatureRepository(db)
    analysis_repo = AnalysisRepository(db)
    try:
        candidates: list = []
        for category, subcategories in config.target_subcategories.items():
            for subcategory in sorted(subcategories):
                matches = market_repo.list_markets(
                    category=category,
                    subcategory=subcategory,
                    included_only=True,
                    limit=config.analysis.max_markets_per_category,
                )
                candidates.extend(matches)
                summary.by_target_category[f"{category}/{subcategory}"] = len(matches)
        summary.markets_considered = len(candidates)

        for market in candidates:
            snapshots = market_repo.list_snapshots(market.id, limit=50)
            features = compute_features(market, snapshots, now=now, config=config.analysis)
            quality_result = evaluate_quality_gate(features)

            if not quality_result.passed:
                summary.quality_gate_failed += 1
                classification = classify(quality_result, None, None, None, config.analysis)
                feature_id = feature_repo.insert(features)
                analysis_repo.insert(
                    AnalysisRecord(
                        market_id=market.id,
                        feature_id=feature_id,
                        computed_at=now,
                        category=market.category,
                        market_implied_probability=features.market_implied_probability,
                        data_quality=features.data_quality,
                        quality_gate_passed=False,
                        quality_gate_reason=quality_result.reason,
                        classification=classification.value,
                    )
                )
                _tally(summary, classification.value)
                continue

            if market.clob_token_ids:
                book = await fetch_order_book_snapshot(clob, market.clob_token_ids[0])
                features.order_book_imbalance = book.imbalance
                features.clob_data_available = book.available

            estimate = estimate_probability(features, config.analysis)
            edge_result = compute_edge(estimate, market.category, config.analysis)
            risk_result = evaluate_risk_gate(features, edge_result, config.analysis)

            score = None
            if risk_result.passed:
                score = compute_score(features, estimate, edge_result, config.analysis)
                summary.scored += 1
            else:
                summary.risk_gate_failed += 1

            classification = classify(quality_result, risk_result, score, edge_result, config.analysis)

            feature_id = feature_repo.insert(features)
            analysis_repo.insert(
                AnalysisRecord(
                    market_id=market.id,
                    feature_id=feature_id,
                    computed_at=now,
                    category=market.category,
                    market_implied_probability=estimate.market_implied_probability,
                    estimated_probability=estimate.estimated_probability,
                    confidence=estimate.confidence,
                    adjustments=[a.as_dict() for a in estimate.adjustments],
                    raw_edge=edge_result.raw_edge,
                    adjusted_edge=edge_result.adjusted_edge,
                    expected_value=edge_result.expected_value,
                    category_reliability=edge_result.category_reliability,
                    data_quality=features.data_quality,
                    quality_gate_passed=True,
                    quality_gate_reason=None,
                    risk_gate_passed=risk_result.passed,
                    risk_gate_reasons=risk_result.reasons,
                    score=score,
                    classification=classification.value,
                )
            )
            _tally(summary, classification.value)
    finally:
        market_repo.close()
        feature_repo.close()
        analysis_repo.close()
        if owns_clob:
            await clob.aclose()

    summary.elapsed_seconds = round(time.monotonic() - start, 2)
    logger.info(
        "analysis complete: target_markets=%d by_target_category=%s quality_gate_failed=%d "
        "risk_gate_failed=%d scored=%d classifications=%s elapsed=%.2fs",
        summary.markets_considered,
        summary.by_target_category,
        summary.quality_gate_failed,
        summary.risk_gate_failed,
        summary.scored,
        summary.classification_counts,
        summary.elapsed_seconds,
    )
    return summary


def _tally(summary: AnalysisSummary, classification: str) -> None:
    summary.classification_counts[classification] = summary.classification_counts.get(classification, 0) + 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    from app.config.loader import load_config

    cfg = load_config()
    database = Database(cfg.db_path)
    database.init_schema()
    asyncio.run(run_analysis_once(cfg, database))
