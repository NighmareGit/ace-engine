"""
event_schema.py — SQLite schema module for the live event streaming system.

Provides idempotent schema creation, monotonic sequence generation, and
time-based event cleanup.  Designed for WAL-mode SQLite with strict
monotonic guarantees on per-source sequence numbers.

Usage::

    from event_schema import ensure_schema, get_next_sequence, cleanup_old_events

    ensure_schema("events.db")
    conn = sqlite3.connect("events.db")
    seq = get_next_sequence(conn, "engine")
    cleanup_old_events(conn, max_age_hours=48)
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS live_events (
        id              TEXT    PRIMARY KEY,           -- ULID
        seq             INTEGER NOT NULL UNIQUE,       -- Monotonic stream sequence
        time            INTEGER NOT NULL,              -- Server timestamp (epoch ms)
        source_time     INTEGER,                       -- Producer timestamp
        source          TEXT    NOT NULL,              -- "dsh" | "engine" | "system"
        source_id       TEXT,                          -- Session/run identifier
        type            TEXT    NOT NULL,              -- Event type (e.g., "engine.task.start")
        correlation_id  TEXT,                          -- Cross-source link
        data            TEXT    NOT NULL DEFAULT '{}', -- JSON payload
        schema_version  INTEGER DEFAULT 1,
        created_at      TEXT    DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS source_sequences (
        source   TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL DEFAULT 0,
        last_time INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS replay_cursors (
        client_id  TEXT PRIMARY KEY,
        last_seq   INTEGER NOT NULL,
        updated_at TEXT    DEFAULT (datetime('now'))
    );
    """,
]

_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_live_events_source      ON live_events(source);",
    "CREATE INDEX IF NOT EXISTS idx_live_events_type         ON live_events(type);",
    "CREATE INDEX IF NOT EXISTS idx_live_events_seq          ON live_events(seq);",
    "CREATE INDEX IF NOT EXISTS idx_live_events_time         ON live_events(time);",
    "CREATE INDEX IF NOT EXISTS idx_live_events_source_id    ON live_events(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_live_events_correlation  ON live_events(correlation_id);",
]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a WAL-mode connection to *db_path*.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  ``":memory:"`` is
        accepted for in-memory databases.

    Returns
    -------
    sqlite3.Connection
        A connection with ``journal_mode=WAL``, ``foreign_keys=ON``,
        and ``busy_timeout=5000`` set.
    """
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def ensure_schema(db_path: str) -> None:
    """Create (or verify) the live-event schema in *db_path*.

    This function is **idempotent** — it uses ``IF NOT EXISTS`` DDL and can
    be called repeatedly without side-effects.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.

    Raises
    ------
    sqlite3.Error
        Propagated if any DDL statement fails.
    """
    conn = _connect(db_path)
    try:
        for ddl in _TABLES:
            conn.execute(ddl)
        for idx in _INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        conn.close()


def get_next_sequence(conn: sqlite3.Connection, source: str) -> int:
    """Return the next monotonic sequence number for *source*.

    Uses the ``source_sequences`` table to guarantee strict ordering.
    The sequence is incremented atomically (``BEGIN IMMEDIATE``) so
    concurrent callers for the same source will never receive duplicates.

    Parameters
    ----------
    conn:
        An open SQLite connection (WAL mode recommended).
    source:
        The logical source name, e.g. ``"dsh"``, ``"engine"``, ``"system"``.

    Returns
    -------
    int
        The new sequence number, guaranteed strictly greater than any
        previously returned value for the same *source*.

    Raises
    ------
    sqlite3.Error
        Propagated on database failure.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        row = conn.execute(
            "SELECT last_seq FROM source_sequences WHERE source = ?;",
            (source,),
        ).fetchone()

        if row is None:
            next_seq = 1
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            conn.execute(
                "INSERT INTO source_sequences (source, last_seq, last_time) "
                "VALUES (?, ?, ?);",
                (source, next_seq, now_ms),
            )
        else:
            next_seq = row[0] + 1
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            conn.execute(
                "UPDATE source_sequences SET last_seq = ?, last_time = ? "
                "WHERE source = ?;",
                (next_seq, now_ms, source),
            )
        conn.commit()
        return next_seq
    except Exception:
        conn.rollback()
        raise


def cleanup_old_events(conn: sqlite3.Connection, max_age_hours: int = 24) -> int:
    """Delete events older than *max_age_hours* from the ``live_events`` table.

    Parameters
    ----------
    conn:
        An open SQLite connection.
    max_age_hours:
        Maximum age of events to keep, in whole hours.  Defaults to ``24``.

    Returns
    -------
    int
        Number of rows deleted.

    Raises
    ------
    sqlite3.Error
        Propagated on database failure.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff_ms = now_ms - (max_age_hours * 3600 * 1000)

    cursor = conn.execute(
        "DELETE FROM live_events WHERE time < ?;",
        (cutoff_ms,),
    )
    conn.commit()
    deleted = cursor.rowcount

    # Vacuum is optional; skip on WAL to avoid blocking writers.
    # For a more aggressive cleanup you could schedule periodic VACUUMs.

    return deleted


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    source: str,
    type: str,
    data: dict | str | None = None,
    time: Optional[int] = None,
    source_time: Optional[int] = None,
    source_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    schema_version: int = 1,
) -> int:
    """Insert a new event into ``live_events`` and return its sequence number.

    Parameters
    ----------
    conn:
        An open SQLite connection.
    event_id:
        ULID for the event.
    source:
        Logical source name.
    type:
        Event type string.
    data:
        JSON payload.  Accepts a ``dict`` (serialised automatically) or a
        pre-encoded JSON string.  Defaults to ``{}``.
    time:
        Server timestamp in epoch milliseconds.  Defaults to now.
    source_time:
        Producer-provided timestamp in epoch milliseconds.
    source_id:
        Session or run identifier.
    correlation_id:
        Cross-source correlation identifier.
    schema_version:
        Schema version tag.  Defaults to ``1``.

    Returns
    -------
    int
        The monotonic sequence number assigned to this event.

    Raises
    ------
    sqlite3.Error
        Propagated on database failure.
    ValueError
        If *event_id* is empty.
    """
    if not event_id:
        raise ValueError("event_id must be non-empty")

    seq = get_next_sequence(conn, source)

    if time is None:
        time = int(datetime.now(timezone.utc).timestamp() * 1000)

    if data is None:
        data_json = "{}"
    elif isinstance(data, dict):
        data_json = json.dumps(data, separators=(",", ":"))
    else:
        data_json = str(data)

    conn.execute(
        """
        INSERT INTO live_events (
            id, seq, time, source_time, source, source_id,
            type, correlation_id, data, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            event_id,
            seq,
            time,
            source_time,
            source,
            source_id,
            type,
            correlation_id,
            data_json,
            schema_version,
        ),
    )
    conn.commit()
    return seq


def get_replay_cursor(conn: sqlite3.Connection, client_id: str) -> Optional[int]:
    """Return the last-seen sequence number for *client_id*, or ``None``."""
    row = conn.execute(
        "SELECT last_seq FROM replay_cursors WHERE client_id = ?;",
        (client_id,),
    ).fetchone()
    return row[0] if row is not None else None


def set_replay_cursor(
    conn: sqlite3.Connection, client_id: str, last_seq: int
) -> None:
    """Upsert the replay cursor for *client_id*."""
    conn.execute(
        """
        INSERT INTO replay_cursors (client_id, last_seq, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(client_id) DO UPDATE SET
            last_seq   = excluded.last_seq,
            updated_at = excluded.updated_at;
        """,
        (client_id, last_seq),
    )
    conn.commit()
