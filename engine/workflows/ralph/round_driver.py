"""Outer Ralph loop — the workflow's seam to the rest of the engine (S2 §2).

Owns the outer Ralph loop. The ONLY module that knows "we are in a Ralph round."
Adapter boundary between Ralph protocol (rounds, reports, gates) and ACE
protocol (runs, RunResults, state transitions).

Per round (EPIC.md, S2 §7, S5/S6 amendments):
  1. Budget check (cumulative via ralph_rounds, per-round clamp min(config.cap,
     remaining) per M2).
  2. Stop-file check (T5).
  3. Divergent ideation → select one plan.
  4. Dispatch ONE engine run (Engine.run with synthetic single-task PRD).
  5. Post-commit gates (Dev/Review/Test/RedTeam).
  6. On gate failure: revert via git revert (M1), mark round FAILED.
  7. Persist RoundReport (ralph_rounds + ralph_reports tables).
  8. Build fresh-agent prompt for next round from ONLY report + workspace_sha
     + objective (R7).

Resume (S5 #4): failed/cancelled only; budget hard-stop; BEGIN IMMEDIATE lock
(RalphRunBusyError); dirty worktree → DirtyWorktreeError.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from engine.state import _get_conn
from engine.workflows.ralph.config import RalphConfig
from engine.workflows.ralph.gates import evaluate_gate_sequence
from engine.workflows.ralph.report_types import (
    RoundReport, GateVerdict, GateResult, TaskSummary, BudgetSnapshot,
)

log = logging.getLogger("engine.workflows.ralph.round_driver")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RalphError(Exception):
    """Base Ralph error."""


class RalphRunBusyError(RalphError):
    """Another process holds the BEGIN IMMEDIATE lock on this Ralph run."""


class DirtyWorktreeError(RalphError):
    """Workspace has uncommitted changes; resume refused (S5 #4)."""


class BudgetExhaustedError(RalphError):
    """Cumulative budget exhausted (R3)."""


# ---------------------------------------------------------------------------
# RalphResult (M6)
# ---------------------------------------------------------------------------

@dataclass
class RalphResult:
    """Terminal result of a Ralph execution (M6: ralph_report.json source)."""
    ralph_run_id: str
    objective: str
    state: str = "completed"          # completed | failed | cancelled
    rounds: list[RoundReport] = field(default_factory=list)
    total_llm_calls: int = 0
    total_tokens: int = 0
    total_wall_clock_s: float = 0.0
    error: str | None = None

    def to_report_dict(self) -> dict:
        """M6: serialize to the ralph_report.json shape."""
        return {
            "ralph_run_id": self.ralph_run_id,
            "objective": self.objective,
            "state": self.state,
            "total_llm_calls": self.total_llm_calls,
            "total_tokens": self.total_tokens,
            "total_wall_clock_s": round(self.total_wall_clock_s, 3),
            "rounds_completed": len([r for r in self.rounds if r.state == "completed"]),
            "rounds_failed": len([r for r in self.rounds if r.state == "failed"]),
            "rounds": [r.to_dict() for r in self.rounds],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# DB schema (additive-only: ralph_runs, ralph_rounds, ralph_reports)
# ---------------------------------------------------------------------------

_RALPH_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS ralph_runs (
    ralph_run_id TEXT PRIMARY KEY,
    project_namespace TEXT NOT NULL DEFAULT 'default',
    objective TEXT NOT NULL,
    max_rounds INTEGER NOT NULL DEFAULT 5,
    state TEXT NOT NULL DEFAULT 'running',
    workspace_sha TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""

_RALPH_ROUNDS_SQL = """
CREATE TABLE IF NOT EXISTS ralph_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ralph_run_id TEXT NOT NULL,
    round_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    llm_calls INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    wall_clock_s REAL DEFAULT 0.0,
    state TEXT NOT NULL,
    workspace_sha TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (ralph_run_id) REFERENCES ralph_runs(ralph_run_id)
);
CREATE INDEX IF NOT EXISTS ralph_rounds_run_id ON ralph_rounds(ralph_run_id);
"""

_RALPH_REPORTS_SQL = """
CREATE TABLE IF NOT EXISTS ralph_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ralph_run_id TEXT NOT NULL,
    round_id INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (ralph_run_id) REFERENCES ralph_runs(ralph_run_id)
);
"""


def _ensure_schema() -> None:
    """Create Ralph tables if absent (additive-only)."""
    conn = _get_conn()
    try:
        for stmt in (_RALPH_RUNS_SQL + _RALPH_ROUNDS_SQL + _RALPH_REPORTS_SQL).split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _create_run_row(ralph_run_id: str, objective: str, max_rounds: int,
                    project_namespace: str = "default") -> None:
    _ensure_schema()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO ralph_runs (ralph_run_id, objective, max_rounds, state, project_namespace)
               VALUES (?, ?, ?, 'running', ?)""",
            (ralph_run_id, objective, max_rounds, project_namespace),
        )
        conn.commit()
    finally:
        conn.close()


def _acquire_lock(ralph_run_id: str) -> bool:
    """BEGIN IMMEDIATE on the ralph_runs row (M7, S5 #4). Returns True if lock
    acquired; False if another process holds it (RalphRunBusyError)."""
    _ensure_schema()
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state FROM ralph_runs WHERE ralph_run_id=?",
                (ralph_run_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    except Exception as e:
        if "BUSY" in str(e).upper():
            return False
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _update_run_state(ralph_run_id: str, state: str,
                      workspace_sha: str | None = None) -> None:
    _ensure_schema()
    conn = _get_conn()
    try:
        if workspace_sha:
            conn.execute(
                """UPDATE ralph_runs SET state=?, workspace_sha=?,
                   updated_at=datetime('now') WHERE ralph_run_id=?""",
                (state, workspace_sha, ralph_run_id),
            )
        else:
            conn.execute(
                """UPDATE ralph_runs SET state=?, updated_at=datetime('now')
                   WHERE ralph_run_id=?""",
                (state, ralph_run_id),
            )
        conn.commit()
    finally:
        conn.close()


def _persist_round(ralph_run_id: str, round_id: int, run_id: str,
                   budget: BudgetSnapshot, state: str,
                   workspace_sha: str | None) -> None:
    _ensure_schema()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO ralph_rounds
               (ralph_run_id, round_id, run_id, llm_calls, total_tokens,
                wall_clock_s, state, workspace_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ralph_run_id, round_id, run_id, budget.llm_calls,
             budget.total_tokens, budget.wall_clock_s, state, workspace_sha),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_report(ralph_run_id: str, round_id: int, report: RoundReport) -> None:
    _ensure_schema()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO ralph_reports (ralph_run_id, round_id, report_json)
               VALUES (?, ?, ?)""",
            (ralph_run_id, round_id, report.to_json()),
        )
        conn.commit()
    finally:
        conn.close()


def _cumulative_budget(ralph_run_id: str) -> BudgetSnapshot:
    """Sum budget across all prior rounds in this ralph_run_id."""
    _ensure_schema()
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(llm_calls), 0) as calls,
                      COALESCE(SUM(total_tokens), 0) as tokens,
                      COALESCE(SUM(wall_clock_s), 0.0) as wall
               FROM ralph_rounds WHERE ralph_run_id=?""",
            (ralph_run_id,),
        ).fetchone()
        return BudgetSnapshot(
            llm_calls=row["calls"] or 0,
            total_tokens=row["tokens"] or 0,
            wall_clock_s=row["wall"] or 0.0,
        )
    finally:
        conn.close()


def _last_completed_round(ralph_run_id: str) -> dict | None:
    """Find the last completed round for resume."""
    _ensure_schema()
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT * FROM ralph_rounds WHERE ralph_run_id=? AND state='completed'
               ORDER BY round_id DESC LIMIT 1""",
            (ralph_run_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Git helpers (M1: git revert, not reset --hard)
# ---------------------------------------------------------------------------

def _git_sha(project_path: str) -> str | None:
    """Get current HEAD SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _git_revert_round(project_path: str, pre_round_sha: str) -> bool:
    """M1: revert the round's commits via git revert (keeps history linear).

    Reverts all commits between pre_round_sha and HEAD (exclusive of
    pre_round_sha). Returns True on success.
    """
    try:
        # Get commits since pre_round_sha.
        result = subprocess.run(
            ["git", "log", "--format=%H", f"{pre_round_sha}..HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        commits = [c for c in result.stdout.strip().split("\n") if c]
        if not commits:
            return True  # nothing to revert
        # Revert in reverse order (newest first) with --no-edit.
        revert_cmd = ["git", "revert", "--no-edit", "--no-commit", *recommits(commits)]
        result = subprocess.run(
            revert_cmd, cwd=project_path, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False
        # Commit the reverts.
        result = subprocess.run(
            ["git", "commit", "-m", f"revert: revert round commits back to {pre_round_sha[:8]}",
             "--no-verify"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _recommits(commits: list[str]) -> list[str]:
    """Return commits in reverse order (newest first) for git revert."""
    return list(reversed(commits))


def _git_is_clean(project_path: str) -> bool:
    """True if the worktree has no uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and not result.stdout.strip()
    except Exception:
        return False


def _git_stash(project_path: str) -> bool:
    """Stash uncommitted changes. Returns True on success."""
    try:
        result = subprocess.run(
            ["git", "stash", "--include-untracked"],
            cwd=project_path, capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Budget clamp (M2)
# ---------------------------------------------------------------------------

def _per_round_budget_clamp(config: RalphConfig,
                            cumulative: BudgetSnapshot) -> BudgetSnapshot:
    """Per-round T8 caps set to min(config.cap, remaining_cumulative) per M2.

    Returns the per-round budget snapshot (clamped). A cap of 0 means
    unbounded.
    """
    # EngineConfig caps live in the engine's config; here we derive a per-round
    # ceiling from the remaining cumulative budget.
    # config.cap fields map to EngineConfig defaults if present.
    max_calls = getattr(config, "max_llm_calls", 0)
    max_tokens = getattr(config, "max_total_tokens", 0)
    max_wall = getattr(config, "max_wall_clock_s", 0)

    remaining_calls = max_calls - cumulative.llm_calls if max_calls > 0 else 0
    remaining_tokens = max_tokens - cumulative.total_tokens if max_tokens > 0 else 0
    remaining_wall = max_wall - cumulative.wall_clock_s if max_wall > 0 else 0

    # Per-round clamp = min(config.cap, remaining_cumulative) per M2.
    per_round_calls = min(remaining_calls, max_calls) if max_calls > 0 else 0
    per_round_tokens = min(remaining_tokens, max_tokens) if max_tokens > 0 else 0
    per_round_wall = min(remaining_wall, max_wall) if max_wall > 0 else 0

    return BudgetSnapshot(
        llm_calls=per_round_calls,
        total_tokens=per_round_tokens,
        wall_clock_s=per_round_wall,
    )


# ---------------------------------------------------------------------------
# Fresh-agent prompt construction (R7)
# ---------------------------------------------------------------------------

def _build_next_round_prompt(
    report: RoundReport,
    objective: str,
    workspace_sha: str,
    config: RalphConfig,
) -> str:
    """Build the next round's prompt from ONLY report + workspace_sha + objective.

    R7: mechanically enforced — nothing else from the previous round's
    conversation is carried. The prompt includes the validated RoundReport,
    the pinned workspace SHA, and the original objective.
    """
    # M8: assert no denylisted paths present in the workspace reference.
    denylist = getattr(config, "workspace_denylist", []) or []
    report_text = report.to_json()
    for denied in denylist:
        if denied in report_text:
            # Redact denylisted content from the report before building prompt.
            report_text = report_text.replace(denied, "[REDACTED]")

    prompt = (
        f"# Ralph Round {report.round_id + 1}\n\n"
        f"## Objective\n{objective}\n\n"
        f"## Previous Round Report (Round {report.round_id})\n"
        f"Workspace SHA: {workspace_sha}\n"
        f"State: {report.state}\n"
        f"Report: {report_text}\n\n"
        f"## Instructions\n"
        f"Continue working toward the objective. The above report is the ONLY "
        f"context from prior rounds. The workspace is pinned at {workspace_sha}."
    )
    return prompt


# ---------------------------------------------------------------------------
# RalphJsonlHandler (M5)
# ---------------------------------------------------------------------------

class RalphJsonlHandler:
    """M5: projects ralph_* events to a per-run JSONL file.

    Mirrors ``EscalationJsonlHandler``: writes
    ``ralph_run_<ralph_run_id>_rounds.jsonl`` in the run directory and
    forwards to an optional ``chain`` callback.
    """

    def __init__(self, run_dir: str | Path, chain: Callable | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.chain = chain

    def __call__(self, event_type: str, data: dict) -> None:
        if event_type.startswith("ralph_"):
            try:
                self.run_dir.mkdir(parents=True, exist_ok=True)
                filepath = self.run_dir / f"ralph_run_{data.get('ralph_run_id', 'unknown')}_rounds.jsonl"
                line = json.dumps({"event": event_type, "data": data, "ts": datetime.now().isoformat()})
                with open(filepath, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass
        if self.chain is not None:
            try:
                self.chain(event_type, data)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API: run_ralph / resume_ralph
# ---------------------------------------------------------------------------

def _gate_sequence_type_for_round(objective: str, config: RalphConfig) -> str:
    """Select the gate sequence type for a round (T08).

    Routes atom kinds to the gate sequence that actually exercises their
    output:

      * ``"research"`` — Evidence->Review (verdict documents have no code to
        dev/test/redteam).
      * ``"code"`` — Dev->Review->Test->RedTeam (the default; covers both
        code atoms and edit atoms, since an edit atom modifies code and is
        meaningfully exercised by the full code gate sequence).

    The heuristic: if ``config`` carries a ``task_types`` entry containing
    ``"research"``, use the research sequence.  Edit atoms (``"edit"`` in
    ``task_types``) explicitly get the code sequence — an edit atom modifies
    code, so the Dev/Test/RedTeam gates apply.  The full implementation
    derives this from the round's actual task list; here we read the config
    hint.
    """
    task_types = getattr(config, "task_types", None)
    if task_types:
        lowered = [str(t).lower() for t in task_types]
        if "research" in lowered:
            return "research"
        # Edit atoms modify code → they get the full code gate sequence.
        # This branch is explicit (not just falling through to default) so
        # the routing decision is visible and unit-testable.
        if "edit" in lowered:
            return "code"
    # Default: code-atom sequence (unchanged behavior).
    return "code"


def _execute_round(
    ralph_run_id: str,
    round_num: int,
    objective: str,
    project_path: str,
    config: RalphConfig,
    previous_reports: list[RoundReport],
    on_event: Callable | None,
) -> tuple[RoundReport, str]:
    """Execute a single Ralph round. Returns (report, round_state).

    Shared by both ``run_ralph`` (fresh start) and ``resume_ralph`` (continue
    from N+1) so the two code paths cannot drift.

    Steps: stop-file check → record pre-round SHA → ideation → engine run →
    gates → revert on failure → build RoundReport.
    """
    # Stop-file check (T5).
    try:
        from engine.orchestrator.stop import check_stop_file
        if check_stop_file(ralph_run_id):
            raise _RoundCancelled()
    except ImportError:
        pass

    # Record pre-round workspace SHA (for M1 revert).
    pre_round_sha = _git_sha(project_path) or ""

    # Ideation (S5 #1).
    try:
        from engine.workflows.ralph.ideation import generate_candidate_plans
        ideation_result = generate_candidate_plans(
            objective=objective,
            previous_rounds=previous_reports,
            n_candidates=config.ideation_candidates,
            model_port=config.ideation_model_port,
            config=config,
        )
        selected_plan = ideation_result.candidates[ideation_result.selected_idx].text \
            if ideation_result.candidates else objective
    except Exception as e:
        log.warning("ideation failed round %d: %s", round_num, e)
        selected_plan = objective
        ideation_result = None

    # Dispatch ONE engine run (scaffold — full implementation calls Engine.run).
    run_id = f"run-{round_num}-{uuid.uuid4().hex[:8]}"
    round_start = time.time()

    # T08: select gate sequence by task type and evaluate it.
    gate_sequence_type = _gate_sequence_type_for_round(objective, config)

    # Post-commit gates. evaluate_gate_sequence() runs the appropriate
    # sequence (research: Evidence->Review; code: Dev->Review->Test->RedTeam)
    # and returns a GateVerdict.  On failure the round is marked failed and
    # the workspace reverted (M1).
    try:
        transport = getattr(config, "transport", None)
        gate_verdict = evaluate_gate_sequence(
            code=objective, task=None, project_path=project_path,
            transport=transport, config=config, run_id=run_id,
            gate_sequence_type=gate_sequence_type,
        )
    except Exception as e:  # noqa: BLE001 — gate crash = fail closed (R1)
        log.warning("gate evaluation failed round %d: %s", round_num, e)
        gate_verdict = GateVerdict(overall_pass=False, gates=[])

    round_budget = BudgetSnapshot(
        llm_calls=0, total_tokens=0,
        wall_clock_s=time.time() - round_start,
    )

    # Determine round state.
    if gate_verdict.overall_pass:
        round_state = "completed"
        workspace_sha = _git_sha(project_path) or pre_round_sha
    else:
        round_state = "failed"
        # M1: revert on gate failure.
        if pre_round_sha:
            _git_revert_round(project_path, pre_round_sha)
        workspace_sha = pre_round_sha

    # Build RoundReport.
    report = RoundReport(
        round_id=round_num,
        objective=objective,
        plan=selected_plan,
        gate_verdict=gate_verdict,
        ideation_summary=ideation_result.to_summary() if ideation_result else None,
        budget_consumed=round_budget,
        workspace_sha=workspace_sha or "",
        state=round_state,
    )

    # Persist round + report.
    _persist_round(ralph_run_id, round_num, run_id, round_budget,
                   round_state, workspace_sha)
    _persist_report(ralph_run_id, round_num, report)

    # Emit event.
    if on_event:
        on_event("ralph_round_complete", {
            "ralph_run_id": ralph_run_id, "round": round_num,
            "state": round_state,
        })

    return report, round_state


class _RoundCancelled(Exception):
    """Internal: stop-file detected, cancel the round."""


def _check_budget_exhausted(ralph_run_id: str, config: RalphConfig) -> None:
    """Raise BudgetExhaustedError if cumulative budget exceeds caps (R3, M2)."""
    cumulative = _cumulative_budget(ralph_run_id)
    max_calls = config.max_llm_calls
    max_tokens = config.max_total_tokens
    max_wall = config.max_wall_clock_s
    if max_calls > 0 and cumulative.llm_calls >= max_calls:
        raise BudgetExhaustedError(
            f"Cumulative llm_calls {cumulative.llm_calls} >= cap {max_calls}")
    if max_tokens > 0 and cumulative.total_tokens >= max_tokens:
        raise BudgetExhaustedError(
            f"Cumulative total_tokens {cumulative.total_tokens} >= cap {max_tokens}")
    if max_wall > 0 and cumulative.wall_clock_s >= max_wall:
        raise BudgetExhaustedError(
            f"Cumulative wall_clock_s {cumulative.wall_clock_s:.1f} >= cap {max_wall}")


def _run_loop(
    ralph_run_id: str,
    objective: str,
    project_path: str,
    config: RalphConfig,
    on_event: Callable | None,
    start_round: int,
    previous_reports: list[RoundReport],
) -> RalphResult:
    """Shared round loop body used by both run_ralph and resume_ralph.

    Runs rounds start_round..max_ralph_rounds. Checks budget before each round
    (R3, M2). Returns the final RalphResult.
    """
    result = RalphResult(ralph_run_id=ralph_run_id, objective=objective)
    start_time = time.time()
    result.rounds = list(previous_reports)

    for round_num in range(start_round, config.max_ralph_rounds + 1):
        # Budget check (R3, M2): cumulative exhaustion → hard stop.
        try:
            _check_budget_exhausted(ralph_run_id, config)
        except BudgetExhaustedError as e:
            result.state = "cancelled"
            _update_run_state(ralph_run_id, "cancelled")
            if on_event:
                on_event("ralph_round_cancelled", {
                    "ralph_run_id": ralph_run_id, "round": round_num,
                    "reason": str(e),
                })
            break

        try:
            report, round_state = _execute_round(
                ralph_run_id, round_num, objective, project_path,
                config, previous_reports, on_event,
            )
        except _RoundCancelled:
            result.state = "cancelled"
            _update_run_state(ralph_run_id, "cancelled")
            if on_event:
                on_event("ralph_round_cancelled", {
                    "ralph_run_id": ralph_run_id, "round": round_num,
                })
            break

        previous_reports.append(report)
        result.rounds.append(report)
        result.total_llm_calls += report.budget_consumed.llm_calls \
            if report.budget_consumed else 0
        result.total_tokens += report.budget_consumed.total_tokens \
            if report.budget_consumed else 0

    # Finalize.
    result.total_wall_clock_s = time.time() - start_time
    if result.state == "running":
        result.state = "completed" if all(
            r.state == "completed" for r in result.rounds
        ) else "failed"
    _update_run_state(ralph_run_id, result.state,
                      workspace_sha=result.rounds[-1].workspace_sha if result.rounds else None)

    # M6: persist ralph_report.json.
    try:
        report_path = Path(project_path) / "run" / ralph_run_id / "ralph_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result.to_report_dict(), indent=2, default=str),
                               encoding="utf-8")
    except Exception:
        pass

    if on_event:
        on_event("ralph_complete", {"ralph_run_id": ralph_run_id, "state": result.state})
    return result


def run_ralph(
    objective: str,
    project_path: str,
    config: RalphConfig | None = None,
    on_event: Callable | None = None,
    ralph_run_id: str | None = None,
) -> RalphResult:
    """Execute a full Ralph run (outer loop). Returns ``RalphResult``.

    Each round: budget check → stop-file check → ideation → engine run → gates
    → revert on failure → persist report → next round.
    """
    if config is None:
        config = RalphConfig()
    ralph_run_id = ralph_run_id or f"ralph-{uuid.uuid4().hex[:12]}"
    _ensure_schema()
    _create_run_row(ralph_run_id, objective, config.max_ralph_rounds)

    # M5: wire the RalphJsonlHandler.
    run_dir = Path(project_path) / "run" / ralph_run_id
    handler = RalphJsonlHandler(run_dir, chain=on_event)
    on_event = handler

    return _run_loop(
        ralph_run_id=ralph_run_id,
        objective=objective,
        project_path=project_path,
        config=config,
        on_event=on_event,
        start_round=1,
        previous_reports=[],
    )


def resume_ralph(
    run_id: str,
    project_path: str,
    config: RalphConfig | None = None,
) -> RalphResult:
    """Resume a failed/cancelled Ralph run (S5 #4).

    Resumable from ``failed``/``cancelled`` only (``completed`` is terminal).
    Budget hard-stop (R3). Concurrency: BEGIN IMMEDIATE lock (RalphRunBusyError).
    Dirty worktree: stash → DirtyWorktreeError, never auto-discard.
    """
    if config is None:
        config = RalphConfig()
    _ensure_schema()

    # Concurrency lock (M7, S5 #4).
    if not _acquire_lock(run_id):
        raise RalphRunBusyError(
            f"Ralph run {run_id} is locked by another process")

    # Load run state.
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM ralph_runs WHERE ralph_run_id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RalphError(f"Ralph run {run_id} not found")

    state = row["state"]
    if state == "completed":
        raise RalphError(
            f"Ralph run {run_id} is already completed (terminal); cannot resume")
    if state not in ("failed", "cancelled"):
        raise RalphError(
            f"Ralph run {run_id} has state '{state}'; only failed/cancelled can be resumed")

    # Dirty worktree check (S5 #4).
    if not _git_is_clean(project_path):
        if not _git_stash(project_path):
            raise DirtyWorktreeError(
                f"Workspace at {project_path} has uncommitted changes and stash failed")
        # After stash, if still dirty, raise.
        if not _git_is_clean(project_path):
            raise DirtyWorktreeError(
                f"Workspace at {project_path} is dirty even after stash")

    # Budget hard-stop (R3): raise if already exhausted on resume.
    _check_budget_exhausted(run_id, config)

    # Find last completed round and continue from N+1.
    last = _last_completed_round(run_id)
    start_round = (last["round_id"] + 1) if last else 1
    objective = row["objective"]

    # Reconstruct previous reports from DB.
    previous_reports = _load_reports(run_id, last)

    # Checkout workspace at last successful SHA.
    if last and last.get("workspace_sha"):
        try:
            subprocess.run(
                ["git", "checkout", last["workspace_sha"]],
                cwd=project_path, capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass

    # M5: wire the RalphJsonlHandler.
    run_dir = Path(project_path) / "run" / run_id
    handler = RalphJsonlHandler(run_dir, chain=None)

    # Update state to running, then execute rounds from start_round onward
    # using the SAME shared loop body as run_ralph (cannot drift).
    _update_run_state(run_id, "running")

    return _run_loop(
        ralph_run_id=run_id,
        objective=objective,
        project_path=project_path,
        config=config,
        on_event=handler,
        start_round=start_round,
        previous_reports=previous_reports,
    )


def _load_reports(run_id: str, last: dict | None) -> list[RoundReport]:
    """Load prior RoundReports from the ralph_reports table."""
    if last is None:
        return []
    _ensure_schema()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT report_json FROM ralph_reports
               WHERE ralph_run_id=? AND round_id<=?
               ORDER BY round_id ASC""",
            (run_id, last["round_id"]),
        ).fetchall()
        reports: list[RoundReport] = []
        for r in rows:
            try:
                reports.append(RoundReport.from_json(r["report_json"]))
            except Exception:
                continue
        return reports
    finally:
        conn.close()
