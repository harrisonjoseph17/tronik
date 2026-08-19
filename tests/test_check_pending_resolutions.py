"""End-to-end test for check_pending_resolutions(), mirroring test_scanner.py's
pattern: a stub Gamma client injected via the gamma= parameter (same seam
run_analysis_once/pipeline.py uses for CLOBClient), asserting persisted rows
and summary counts against a real sqlite DB."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config.loader import load_config
from app.polymarket.resolutions import check_pending_resolutions
from app.storage.analysis_models import AnalysisRecord, DataQualityState
from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import AnalysisRepository, MarketRepository, ResolutionRepository
from app.storage.resolution_models import ResolutionStatus

RESOLVED_MARKET = {
    "id": "3414461",
    "question": "Will Elon Musk post 140-159 tweets from August 11 to August 18, 2026?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0", "1"]',
    "active": True,
    "closed": True,
    "closedTime": "2026-08-18 16:57:10+00",
    "umaEndDate": "2026-08-18T16:57:10Z",
    "umaResolutionStatus": "resolved",
    "umaResolutionStatuses": '["proposed"]',
}

UNRESOLVED_MARKET = {
    "id": "3563932",
    "question": "Game 1: Any Player Quadra Kill?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.5", "0.5"]',
    "active": True,
    "closed": False,
    "umaResolutionStatuses": "[]",
}


class _StubGamma:
    """Records slugs looked up; returns canned bare-list responses keyed by slug."""

    def __init__(self, by_slug: dict):
        self._by_slug = by_slug
        self.slugs_queried: list[str] = []

    async def get_markets(self, **params):
        slug = params["slug"]
        self.slugs_queried.append(slug)
        market = self._by_slug.get(slug)
        return [market] if market is not None else []


@pytest.fixture
def config():
    return load_config(profile_path=Path("config/profile.yaml"), markets_path=Path("config/markets.yaml"))


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "resolve_test.db")
    database.init_schema()
    return database


def _seed(db: Database, market_id: str, slug: str) -> None:
    market_repo = MarketRepository(db)
    analysis_repo = AnalysisRepository(db)
    try:
        market_repo.upsert_market(
            Market(id=market_id, question="Q", slug=slug, category="crypto", subcategory="btc_up_down")
        )
        analysis_repo.insert(
            AnalysisRecord(
                market_id=market_id,
                computed_at=datetime.now(timezone.utc),
                data_quality=DataQualityState.SUFFICIENT,
                quality_gate_passed=True,
                classification="WATCH",
            )
        )
    finally:
        market_repo.close()
        analysis_repo.close()


@pytest.mark.asyncio
async def test_check_pending_resolutions_persists_resolved_outcome(config, db):
    _seed(db, "m-resolved", "elon-tweets-slug")
    gamma = _StubGamma({"elon-tweets-slug": RESOLVED_MARKET})

    summary = await check_pending_resolutions(config, db, gamma=gamma)

    assert summary.checked == 1
    assert summary.newly_resolved == 1
    assert summary.still_unresolved == 0

    resolution_repo = ResolutionRepository(db)
    try:
        resolution = resolution_repo.get("m-resolved")
    finally:
        resolution_repo.close()
    assert resolution is not None
    assert resolution.resolution_status == ResolutionStatus.RESOLVED
    assert resolution.winning_outcome == "No"
    assert resolution.winning_outcome_index == 1


@pytest.mark.asyncio
async def test_check_pending_resolutions_leaves_unresolved_markets_unresolved(config, db):
    _seed(db, "m-unresolved", "esports-slug")
    gamma = _StubGamma({"esports-slug": UNRESOLVED_MARKET})

    summary = await check_pending_resolutions(config, db, gamma=gamma)

    assert summary.checked == 1
    assert summary.still_unresolved == 1
    assert summary.newly_resolved == 0

    resolution_repo = ResolutionRepository(db)
    try:
        resolution = resolution_repo.get("m-unresolved")
    finally:
        resolution_repo.close()
    assert resolution is not None
    assert resolution.resolution_status == ResolutionStatus.UNRESOLVED


@pytest.mark.asyncio
async def test_check_pending_resolutions_skips_markets_already_resolved(config, db):
    """A market already marked resolved must not trigger another HTTP call -
    list_market_ids_pending_check() excludes it, so the stub never sees its slug."""
    _seed(db, "m-resolved", "elon-tweets-slug")
    gamma = _StubGamma({"elon-tweets-slug": RESOLVED_MARKET})
    await check_pending_resolutions(config, db, gamma=gamma)
    assert gamma.slugs_queried == ["elon-tweets-slug"]

    # Second run: same market, now already resolved - must be skipped entirely.
    gamma2 = _StubGamma({"elon-tweets-slug": RESOLVED_MARKET})
    summary2 = await check_pending_resolutions(config, db, gamma=gamma2)
    assert summary2.checked == 0
    assert gamma2.slugs_queried == []


@pytest.mark.asyncio
async def test_check_pending_resolutions_joins_against_analyses_by_market_id(config, db):
    """Requirement: resolved outcomes must be linkable back to analyses via
    market_id, with no FK/schema change to analyses itself."""
    _seed(db, "m-resolved", "elon-tweets-slug")
    gamma = _StubGamma({"elon-tweets-slug": RESOLVED_MARKET})
    await check_pending_resolutions(config, db, gamma=gamma)

    analysis_repo = AnalysisRepository(db)
    resolution_repo = ResolutionRepository(db)
    try:
        analyses = analysis_repo.list_for_market("m-resolved")
        resolution = resolution_repo.get("m-resolved")
    finally:
        analysis_repo.close()
        resolution_repo.close()

    assert len(analyses) == 1
    assert analyses[0].market_id == resolution.market_id
    assert analyses[0].classification == "WATCH"
    assert resolution.winning_outcome == "No"


@pytest.mark.asyncio
async def test_check_pending_resolutions_counts_not_found_when_slug_missing_locally(config, db):
    """A market row with no slug (e.g. never fully normalized) can't be
    looked up on Gamma - must count as not_found without any HTTP call."""
    market_repo = MarketRepository(db)
    analysis_repo = AnalysisRepository(db)
    try:
        market_repo.upsert_market(
            Market(id="no-slug-market", question="Q", slug=None, category="crypto", subcategory="btc_up_down")
        )
        analysis_repo.insert(
            AnalysisRecord(
                market_id="no-slug-market",
                computed_at=datetime.now(timezone.utc),
                data_quality=DataQualityState.SUFFICIENT,
                quality_gate_passed=True,
                classification="WATCH",
            )
        )
    finally:
        market_repo.close()
        analysis_repo.close()

    gamma = _StubGamma({})
    summary = await check_pending_resolutions(config, db, gamma=gamma)
    assert summary.not_found == 1
    assert summary.checked == 0
    assert gamma.slugs_queried == []
