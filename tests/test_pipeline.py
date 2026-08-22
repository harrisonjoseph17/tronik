from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analysis.pipeline import run_analysis_once
from app.config.loader import AnalysisSettings, AppConfig, FilterSettings
from app.polymarket.client import PolymarketAPIError
from app.storage.analysis_models import DataQualityState
from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import AnalysisRepository, MarketRepository, SignalJournalRepository

NOW = datetime.now(timezone.utc)

HEALTHY_BOOK = {
    "bids": [{"price": "0.40", "size": "100"}],
    "asks": [{"price": "0.60", "size": "100"}],
}


class _StubClob:
    """Returns a healthy book by default, or whatever's configured, for any
    token_id - these tests are about pipeline wiring, not CLOB parsing
    (that's test_order_book.py's job)."""

    def __init__(self, book=None, error: Exception | None = None):
        self._book = book if book is not None else HEALTHY_BOOK
        self._error = error
        self.calls: list[str] = []

    async def get_book(self, token_id: str):
        self.calls.append(token_id)
        if self._error:
            raise self._error
        return self._book


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
        subcategory="btc_up_down",
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
        clob_token_ids=["tok-1"],
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


@pytest.mark.asyncio
async def test_market_with_sufficient_history_gets_scored_and_classified(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m1")
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())

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


@pytest.mark.asyncio
async def test_market_with_no_snapshot_history_is_avoided(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m2")
        market_repo.upsert_market(market)
        # no snapshots inserted at all
    finally:
        market_repo.close()

    clob = _StubClob()
    summary = await run_analysis_once(config, db, clob=clob)

    assert summary.quality_gate_failed == 1
    assert summary.scored == 0
    assert summary.classification_counts == {"AVOID": 1}
    assert clob.calls == []  # CLOB is never called for a quality-gated-out market

    analysis_repo = AnalysisRepository(db)
    try:
        record = analysis_repo.list_for_market("m2")[0]
    finally:
        analysis_repo.close()

    assert record.quality_gate_passed is False
    assert record.risk_gate_passed is None
    assert record.score is None
    assert record.classification == "AVOID"


@pytest.mark.asyncio
async def test_market_with_wide_spread_passes_quality_but_fails_risk_gate(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m3")
        market.spread = 0.5  # far above risk_gate_max_spread
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0)
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())

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


@pytest.mark.asyncio
async def test_clob_unavailable_fails_risk_gate_even_with_good_edge(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m6")
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    clob = _StubClob(error=PolymarketAPIError("not found", status_code=404))
    summary = await run_analysis_once(config, db, clob=clob)

    assert summary.risk_gate_failed == 1
    assert summary.scored == 0
    assert clob.calls == ["tok-1"]

    analysis_repo = AnalysisRepository(db)
    try:
        record = analysis_repo.list_for_market("m6")[0]
    finally:
        analysis_repo.close()

    assert record.risk_gate_passed is False
    assert "clob_unavailable" in record.risk_gate_reasons


@pytest.mark.asyncio
async def test_market_without_clob_token_ids_skips_the_clob_call_and_fails_gate(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m7")
        market.clob_token_ids = []
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    clob = _StubClob()
    summary = await run_analysis_once(config, db, clob=clob)

    assert clob.calls == []  # never called - nothing to call it with
    assert summary.risk_gate_failed == 1

    analysis_repo = AnalysisRepository(db)
    try:
        record = analysis_repo.list_for_market("m7")[0]
    finally:
        analysis_repo.close()

    assert "clob_unavailable" in record.risk_gate_reasons


@pytest.mark.asyncio
async def test_excluded_markets_are_not_considered(config, db):
    market_repo = MarketRepository(db)
    try:
        excluded = _healthy_market("m4")
        excluded.filter_reason = "closed"
        market_repo.upsert_market(excluded)
        _seed_snapshots(market_repo, excluded, count=5, span_hours=3.0)
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())
    assert summary.markets_considered == 0


@pytest.mark.asyncio
async def test_other_category_markets_are_not_considered(config, db):
    market_repo = MarketRepository(db)
    try:
        other_market = _healthy_market("m5")
        other_market.category = "other"
        other_market.subcategory = "uncategorized"
        market_repo.upsert_market(other_market)
        _seed_snapshots(market_repo, other_market, count=5, span_hours=3.0)
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())
    assert summary.markets_considered == 0


@pytest.mark.asyncio
async def test_non_target_subcategory_within_a_target_category_is_ignored(config, db):
    """A market categorized as politics/some_other_subcategory (a real,
    non-target subcategory within a category that IS otherwise targeted)
    must NOT be analyzed just because "politics" itself is a target
    top-level category - only elon_musk_tweets/white_house_tweets are."""
    market_repo = MarketRepository(db)
    try:
        other_market = _healthy_market("m8")
        other_market.category = "politics"
        other_market.subcategory = "some_other_subcategory"
        market_repo.upsert_market(other_market)
        _seed_snapshots(market_repo, other_market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    clob = _StubClob()
    summary = await run_analysis_once(config, db, clob=clob)

    assert summary.markets_considered == 0
    assert clob.calls == []  # never fetched - excluded before feature/CLOB work


@pytest.mark.asyncio
async def test_sports_basketball_is_excluded_from_analysis(config, db):
    """Requirement #17 (scope reduction): sports/basketball was removed
    from target_subcategories entirely. The categorize rule for it still
    exists in config/markets.yaml (untouched, per the minimal-footprint
    design), so a basketball market can still be discovered/tagged, but it
    must never be analyzed now that "sports" isn't a target category at
    all - not even the narrower "wrong subcategory within a target
    category" case, since the whole category is gone from the allowlist."""
    market_repo = MarketRepository(db)
    try:
        basketball_market = _healthy_market("m9")
        basketball_market.category = "sports"
        basketball_market.subcategory = "basketball"
        market_repo.upsert_market(basketball_market)
        _seed_snapshots(market_repo, basketball_market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    clob = _StubClob()
    summary = await run_analysis_once(config, db, clob=clob)

    assert summary.markets_considered == 0
    assert clob.calls == []
    assert "sports/basketball" not in summary.by_target_category


@pytest.mark.asyncio
async def test_multiple_markets_across_categories(config, db):
    market_repo = MarketRepository(db)
    try:
        pairs = [
            ("politics", "elon_musk_tweets"),
            ("crypto", "btc_up_down"),
        ]
        for i, (category, subcategory) in enumerate(pairs):
            market = _healthy_market(f"m{i}")
            market.category = category
            market.subcategory = subcategory
            market_repo.upsert_market(market)
            _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())
    assert summary.markets_considered == 2
    assert summary.scored == 2
    assert summary.by_target_category == {
        "politics/elon_musk_tweets": 1,
        "politics/white_house_tweets": 0,
        "crypto/btc_up_down": 1,
    }


# --------------------------------------------------------------------------
# Signal journal (requirement #18) - record each market's FIRST actionable
# COMPOUND/WATCH signal, never overwritten by later analyze cycles.
# --------------------------------------------------------------------------


@pytest.fixture
def compound_forcing_config() -> AppConfig:
    """Same baseline as `config`, but with compound_min_score/
    asymmetric_edge_threshold tuned so that ANY market that clears the risk
    gate deterministically lands on COMPOUND - avoids depending on exact
    score/edge thresholds tied to price_change, which the plain `config`
    fixture doesn't pin down (existing tests assert classification is one
    of several possible values for the same seeding recipe)."""
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
            compound_min_score=0.0,
            watch_min_score=0.0,
            asymmetric_edge_threshold=999.0,  # never trips - keeps COMPOUND, not ASYMMETRIC
        ),
        category_rules=[],
    )


@pytest.mark.asyncio
async def test_first_compound_signal_is_recorded_in_journal(compound_forcing_config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m10")
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    summary = await run_analysis_once(compound_forcing_config, db, clob=_StubClob())
    assert summary.classification_counts == {"COMPOUND": 1}
    assert summary.first_signals_recorded == 1

    analysis_repo = AnalysisRepository(db)
    journal_repo = SignalJournalRepository(db)
    try:
        record = analysis_repo.list_for_market("m10")[0]
        entry = journal_repo.get("m10")
    finally:
        analysis_repo.close()
        journal_repo.close()

    assert entry is not None
    assert entry.classification == "COMPOUND"
    assert entry.category == "crypto"
    assert entry.subcategory == "btc_up_down"
    assert entry.market_implied_probability == pytest.approx(record.market_implied_probability)
    assert entry.estimated_probability == pytest.approx(record.estimated_probability)
    assert entry.adjusted_edge == pytest.approx(record.adjusted_edge)
    assert entry.score == pytest.approx(record.score)


@pytest.mark.asyncio
async def test_repeated_analyze_cycles_do_not_overwrite_first_signal(compound_forcing_config, db):
    """Directly proves requirement #18's core ask: running analyze again
    (new snapshot data that would otherwise produce different numbers)
    must not change the journal row written by the first cycle."""
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m11")
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=0.40)
    finally:
        market_repo.close()

    first_summary = await run_analysis_once(compound_forcing_config, db, clob=_StubClob())
    assert first_summary.first_signals_recorded == 1

    journal_repo = SignalJournalRepository(db)
    try:
        first_entry = journal_repo.get("m11")
    finally:
        journal_repo.close()

    # A second cycle - seed a very different price move, which would
    # produce different estimated_probability/edge/score if it were free
    # to overwrite.
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m11")
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0, price_change=-0.30)
    finally:
        market_repo.close()

    second_summary = await run_analysis_once(compound_forcing_config, db, clob=_StubClob())
    assert second_summary.first_signals_recorded == 0  # already journaled - no-op

    journal_repo = SignalJournalRepository(db)
    try:
        second_entry = journal_repo.get("m11")
    finally:
        journal_repo.close()

    assert second_entry.first_signal_at == first_entry.first_signal_at
    assert second_entry.estimated_probability == pytest.approx(first_entry.estimated_probability)
    assert second_entry.adjusted_edge == pytest.approx(first_entry.adjusted_edge)
    assert second_entry.score == pytest.approx(first_entry.score)


@pytest.mark.asyncio
async def test_no_trade_market_is_never_journaled(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m12")
        market_repo.upsert_market(market)
        _seed_snapshots(market_repo, market, count=5, span_hours=3.0)  # no price_change -> wide_spread -> NO_TRADE
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())
    assert summary.classification_counts == {"NO_TRADE": 1}
    assert summary.first_signals_recorded == 0

    journal_repo = SignalJournalRepository(db)
    try:
        entry = journal_repo.get("m12")
    finally:
        journal_repo.close()
    assert entry is None


@pytest.mark.asyncio
async def test_avoid_market_is_never_journaled(config, db):
    market_repo = MarketRepository(db)
    try:
        market = _healthy_market("m13")
        market_repo.upsert_market(market)
        # no snapshots inserted at all -> quality gate fails -> AVOID
    finally:
        market_repo.close()

    summary = await run_analysis_once(config, db, clob=_StubClob())
    assert summary.classification_counts == {"AVOID": 1}
    assert summary.first_signals_recorded == 0

    journal_repo = SignalJournalRepository(db)
    try:
        entry = journal_repo.get("m13")
    finally:
        journal_repo.close()
    assert entry is None
