"""Atom trace recording — state-transition atoms persisted to engine.db.

AtomRecorder writes one row per state transition to an ``atoms`` table in
``engine.db``.  It is intentionally stateless w.r.t. pipeline progress — the
caller supplies ``task_id`` and ``seq`` on every ``record()`` call so the
recorder can be tested without a live pipeline.

Rule 6: Traces live in engine.db only — never benchmark-results.db.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

# ── Atoms table DDL ───────────────────────────────────────────────────────────

ATOMS_DDL = """\
CREATE TABLE IF NOT EXISTS atoms (
    atom_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    gate_hash   TEXT,
    duration_ms INTEGER NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hash_dict(d: dict) -> str:
    """Deterministic SHA-256 hex of a dict (keys sorted, default=str)."""
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode()
    ).hexdigest()


def init_atoms_db(db_path: str = "engine.db") -> None:
    """Idempotently create the *atoms* table in *db_path*."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(ATOMS_DDL)
        conn.commit()
    finally:
        conn.close()


# ── AtomRecorder ──────────────────────────────────────────────────────────────

class AtomRecorder:
    """Record state-transition atoms for a single run.

    Usage::

        with AtomRecorder("run-abc", "engine.db") as rec:
            atom_id = rec.record(
                task_id="T01", seq=1,
                from_state="IDLE", to_state="PARSING",
                inputs={"prompt": "..."}, outputs={"parsed": True},
            )
    """

    def __init__(self, run_id: str, db_path: str = "engine.db") -> None:
        self.run_id = run_id
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = sqlite3.connect(db_path)
        # Ensure table exists even if init_atoms_db was never called.
        self._conn.execute(ATOMS_DDL)
        self._conn.commit()

    # ── Context manager ────────────────────────────────────────────────────

    def __enter__(self) -> AtomRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Core API ───────────────────────────────────────────────────────────

    def record(
        self,
        task_id: str,
        seq: int,
        from_state: str,
        to_state: str,
        inputs: dict,
        outputs: dict,
        gate_hash: str | None = None,
        meta: dict | None = None,
    ) -> str:
        """Insert one atom row and return its ``atom_id``.

        ``duration_ms`` is set to 0 (placeholder); real timing comes from
        ``pipeline.py`` integration.
        """
        atom_id = f"{self.run_id}-{seq:04d}"
        input_hash = _hash_dict(inputs)
        output_hash = _hash_dict(outputs)
        meta_json = json.dumps(meta or {}, sort_keys=True, default=str)

        if self._conn is None:
            raise RuntimeError("AtomRecorder is closed")

        self._conn.execute(
            """\
            INSERT OR REPLACE INTO atoms
                (atom_id, run_id, task_id, seq, from_state, to_state,
                 input_hash, output_hash, gate_hash, duration_ms, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                atom_id,
                self.run_id,
                task_id,
                seq,
                from_state,
                to_state,
                input_hash,
                output_hash,
                gate_hash,
                0,  # placeholder duration
                meta_json,
            ),
        )
        self._conn.commit()
        return atom_id

    def get_trace(self, run_id: str | None = None) -> list[dict]:
        """Return all atoms for *run_id*, ordered by ``seq``.

        If *run_id* is ``None``, uses ``self.run_id``.
        """
        rid = run_id or self.run_id
        if self._conn is None:
            raise RuntimeError("AtomRecorder is closed")

        cur = self._conn.execute(
            """\
            SELECT atom_id, run_id, task_id, seq, from_state, to_state,
                   input_hash, output_hash, gate_hash, duration_ms, meta_json,
                   created_at
            FROM atoms
            WHERE run_id = ?
            ORDER BY seq
            """,
            (rid,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        """Close the underlying DB connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
