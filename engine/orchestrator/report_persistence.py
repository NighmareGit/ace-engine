"""Final report — persistence layer (T7c).

Persists a RunReport to:
  * engine.db (orchestrator_reports table, additive) for machine consumption
  * a markdown file under run/<run_id>/report.md for human reading

No branch writes, no network. Best-effort: persistence failures must never
fail the run.
"""

import json
import os

from engine.state import _get_conn


def _ensure_table():
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        success INTEGER NOT NULL,
        state TEXT NOT NULL,
        prd_path TEXT,
        project_path TEXT,
        wall_clock_s REAL,
        total_tokens INTEGER,
        budget_json TEXT,
        task_outcomes_json TEXT,
        replan_history_json TEXT,
        checkpoint_sha TEXT,
        stop_reason TEXT,
        ac_checklist_json TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()


def persist_report(report) -> int:
    """Persist the report to engine.db. Returns row id."""
    _ensure_table()
    conn = _get_conn()
    cur = conn.execute("""
        INSERT INTO orchestrator_reports
        (run_id, success, state, prd_path, project_path, wall_clock_s,
         total_tokens, budget_json, task_outcomes_json, replan_history_json,
         checkpoint_sha, stop_reason, ac_checklist_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        report.run_id,
        1 if report.success else 0,
        report.state,
        report.prd_path,
        report.project_path,
        report.wall_clock_s,
        report.total_tokens,
        json.dumps(report.budget),
        json.dumps([t.__dict__ if hasattr(t, '__dict__') else t
                    for t in report.task_outcomes]),
        json.dumps(report.replan_history),
        report.checkpoint_sha,
        report.stop_reason,
        json.dumps([c.__dict__ if hasattr(c, '__dict__') else c
                    for c in report.ac_checklist]),
    ))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def get_report_history(run_id: str) -> list[dict]:
    """Return report rows for a run."""
    _ensure_table()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM orchestrator_reports WHERE run_id = ? ORDER BY id DESC",
        (run_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def write_report_markdown(report, run_dir: str) -> str:
    """Write the report as report.md under run_dir. Returns the file path."""
    from engine.orchestrator.report_renderer import render_report_markdown
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "report.md")
    with open(path, "w") as f:
        f.write(render_report_markdown(report))
    return path
