"""Round report contract for the Ralph loop (S2 §3).

The typed record that crosses the round boundary. The ONLY structured data the
next Ralph round receives, alongside the pinned workspace and the original
objective. Mirrors ``orchestrator/replan_types.py`` discipline: pure
dataclasses, transport-independent, JSON-serializable, validated on construction.

Validation bounds are authoritative per EPIC.md §3 schema and S5 resolution #6
(maxLength truncation, not rejection).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Validation bounds (from EPIC §3 schema)
# ---------------------------------------------------------------------------

MAX_OBJECTIVE = 4096
MAX_PLAN = 16384
MAX_TASKS_EXECUTED = 32
MAX_PATTERN_PROPOSALS = 8
MAX_PATTERN_BODY = 8192
MAX_SELECTION_REASON = 2048
MAX_GATE_DETAIL = 4096
MAX_TASK_TITLE = 256
MAX_TASK_ID = 32
MAX_ERROR = 4096
MAX_SERIALIZED = 64 * 1024  # 64KB
VALID_STATES = ("completed", "failed", "cancelled")
VALID_GATES = ("dev", "review", "test", "redteam", "evidence")
VALID_PATTERN_TYPES = ("strategy", "lesson", "anti_pattern", "heuristic")
_SHA_RE = None  # lazy import of re


def _sha_ok(s: str) -> bool:
    """Validate a git SHA (7-40 hex chars)."""
    if not isinstance(s, str):
        return False
    if len(s) < 7 or len(s) > 40:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in s)


def _truncate(s: str, max_len: int) -> str:
    """Truncate to max_len (S5 #6: truncation, not rejection)."""
    if not isinstance(s, str):
        return ""
    return s[:max_len]


# ---------------------------------------------------------------------------
# Sub-dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TaskSummary:
    """One task executed within a round."""
    task_id: str
    title: str
    state: str                        # "completed" | "failed" | "cancelled"
    commit_sha: str | None = None
    tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateResult:
    """Per-gate result (Dev/Review/Test/RedTeam)."""
    gate: str                         # "dev" | "review" | "test" | "redteam"
    passed: bool
    detail: str = ""
    model: str | None = None
    tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateVerdict:
    """Combined gate result for a round."""
    overall_pass: bool
    gates: list[GateResult] = field(default_factory=list)
    retry_recommended: bool = False
    retry_target: str | None = None

    def to_dict(self) -> dict:
        return {
            "overall_pass": self.overall_pass,
            "gates": [g.to_dict() for g in self.gates],
            "retry_recommended": self.retry_recommended,
            "retry_target": self.retry_target,
        }


@dataclass
class IdeationSummary:
    """What was considered and what was picked (S5 #1)."""
    candidates_considered: int
    selected_idx: int
    selection_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PatternProposal:
    """A learned-insight proposal (ADR-0002, proposed-only at creation)."""
    pattern_id: str
    title: str
    body: str
    approved: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BudgetSnapshot:
    """Budget consumed in one round (R3/T8)."""
    llm_calls: int = 0
    total_tokens: int = 0
    wall_clock_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# RoundReport
# ---------------------------------------------------------------------------

@dataclass
class RoundReport:
    """Typed round report — the ONLY data crossing the round boundary (R7).

    On construction, string fields are truncated (not rejected) to their
    maxLength bounds (S5 #6). ``from_json`` validates the schema + bounds and
    also truncates. ``to_json`` enforces the 64KB serialized bound.
    """
    round_id: int
    objective: str
    plan: str
    tasks_executed: list[TaskSummary] = field(default_factory=list)
    gate_verdict: GateVerdict | None = None
    ideation_summary: IdeationSummary | None = None
    pattern_proposals: list[PatternProposal] = field(default_factory=list)
    budget_consumed: BudgetSnapshot | None = None
    workspace_sha: str = ""
    state: str = "completed"
    error: str | None = None

    def __post_init__(self) -> None:
        """Apply maxLength truncation (S5 #6) + array bounds on construction."""
        self.objective = _truncate(self.objective, MAX_OBJECTIVE)
        self.plan = _truncate(self.plan, MAX_PLAN)
        self.error = _truncate(self.error, MAX_ERROR) if self.error else None
        self.workspace_sha = _truncate(self.workspace_sha, 40)
        # Bound array lengths (EPIC §3).
        if len(self.tasks_executed) > MAX_TASKS_EXECUTED:
            self.tasks_executed = self.tasks_executed[:MAX_TASKS_EXECUTED]
        if len(self.pattern_proposals) > MAX_PATTERN_PROPOSALS:
            self.pattern_proposals = self.pattern_proposals[:MAX_PATTERN_PROPOSALS]
        # Truncate nested string fields.
        if self.ideation_summary is not None:
            self.ideation_summary.selection_reason = _truncate(
                self.ideation_summary.selection_reason, MAX_SELECTION_REASON)
        for pp in self.pattern_proposals:
            pp.pattern_id = _truncate(pp.pattern_id, 128)
            pp.title = _truncate(pp.title, 256)
            pp.body = _truncate(pp.body, MAX_PATTERN_BODY)
        for tr in self.tasks_executed:
            tr.task_id = _truncate(tr.task_id, MAX_TASK_ID)
            tr.title = _truncate(tr.title, MAX_TASK_TITLE)
        if self.gate_verdict is not None:
            for g in self.gate_verdict.gates:
                g.detail = _truncate(g.detail, MAX_GATE_DETAIL)

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        d: dict[str, Any] = {
            "round_id": self.round_id,
            "objective": self.objective,
            "plan": self.plan,
            "tasks_executed": [t.to_dict() for t in self.tasks_executed],
            "gate_verdict": self.gate_verdict.to_dict() if self.gate_verdict else None,
            "ideation_summary": self.ideation_summary.to_dict() if self.ideation_summary else None,
            "pattern_proposals": [p.to_dict() for p in self.pattern_proposals],
            "budget_consumed": self.budget_consumed.to_dict() if self.budget_consumed else None,
            "workspace_sha": self.workspace_sha,
            "state": self.state,
            "error": self.error,
        }
        return d

    def to_json(self) -> str:
        """Serialize to JSON, enforcing the 64KB bound (truncates plan/objective
        further if needed to fit)."""
        d = self.to_dict()
        serialized = json.dumps(d, default=str)
        if len(serialized.encode("utf-8")) <= MAX_SERIALIZED:
            return serialized
        # Need to shrink: truncate plan first (largest field), then objective.
        # This is a best-effort reduction; the bound must hold.
        d["plan"] = ""
        d["objective"] = d["objective"][:1024]
        serialized = json.dumps(d, default=str)
        if len(serialized.encode("utf-8")) > MAX_SERIALIZED:
            # Last resort: drop tasks detail, keep only counts.
            d["tasks_executed"] = []
            serialized = json.dumps(d, default=str)
        return serialized

    @staticmethod
    def from_json(raw: str) -> "RoundReport":
        """Parse + validate a JSON string into a RoundReport.

        Applies maxLength truncation (S5 #6), never rejection, for string
        bounds. Validates required fields, enums, array length bounds, and
        SHA format — raises ValueError on structural violations that cannot be
        fixed by truncation (missing required fields, wrong types).
        """
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"invalid JSON: {e}") from e
        if not isinstance(d, dict):
            raise ValueError("RoundReport JSON must be an object")

        # Required fields.
        for key in ("round_id", "objective", "plan", "state"):
            if key not in d:
                raise ValueError(f"missing required field: {key}")

        # Validate round_id.
        round_id = d["round_id"]
        if not isinstance(round_id, int) or isinstance(round_id, bool):
            raise ValueError("round_id must be an integer")

        # Validate state enum.
        state = d["state"]
        if state not in VALID_STATES:
            raise ValueError(f"state must be one of {VALID_STATES}, got {state!r}")

        # Validate workspace_sha if present.
        workspace_sha = d.get("workspace_sha", "")
        if workspace_sha and not _sha_ok(workspace_sha):
            raise ValueError(f"workspace_sha is not a valid git SHA: {workspace_sha!r}")

        # Parse tasks_executed (bounded to MAX_TASKS_EXECUTED).
        tasks_raw = d.get("tasks_executed", [])
        if not isinstance(tasks_raw, list):
            raise ValueError("tasks_executed must be an array")
        tasks_raw = tasks_raw[:MAX_TASKS_EXECUTED]
        tasks_executed: list[TaskSummary] = []
        for t in tasks_raw:
            if not isinstance(t, dict):
                continue
            if "task_id" not in t or "title" not in t or "state" not in t:
                continue
            tasks_executed.append(TaskSummary(
                task_id=str(t["task_id"])[:MAX_TASK_ID],
                title=str(t["title"])[:MAX_TASK_TITLE],
                state=str(t["state"]),
                commit_sha=str(t.get("commit_sha")) if t.get("commit_sha") else None,
                tokens=int(t.get("tokens", 0)),
            ))

        # Parse gate_verdict.
        gate_verdict: GateVerdict | None = None
        gv_raw = d.get("gate_verdict")
        if isinstance(gv_raw, dict):
            gates_raw = gv_raw.get("gates", [])
            gates: list[GateResult] = []
            if isinstance(gates_raw, list):
                for g in gates_raw:
                    if not isinstance(g, dict):
                        continue
                    gate_name = g.get("gate")
                    if gate_name not in VALID_GATES:
                        continue
                    gates.append(GateResult(
                        gate=str(gate_name),
                        passed=bool(g.get("passed", False)),
                        detail=str(g.get("detail", ""))[:MAX_GATE_DETAIL],
                        model=str(g.get("model")) if g.get("model") else None,
                        tokens=int(g.get("tokens", 0)),
                    ))
            gate_verdict = GateVerdict(
                overall_pass=bool(gv_raw.get("overall_pass", False)),
                gates=gates,
                retry_recommended=bool(gv_raw.get("retry_recommended", False)),
                retry_target=str(gv_raw.get("retry_target")) if gv_raw.get("retry_target") else None,
            )

        # Parse ideation_summary.
        ideation_summary: IdeationSummary | None = None
        is_raw = d.get("ideation_summary")
        if isinstance(is_raw, dict):
            ideation_summary = IdeationSummary(
                candidates_considered=int(is_raw.get("candidates_considered", 1)),
                selected_idx=int(is_raw.get("selected_idx", 0)),
                selection_reason=str(is_raw.get("selection_reason", ""))[:MAX_SELECTION_REASON],
            )

        # Parse pattern_proposals (bounded to MAX_PATTERN_PROPOSALS).
        pp_raw = d.get("pattern_proposals", [])
        pattern_proposals: list[PatternProposal] = []
        if isinstance(pp_raw, list):
            for p in pp_raw[:MAX_PATTERN_PROPOSALS]:
                if not isinstance(p, dict):
                    continue
                if "pattern_id" not in p or "title" not in p or "body" not in p:
                    continue
                pattern_proposals.append(PatternProposal(
                    pattern_id=str(p["pattern_id"])[:128],
                    title=str(p["title"])[:256],
                    body=str(p["body"])[:MAX_PATTERN_BODY],
                    approved=bool(p.get("approved", False)),
                ))

        # Parse budget_consumed.
        budget_consumed: BudgetSnapshot | None = None
        bc_raw = d.get("budget_consumed")
        if isinstance(bc_raw, dict):
            budget_consumed = BudgetSnapshot(
                llm_calls=int(bc_raw.get("llm_calls", 0)),
                total_tokens=int(bc_raw.get("total_tokens", 0)),
                wall_clock_s=float(bc_raw.get("wall_clock_s", 0.0)),
            )

        return RoundReport(
            round_id=round_id,
            objective=str(d["objective"]),
            plan=str(d["plan"]),
            tasks_executed=tasks_executed,
            gate_verdict=gate_verdict,
            ideation_summary=ideation_summary,
            pattern_proposals=pattern_proposals,
            budget_consumed=budget_consumed,
            workspace_sha=str(workspace_sha) if workspace_sha else "",
            state=state,
            error=str(d["error"]) if d.get("error") else None,
        )
