"""Poisoning gate for memory_claims (G3-T02).

Extends the ADR-0002 gate contract from ``engine.workflows.ralph.pattern_gate``
to the ``memory_claims`` store. The status-column contract mirrors ralph:

    pending  ──approve──▶  approved
    pending  ──reject──▶   rejected

Only ``pending`` rows transition (ADR-0002 invariant). ``approved`` rows are
recallable; ``pending``/``rejected`` rows are invisible to recall — the
one-way write (gate), gated read (recall) contract.

``submit_claims`` persists a verdict sidecar's claims as ``pending`` and
auto-runs ``CrossRunComparator`` against all ``approved`` claims, persisting
any detected contradictions to ``memory_contradictions``. The contradiction
pre-check is non-blocking — findings are attached to the result for the
human reviewer to consider.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from engine.state import _get_conn

from engine.memory.comparator import Contradiction, CrossRunComparator

log = logging.getLogger("engine.memory.gate")


# ---------------------------------------------------------------------------
# Status-column contract (mirrors pattern_gate.VALID_STATUSES for ralph)
# ---------------------------------------------------------------------------

VALID_STATUSES = ("pending", "approved", "rejected")
DEFAULT_STATUS = "pending"

# Contradiction status-column contract (DESIGN-G3 §2).
CONTRADICTION_STATUSES = ("pending", "confirmed", "dismissed")
CONTRADICTION_DEFAULT_STATUS = "pending"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ApprovalResult:
    """Outcome of an approve/reject action, with contradiction findings attached.

    ``contradictions`` is empty when the claim is clean against the approved
    store. Non-empty means the gate detected conflicts pre-approval; the human
    decides whether to approve anyway (non-blocking).
    """

    record_id: str
    approved: bool
    contradictions: list[Contradiction] = field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# DDL lives in engine/state.py init_db() (additive). This module assumes the
# memory_claims + memory_contradictions tables exist.


# ---------------------------------------------------------------------------
# Submit + auto-run comparator
# ---------------------------------------------------------------------------

def submit_claims(
    run_id: str,
    task_id: str,
    sidecar: dict,
    *,
    source_atom_id: str = "",
    sidecar_path: str = "",
) -> list[str]:
    """Persist a verdict sidecar's claims as ``pending`` and auto-run the comparator.

    Each claim in ``sidecar["claims"]`` becomes a ``memory_claims`` row with
    status ``pending``. The comparator then runs the new claims against all
    existing ``approved`` claims; any contradictions are persisted to
    ``memory_contradictions``.

    Args:
        run_id: the research run that produced this sidecar.
        task_id: the task id the claims belong to.
        sidecar: verdict sidecar dict with a ``"claims"`` list; each claim
            carries ``id``, ``text``, ``source_ids``, and optionally
            ``verdict_position``.
        source_atom_id: the research atom that produced the claims.
        sidecar_path: filesystem path to the verdict.evidence.json sidecar.

    Returns:
        List of newly-created ``memory_claims`` ids (order matches the
        sidecar's claims list). Empty when the sidecar has no claims.
    """
    claims = sidecar.get("claims", []) if sidecar else []
    if not claims:
        return []

    # Pull the verdict position at the sidecar level (applies to all claims
    # when per-claim position is absent).
    sidecar_position = sidecar.get("verdict_position", "")

    conn = _get_conn()
    new_ids: list[str] = []
    decorated: list[dict] = []
    try:
        now = datetime.now().isoformat()
        for claim in claims:
            claim_id = claim.get("id") or str(uuid.uuid4())
            record_id = str(uuid.uuid4())
            text = claim.get("text", "")
            source_ids = claim.get("source_ids", [])
            position = claim.get("verdict_position", "") or sidecar_position

            conn.execute(
                """INSERT INTO memory_claims
                   (id, run_id, task_id, claim_id, text, source_ids, status,
                    verdict_position, source_atom_id, sidecar_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id, run_id, task_id, claim_id, text,
                    json.dumps(source_ids), DEFAULT_STATUS, position,
                    source_atom_id, sidecar_path, now,
                ),
            )
            new_ids.append(record_id)
            # Decorate with record_id so the comparator's returned claim_a_id
            # matches the memory_claims primary key (needed for the
            # contradiction join in list_pending).
            decorated.append({
                "id": record_id,
                "text": text,
                "run_id": run_id,
                "verdict_position": position,
                "source_atom_id": source_atom_id,
            })
        conn.commit()
    finally:
        conn.close()

    # Auto-run comparator against approved claims; persist contradictions.
    _run_contradiction_check(run_id, task_id, decorated, source_atom_id)

    return new_ids


def _run_contradiction_check(
    run_id: str,
    task_id: str,
    new_claims: list[dict],
    source_atom_id: str,
) -> None:
    """Compare new claims against approved claims and persist contradictions.

    Side-effect: writes rows to ``memory_contradictions``. Never raises — the
    check is non-blocking; a comparator failure must not break claim
    submission. Runs inside a try/except that logs and continues.
    """
    try:
        approved = _fetch_approved_claims()
        if not approved:
            return

        # Decorate new claims with run_id so the comparator can filter
        # self-same-run pairs.
        decorated = []
        for c in new_claims:
            decorated.append({
                "id": c.get("id", ""),
                "text": c.get("text", ""),
                "run_id": run_id,
                "verdict_position": c.get("verdict_position", ""),
                "source_atom_id": source_atom_id,
            })

        contradictions = CrossRunComparator.compare(
            {"claims": decorated}, approved,
        )
        if not contradictions:
            return

        conn = _get_conn()
        try:
            now = datetime.now().isoformat()
            for con in contradictions:
                conn.execute(
                    """INSERT INTO memory_contradictions
                       (id, claim_a_id, claim_b_id, run_ref_a, run_ref_b,
                        similarity, kind, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), con.claim_a_id, con.claim_b_id,
                        con.run_ref_a, con.run_ref_b, con.similarity,
                        con.kind, CONTRADICTION_DEFAULT_STATUS, now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        log.info(
            "contradiction check: %d finding(s) for run=%s task=%s",
            len(contradictions), run_id, task_id,
        )
    except Exception as exc:  # noqa: BLE001 — non-blocking check
        log.warning("contradiction check failed (non-blocking): %s", exc)


def _fetch_approved_claims() -> list[dict]:
    """Fetch all ``approved`` claims as dicts shaped for the comparator."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, text AS claim_text, run_id, verdict_position,
                      source_atom_id
               FROM memory_claims WHERE status='approved'"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Approve / reject helpers
# ---------------------------------------------------------------------------

def approve(record_id: str, approved_by: str = "cli") -> ApprovalResult:
    """Transition a claim ``pending→approved`` with a contradiction pre-check.

    The pre-check runs the comparator against existing approved claims and
    attaches findings to the ``ApprovalResult`` (non-blocking — the human
    decides). Only ``pending`` rows can be approved (ADR-0002 invariant).
    """
    contradictions = _pre_check_contradictions(record_id)

    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE memory_claims SET status='approved' WHERE id=? AND status='pending'""",
            (record_id,),
        )
        conn.commit()
        updated = cur.rowcount > 0
    finally:
        conn.close()

    if updated:
        return ApprovalResult(
            record_id=record_id,
            approved=True,
            contradictions=contradictions,
            message=f"approved by {approved_by}"
            + (f" with {len(contradictions)} contradiction(s)"
               if contradictions else " (clean)"),
        )
    return ApprovalResult(
        record_id=record_id,
        approved=False,
        contradictions=contradictions,
        message="record not found or not in 'pending' status",
    )


def reject(record_id: str, rejected_by: str = "cli") -> ApprovalResult:
    """Transition a claim ``pending→rejected``."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE memory_claims SET status='rejected' WHERE id=? AND status='pending'""",
            (record_id,),
        )
        conn.commit()
        updated = cur.rowcount > 0
    finally:
        conn.close()

    if updated:
        return ApprovalResult(
            record_id=record_id,
            approved=False,
            message=f"rejected by {rejected_by}",
        )
    return ApprovalResult(
        record_id=record_id,
        approved=False,
        message="record not found or not in 'pending' status",
    )


def approve_for_recall(record_id: str, store: str,
                       approved_by: str = "cli") -> ApprovalResult:
    """Unified approval dispatch across stores (DESIGN-G3 §3).

    Thin router to the per-store approve, sharing the status-transition
    contract (only ``pending`` rows transition — ADR-0002 invariant).

    Args:
        record_id: the row to approve (claim id or pattern id).
        store: ``'claims'`` (memory_claims) or ``'patterns'`` (ralph_patterns).
        approved_by: actor tag for the audit trail.

    Returns:
        ``ApprovalResult`` from the underlying per-store approve.
    """
    if store == "patterns":
        from engine.workflows.ralph.pattern_gate import approve_pattern
        ok = approve_pattern(record_id, approved_by=approved_by)
        return ApprovalResult(
            record_id=record_id,
            approved=ok,
            message="approved" if ok else "pattern not found or not in 'proposed' status",
        )
    # Default: memory_claims.
    return approve(record_id, approved_by=approved_by)


def confirm_contradiction(contradiction_id: str) -> bool:
    """Transition a contradiction ``pending→confirmed``.

    Only ``pending`` rows can be confirmed. Returns True if the row was updated.
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE memory_contradictions SET status='confirmed'
               WHERE id=? AND status='pending'""",
            (contradiction_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def dismiss_contradiction(contradiction_id: str) -> bool:
    """Transition a contradiction ``pending→dismissed``.

    Only ``pending`` rows can be dismissed. Returns True if the row was updated.
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE memory_contradictions SET status='dismissed'
               WHERE id=? AND status='pending'""",
            (contradiction_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _pre_check_contradictions(record_id: str) -> list[Contradiction]:
    """Run the comparator for ``record_id`` against approved claims.

    Returns the list of detected contradictions (empty = clean). Never raises;
    a comparator failure must not block the approval flow.
    """
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                """SELECT id, text AS claim_text, run_id, verdict_position,
                          source_atom_id
                   FROM memory_claims WHERE id=?""",
                (record_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return []
        # The comparator's new-side reads ``text``/``run_id``/``verdict_position``
        # /``source_atom_id``; the SQL alias returns ``claim_text`` — normalise.
        row_dict = dict(row)
        target = [{
            "id": row_dict.get("id", ""),
            "text": row_dict.get("claim_text", ""),
            "run_id": row_dict.get("run_id", ""),
            "verdict_position": row_dict.get("verdict_position", ""),
            "source_atom_id": row_dict.get("source_atom_id", ""),
        }]
        approved = _fetch_approved_claims()
        if not approved:
            return []
        return CrossRunComparator.compare({"claims": target}, approved)
    except Exception as exc:  # noqa: BLE001 — non-blocking pre-check
        log.warning("pre-check failed (non-blocking): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Pending queue
# ---------------------------------------------------------------------------

def list_pending(limit: int = 100) -> list[dict]:
    """List ``pending`` claims ordered by ``created_at DESC`` with contradiction counts.

    This is the pending queue surfaced by ``ace memory list`` — the
    status-column pattern from ``ralph_patterns.status`` reused for the
    ``memory_claims`` store.

    Two simple queries merged in Python (avoids the UNION ALL subquery):
    one fetches pending claims, the other counts contradictions per claim id.
    """
    conn = _get_conn()
    try:
        # Query 1: pending claims.
        rows = conn.execute(
            """SELECT id, run_id, task_id, claim_id, text,
                      verdict_position, source_atom_id, created_at
               FROM memory_claims
               WHERE status='pending'
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        claims = [dict(r) for r in rows]

        if not claims:
            return []

        # Query 2: contradiction counts per claim id.
        claim_ids = [c["id"] for c in claims]
        placeholders = ",".join("?" for _ in claim_ids)
        counts = conn.execute(
            f"""SELECT claim_a_id AS cid, COUNT(*) AS cnt
                FROM memory_contradictions
                WHERE claim_a_id IN ({placeholders})
                GROUP BY claim_a_id""",
            claim_ids,
        ).fetchall()
        counts_b = conn.execute(
            f"""SELECT claim_b_id AS cid, COUNT(*) AS cnt
                FROM memory_contradictions
                WHERE claim_b_id IN ({placeholders})
                GROUP BY claim_b_id""",
            claim_ids,
        ).fetchall()

        # Merge counts in Python.
        count_map: dict[str, int] = {}
        for r in counts:
            count_map[r["cid"]] = count_map.get(r["cid"], 0) + r["cnt"]
        for r in counts_b:
            count_map[r["cid"]] = count_map.get(r["cid"], 0) + r["cnt"]

        for c in claims:
            c["contradiction_count"] = count_map.get(c["id"], 0)

        return claims
    finally:
        conn.close()
