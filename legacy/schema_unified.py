"""Unified telemetry schema for coder-harness.

All tools (sandbox_manager, work_engine, telemetry_collector) must use this
schema.  Call ensure_schema() before any DB operations to guarantee that all
required columns exist — even if the database was originally created by an
older version of one of those tools.

Usage:
    from schema_unified import ensure_schema, DB_PATH
    ensure_schema()                # uses default DB_PATH
    ensure_schema("/custom/path")  # explicit path
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get(
    "CODER_HARNESS_DB",
    os.path.expanduser("~/coder-harness-telemetry.db"),
)

# ---------------------------------------------------------------------------
# Canonical DDL — covers all four tables used across the project
# ---------------------------------------------------------------------------

UNIFIED_SCHEMA = """
-- Work sessions: lifecycle of a sandbox / inference task
CREATE TABLE IF NOT EXISTS work_sessions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_name            TEXT NOT NULL,
    project                 TEXT NOT NULL,
    branch                  TEXT,
    config_id               TEXT,
    task_prompt             TEXT,
    session_name            TEXT,
    session_uuid            TEXT,
    created_at              TEXT DEFAULT (datetime('now')),
    completed_at            TEXT,
    status                  TEXT,
    inference_tokens        INTEGER,
    inference_seconds       REAL,
    inference_tokens_per_sec REAL,
    git_commits             INTEGER DEFAULT 0,
    git_files_changed       INTEGER DEFAULT 0,
    error_message           TEXT
);

-- Event timeline for each session
CREATE TABLE IF NOT EXISTS work_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES work_sessions(id),
    event_type      TEXT NOT NULL,
    event_data      TEXT,
    timestamp       TEXT DEFAULT (datetime('now'))
);

-- Periodic GPU snapshots (from nvidia-smi via telemetry_collector)
CREATE TABLE IF NOT EXISTS gpu_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gpu_index       INTEGER NOT NULL,
    name            TEXT,
    memory_used_mb  REAL,
    memory_total_mb REAL,
    temperature_c   REAL,
    utilization_pct REAL,
    timestamp       TEXT DEFAULT (datetime('now'))
);

-- Benchmark results (from remote_control / work_engine)
CREATE TABLE IF NOT EXISTS benchmark_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id       TEXT NOT NULL,
    benchmark_type  TEXT NOT NULL,
    tokens_per_sec  REAL,
    tokens_generated INTEGER,
    elapsed_seconds REAL,
    vram_used_mb    REAL,
    timestamp       TEXT DEFAULT (datetime('now'))
);
"""

# Columns that may be missing in databases created by older tool versions.
# Key = column name, Value = SQL type.
_MIGRATION_COLUMNS: dict[str, str] = {
    "task_prompt":  "TEXT",
    "session_name": "TEXT",
    "session_uuid": "TEXT",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_schema(db_path: str | None = None) -> bool:
    """Create or migrate the telemetry database to the unified schema.

    1. Creates any missing tables (IF NOT EXISTS).
    2. Adds any missing columns to existing tables (ALTER TABLE … ADD COLUMN).

    This is safe to call repeatedly and from any tool — it is idempotent.

    Returns True on success.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        # 1. Create tables that don't exist yet
        conn.executescript(UNIFIED_SCHEMA)

        # 2. Migrate: add columns that may be missing in older schemas
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(work_sessions)").fetchall()
        }

        for col, typ in _MIGRATION_COLUMNS.items():
            if col not in existing:
                conn.execute(
                    f"ALTER TABLE work_sessions ADD COLUMN {col} {typ}"
                )

        conn.commit()
    finally:
        conn.close()

    return True


# ---------------------------------------------------------------------------
# CLI helper — run directly to bootstrap / migrate the database
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else None
    ensure_schema(target)
    print(f"Schema ensured at {target or DB_PATH}")
