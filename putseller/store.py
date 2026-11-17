"""SQLite storage for the paper trading log."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    taken_at TEXT NOT NULL UNIQUE,
    scan_date TEXT NOT NULL,
    snapshot_path TEXT,
    rules_version TEXT NOT NULL,
    rules_json TEXT NOT NULL
);
-- One row per contract inside the delta/expiry window, per scan.
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    dte INTEGER NOT NULL,
    underlying REAL NOT NULL,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    delta REAL,
    iv REAL,
    annualized_return_pct REAL,
    verdict TEXT NOT NULL,          -- pass | near_miss | fail
    failed_rules TEXT NOT NULL,     -- comma separated
    checks_json TEXT NOT NULL
);
-- Hypothetical one-contract short puts. kind = 'pass' (rules allowed it) or
-- 'near_miss' (exactly one filter rule, blocked_by, stopped it).
-- exit_* is the outcome under the exit rules; held_pnl is the outcome had the
-- put simply been held to expiry. Money columns are dollars per contract.
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    kind TEXT NOT NULL,
    blocked_by TEXT,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    entry_date TEXT NOT NULL,
    entry_credit REAL NOT NULL,     -- per share, filled at the bid
    entry_underlying REAL NOT NULL,
    entry_delta REAL,
    rules_version TEXT NOT NULL,
    exit_date TEXT,
    exit_reason TEXT,
    exit_cost REAL,                 -- per share, filled at the ask
    exit_pnl REAL,
    settled_date TEXT,
    settle_underlying REAL,
    held_pnl REAL
);
CREATE TABLE IF NOT EXISTS marks (
    position_id INTEGER NOT NULL REFERENCES positions(id),
    mark_date TEXT NOT NULL,
    underlying REAL NOT NULL,
    bid REAL,
    ask REAL,
    delta REAL,
    PRIMARY KEY (position_id, mark_date)
);
CREATE TABLE IF NOT EXISTS iv_history (
    ticker TEXT NOT NULL,
    day TEXT NOT NULL,
    atm_iv REAL NOT NULL,
    PRIMARY KEY (ticker, day)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
