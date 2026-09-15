"""Oracle tier — escalation runtime (O3).

Wires the P6 oracle into the engine's escalation ladder:

  9B subject → 35B re-plan (T4) → ORACLE (P6)

Trigger: exhaustion of the re-plan ladder (the re-plan while-loop in engine.py
ends without a passing validation) AND oracle enabled → escalate the TASK (not
the run) to the oracle. The oracle re-plans the task into atoms (O2 path,
per-task); atoms execute through the EXISTING pipeline as sub-tasks; results
aggregate back to the parent task.

Key semantics (grill fix G5 — run-barrier):
  - While an oracle escalation is in flight, no concurrent wave mutation of the
    parent task's dependencies. Enforced via a task-level lock flag in
    orchestrator_session (oracle_lock_task_id).
  - One oracle escalation per task per run (no recursive oracle).
  - Budget: oracle calls count against T8 counters; escalation has its own cap
    (max_oracle_escalations_per_run, default 1).
  - Telemetry: oracle_events table (trigger, digest outcome, atom count,
    atom results, aggregation) + graceful degradation events.

The oracle is NEVER on the hot path and never default-enabled. Every call site
degrades gracefully (log + telemetry + fall through to existing behaviour).
"""

import logging
from datetime import datetime

from engine.orchestrator.oracle_types import (
    OracleConfig, OracleRequest, OracleUnavailable,
)
from engine.orchestrator.oracle_digest import digest_prd, record_oracle_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run-barrier lock (grill fix G5).
# ---------------------------------------------------------------------------

def acquire_oracle_lock(run_id: str, task_id: str) -> bool:
    """Try to acquire the per-run oracle escalation lock.

    Returns True if the lock was acquired (no oracle escalation currently in
    flight for this run). Returns False if an oracle escalation is already in
    flight — the caller must NOT start a concurrent one. The lock is stored in
    the orchestrator_session row so it survives crash/resume.
    """
    from engine.orchestrator import session as sess
    cur = _get_oracle_lock(run_id)
    if cur:
        return False  # an oracle escalation is already in flight
    _set_oracle_lock(run_id, task_id)
    return True


def release_oracle_lock(run_id: str) -> None:
    """Release the per-run oracle escalation lock."""
    _set_oracle_lock(run_id, "")


def is_oracle_in_flight(run_id: str) -> bool:
    """True when an oracle escalation is currently in flight for this run."""
    return bool(_get_oracle_lock(run_id))


def _get_oracle_lock(run_id: str) -> str:
    """Return the task_id currently holding the oracle lock, or ''."""
    from engine.state import _get_conn
    conn = _get_conn()
    row = conn.execute(
        "SELECT oracle_lock_task_id FROM orchestrator_session WHERE run_id = ?",
        (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return ""
    return row["oracle_lock_task_id"] or ""


def _set_oracle_lock(run_id: str, task_id: str) -> None:
    """Set (or clear) the oracle lock task_id for a run.

    INSERT OR REPLACE if no session row exists yet (standalone escalation
    outside a full engine run).
    """
    from engine.state import _get_conn
    conn = _get_conn()
    now = datetime.now().isoformat()
    # Try UPDATE first; if no row matched, INSERT.
    conn.execute(
        "UPDATE orchestrator_session SET oracle_lock_task_id = ?, "
        "updated_at = ? WHERE run_id = ?",
        (task_id or "", now, run_id),
    )
    if conn.total_changes == 0:
        conn.execute(
            "INSERT OR REPLACE INTO orchestrator_session "
            "(run_id, task_states, status, created_at, updated_at, "
            "oracle_lock_task_id) VALUES (?, '{}', 'running', ?, ?, ?)",
            (run_id, now, now, task_id or ""),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Per-run escalation cap.
# ---------------------------------------------------------------------------

def get_oracle_escalation_count(run_id: str) -> int:
    """Return the number of oracle escalations already performed this run."""
    from engine.state import _get_conn
    conn = _get_conn()
    row = conn.execute(
        "SELECT oracle_escalation_count FROM orchestrator_session WHERE run_id = ?",
        (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return 0
    return row["oracle_escalation_count"] or 0


def increment_oracle_escalation_count(run_id: str) -> int:
    """Atomically increment the per-run oracle escalation count. Returns new count.

    Creates a session row if none exists (standalone escalation outside a full
    engine run).
    """
    from engine.state import _get_conn
    conn = _get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE orchestrator_session SET oracle_escalation_count = "
        "COALESCE(oracle_escalation_count, 0) + 1, updated_at = ? "
        "WHERE run_id = ?",
        (now, run_id),
    )
    if conn.total_changes == 0:
        # No session row yet — create one with count=1.
        conn.execute(
            "INSERT OR REPLACE INTO orchestrator_session "
            "(run_id, task_states, status, created_at, updated_at, "
            "oracle_escalation_count) VALUES (?, '{}', 'running', ?, ?, 1)",
            (run_id, now, now),
        )
    conn.commit()
    row = conn.execute(
        "SELECT oracle_escalation_count FROM orchestrator_session WHERE run_id = ?",
        (run_id,)
    ).fetchone()
    conn.close()
    return row["oracle_escalation_count"] if row else 1


def _ensure_oracle_columns():
    """Add oracle columns to orchestrator_session if absent (additive, safe)."""
    from engine.state import _get_conn
    # Ensure the base session table exists before adding columns.
    from engine.orchestrator import session as sess
    sess.create_session("_oracle_init_", [])
    conn = _get_conn()
    # Check existing columns.
    info = conn.execute("PRAGMA table_info(orchestrator_session)").fetchall()
    cols = {r["name"] for r in info}
    if "oracle_lock_task_id" not in cols:
        conn.execute(
            "ALTER TABLE orchestrator_session ADD COLUMN oracle_lock_task_id TEXT")
    if "oracle_escalation_count" not in cols:
        conn.execute(
            "ALTER TABLE orchestrator_session ADD COLUMN oracle_escalation_count "
            "INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Trigger evaluation.
# ---------------------------------------------------------------------------

def should_escalate_to_oracle(run_id: str, task_id: str,
                               replan_exhausted: bool,
                               cfg: OracleConfig) -> tuple[bool, str]:
    """Decide whether to escalate a task to the oracle.

    Conditions (ALL must hold):
      1. Oracle enabled in config.
      2. The re-plan ladder is exhausted (replan_exhausted=True).
      3. The per-run escalation cap has not been hit.
      4. No oracle escalation is currently in flight (run-barrier).

    Returns (should_escalate, reason).
    """
    if not cfg.enabled:
        return (False, "oracle disabled")
    if not replan_exhausted:
        return (False, "re-plan ladder not exhausted")
    _ensure_oracle_columns()
    if is_oracle_in_flight(run_id):
        return (False, "oracle escalation already in flight (run-barrier)")
    count = get_oracle_escalation_count(run_id)
    if count >= cfg.max_oracle_escalations_per_run:
        return (False, f"per-run oracle cap reached: {count}/"
                       f"{cfg.max_oracle_escalations_per_run}")
    return (True, "re-plan exhausted + oracle enabled: escalating task")


# ---------------------------------------------------------------------------
# Escalation: digest + aggregate.
# ---------------------------------------------------------------------------

def escalate_task_to_oracle(task, pipeline, cfg: OracleConfig, transport,
                            original_task_ids: list[str]) -> dict:
    """Escalate a single task to the oracle.

    Flow:
      1. Acquire run-barrier lock (fail fast if one is already in flight).
      2. Build an OracleRequest from the task + its PRD context.
      3. Call digest_prd (O2) — oracle or 35B-fallback.
      4. On success: atoms are returned; aggregate their outcomes back to the
         parent task (the atoms are NOT executed inline here — the engine's
         existing pipeline executes them as sub-tasks; this function returns the
         atom list so the caller can dispatch them).
      5. Release the lock. Count against T8. Record telemetry.

    Returns a dict:
      {
        "escalated": bool,
        "atoms": list[AtomTicket],
        "reason": str,
        "error": str | None,
        "fallback_used": bool,
        "degraded": bool,
      }
    """
    from engine.orchestrator import budget as budget_mod
    from engine.orchestrator import session as sess

    run_id = pipeline.run_id

    # Disabled -> graceful degradation (no lock, no transport call).
    if not cfg.enabled:
        return {"escalated": False, "atoms": [], "reason": "",
                "error": "oracle disabled",
                "fallback_used": False, "degraded": True}

    _ensure_oracle_columns()

    # Ensure a session row exists so budget counting has a row to increment
    # (standalone escalation outside a full engine run).
    sess._ensure_tables()
    if sess.get_session(run_id) is None:
        sess.create_session(run_id, [task.id])

    # 1. Run-barrier lock.
    if not acquire_oracle_lock(run_id, task.id):
        record_oracle_event(run_id, task.id, "escalation_blocked",
                            detail="run-barrier: oracle already in flight")
        return {"escalated": False, "atoms": [], "reason": "",
                "error": "oracle escalation already in flight",
                "fallback_used": False, "degraded": True}

    try:
        # 2. Build the oracle request.
        prd_text = task.prd_section or task.description or ""
        req = OracleRequest(
            run_id=run_id, task_id=task.id,
            prd_text=prd_text,
            task_title=task.title, task_description=task.description or "",
            task_dependencies=list(task.dependencies or []),
            acceptance_criteria=list(getattr(task, "acceptance_criteria", []) or []),
            error_history=[],  # populated by caller if available
            replan_history=[],  # populated by caller if available
        )

        # 3. Digest (O2 path — oracle or 35B-fallback).
        try:
            result = digest_prd(req, cfg, transport, original_task_ids)
        except OracleUnavailable as e:
            record_oracle_event(run_id, task.id, "oracle_unreachable",
                                detail=str(e))
            return {"escalated": False, "atoms": [], "reason": "",
                    "error": f"oracle unavailable: {e}",
                    "fallback_used": False, "degraded": True}

        # 4. Count the oracle call against T8 budget.
        budget_mod.increment_and_check(run_id, tokens=result.tokens)

        if not result.ok or not result.atoms:
            record_oracle_event(run_id, task.id, "escalation_digest_failed",
                                detail=result.error, tokens=result.tokens)
            return {"escalated": False, "atoms": [], "reason": "",
                    "error": result.error,
                    "fallback_used": result.fallback_used,
                    "degraded": result.degraded}

        # 5. Record success + increment per-run cap.
        increment_oracle_escalation_count(run_id)
        record_oracle_event(run_id, task.id, "escalation_ok",
                            detail=f"atoms={result.atom_count}",
                            atoms_count=result.atom_count,
                            tokens=result.tokens)

        logger.info("task %s oracle escalation produced %d atoms%s",
                    task.id, result.atom_count,
                    " (35B fallback)" if result.fallback_used else "")

        return {
            "escalated": True,
            "atoms": result.atoms,
            "reason": f"oracle digest produced {result.atom_count} atoms",
            "error": None,
            "fallback_used": result.fallback_used,
            "degraded": False,
        }
    finally:
        # Always release the run-barrier lock.
        release_oracle_lock(run_id)


def aggregate_atom_results(atoms: list, atom_results: list[dict]) -> dict:
    """Aggregate atom execution outcomes back to the parent task.

    Each atom_result is a dict with at least {atom_id, ok, error}. Aggregation:
      - all_passed = every atom ok
      - passed_count / failed_count
      - failed_atoms = [atom_id for atoms that failed]

    This is the deterministic aggregation — no LLM involved.
    """
    passed = [r for r in atom_results if r.get("ok")]
    failed = [r for r in atom_results if not r.get("ok")]
    return {
        "all_passed": len(failed) == 0 and len(atom_results) > 0,
        "total": len(atom_results),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "failed_atoms": [r.get("atom_id", "?") for r in failed],
        "atom_count": len(atoms),
    }
