"""Tests for scripts/evaluate_resolutions.py - the corrected market-level
outcome evaluation report. The central thing under test: `analyses` is a
time-series table (one market accumulates many snapshot rows over time),
so every metric here must be computed PER MARKET, never per snapshot -
repeated COMPOUND/WATCH snapshots for the same market must never inflate
sample size."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.analysis_models import AnalysisRecord, DataQualityState
from app.storage.database import Database
from app.storage.models import Market
from app.storage.repositories import AnalysisRepository, MarketRepository, ResolutionRepository
from app.storage.resolution_models import Resolution, ResolutionStatus
from scripts.evaluate_resolutions import (
    AnalysisSnapshot,
    ResolvedMarket,
    aggregate_by_classification,
    aggregate_by_category,
    brier_score,
    build_primary_evaluations,
    calibration_bins,
    derive_predicted_side,
    run_evaluation,
    select_primary_snapshot,
    snapshot_level_report,
)

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def _snap(**overrides) -> AnalysisSnapshot:
    defaults = dict(
        market_id="m1",
        computed_at=T0,
        category="crypto",
        market_implied_probability=0.55,
        estimated_probability=0.6,
        raw_edge=0.05,
        adjusted_edge=0.03,
        score=70.0,
        classification="COMPOUND",
    )
    defaults.update(overrides)
    return AnalysisSnapshot(**defaults)


def _resolved(**overrides) -> ResolvedMarket:
    defaults = dict(market_id="m1", resolved_at=T0 + timedelta(hours=10), winning_outcome="Yes")
    defaults.update(overrides)
    return ResolvedMarket(**defaults)


# --------------------------------------------------------------------------
# select_primary_snapshot
# --------------------------------------------------------------------------


def test_select_primary_snapshot_picks_latest_at_or_before_resolution():
    resolved_at = T0 + timedelta(hours=5)
    snapshots = [
        _snap(computed_at=T0),
        _snap(computed_at=T0 + timedelta(hours=1)),
        _snap(computed_at=T0 + timedelta(hours=3)),  # should win - latest <= resolved_at
    ]
    primary = select_primary_snapshot(snapshots, resolved_at)
    assert primary.computed_at == T0 + timedelta(hours=3)


def test_select_primary_snapshot_never_selects_after_resolution():
    resolved_at = T0 + timedelta(hours=2)
    snapshots = [
        _snap(computed_at=T0 + timedelta(hours=1)),
        _snap(computed_at=T0 + timedelta(hours=3)),  # after resolution - must be ignored
        _snap(computed_at=T0 + timedelta(hours=10)),  # after resolution - must be ignored
    ]
    primary = select_primary_snapshot(snapshots, resolved_at)
    assert primary.computed_at == T0 + timedelta(hours=1)


def test_select_primary_snapshot_returns_none_when_all_snapshots_are_after_resolution():
    resolved_at = T0
    snapshots = [_snap(computed_at=T0 + timedelta(hours=1))]
    assert select_primary_snapshot(snapshots, resolved_at) is None


def test_select_primary_snapshot_returns_none_for_empty_input():
    assert select_primary_snapshot([], T0) is None


def test_select_primary_snapshot_returns_none_when_resolved_at_missing():
    assert select_primary_snapshot([_snap()], None) is None


# --------------------------------------------------------------------------
# derive_predicted_side / brier_score
# --------------------------------------------------------------------------


def test_derive_predicted_side_boundary_is_yes():
    assert derive_predicted_side(0.5) == "Yes"
    assert derive_predicted_side(0.5000001) == "Yes"
    assert derive_predicted_side(0.4999999) == "No"


def test_brier_score_perfect_prediction_is_zero():
    assert brier_score(1.0, "Yes") == 0.0
    assert brier_score(0.0, "No") == 0.0


def test_brier_score_maximally_wrong_prediction_is_one():
    assert brier_score(1.0, "No") == 1.0
    assert brier_score(0.0, "Yes") == 1.0


def test_brier_score_matches_hand_computed_value():
    # p=0.7 predicted, actual "No" (0.0): (0.7 - 0.0)^2 = 0.49
    assert brier_score(0.7, "No") == pytest.approx(0.49)
    # p=0.7 predicted, actual "Yes" (1.0): (0.7 - 1.0)^2 = 0.09
    assert brier_score(0.7, "Yes") == pytest.approx(0.09)


def test_brier_score_penalizes_confident_wrong_prediction_worse_than_uncertain():
    confident_wrong = brier_score(0.95, "No")
    uncertain_wrong = brier_score(0.55, "No")
    assert confident_wrong > uncertain_wrong


# --------------------------------------------------------------------------
# build_primary_evaluations - the core "one market, not N snapshots" rule
# --------------------------------------------------------------------------


def test_multiple_analyses_for_one_market_produce_exactly_one_evaluation():
    """Directly reproduces the user's real example: a market with 22
    COMPOUND + 2 WATCH snapshots must contribute exactly 1 primary
    evaluation, not 24."""
    resolved_at = T0 + timedelta(days=1)
    snapshots = [
        _snap(computed_at=T0 + timedelta(hours=i), classification="COMPOUND") for i in range(22)
    ] + [
        _snap(computed_at=T0 + timedelta(hours=22 + i), classification="WATCH") for i in range(2)
    ]
    resolved = [_resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Yes")]
    evaluations, excluded = build_primary_evaluations(resolved, {"m1": snapshots})

    assert len(evaluations) == 1
    assert excluded == []
    # The primary snapshot is the LAST one <= resolved_at - the second WATCH row.
    assert evaluations[0].snapshot.classification == "WATCH"
    assert evaluations[0].snapshot.computed_at == T0 + timedelta(hours=23)


def test_market_with_no_analysis_before_resolution_is_excluded():
    resolved_at = T0
    snapshots = [_snap(computed_at=T0 + timedelta(hours=1))]  # only after resolution
    resolved = [_resolved(market_id="m1", resolved_at=resolved_at)]
    evaluations, excluded = build_primary_evaluations(resolved, {"m1": snapshots})
    assert evaluations == []
    assert excluded == ["m1"]


def test_market_with_zero_analyses_at_all_is_excluded():
    resolved = [_resolved(market_id="ghost")]
    evaluations, excluded = build_primary_evaluations(resolved, {})
    assert evaluations == []
    assert excluded == ["ghost"]


def test_multiple_resolved_markets_each_produce_their_own_single_evaluation():
    resolved = [
        _resolved(market_id="m1", winning_outcome="Yes"),
        _resolved(market_id="m2", winning_outcome="No"),
    ]
    analyses = {
        "m1": [_snap(market_id="m1", computed_at=T0), _snap(market_id="m1", computed_at=T0 + timedelta(hours=1))],
        "m2": [_snap(market_id="m2", computed_at=T0)],
    }
    evaluations, excluded = build_primary_evaluations(resolved, analyses)
    assert excluded == []
    assert {e.market_id for e in evaluations} == {"m1", "m2"}
    assert len(evaluations) == 2


# --------------------------------------------------------------------------
# aggregate_by_classification - market-level, not snapshot-level
# --------------------------------------------------------------------------


def test_repeated_compound_snapshots_do_not_inflate_compound_sample_size():
    """A single market with 22 COMPOUND snapshots resolves - COMPOUND's
    unique_markets must be 1 (or 0, if the primary snapshot happened to be
    WATCH), never 22."""
    resolved_at = T0 + timedelta(hours=1)
    snapshots = [_snap(computed_at=T0 + timedelta(minutes=i), classification="COMPOUND") for i in range(22)]
    resolved = [_resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Yes")]
    evaluations, _ = build_primary_evaluations(resolved, {"m1": snapshots})
    stats = aggregate_by_classification(evaluations)

    assert stats["COMPOUND"].unique_markets == 1
    assert stats["WATCH"].unique_markets == 0


def test_classification_performance_is_market_level_across_many_markets():
    """Three distinct markets, each with several COMPOUND snapshots -
    COMPOUND's unique_markets must be exactly 3, not the total snapshot
    count (which would be much higher)."""
    resolved = []
    analyses = {}
    for i in range(3):
        market_id = f"m{i}"
        resolved_at = T0 + timedelta(hours=1)
        resolved.append(_resolved(market_id=market_id, resolved_at=resolved_at, winning_outcome="Yes"))
        analyses[market_id] = [
            _snap(market_id=market_id, computed_at=T0 + timedelta(minutes=j), classification="COMPOUND")
            for j in range(5)  # 5 snapshots each = 15 total snapshot rows
        ]

    evaluations, excluded = build_primary_evaluations(resolved, analyses)
    stats = aggregate_by_classification(evaluations)

    assert excluded == []
    assert stats["COMPOUND"].unique_markets == 3


def test_classification_stats_includes_all_five_classification_values():
    evaluations, _ = build_primary_evaluations([], {})
    stats = aggregate_by_classification(evaluations)
    assert set(stats) == {"COMPOUND", "ASYMMETRIC", "WATCH", "NO_TRADE", "AVOID"}


def test_classification_accuracy_and_averages_computed_correctly():
    resolved_at = T0 + timedelta(hours=1)
    resolved = [
        _resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Yes"),
        _resolved(market_id="m2", resolved_at=resolved_at, winning_outcome="No"),
    ]
    analyses = {
        "m1": [_snap(market_id="m1", computed_at=T0, estimated_probability=0.7, score=80.0, classification="COMPOUND")],
        # m2 predicted Yes (est_p >= 0.5) but actual is No - incorrect.
        "m2": [_snap(market_id="m2", computed_at=T0, estimated_probability=0.6, score=60.0, classification="COMPOUND")],
    }
    evaluations, _ = build_primary_evaluations(resolved, analyses)
    stats = aggregate_by_classification(evaluations)
    compound = stats["COMPOUND"]

    assert compound.unique_markets == 2
    assert compound.binary_n == 2
    assert compound.correct == 1
    assert compound.incorrect == 1
    assert compound.accuracy == pytest.approx(0.5)
    assert compound.avg_estimated_probability == pytest.approx(0.65)
    assert compound.avg_score == pytest.approx(70.0)


def test_non_binary_winning_outcome_excluded_from_accuracy_but_counted_in_market_total():
    resolved_at = T0 + timedelta(hours=1)
    resolved = [_resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Lakers")]
    analyses = {"m1": [_snap(market_id="m1", computed_at=T0, classification="COMPOUND")]}
    evaluations, _ = build_primary_evaluations(resolved, analyses)
    stats = aggregate_by_classification(evaluations)

    assert stats["COMPOUND"].unique_markets == 1  # still counted at the market-count level
    assert stats["COMPOUND"].binary_n == 0  # but excluded from the binary/accuracy sample


# --------------------------------------------------------------------------
# aggregate_by_category
# --------------------------------------------------------------------------


def test_aggregate_by_category_is_binary_and_market_level():
    resolved_at = T0 + timedelta(hours=1)
    resolved = [
        _resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Yes"),
        _resolved(market_id="m2", resolved_at=resolved_at, winning_outcome="Yes"),
    ]
    analyses = {
        "m1": [_snap(market_id="m1", computed_at=T0, category="politics", estimated_probability=0.8)],
        "m2": [_snap(market_id="m2", computed_at=T0, category="crypto", estimated_probability=0.3)],
    }
    evaluations, _ = build_primary_evaluations(resolved, analyses)
    stats = aggregate_by_category(evaluations)

    assert stats["politics"].binary_n == 1
    assert stats["politics"].correct == 1
    assert stats["crypto"].binary_n == 1
    assert stats["crypto"].correct == 0  # predicted No (0.3 < 0.5), actual Yes


# --------------------------------------------------------------------------
# calibration_bins
# --------------------------------------------------------------------------


def test_calibration_bins_are_market_level_not_snapshot_level():
    """A market with many snapshots must contribute exactly one observation
    to calibration - its primary snapshot - never one per row."""
    resolved_at = T0 + timedelta(hours=1)
    snapshots = [
        _snap(computed_at=T0 + timedelta(minutes=i), estimated_probability=0.75) for i in range(10)
    ]
    resolved = [_resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Yes")]
    evaluations, _ = build_primary_evaluations(resolved, {"m1": snapshots})
    bins = calibration_bins(evaluations, bin_width=0.2)

    matching = [b for b in bins if b.low <= 0.75 < b.high]
    assert len(matching) == 1
    assert matching[0].n == 1  # one market, not 10 snapshots


def test_calibration_bin_actual_frequency_computed_correctly():
    resolved_at = T0 + timedelta(hours=1)
    resolved = [
        _resolved(market_id="m1", resolved_at=resolved_at, winning_outcome="Yes"),
        _resolved(market_id="m2", resolved_at=resolved_at, winning_outcome="No"),
    ]
    analyses = {
        "m1": [_snap(market_id="m1", computed_at=T0, estimated_probability=0.65)],
        "m2": [_snap(market_id="m2", computed_at=T0, estimated_probability=0.65)],
    }
    evaluations, _ = build_primary_evaluations(resolved, analyses)
    bins = calibration_bins(evaluations, bin_width=0.2)
    bucket = next(b for b in bins if b.low <= 0.65 < b.high)

    assert bucket.n == 2
    assert bucket.actual_yes_frequency == pytest.approx(0.5)


# --------------------------------------------------------------------------
# snapshot_level_report - deliberately NOT the same as the market-level numbers
# --------------------------------------------------------------------------


def test_snapshot_level_report_counts_every_row_unlike_market_level():
    resolved = [_resolved(market_id="m1", winning_outcome="Yes")]
    snapshots = [_snap(computed_at=T0 + timedelta(minutes=i), classification="COMPOUND") for i in range(22)]
    result = snapshot_level_report(resolved, {"m1": snapshots})

    assert result["COMPOUND"].snapshot_n == 22  # intentionally NOT 1 - this is the snapshot view


def test_snapshot_level_report_ignores_markets_without_a_winning_outcome():
    resolved = [ResolvedMarket(market_id="m1", resolved_at=None, winning_outcome=None)]
    snapshots = [_snap(computed_at=T0)]
    result = snapshot_level_report(resolved, {"m1": snapshots})
    assert result == {}


# --------------------------------------------------------------------------
# End-to-end integration against a real (temp) SQLite DB via the actual
# repositories, mirroring tests/test_check_pending_resolutions.py's pattern.
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "eval_test.db")
    database.init_schema()
    return database


def _seed_market(db: Database, market_id: str, category: str) -> None:
    market_repo = MarketRepository(db)
    try:
        market_repo.upsert_market(
            Market(id=market_id, question="Q", category=category, subcategory="btc_up_down")
        )
    finally:
        market_repo.close()


def _insert_analysis(db: Database, **overrides) -> None:
    defaults = dict(
        market_id="m1",
        computed_at=T0,
        category="crypto",
        market_implied_probability=0.55,
        estimated_probability=0.6,
        raw_edge=0.05,
        adjusted_edge=0.03,
        data_quality=DataQualityState.SUFFICIENT,
        quality_gate_passed=True,
        risk_gate_passed=True,
        score=70.0,
        classification="COMPOUND",
    )
    defaults.update(overrides)
    repo = AnalysisRepository(db)
    try:
        repo.insert(AnalysisRecord(**defaults))
    finally:
        repo.close()


def _insert_resolution(db: Database, **overrides) -> None:
    defaults = dict(
        market_id="m1",
        resolution_status=ResolutionStatus.RESOLVED,
        resolved_at=T0 + timedelta(hours=10),
        winning_outcome_index=0,
        winning_outcome="Yes",
        raw_resolution_json="{}",
        checked_at=T0 + timedelta(hours=10),
    )
    defaults.update(overrides)
    repo = ResolutionRepository(db)
    try:
        repo.upsert(Resolution(**defaults))
    finally:
        repo.close()


def test_end_to_end_reproduces_the_users_real_scenario(db):
    """market 3257346-style: 22 COMPOUND + 2 WATCH snapshots, resolves Yes.
    The full DB -> report pipeline must still count it as exactly 1 market."""
    _seed_market(db, "m1", category="politics")
    for i in range(22):
        _insert_analysis(db, computed_at=T0 + timedelta(hours=i), classification="COMPOUND", estimated_probability=0.7)
    for i in range(2):
        _insert_analysis(db, computed_at=T0 + timedelta(hours=22 + i), classification="WATCH", estimated_probability=0.72)
    _insert_resolution(db, resolved_at=T0 + timedelta(hours=30), winning_outcome="Yes")

    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row
    try:
        report = run_evaluation(conn)
    finally:
        conn.close()

    assert "primary_evaluations_built: 1" in report
    assert "excluded_no_analysis_before_resolution: 0" in report
    # The 24 snapshots must never show up as the market-level sample size.
    assert "unique_markets = 24" not in report


def test_end_to_end_excludes_market_resolved_before_any_analysis(db):
    _seed_market(db, "m1", category="crypto")
    _insert_analysis(db, computed_at=T0 + timedelta(hours=5))  # after resolution
    _insert_resolution(db, resolved_at=T0, winning_outcome="Yes")

    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row
    try:
        report = run_evaluation(conn)
    finally:
        conn.close()

    assert "primary_evaluations_built: 0" in report
    assert "excluded_no_analysis_before_resolution: 1" in report
    assert "['m1']" in report


def test_end_to_end_two_markets_multiple_snapshots_each_report_counts_correctly(db):
    _seed_market(db, "m1", category="politics")
    _seed_market(db, "m2", category="sports")
    for i in range(5):
        _insert_analysis(db, market_id="m1", computed_at=T0 + timedelta(hours=i), classification="WATCH")
    for i in range(3):
        _insert_analysis(db, market_id="m2", computed_at=T0 + timedelta(hours=i), classification="WATCH")
    _insert_resolution(db, market_id="m1", resolved_at=T0 + timedelta(hours=10), winning_outcome="Yes")
    _insert_resolution(db, market_id="m2", resolved_at=T0 + timedelta(hours=10), winning_outcome="No")

    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row
    try:
        report = run_evaluation(conn)
    finally:
        conn.close()

    assert "primary_evaluations_built: 2" in report
    assert "WATCH: unique_markets=2" in report
