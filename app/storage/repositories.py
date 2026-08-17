"""Read/write access to the markets + market_snapshots tables.

All queries are parameterized (no string interpolation). first_seen_at is
preserved across upserts by simply never including it in the UPDATE SET
clause - only INSERT sets it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.storage.database import Database
from app.storage.models import Market

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
        self, *, category: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[Market]:
        query = "SELECT * FROM markets WHERE 1=1"
        params: list = []
        if category is not None:
            query += " AND category = ?"
            params.append(category)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_market(row) for row in rows]

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
