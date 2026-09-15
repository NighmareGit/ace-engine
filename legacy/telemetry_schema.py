"""BeeLlama telemetry schema — extended inference-layer tables.

This module defines the canonical SQLite schema for per-request inference
tracing, GPU state capture, cache efficiency metrics, pipeline events, and
telemetry session tracking.  It **extends** ``schema_unified.py`` which
provides the four baseline tables (work_sessions, work_events, gpu_snapshots,
benchmark_results).

Usage:
    from telemetry_schema import ensure_telemetry_schema, DB_PATH
    ensure_telemetry_schema()                # uses default DB_PATH
    ensure_telemetry_schema("/custom/path")  # explicit path

All DDL is idempotent (CREATE TABLE/INDEX IF NOT EXISTS) so it is safe to
call from any module at any time.
"""

from __future__ import annotations

import os
import sqlite3

# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------

DB_PATH: str = os.path.expanduser("~/coder-harness-telemetry.db")

# ---------------------------------------------------------------------------
# Canonical DDL — six new tables for the BeeLlama telemetry layer
# ---------------------------------------------------------------------------

TELEMETRY_SCHEMA: str = """
-- ────────────────────────────────────────────────────────────────────────────
-- 1. Per-request inference trace data from BeeLlama API
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inference_traces (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT UNIQUE,
    config_id           TEXT,
    started_at          TEXT DEFAULT (datetime('now')),
    completed_at        TEXT,
    status              TEXT CHECK(status IN ('running','complete','failed')) DEFAULT 'running',

    -- Prompt phase
    prompt_tokens       INTEGER,
    prompt_ms           REAL,
    prompt_per_second   REAL,

    -- Generation phase
    predicted_tokens    INTEGER,
    predicted_ms        REAL,
    predicted_per_second REAL,

    -- Draft / speculative decoding
    draft_n             INTEGER DEFAULT 0,
    draft_n_accepted    INTEGER DEFAULT 0,
    draft_acceptance_rate REAL,

    -- Cache efficiency (BeeLlama extended timings)
    cache_n             INTEGER DEFAULT 0,
    cache_lcp_n         INTEGER DEFAULT 0,
    cache_planned_n     INTEGER DEFAULT 0,
    cache_reprocessed_n INTEGER DEFAULT 0,
    cache_source        TEXT,
    cache_reason        TEXT,

    -- GPU state at request time
    gpu_temperature     REAL,
    gpu_vram_used_mb    REAL,
    gpu_utilization_pct REAL,
    gpu_power_watts     REAL,

    -- Derived metrics
    total_tokens        INTEGER,
    total_ms            REAL,
    tokens_per_ms       REAL,

    -- Metadata
    model_name          TEXT,
    model_path          TEXT,
    context_size        INTEGER,
    kvarn_level         TEXT,
    thinking_tokens     INTEGER DEFAULT 0,
    visible_tokens      INTEGER DEFAULT 0,
    raw_timings_json    TEXT,
    raw_usage_json      TEXT
);

-- ────────────────────────────────────────────────────────────────────────────
-- 2. Per-layer compute timing data (populated when --perf is enabled)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS layer_timings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        INTEGER REFERENCES inference_traces(id),
    layer_index     INTEGER,
    layer_type      TEXT,
    compute_ms      REAL,
    memory_ms       REAL,
    gpu_memory_bytes INTEGER,
    timestamp       TEXT DEFAULT (datetime('now'))
);

-- ────────────────────────────────────────────────────────────────────────────
-- 3. Enhanced GPU telemetry beyond basic nvidia-smi
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gpu_snapshots_enhanced (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    gpu_index                    INTEGER NOT NULL,
    timestamp                    TEXT DEFAULT (datetime('now')),

    -- Standard nvidia-smi
    name                         TEXT,
    memory_used_mb               REAL,
    memory_total_mb              REAL,
    temperature_c                REAL,
    utilization_gpu_pct          REAL,
    utilization_memory_pct       REAL,

    -- Power and clocks
    power_draw_watts             REAL,
    power_limit_watts            REAL,
    clock_sm_mhz                 REAL,
    clock_mem_mhz                REAL,

    -- Process-level
    process_count                INTEGER,
    process_vram_mb              REAL,

    -- Derived
    memory_bandwidth_utilization_pct REAL,
    thermal_throttling_detected  INTEGER DEFAULT 0,
    pcie_gen                     INTEGER,
    pcie_width                   INTEGER
);

-- ────────────────────────────────────────────────────────────────────────────
-- 4. Periodic cache performance snapshots
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cache_efficiency_snapshots (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time            TEXT DEFAULT (datetime('now')),
    config_id                TEXT,

    -- Aggregate cache stats
    total_requests           INTEGER,
    cache_hit_count          INTEGER,
    cache_hit_rate           REAL,
    avg_lcp_tokens           REAL,
    avg_reprocessed_tokens   REAL,

    -- KV memory stats (from llama_kv_memory_stats)
    kv_used_bytes            INTEGER,
    kv_capacity_bytes        INTEGER,
    kv_utilization_pct       REAL,

    -- KVarN specific
    kvarn_compressed_bytes   INTEGER,
    kvarn_exact_tail_bytes   INTEGER,
    kvarn_compression_ratio  REAL,
    raw_kv_stats_json        TEXT
);

-- ────────────────────────────────────────────────────────────────────────────
-- 5. Discrete events in the inference pipeline
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    INTEGER REFERENCES inference_traces(id),
    event_type  TEXT NOT NULL CHECK(event_type IN (
        'batch_submit', 'batch_complete',
        'draft_propose', 'draft_accept', 'draft_reject',
        'cache_lookup', 'cache_miss', 'cache_hit',
        'defrag_start', 'defrag_end',
        'context_shift',
        'slot_acquire', 'slot_release',
        'kv_serialize', 'kv_deserialize',
        'stall_detected', 'memory_pressure',
        'thermal_warning', 'oom_risk'
    )),
    event_data  TEXT,
    duration_ms REAL,
    timestamp   TEXT DEFAULT (datetime('now'))
);

-- ────────────────────────────────────────────────────────────────────────────
-- 6. Collection session tracking
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS telemetry_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name      TEXT,
    config_id         TEXT,
    started_at        TEXT DEFAULT (datetime('now')),
    ended_at          TEXT,
    total_traces      INTEGER DEFAULT 0,
    total_snapshots   INTEGER DEFAULT 0,
    total_events      INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'active',
    notes             TEXT
);
"""

# ---------------------------------------------------------------------------
# Indexes — speed up common query paths
# ---------------------------------------------------------------------------

TELEMETRY_INDEXES: str = """
CREATE INDEX IF NOT EXISTS idx_traces_config   ON inference_traces(config_id);
CREATE INDEX IF NOT EXISTS idx_traces_status   ON inference_traces(status);
CREATE INDEX IF NOT EXISTS idx_traces_started  ON inference_traces(started_at);
CREATE INDEX IF NOT EXISTS idx_layer_trace     ON layer_timings(trace_id);
CREATE INDEX IF NOT EXISTS idx_gpu_time        ON gpu_snapshots_enhanced(timestamp);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace  ON pipeline_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_type   ON pipeline_events(event_type);
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_telemetry_schema(db_path: str | None = None) -> bool:
    """Create the BeeLlama telemetry tables and indexes.

    This is idempotent — safe to call repeatedly from any module.
    Existing databases gain the new tables without touching the original
    ``schema_unified.py`` tables.

    Returns True on success.
    """
    path: str = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        conn.executescript(TELEMETRY_SCHEMA)
        conn.executescript(TELEMETRY_INDEXES)
        conn.commit()
    finally:
        conn.close()

    return True


def list_tables(db_path: str | None = None) -> list[str]:
    """Return the names of every table in the database.

    Useful for verifying that the schema was applied correctly.
    """
    path: str = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    return [row[0] for row in rows]


def migration_check(db_path: str | None = None) -> dict[str, bool]:
    """Check which BeeLlama telemetry tables exist in the database.

    Returns a dict mapping each expected table name to ``True`` (exists) or
    ``False`` (missing).  This is handy for deciding whether to run the
    full ``ensure_telemetry_schema()`` migration.
    """
    _EXPECTED: list[str] = [
        "inference_traces",
        "layer_timings",
        "gpu_snapshots_enhanced",
        "cache_efficiency_snapshots",
        "pipeline_events",
        "telemetry_sessions",
        # Also check the original unified tables
        "work_sessions",
        "work_events",
        "gpu_snapshots",
        "benchmark_results",
    ]

    existing: set[str] = set(list_tables(db_path))
    return {name: name in existing for name in _EXPECTED}


# ---------------------------------------------------------------------------
# CLI helper — run directly to bootstrap / inspect the database
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else None

    print(f"Target database: {target or DB_PATH}")
    print()

    # Show migration status before
    print("Migration check (before):")
    before = migration_check(target)
    for table, exists in sorted(before.items()):
        symbol = "✅" if exists else "❌"
        print(f"  {symbol} {table}")
    print()

    # Apply schema
    ensure_telemetry_schema(target)
    print("Schema applied successfully.")
    print()

    # Show migration status after
    print("Migration check (after):")
    after = migration_check(target)
    for table, exists in sorted(after.items()):
        symbol = "✅" if exists else "❌"
        print(f"  {symbol} {table}")
    print()

    # Show all tables in the database
    all_tables = list_tables(target)
    print(f"All tables ({len(all_tables)}):")
    for t in all_tables:
        print(f"  • {t}")
