"""SQLite storage: WAL mode, one connection per thread, CREATE TABLE IF NOT EXISTS
migrations only (Section 7 tables + Section 11 action/approval fields)."""
from __future__ import annotations

import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    run_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
    drive_id    TEXT PRIMARY KEY,
    blob        TEXT NOT NULL,   -- JSON of the current Drive
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_threads (
    thread_id   TEXT PRIMARY KEY,
    drive_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    diff        TEXT NOT NULL,   -- JSON
    source_message_id TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    action_id       TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    drive_id        TEXT NOT NULL,
    drive_version   INTEGER NOT NULL,
    app             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    tier            TEXT NOT NULL,
    payload         TEXT NOT NULL,   -- JSON
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    lease_until     TEXT,
    result          TEXT,            -- JSON
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id         TEXT PRIMARY KEY,   -- short token
    action_id           TEXT NOT NULL,
    drive_id            TEXT NOT NULL,
    drive_version       INTEGER NOT NULL,
    telegram_message_id TEXT,
    status              TEXT NOT NULL,
    decided_at          TEXT,
    snoozed_until       TEXT   -- set when status = 'SNOOZED' (the "Remind me in 2h" button)
);

CREATE TABLE IF NOT EXISTS traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    span_id     TEXT NOT NULL,
    parent_id   TEXT,
    name        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT,
    attrs       TEXT  -- JSON
);

CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL   -- JSON
);
"""

_local = threading.local()


def init_db(db_path: str) -> None:
    """Create tables if missing. Safe to call from multiple threads/processes."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS alone doesn't add a column to a
        # pre-existing approvals table from before "Remind me in 2h" did
        # anything — SQLite has no ADD COLUMN IF NOT EXISTS, so try/except
        # around the one-time migration instead.
        try:
            conn.execute("ALTER TABLE approvals ADD COLUMN snoozed_until TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return this thread's connection, opening one (WAL mode) if needed.
    Cached per-thread AND per-path, so a thread that talks to two different
    SQLite files (e.g. tests using a tmp_path db) never thrashes connections."""
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = {}
        _local.conns = cache
    conn = cache.get(db_path)
    if conn is not None:
        return conn
    conn = sqlite3.connect(db_path, check_same_thread=True)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    cache[db_path] = conn
    return conn
