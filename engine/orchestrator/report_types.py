"""Final report — types layer (T7a).

Transport-independent dataclasses for the run summary report. Pure data:
the renderer turns these into markdown, the persistence layer stores them.
"""

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class TaskOutcome:
    """Outcome for a single task within a run."""
    task_id: str
    title: str
    state: str                # COMMIT | FAILED | CANCELLED | PENDING
    attempts: int
    commit_sha: str | None
    tokens: int
    error: str | None = None


@dataclass
class ACChecklistItem:
    """One acceptance-criterion checkmark (mechanical, no LLM)."""
    task_id: str
    criterion: str
    met: bool                 # True if task reached a terminal PASS state


@dataclass
class RunReport:
    """Complete run summary report."""
    run_id: str
    success: bool
    state: str                # DONE | FAILED | CANCELLED
    prd_path: str
    project_path: str
    wall_clock_s: float
    total_tokens: int
    budget: dict              # {llm_calls, max_llm_calls, total_tokens, ...}
    task_outcomes: list       # list[TaskOutcome]
    replan_history: list      # list[dict] from session.get_replan_history
    checkpoint_sha: str | None
    stop_reason: str | None   # why the run stopped (budget cap, stop-file, ...)
    ac_checklist: list        # list[ACChecklistItem]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["task_outcomes"] = [asdict(t) for t in self.task_outcomes]
        d["ac_checklist"] = [asdict(c) for c in self.ac_checklist]
        return d
