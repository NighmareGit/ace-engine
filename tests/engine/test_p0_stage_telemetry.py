"""P0 tests: stage telemetry + state.py env override (design T1.1, T2.1-T2.5).

Mock transport only — no network, no GPU. Drives the real Engine.run() with a
mock transport whose generated code fails validation, then inspects the
engine_task_results table and the returned TaskResult.
"""

import json
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.engine import Engine
from engine import state as engine_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point engine.state at a temp DB and init it."""
    test_db = str(tmp_path / "engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", test_db)
    engine_state.init_db()
    return test_db


class _FailingValidationTransport:
    """Mock transport: generated code always fails import validation."""

    def __init__(self):
        self.calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        return {
            "content": "```python\nimport nonexistent_module_12345\n```",
            "reasoning_content": "",
            "total_tokens": 100,
            "thinking_tokens": 0,
            "prompt_tokens": 50,
            "completion_tokens": 50,
            "predicted_per_second": 10.0,
        }

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)


def _make_task():
    return Task(id="T01", title="Test", description="test", module="app.py")


def _run_failing_task(tmp_db, monkeypatch, retries=3):
    """Run the real Engine.run() against code that always fails validation."""
    monkeypatch.setattr("engine.engine.parse_prd", lambda _: [_make_task()])
    transport = _FailingValidationTransport()
    eng = Engine(transport=transport, config=EngineConfig(max_retries_generate=retries))
    result = eng.run("/tmp/prd.md", "/tmp/project")
    return result, eng


# ---------------------------------------------------------------------------
# T1.1 — state.py honors ENGINE_DB_PATH
# ---------------------------------------------------------------------------

def test_state_honors_engine_db_path(tmp_path, monkeypatch):
    """Setting ENGINE_DB_PATH makes _get_conn open that path."""
    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("ENGINE_DB_PATH", custom)
    conn = engine_state._get_conn()
    assert os.path.exists(custom)
    conn.close()


# ---------------------------------------------------------------------------
# T2.1 — a failed validation persists a parseable validation_result row
# ---------------------------------------------------------------------------

def test_failed_validation_persists_stages(tmp_db, monkeypatch):
    _run_failing_task(tmp_db, monkeypatch, retries=1)
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT validation_result, error_message FROM engine_task_results "
        "WHERE task_id='T01' AND state='VALIDATE' ORDER BY attempts ASC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None, "expected a VALIDATE row after failed validation"
    stages = json.loads(row[0])
    assert isinstance(stages, list)
    assert all(set(s.keys()) >= {"stage", "file", "passed", "error"} for s in stages)
    # error_message should carry the failing stage detail
    assert row[1] is not None and row[1] != ""


# ---------------------------------------------------------------------------
# T2.2 — each failed attempt appends a row (ascending attempts)
# ---------------------------------------------------------------------------

def test_each_failed_attempt_appends_row(tmp_db, monkeypatch):
    _run_failing_task(tmp_db, monkeypatch, retries=3)
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT attempts FROM engine_task_results WHERE task_id='T01' "
        "AND state='VALIDATE' ORDER BY attempts ASC"
    ).fetchall()
    conn.close()
    assert len(rows) == 3, f"expected 3 attempt rows, got {len(rows)}"
    assert [r[0] for r in rows] == [1, 2, 3]


# ---------------------------------------------------------------------------
# T2.3 — terminal TaskResult.error_message contains stage + file + error
# ---------------------------------------------------------------------------

def test_terminal_error_message_has_stage_detail(tmp_db, monkeypatch):
    result, _ = _run_failing_task(tmp_db, monkeypatch, retries=2)
    task_result = result.tasks[0]
    assert task_result.state == "FAILED"
    assert "imports" in task_result.error_message
    assert "app.py" in task_result.error_message
    assert "nonexistent_module_12345" in task_result.error_message


# ---------------------------------------------------------------------------
# T2.4 — TaskResult.validation_stages == last attempt's stages
# ---------------------------------------------------------------------------

def test_validation_stages_captured(tmp_db, monkeypatch):
    result, _ = _run_failing_task(tmp_db, monkeypatch, retries=2)
    task_result = result.tasks[0]
    assert task_result.validation_stages is not None
    assert isinstance(task_result.validation_stages, list)
    failing = [s for s in task_result.validation_stages if not s["passed"]]
    assert len(failing) >= 1
    assert failing[0]["stage"] == "imports"


# ---------------------------------------------------------------------------
# T2.5 — WARNING log emitted at exhaustion with the failing stage name
# ---------------------------------------------------------------------------

def test_warning_log_at_exhaustion(tmp_db, monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="engine.engine"):
        _run_failing_task(tmp_db, monkeypatch, retries=2)
    messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("validation exhausted" in m and "imports" in m for m in messages), \
        f"expected a WARNING about validation exhaustion, got: {messages}"
