"""Unit tests for engine/tail.py — live-tail event stream.

Covers EPIC R6:
- ace tail <run_id> prints events as they appear in engine_events
- Polling interval configurable (default 1 s)
- Filters by event_type when specified
- Read-only (no DB writes)
"""

import sqlite3
import threading
import time

import pytest

from engine.engine_events import init_engine_events_db, persist_event, query_events
from engine.tail import tail_run


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    db = str(tmp_path / "engine.db")
    init_engine_events_db(db_path=db)
    return db


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestTail:
    def test_tail_existing_events(self, db_path):
        """tail_run returns existing events for a run."""
        persist_event("state_change", {"from": "IDLE", "to": "PARSING"},
                       run_id="run-1", db_path=db_path)
        persist_event("state_change", {"from": "PARSING", "to": "QUEUED"},
                       run_id="run-1", db_path=db_path)
        events = tail_run("run-1", timeout=0.5, db_path=db_path, print_events=False)
        assert len(events) == 2

    def test_tail_filters_by_event_type(self, db_path):
        """tail_run filters by event_type when specified."""
        persist_event("type_a", {}, run_id="run-1", db_path=db_path)
        persist_event("type_b", {}, run_id="run-1", db_path=db_path)
        persist_event("type_a", {}, run_id="run-1", db_path=db_path)
        events = tail_run("run-1", event_type="type_a", timeout=0.5,
                          db_path=db_path, print_events=False)
        assert len(events) == 2
        assert all(e["event_type"] == "type_a" for e in events)

    def test_tail_no_events(self, db_path):
        """tail_run returns empty list when no events exist."""
        events = tail_run("run-empty", timeout=0.5, db_path=db_path, print_events=False)
        assert len(events) == 0

    def test_tail_read_only(self, db_path):
        """tail_run does not write to the DB."""
        persist_event("test", {"key": "val"}, run_id="run-1", db_path=db_path)
        tail_run("run-1", timeout=0.5, db_path=db_path, print_events=False)
        # Count should be unchanged.
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM engine_events").fetchone()[0]
        conn.close()
        assert count == 1

    def test_tail_captures_new_events(self, db_path):
        """tail_run captures events that appear during polling."""
        # Start with one event.
        persist_event("e1", {}, run_id="run-1", db_path=db_path)

        # Schedule a new event to appear after 0.3s.
        def delayed_emit():
            time.sleep(0.3)
            persist_event("e2", {}, run_id="run-1", db_path=db_path)

        t = threading.Thread(target=delayed_emit)
        t.start()
        events = tail_run("run-1", interval=0.1, timeout=2.0, db_path=db_path,
                          print_events=False)
        t.join()
        assert len(events) == 2
        event_types = [e["event_type"] for e in events]
        assert "e1" in event_types
        assert "e2" in event_types

    def test_tail_respects_timeout(self, db_path):
        """tail_run exits after timeout."""
        start = time.time()
        tail_run("run-1", interval=0.1, timeout=0.5, db_path=db_path,
                 print_events=False)
        elapsed = time.time() - start
        assert elapsed < 2.0  # should exit near the timeout
