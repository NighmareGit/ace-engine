"""Tool-Gap Detector (TGD) — persistence layer (ORCH-7).

Persists a GapReport to engine.db (capability_gaps table, additive) for
machine consumption and triage tracking. The triage-outcome fields power the
kill-criterion metric (triage yield): >= 1 accepted action per 10 reports.

No branch writes, no network. Best-effort: persistence failures must never
fail the run.
"""

import json

from engine.state import _get_conn


def _ensure_table():
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS capability_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,                        -- NULL for history scans
        scanned_runs INTEGER DEFAULT 0,
        signal TEXT NOT NULL,               -- G1..G11
        severity TEXT,
        confidence REAL,
        triage_category TEXT,
        message TEXT,
        evidence_json TEXT,                 -- list of evidence rows
        -- Kill-criterion instrumentation: triage outcome per finding.
        triage_outcome TEXT DEFAULT '',     -- accepted | rejected | deferred | ''
        triage_note TEXT DEFAULT '',
        triage_at TEXT,                     -- ISO timestamp of triage decision
        detector_version TEXT DEFAULT '1.0',
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()


def persist_gap_report(report) -> list[int]:
    """Persist the gap report findings to engine.db. Returns list of row ids."""
    _ensure_table()
    conn = _get_conn()
    row_ids = []
    for f in report.findings:
        cur = conn.execute("""
            INSERT INTO capability_gaps
            (run_id, scanned_runs, signal, severity, confidence,
             triage_category, message, evidence_json, triage_outcome,
             triage_note, detector_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report.run_id,
            report.scanned_runs,
            f.signal.value,
            f.severity.value,
            f.confidence,
            f.triage.value,
            f.message,
            json.dumps([e.__dict__ if hasattr(e, '__dict__') else e
                        for e in f.evidence]),
            f.triage_outcome,
            f.triage_note,
            report.detector_version,
        ))
        row_ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return row_ids


def record_triage(row_id: int, outcome: str, note: str = "") -> None:
    """Record a triage decision for a gap finding (kill-criterion metric).

    Args:
        row_id: the capability_gaps row id.
        outcome: "accepted" | "rejected" | "deferred".
        note: optional free-text rationale.
    """
    from datetime import datetime
    _ensure_table()
    conn = _get_conn()
    conn.execute("""
        UPDATE capability_gaps
        SET triage_outcome = ?, triage_note = ?, triage_at = ?
        WHERE id = ?
    """, (outcome, note, datetime.now().isoformat(), row_id))
    conn.commit()
    conn.close()


def compute_triage_yield(window: int = 10) -> dict:
    """Compute the triage-yield kill-criterion metric.

    Metric: >= 1 accepted (actionable) finding per `window` reports over any
    `window`-report window. Returns {accepted, total, yield_rate, passes}.

    Source: capability_gaps grouped by the report batch (created_at proximity
    is approximated by id ordering — each persist call creates a contiguous
    batch of rows for one report).
    """
    _ensure_table()
    conn = _get_conn()
    total_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM capability_gaps"
    ).fetchone()["n"]
    accepted_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM capability_gaps WHERE triage_outcome = 'accepted'"
    ).fetchone()["n"]
    conn.close()
    # Report count = distinct created_at batches. Approximate by counting
    # rows where the prior row has a different run_id or is the first row.
    # Simpler: count distinct (run_id, created_at) pairs.
    conn = _get_conn()
    report_batches = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(run_id, '__hist__') || ':' || "
        "SUBSTR(created_at, 1, 19)) AS n FROM capability_gaps"
    ).fetchone()["n"]
    conn.close()
    total = report_batches or total_rows  # fallback
    yield_rate = (accepted_rows / total) if total else 0.0
    # Kill criterion: >= 1 accepted per `window` reports.
    passes = yield_rate >= (1.0 / window)
    return {
        "accepted": accepted_rows,
        "total_reports": total,
        "yield_rate": round(yield_rate, 4),
        "window": window,
        "passes": passes,
    }


def get_gap_history(run_id: str | None = None) -> list[dict]:
    """Return gap rows, optionally scoped to a run."""
    _ensure_table()
    conn = _get_conn()
    if run_id:
        rows = conn.execute(
            "SELECT * FROM capability_gaps WHERE run_id = ? ORDER BY id DESC",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM capability_gaps ORDER BY id DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
