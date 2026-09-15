"""Session log recording — every LLM call persisted to engine.db.

SessionLogRecorder writes one row per LLM call to a ``session_logs`` table in
``engine.db``.  It mirrors AtomRecorder's context-manager API (trace.py:60-165)
so callers can use it as a ``with`` block.

Full prompts are written to a per-run JSONL sidecar
(``engine_run_<id>_session_payloads.jsonl``) — same sidecar pattern as
state.py:210-217 (tasks_json sidecar).  The DB row stores a SHA-256 hash +
the first 4000 chars inline; the sidecar holds the full payload for replay.

Rule 6: Session logs live in engine.db only — never benchmark-results.db.
Additive-only DDL (CREATE TABLE IF NOT EXISTS).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Optional


# ── DDL ──────────────────────────────────────────────────────────────────────

SESSION_LOGS_DDL = """\
CREATE TABLE IF NOT EXISTS session_logs (
    session_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    stage          TEXT NOT NULL,
    atom_id        TEXT REFERENCES atoms(atom_id),
    model          TEXT NOT NULL,
    port           INTEGER NOT NULL,
    prompt_hash    TEXT NOT NULL,
    prompt_truncated TEXT NOT NULL,
    prompt_payload_path TEXT,
    system_message TEXT,
    user_message   TEXT NOT NULL,
    response_content TEXT NOT NULL,
    response_reasoning TEXT,
    finish_reason  TEXT NOT NULL,
    prompt_tokens  INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens   INTEGER NOT NULL,
    thinking_tokens INTEGER DEFAULT 0,
    latency_ms     INTEGER NOT NULL,
    exhausted      INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

SESSION_LOGS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_session_logs_run_task ON session_logs(run_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_logs_atom ON session_logs(atom_id)",
]

GENERATION_TELEMETRY_DDL = """\
CREATE TABLE IF NOT EXISTS generation_telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    stage           TEXT NOT NULL,
    model           TEXT NOT NULL,
    port            INTEGER NOT NULL,
    role            TEXT NOT NULL,
    prompt_tokens   INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    thinking_tokens INTEGER DEFAULT 0,
    total_tokens    INTEGER NOT NULL,
    tokens_per_sec  REAL DEFAULT 0.0,
    latency_ms      INTEGER NOT NULL,
    finish_reason   TEXT NOT NULL,
    exhausted       INTEGER NOT NULL DEFAULT 0,
    max_tokens_used INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

GENERATION_TELEMETRY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gen_tel_model ON generation_telemetry(model, role)",
    "CREATE INDEX IF NOT EXISTS idx_gen_tel_run ON generation_telemetry(run_id, task_id)",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hash_text(text: str) -> str:
    """SHA-256 hex of arbitrary text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int = 4000) -> str:
    """Truncate text to *limit* chars, appending a marker if truncated."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def _cap_response(text: str, limit: int = 16_384) -> str:
    """Cap response content at *limit* chars (model output limit ~16 KB)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [response capped, {len(text)} chars total]"


# Secret patterns to redact from inline prompt storage (R9).
_SECRET_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9]{16,}', re.IGNORECASE),
    re.compile(
        r'(?:api[_-]?key|token|secret|password)\s*[:=]\s*["\']?([^\s"\']{16,})',
        re.IGNORECASE,
    ),
    re.compile(r'(?:Bearer|Basic)\s+[a-zA-Z0-9+/=]{20,}', re.IGNORECASE),
]


def _redact_secrets(text: str) -> str:
    """Redact secret patterns from text before inline storage (R9).

    Replaces matched secrets with ``<REDACTED>``.
    """
    result = text
    for pat in _SECRET_PATTERNS:
        result = pat.sub("<REDACTED>", result)
    return result


def _sidecar_path(run_id: str, run_dir: Optional[str] = None) -> str:
    """Canonical sidecar path for a run's session payloads."""
    if run_dir:
        return os.path.join(run_dir, f"engine_run_{run_id}_session_payloads.jsonl")
    return f"engine_run_{run_id}_session_payloads.jsonl"


def _get_conn(db_path: str = "engine.db") -> sqlite3.Connection:
    """Get a WAL-mode connection with busy_timeout (reuses state.py pattern)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_session_logs_db(db_path: str = "engine.db") -> None:
    """Idempotently create the ``session_logs`` + ``generation_telemetry``
    tables and indexes in *db_path*."""
    conn = _get_conn(db_path)
    try:
        conn.execute(SESSION_LOGS_DDL)
        for idx in SESSION_LOGS_INDEXES:
            conn.execute(idx)
        conn.execute(GENERATION_TELEMETRY_DDL)
        for idx in GENERATION_TELEMETRY_INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        conn.close()


# ── SessionLogRecorder ──────────────────────────────────────────────────────

class SessionLogRecorder:
    """Record every LLM call for a single run.

    Usage::

        with SessionLogRecorder("run-abc", db_path="engine.db") as rec:
            rec.record(task_id="T01", attempt=1, stage="generate",
                       model="3090-qwen36-35b", port=8080,
                       prompt_text=prompt, response=response_dict,
                       latency_ms=3200, atom_id=None)
    """

    def __init__(
        self,
        run_id: str,
        db_path: str = "engine.db",
        run_dir: Optional[str] = None,
        max_inline_prompt: int = 4000,
        max_inline_response: int = 16_384,
    ) -> None:
        self.run_id = run_id
        self._db_path = db_path
        self._run_dir = run_dir
        self._max_inline_prompt = max_inline_prompt
        self._max_inline_response = max_inline_response
        self._conn: sqlite3.Connection | None = _get_conn(db_path)
        # Ensure tables exist even if init_session_logs_db was never called.
        self._conn.execute(SESSION_LOGS_DDL)
        for idx in SESSION_LOGS_INDEXES:
            self._conn.execute(idx)
        self._conn.execute(GENERATION_TELEMETRY_DDL)
        for idx in GENERATION_TELEMETRY_INDEXES:
            self._conn.execute(idx)
        self._conn.commit()
        self._sidecar_file: Optional[Any] = None
        self._sidecar_path: Optional[str] = None

    # ── Context manager ────────────────────────────────────────────────────

    def __enter__(self) -> "SessionLogRecorder":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Sidecar ───────────────────────────────────────────────────────────

    def _open_sidecar(self) -> None:
        """Lazily open the per-run JSONL sidecar for writing."""
        if self._sidecar_file is not None:
            return
        path = _sidecar_path(self.run_id, self._run_dir)
        self._sidecar_path = path
        self._sidecar_file = open(path, "a", encoding="utf-8")

    def _write_sidecar_payload(self, session_id: str, prompt_text: str,
                                system_message: Optional[str],
                                user_message: str,
                                response_content: str,
                                response_reasoning: Optional[str],
                                meta: Optional[dict] = None) -> None:
        """Append the full prompt/response payload to the sidecar JSONL."""
        self._open_sidecar()
        entry = {
            "session_id": session_id,
            "run_id": self.run_id,
            "system_message": system_message,
            "user_message": user_message,
            "prompt_text": prompt_text,
            "response_content": response_content,
            "response_reasoning": response_reasoning,
            "meta": meta or {},
            "written_at": time.time(),
        }
        self._sidecar_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._sidecar_file.flush()

    # ── Core API ───────────────────────────────────────────────────────────

    def record(
        self,
        task_id: str,
        attempt: int,
        stage: str,
        model: str,
        port: int,
        prompt_text: str,
        response: dict,
        latency_ms: float,
        atom_id: Optional[str] = None,
        system_message: Optional[str] = None,
        role: str = "coder",
        max_tokens_used: int = 0,
        error: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Insert one session_log row (and a generation_telemetry row).

        Returns the ``session_id``.
        """
        if self._conn is None:
            raise RuntimeError("SessionLogRecorder is closed")

        session_id = f"{self.run_id}-{task_id}-{attempt}-{stage}"
        prompt_hash = _hash_text(prompt_text)
        # Redact secrets before inline storage (R9).
        prompt_redacted = _redact_secrets(prompt_text)
        prompt_truncated = _truncate(prompt_redacted, self._max_inline_prompt)

        # Extract response fields (flat dict from transport).
        response_content = response.get("content", "") or ""
        response_reasoning = response.get("reasoning_content", "") or ""
        finish_reason = response.get("finish_reason", "")
        prompt_tokens = response.get("prompt_tokens", 0)
        completion_tokens = response.get("completion_tokens", 0)
        total_tokens = response.get("total_tokens", 0)
        thinking_tokens = response.get("thinking_tokens", 0)
        tokens_per_sec = response.get("predicted_per_second", 0.0)
        # Epic-5: exhausted = empty content + reasoning produced.
        # finish_reason="length" is the classic signal, but some servers
        # omit it — treat blank/missing finish_reason the same (the
        # escalation budget in engine.py bounds retries regardless).
        _fr = response.get("finish_reason", "")
        exhausted = 1 if (_fr in ("length", "", None)
                          and not response_content.strip()
                          and (response_reasoning or thinking_tokens)) else 0

        # Cap response in-row.
        response_content_capped = _cap_response(response_content,
                                                 self._max_inline_response)

        # Sidecar: full prompt payload.
        self._write_sidecar_payload(
            session_id, prompt_text, system_message, prompt_text,
            response_content, response_reasoning, meta,
        )

        # In-row insert.
        self._conn.execute(
            """\
            INSERT OR REPLACE INTO session_logs
                (session_id, run_id, task_id, attempt, stage, atom_id,
                 model, port, prompt_hash, prompt_truncated,
                 prompt_payload_path, system_message, user_message,
                 response_content, response_reasoning, finish_reason,
                 prompt_tokens, completion_tokens, total_tokens,
                 thinking_tokens, latency_ms, exhausted, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, self.run_id, task_id, attempt, stage, atom_id,
                model, port, prompt_hash, prompt_truncated,
                self._sidecar_path, system_message, prompt_truncated,
                response_content_capped, response_reasoning, finish_reason,
                prompt_tokens, completion_tokens, total_tokens,
                thinking_tokens, int(latency_ms), exhausted, error,
            ),
        )

        # generation_telemetry row (per-attempt token/latency profile).
        self._conn.execute(
            """\
            INSERT INTO generation_telemetry
                (run_id, task_id, attempt, stage, model, port, role,
                 prompt_tokens, completion_tokens, thinking_tokens,
                 total_tokens, tokens_per_sec, latency_ms, finish_reason,
                 exhausted, max_tokens_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id, task_id, attempt, stage, model, port, role,
                prompt_tokens, completion_tokens, thinking_tokens,
                total_tokens, tokens_per_sec, int(latency_ms), finish_reason,
                exhausted, max_tokens_used,
            ),
        )

        self._conn.commit()
        return session_id

    def get_logs(
        self,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[dict]:
        """Return session logs for *run_id* (default: self.run_id), optionally
        filtered by *task_id``, ordered by created_at."""
        rid = run_id or self.run_id
        if self._conn is None:
            raise RuntimeError("SessionLogRecorder is closed")

        if task_id:
            cur = self._conn.execute(
                """\
                SELECT * FROM session_logs
                WHERE run_id = ? AND task_id = ?
                ORDER BY created_at
                """,
                (rid, task_id),
            )
        else:
            cur = self._conn.execute(
                """\
                SELECT * FROM session_logs
                WHERE run_id = ?
                ORDER BY created_at
                """,
                (rid,),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_telemetry(
        self,
        run_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> list[dict]:
        """Return generation_telemetry rows, optionally filtered by *model*."""
        rid = run_id or self.run_id
        if self._conn is None:
            raise RuntimeError("SessionLogRecorder is closed")

        if model:
            cur = self._conn.execute(
                """\
                SELECT * FROM generation_telemetry
                WHERE run_id = ? AND model = ?
                ORDER BY created_at
                """,
                (rid, model),
            )
        else:
            cur = self._conn.execute(
                """\
                SELECT * FROM generation_telemetry
                WHERE run_id = ?
                ORDER BY created_at
                """,
                (rid,),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        """Close the DB connection and sidecar file."""
        if self._sidecar_file is not None:
            try:
                self._sidecar_file.close()
            except Exception:
                pass
            self._sidecar_file = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
