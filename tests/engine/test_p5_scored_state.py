"""P5 tests: scored_state flag (design T6.1-T6.4).

Mock transport only. Covers:
  - Score on a validation-failed task -> engine_scores.scored_state == "validation_failed" (T6.1)
  - Score on a committed task -> scored_state == "committed" (T6.2)
  - report.json per-task scores includes scored_state (T6.3)
  - Deterministic gate still blocks commit independent of LLM score (T6.4)
"""

import json
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.engine import Engine, build_run_report
from engine import llm_judge
from engine import state as engine_state


# ---------------------------------------------------------------------------
# T6.1 — score on a validation-failed task -> scored_state == "validation_failed"
# ---------------------------------------------------------------------------

def test_scored_state_validation_failed(tmp_path, monkeypatch):
    db = str(tmp_path / "engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", db)
    engine_state.init_db()

    class _T:
        def curl_beellama(self, port, messages, **kwargs):
            return {"content": json.dumps({d: 7 for d in llm_judge.DIMENSIONS}),
                    "finish_reason": "stop", "model": "mock-judge"}
        def get_model_list(self, port):
            return ["mock-judge"]

    r = llm_judge.score_task("run-x", "T01", "t", "code", _T(), save=True,
                              scored_state="validation_failed")
    assert r["scored_state"] == "validation_failed"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT scored_state FROM engine_scores WHERE task_id='T01'").fetchone()
    conn.close()
    assert row[0] == "validation_failed"


# ---------------------------------------------------------------------------
# T6.2 — score on a committed task -> scored_state == "committed"
# ---------------------------------------------------------------------------

def test_scored_state_committed(tmp_path, monkeypatch):
    db = str(tmp_path / "engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", db)
    engine_state.init_db()

    class _T:
        def curl_beellama(self, port, messages, **kwargs):
            return {"content": json.dumps({d: 9 for d in llm_judge.DIMENSIONS}),
                    "finish_reason": "stop", "model": "mock-judge"}
        def get_model_list(self, port):
            return ["mock-judge"]

    r = llm_judge.score_task("run-x", "T01", "t", "code", _T(), save=True,
                              scored_state="committed")
    assert r["scored_state"] == "committed"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT scored_state FROM engine_scores WHERE task_id='T01'").fetchone()
    conn.close()
    assert row[0] == "committed"


# ---------------------------------------------------------------------------
# T6.3 — report.json per-task scores includes scored_state
# ---------------------------------------------------------------------------

def test_report_json_includes_scored_state(tmp_path, monkeypatch):
    db = str(tmp_path / "engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", db)
    engine_state.init_db()

    class _T:
        def curl_beellama(self, port, messages, **kwargs):
            return {"content": json.dumps({d: 8 for d in llm_judge.DIMENSIONS}),
                    "finish_reason": "stop", "model": "mock-judge"}
        def get_model_list(self, port):
            return ["mock-judge"]

    scores = llm_judge.score_task("run-x", "T01", "t", "code", _T(), save=True,
                                   scored_state="committed")
    # Simulate TaskResult.scores carrying scored_state into the report.
    from engine.engine import RunResult, TaskResult
    tr = TaskResult(task_id="T01", title="t", state="COMMIT", attempts=1,
                    commit_sha="abc", time_s=1.0, tokens=100, error_message=None,
                    scores=scores)
    result = RunResult(run_id="run-x", success=True, prd_path="p.md",
                       project_path="/p", total_time_s=1.0, total_tokens=100,
                       tasks=[tr], states_visited=[], error_message=None)
    report = build_run_report(result, EngineConfig()).to_dict()
    per_task = report["per_task"][0]
    assert per_task["scores"]["scored_state"] == "committed"


# ---------------------------------------------------------------------------
# T6.4 — deterministic gate still blocks commit independent of LLM score
#        (a 10/10 on invalid code is still not committed).
# ---------------------------------------------------------------------------

class _InvalidCodeTransport:
    """Generates code that fails import validation (deterministic gate)."""

    def __init__(self):
        self.calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        if port == 8080:
            # LLM judge gives a perfect 10/10 even on invalid code.
            return {"content": json.dumps({d: 10 for d in llm_judge.DIMENSIONS}),
                    "finish_reason": "stop", "model": "mock-judge"}
        return {"content": "```python\nimport nonexistent_module_12345\n```",
                "finish_reason": "stop", "reasoning_content": "",
                "thinking_tokens": 0, "total_tokens": 50,
                "prompt_tokens": 20, "completion_tokens": 30,
                "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        return ("", "", 0)

    def get_model_list(self, port):
        return ["mock-judge"]


def test_deterministic_gate_blocks_commit_despite_perfect_score(tmp_path, monkeypatch):
    """A 10/10 LLM score on invalid code must NOT be committed (T6.4)."""
    _tmp_db = str(tmp_path / "engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", _tmp_db)
    engine_state.init_db()

    import engine.engine as eng_mod
    import engine.committer as cm
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py")]
    cm._ensure_repo_exists = lambda r, a: None
    try:
        # non-3090 config => generation on 8082, judge on 8080
        cfg = EngineConfig(model_config="3070-qwen35-9b", judge_mode="llm")
        eng = Engine(transport=_InvalidCodeTransport(), config=cfg)
        result = eng.run("/tmp/prd.md", "/tmp/project")
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure

    tr = result.tasks[0]
    # Deterministic gate blocks commit -> task is FAILED, not COMMIT.
    assert tr.state == "FAILED"
    # But the LLM judge still scored it (score is data, not a gate).
    assert tr.scores is not None
    assert tr.scores["overall"] == 10.0
    # And the score is flagged as validation_failed.
    assert tr.scores["scored_state"] == "validation_failed"
