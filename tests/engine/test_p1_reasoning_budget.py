"""P1 tests: reasoning-budget strategy (design T3.1-T3.6).

Mock transport only. Covers:
  - _is_reasoning_exhaustion true/false table (T3.1)
  - GeneratedCode.finish_reason populated (T3.2)
  - Generation escalates once on exhaustion, surfaces distinct error on second (T3.3)
  - Escalation retry count <= 1 even with max_retries_generate > 1 (T3.4)
  - Judge escalation: 4000 -> higher on exhaustion, zeros+error if still exhausted (T3.5)
  - Non-exhaustion empty content is NOT treated as exhaustion (T3.6)
"""

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.engine import Engine
from engine.generator import GeneratedCode, _is_reasoning_exhaustion
from engine import llm_judge


# ---------------------------------------------------------------------------
# T3.1 — _is_reasoning_exhaustion true/false table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response,expected", [
    # empty content + finish_reason=length + reasoning_content => True
    ({"content": "", "finish_reason": "length",
      "reasoning_content": "thinking..."}, True),
    # empty content + finish_reason=length + thinking_tokens => True
    ({"content": "   ", "finish_reason": "length", "thinking_tokens": 650}, True),
    # has content => False
    ({"content": "print('hi')", "finish_reason": "length",
      "reasoning_content": "thinking..."}, False),
    # empty but finish_reason=stop => False
    ({"content": "", "finish_reason": "stop",
      "reasoning_content": "thinking..."}, False),
    # empty + length but no reasoning/thinking => False
    ({"content": "", "finish_reason": "length"}, False),
    # reasoning present but not length => False
    ({"content": "", "finish_reason": "stop", "thinking_tokens": 500}, False),
    # Epic-5 regression (run2-RL1-2 attempt-1): empty content + reasoning +
    # finish_reason="" (blank — server omitted it) => True
    ({"content": "", "finish_reason": "",
      "reasoning_content": "Thinking Process:\n1. Analyze...",
      "thinking_tokens": 4042}, True),
    # Epic-5 regression: empty content + thinking_tokens + finish_reason absent => True
    ({"content": "", "thinking_tokens": 4042}, True),
    # Epic-5 regression: empty content + reasoning + finish_reason=None => True
    ({"content": "", "finish_reason": None,
      "reasoning_content": "thinking..."}, True),
    # finish_reason=stop with reasoning => still False (genuine stop, not budget)
    ({"content": "", "finish_reason": "stop",
      "reasoning_content": "brief thought"}, False),
])
def test_is_reasoning_exhaustion_table(response, expected):
    assert _is_reasoning_exhaustion(response) is expected


# ---------------------------------------------------------------------------
# T3.2 — GeneratedCode.finish_reason populated from transport response
# ---------------------------------------------------------------------------

def test_generated_code_finish_reason_populated():
    from engine.generator import generate_code

    class _T:
        def curl_beellama(self, port, messages, **kwargs):
            # Genuinely exhausted: empty content + finish_reason=length + reasoning
            return {"content": "",
                    "finish_reason": "length",
                    "reasoning_content": "thought",
                    "thinking_tokens": 10,
                    "total_tokens": 50, "prompt_tokens": 20,
                    "completion_tokens": 30, "predicted_per_second": 5.0}

    task = Task(id="T01", title="T", description="d", module="app.py")
    ctx = type("C", (), {"imports": {}, "file_tree": {}, "relevant_files": [],
                         "framework": None, "existing_code": {}})()
    cfg = EngineConfig()
    code = generate_code(ctx, task, cfg, _T())
    assert code.finish_reason == "length"
    assert code.exhausted is True
    assert code.max_tokens_used == cfg.max_tokens_by_role["coder"]


# ---------------------------------------------------------------------------
# T3.3 / T3.4 — generation escalates once, distinct error on second exhaustion
# ---------------------------------------------------------------------------

class _ExhaustTransport:
    """Returns exhausted response first N times, then valid code."""

    def __init__(self, exhaust_times=1):
        self.exhaust_times = exhaust_times
        self.calls = 0
        self.max_tokens_seen = []

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        self.max_tokens_seen.append(kwargs.get("max_tokens"))
        if self.calls <= self.exhaust_times:
            return {"content": "", "finish_reason": "length",
                    "reasoning_content": "thinking...",
                    "thinking_tokens": 650, "total_tokens": 10,
                    "prompt_tokens": 5, "completion_tokens": 5,
                    "predicted_per_second": 1.0}
        return {"content": "```python\nx = 1\n```",
                "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        # Canned success so the commit step can complete after recovery.
        return ("", "", 0)


def _run_with_transport(transport, retries=3, monkeypatch=None):
    import engine.engine as eng_mod
    import engine.committer as cm
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py")]
    # Commit's pre-flight repo check must stay offline (mock transport = no network).
    cm._ensure_repo_exists = lambda repo, autocreate: None
    try:
        eng = Engine(transport=transport, config=EngineConfig(max_retries_generate=retries))
        return eng.run("/tmp/prd.md", "/tmp/project")
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


def test_generation_escalates_once_then_recovers(monkeypatch):
    """First call exhausted -> escalate to 8192 -> recovers (T3.3)."""
    transport = _ExhaustTransport(exhaust_times=1)
    result = _run_with_transport(transport, retries=3, monkeypatch=monkeypatch)
    # exhausted at 4096, escalated to 8192, then valid
    assert 4096 in transport.max_tokens_seen
    assert 8192 in transport.max_tokens_seen
    assert result.tasks[0].state != "FAILED"  # recovered


def test_second_exhaustion_surfaces_distinct_error(monkeypatch):
    """Exhausted even after escalation -> distinct 'reasoning exhausted' error (T3.3)."""
    transport = _ExhaustTransport(exhaust_times=99)  # always exhausted
    result = _run_with_transport(transport, retries=3, monkeypatch=monkeypatch)
    tr = result.tasks[0]
    assert tr.state == "FAILED"
    assert "reasoning exhausted" in tr.error_message
    # escalated budget present in the message
    assert "8192" in tr.error_message


def test_escalation_count_at_most_one(monkeypatch):
    """Even with many retries, escalation retry happens <= 1 time (T3.4)."""
    transport = _ExhaustTransport(exhaust_times=99)
    _run_with_transport(transport, retries=5, monkeypatch=monkeypatch)
    # 4096 (initial) + 8192 (one escalate) only — no further doubling
    assert transport.max_tokens_seen.count(8192) == 1
    assert 16384 not in transport.max_tokens_seen


# ---------------------------------------------------------------------------
# T3.5 — Judge escalation: 4096 -> higher on exhaustion; still exhausted -> zeros+error
# ---------------------------------------------------------------------------

class _JudgeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.max_tokens_seen = []

    def curl_beellama(self, port, messages, **kwargs):
        self.max_tokens_seen.append(kwargs.get("max_tokens"))
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply

    def get_model_list(self, port):
        return ["mock-judge"]


def test_judge_escalates_on_exhaustion(monkeypatch):
    """Judge: first call 4096, exhaustion -> retry at higher budget (T3.5)."""
    exhausted = {"content": "", "finish_reason": "length",
                 "reasoning_content": "thinking...", "thinking_tokens": 500}
    good = {"content": json.dumps({d: 7 for d in llm_judge.DIMENSIONS}),
            "finish_reason": "stop"}
    t = _JudgeTransport([exhausted, good])
    r = llm_judge.score_task("run", "T01", "t", "code", t, save=False)
    assert t.max_tokens_seen[0] == 4096
    assert t.max_tokens_seen[1] == 8192  # doubled, below 32768 ceiling
    assert r["error"] is None
    assert r["overall"] == 7.0


def test_judge_still_exhausted_gives_zeros(monkeypatch):
    """Judge exhausted even after escalation -> zeros + error, no reroll (T3.5)."""
    exhausted = {"content": "", "finish_reason": "length",
                 "reasoning_content": "thinking...", "thinking_tokens": 500}
    t = _JudgeTransport([exhausted, exhausted])  # both attempts exhausted
    r = llm_judge.score_task("run", "T01", "t", "code", t, save=False)
    assert r["scores"] == {d: 0 for d in llm_judge.DIMENSIONS}
    assert r["error"] is not None
    assert t.calls == 2  # initial + ONE retry, no more


# ---------------------------------------------------------------------------
# T3.6 — Non-exhaustion empty content (finish_reason=stop) is NOT exhaustion
# ---------------------------------------------------------------------------

def test_non_exhaustion_empty_not_treated_as_exhaustion():
    """Empty content with finish_reason=stop and no reasoning => not exhaustion."""
    resp = {"content": "", "finish_reason": "stop"}
    assert _is_reasoning_exhaustion(resp) is False


# ---------------------------------------------------------------------------
# Epic-5 regression: run2-RL1-2 attempt-1 signature
# empty content + long reasoning + finish_reason="" => escalation fires
# ---------------------------------------------------------------------------

class _BlankFinishReasonExhaustTransport:
    """Reproduces run2-RL1-2 attempt-1: empty content + 16k-char reasoning +
    finish_reason="" (server omitted it).  Exhausts once, then recovers."""

    def __init__(self):
        self.calls = 0
        self.max_tokens_seen = []

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        self.max_tokens_seen.append(kwargs.get("max_tokens"))
        if self.calls == 1:
            # Exact signature from run2-RL1-2 attempt-1:
            # empty content, long reasoning, blank finish_reason, thinking_tokens>0
            return {"content": "",
                    "finish_reason": "",
                    "reasoning_content": "Thinking Process:\n" + "x" * 16000,
                    "thinking_tokens": 4042,
                    "total_tokens": 5222, "prompt_tokens": 1176,
                    "completion_tokens": 4096, "predicted_per_second": 1.0}
        # Escalated retry: valid code
        return {"content": "```python\nx = 1\n```",
                "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        return ("", "", 0)


def test_blank_finish_reason_triggers_escalation(monkeypatch):
    """run2-RL1-2 attempt-1: empty content + reasoning + finish_reason=''
    MUST trigger the budget-escalation retry (Epic-5 regression)."""
    transport = _BlankFinishReasonExhaustTransport()
    result = _run_with_transport(transport, retries=3, monkeypatch=monkeypatch)
    # Escalation fired: initial 4096 then doubled to 8192
    assert 4096 in transport.max_tokens_seen
    assert 8192 in transport.max_tokens_seen
    # Recovered after escalation
    assert result.tasks[0].state != "FAILED"


def test_blank_finish_reason_detected_in_generated_code(monkeypatch):
    """generate_code flags exhausted=True for blank-finish_reason exhaustion."""
    from engine.generator import generate_code

    class _T:
        def curl_beellama(self, port, messages, **kwargs):
            return {"content": "",
                    "finish_reason": "",
                    "reasoning_content": "Thinking Process:\n" + "y" * 16000,
                    "thinking_tokens": 4042,
                    "total_tokens": 5222, "prompt_tokens": 1176,
                    "completion_tokens": 4096, "predicted_per_second": 1.0}

    task = Task(id="T01", title="T", description="d", module="app.py")
    ctx = type("C", (), {"imports": {}, "file_tree": {}, "relevant_files": [],
                         "framework": None, "existing_code": {}})()
    cfg = EngineConfig()
    code = generate_code(ctx, task, cfg, _T())
    assert code.exhausted is True
    assert code.finish_reason == ""
    assert code.thinking_tokens == 4042
