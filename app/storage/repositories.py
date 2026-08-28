"""Read/write access to the markets + market_snapshots tables.

All queries are parameterized (no string interpolation). first_seen_at is
preserved across upserts by simply never including it in the UPDATE SET
clause - only INSERT sets it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.storage.analysis_models import AnalysisRecord, DataQualityState, Features
from app.storage.database import Database
from app.storage.models import Market
from app.storage.paper_trade_models import PaperTrade, PaperTradeStatus
from app.storage.resolution_models import Resolution, ResolutionStatus
from app.storage.signal_journal_models import SignalJournalEntry

_UPSERT_SQL = """
INSERT INTO markets (
    market_id, event_id, question, slug, category, subcategory,
    outcomes_json, outcome_prices_json, clob_token_ids_json,
    volume, volume_24hr, liquidity, best_bid, best_ask, spread, mid_price,
    active, closed, archived, enable_order_book,
    start_date, end_date, status, is_duplicate, duplicate_of, filter_reason,
    first_seen_at, last_seen_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market_id) DO UPDATE SET
    event_id=excluded.event_id,
    question=excluded.question,
    slug=excluded.slug,
    category=excluded.category,
    subcategory=excluded.subcategory,
    outcomes_json=excluded.outcomes_json,
    outcome_prices_json=excluded.outcome_prices_json,
    clob_token_ids_json=excluded.clob_token_ids_json,
    volume=excluded.volume,
    volume_24hr=excluded.volume_24hr,
    liquidity=excluded.liquidity,
    best_bid=excluded.best_bid,
    best_ask=excluded.best_ask,
    spread=excluded.spread,
    mid_price=excluded.mid_price,
    active=excluded.active,
    closed=excluded.closed,
    archived=excluded.archived,
    enable_order_book=excluded.enable_order_book,
    start_date=excluded.start_date,
    end_date=excluded.end_date,
    status=excluded.status,
    is_duplicate=excluded.is_duplicate,
    duplicate_of=excluded.duplicate_of,
    filter_reason=excluded.filter_reason,
    last_seen_at=excluded.last_seen_at,
    updated_at=excluded.updated_at
"""

_INSERT_SNAPSHOT_SQL = """
INSERT INTO market_snapshots (
    market_id, captured_at, category, status,
    volume, volume_24hr, liquidity, best_bid, best_ask, spread, mid_price,
    filter_reason, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_FEATURES_SQL = """
INSERT INTO features (
    market_id, computed_at, market_implied_probability, volume, volume_24hr,
    liquidity, spread, time_to_resolution_hours, market_age_hours,
    snapshot_count, history_span_hours, price_change_recent, volume_change_recent,
    order_book_imbalance, clob_data_available, data_quality, data_quality_reasons_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ANALYSIS_SQL = """
INSERT INTO analyses (
    market_id, feature_id, computed_at, category,
    market_implied_probability, estimated_probability, confidence, adjustments_json,
    raw_edge, adjusted_edge, expected_value, category_reliability,
    data_quality, quality_gate_passed, quality_gate_reason,
    risk_gate_passed, risk_gate_reasons_json, score, classification
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


def _dt_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class MarketRepository:
    def __init__(self, db: Database):
        self._conn = db.connect()

    def close(self) -> None:
        self._conn.close()

    def upsert_market(self, market: Market, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._upsert_one(market, now)
        self._conn.commit()

    def bulk_upsert_markets(self, markets: list[Market], *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        for market in markets:
            self._upsert_one(market, now)
        self._conn.commit()
        return len(markets)

    def insert_snapshot(
        self, market: Market, *, captured_at: datetime | None = None, raw_json: str | None = None
    ) -> None:
        captured_at = captured_at or datetime.now(timezone.utc)
        self._insert_snapshot_one(market, captured_at, raw_json)
        self._conn.commit()

    def record_scan(self, markets: list[Market], *, now: datetime | None = None) -> int:
        """Upsert current state + append a snapshot for every market, in one transaction."""
        now = now or datetime.now(timezone.utc)
        for market in markets:
            self._upsert_one(market, now)
            self._insert_snapshot_one(market, now, raw_json=None)
        self._conn.commit()
        return len(markets)

    def get_market(self, market_id: str) -> Market | None:
        row = self._conn.execute(
            "SELECT * FROM markets WHERE market_id = ?", (market_id,)
        ).fetchone()
        return self._row_to_market(row) if row else None

    def list_markets(
        self,
        *,
        category: str | None = None,
        subcategory: str | None = None,
        status: str | None = None,
        included_only: bool = False,
        limit: int = 100,
    ) -> list[Market]:
        query = "SELECT * FROM markets WHERE 1=1"
        params: list = []
        if category is not None:
            query += " AND category = ?"
            params.append(category)
        if subcategory is not None:
            query += " AND subcategory = ?"
            params.append(subcategory)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if included_only:
            query += " AND filter_reason IS NULL"
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_market(row) for row in rows]

    def list_snapshots(self, market_id: str, *, limit: int = 50) -> list[dict]:
        """Most-recent-first snapshot history for one market, used by
        features.py to derive price/volume change over the available
        history span."""
        rows = self._conn.execute(
            """
            SELECT captured_at, mid_price, volume, volume_24hr, liquidity, spread
            FROM market_snapshots
            WHERE market_id = ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (market_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_by_category(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM markets GROUP BY category"
        ).fetchall()
        return {row["category"]: row["cnt"] for row in rows}

    def _upsert_one(self, market: Market, now: datetime) -> None:
        now_text = now.isoformat()
        self._conn.execute(
            _UPSERT_SQL,
            (
                market.id,
                market.event_id,
                market.question,
                market.slug,
                market.category,
                market.subcategory,
                json.dumps(market.outcomes),
                json.dumps(market.outcome_prices),
                json.dumps(market.clob_token_ids),
                market.volume,
                market.volume_24hr,
                market.liquidity,
                market.best_bid,
                market.best_ask,
                market.spread,
                market.mid_price,
                _bool_to_int(market.active),
                _bool_to_int(market.closed),
                _bool_to_int(market.archived),
                _bool_to_int(market.enable_order_book),
                _dt_to_text(market.start_date),
                _dt_to_text(market.end_date),
                market.status,
                _bool_to_int(market.is_duplicate),
                market.duplicate_of,
                market.filter_reason,
                now_text,
                now_text,
                now_text,
            ),
        )

    def _insert_snapshot_one(
        self, market: Market, captured_at: datetime, raw_json: str | None
    ) -> None:
        self._conn.execute(
            _INSERT_SNAPSHOT_SQL,
            (
                market.id,
                captured_at.isoformat(),
                market.category,
                market.status,
                market.volume,
                market.volume_24hr,
                market.liquidity,
                market.best_bid,
                market.best_ask,
                market.spread,
                market.mid_price,
                market.filter_reason,
                raw_json,
            ),
        )

    def _row_to_market(self, row: sqlite3.Row) -> Market:
        return Market(
            id=row["market_id"],
            event_id=row["event_id"],
            question=row["question"],
            slug=row["slug"],
            category=row["category"],
            subcategory=row["subcategory"],
            outcomes=json.loads(row["outcomes_json"]),
            outcome_prices=json.loads(row["outcome_prices_json"]),
            clob_token_ids=json.loads(row["clob_token_ids_json"]),
            volume=row["volume"],
            volume_24hr=row["volume_24hr"],
            liquidity=row["liquidity"],
            best_bid=row["best_bid"],
            best_ask=row["best_ask"],
            spread=row["spread"],
            mid_price=row["mid_price"],
            active=bool(row["active"]),
            closed=bool(row["closed"]),
            archived=bool(row["archived"]),
            enable_order_book=bool(row["enable_order_book"]),
            start_date=datetime.fromisoformat(row["start_date"]) if row["start_date"] else None,
            end_date=datetime.fromisoformat(row["end_date"]) if row["end_date"] else None,
            status=row["status"],
            is_duplicate=bool(row["is_duplicate"]),
            duplicate_of=row["duplicate_of"],
            filter_reason=row["filter_reason"],
        )


class FeatureRepository:
    def __init__(self, db: Database):
        self._conn = db.connect()

    def close(self) -> None:
        self._conn.close()

    def insert(self, features: Features) -> int:
        cursor = self._conn.execute(
            _INSERT_FEATURES_SQL,
            (
                features.market_id,
                features.computed_at.isoformat(),
                features.market_implied_probability,
                features.volume,
                features.volume_24hr,
                features.liquidity,
                features.spread,
                features.time_to_resolution_hours,
                features.market_age_hours,
                features.snapshot_count,
                features.history_span_hours,
                features.price_change_recent,
                features.volume_change_recent,
                features.order_book_imbalance,
                _bool_to_int(features.clob_data_available),
                features.data_quality.value,
                json.dumps(features.data_quality_reasons),
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def list_for_market(self, market_id: str, *, limit: int = 20) -> list[Features]:
        rows = self._conn.execute(
            "SELECT * FROM features WHERE market_id = ? ORDER BY computed_at DESC LIMIT ?",
            (market_id, limit),
        ).fetchall()
        return [self._row_to_features(row) for row in rows]

    def _row_to_features(self, row: sqlite3.Row) -> Features:
        return Features(
            market_id=row["market_id"],
            computed_at=datetime.fromisoformat(row["computed_at"]),
            market_implied_probability=row["market_implied_probability"],
            volume=row["volume"],
            volume_24hr=row["volume_24hr"],
            liquidity=row["liquidity"],
            spread=row["spread"],
            time_to_resolution_hours=row["time_to_resolution_hours"],
            market_age_hours=row["market_age_hours"],
            snapshot_count=row["snapshot_count"],
            history_span_hours=row["history_span_hours"],
            price_change_recent=row["price_change_recent"],
            volume_change_recent=row["volume_change_recent"],
            order_book_imbalance=row["order_book_imbalance"],
            clob_data_available=bool(row["clob_data_available"]),
            data_quality=DataQualityState(row["data_quality"]),
            data_quality_reasons=json.loads(row["data_quality_reasons_json"]),
        )


class AnalysisRepository:
    def __init__(self, db: Database):
        self._conn = db.connect()

    def close(self) -> None:
        self._conn.close()

    def insert(self, record: AnalysisRecord) -> int:
        cursor = self._conn.execute(
            _INSERT_ANALYSIS_SQL,
            (
                record.market_id,
                record.feature_id,
                record.computed_at.isoformat(),
                record.category,
                record.market_implied_probability,
                record.estimated_probability,
                record.confidence,
                json.dumps(record.adjustments),
                record.raw_edge,
                record.adjusted_edge,
                record.expected_value,
                record.category_reliability,
                record.data_quality.value,
                _bool_to_int(record.quality_gate_passed),
                record.quality_gate_reason,
                None if record.risk_gate_passed is None else _bool_to_int(record.risk_gate_passed),
                json.dumps(record.risk_gate_reasons),
                record.score,
                record.classification,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def list_for_market(self, market_id: str, *, limit: int = 20) -> list[AnalysisRecord]:
        rows = self._conn.execute(
            "SELECT * FROM analyses WHERE market_id = ? ORDER BY computed_at DESC LIMIT ?",
            (market_id, limit),
        ).fetchall()
        return [self._row_to_analysis(row) for row in rows]

    def get_at(self, market_id: str, computed_at: datetime) -> AnalysisRecord | None:
        """Exact-match point lookup, not subject to list_for_market's
        DESC/LIMIT window - a market's first-ever analysis row (used by
        requirement #19's paper-trade creation to recover raw_edge, which
        signal_journal doesn't store) could otherwise scroll out of that
        window entirely once enough later rows accumulate."""
        row = self._conn.execute(
            "SELECT * FROM analyses WHERE market_id = ? AND computed_at = ?",
            (market_id, computed_at.isoformat()),
        ).fetchone()
        return self._row_to_analysis(row) if row else None

    def list_by_classification(self, classification: str, *, limit: int = 100) -> list[AnalysisRecord]:
        rows = self._conn.execute(
            "SELECT * FROM analyses WHERE classification = ? ORDER BY computed_at DESC LIMIT ?",
            (classification, limit),
        ).fetchall()
        return [self._row_to_analysis(row) for row in rows]

    def _row_to_analysis(self, row: sqlite3.Row) -> AnalysisRecord:
        return AnalysisRecord(
            market_id=row["market_id"],
            feature_id=row["feature_id"],
            computed_at=datetime.fromisoformat(row["computed_at"]),
            category=row["category"],
            market_implied_probability=row["market_implied_probability"],
            estimated_probability=row["estimated_probability"],
            confidence=row["confidence"],
            adjustments=json.loads(row["adjustments_json"]),
            raw_edge=row["raw_edge"],
            adjusted_edge=row["adjusted_edge"],
            expected_value=row["expected_value"],
            category_reliability=row["category_reliability"],
            data_quality=DataQualityState(row["data_quality"]),
            quality_gate_passed=bool(row["quality_gate_passed"]),
            quality_gate_reason=row["quality_gate_reason"],
            risk_gate_passed=None if row["risk_gate_passed"] is None else bool(row["risk_gate_passed"]),
            risk_gate_reasons=json.loads(row["risk_gate_reasons_json"]),
            score=row["score"],
            classification=row["classification"],
        )


_UPSERT_RESOLUTION_SQL = """
INSERT INTO resolutions (
    market_id, resolution_status, resolved_at, winning_outcome_index,
    winning_outcome, raw_resolution_json, checked_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market_id) DO UPDATE SET
    resolution_status=excluded.resolution_status,
    resolved_at=excluded.resolved_at,
    winning_outcome_index=excluded.winning_outcome_index,
    winning_outcome=excluded.winning_outcome,
    raw_resolution_json=excluded.raw_resolution_json,
    checked_at=excluded.checked_at
"""


class ResolutionRepository:
    def __init__(self, db: Database):
        self._conn = db.connect()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, resolution: Resolution) -> None:
        """Idempotent, with one hard rule: once a market's resolution_status
        is 'resolved', nothing about its outcome can ever change - not the
        status, not the winning outcome, not the raw JSON. Only checked_at
        still advances, so it's visible that a check happened without
        disturbing the confirmed record."""
        existing = self.get(resolution.market_id)
        checked_at_text = resolution.checked_at.isoformat()

        if existing is not None and existing.resolution_status == ResolutionStatus.RESOLVED:
            self._conn.execute(
                "UPDATE resolutions SET checked_at = ? WHERE market_id = ?",
                (checked_at_text, resolution.market_id),
            )
            self._conn.commit()
            return

        self._conn.execute(
            _UPSERT_RESOLUTION_SQL,
            (
                resolution.market_id,
                resolution.resolution_status.value,
                resolution.resolved_at.isoformat() if resolution.resolved_at else None,
                resolution.winning_outcome_index,
                resolution.winning_outcome,
                resolution.raw_resolution_json,
                checked_at_text,
            ),
        )
        self._conn.commit()

    def get(self, market_id: str) -> Resolution | None:
        row = self._conn.execute(
            "SELECT * FROM resolutions WHERE market_id = ?", (market_id,)
        ).fetchone()
        return self._row_to_resolution(row) if row else None

    def list_by_status(self, status: ResolutionStatus, *, limit: int = 100) -> list[Resolution]:
        rows = self._conn.execute(
            "SELECT * FROM resolutions WHERE resolution_status = ? ORDER BY checked_at DESC LIMIT ?",
            (status.value, limit),
        ).fetchall()
        return [self._row_to_resolution(row) for row in rows]

    def list_market_ids_pending_check(self, *, limit: int = 500) -> list[str]:
        """Distinct market_ids from analyses with no resolutions row yet, or
        one that isn't (yet) resolved - the candidate set for a check run."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT a.market_id
            FROM analyses a
            LEFT JOIN resolutions r ON r.market_id = a.market_id
            WHERE r.market_id IS NULL OR r.resolution_status != ?
            LIMIT ?
            """,
            (ResolutionStatus.RESOLVED.value, limit),
        ).fetchall()
        return [row["market_id"] for row in rows]

    def _row_to_resolution(self, row: sqlite3.Row) -> Resolution:
        return Resolution(
            market_id=row["market_id"],
            resolution_status=ResolutionStatus(row["resolution_status"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            winning_outcome_index=row["winning_outcome_index"],
            winning_outcome=row["winning_outcome"],
            raw_resolution_json=row["raw_resolution_json"],
            checked_at=datetime.fromisoformat(row["checked_at"]),
        )


_INSERT_SIGNAL_JOURNAL_SQL = """
INSERT OR IGNORE INTO signal_journal (
    market_id, first_signal_at, classification, category, subcategory,
    market_implied_probability, estimated_probability, adjusted_edge, score
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class SignalJournalRepository:
    """Requirement #18: records each market's FIRST actionable
    (COMPOUND/WATCH) analysis snapshot. Simpler than ResolutionRepository's
    upsert - there is no state machine and no field ever advances after the
    first write, not even a "last seen" timestamp. Once a market_id has a
    row, every later call is a pure no-op."""

    def __init__(self, db: Database):
        self._conn = db.connect()

    def close(self) -> None:
        self._conn.close()

    def record_first_signal(self, entry: SignalJournalEntry) -> bool:
        """Returns True if this call actually inserted the market's first
        signal, False if one already existed (no-op - nothing changed)."""
        if self.get(entry.market_id) is not None:
            return False

        self._conn.execute(
            _INSERT_SIGNAL_JOURNAL_SQL,
            (
                entry.market_id,
                entry.first_signal_at.isoformat(),
                entry.classification,
                entry.category,
                entry.subcategory,
                entry.market_implied_probability,
                entry.estimated_probability,
                entry.adjusted_edge,
                entry.score,
            ),
        )
        self._conn.commit()
        return True

    def get(self, market_id: str) -> SignalJournalEntry | None:
        row = self._conn.execute(
            "SELECT * FROM signal_journal WHERE market_id = ?", (market_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def list_by_classification(
        self, classification: str, *, limit: int = 100
    ) -> list[SignalJournalEntry]:
        rows = self._conn.execute(
            "SELECT * FROM signal_journal WHERE classification = ? "
            "ORDER BY first_signal_at DESC LIMIT ?",
            (classification, limit),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: sqlite3.Row) -> SignalJournalEntry:
        return SignalJournalEntry(
            market_id=row["market_id"],
            first_signal_at=datetime.fromisoformat(row["first_signal_at"]),
            classification=row["classification"],
            category=row["category"],
            subcategory=row["subcategory"],
            market_implied_probability=row["market_implied_probability"],
            estimated_probability=row["estimated_probability"],
            adjusted_edge=row["adjusted_edge"],
            score=row["score"],
        )


_INSERT_PAPER_TRADE_SQL = """
INSERT INTO paper_trades (
    market_id, classification, category, subcategory, signal_at,
    selected_outcome, entry_probability, estimated_probability, raw_edge,
    adjusted_edge, score, status, settled_at, winning_outcome, pnl
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class PaperTradeRepository:
    """Requirement #19. Read-only with respect to every other table -
    create() only ever reads signal_journal/analyses/markets to build a
    PaperTrade, settle() only ever reads resolutions. One trade per
    market_id (the table's PK, same defense-in-depth pattern as
    resolutions/signal_journal), and once settled a trade is immutable -
    settle() refuses to change a row that isn't currently OPEN."""

    def __init__(self, db: Database):
        self._conn = db.connect()

    def close(self) -> None:
        self._conn.close()

    def create(self, trade: PaperTrade) -> bool:
        """Returns True if this call actually created the trade, False if
        one already existed for this market_id (no-op - nothing changed)."""
        if self.get(trade.market_id) is not None:
            return False

        self._conn.execute(
            _INSERT_PAPER_TRADE_SQL,
            (
                trade.market_id,
                trade.classification,
                trade.category,
                trade.subcategory,
                trade.signal_at.isoformat(),
                trade.selected_outcome,
                trade.entry_probability,
                trade.estimated_probability,
                trade.raw_edge,
                trade.adjusted_edge,
                trade.score,
                trade.status.value,
                trade.settled_at.isoformat() if trade.settled_at else None,
                trade.winning_outcome,
                trade.pnl,
            ),
        )
        self._conn.commit()
        return True

    def get(self, market_id: str) -> PaperTrade | None:
        row = self._conn.execute(
            "SELECT * FROM paper_trades WHERE market_id = ?", (market_id,)
        ).fetchone()
        return self._row_to_trade(row) if row else None

    def list_pending_signal_market_ids(self, *, limit: int = 500) -> list[str]:
        """Distinct market_ids present in signal_journal with no
        paper_trades row yet - the candidate set for a creation run."""
        rows = self._conn.execute(
            """
            SELECT sj.market_id
            FROM signal_journal sj
            LEFT JOIN paper_trades pt ON pt.market_id = sj.market_id
            WHERE pt.market_id IS NULL
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row["market_id"] for row in rows]

    def list_open(self, *, limit: int = 500) -> list[PaperTrade]:
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE status = ? ORDER BY signal_at ASC LIMIT ?",
            (PaperTradeStatus.OPEN.value, limit),
        ).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def list_all(self, *, limit: int = 1000) -> list[PaperTrade]:
        rows = self._conn.execute(
            "SELECT * FROM paper_trades ORDER BY signal_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def settle(
        self,
        market_id: str,
        *,
        status: PaperTradeStatus,
        settled_at: datetime,
        winning_outcome: str | None,
        pnl: float | None,
    ) -> bool:
        """Only transitions a trade that is currently OPEN. Returns True if
        this call actually settled it, False if it was already settled
        (no-op - the original settlement is never overwritten, same
        immutability guarantee as ResolutionRepository.upsert)."""
        existing = self.get(market_id)
        if existing is None or existing.status != PaperTradeStatus.OPEN:
            return False

        self._conn.execute(
            "UPDATE paper_trades SET status = ?, settled_at = ?, winning_outcome = ?, pnl = ? "
            "WHERE market_id = ?",
            (status.value, settled_at.isoformat(), winning_outcome, pnl, market_id),
        )
        self._conn.commit()
        return True

    def _row_to_trade(self, row: sqlite3.Row) -> PaperTrade:
        return PaperTrade(
            market_id=row["market_id"],
            classification=row["classification"],
            category=row["category"],
            subcategory=row["subcategory"],
            signal_at=datetime.fromisoformat(row["signal_at"]),
            selected_outcome=row["selected_outcome"],
            entry_probability=row["entry_probability"],
            estimated_probability=row["estimated_probability"],
            raw_edge=row["raw_edge"],
            adjusted_edge=row["adjusted_edge"],
            score=row["score"],
            status=PaperTradeStatus(row["status"]),
            settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
            winning_outcome=row["winning_outcome"],
            pnl=row["pnl"],
        )
