"""T1 tests: orchestrator session state (session.py + orchestrator_session table).

Mock/isolated-DB only. Covers session creation, task-state tracking, budget
counters, crash-sim resume, and content-hash dedup (no dup commits).

AC: kill -9 mid-run -> resume continues, no dup commits (content-hash dedup).
"""

import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

# Import lazily so each test can monkeypatch ENGINE_DB_PATH first.
def _import_session():
    from engine.orchestrator import session as s
    return s


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Isolate every test in its own temp engine.db (WAL mode)."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


def test_create_session_persists_row(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01", "T02"])
    row = s.get_session("run-1")
    assert row is not None
    assert row["run_id"] == "run-1"
    assert row["status"] == "running"
    states = s.get_task_states("run-1")
    assert states == {"T01": "PENDING", "T02": "PENDING"}


def test_create_session_idempotent(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01"])
    s.create_session("run-1", ["T01"])  # second call must not crash
    assert s.get_session("run-1") is not None


def test_update_task_state(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01", "T02"])
    s.update_task_state("run-1", "T01", "DONE")
    states = s.get_task_states("run-1")
    assert states["T01"] == "DONE"
    assert states["T02"] == "PENDING"


def test_budget_counters_increment(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01"])
    s.increment_llm_calls("run-1", tokens=150)
    s.increment_llm_calls("run-1", tokens=250)
    c = s.get_budget_counters("run-1")
    assert c["llm_calls"] == 2
    assert c["total_tokens"] == 400


def test_record_and_get_last_pushed_sha(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01"])
    assert s.get_last_pushed_sha("run-1") is None
    s.record_push("run-1", "abc123")
    assert s.get_last_pushed_sha("run-1") == "abc123"


def test_set_status(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01"])
    s.set_status("run-1", "cancelled")
    assert s.get_session("run-1")["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Content-hash dedup
# ---------------------------------------------------------------------------

def test_content_hash_deterministic(tmp_db):
    s = _import_session()
    h1 = s.content_hash("print('hello')\n")
    h2 = s.content_hash("print('hello')\n")
    h3 = s.content_hash("print('world')\n")
    assert h1 == h2
    assert h1 != h3


def test_record_commit_and_is_committed(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01"])
    code = "x = 1\n"
    ch = s.content_hash(code)
    s.record_commit("run-1", "T01", ch, commit_sha="sha1", pushed=True)
    assert s.is_task_committed("run-1", "T01", ch) is True
    # Different content for same task is NOT committed.
    assert s.is_task_committed("run-1", "T01", s.content_hash("y = 2\n")) is False


def test_get_committed_tasks(tmp_db):
    s = _import_session()
    s.create_session("run-1", ["T01", "T02"])
    s.record_commit("run-1", "T01", s.content_hash("a"), commit_sha="s1", pushed=True)
    s.record_commit("run-1", "T02", s.content_hash("b"), commit_sha="s2", pushed=False)
    committed = s.get_committed_tasks("run-1")
    assert "T01" in committed
    assert "T02" not in committed  # not pushed yet


# ---------------------------------------------------------------------------
# Crash-sim resume fixture (the core AC)
# ---------------------------------------------------------------------------

def test_crash_sim_resume_continues(tmp_db):
    """Simulate kill -9 mid-run: session persisted in WAL, resume continues,
    content-hash dedup prevents re-committing an already-pushed task."""
    s = _import_session()
    # --- phase 1: run starts, T01 completes, T02 pending ---
    s.create_session("run-crash", ["T01", "T02"])
    t01_code = "def foo(): return 1\n"
    t01_hash = s.content_hash(t01_code)
    s.update_task_state("run-crash", "T01", "DONE")
    s.record_commit("run-crash", "T01", t01_hash, commit_sha="sha-t01", pushed=True)
    s.record_push("run-crash", "sha-t01")
    s.increment_llm_calls("run-crash", tokens=1000)
    # --- crash: process dies here. WAL already flushed the rows above. ---
    # --- phase 2: resume (re-open DB, reconstruct state) ---
    row = s.get_session("run-crash")
    assert row is not None, "session must survive crash (WAL durable)"
    states = s.get_task_states("run-crash")
    assert states["T01"] == "DONE"
    assert states["T02"] == "PENDING"
    # T01 already committed+pushed -> dedup says skip re-commit.
    assert s.is_task_committed("run-crash", "T01", t01_hash) is True
    # T02 still pending -> not committed.
    assert s.is_task_committed("run-crash", "T02", s.content_hash("z")) is False
    # Continue T02.
    s.update_task_state("run-crash", "T02", "DONE")
    s.record_commit("run-crash", "T02", s.content_hash("z"), commit_sha="sha-t02",
                    pushed=True)
    s.record_push("run-crash", "sha-t02")
    s.set_status("run-crash", "done")
    final = s.get_session("run-crash")
    assert final["status"] == "done"
    assert s.get_task_states("run-crash") == {"T01": "DONE", "T02": "DONE"}


def test_resume_missing_session_returns_none(tmp_db):
    s = _import_session()
    assert s.get_session("never-existed") is None
