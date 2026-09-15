"""S2 regression: EditTaskHandler records edit_op_results rows via
record_edit_op_result after each attempt (applied on success, applied=0 on
retryable failure).

The vestigial "edit" retry key was removed from pipeline.py's retry dicts —
edit metrics now flow exclusively through record_edit_op_result.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.task_handler import _resolve_handler


class _MockEngine:
    def __init__(self, transport, config):
        self.transport = transport
        self.config = config


class _MockPipeline:
    def __init__(self, run_id="edit-run"):
        self.run_id = run_id


class _EditTransport:
    """Returns a valid SEARCH/REPLACE response for the edit atom."""

    def __init__(self):
        self.calls = []

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, max_tokens=512, temperature=0.3):
        self.calls.append(messages)
        fence = (
            "<<<<<<< SEARCH\n"
            "OLD_TEXT = 'before'\n"
            "=======\n"
            "NEW_TEXT = 'after'\n"
            ">>>>>>> REPLACE\n"
        )
        return {
            "content": fence,
            "total_tokens": 100,
            "thinking_tokens": 0,
            "finish_reason": "stop",
            "reasoning_content": "",
        }

    def run_command(self, cmd, timeout=60, cwd=None):
        # Support base64 write + cat read for commit-gate.
        import base64
        if "base64 -d >" in cmd:
            parts = cmd.split("base64 -d >")
            b64_part = parts[0].strip().rsplit("echo ", 1)[-1].strip()
            path = parts[1].strip()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64_part))
            return ("", "", 0)
        if cmd.strip().startswith("cat "):
            path = cmd.strip()[4:].strip()
            try:
                with open(path) as f:
                    return (f.read(), "", 0)
            except FileNotFoundError:
                return ("", "No such file", 1)
        return ("", "", 0)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import engine.state as state_mod

    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    state_mod.init_db(db_path)
    return db_path


def _edit_task():
    return Task(
        id="E01",
        title="Edit utils.py",
        description="Rename OLD_TEXT to NEW_TEXT",
        module="utils.py",
        task_type="edit",
    )


def _setup_project(tmp_path):
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "utils.py").write_text("OLD_TEXT = 'before'\n")
    return str(proj)


def test_edit_handler_records_metrics_on_success(tmp_path, monkeypatch, tmp_db):
    """S2: a successful edit attempt writes an edit_op_results row with
    applied=1."""
    from engine.edit_ops import EditTaskHandler

    project_dir = _setup_project(tmp_path)
    transport = _EditTransport()
    config = EngineConfig()
    engine = _MockEngine(transport, config)
    pipeline = _MockPipeline(run_id="s2-edit-run")

    task = _edit_task()
    handler = _resolve_handler(task, engine)
    assert isinstance(handler, EditTaskHandler)

    result = handler.run(task, pipeline, project_dir)
    assert result.state == "COMMIT", f"edit did not commit: {result.error_message}"

    from engine.state import _get_conn
    conn = _get_conn(tmp_db)
    rows = conn.execute(
        "SELECT run_id, task_id, attempt, applied FROM edit_op_results WHERE run_id=?",
        ("s2-edit-run",),
    ).fetchall()
    conn.close()
    assert len(rows) >= 1, "S2: no edit_op_results row after successful edit"
    # The successful attempt should have applied=1.
    assert any(r["applied"] == 1 for r in rows), (
        f"S2: no applied=1 row in edit_op_results, got {[dict(r) for r in rows]}")


def test_edit_handler_records_metrics_on_retry(tmp_path, monkeypatch, tmp_db):
    """S2: a failing-then-succeeding edit attempt writes edit_op_results
    rows for each attempt (applied=0 for failures, applied=1 for success)."""
    from engine.edit_ops import EditTaskHandler

    project_dir = _setup_project(tmp_path)

    class _FlakyTransport(_EditTransport):
        def __init__(self):
            super().__init__()
            self._attempt = 0

        def curl_beellama(self, port, messages, max_tokens=512, temperature=0.3):
            self._attempt += 1
            # First attempt: return no edit blocks (forces a retryable fail).
            if self._attempt == 1:
                return {
                    "content": "I don't have any edits to make.",
                    "total_tokens": 10,
                    "thinking_tokens": 0,
                    "finish_reason": "stop",
                    "reasoning_content": "",
                }
            return super().curl_beellama(port, messages, max_tokens, temperature)

    transport = _FlakyTransport()
    config = EngineConfig()
    engine = _MockEngine(transport, config)
    pipeline = _MockPipeline(run_id="s2-retry-run")

    task = _edit_task()
    handler = _resolve_handler(task, engine)

    result = handler.run(task, pipeline, project_dir)
    # Should succeed on attempt 2.
    assert result.state == "COMMIT", f"edit did not commit: {result.error_message}"

    from engine.state import _get_conn
    conn = _get_conn(tmp_db)
    rows = conn.execute(
        "SELECT attempt, applied FROM edit_op_results WHERE run_id=? ORDER BY attempt",
        ("s2-retry-run",),
    ).fetchall()
    conn.close()
    assert len(rows) >= 2, (
        f"S2: expected ≥2 edit_op_results rows (1 fail + 1 success), got {len(rows)}")
    # At least one failure (applied=0) and one success (applied=1).
    applied_vals = [r["applied"] for r in rows]
    assert 0 in applied_vals, "S2: missing applied=0 (failure) row"
    assert 1 in applied_vals, "S2: missing applied=1 (success) row"
