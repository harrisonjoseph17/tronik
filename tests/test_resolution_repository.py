from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import MarketRepository, ResolutionRepository
from app.storage.resolution_models import Resolution, ResolutionStatus

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    # resolutions has a FK to markets(market_id) - seed m1.
    market_repo = MarketRepository(database)
    try:
        market_repo.upsert_market(Market(id="m1", question="Q1", category="crypto", subcategory="btc_up_down"))
    finally:
        market_repo.close()
    return database


@pytest.fixture
def repo(db):
    repository = ResolutionRepository(db)
    yield repository
    repository.close()


def _unresolved(**overrides) -> Resolution:
    defaults = dict(
        market_id="m1",
        resolution_status=ResolutionStatus.UNRESOLVED,
        raw_resolution_json='{"closed": false}',
        checked_at=T1,
    )
    defaults.update(overrides)
    return Resolution(**defaults)


def _resolved(**overrides) -> Resolution:
    defaults = dict(
        market_id="m1",
        resolution_status=ResolutionStatus.RESOLVED,
        resolved_at=T1,
        winning_outcome_index=1,
        winning_outcome="No",
        raw_resolution_json='{"closed": true, "umaResolutionStatus": "resolved"}',
        checked_at=T1,
    )
    defaults.update(overrides)
    return Resolution(**defaults)


def test_insert_then_get(repo):
    repo.upsert(_unresolved())
    fetched = repo.get("m1")
    assert fetched is not None
    assert fetched.resolution_status == ResolutionStatus.UNRESOLVED


def test_unresolved_to_resolved_transition_is_allowed(repo):
    repo.upsert(_unresolved(checked_at=T1))
    repo.upsert(_resolved(checked_at=T2))
    fetched = repo.get("m1")
    assert fetched.resolution_status == ResolutionStatus.RESOLVED
    assert fetched.winning_outcome == "No"
    assert fetched.checked_at == T2


def test_resolved_to_resolved_does_not_change_the_outcome(repo):
    repo.upsert(_resolved(winning_outcome="No", winning_outcome_index=1, checked_at=T1))
    # A later check somehow computes a different outcome - must be ignored.
    repo.upsert(
        _resolved(winning_outcome="Yes", winning_outcome_index=0, checked_at=T2, raw_resolution_json='{"different": true}')
    )
    fetched = repo.get("m1")
    assert fetched.winning_outcome == "No"
    assert fetched.winning_outcome_index == 1
    assert fetched.raw_resolution_json == '{"closed": true, "umaResolutionStatus": "resolved"}'


def test_resolved_to_resolved_still_advances_checked_at(repo):
    repo.upsert(_resolved(checked_at=T1))
    repo.upsert(_resolved(checked_at=T2))
    fetched = repo.get("m1")
    assert fetched.checked_at == T2


def test_resolved_to_unresolved_is_rejected(repo):
    """Requirement: resolved -> unresolved must be rejected/ignored."""
    repo.upsert(_resolved(checked_at=T1))
    repo.upsert(_unresolved(checked_at=T2))
    fetched = repo.get("m1")
    assert fetched.resolution_status == ResolutionStatus.RESOLVED
    assert fetched.winning_outcome == "No"
    assert fetched.checked_at == T2  # still advances


def test_upsert_is_idempotent_for_repeated_identical_calls(repo):
    repo.upsert(_resolved(checked_at=T1))
    repo.upsert(_resolved(checked_at=T1))
    repo.upsert(_resolved(checked_at=T1))
    fetched = repo.get("m1")
    assert fetched.resolution_status == ResolutionStatus.RESOLVED
    assert fetched.winning_outcome == "No"


def test_raw_json_round_trips_exactly(repo):
    raw = '{"id": "123", "question": "Will it happen?", "outcomePrices": "[\\"0\\", \\"1\\"]"}'
    repo.upsert(_resolved(raw_resolution_json=raw))
    fetched = repo.get("m1")
    assert fetched.raw_resolution_json == raw


def test_get_missing_market_returns_none(repo):
    assert repo.get("does-not-exist") is None


def test_list_by_status(repo):
    repo.upsert(_resolved())
    rows = repo.list_by_status(ResolutionStatus.RESOLVED)
    assert len(rows) == 1
    assert rows[0].market_id == "m1"
    assert repo.list_by_status(ResolutionStatus.INVALID) == []


def test_list_market_ids_pending_check_excludes_resolved(db):
    from app.storage.analysis_models import AnalysisRecord, DataQualityState
    from app.storage.repositories import AnalysisRepository

    market_repo = MarketRepository(db)
    analysis_repo = AnalysisRepository(db)
    resolution_repo = ResolutionRepository(db)
    try:
        market_repo.upsert_market(Market(id="m2", question="Q2", category="crypto", subcategory="btc_up_down"))
        analysis_repo.insert(
            AnalysisRecord(
                market_id="m1",
                computed_at=T1,
                data_quality=DataQualityState.SUFFICIENT,
                quality_gate_passed=True,
                classification="WATCH",
            )
        )
        analysis_repo.insert(
            AnalysisRecord(
                market_id="m2",
                computed_at=T1,
                data_quality=DataQualityState.SUFFICIENT,
                quality_gate_passed=True,
                classification="WATCH",
            )
        )
        resolution_repo.upsert(_resolved(market_id="m1"))

        pending = resolution_repo.list_market_ids_pending_check()
        assert pending == ["m2"]
    finally:
        market_repo.close()
        analysis_repo.close()
        resolution_repo.close()


def test_list_market_ids_pending_check_includes_markets_with_no_resolution_row(db):
    from app.storage.analysis_models import AnalysisRecord, DataQualityState
    from app.storage.repositories import AnalysisRepository

    analysis_repo = AnalysisRepository(db)
    resolution_repo = ResolutionRepository(db)
    try:
        analysis_repo.insert(
            AnalysisRecord(
                market_id="m1",
                computed_at=T1,
                data_quality=DataQualityState.SUFFICIENT,
                quality_gate_passed=True,
                classification="WATCH",
            )
        )
        pending = resolution_repo.list_market_ids_pending_check()
        assert pending == ["m1"]
    finally:
        analysis_repo.close()
        resolution_repo.close()
