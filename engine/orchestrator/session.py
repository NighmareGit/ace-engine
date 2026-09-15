"""Orchestrator session state — run-level persistence in engine.db.

One row per run in the ``orchestrator_session`` table (WAL mode, additive —
the CCBS scored_state contract is untouched). Tracks task states, the last
pushed SHA (feeds T6), and budget counters (feeds T8). Resume-safe: every
mutation commits immediately so a kill -9 mid-run loses nothing.

Content-hash dedup: ``orchestrator_commits`` records the SHA-256 of each
task's committed file-content; ``is_task_committed`` returns True only when
that exact content was both committed AND pushed, so resume never re-commits
an already-pushed task.
"""

import hashlib
import json
import os
from datetime import datetime

# Reuse the existing DB plumbing (WAL, busy_timeout, ENGINE_DB_PATH override).
from engine.state import _get_conn


def _now() -> str:
    return datetime.now().isoformat()


def _ensure_tables():
    """Create the orchestrator tables if absent. Additive — safe to call repeatedly."""
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_session (
        run_id TEXT PRIMARY KEY,
        task_states TEXT DEFAULT '{}',
        last_pushed_sha TEXT,
        llm_calls INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        wall_clock_start TEXT,
        max_llm_calls INTEGER DEFAULT 0,
        max_total_tokens INTEGER DEFAULT 0,
        max_wall_clock_s INTEGER DEFAULT 0,
        status TEXT DEFAULT 'running',
        created_at TEXT,
        updated_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        commit_sha TEXT,
        pushed INTEGER DEFAULT 0,
        created_at TEXT,
        UNIQUE(run_id, task_id, content_hash)
    )""")
    # T4: re-plan telemetry (additive).
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_replans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        trigger_signature TEXT NOT NULL,
        trigger_reason TEXT,
        raw_response TEXT,
        validated INTEGER DEFAULT 0,
        applied INTEGER DEFAULT 0,
        error TEXT,
        tokens INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def create_session(run_id: str, task_ids: list, config=None) -> None:
    """Create (or replace) a session row for a run. Idempotent."""
    _ensure_tables()
    states = {tid: "PENDING" for tid in task_ids}
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO orchestrator_session
        (run_id, task_states, status, wall_clock_start, max_llm_calls,
         max_total_tokens, max_wall_clock_s, created_at, updated_at)
        VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        json.dumps(states),
        _now(),
        getattr(config, "max_llm_calls", 0) if config else 0,
        getattr(config, "max_total_tokens", 0) if config else 0,
        getattr(config, "max_wall_clock_s", 0) if config else 0,
        _now(),
        _now(),
    ))
    conn.commit()
    conn.close()


def get_session(run_id: str) -> dict | None:
    """Return the session row as a dict, or None if no such run."""
    _ensure_tables()
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM orchestrator_session WHERE run_id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Task states
# ---------------------------------------------------------------------------

def get_task_states(run_id: str) -> dict:
    """Return {task_id: state} for the run (empty dict if unknown)."""
    session = get_session(run_id)
    if session is None:
        return {}
    return json.loads(session.get("task_states") or "{}")


def update_task_state(run_id: str, task_id: str, state: str) -> None:
    """Set a single task's state and bump updated_at."""
    session = get_session(run_id)
    if session is None:
        raise ValueError(f"No orchestrator session for run_id={run_id}")
    states = json.loads(session.get("task_states") or "{}")
    states[task_id] = state
    conn = _get_conn()
    conn.execute(
        "UPDATE orchestrator_session SET task_states = ?, updated_at = ? WHERE run_id = ?",
        (json.dumps(states), _now(), run_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Budget counters (T8)
# ---------------------------------------------------------------------------

def increment_llm_calls(run_id: str, tokens: int = 0) -> dict:
    """Atomically increment llm_calls by 1 and total_tokens by tokens.
    Returns the updated counters dict."""
    conn = _get_conn()
    conn.execute(
        """UPDATE orchestrator_session
           SET llm_calls = llm_calls + 1, total_tokens = total_tokens + ?,
               updated_at = ?
           WHERE run_id = ?""",
        (tokens, _now(), run_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT llm_calls, total_tokens, wall_clock_start, max_llm_calls, "
        "max_total_tokens, max_wall_clock_s FROM orchestrator_session WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def get_budget_counters(run_id: str) -> dict:
    """Return {llm_calls, total_tokens, wall_clock_start, max_*}."""
    session = get_session(run_id)
    if session is None:
        return {}
    return {
        "llm_calls": session.get("llm_calls", 0),
        "total_tokens": session.get("total_tokens", 0),
        "wall_clock_start": session.get("wall_clock_start"),
        "max_llm_calls": session.get("max_llm_calls", 0),
        "max_total_tokens": session.get("max_total_tokens", 0),
        "max_wall_clock_s": session.get("max_wall_clock_s", 0),
    }


# ---------------------------------------------------------------------------
# Git checkpoint (T6)
# ---------------------------------------------------------------------------

def record_push(run_id: str, sha: str) -> None:
    """Record the last pushed SHA for the run."""
    _ensure_tables()
    conn = _get_conn()
    conn.execute(
        "UPDATE orchestrator_session SET last_pushed_sha = ?, updated_at = ? WHERE run_id = ?",
        (sha, _now(), run_id),
    )
    conn.commit()
    conn.close()


def get_last_pushed_sha(run_id: str) -> str | None:
    session = get_session(run_id)
    if session is None:
        return None
    return session.get("last_pushed_sha")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def set_status(run_id: str, status: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE orchestrator_session SET status = ?, updated_at = ? WHERE run_id = ?",
        (status, _now(), run_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Content-hash dedup
# ---------------------------------------------------------------------------

def content_hash(text: str) -> str:
    """SHA-256 of file content — the dedup key for committed tasks."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_commit(run_id: str, task_id: str, ch: str, commit_sha: str,
                  pushed: bool = False) -> None:
    """Record (run_id, task_id, content_hash) -> commit_sha + pushed flag."""
    _ensure_tables()
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO orchestrator_commits
        (run_id, task_id, content_hash, commit_sha, pushed, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (run_id, task_id, ch, commit_sha, 1 if pushed else 0, _now()))
    conn.commit()
    conn.close()


def is_task_committed(run_id: str, task_id: str, ch: str) -> bool:
    """True only when this exact content was committed AND pushed."""
    _ensure_tables()
    conn = _get_conn()
    row = conn.execute("""
        SELECT pushed FROM orchestrator_commits
        WHERE run_id = ? AND task_id = ? AND content_hash = ?
    """, (run_id, task_id, ch)).fetchone()
    conn.close()
    return bool(row and row["pushed"])


def get_committed_tasks(run_id: str) -> set:
    """Return the set of task_ids that have at least one pushed commit."""
    _ensure_tables()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT task_id FROM orchestrator_commits WHERE run_id = ? AND pushed = 1",
        (run_id,),
    ).fetchall()
    conn.close()
    return {r["task_id"] for r in rows}


# ---------------------------------------------------------------------------
# Re-plan telemetry (T4)
# ---------------------------------------------------------------------------

def record_replan(run_id: str, task_id: str, trigger_signature: tuple,
                  trigger_reason: str, raw_response: str | None,
                  validated: bool, applied: bool, error: str | None,
                  tokens: int) -> int:
    """Persist a re-plan attempt to orchestrator_commits. Returns row id."""
    _ensure_tables()
    conn = _get_conn()
    cur = conn.execute("""
        INSERT INTO orchestrator_replans
        (run_id, task_id, trigger_signature, trigger_reason, raw_response,
         validated, applied, error, tokens, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id, task_id, json.dumps(list(trigger_signature)),
        trigger_reason, raw_response or "",
        1 if validated else 0, 1 if applied else 0,
        error or "", tokens or 0, _now(),
    ))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def get_replan_history(run_id: str) -> list[dict]:
    """Return the re-plan history for a run (most recent first)."""
    _ensure_tables()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM orchestrator_replans WHERE run_id = ? ORDER BY id DESC",
        (run_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
