"""Regression tests for the three smoke-verified wiring gaps.

GAP 1: T7 report emitted by engine run() on DONE/CANCELLED/FAILED.
GAP 2: T6 record_push called after successful commit.
GAP 3: T4 re-plan loops up to max_replans_per_task (cycle 2 exercises depth cap).
"""

import os
import sys
import json
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# GAP 1: T7 report emission by engine run()
# ---------------------------------------------------------------------------

class _PassingTransport:
    """Returns valid code, succeeds at all git ops."""

    def __init__(self):
        self.calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        return {"content": "```python\nx = 1\n```", "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def _run_engine(transport, tmp_path, **cfg_kwargs):
    """Run the engine, return (result, project_path)."""
    import engine.engine as eng_mod
    import engine.committer as cm
    from engine.engine import Engine, EngineConfig, Task

    project_path = str(tmp_path / "project")
    events = []
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d",
                                        module="app.py",
                                        acceptance_criteria=["must work"])]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    cfg = EngineConfig(**cfg_kwargs)
    try:
        eng = Engine(transport=transport, config=cfg,
                     on_event=lambda et, d: events.append((et, d)))
        result = eng.run("/tmp/prd.md", project_path)
        return result, project_path, events
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


def test_gap1_report_emitted_on_DONE(tmp_path, monkeypatch):
    """GAP 1: engine run() emits report.md + orchestrator_reports on DONE."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    transport = _PassingTransport()
    result, project_path, _ = _run_engine(transport, tmp_path)
    assert result.success is True
    # report.md exists.
    report_file = os.path.join(project_path, "run", result.run_id, "report.md")
    assert os.path.exists(report_file), f"report.md not found at {report_file}"
    content = open(report_file).read()
    assert "# Run Report" in content
    assert "Acceptance Criteria Checklist" in content
    # orchestrator_reports table has a row.
    from engine.orchestrator.report_persistence import get_report_history
    rows = get_report_history(result.run_id)
    assert len(rows) >= 1
    assert rows[0]["success"] == 1


def test_gap1_report_emitted_on_CANCELLED(tmp_path, monkeypatch):
    """GAP 1: engine run() emits report.md on CANCELLED (budget cap)."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    transport = _PassingTransport()
    # max_llm_calls=1 → second task cancelled.
    result, project_path, _ = _run_engine(
        transport, tmp_path, max_llm_calls=1, max_retries_generate=3)
    assert result.success is False
    states = [t.state for t in result.tasks]
    assert "CANCELLED" in states
    # report.md exists.
    report_file = os.path.join(project_path, "run", result.run_id, "report.md")
    assert os.path.exists(report_file), "report.md not emitted on CANCELLED"
    content = open(report_file).read()
    assert "CANCELLED" in content


def test_gap1_report_emitted_on_FAILED(tmp_path, monkeypatch):
    """GAP 1: engine run() emits report.md on FAILED."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)

    class _FailingTransport(_PassingTransport):
        def curl_beellama(self, port, messages, **kwargs):
            self.calls += 1
            return {"content": "```python\nimport nonexistent_xyz\n```",
                    "finish_reason": "stop", "reasoning_content": "",
                    "thinking_tokens": 0, "total_tokens": 50,
                    "prompt_tokens": 20, "completion_tokens": 30,
                    "predicted_per_second": 5.0}

    transport = _FailingTransport()
    result, project_path, _ = _run_engine(
        transport, tmp_path, max_retries_generate=1, max_replans_per_task=0)
    assert result.success is False
    states = [t.state for t in result.tasks]
    assert "FAILED" in states
    # report.md exists.
    report_file = os.path.join(project_path, "run", result.run_id, "report.md")
    assert os.path.exists(report_file), "report.md not emitted on FAILED"


# ---------------------------------------------------------------------------
# GAP 2: T6 record_push after successful commit
# ---------------------------------------------------------------------------

def test_gap2_record_push_after_successful_commit(tmp_path, monkeypatch):
    """GAP 2: after a successful push, last_pushed_sha == pushed sha."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    transport = _PassingTransport()
    result, _, _ = _run_engine(transport, tmp_path)
    assert result.success is True
    # The commit SHA was recorded.
    from engine.orchestrator import session as sess
    sha = sess.get_last_pushed_sha(result.run_id)
    assert sha is not None, "last_pushed_sha is None after successful push"
    assert sha == "abc123"


def test_gap2_record_push_not_set_on_failed_commit(tmp_path, monkeypatch):
    """GAP 2: when commit/push fails, last_pushed_sha stays None and run
    continues (does not crash)."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)

    class _FailedPushTransport(_PassingTransport):
        def run_git(self, project_path, *args):
            if args[0] == "push":
                return ("", "fatal: transient network error", 1)
            if args[0] == "rev-parse":
                return ("abc123", "", 0)
            return ("", "", 0)

    transport = _FailedPushTransport()
    result, _, _ = _run_engine(transport, tmp_path, max_retries_commit=1)
    # Run should complete (not crash) even though push failed.
    from engine.orchestrator import session as sess
    sha = sess.get_last_pushed_sha(result.run_id)
    assert sha is None, "last_pushed_sha should be None after failed push"


# ---------------------------------------------------------------------------
# GAP 3: T4 re-plan cycle 2 (depth cap exercised)
# ---------------------------------------------------------------------------

class _ReplanTransport:
    """Generate fails validation; re-plan port returns a valid patch on every
    call. The patch amends the task so the SECOND retry passes."""

    def __init__(self):
        self.gen_calls = 0
        self.replan_calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        if port == 8080:
            # Re-plan brain response.
            self.replan_calls += 1
            return {"content": json.dumps({
                "action": "re-spec", "task_id": "T01",
                "spec_patch": {
                    "hypothesis": "flask is not available",
                    "constraint_updates": ["Use stdlib http.server."],
                    "dependency_allowlist_additions": [],
                    "acceptance_amendments": [],
                },
                "reason": "validation exhausted on import 'flask'.",
            }), "finish_reason": "stop", "reasoning_content": "",
                "thinking_tokens": 0, "total_tokens": 100,
                "prompt_tokens": 50, "completion_tokens": 50,
                "predicted_per_second": 5.0}
        # Generation: fail first N-1 times, then succeed.
        self.gen_calls += 1
        if self.gen_calls <= 3:  # attempts 1,2 (initial) + 1 (replan1 retry)
            return {"content": "```python\nimport nonexistent_module_12345\n```",
                    "finish_reason": "stop", "reasoning_content": "",
                    "thinking_tokens": 0, "total_tokens": 50,
                    "prompt_tokens": 20, "completion_tokens": 30,
                    "predicted_per_second": 5.0}
        return {"content": "```python\nx = 1\n```", "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def test_gap3_replan_cycle2_exercises_depth_cap(tmp_path, monkeypatch):
    """GAP 3: re-plan loops up to max_replan_per_task (2). First re-plan
    applies, amended task fails again, second re-plan fires, then exhaustion."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    transport = _ReplanTransport()
    import engine.engine as eng_mod
    import engine.committer as cm
    from engine.engine import Engine, EngineConfig, Task

    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d",
                                        module="app.py")]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    cfg = EngineConfig(max_retries_generate=1, max_replans_per_task=2,
                      replan_model_port=8080, model_config="3070-qwen35-9b")
    try:
        eng = Engine(transport=transport, config=cfg)
        result = eng.run("/tmp/prd.md", str(tmp_path / "project"))
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure

    # The re-plan brain should have been called at least twice (cycle 1 + 2).
    assert transport.replan_calls >= 2, (
        f"expected >=2 re-plan calls, got {transport.replan_calls}")
    # Telemetry records both re-plan attempts.
    from engine.orchestrator import session as sess
    history = sess.get_replan_history(result.run_id)
    assert len(history) >= 2, f"expected >=2 re-plan telemetry rows, got {len(history)}"
