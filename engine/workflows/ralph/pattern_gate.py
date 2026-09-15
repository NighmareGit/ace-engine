"""ADR-0002-compliant pattern registry for the Ralph loop (S2 §2/S6, S3 #8).

Canonical store is the ``ralph_patterns`` table in engine.db (additive-only,
WAL). Patterns are ``proposed`` on creation and become ``approved`` only
through the mechanical approval gate (``approve_pattern``). Prompt injection
reads ``approved`` rows only (R2).

Markdown skill-docs are a **one-way export** of approved rows (seeds the G9
skill registry), never the source of truth (S3 hand-off #8).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine.state import _get_conn

log = logging.getLogger("engine.workflows.ralph.pattern_gate")


# ---------------------------------------------------------------------------
# Schema (S3 hand-off #8 — authoritative)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ralph_patterns (
    id TEXT PRIMARY KEY,
    project_namespace TEXT NOT NULL,
    pattern_type TEXT NOT NULL,
    body TEXT NOT NULL,
    keywords TEXT,
    score REAL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'proposed',
    source_ralph_run_id TEXT,
    source_round_id INTEGER,
    approved_by TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT,
    FOREIGN KEY (source_ralph_run_id) REFERENCES ralph_runs(ralph_run_id)
);
CREATE INDEX IF NOT EXISTS ralph_patterns_status_ns ON ralph_patterns(status, project_namespace);
CREATE INDEX IF NOT EXISTS ralph_patterns_keywords ON ralph_patterns(keywords);
"""

VALID_STATUSES = ("proposed", "approved", "rejected", "expired")
VALID_PATTERN_TYPES = ("strategy", "lesson", "anti_pattern", "heuristic")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class PatternCapHit(Exception):
    """Raised when max_patterns_per_run cap is reached (M3)."""


@dataclass
class PatternProposal:
    """A pattern proposed by a Ralph round (not yet persisted)."""
    pattern_id: str
    project_namespace: str
    pattern_type: str
    title: str
    body: str
    keywords: list[str] | None = None
    score: float = 0.0
    source_ralph_run_id: str | None = None
    source_round_id: int | None = None


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

def _ensure_schema() -> None:
    """Create the ralph_patterns table if absent (additive-only)."""
    conn = _get_conn()
    try:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Propose / persist / approve / reject
# ---------------------------------------------------------------------------

def propose_pattern_update(
    round_report: Any,
    project_namespace: str = "default",
    pattern_type: str = "lesson",
    title: str = "",
    body: str = "",
    keywords: list[str] | None = None,
    score: float = 0.0,
) -> PatternProposal:
    """Create a pattern proposal from a round report. Does NOT persist (S2 §2).

    The proposal is a ``PatternProposal`` with status implied as ``proposed``.
    Call ``persist_pattern`` to write it to the table.
    """
    pattern_id = str(uuid.uuid4())
    source_run = getattr(round_report, "ralph_run_id", None)
    source_round = getattr(round_report, "round_id", None)
    return PatternProposal(
        pattern_id=pattern_id,
        project_namespace=project_namespace,
        pattern_type=pattern_type,
        title=title,
        body=body[:8192],
        keywords=keywords or [],
        score=score,
        source_ralph_run_id=source_run,
        source_round_id=source_round,
    )


def persist_pattern(
    proposal: PatternProposal,
    max_length_bytes: int = 8192,
    max_patterns_per_run: int = 0,
    pattern_approval_required: bool = True,
) -> str:
    """Write a pattern to the ``ralph_patterns`` table.

    Returns the pattern_id.

    Enforcement:
    - **max_patterns_per_run** (M3): if > 0, counts existing patterns for this
      ``source_ralph_run_id`` and raises ``PatternCapHit`` when at cap instead
      of silently dropping.
    - **pattern_approval_required** (S5 #3): when True (default), the row is
      written ``proposed`` and must pass ``approve_pattern``. When False
      (testing kill-switch), the row is written ``approved`` with
      ``approved_by='auto'``.
    """
    _ensure_schema()
    body = proposal.body[:max_length_bytes]
    keywords_json = json.dumps(proposal.keywords or [])

    # M3: enforce max_patterns_per_run cap.
    if max_patterns_per_run > 0 and proposal.source_ralph_run_id:
        conn = _get_conn()
        try:
            existing = conn.execute(
                """SELECT COUNT(*) FROM ralph_patterns
                   WHERE source_ralph_run_id=?""",
                (proposal.source_ralph_run_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        if existing >= max_patterns_per_run:
            raise PatternCapHit(
                f"max_patterns_per_run={max_patterns_per_run} reached for "
                f"ralph_run_id={proposal.source_ralph_run_id} "
                f"(existing={existing})")

    # S5 #3: determine initial status from pattern_approval_required.
    status = "proposed" if pattern_approval_required else "approved"
    approved_by = None if pattern_approval_required else "auto"

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO ralph_patterns
               (id, project_namespace, pattern_type, body, keywords, score,
                status, source_ralph_run_id, source_round_id, approved_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.pattern_id, proposal.project_namespace,
                proposal.pattern_type, body, keywords_json, proposal.score,
                status, proposal.source_ralph_run_id,
                proposal.source_round_id, approved_by,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return proposal.pattern_id


def approve_pattern(pattern_id: str, approved_by: str = "owner") -> bool:
    """Transition a pattern ``proposed→approved`` (ADR-0002 gate).

    Returns True if the row was updated. Only ``proposed`` rows can be approved.
    """
    _ensure_schema()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE ralph_patterns SET status='approved', approved_by=?
               WHERE id=? AND status='proposed'""",
            (approved_by, pattern_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reject_pattern(pattern_id: str, rejected_by: str = "owner") -> bool:
    """Transition a pattern ``proposed→rejected``."""
    _ensure_schema()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE ralph_patterns SET status='rejected', approved_by=?
               WHERE id=? AND status='proposed'""",
            (rejected_by, pattern_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def expire_pattern(pattern_id: str) -> bool:
    """Transition a pattern to ``expired`` (TTL)."""
    _ensure_schema()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE ralph_patterns SET status='expired' WHERE id=? AND status='approved'",
            (pattern_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Markdown export (one-way projection, S3 hand-off #8)
# ---------------------------------------------------------------------------

def export_approved_patterns(
    project_namespace: str = "default",
    skills_dir: str | Path = "engine/intent/skills",
    limit: int = 100,
) -> list[Path]:
    """Project approved patterns to markdown skill-docs (one-way export).

    This is a ONE-WAY export: markdown is regenerated from SQL, never read
    back as source of truth. Returns the list of written file paths.
    """
    from engine.workflows.ralph.pattern_query import query_patterns
    rows = query_patterns(project_namespace=project_namespace,
                          keywords=None, status="approved", limit=limit)
    skills_path = Path(skills_dir)
    skills_path.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for row in rows:
        pid = row["id"]
        title = row.get("body", "").split("\n")[0][:80] if row.get("body") else pid
        safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)
        filename = f"ralph_pattern_{pid[:8]}_{safe_title[:40]}.md"
        filepath = skills_path / filename
        content = _render_markdown(row)
        filepath.write_text(content, encoding="utf-8")
        written.append(filepath)
    return written


def _render_markdown(row: dict) -> str:
    """Render a pattern row as markdown skill-doc."""
    keywords = row.get("keywords", "[]")
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except (json.JSONDecodeError, TypeError):
            keywords = []
    return (
        f"---\n"
        f"pattern_id: {row['id']}\n"
        f"pattern_type: {row.get('pattern_type', 'lesson')}\n"
        f"score: {row.get('score', 0.0)}\n"
        f"keywords: {', '.join(keywords)}\n"
        f"source_ralph_run_id: {row.get('source_ralph_run_id', '')}\n"
        f"---\n\n"
        f"# {row.get('body', '').split(chr(10))[0][:80]}\n\n"
        f"{row.get('body', '')}\n"
    )
