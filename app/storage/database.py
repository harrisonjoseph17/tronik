"""SQLite schema + connection management.

Stage 1 only needs `markets` (current state per market) and
`market_snapshots` (append-only history). Later stages add
features/analyses/signals/paper_trades/etc. as those stages are built.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS markets (
    market_id           TEXT PRIMARY KEY,
    event_id             TEXT,
    question              TEXT NOT NULL,
    slug                   TEXT,
    category               TEXT NOT NULL DEFAULT 'other',
    subcategory            TEXT NOT NULL DEFAULT 'uncategorized',
    outcomes_json          TEXT NOT NULL DEFAULT '[]',
    outcome_prices_json    TEXT NOT NULL DEFAULT '[]',
    clob_token_ids_json    TEXT NOT NULL DEFAULT '[]',
    volume                 REAL,
    volume_24hr            REAL,
    liquidity               REAL,
    best_bid                REAL,
    best_ask                REAL,
    spread                   REAL,
    mid_price                REAL,
    active                   INTEGER NOT NULL DEFAULT 0,
    closed                   INTEGER NOT NULL DEFAULT 0,
    archived                 INTEGER NOT NULL DEFAULT 0,
    enable_order_book        INTEGER NOT NULL DEFAULT 0,
    start_date               TEXT,
    end_date                 TEXT,
    status                   TEXT NOT NULL DEFAULT 'unknown',
    is_duplicate             INTEGER NOT NULL DEFAULT 0,
    duplicate_of             TEXT,
    filter_reason            TEXT,
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);
CREATE INDEX IF NOT EXISTS idx_markets_status   ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_end_date ON markets(end_date);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    category      TEXT,
    status        TEXT NOT NULL,
    volume        REAL,
    volume_24hr   REAL,
    liquidity     REAL,
    best_bid      REAL,
    best_ask      REAL,
    spread        REAL,
    mid_price     REAL,
    filter_reason TEXT,
    raw_json      TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_market_id   ON market_snapshots(market_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at ON market_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_category    ON market_snapshots(category);
CREATE INDEX IF NOT EXISTS idx_snapshots_status      ON market_snapshots(status);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()
