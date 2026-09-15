"""Engine events persistence — parallel write for the fire-and-forget event bus.

Makes the fire-and-forget event bus (engine/events.py) queryable by writing
each event to an ``engine_events`` table in engine.db.  The callback path is
unchanged — this is an additive best-effort write.

Rule 6: Events live in engine.db only — never benchmark-results.db.
Additive-only DDL (CREATE TABLE IF NOT EXISTS).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Optional

log = logging.getLogger("engine.engine_events")

# ── DDL ──────────────────────────────────────────────────────────────────────

ENGINE_EVENTS_DDL = """\
CREATE TABLE IF NOT EXISTS engine_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    task_id         TEXT,
    event_type      TEXT NOT NULL,
    data_json       TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

ENGINE_EVENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_engine_events_run ON engine_events(run_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_engine_events_type ON engine_events(event_type)",
]


def _db_path() -> str:
    """Resolve the active engine DB path, honoring ENGINE_DB_PATH env override."""
    return os.environ.get("ENGINE_DB_PATH") or "engine.db"


def _get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Get a WAL-mode connection with busy_timeout."""
    conn = sqlite3.connect(db_path or _db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_engine_events_db(db_path: Optional[str] = None) -> None:
    """Idempotently create the ``engine_events`` table and indexes."""
    conn = _get_conn(db_path)
    try:
        conn.execute(ENGINE_EVENTS_DDL)
        for idx in ENGINE_EVENTS_INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        conn.close()


def persist_event(
    event_type: str,
    data: dict,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Write one event row to engine_events.  Best-effort: never raises.

    The swallow-contract matches engine/events.py — observability must never
    break the pipeline.
    """
    try:
        conn = _get_conn(db_path)
        try:
            # Extract run_id/task_id from data if not provided explicitly.
            rid = run_id or data.get("run_id") or data.get("runId")
            tid = task_id or data.get("task_id") or data.get("taskId")
            conn.execute(
                """\
                INSERT INTO engine_events (run_id, task_id, event_type, data_json)
                VALUES (?, ?, ?, ?)
                """,
                (rid, tid, event_type, json.dumps(data, default=str)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # best-effort; never break the pipeline


def query_events(
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    event_type: Optional[str] = None,
    since_id: int = 0,
    db_path: Optional[str] = None,
    limit: int = 1000,
) -> list[dict]:
    """Query engine_events with optional filters.

    Args:
        run_id: Filter by run ID.
        task_id: Filter by task ID.
        event_type: Filter by event type.
        since_id: Only return rows with id > since_id (for polling).
        db_path: Override DB path.
        limit: Max rows to return.

    Returns:
        List of event dicts, ordered by id.
    """
    conn = _get_conn(db_path)
    try:
        query = "SELECT * FROM engine_events WHERE id > ?"
        params: list[Any] = [since_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        cur = conn.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
