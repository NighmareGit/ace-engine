"""State definitions and SQLite persistence for the engine."""

import sqlite3
import json
import shutil
import os
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from enum import Enum


class State(Enum):
    IDLE = "IDLE"
    PARSING = "PARSING"
    QUEUED = "QUEUED"
    CONTEXT = "CONTEXT"
    GENERATE = "GENERATE"
    VALIDATE = "VALIDATE"
    TEST = "TEST"
    COMMIT = "COMMIT"
    NEXT = "NEXT"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class TransitionResult:
    """Result of a state transition."""
    from_state: State
    to_state: State
    timestamp: str          # ISO format datetime
    checkpoint_saved: bool  # True if checkpoint succeeded
    event_emitted: bool     # True if on_event callback was called
    error: str | None       # Error message if transition failed


VALID_TRANSITIONS = {
    State.IDLE: [State.PARSING, State.CANCELLED],
    State.PARSING: [State.QUEUED, State.FAILED, State.CANCELLED],
    State.QUEUED: [State.CONTEXT, State.DONE, State.CANCELLED],
    State.CONTEXT: [State.GENERATE, State.FAILED, State.CANCELLED],
    State.GENERATE: [State.VALIDATE, State.FAILED, State.CANCELLED],
    State.VALIDATE: [State.TEST, State.GENERATE, State.FAILED, State.CANCELLED],
    State.TEST: [State.COMMIT, State.GENERATE, State.FAILED, State.CANCELLED],
    State.COMMIT: [State.NEXT, State.QUEUED, State.CANCELLED],
    State.NEXT: [State.QUEUED, State.DONE, State.CANCELLED],
    State.DONE: [],
    State.FAILED: [],
    State.CANCELLED: [],
}

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = str(REPO_ROOT / "engine.db")
BACKUP_PATH = str(REPO_ROOT / "engine.db.bak")


def _db_path():
    """Resolve the active engine DB path, honoring ENGINE_DB_PATH env override."""
    return os.environ.get("ENGINE_DB_PATH") or DB_PATH


def _backup_path():
    """Resolve the backup DB path, honoring ENGINE_DB_BACKUP env override."""
    return os.environ.get("ENGINE_DB_BACKUP") or BACKUP_PATH


def _get_conn(db_path: str | None = None):
    path = db_path if db_path is not None else _db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")  # prevent SQLITE_BUSY
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _backup_db():
    """Backup DB before writes."""
    if os.path.exists(_db_path()):
        shutil.copy2(_db_path(), _backup_path())


def _validate_db(db_path: str | None = None):
    """Validate DB integrity on open."""
    try:
        conn = _get_conn(db_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result[0] == "ok"
    except Exception:
        return False


def _restore_backup(db_path: str | None = None):
    """Restore from backup if DB is corrupted."""
    path = db_path if db_path is not None else _db_path()
    bpath = _backup_path()
    if os.path.exists(bpath):
        shutil.copy2(bpath, path)
        return True
    return False


def init_db(db_path: str | None = None) -> None:
    """Create engine_runs and engine_task_results tables."""
    path = db_path if db_path is not None else _db_path()
    if not _validate_db(path):
        _restore_backup(path)
    conn = _get_conn(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_runs (
        id TEXT PRIMARY KEY,
        prd_path TEXT NOT NULL,
        project_path TEXT NOT NULL,
        config TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'IDLE',
        current_task_id TEXT,
        current_task_idx INTEGER,
        tasks_json TEXT,
        task_retries_json TEXT,
        started_at TEXT DEFAULT (datetime('now')),
        completed_at TEXT,
        result_json TEXT,
        error_message TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_task_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES engine_runs(id),
        task_id TEXT NOT NULL,
        state TEXT NOT NULL,
        generated_code TEXT,
        validation_result TEXT,
        test_result TEXT,
        commit_sha TEXT,
        attempts INTEGER DEFAULT 0,
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS judge_verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        overall_pass INTEGER NOT NULL,
        score REAL NOT NULL,
        dimensions TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        judge_model TEXT NOT NULL,
        completeness INTEGER CHECK(completeness BETWEEN 0 AND 10),
        correctness INTEGER CHECK(correctness BETWEEN 0 AND 10),
        quality INTEGER CHECK(quality BETWEEN 0 AND 10),
        intelligence INTEGER CHECK(intelligence BETWEEN 0 AND 10),
        role_fit INTEGER CHECK(role_fit BETWEEN 0 AND 10),
        overall REAL GENERATED ALWAYS AS (
            (completeness + correctness + quality + intelligence + role_fit) / 5.0
        ) STORED,
        reasoning TEXT,
        error TEXT,
        scored_state TEXT,                 -- P5: validation_failed | test_failed | committed
        scored_at TEXT DEFAULT (datetime('now'))
    )""")
    # P5 migration: additive column for existing DBs (no data migration needed).
    try:
        conn.execute("ALTER TABLE engine_scores ADD COLUMN scored_state TEXT")
    except Exception:
        pass
    # T01: research schema — additive DDL (research_verdicts + research_scores).
    conn.execute("""CREATE TABLE IF NOT EXISTS research_verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        verdict_text TEXT NOT NULL,
        verdict_path TEXT,
        generated_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS research_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        judge_model TEXT NOT NULL,
        citation_coverage INTEGER CHECK(citation_coverage BETWEEN 0 AND 10),
        claim_traceability INTEGER CHECK(claim_traceability BETWEEN 0 AND 10),
        contradiction_handling INTEGER CHECK(contradiction_handling BETWEEN 0 AND 10),
        verdict_justification INTEGER CHECK(verdict_justification BETWEEN 0 AND 10),
        source_quality INTEGER CHECK(source_quality BETWEEN 0 AND 10),
        overall REAL GENERATED ALWAYS AS (
            (citation_coverage + claim_traceability + contradiction_handling
             + verdict_justification + source_quality) / 5.0
        ) STORED,
        reasoning TEXT,
        error TEXT,
        scored_at TEXT DEFAULT (datetime('now'))
    )""")
    # G1-T01/T06: edit-op results — additive DDL (edit_op_results).
    # Records per-attempt apply outcome for first_apply_rate metric.
    conn.execute("""CREATE TABLE IF NOT EXISTS edit_op_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        match_count INTEGER,
        applied INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    # G3-T01: memory_claims + memory_contradictions — additive DDL.
    # memory_claims stores individual research-claim verdicts (pending queue);
    # memory_contradictions is the CONFLICTS_WITH-style typed edge between
    # two claims (claim_a_id, claim_b_id, kind).
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_claims (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        claim_id TEXT,
        text TEXT NOT NULL,
        source_ids TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        verdict_position TEXT,
        source_atom_id TEXT,
        sidecar_path TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS memory_claims_status
        ON memory_claims(status)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS memory_claims_atom
        ON memory_claims(source_atom_id)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_contradictions (
        id TEXT PRIMARY KEY,
        claim_a_id TEXT NOT NULL REFERENCES memory_claims(id),
        claim_b_id TEXT NOT NULL REFERENCES memory_claims(id),
        run_ref_a TEXT,
        run_ref_b TEXT,
        similarity REAL NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS memory_contradictions_kind
        ON memory_contradictions(kind)""")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # prevent WAL growth
    conn.close()
    # engine.db tracks its own schema version (separate DB per Rule 6 —
    # do NOT bump benchmark-results.db's schema_version)
    conn = _get_conn(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL, updated_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("INSERT INTO schema_version (version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version)")
    conn.commit()
    conn.close()


def checkpoint_run(run_id, data):
    """Full parameterized INSERT with backup + WAL checkpoint."""
    _backup_db()  # backup before write
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO engine_runs
        (id, prd_path, project_path, config, state, current_task_id,
         current_task_idx, tasks_json, task_retries_json, started_at,
         completed_at, result_json, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        data.get("prd_path", ""),
        data.get("project_path", ""),
        data.get("config", "3090-qwen36-35b"),
        data.get("state", "IDLE"),
        data.get("current_task_id"),
        data.get("current_task_idx", 0),
        data.get("tasks_json", "[]"),
        data.get("task_retries_json", "{}"),
        data.get("started_at", datetime.now().isoformat()),
        data.get("completed_at"),
        data.get("result_json"),
        data.get("error_message"),
    ))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # prevent WAL growth
    conn.close()

    # Write tasks_json to sidecar file as backup (CHAOS FIX C9)
    tasks_json = data.get("tasks_json")
    if tasks_json:
        sidecar = f"engine_run_{run_id}_tasks.json"
        try:
            with open(sidecar, "w") as f:
                f.write(tasks_json)
        except Exception:
            pass


def save_task_result(run_id, task_id, result):
    """Full parameterized INSERT OR REPLACE into engine_task_results."""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO engine_task_results
        (run_id, task_id, state, generated_code, validation_result,
         test_result, commit_sha, attempts, started_at, completed_at, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        task_id,
        result.get("state", "IDLE"),
        result.get("generated_code"),
        result.get("validation_result"),
        result.get("test_result"),
        result.get("commit_sha"),
        result.get("attempts", 0),
        result.get("started_at", datetime.now().isoformat()),
        result.get("completed_at"),
        result.get("error_message"),
    ))
    conn.commit()
    conn.close()


def load_run(run_id):
    """SELECT from engine_runs with tasks_json validation + sidecar restore."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM engine_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)

    # Validate tasks_json integrity
    tasks_json = result.get("tasks_json", "[]")
    try:
        _json = json
        _json.loads(tasks_json)
    except (json.JSONDecodeError, TypeError):
        # tasks_json corrupted — try sidecar file
        sidecar = f"engine_run_{run_id}_tasks.json"
        try:
            with open(sidecar, "r") as f:
                result["tasks_json"] = f.read()
        except FileNotFoundError:
            # Sidecar also missing — mark as unrecoverable
            result["error_message"] = "tasks_json corrupted, no sidecar backup"
            result["tasks_json"] = "[]"

    # Validate task_retries_json
    retries_json = result.get("task_retries_json", "{}")
    try:
        json.loads(retries_json)
    except (json.JSONDecodeError, TypeError):
        result["task_retries_json"] = "{}"

    return result


def list_runs(status=None):
    """SELECT from engine_runs with optional filter."""
    conn = _get_conn()
    if status:
        rows = conn.execute("SELECT * FROM engine_runs WHERE state = ?", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM engine_runs").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_judge_verdict(run_id, verdict_dict):
    """Save a judge verdict for a run. Idempotent INSERT.

    Args:
        run_id: The engine run ID.
        verdict_dict: Dict with keys overall_pass (bool), score (float),
                      dimensions (list of dicts — serialised to JSON).
    """
    conn = _get_conn()
    conn.execute("""
        INSERT INTO judge_verdicts (run_id, overall_pass, score, dimensions)
        VALUES (?, ?, ?, ?)
    """, (
        run_id,
        1 if verdict_dict.get("overall_pass") else 0,
        verdict_dict.get("score", 0.0),
        json.dumps(verdict_dict.get("dimensions")),
    ))
    conn.commit()
    conn.close()


def save_engine_scores(run_id, task_id, judge_model, scores, reasoning=None,
                       error=None, scored_state=None):
    """Save per-task LLM judge scores to engine_scores (Rule 6: engine.db only).

    Args:
        run_id: Engine run ID.
        task_id: Task identifier (e.g. T01).
        judge_model: Judge model identity string (blinding audit trail).
        scores: Dict with 5 integer dims: completeness, correctness, quality,
                intelligence, role_fit (0-10 each).
        reasoning: Optional judge reasoning text.
        error: Optional error string (parse failure etc. — final data).
        scored_state: P5 — state code the code was in when scored:
            "validation_failed" | "test_failed" | "committed".

    Returns the inserted row id.
    """
    conn = _get_conn()
    cur = conn.execute("""
        INSERT INTO engine_scores
        (run_id, task_id, judge_model, completeness, correctness, quality,
         intelligence, role_fit, reasoning, error, scored_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id, task_id, judge_model,
        int(scores["completeness"]), int(scores["correctness"]),
        int(scores["quality"]), int(scores["intelligence"]),
        int(scores["role_fit"]),
        reasoning, error, scored_state,
    ))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def save_research_scores(run_id, task_id, judge_model, scores, reasoning=None,
                         error=None):
    """Save per-task evidence judge scores to research_scores (T01/T05).

    Args:
        run_id: Engine run ID.
        task_id: Task identifier (e.g. T01).
        judge_model: Judge model identity string (blinding audit trail).
        scores: Dict with 5 integer dims: citation_coverage, claim_traceability,
                contradiction_handling, verdict_justification, source_quality
                (0-10 each).
        reasoning: Optional judge reasoning text.
        error: Optional error string (parse failure etc. — final data).

    Returns the inserted row id.
    """
    conn = _get_conn()
    cur = conn.execute("""
        INSERT INTO research_scores
        (run_id, task_id, judge_model, citation_coverage, claim_traceability,
         contradiction_handling, verdict_justification, source_quality,
         reasoning, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id, task_id, judge_model,
        int(scores.get("citation_coverage", 0)),
        int(scores.get("claim_traceability", 0)),
        int(scores.get("contradiction_handling", 0)),
        int(scores.get("verdict_justification", 0)),
        int(scores.get("source_quality", 0)),
        reasoning, error,
    ))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id
