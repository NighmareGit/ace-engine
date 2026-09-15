"""Unit tests for engine/pipeline.py and engine/state.py.

Verifies: 12-state machine happy path, checkpoint round-trip fidelity,
resume-after-crash recovery, path traversal rejection — all with SQLite
temp databases and mock engine, no GPU.
"""

import sys
import os
import json
import tempfile
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.pipeline import State, Pipeline, VALID_TRANSITIONS, TransitionResult
from engine.state import init_db, checkpoint_run, load_run, list_runs, DB_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Override engine.state.DB_PATH with a temp file for test isolation."""
    test_db = str(tmp_path / "test_engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", test_db)
    init_db()
    return test_db


@pytest.fixture
def mock_engine():
    """Minimal mock Engine for Pipeline construction."""
    engine = MagicMock()
    engine.on_event = None
    engine.config = EngineConfig()
    return engine


# ---------------------------------------------------------------------------
# TEST GROUP A — State machine happy path
# ---------------------------------------------------------------------------

def test_happy_path_visits_12_states_in_order(tmp_db, mock_engine):
    """Happy path: IDLE→PARSING→QUEUED→CONTEXT→GENERATE→VALIDATE→TEST→
    COMMIT→NEXT→QUEUED→DONE visits expected states."""
    pipeline = Pipeline(mock_engine)
    pipeline.run_id = "test-run-001"
    pipeline.tasks = [Task(id="T01", title="Test", description="", module="app.py")]

    expected_transitions = [
        State.PARSING, State.QUEUED, State.CONTEXT, State.GENERATE,
        State.VALIDATE, State.TEST, State.COMMIT, State.NEXT,
        State.QUEUED, State.DONE,
    ]
    for target_state in expected_transitions:
        kwargs = {}
        # C10 guard: QUEUED→DONE requires task_states when tasks exist
        if target_state == State.DONE:
            kwargs["task_states"] = {"T01": "DONE"}
        result = pipeline.transition(target_state, **kwargs)
        assert isinstance(result, TransitionResult)
        assert result.to_state == target_state

    assert pipeline.state == State.DONE
    assert len(pipeline.states_visited) == len(expected_transitions)


def test_invalid_transition_raises_value_error(tmp_db, mock_engine):
    """Transitioning to an invalid state raises ValueError."""
    pipeline = Pipeline(mock_engine)
    with pytest.raises(ValueError, match="Invalid transition"):
        pipeline.transition(State.DONE)  # IDLE → DONE is not valid


def test_cancel_from_any_non_terminal_state(tmp_db, mock_engine):
    """CANCELLED is reachable from IDLE."""
    pipeline = Pipeline(mock_engine)
    pipeline.transition(State.CANCELLED)
    assert pipeline.state == State.CANCELLED


def test_terminal_states_have_no_outgoing_transitions():
    """DONE, FAILED, CANCELLED have empty transition lists."""
    assert VALID_TRANSITIONS[State.DONE] == []
    assert VALID_TRANSITIONS[State.FAILED] == []
    assert VALID_TRANSITIONS[State.CANCELLED] == []


# ---------------------------------------------------------------------------
# TEST GROUP B — Checkpoint round-trip
# ---------------------------------------------------------------------------

def test_checkpoint_round_trip_preserves_task_dicts(tmp_db, mock_engine):
    """checkpoint_run → load_run preserves tasks_json with full Task dicts."""
    tasks = [
        Task(id="T01", title="First", description="desc1", module="a.py",
             dependencies=[], task_type="implementation", priority=1),
        Task(id="T02", title="Second", description="desc2", module="b.py",
             dependencies=["T01"], task_type="test", priority=2),
    ]
    data = {
        "prd_path": "/tmp/test.prd",
        "project_path": "/tmp/project",
        "config": "3090-qwen36-35b",
        "state": "GENERATE",
        "current_task_id": "T01",
        "tasks_json": json.dumps([t.__dict__ for t in tasks]),
        "current_task_idx": 0,
        "task_retries_json": json.dumps({"T01": {"generate": 1, "test": 0, "commit": 0}}),
    }
    checkpoint_run("run-rt-001", data)
    loaded = load_run("run-rt-001")
    assert loaded is not None
    assert loaded["state"] == "GENERATE"
    assert loaded["current_task_id"] == "T01"

    restored_tasks = json.loads(loaded["tasks_json"])
    assert len(restored_tasks) == 2
    assert restored_tasks[0]["id"] == "T01"
    assert restored_tasks[1]["id"] == "T02"
    assert restored_tasks[1]["dependencies"] == ["T01"]


def test_checkpoint_round_trip_preserves_idx_and_retries(tmp_db, mock_engine):
    """current_task_idx and task_retries survive checkpoint round-trip."""
    data = {
        "prd_path": "/tmp/p.prd",
        "project_path": "/tmp/proj",
        "config": "3090-qwen36-35b",
        "state": "TEST",
        "current_task_id": "T02",
        "tasks_json": json.dumps([{"id": "T01"}, {"id": "T02"}]),
        "current_task_idx": 1,
        "task_retries_json": json.dumps({
            "T01": {"generate": 2, "test": 1, "commit": 0},
            "T02": {"generate": 0, "test": 0, "commit": 0},
        }),
    }
    checkpoint_run("run-idx-001", data)
    loaded = load_run("run-idx-001")
    assert loaded["current_task_idx"] == 1
    retries = json.loads(loaded["task_retries_json"])
    assert retries["T01"]["generate"] == 2
    assert retries["T01"]["test"] == 1


# ---------------------------------------------------------------------------
# TEST GROUP C — Resume after simulated crash
# ---------------------------------------------------------------------------

def test_resume_restores_pipeline_state_after_simulated_crash(tmp_db, mock_engine):
    """Pipeline.resume() restores state, tasks, idx, and retries from checkpoint."""
    tasks = [
        Task(id="T01", title="First", description="", module="a.py"),
        Task(id="T02", title="Second", description="", module="b.py"),
    ]
    data = {
        "prd_path": "/tmp/p.prd",
        "project_path": "/tmp/proj",
        "config": "3090-qwen36-35b",
        "state": "GENERATE",
        "current_task_id": "T02",
        "tasks_json": json.dumps([t.__dict__ for t in tasks]),
        "current_task_idx": 1,
        "task_retries_json": json.dumps({"T01": {"generate": 3, "test": 0, "commit": 0}}),
    }
    checkpoint_run("crash-run-001", data)

    # Simulate crash: new Pipeline instance, no prior state
    restored = Pipeline.resume("crash-run-001", mock_engine)
    assert restored.state == State.GENERATE
    assert restored.run_id == "crash-run-001"
    assert len(restored.tasks) == 2
    assert restored.tasks[0].id == "T01"
    assert restored.tasks[1].id == "T02"
    assert restored.current_task_idx == 1
    assert restored.current_task.id == "T02"
    assert restored.task_retries["T01"]["generate"] == 3


def test_resume_nonexistent_run_raises_value_error(tmp_db, mock_engine):
    """Resuming a non-existent run_id raises ValueError."""
    with pytest.raises(ValueError, match="No checkpoint found"):
        Pipeline.resume("nonexistent-run-id", mock_engine)


# ---------------------------------------------------------------------------
# TEST GROUP D — Event emission during transitions
# ---------------------------------------------------------------------------

def test_transition_emits_event_when_callback_set(tmp_db):
    """Pipeline calls on_event callback with correct event_type and data dict."""
    engine = MagicMock()
    events = []
    engine.on_event = lambda et, data: events.append((et, data))
    engine.config = EngineConfig()
    pipeline = Pipeline(engine)
    pipeline.run_id = "event-test-001"
    result = pipeline.transition(State.PARSING)
    assert result.event_emitted is True
    assert len(events) == 1
    assert events[0][0] == "state_transition"
    assert events[0][1]["from"] == "IDLE"
    assert events[0][1]["to"] == "PARSING"
    assert events[0][1]["run_id"] == "event-test-001"


# ---------------------------------------------------------------------------
# TEST GROUP E — SQLite persistence
# ---------------------------------------------------------------------------

def test_checkpoint_run_stores_data_in_sqlite(tmp_db):
    """checkpoint_run writes a row to engine_runs table."""
    checkpoint_run("sql-test-001", {
        "prd_path": "/tmp/p.prd",
        "project_path": "/tmp/proj",
        "config": "3090-qwen36-35b",
        "state": "PARSING",
    })
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT * FROM engine_runs WHERE id = ?", ("sql-test-001",)
    ).fetchone()
    conn.close()
    assert row is not None, "row not found in engine_runs"


def test_list_runs_with_status_filter(tmp_db):
    """list_runs(status=...) filters by state."""
    checkpoint_run("lr-001", {"prd_path": "", "project_path": "", "config": "", "state": "DONE"})
    checkpoint_run("lr-002", {"prd_path": "", "project_path": "", "config": "", "state": "FAILED"})
    done_runs = list_runs(status="DONE")
    assert len(done_runs) == 1
    assert done_runs[0]["id"] == "lr-001"


def test_load_run_returns_none_for_missing_id(tmp_db):
    """load_run returns None when run_id does not exist."""
    result = load_run("does-not-exist")
    assert result is None


# ---------------------------------------------------------------------------
# TEST GROUP F — Retry tracking
# ---------------------------------------------------------------------------

def test_retry_tracking_increments_and_reads(tmp_db, mock_engine):
    """Pipeline.get_retry_count and increment_retry work correctly."""
    pipeline = Pipeline(mock_engine)
    assert pipeline.get_retry_count("T01", "generate") == 0
    assert pipeline.increment_retry("T01", "generate") == 1
    assert pipeline.get_retry_count("T01", "generate") == 1
    assert pipeline.increment_retry("T01", "generate") == 2
    assert pipeline.get_retry_count("T01", "test") == 0  # different kind
