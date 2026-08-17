from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analysis.pipeline import run_analysis_once
from app.config.loader import AnalysisSettings, AppConfig, FilterSettings
from app.storage.analysis_models import DataQualityState
from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import AnalysisRepository, MarketRepository

NOW = datetime.now(timezone.utc)


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        filters=FilterSettings(),
        analysis=AnalysisSettings(
            min_snapshots_for_history=3,
            min_history_hours=1.0,
            risk_gate_max_spread=0.08,
            risk_gate_min_liquidity=2000.0,
            risk_gate_min_hours_to_resolution=4.0,
            risk_gate_min_edge=0.05,
            momentum_adjustment_weight=0.3,
            max_probability_adjustment=0.15,
        ),
        category_rules=[],
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "pipeline_test.db")
    database.init_schema()
    return database


def _healthy_market(id_: str) -> Market:
    return Market(
        id=id_,
        question="Will BTC be up?",
        category="crypto",
        subcategory="btc_updown",
        mid_price=0.55,
        liquidity=5000.0,
        spread=0.02,
        best_bid=0.54,
        best_ask=0.56,
        volume=10000.0,
        volume_24hr=2000.0,
        active=True,
        status="active",
        start_date=NOW - timedelta(days=2),
        end_date=NOW + timedelta(hours=48),
        filter_reason=None,
    )


def _seed_snapshots(
    repo: MarketRepository, market: Market, *, count: int, span_hours: float, price_change: float = 0.0
) -> None:
    """Insert `count` snapshots spanning `span_hours`, ramping mid_price from
    (market.mid_price - price_change) up to market.mid_price, so
    price_change_recent (and therefore edge) is nonzero when needed."""
    for i in range(count):
        frac = i / max(count - 1, 1)
        hours_ago = span_hours - (span_hours * frac)
        snapshot_market = market.model_copy(
            update={"mid_price": market.mid_price - price_change + price_change * frac}
        )
        repo.insert_snapshot(snapshot_market, captured_at=NOW - timedelta(hours=hours_ago))


def test_market_with_sufficient_history_gets_scored_and_classified(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m1")
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    summary = run_analysis_once(config, db)

    assert summary.markets_considered == 1
    assert summary.quality_gate_failed == 0
    assert summary.scored == 1

    analysis_repo = AnalysisRepository(db)
    try:
        record = analysis_repo.list_for_market("m1")[0]
    finally:
        analysis_repo.close()

    assert record.quality_gate_passed is True
    assert record.risk_gate_passed is True
    assert record.score is not None
    assert record.data_quality == DataQualityState.SUFFICIENT
    assert record.market_implied_probability == 0.55
    assert record.classification in {"COMPOUND", "ASYMMETRIC", "WATCH", "NO_TRADE"}


def test_market_with_no_snapshot_history_is_avoided(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m2")
        market_repo.upsert_market(market)
        # no snapshots inserted at all
    finally:
        market_repo.close()

    summary = run_analysis_once(config, db)

    assert summary.quality_gate_failed == 1
    assert summary.scored == 0
    assert summary.classification_counts == {"AVOID": 1}

    analysis_repo = AnalysisRepository(db)
    try:
        record = analysis_repo.list_for_market("m2")[0]
    finally:
        analysis_repo.close()

    assert record.quality_gate_passed is False
    assert record.risk_gate_passed is None
    assert record.score is None
    assert record.classification == "AVOID"


def test_market_with_wide_spread_passes_quality_but_fails_risk_gate(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m3")
        market.spread = 0.5  # far above risk_gate_max_spread
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0)
    finally:
        market_repo.close()

    summary = run_analysis_once(config, db)

    assert summary.quality_gate_failed == 0
    assert summary.risk_gate_failed == 1
    assert summary.scored == 0
    assert summary.classification_counts == {"NO_TRADE": 1}

    analysis_repo = AnalysisRepository(db)
    try:
        record = analysis_repo.list_for_market("m3")[0]
    finally:
        analysis_repo.close()

    assert record.quality_gate_passed is True
    assert record.risk_gate_passed is False
    assert "wide_spread" in record.risk_gate_reasons
    assert record.score is None


def test_excluded_markets_are_not_considered(config, db):
    market_repo = MarketRepository(db)
    try:
        excluded = _healthy_market("m4")
        excluded.filter_reason = "closed"
        market_repo.upsert_market(excluded)
        _seed_snapshots(market_repo, excluded, count=5, span_hours=3.0)
    finally:
        market_repo.close()

    summary = run_analysis_once(config, db)
    assert summary.markets_considered == 0


def test_other_category_markets_are_not_considered(config, db):
    market_repo = MarketRepository(db)
    try:
        other_market = _healthy_market("m5")
        other_market.category = "other"
        other_market.subcategory = "uncategorized"
        market_repo.upsert_market(other_market)
        _seed_snapshots(market_repo, other_market, count=5, span_hours=3.0)
    finally:
        market_repo.close()

    summary = run_analysis_once(config, db)
    assert summary.markets_considered == 0


def test_multiple_markets_across_categories(config, db):
    market_repo = MarketRepository(db)
    try:
        for i, category in enumerate(["politics", "crypto", "sports"]):
            market = _healthy_market(f"m{i}")
            market.category = category
            market.subcategory = "x"
            market_repo.upsert_market(market)
            _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    summary = run_analysis_once(config, db)
    assert summary.markets_considered == 3
    assert summary.scored == 3
