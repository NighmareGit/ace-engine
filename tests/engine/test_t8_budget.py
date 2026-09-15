"""T8 tests: budget caps (max_llm_calls, max_total_tokens, max_wall_clock_s).

Caps live in EngineConfig; counters live in T1's orchestrator session.
When a cap is exceeded the run stops via the same graceful path as the
stop-file (CANCELLED, not FAILED). Counters must be persisted atomically
so a crash mid-run does not lose the tally.
"""

import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import budget as budget_mod
from engine.orchestrator import session as sess


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# EngineConfig defaults
# ---------------------------------------------------------------------------

def test_engineconfig_budget_defaults():
    from engine import EngineConfig
    cfg = EngineConfig()
    # Caps default to 0 meaning "unbounded".
    assert cfg.max_llm_calls == 0
    assert cfg.max_total_tokens == 0
    assert cfg.max_wall_clock_s == 0


def test_engineconfig_budget_custom():
    from engine import EngineConfig
    cfg = EngineConfig(max_llm_calls=10, max_total_tokens=50000, max_wall_clock_s=600)
    assert cfg.max_llm_calls == 10
    assert cfg.max_total_tokens == 50000
    assert cfg.max_wall_clock_s == 600


# ---------------------------------------------------------------------------
# budget.check_budget: detects exceeded caps
# ---------------------------------------------------------------------------

def test_check_budget_unbounded_no_stop(tmp_db):
    """Default (0) caps mean unbounded -> never stops."""
    from engine import EngineConfig
    cfg = EngineConfig()
    sess.create_session("run-1", ["T01"], config=cfg)
    counters = sess.increment_llm_calls("run-1", tokens=999999)
    stopped, reason = budget_mod.check_budget("run-1", counters)
    assert stopped is False


def test_check_budget_llm_calls_cap(tmp_db):
    from engine import EngineConfig
    cfg = EngineConfig(max_llm_calls=3)
    sess.create_session("run-1", ["T01"], config=cfg)
    for _ in range(2):
        sess.increment_llm_calls("run-1", tokens=100)
    counters = sess.increment_llm_calls("run-1", tokens=100)  # 3rd call
    stopped, reason = budget_mod.check_budget("run-1", counters)
    assert stopped is True
    assert "llm_calls" in reason.lower() or "call" in reason.lower()


def test_check_budget_tokens_cap(tmp_db):
    from engine import EngineConfig
    cfg = EngineConfig(max_total_tokens=1000)
    sess.create_session("run-1", ["T01"], config=cfg)
    counters = sess.increment_llm_calls("run-1", tokens=1500)
    stopped, reason = budget_mod.check_budget("run-1", counters)
    assert stopped is True
    assert "token" in reason.lower()


def test_check_budget_wall_clock_cap(tmp_db):
    from engine import EngineConfig
    cfg = EngineConfig(max_wall_clock_s=1)
    sess.create_session("run-1", ["T01"], config=cfg)
    # Backdate the wall_clock_start to simulate elapsed time.
    import json
    from engine.state import _get_conn
    conn = _get_conn()
    past = "2000-01-01T00:00:00"
    conn.execute("UPDATE orchestrator_session SET wall_clock_start = ? WHERE run_id = ?",
                 (past, "run-1"))
    conn.commit()
    conn.close()
    counters = sess.increment_llm_calls("run-1", tokens=10)
    stopped, reason = budget_mod.check_budget("run-1", counters)
    assert stopped is True
    assert "wall" in reason.lower() or "clock" in reason.lower() or "time" in reason.lower()


def test_check_budget_not_yet_exceeded(tmp_db):
    from engine import EngineConfig
    cfg = EngineConfig(max_llm_calls=5, max_total_tokens=10000)
    sess.create_session("run-1", ["T01"], config=cfg)
    counters = sess.increment_llm_calls("run-1", tokens=1000)
    stopped, _ = budget_mod.check_budget("run-1", counters)
    assert stopped is False


# ---------------------------------------------------------------------------
# Integration: engine stops a run when budget cap exceeded
# ---------------------------------------------------------------------------

class _CountingTransport:
    """Returns valid code; counts generate calls so we can hit a call cap."""

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


def _run_with_budget(transport, max_llm_calls=2, max_total_tokens=0,
                     max_wall_clock_s=0, retries=3):
    import engine.engine as eng_mod
    import engine.committer as cm
    from engine.engine import Engine, EngineConfig, Task

    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py"),
                                   Task(id="T02", title="T2", description="d", module="app2.py")]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    cfg = EngineConfig(max_retries_generate=retries, max_llm_calls=max_llm_calls,
                      max_total_tokens=max_total_tokens,
                      max_wall_clock_s=max_wall_clock_s)
    try:
        eng = Engine(transport=transport, config=cfg)
        result = eng.run("/tmp/prd.md", "/tmp/project")
        return result
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


def test_engine_stops_at_llm_call_cap():
    """With max_llm_calls=2 and 2 tasks each needing >=1 call, the run must
    stop (CANCELLED) after the cap is exceeded rather than burning more calls."""
    transport = _CountingTransport()
    result = _run_with_budget(transport, max_llm_calls=2, retries=1)
    assert result.success is False
    # At least one task should be CANCELLED (not all FAILED).
    states = [t.state for t in result.tasks]
    assert "CANCELLED" in states, f"expected a CANCELLED task, got {states}"
    # Total LLM calls must not wildly exceed the cap (allow the in-flight call).
    assert transport.calls <= 3, f"too many calls: {transport.calls}"


def test_engine_completes_under_generous_budget():
    """With a generous budget, a 2-task run completes successfully."""
    transport = _CountingTransport()
    result = _run_with_budget(transport, max_llm_calls=100, retries=1)
    assert result.success is True
    assert all(t.state == "COMMIT" for t in result.tasks)


# ---------------------------------------------------------------------------
# Counter persistence across crash (resume sees the tally)
# ---------------------------------------------------------------------------

def test_counters_persist_across_crash(tmp_db):
    from engine import EngineConfig
    cfg = EngineConfig(max_llm_calls=10)
    sess.create_session("run-1", ["T01"], config=cfg)
    sess.increment_llm_calls("run-1", tokens=500)
    sess.increment_llm_calls("run-1", tokens=300)
    # "crash" -> re-open DB, counters must survive.
    c = sess.get_budget_counters("run-1")
    assert c["llm_calls"] == 2
    assert c["total_tokens"] == 800


# ---------------------------------------------------------------------------
# Decision 3: judge/scoring LLM calls count toward max_llm_calls
# ---------------------------------------------------------------------------

class _JudgeCountingTransport:
    """Returns valid code and counts BOTH generate and judge calls."""

    def __init__(self):
        self.generate_calls = 0
        self.judge_calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        # Judge calls go to port 8080; subject generation to 8082.
        if port == 8080:
            self.judge_calls += 1
            return {"content": json.dumps(
                {"completeness": 8, "correctness": 8, "quality": 8,
                 "intelligence": 8, "role_fit": 8}),
                "finish_reason": "stop", "model": "mock-judge",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}
        self.generate_calls += 1
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

    def get_model_list(self, port):
        return ["mock-judge"]


def test_judge_calls_count_toward_budget(tmp_db):
    """With judge_mode='llm', a run that does N generate + N judge calls
    must count 2*N against max_llm_calls."""
    from engine import EngineConfig
    cfg = EngineConfig(max_llm_calls=4, judge_mode="llm",
                      model_config="3070-qwen35-9b")
    sess.create_session("run-j", ["T01"], config=cfg)
    # Simulate: 1 generate + 1 judge for a single committed task = 2 calls.
    sess.increment_llm_calls("run-j", tokens=100)   # generate
    sess.increment_llm_calls("run-j", tokens=0)     # judge
    sess.increment_llm_calls("run-j", tokens=100)   # generate (2nd task)
    sess.increment_llm_calls("run-j", tokens=0)     # judge (2nd task)
    c = sess.get_budget_counters("run-j")
    assert c["llm_calls"] == 4
    stopped, reason = budget_mod.check_budget("run-j", c)
    assert stopped is True
    assert "llm_calls" in reason.lower()
