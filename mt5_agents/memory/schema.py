"""Phase-1 immutable event and hypothetical label schema.

Initialize once before launching trading workers; do not migrate schema mid-run.
"""
from pathlib import Path
import sqlite3

from .db import PersistenceError, connect, database_path

DDL = """
CREATE TABLE IF NOT EXISTS hypothetical_signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    entry_time_utc TEXT NOT NULL,
    entry_bid REAL NOT NULL,
    entry_ask REAL NOT NULL,
    entry_price REAL NOT NULL,
    take_profit REAL NOT NULL,
    stop_loss REAL NOT NULL,
    max_holding_bars INTEGER NOT NULL CHECK(max_holding_bars > 0),
    decision TEXT NOT NULL,
    model_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','LABELED')),
    bars_seen INTEGER NOT NULL DEFAULT 0 CHECK(bars_seen >= 0),
    last_bar_close_utc TEXT,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hypothetical_pending ON hypothetical_signals(status,symbol,entry_time_utc);
CREATE TABLE IF NOT EXISTS observed_bars (
    signal_id TEXT NOT NULL REFERENCES hypothetical_signals(signal_id),
    bar_open_utc TEXT NOT NULL,
    bar_close_utc TEXT NOT NULL,
    quote_json TEXT NOT NULL,
    PRIMARY KEY(signal_id,bar_open_utc)
);
CREATE TABLE IF NOT EXISTS closed_labels (
    signal_id TEXT PRIMARY KEY REFERENCES hypothetical_signals(signal_id),
    outcome TEXT NOT NULL CHECK(outcome IN ('TP','SL','TIME')),
    -- Preliminary direction label only; NOT net profit after execution costs.
    provisional_y INTEGER CHECK(provisional_y IN (-1,1)),
    exit_time_utc TEXT NOT NULL,
    exit_quote REAL NOT NULL,
    gross_price_move REAL NOT NULL,
    bars_seen INTEGER NOT NULL,
    reason TEXT NOT NULL,
    training_eligible INTEGER NOT NULL DEFAULT 0 CHECK(training_eligible=0),
    created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_time_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    persisted_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_entity ON events(entity_id,event_time_utc);
"""


def initialize_database(db_path: str | Path) -> Path:
    path = database_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceError(f"Cannot create database parent: {exc}") from exc
    db = connect(path)
    try:
        try:
            journal_mode = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if journal_mode != "wal":
                raise PersistenceError("Database does not support WAL mode")
            db.executescript(DDL)
        except sqlite3.Error as exc:
            raise PersistenceError(f"Cannot initialize audit schema: {exc}") from exc
    finally:
        db.close()
    return path
