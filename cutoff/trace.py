"""Tracing: one run per email, child spans for each pipeline step (Section 14).

Usage:
    with trace.run(run_id):
        with trace.span("ingest", message_id=msg.message_id):
            ...
"""
from __future__ import annotations

import contextvars
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from cutoff import db

_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_run_id", default=None
)
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_span_id", default=None
)
_db_path: contextvars.ContextVar[str] = contextvars.ContextVar("db_path", default="cutoff.db")


def configure(db_path: str) -> None:
    _db_path.set(db_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run(run_id: str | None = None):
    """Start a new top-level run (one per email processed)."""
    run_id = run_id or uuid.uuid4().hex
    token = _current_run_id.set(run_id)
    parent_token = _current_span_id.set(None)
    try:
        yield run_id
    finally:
        _current_run_id.reset(token)
        _current_span_id.reset(parent_token)


@contextmanager
def span(name: str, **attrs):
    """A traced span, nested under the current run/span if any."""
    run_id = _current_run_id.get() or uuid.uuid4().hex
    span_id = uuid.uuid4().hex
    parent_id = _current_span_id.get()
    started_at = _now()

    conn = db.get_connection(_db_path.get())
    conn.execute(
        "INSERT INTO traces (run_id, span_id, parent_id, name, started_at, ended_at, status, attrs) "
        "VALUES (?, ?, ?, ?, ?, NULL, 'running', ?)",
        (run_id, span_id, parent_id, name, started_at, json.dumps(attrs, default=str)),
    )
    conn.commit()

    token = _current_span_id.set(span_id)
    status = "ok"
    try:
        yield span_id
    except Exception:
        status = "error"
        raise
    finally:
        _current_span_id.reset(token)
        conn.execute(
            "UPDATE traces SET ended_at = ?, status = ? WHERE run_id = ? AND span_id = ?",
            (_now(), status, run_id, span_id),
        )
        conn.commit()


def list_spans(run_id: str) -> list[dict]:
    conn = db.get_connection(_db_path.get())
    rows = conn.execute(
        "SELECT run_id, span_id, parent_id, name, started_at, ended_at, status, attrs "
        "FROM traces WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_run_id() -> str | None:
    """The dashboard's Live Trace tab always shows this run. A duplicate
    email delivery or a short-circuited "nothing changed" message produces a
    real but visually empty one-span run — found live: on a demo board, the
    literal most-recent run is that kind of trivial run far more often than
    not, so the tab looks stuck. Prefers the most recent run with more than
    one span (i.e. one that got past ingest and did something); falls back
    to the literal latest run so the tab is never empty before any
    multi-span run has happened yet."""
    conn = db.get_connection(_db_path.get())
    row = conn.execute(
        "SELECT run_id FROM traces GROUP BY run_id HAVING COUNT(*) > 1 ORDER BY MAX(id) DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["run_id"]
    row = conn.execute("SELECT run_id FROM traces ORDER BY id DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None
