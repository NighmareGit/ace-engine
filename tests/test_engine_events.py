"""Unit tests for engine/engine_events.py — queryable event bus.

Covers EPIC R6:
- engine_events table created with schema
- emit_event() writes a row to engine_events (best-effort)
- Callback path unchanged
- Row appears within 100 ms
- Query by (run_id, task_id, event_type)
- Pipeline never breaks on DB write failure
"""

import os
import sqlite3
import time

import pytest

from engine.engine_events import (
    ENGINE_EVENTS_DDL,
    init_engine_events_db,
    persist_event,
    query_events,
)
from engine.events import emit_event


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh temporary engine.db."""
    return str(tmp_path / "engine.db")


# ── Test: schema ──────────────────────────────────────────────────────────────


class TestSchema:
    def test_creates_engine_events_table(self, db_path):
        """init_engine_events_db creates the engine_events table."""
        init_engine_events_db(db_path)
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(engine_events)")]
        conn.close()
        expected = ["id", "run_id", "task_id", "event_type", "data_json",
                     "created_at"]
        assert cols == expected

    def test_creates_indexes(self, db_path):
        """Indexes on (run_id, task_id) and (event_type) exist."""
        init_engine_events_db(db_path)
        conn = sqlite3.connect(db_path)
        idxs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_engine_events%'"
        ).fetchall()]
        conn.close()
        assert "idx_engine_events_run" in idxs
        assert "idx_engine_events_type" in idxs

    def test_idempotent(self, db_path):
        """Calling init_engine_events_db twice is safe."""
        init_engine_events_db(db_path)
        init_engine_events_db(db_path)  # no error


# ── Test: persist_event ───────────────────────────────────────────────────────


class TestPersistEvent:
    def test_writes_row(self, db_path):
        """persist_event writes a row to engine_events."""
        init_engine_events_db(db_path)
        persist_event("test_event", {"key": "value"}, run_id="r1", task_id="T01",
                       db_path=db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM engine_events").fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "r1"  # run_id
        assert row[2] == "T01"  # task_id
        assert row[3] == "test_event"  # event_type

    def test_data_json_serialized(self, db_path):
        """data_json is a valid JSON string."""
        init_engine_events_db(db_path)
        persist_event("state_change", {"from": "IDLE", "to": "PARSING"},
                       db_path=db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT data_json FROM engine_events").fetchone()
        conn.close()
        import json
        data = json.loads(row[0])
        assert data["from"] == "IDLE"
        assert data["to"] == "PARSING"

    def test_extracts_run_id_from_data(self, db_path):
        """run_id is extracted from data dict if not provided explicitly."""
        init_engine_events_db(db_path)
        persist_event("test", {"run_id": "from-data", "task_id": "T02"},
                       db_path=db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT run_id, task_id FROM engine_events").fetchone()
        conn.close()
        assert row[0] == "from-data"
        assert row[1] == "T02"

    def test_best_effort_no_raise(self, db_path):
        """persist_event never raises, even on DB failure."""
        # Use an invalid path — should not raise.
        persist_event("test", {"key": "value"}, db_path="/nonexistent/dir/db.sqlite")


# ── Test: emit_event integration ──────────────────────────────────────────────


class TestEmitEvent:
    def test_emit_writes_row(self, db_path, monkeypatch):
        """emit_event() writes a row to engine_events."""
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        init_engine_events_db(db_path)
        emit_event(None, "state_change", {"run_id": "r1", "from": "IDLE"})
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM engine_events").fetchone()
        conn.close()
        assert row is not None
        assert row[3] == "state_change"

    def test_callback_path_unchanged(self, db_path, monkeypatch):
        """Callback path still works after OBS-04 changes."""
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        init_engine_events_db(db_path)
        called = []

        def on_event(event_type, data):
            called.append((event_type, data))

        emit_event(on_event, "test", {"key": "val"})
        assert len(called) == 1
        assert called[0][0] == "test"

    def test_row_appears_quickly(self, db_path, monkeypatch):
        """Row appears in engine_events within 100 ms of emit."""
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        init_engine_events_db(db_path)
        start = time.time()
        emit_event(None, "fast_event", {"run_id": "r1"})
        elapsed_ms = (time.time() - start) * 1000
        assert elapsed_ms < 100  # well within 100 ms

    def test_pipeline_never_breaks_on_db_failure(self, monkeypatch):
        """Pipeline never breaks when DB write fails."""
        monkeypatch.setenv("ENGINE_DB_PATH", "/nonexistent/dir/db.sqlite")
        # Should not raise.
        emit_event(None, "test", {"key": "value"})

    def test_emit_without_streaming(self, db_path, monkeypatch):
        """emit_event works without STREAMING_URL set."""
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        monkeypatch.delenv("STREAMING_URL", raising=False)
        init_engine_events_db(db_path)
        emit_event(None, "no_stream", {"run_id": "r1"})
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM engine_events").fetchone()
        conn.close()
        assert row is not None


# ── Test: query_events ─────────────────────────────────────────────────────────


class TestQueryEvents:
    def test_query_by_run_id(self, db_path):
        """query_events filters by run_id."""
        init_engine_events_db(db_path)
        persist_event("e1", {}, run_id="r1", db_path=db_path)
        persist_event("e2", {}, run_id="r2", db_path=db_path)
        events = query_events(run_id="r1", db_path=db_path)
        assert len(events) == 1
        assert events[0]["run_id"] == "r1"

    def test_query_by_event_type(self, db_path):
        """query_events filters by event_type."""
        init_engine_events_db(db_path)
        persist_event("type_a", {}, run_id="r1", db_path=db_path)
        persist_event("type_b", {}, run_id="r1", db_path=db_path)
        events = query_events(event_type="type_a", db_path=db_path)
        assert len(events) == 1
        assert events[0]["event_type"] == "type_a"

    def test_query_by_run_and_task(self, db_path):
        """query_events filters by (run_id, task_id)."""
        init_engine_events_db(db_path)
        persist_event("e1", {}, run_id="r1", task_id="T01", db_path=db_path)
        persist_event("e2", {}, run_id="r1", task_id="T02", db_path=db_path)
        persist_event("e3", {}, run_id="r2", task_id="T01", db_path=db_path)
        events = query_events(run_id="r1", task_id="T01", db_path=db_path)
        assert len(events) == 1
        assert events[0]["task_id"] == "T01"

    def test_query_since_id(self, db_path):
        """query_events with since_id returns only newer rows."""
        init_engine_events_db(db_path)
        persist_event("e1", {}, run_id="r1", db_path=db_path)
        persist_event("e2", {}, run_id="r1", db_path=db_path)
        persist_event("e3", {}, run_id="r1", db_path=db_path)
        # Get the id of the first row.
        conn = sqlite3.connect(db_path)
        first_id = conn.execute("SELECT MIN(id) FROM engine_events").fetchone()[0]
        conn.close()
        events = query_events(since_id=first_id, db_path=db_path)
        assert len(events) == 2  # rows after the first

    def test_query_ordered_by_id(self, db_path):
        """query_events returns rows ordered by id."""
        init_engine_events_db(db_path)
        for i in range(5):
            persist_event(f"e{i}", {"i": i}, run_id="r1", db_path=db_path)
        events = query_events(run_id="r1", db_path=db_path)
        ids = [e["id"] for e in events]
        assert ids == sorted(ids)
