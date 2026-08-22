"""SQLite schema + connection management.

Stage 1 added `markets` (current state per market) and `market_snapshots`
(append-only history). Stage 2 adds `features` and `analyses` - every raw
input and derived value from the feature/probability/edge/risk-gate/scoring
pipeline is stored so an analysis can be reproduced later without re-fetching
anything. `resolutions` (added after Stage 2) records what a market actually
resolved to, so `analyses` rows can eventually be checked against real
outcomes - joined on market_id, no FK added to analyses since it's a
time-series table written before a market resolves. `signal_journal`
(requirement #18) records each market's first-ever actionable
(COMPOUND/WATCH) analysis snapshot, one row per market_id like
`resolutions`, so it's joinable with `resolutions` by market_id the same
way. Later stages add paper_trades/etc. as those are built.
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

CREATE TABLE IF NOT EXISTS features (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id                     TEXT NOT NULL,
    computed_at                   TEXT NOT NULL,
    market_implied_probability    REAL,
    volume                        REAL,
    volume_24hr                   REAL,
    liquidity                     REAL,
    spread                        REAL,
    time_to_resolution_hours      REAL,
    market_age_hours              REAL,
    snapshot_count                INTEGER NOT NULL DEFAULT 0,
    history_span_hours            REAL,
    price_change_recent           REAL,
    volume_change_recent          REAL,
    order_book_imbalance          REAL,
    clob_data_available           INTEGER NOT NULL DEFAULT 0,
    data_quality                  TEXT NOT NULL,
    data_quality_reasons_json     TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_features_market_id   ON features(market_id);
CREATE INDEX IF NOT EXISTS idx_features_computed_at ON features(computed_at);

CREATE TABLE IF NOT EXISTS analyses (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id                     TEXT NOT NULL,
    feature_id                    INTEGER,
    computed_at                   TEXT NOT NULL,
    category                      TEXT,
    market_implied_probability    REAL,
    estimated_probability         REAL,
    confidence                    REAL,
    adjustments_json              TEXT NOT NULL DEFAULT '[]',
    raw_edge                      REAL,
    adjusted_edge                 REAL,
    expected_value                REAL,
    category_reliability          REAL,
    data_quality                  TEXT NOT NULL,
    quality_gate_passed           INTEGER NOT NULL,
    quality_gate_reason           TEXT,
    risk_gate_passed              INTEGER,
    risk_gate_reasons_json        TEXT NOT NULL DEFAULT '[]',
    score                         REAL,
    classification                TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id),
    FOREIGN KEY (feature_id) REFERENCES features(id)
);
CREATE INDEX IF NOT EXISTS idx_analyses_market_id      ON analyses(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_computed_at    ON analyses(computed_at);
CREATE INDEX IF NOT EXISTS idx_analyses_classification ON analyses(classification);

CREATE TABLE IF NOT EXISTS resolutions (
    market_id               TEXT PRIMARY KEY,
    resolution_status        TEXT NOT NULL,
    resolved_at               TEXT,
    winning_outcome_index      INTEGER,
    winning_outcome             TEXT,
    raw_resolution_json          TEXT NOT NULL,
    checked_at                    TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_resolutions_status ON resolutions(resolution_status);

-- Requirement #18: each market's FIRST actionable (COMPOUND/WATCH)
-- analysis snapshot, written once and never overwritten. market_id as the
-- PK (like resolutions) makes this joinable with resolutions by market_id
-- without any FK between the two tables themselves.
CREATE TABLE IF NOT EXISTS signal_journal (
    market_id                   TEXT PRIMARY KEY,
    first_signal_at              TEXT NOT NULL,
    classification                 TEXT NOT NULL,
    category                        TEXT,
    subcategory                      TEXT,
    market_implied_probability        REAL,
    estimated_probability                REAL,
    adjusted_edge                          REAL,
    score                                    REAL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_journal_classification ON signal_journal(classification);
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
