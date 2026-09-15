"""Edit-op metrics instrumentation (G1-T06).

Exposes ``first_apply_rate(run_id)`` and a ``record_edit_op_result`` helper
that writes per-attempt rows to the ``edit_op_results`` table in engine.db
(added to ``engine/state.py:init_db``).

First-apply rate = COUNT(applied=1 AND attempt=1) / COUNT(*).
Pre-registered threshold: >= 0.90 (GRILL.md delta 3).
"""

from __future__ import annotations

import logging
from typing import Optional

from engine.state import _get_conn

log = logging.getLogger("engine.edit_ops.metrics")


def record_edit_op_result(
    run_id: str,
    task_id: str,
    attempt: int,
    applied: bool,
    match_count: Optional[int] = None,
    db_path: str | None = None,
) -> None:
    """Record a per-attempt edit-op outcome to the edit_op_results table.

    Args:
        run_id: The engine run ID.
        task_id: Task identifier (e.g. T01).
        attempt: 1-based attempt number.
        applied: True if the apply succeeded, False otherwise.
        match_count: The match count for the first failing block (populated
            on multi-match / zero-match failures); None on success.
        db_path: Optional path to the SQLite database (defaults to engine.db
            per engine/state.py resolution, honoring ENGINE_DB_PATH).
    """
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO edit_op_results
                (run_id, task_id, attempt, match_count, applied)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, task_id, attempt,
             match_count, 1 if applied else 0),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001 — telemetry must not break pipeline
        log.warning("record_edit_op_result failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def first_apply_rate(run_id: str, db_path: str | None = None) -> float:
    """Compute the first-apply rate for a run.

    First-apply rate = COUNT(applied=1 AND attempt=1) / COUNT(*).

    Returns 0.0 when there are no rows (avoids division by zero).

    Args:
        run_id: The engine run ID to query.
        db_path: Optional path to the SQLite database (defaults to engine.db
            per engine/state.py resolution, honoring ENGINE_DB_PATH).

    Returns:
        Float in [0.0, 1.0] — the fraction of edit-op attempts that
        succeeded on the first try.
    """
    conn = _get_conn(db_path)
    try:
        total_row = conn.execute(
            "SELECT COUNT(*) FROM edit_op_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        if total == 0:
            return 0.0

        first_ok_row = conn.execute(
            """
            SELECT COUNT(*) FROM edit_op_results
            WHERE run_id = ? AND attempt = 1 AND applied = 1
            """,
            (run_id,),
        ).fetchone()
        first_ok = int(first_ok_row[0]) if first_ok_row else 0
        return first_ok / total
    except Exception as e:  # noqa: BLE001
        log.warning("first_apply_rate query failed: %s", e)
        return 0.0
    finally:
        conn.close()


def session_log_match_count(
    run_id: str,
    task_id: str,
    attempt: int,
    match_count: int,
    db_path: str | None = None,
) -> None:
    """Capture the match count on a failed edit attempt (session-log mirror).

    Records a row with applied=0 and the observed match count so the
    session log reflects WHY an attempt failed (multi-match vs zero-match).

    Args:
        run_id: The engine run ID.
        task_id: Task identifier.
        attempt: 1-based attempt number.
        match_count: The observed match count for the failing block.
        db_path: Optional path to the SQLite database (defaults to engine.db
            per engine/state.py resolution, honoring ENGINE_DB_PATH).
    """
    record_edit_op_result(
        run_id=run_id, task_id=task_id, attempt=attempt,
        applied=False, match_count=match_count, db_path=db_path)
