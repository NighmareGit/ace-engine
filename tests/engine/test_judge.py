"""Tests for engine/judge.py — deterministic Phase-2 judge scoring."""

import sys, os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.judge import judge_run, JudgeVerdict, PASS_THRESHOLD
from engine.engine import RunResult, TaskResult


def _perfect_dict() -> dict:
    return {
        "success": True, "total_tokens": 5000,
        "tasks": [{"task_id": "T01", "title": "A", "state": "done",
                   "commit_sha": "abc123", "tokens": 1000, "error_message": None}],
        "error_message": None,
    }


def _perfect_run_result() -> RunResult:
    task = TaskResult(task_id="T01", title="A", state="done", attempts=1,
                      commit_sha="abc123", time_s=1.0, tokens=1000,
                      error_message=None)
    return RunResult(run_id="r1", success=True, prd_path="/tmp/prd.md",
                     project_path="/tmp/proj", total_time_s=10.0,
                     tasks=[task], states_visited=[], total_tokens=5000,
                     error_message=None)


def test_all_pass_perfect_score():
    """A perfect run scores 1.0 and passes the threshold."""
    verdict = judge_run(_perfect_dict())
    assert isinstance(verdict, JudgeVerdict)
    assert verdict.overall_pass is True
    assert verdict.score == 1.0
    assert len(verdict.dimensions) == 5
    assert all(d.passed for d in verdict.dimensions)


def test_fail_on_success_false():
    """When success is False, the success dimension fails and score < threshold."""
    data = _perfect_dict()
    data["success"] = False
    verdict = judge_run(data)
    assert verdict.overall_pass is False
    succ_dim = next(d for d in verdict.dimensions if d.name == "success")
    assert succ_dim.passed is False
    assert verdict.score < PASS_THRESHOLD


def test_fail_on_zero_tokens():
    """When total_tokens is 0, the tokens dimension fails."""
    data = _perfect_dict()
    data["total_tokens"] = 0
    verdict = judge_run(data)
    tok_dim = next(d for d in verdict.dimensions if d.name == "tokens")
    assert tok_dim.passed is False
    # 1.0 - 0.15 = 0.85, still passes
    assert verdict.overall_pass is True
    assert abs(verdict.score - 0.85) < 1e-9


def test_partial_score_commit_missing():
    """Missing commit evidence dims the score but may still pass."""
    data = _perfect_dict()
    data["tasks"] = [{"task_id": "T01", "title": "A", "state": "done",
                       "commit_sha": None, "tokens": 1000, "error_message": None}]
    verdict = judge_run(data)
    commit_dim = next(d for d in verdict.dimensions if d.name == "commit")
    assert commit_dim.passed is False
    # 1.0 - 0.20 = 0.80 → exactly at threshold → passes
    assert verdict.overall_pass is True
    assert abs(verdict.score - 0.80) < 1e-9


def test_threshold_boundary_below():
    """success + tasks + commit (no tokens, no error_free) = 0.70 → fail."""
    data = _perfect_dict()
    data["total_tokens"] = 0         # tokens fails  (−0.15)
    data["error_message"] = "boom"   # error_free fails (−0.15)
    verdict = judge_run(data)
    assert verdict.overall_pass is False
    assert abs(verdict.score - 0.70) < 1e-9


def test_dict_vs_object_produces_same_score():
    """A raw dict and an equivalent RunResult produce identical scores."""
    d_verdict = judge_run(_perfect_dict())
    o_verdict = judge_run(_perfect_run_result())
    assert d_verdict.score == o_verdict.score
    assert d_verdict.overall_pass == o_verdict.overall_pass
    for dd, od in zip(d_verdict.dimensions, o_verdict.dimensions):
        assert dd.name == od.name
        assert dd.passed == od.passed


def test_empty_tasks_fails():
    """An empty tasks list means the tasks dimension does not pass."""
    data = _perfect_dict()
    data["tasks"] = []
    verdict = judge_run(data)
    tasks_dim = next(d for d in verdict.dimensions if d.name == "tasks")
    assert tasks_dim.passed is False


def test_non_terminal_task_fails():
    """A task in 'running' state (non-terminal) means tasks dimension fails."""
    data = _perfect_dict()
    data["tasks"] = [{"task_id": "T01", "title": "A", "state": "running",
                       "commit_sha": "abc", "tokens": 500, "error_message": None}]
    verdict = judge_run(data)
    tasks_dim = next(d for d in verdict.dimensions if d.name == "tasks")
    assert tasks_dim.passed is False


def test_to_dict_structure():
    """to_dict() returns a plain dict with expected keys."""
    verdict = judge_run(_perfect_dict())
    d = verdict.to_dict()
    assert isinstance(d, dict)
    assert "overall_pass" in d and "score" in d and "dimensions" in d
    assert len(d["dimensions"]) == 5
    assert all(k in dim for dim in d["dimensions"] for k in ("name", "weight", "passed"))
