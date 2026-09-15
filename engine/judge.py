"""Phase-2 minimal judge — deterministic, measurable scoring only.

No LLM calls, no network.  Scores a RunResult (or equivalent dict) on
five weighted dimensions drawn from the self-scoring-benchmark contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScoreDimension:
    """One scored dimension with weight, pass/fail, and detail string."""
    name: str
    weight: float
    passed: bool
    detail: str


@dataclass
class JudgeVerdict:
    """Aggregated verdict from deterministic judge scoring."""
    overall_pass: bool
    score: float              # 0.0 – 1.0
    dimensions: list[ScoreDimension] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON / DB storage."""
        return {
            "overall_pass": self.overall_pass,
            "score": round(self.score, 4),
            "dimensions": [
                {
                    "name": d.name,
                    "weight": d.weight,
                    "passed": d.passed,
                    "detail": d.detail,
                }
                for d in self.dimensions
            ],
        }


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

PASS_THRESHOLD = 0.8

# Sane-token upper bound (10 M tokens — well above any real run)
_MAX_TOKENS = 10_000_000

# Terminal task states (from engine/pipeline.py State enum values)
_TERMINAL_STATES = {"done", "failed", "cancelled"}

# Dimension weights (sum = 1.0)
_WEIGHTS = {
    "success":       0.30,
    "tasks":         0.20,
    "commit":        0.20,
    "tokens":        0.15,
    "error_free":    0.15,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract(result: dict | Any) -> dict:
    """Normalise a RunResult dataclass or raw dict into a plain dict."""
    if isinstance(result, dict):
        return result
    # Assume dataclass-like object (RunResult from engine.engine)
    return {
        "success": getattr(result, "success", False),
        "total_tokens": getattr(result, "total_tokens", 0),
        "tasks": getattr(result, "tasks", []),
        "error_message": getattr(result, "error_message", None),
    }


def _task_has_commit(task: Any) -> bool:
    """Return True if a task dict or TaskResult has commit evidence."""
    if isinstance(task, dict):
        sha = task.get("commit_sha")
    else:
        sha = getattr(task, "commit_sha", None)
    return bool(sha)


def _task_state(task: Any) -> str:
    """Return the state string of a task."""
    if isinstance(task, dict):
        return str(task.get("state", "")).lower()
    return str(getattr(task, "state", "")).lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def judge_run(result: dict | Any) -> JudgeVerdict:
    """Score a completed engine run on deterministic dimensions.

    Accepted input: a ``RunResult`` dataclass **or** a dict with keys
    ``success``, ``total_tokens``, ``tasks``, ``error_message``.

    Returns a ``JudgeVerdict`` with ``overall_pass`` True when the
    weighted score meets or exceeds ``PASS_THRESHOLD`` (0.8).
    """
    data = _extract(result)
    dims: list[ScoreDimension] = []

    # 1. success — did the run itself succeed?
    success_val = bool(data.get("success"))
    dims.append(ScoreDimension(
        name="success",
        weight=_WEIGHTS["success"],
        passed=success_val,
        detail="Run succeeded" if success_val else "Run reported failure",
    ))

    # 2. tokens — total_tokens > 0 and within sane bounds
    tokens = int(data.get("total_tokens", 0))
    tokens_ok = 0 < tokens < _MAX_TOKENS
    dims.append(ScoreDimension(
        name="tokens",
        weight=_WEIGHTS["tokens"],
        passed=tokens_ok,
        detail=f"total_tokens={tokens}" + ("" if tokens_ok else " (out of range)"),
    ))

    # 3. tasks — non-empty list, all tasks in a terminal state
    tasks = data.get("tasks") or []
    tasks_nonempty = len(tasks) > 0
    all_terminal = tasks_nonempty and all(
        _task_state(t) in _TERMINAL_STATES for t in tasks
    )
    dims.append(ScoreDimension(
        name="tasks",
        weight=_WEIGHTS["tasks"],
        passed=all_terminal,
        detail=f"{len(tasks)} task(s), all terminal"
        if all_terminal
        else f"{len(tasks)} task(s), not all terminal",
    ))

    # 4. commit evidence — at least one task has a commit_sha
    has_commit = any(_task_has_commit(t) for t in tasks) if tasks else False
    dims.append(ScoreDimension(
        name="commit",
        weight=_WEIGHTS["commit"],
        passed=has_commit,
        detail="Commit evidence found" if has_commit else "No commit evidence",
    ))

    # 5. error_free — no error_message in run result
    err = data.get("error_message")
    error_free = err is None or err == ""
    dims.append(ScoreDimension(
        name="error_free",
        weight=_WEIGHTS["error_free"],
        passed=error_free,
        detail="No errors" if error_free else f"Error: {err}",
    ))

    # Weighted score
    score = sum(d.weight for d in dims if d.passed)
    overall_pass = score >= PASS_THRESHOLD

    return JudgeVerdict(
        overall_pass=overall_pass,
        score=score,
        dimensions=dims,
    )
