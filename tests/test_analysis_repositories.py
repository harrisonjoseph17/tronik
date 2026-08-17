from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.analysis_models import AnalysisRecord, DataQualityState, Features
from app.storage.database import Database
from app.storage.repositories import AnalysisRepository, FeatureRepository, MarketRepository
from app.storage.models import Market


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    # features/analyses have a FK to markets(market_id) - seed m1/m2 so
    # inserts in these tests satisfy the constraint.
    market_repo = MarketRepository(database)
    try:
        market_repo.bulk_upsert_markets(
            [
                Market(id="m1", question="Q1", category="crypto", subcategory="btc_updown"),
                Market(id="m2", question="Q2", category="crypto", subcategory="btc_updown"),
            ]
        )
    finally:
        market_repo.close()
    return database


@pytest.fixture
def feature_repo(db):
    repo = FeatureRepository(db)
    yield repo
    repo.close()


@pytest.fixture
def analysis_repo(db):
    repo = AnalysisRepository(db)
    yield repo
    repo.close()


def _features(**overrides) -> Features:
    defaults = dict(
        market_id="m1",
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        market_implied_probability=0.6,
        liquidity=5000.0,
        spread=0.02,
        snapshot_count=5,
        history_span_hours=3.0,
        price_change_recent=0.01,
        data_quality=DataQualityState.SUFFICIENT,
        data_quality_reasons=[],
    )
    defaults.update(overrides)
    return Features(**defaults)


def _analysis(**overrides) -> AnalysisRecord:
    defaults = dict(
        market_id="m1",
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        category="crypto",
        market_implied_probability=0.6,
        estimated_probability=0.63,
        confidence=0.7,
        adjustments=[{"name": "momentum", "value": 0.03, "reason": "test"}],
        raw_edge=0.03,
        adjusted_edge=0.014,
        expected_value=0.014,
        category_reliability=0.8,
        data_quality=DataQualityState.SUFFICIENT,
        quality_gate_passed=True,
        risk_gate_passed=True,
        risk_gate_reasons=[],
        score=72.5,
        classification="COMPOUND",
    )
    defaults.update(overrides)
    return AnalysisRecord(**defaults)


def test_insert_and_list_features(feature_repo):
    feature_repo.insert(_features())
    rows = feature_repo.list_for_market("m1")
    assert len(rows) == 1
    assert rows[0].market_implied_probability == 0.6
    assert rows[0].data_quality == DataQualityState.SUFFICIENT
    assert rows[0].snapshot_count == 5


def test_features_round_trips_reasons_list(feature_repo):
    feature_repo.insert(
        _features(data_quality=DataQualityState.PARTIAL, data_quality_reasons=["missing_volume"])
    )
    row = feature_repo.list_for_market("m1")[0]
    assert row.data_quality == DataQualityState.PARTIAL
    assert row.data_quality_reasons == ["missing_volume"]


def test_insert_and_list_analysis(analysis_repo):
    analysis_repo.insert(_analysis())
    rows = analysis_repo.list_for_market("m1")
    assert len(rows) == 1
    record = rows[0]
    assert record.estimated_probability == 0.63
    assert record.market_implied_probability == 0.6  # preserved separately, not overwritten
    assert record.classification == "COMPOUND"
    assert record.adjustments == [{"name": "momentum", "value": 0.03, "reason": "test"}]


def test_analysis_gated_out_has_null_score_and_risk_gate(analysis_repo):
    analysis_repo.insert(
        _analysis(
            estimated_probability=None,
            confidence=None,
            raw_edge=None,
            adjusted_edge=None,
            expected_value=None,
            data_quality=DataQualityState.INSUFFICIENT,
            quality_gate_passed=False,
            quality_gate_reason="insufficient_data",
            risk_gate_passed=None,
            score=None,
            classification="AVOID",
        )
    )
    record = analysis_repo.list_for_market("m1")[0]
    assert record.score is None
    assert record.risk_gate_passed is None
    assert record.quality_gate_passed is False
    assert record.classification == "AVOID"


def test_list_by_classification(analysis_repo):
    analysis_repo.insert(_analysis(market_id="m1", classification="COMPOUND"))
    analysis_repo.insert(_analysis(market_id="m2", classification="NO_TRADE"))
    compounds = analysis_repo.list_by_classification("COMPOUND")
    assert len(compounds) == 1
    assert compounds[0].market_id == "m1"


def test_market_repository_list_snapshots(db):
    repo = MarketRepository(db)
    try:
        market = Market(id="m1", question="Q", category="crypto", subcategory="btc_updown", mid_price=0.5)
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        repo.upsert_market(market)
        repo.insert_snapshot(market, captured_at=t1)
        repo.insert_snapshot(market, captured_at=t2)
        snapshots = repo.list_snapshots("m1")
        assert len(snapshots) == 2
        assert snapshots[0]["captured_at"] == t2.isoformat()  # most recent first
    finally:
        repo.close()


def test_market_repository_included_only_excludes_filtered(db):
    repo = MarketRepository(db)
    try:
        included = Market(id="m1", question="Q1", category="crypto", subcategory="btc_updown", filter_reason=None)
        excluded = Market(id="m2", question="Q2", category="crypto", subcategory="btc_updown", filter_reason="closed")
        repo.bulk_upsert_markets([included, excluded])
        results = repo.list_markets(included_only=True)
        ids = {m.id for m in results}
        assert ids == {"m1"}
    finally:
        repo.close()
