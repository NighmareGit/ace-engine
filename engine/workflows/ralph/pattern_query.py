"""Programmatic query interface for ralph_patterns (S5 #5, R2, R6).

SQL LIKE-based multi-keyword AND (no FTS5 — its virtual table + sync triggers
conflict with the additive-only DB rule). Returns only ``approved`` rows by
default (ADR-0002: ``proposed`` rows are never injected into prompts, R2).

Ranking: ``score DESC, created_at DESC`` (S5 #5).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from engine.state import _get_conn

log = logging.getLogger("engine.workflows.ralph.pattern_query")


def query_patterns(
    project_namespace: str = "default",
    keywords: list[str] | None = None,
    status: str = "approved",
    limit: int = 10,
) -> list[dict]:
    """Query ralph_patterns with LIKE multi-keyword AND matching.

    Args:
        project_namespace: per-project isolation (R2: kills cross-project
            contamination). Defaults to current project.
        keywords: list of keywords; ALL must match (AND semantics) via SQL
            LIKE on the keywords column. None = no keyword filter.
        status: row status to filter (default ``approved`` — ADR-0002 gate).
        limit: max rows returned (default 10).

    Returns:
        List of pattern dicts, ranked ``score DESC, created_at DESC``.
    """
    # Ensure the table exists (idempotent, additive).
    from engine.workflows.ralph.pattern_gate import _ensure_schema
    _ensure_schema()

    conn = _get_conn()
    try:
        query = "SELECT * FROM ralph_patterns WHERE project_namespace = ? AND status = ?"
        params: list[Any] = [project_namespace, status]

        # LIKE multi-keyword AND (S5 #5).
        if keywords:
            for kw in keywords:
                query += " AND keywords LIKE ?"
                params.append(f"%{kw}%")

        query += " ORDER BY score DESC, created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))

        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_pattern(pattern_id: str) -> dict | None:
    """Fetch a single pattern by ID."""
    from engine.workflows.ralph.pattern_gate import _ensure_schema
    _ensure_schema()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM ralph_patterns WHERE id = ?", (pattern_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _row_to_dict(row: Any) -> dict:
    """Convert a sqlite3.Row to a plain dict, parsing JSON keywords."""
    d = dict(row)
    kw = d.get("keywords")
    if isinstance(kw, str):
        try:
            d["keywords"] = json.loads(kw)
        except (json.JSONDecodeError, TypeError):
            d["keywords"] = []
    return d
