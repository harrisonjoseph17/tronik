"""Market-level outcome evaluation report.

Why this exists: `analyses` is an intentional time-series table (the
recurring scan/analyze/resolve timer writes a fresh snapshot for every
still-open market roughly every 15 minutes), so a single market can
accumulate dozens of rows before it resolves. A naive join of `analyses`
against `resolutions` therefore treats one market's 22 COMPOUND snapshots
as 22 independent "predictions" - wildly overstating sample size and
double/triple-counting the same underlying market. This script corrects
for that: every metric here is computed PER MARKET, never per snapshot.

Primary-snapshot rule (the one number used to "grade" each resolved
market): the LAST analysis row for that market with `computed_at` at or
before the market's `resolved_at`. Never use a snapshot computed after
resolution - that would leak the outcome into the "prediction". A market
with no analysis at or before its resolution time is excluded from the
primary evaluation and its market_id is reported under the exclusion
count, not silently dropped.

Binary directional/Brier metrics only apply where `winning_outcome` is
exactly "Yes" or exactly "No" - Gamma outcome strings aren't guaranteed to
be binary (e.g. team-vs-team sports markets), so anything else is counted
in the classification-level market counts and the COMPOUND/WATCH detail
table, but excluded from the binary sample (with its own reported count).

`Classification` (app/analysis/classify.py) has 5 values, not 4:
COMPOUND, ASYMMETRIC, WATCH, NO_TRADE, AVOID. All 5 are reported at the
market-count level so none are silently dropped; the detailed
correct/incorrect/accuracy/average-field breakdown is only produced for
COMPOUND/WATCH, per the requesting spec.

A second, clearly separate SNAPSHOT-level section is also printed -
labeled "SNAPSHOT ANALYSIS - NOT INDEPENDENT TRADES" - showing the same
kind of aggregation but keyed per analysis row instead of per market, so
probability/edge drift over time is visible without ever being confused
for the market-level (primary) numbers used for grading.

Late-stage demotion note: the literal primary-snapshot rule above (last
snapshot at or before resolved_at) can land on a market's exact moment of
edge convergence - as a market approaches actual resolution, its own
market_implied_probability converges toward the true outcome too, so
adjusted_edge naturally shrinks toward zero and risk_gate.py's
insufficient_edge check (or another gate condition) can correctly force
NO_TRADE/AVOID in the final snapshot even though the market showed real
COMPOUND/WATCH signal for hours beforehand. This is not a bug - it is the
gate working as designed - but it means a 0-count for COMPOUND/WATCH at
the primary-evaluation level can understate how often the system produced
real earlier signal. detect_late_stage_demotions() surfaces this
separately (which markets had a COMPOUND/WATCH snapshot at some point
before resolution but were demoted by the time of their primary
evaluation, and why) without changing the primary numbers themselves or
touching any gate/scoring logic.

Read-only: no writes to any table, no schema changes, no calls to
app/analysis/*. Queries analyses/resolutions/markets directly via
sqlite3, the same read-only-script pattern as
scripts/api_probe_resolutions.py, since AnalysisRepository has no
"fetch all analyses for many markets" method (only per-market
list_for_market/list_by_classification, both capped by a `limit`).

Run with: python scripts/evaluate_resolutions.py [--db-path PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.loader import load_config  # noqa: E402

BINARY_OUTCOMES = ("Yes", "No")
ALL_CLASSIFICATIONS = ("COMPOUND", "ASYMMETRIC", "WATCH", "NO_TRADE", "AVOID")
DETAILED_CLASSIFICATIONS = ("COMPOUND", "WATCH")
DEFAULT_CALIBRATION_BIN_WIDTH = 0.2


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisSnapshot:
    market_id: str
    computed_at: datetime
    category: str | None
    market_implied_probability: float | None
    estimated_probability: float | None
    raw_edge: float | None
    adjusted_edge: float | None
    score: float | None
    classification: str
    risk_gate_reasons: list[str] = field(default_factory=list)
    quality_gate_reason: str | None = None


@dataclass(frozen=True)
class ResolvedMarket:
    market_id: str
    resolved_at: datetime | None
    winning_outcome: str | None


@dataclass(frozen=True)
class PrimaryEvaluation:
    market_id: str
    resolved_at: datetime
    winning_outcome: str
    snapshot: AnalysisSnapshot

    @property
    def is_binary(self) -> bool:
        return self.winning_outcome in BINARY_OUTCOMES

    @property
    def predicted_side(self) -> str | None:
        if self.snapshot.estimated_probability is None:
            return None
        return derive_predicted_side(self.snapshot.estimated_probability)

    @property
    def correct(self) -> bool | None:
        if not self.is_binary or self.predicted_side is None:
            return None
        return self.predicted_side == self.winning_outcome

    @property
    def brier(self) -> float | None:
        if not self.is_binary or self.snapshot.estimated_probability is None:
            return None
        return brier_score(self.snapshot.estimated_probability, self.winning_outcome)


@dataclass
class ClassificationStats:
    classification: str
    unique_markets: int = 0
    binary_n: int = 0
    correct: int = 0
    incorrect: int = 0
    accuracy: float | None = None
    avg_estimated_probability: float | None = None
    avg_market_implied_probability: float | None = None
    avg_raw_edge: float | None = None
    avg_adjusted_edge: float | None = None
    avg_score: float | None = None


@dataclass
class CategoryStats:
    category: str
    binary_n: int = 0
    correct: int = 0
    accuracy: float | None = None
    brier: float | None = None


@dataclass
class CalibrationBin:
    low: float
    high: float
    n: int = 0
    avg_predicted: float | None = None
    actual_yes_frequency: float | None = None


# --------------------------------------------------------------------------
# Pure logic (no I/O) - this is what tests/test_evaluate_resolutions.py
# exercises directly.
# --------------------------------------------------------------------------


def select_primary_snapshot(
    snapshots: list[AnalysisSnapshot], resolved_at: datetime | None
) -> AnalysisSnapshot | None:
    """The last analysis snapshot at or before resolution. Never selects a
    snapshot computed after resolved_at. Returns None if no snapshot
    qualifies (market_id -> primary evaluation exclusion)."""
    if resolved_at is None:
        return None
    eligible = [s for s in snapshots if s.computed_at <= resolved_at]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.computed_at)


def derive_predicted_side(estimated_probability: float) -> str:
    return "Yes" if estimated_probability >= 0.5 else "No"


def brier_score(estimated_probability: float, winning_outcome: str) -> float:
    actual = 1.0 if winning_outcome == "Yes" else 0.0
    return (estimated_probability - actual) ** 2


def build_primary_evaluations(
    resolved_markets: list[ResolvedMarket],
    analyses_by_market: dict[str, list[AnalysisSnapshot]],
) -> tuple[list[PrimaryEvaluation], list[str]]:
    """One PrimaryEvaluation per resolved market - never per snapshot, no
    matter how many analyses rows that market accumulated. Returns
    (evaluations, excluded_market_ids) so exclusions are always visible,
    never silently dropped."""
    evaluations: list[PrimaryEvaluation] = []
    excluded: list[str] = []
    for market in resolved_markets:
        if market.resolved_at is None or market.winning_outcome is None:
            excluded.append(market.market_id)
            continue
        snapshots = analyses_by_market.get(market.market_id, [])
        primary = select_primary_snapshot(snapshots, market.resolved_at)
        if primary is None:
            excluded.append(market.market_id)
            continue
        evaluations.append(
            PrimaryEvaluation(
                market_id=market.market_id,
                resolved_at=market.resolved_at,
                winning_outcome=market.winning_outcome,
                snapshot=primary,
            )
        )
    return evaluations, excluded


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def aggregate_by_classification(
    evaluations: list[PrimaryEvaluation],
) -> dict[str, ClassificationStats]:
    """Market-level: each evaluation is exactly one market (guaranteed by
    build_primary_evaluations), so unique_markets here is a true unique-
    market count, not a snapshot count - repeated COMPOUND snapshots for
    one market can never inflate this beyond 1 per market."""
    stats: dict[str, ClassificationStats] = {
        c: ClassificationStats(classification=c) for c in ALL_CLASSIFICATIONS
    }
    for classification in {e.snapshot.classification for e in evaluations} - set(stats):
        stats[classification] = ClassificationStats(classification=classification)

    by_classification: dict[str, list[PrimaryEvaluation]] = {}
    for evaluation in evaluations:
        by_classification.setdefault(evaluation.snapshot.classification, []).append(evaluation)

    for classification, group in by_classification.items():
        s = stats[classification]
        s.unique_markets = len(group)
        binary = [e for e in group if e.correct is not None]
        s.binary_n = len(binary)
        s.correct = sum(1 for e in binary if e.correct)
        s.incorrect = s.binary_n - s.correct
        s.accuracy = (s.correct / s.binary_n) if s.binary_n else None
        s.avg_estimated_probability = _mean([e.snapshot.estimated_probability for e in group])
        s.avg_market_implied_probability = _mean(
            [e.snapshot.market_implied_probability for e in group]
        )
        s.avg_raw_edge = _mean([e.snapshot.raw_edge for e in group])
        s.avg_adjusted_edge = _mean([e.snapshot.adjusted_edge for e in group])
        s.avg_score = _mean([e.snapshot.score for e in group])

    return stats


def aggregate_by_category(evaluations: list[PrimaryEvaluation]) -> dict[str, CategoryStats]:
    """Binary-only, market-level - one evaluation per market, grouped by
    the category recorded on that market's primary snapshot."""
    by_category: dict[str, list[PrimaryEvaluation]] = {}
    for evaluation in evaluations:
        if evaluation.correct is None:
            continue
        category = evaluation.snapshot.category or "unknown"
        by_category.setdefault(category, []).append(evaluation)

    result: dict[str, CategoryStats] = {}
    for category, group in by_category.items():
        s = CategoryStats(category=category)
        s.binary_n = len(group)
        s.correct = sum(1 for e in group if e.correct)
        s.accuracy = s.correct / s.binary_n if s.binary_n else None
        s.brier = _mean([e.brier for e in group])
        result[category] = s
    return result


def calibration_bins(
    evaluations: list[PrimaryEvaluation], bin_width: float = DEFAULT_CALIBRATION_BIN_WIDTH
) -> list[CalibrationBin]:
    """Market-level calibration: one observation per resolved market (its
    primary snapshot's estimated_probability vs. whether the outcome was
    actually "Yes"), bucketed by predicted probability. Covers every
    binary-resolved market regardless of classification, not just
    COMPOUND/WATCH, to show calibration across the full probability range."""
    binary = [e for e in evaluations if e.is_binary and e.snapshot.estimated_probability is not None]
    n_bins = max(1, round(1.0 / bin_width))
    bins = [
        CalibrationBin(low=round(i * bin_width, 10), high=round((i + 1) * bin_width, 10))
        for i in range(n_bins)
    ]

    members_by_bin: list[list[PrimaryEvaluation]] = [[] for _ in range(n_bins)]
    for evaluation in binary:
        p = evaluation.snapshot.estimated_probability
        # Single membership test (boundary comparison, not division) so bin.n
        # and the avg/frequency computed from `members` can never disagree.
        idx = next(
            (i for i, b in enumerate(bins) if b.low <= p < b.high or (b.high >= 1.0 and p == 1.0)),
            n_bins - 1,
        )
        members_by_bin[idx].append(evaluation)

    for b, members in zip(bins, members_by_bin):
        b.n = len(members)
        if members:
            b.avg_predicted = _mean([e.snapshot.estimated_probability for e in members])
            b.actual_yes_frequency = sum(1 for e in members if e.winning_outcome == "Yes") / len(
                members
            )

    return bins


@dataclass
class SnapshotLevelStats:
    classification: str
    snapshot_n: int = 0
    binary_n: int = 0
    correct: int = 0
    accuracy: float | None = None
    avg_estimated_probability: float | None = None
    avg_adjusted_edge: float | None = None


def snapshot_level_report(
    resolved_markets: list[ResolvedMarket],
    analyses_by_market: dict[str, list[AnalysisSnapshot]],
) -> dict[str, SnapshotLevelStats]:
    """SNAPSHOT ANALYSIS - NOT INDEPENDENT TRADES. Every analysis row for
    every resolved market is treated as its own observation here - this
    intentionally reproduces the "22 COMPOUND snapshots = 22 rows" view so
    probability/edge drift over time is visible, but these numbers must
    never be read as prediction accuracy/sample size (see the market-level
    aggregate_by_classification for that)."""
    outcome_by_market = {m.market_id: m.winning_outcome for m in resolved_markets if m.winning_outcome}

    by_classification: dict[str, list[tuple[AnalysisSnapshot, str]]] = {}
    for market_id, snapshots in analyses_by_market.items():
        winning_outcome = outcome_by_market.get(market_id)
        if winning_outcome is None:
            continue
        for snapshot in snapshots:
            by_classification.setdefault(snapshot.classification, []).append(
                (snapshot, winning_outcome)
            )

    result: dict[str, SnapshotLevelStats] = {}
    for classification, rows in by_classification.items():
        s = SnapshotLevelStats(classification=classification)
        s.snapshot_n = len(rows)
        binary_rows = [
            (snap, outcome) for snap, outcome in rows if outcome in BINARY_OUTCOMES and snap.estimated_probability is not None
        ]
        s.binary_n = len(binary_rows)
        s.correct = sum(
            1 for snap, outcome in binary_rows if derive_predicted_side(snap.estimated_probability) == outcome
        )
        s.accuracy = (s.correct / s.binary_n) if s.binary_n else None
        s.avg_estimated_probability = _mean([snap.estimated_probability for snap, _ in rows])
        s.avg_adjusted_edge = _mean([snap.adjusted_edge for snap, _ in rows])
        result[classification] = s

    return result


@dataclass(frozen=True)
class LateStageDemotion:
    """A market that showed real COMPOUND/WATCH signal at some point before
    resolution but whose primary (last-before-resolution) snapshot had
    already been demoted to NO_TRADE/AVOID by the time it resolved."""

    market_id: str
    best_prior_classification: str
    best_prior_score: float | None
    best_prior_computed_at: datetime
    primary_classification: str
    primary_reasons: list[str]


def detect_late_stage_demotions(
    evaluations: list[PrimaryEvaluation],
    analyses_by_market: dict[str, list[AnalysisSnapshot]],
) -> list[LateStageDemotion]:
    """Does NOT change the primary evaluation or any of its numbers - this
    only explains, for markets whose PRIMARY snapshot is NOT COMPOUND/WATCH,
    whether an earlier (still pre-resolution) snapshot for that same market
    ever was COMPOUND/WATCH, and why the primary one wasn't. Surfaces the
    "edge converged right before resolution" pattern without touching any
    gate/scoring logic."""
    demotions: list[LateStageDemotion] = []
    for evaluation in evaluations:
        if evaluation.snapshot.classification in DETAILED_CLASSIFICATIONS:
            continue  # primary itself is COMPOUND/WATCH - nothing to flag

        prior_signal = [
            s
            for s in analyses_by_market.get(evaluation.market_id, [])
            if s.classification in DETAILED_CLASSIFICATIONS
            and s.computed_at <= evaluation.resolved_at
            and s.computed_at < evaluation.snapshot.computed_at
        ]
        if not prior_signal:
            continue

        best = max(prior_signal, key=lambda s: (s.score if s.score is not None else float("-inf")))
        reasons = list(evaluation.snapshot.risk_gate_reasons)
        if evaluation.snapshot.quality_gate_reason:
            reasons.append(evaluation.snapshot.quality_gate_reason)

        demotions.append(
            LateStageDemotion(
                market_id=evaluation.market_id,
                best_prior_classification=best.classification,
                best_prior_score=best.score,
                best_prior_computed_at=best.computed_at,
                primary_classification=evaluation.snapshot.classification,
                primary_reasons=reasons,
            )
        )

    return demotions


# --------------------------------------------------------------------------
# DB-facing helpers
# --------------------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_reasons(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def fetch_resolution_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT resolution_status, COUNT(*) AS n FROM resolutions GROUP BY resolution_status"
    ).fetchall()
    return {row["resolution_status"]: row["n"] for row in rows}


def fetch_resolved_markets(conn: sqlite3.Connection) -> list[ResolvedMarket]:
    rows = conn.execute(
        "SELECT market_id, resolved_at, winning_outcome FROM resolutions "
        "WHERE resolution_status = 'resolved'"
    ).fetchall()
    return [
        ResolvedMarket(
            market_id=row["market_id"],
            resolved_at=_parse_dt(row["resolved_at"]),
            winning_outcome=row["winning_outcome"],
        )
        for row in rows
    ]


def fetch_analyses_for_markets(
    conn: sqlite3.Connection, market_ids: list[str]
) -> dict[str, list[AnalysisSnapshot]]:
    """No `limit` - a market with dozens of snapshots must never be
    truncated, or the primary-snapshot selection could silently miss the
    true latest-before-resolution row."""
    result: dict[str, list[AnalysisSnapshot]] = {market_id: [] for market_id in market_ids}
    for market_id in market_ids:
        rows = conn.execute(
            "SELECT market_id, computed_at, category, market_implied_probability, "
            "estimated_probability, raw_edge, adjusted_edge, score, classification, "
            "risk_gate_reasons_json, quality_gate_reason "
            "FROM analyses WHERE market_id = ? ORDER BY computed_at ASC",
            (market_id,),
        ).fetchall()
        result[market_id] = [
            AnalysisSnapshot(
                market_id=row["market_id"],
                computed_at=_parse_dt(row["computed_at"]),
                category=row["category"],
                market_implied_probability=row["market_implied_probability"],
                estimated_probability=row["estimated_probability"],
                raw_edge=row["raw_edge"],
                adjusted_edge=row["adjusted_edge"],
                score=row["score"],
                classification=row["classification"],
                risk_gate_reasons=_parse_reasons(row["risk_gate_reasons_json"]),
                quality_gate_reason=row["quality_gate_reason"],
            )
            for row in rows
        ]
    return result


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(
    status_counts: dict[str, int],
    evaluations: list[PrimaryEvaluation],
    excluded_market_ids: list[str],
    classification_stats: dict[str, ClassificationStats],
    category_stats: dict[str, CategoryStats],
    bins: list[CalibrationBin],
    snapshot_stats: dict[str, SnapshotLevelStats],
    demotions: list[LateStageDemotion],
) -> str:
    lines: list[str] = []
    out = lines.append

    out("=" * 78)
    out("MARKET-LEVEL OUTCOME EVALUATION (primary snapshot per market)")
    out("=" * 78)
    out("")
    out("-- resolutions table summary --")
    for status in ("resolved", "unresolved", "invalid"):
        out(f"  {status}: {status_counts.get(status, 0)}")
    out("")
    out(f"unique_resolved_markets_with_a_winning_outcome: {len(evaluations) + len(excluded_market_ids)}")
    out(f"primary_evaluations_built: {len(evaluations)}")
    out(f"excluded_no_analysis_before_resolution: {len(excluded_market_ids)}")
    if excluded_market_ids:
        out(f"  excluded_market_ids: {excluded_market_ids}")
    out("")

    out("-- classification market-level counts (unique resolved markets) --")
    for classification in ALL_CLASSIFICATIONS:
        s = classification_stats.get(classification)
        out(f"  {classification}: unique_markets={s.unique_markets if s else 0}")
    extra = set(classification_stats) - set(ALL_CLASSIFICATIONS)
    for classification in sorted(extra):
        s = classification_stats[classification]
        out(f"  {classification} (unexpected value, flagged): unique_markets={s.unique_markets}")
    out("")

    out("-- late-stage demotion note (does not change the numbers above) --")
    out("  Markets below showed real COMPOUND/WATCH signal at some point before")
    out("  resolution, but their PRIMARY (last-before-resolution) snapshot had")
    out("  already been gated back to NO_TRADE/AVOID by the time they resolved -")
    out("  typically because adjusted_edge naturally converges toward zero as the")
    out("  market's own price catches up to the outcome. This is risk_gate.py")
    out("  working as designed, not a bug - see the module docstring above for")
    out("  the full explanation. The primary-evaluation numbers above are")
    out("  unchanged; this section only explains part of why COMPOUND/WATCH")
    out("  counts can look lower than a naive (non-market-level) join suggests.")
    out(f"  late_stage_demotions: {len(demotions)}")
    if demotions:
        reason_counts: dict[str, int] = {}
        for d in demotions:
            for reason in (d.primary_reasons or ["(no gate reason recorded)"]):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        out(f"  primary_reason_breakdown: {dict(sorted(reason_counts.items(), key=lambda kv: -kv[1]))}")
        for d in demotions:
            out(
                f"    {d.market_id}: reached {d.best_prior_classification} "
                f"(score={_fmt(d.best_prior_score, 2)}) at {d.best_prior_computed_at.isoformat()}, "
                f"primary={d.primary_classification} reasons={d.primary_reasons}"
            )
    out("")

    out("-- COMPOUND / WATCH detailed performance (market-level) --")
    for classification in DETAILED_CLASSIFICATIONS:
        s = classification_stats.get(classification) or ClassificationStats(classification)
        out(f"  {classification}:")
        out(f"    unique_markets = {s.unique_markets}")
        out(f"    binary_n       = {s.binary_n}")
        out(f"    correct        = {s.correct}")
        out(f"    incorrect      = {s.incorrect}")
        out(f"    accuracy       = {_fmt(s.accuracy)}")
        out(f"    avg_estimated_probability      = {_fmt(s.avg_estimated_probability)}")
        out(f"    avg_market_implied_probability = {_fmt(s.avg_market_implied_probability)}")
        out(f"    avg_raw_edge                   = {_fmt(s.avg_raw_edge)}")
        out(f"    avg_adjusted_edge              = {_fmt(s.avg_adjusted_edge)}")
        out(f"    avg_score                      = {_fmt(s.avg_score)}")
    out("")

    out("-- COMPOUND / WATCH market-level detail table --")
    header = (
        f"  {'market_id':<12} {'class':<9} {'computed_at':<20} {'resolved_at':<20} "
        f"{'winner':<8} {'pred':<6} {'correct':<8} {'est_p':<7} {'impl_p':<7} "
        f"{'raw_edge':<9} {'adj_edge':<9} {'score':<7}"
    )
    out(header)
    detail_rows = [e for e in evaluations if e.snapshot.classification in DETAILED_CLASSIFICATIONS]
    detail_rows.sort(key=lambda e: (e.snapshot.classification, e.market_id))
    for e in detail_rows:
        out(
            f"  {e.market_id:<12} {e.snapshot.classification:<9} "
            f"{e.snapshot.computed_at.isoformat():<20} {e.resolved_at.isoformat():<20} "
            f"{e.winning_outcome:<8} {str(e.predicted_side):<6} {str(e.correct):<8} "
            f"{_fmt(e.snapshot.estimated_probability, 3):<7} "
            f"{_fmt(e.snapshot.market_implied_probability, 3):<7} "
            f"{_fmt(e.snapshot.raw_edge, 3):<9} {_fmt(e.snapshot.adjusted_edge, 3):<9} "
            f"{_fmt(e.snapshot.score, 2):<7}"
        )
    if not detail_rows:
        out("  (none)")
    out("")

    binary_evals = [e for e in evaluations if e.correct is not None]
    non_binary_n = len(evaluations) - len(binary_evals)
    out("-- binary directional performance (ALL classifications, market-level) --")
    n = len(binary_evals)
    correct = sum(1 for e in binary_evals if e.correct)
    accuracy = (correct / n) if n else None
    brier = _mean([e.brier for e in binary_evals])
    out(f"  binary_sample_size = {n}")
    out(f"  excluded_non_binary_outcome = {non_binary_n}")
    out(f"  correct = {correct}")
    out(f"  accuracy = {_fmt(accuracy)}")
    out(f"  brier_score = {_fmt(brier)}")
    out("")

    out("-- category-level binary performance (market-level) --")
    for category in ("politics", "crypto", "sports"):
        s = category_stats.get(category)
        if s is None:
            out(f"  {category}: n=0")
        else:
            out(
                f"  {category}: n={s.binary_n} correct={s.correct} "
                f"accuracy={_fmt(s.accuracy)} brier={_fmt(s.brier)}"
            )
    other_categories = sorted(set(category_stats) - {"politics", "crypto", "sports"})
    for category in other_categories:
        s = category_stats[category]
        out(
            f"  {category}: n={s.binary_n} correct={s.correct} "
            f"accuracy={_fmt(s.accuracy)} brier={_fmt(s.brier)}"
        )
    out("")

    out("-- probability calibration (market-level, all binary-resolved markets) --")
    for b in bins:
        out(
            f"  [{b.low:.1f}, {b.high:.1f}): n={b.n} "
            f"avg_predicted={_fmt(b.avg_predicted, 3)} "
            f"actual_yes_frequency={_fmt(b.actual_yes_frequency, 3)}"
        )
    out("")

    out("=" * 78)
    out("SNAPSHOT ANALYSIS - NOT INDEPENDENT TRADES")
    out("(every analysis row counted separately - shows drift over time,")
    out(" NOT prediction sample size. Do not use these n's for accuracy claims.)")
    out("=" * 78)
    for classification in ALL_CLASSIFICATIONS:
        s = snapshot_stats.get(classification)
        if s is None:
            out(f"  {classification}: snapshot_n=0")
            continue
        out(
            f"  {classification}: snapshot_n={s.snapshot_n} binary_n={s.binary_n} "
            f"correct={s.correct} accuracy={_fmt(s.accuracy)} "
            f"avg_estimated_probability={_fmt(s.avg_estimated_probability)} "
            f"avg_adjusted_edge={_fmt(s.avg_adjusted_edge)}"
        )

    return "\n".join(lines)


def run_evaluation(conn: sqlite3.Connection) -> str:
    status_counts = fetch_resolution_status_counts(conn)
    resolved_markets = fetch_resolved_markets(conn)
    analyses_by_market = fetch_analyses_for_markets(conn, [m.market_id for m in resolved_markets])

    evaluations, excluded = build_primary_evaluations(resolved_markets, analyses_by_market)
    classification_stats = aggregate_by_classification(evaluations)
    category_stats = aggregate_by_category(evaluations)
    bins = calibration_bins(evaluations)
    snapshot_stats = snapshot_level_report(resolved_markets, analyses_by_market)
    demotions = detect_late_stage_demotions(evaluations, analyses_by_market)

    return render_report(
        status_counts,
        evaluations,
        excluded,
        classification_stats,
        category_stats,
        bins,
        snapshot_stats,
        demotions,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None, help="Override the configured db_path")
    args = parser.parse_args()

    db_path = args.db_path or load_config().db_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        print(run_evaluation(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
