"""Autopsy CLI — read-only failure reconstruction over engine.db + run dir.

``ace autopsy <run_id> [task_id]`` prints a structured 7-section report that
answers "why did task X fail" in one command.  Implemented as a new
``engine/autopsy.py`` (extending the read-only philosophy of
``engine/forensics.py``).

Output sections:
1. Run header (from engine_runs)
2. Task summary (from engine_task_results)
3. Session log timeline (from session_logs)
4. Validation stage trace (from engine_task_results.validation_result)
5. Gate verdicts (from judge_verdicts + engine_scores)
6. Re-plan history (from run dir escalations.jsonl + session_logs)
7. Divergence check (calls forensics.bisect)

Read-only: all sources are SELECT from engine.db or read of run-dir files.
No writes. No task mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Optional


def _get_conn(db_path: str = "engine.db") -> sqlite3.Connection:
    """Get a read-only connection (WAL mode for concurrent safety)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _db_path() -> str:
    """Resolve the active engine DB path."""
    return os.environ.get("ENGINE_DB_PATH") or "engine.db"


def _run_dir(run_id: str) -> Optional[str]:
    """Resolve the run directory for a given run_id.

    Checks common locations: ./run/<run_id>, ../ace-engine-ralph-ws/run/<run_id>.
    """
    candidates = [
        os.path.join("run", run_id),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ace-engine-ralph-ws", "run", run_id),
        os.path.join(os.path.expanduser("~"), "projects", "ace-engine-ralph-ws",
                     "run", run_id),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


# ── Section formatters ────────────────────────────────────────────────────────

def _section_header(title: str) -> str:
    """Format a section header."""
    bar = "=" * 60
    return f"\n{bar}\n## {title}\n{bar}"


def _fmt_run_header(conn, run_id: str) -> str:
    """Section 1: Run header."""
    row = conn.execute("SELECT * FROM engine_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return _section_header("Run Header") + f"\n[run {run_id} not found in engine_runs]"
    d = dict(row)
    lines = [
        f"run_id:       {d.get('id')}",
        f"prd_path:     {d.get('prd_path')}",
        f"project_path: {d.get('project_path')}",
        f"state:        {d.get('state')}",
        f"started_at:   {d.get('started_at')}",
        f"completed_at: {d.get('completed_at')}",
    ]
    # Count tasks.
    task_rows = conn.execute(
        "SELECT COUNT(*) FROM engine_task_results WHERE run_id = ?", (run_id,)
    ).fetchone()
    failed_rows = conn.execute(
        "SELECT COUNT(*) FROM engine_task_results WHERE run_id = ? AND state = 'FAILED'",
        (run_id,)
    ).fetchone()
    lines.append(f"total tasks:  {task_rows[0]}")
    lines.append(f"failed tasks: {failed_rows[0]}")
    return _section_header("Run Header") + "\n" + "\n".join(lines)


def _fmt_task_summary(conn, run_id: str) -> str:
    """Section 2: Task summary."""
    rows = conn.execute(
        """\
        SELECT task_id, state, attempts, commit_sha, error_message
        FROM engine_task_results
        WHERE run_id = ?
        ORDER BY task_id
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return _section_header("Task Summary") + "\n[no tasks found]"
    lines = []
    for r in rows:
        d = dict(r)
        line = f"  {d['task_id']}: state={d['state']}, attempts={d['attempts']}"
        if d.get("commit_sha"):
            line += f", commit={d['commit_sha'][:8]}"
        if d.get("error_message"):
            line += f"\n    error: {d['error_message'][:200]}"
        lines.append(line)
    return _section_header("Task Summary") + "\n" + "\n".join(lines)


def _fmt_session_log_timeline(conn, run_id: str, task_id: Optional[str] = None) -> str:
    """Section 3: Session log timeline."""
    if task_id:
        rows = conn.execute(
            """\
            SELECT attempt, stage, model, port, prompt_hash, prompt_truncated,
                   response_content, response_reasoning, finish_reason,
                   prompt_tokens, completion_tokens, total_tokens, thinking_tokens,
                   latency_ms, exhausted, error
            FROM session_logs
            WHERE run_id = ? AND task_id = ?
            ORDER BY created_at
            """,
            (run_id, task_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """\
            SELECT task_id, attempt, stage, model, port, prompt_hash,
                   prompt_truncated, response_content, response_reasoning,
                   finish_reason, prompt_tokens, completion_tokens, total_tokens,
                   thinking_tokens, latency_ms, exhausted, error
            FROM session_logs
            WHERE run_id = ?
            ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()
    if not rows:
        return _section_header("Session Log Timeline") + "\n[no session logs captured for this run]"
    lines = []
    for r in rows:
        d = dict(r)
        tid = d.get("task_id", task_id or "?")
        exhausted_marker = " ⚠ EXHAUSTED" if d.get("exhausted") else ""
        lines.append(
            f"[attempt {d['attempt']}] {d['stage']} | "
            f"model={d['model']} | port={d['port']} | "
            f"{d['latency_ms'] / 1000:.1f}s | {d['total_tokens']} toks | "
            f"finish={d['finish_reason']}{exhausted_marker}"
        )
        lines.append(f"  prompt_hash={d['prompt_hash'][:12]} "
                     f"(truncated: {d['prompt_truncated'][:80]!r}...)")
        resp = d.get("response_content", "") or ""
        lines.append(f"  response: {resp[:500]!r}")
        reasoning = d.get("response_reasoning", "") or ""
        if reasoning:
            lines.append(f"  reasoning: {reasoning[:500]!r}")
        if d.get("error"):
            lines.append(f"  ERROR: {d['error']}")
    return _section_header("Session Log Timeline") + "\n" + "\n".join(lines)


def _fmt_validation_trace(conn, run_id: str, task_id: Optional[str] = None) -> str:
    """Section 4: Validation stage trace."""
    if task_id:
        rows = conn.execute(
            """\
            SELECT task_id, validation_result, attempts
            FROM engine_task_results
            WHERE run_id = ? AND task_id = ? AND validation_result IS NOT NULL
            ORDER BY task_id
            """,
            (run_id, task_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """\
            SELECT task_id, validation_result, attempts
            FROM engine_task_results
            WHERE run_id = ? AND validation_result IS NOT NULL
            ORDER BY task_id
            """,
            (run_id,),
        ).fetchall()
    if not rows:
        return _section_header("Validation Stage Trace") + "\n[no validation results found]"
    lines = []
    for r in rows:
        d = dict(r)
        try:
            vr = json.loads(d["validation_result"])
        except (json.JSONDecodeError, TypeError):
            vr = {"raw": d["validation_result"]}
        # vr is typically a dict with "stages" list or "passed" bool.
        stages = vr.get("stages", [])
        if not stages and not vr.get("passed", True):
            stages = [{"stage": "unknown", "passed": False, "error": vr.get("error", "")}]
        for stage in stages:
            status = "PASS" if stage.get("passed") else "FAIL"
            line = f"  {d['task_id']}: stage={stage.get('stage', '?')} [{status}]"
            if stage.get("error"):
                line += f"\n    error: {stage['error'][:300]}"
            if stage.get("file"):
                line += f"\n    file: {stage['file']}"
            lines.append(line)
    return _section_header("Validation Stage Trace") + "\n" + "\n".join(lines)


def _fmt_gate_verdicts(conn, run_id: str) -> str:
    """Section 5: Gate verdicts."""
    rows = conn.execute(
        """\
        SELECT task_id, overall, completeness, correctness, quality,
               intelligence, role_fit, scored_state
        FROM engine_scores
        WHERE run_id = ?
        ORDER BY task_id
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return _section_header("Gate Verdicts") + "\n[no judge scores found]"
    lines = []
    for r in rows:
        d = dict(r)
        overall = d.get("overall")
        overall_str = f"{overall:.1f}" if overall is not None else "?"
        lines.append(
            f"  {d['task_id']}: overall={overall_str} "
            f"(C={d['completeness']} Cr={d['correctness']} Q={d['quality']} "
            f"I={d['intelligence']} R={d['role_fit']}) "
            f"scored_state={d.get('scored_state', '?')}"
        )
    return _section_header("Gate Verdicts") + "\n" + "\n".join(lines)


def _fmt_replan_history(conn, run_id: str, task_id: Optional[str] = None) -> str:
    """Section 6: Re-plan history."""
    # From session_logs where stage='replan'.
    if task_id:
        rows = conn.execute(
            """\
            SELECT attempt, stage, response_content, prompt_truncated
            FROM session_logs
            WHERE run_id = ? AND task_id = ? AND stage = 'replan'
            ORDER BY created_at
            """,
            (run_id, task_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """\
            SELECT task_id, attempt, stage, response_content, prompt_truncated
            FROM session_logs
            WHERE run_id = ? AND stage = 'replan'
            ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()
    # Also check escalations.jsonl sidecar.
    escalations = []
    run_dir = _run_dir(run_id)
    if run_dir:
        esc_path = os.path.join(run_dir, f"engine_run_{run_id}_escalations.jsonl")
        try:
            with open(esc_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        escalations.append(json.loads(line))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    if not rows and not escalations:
        return _section_header("Re-plan History") + "\n[no re-plans found]"
    lines = []
    for r in rows:
        d = dict(r)
        tid = d.get("task_id", task_id or "?")
        resp = (d.get("response_content", "") or "")[:300]
        lines.append(f"  re-plan (session_log) — {tid} attempt {d['attempt']}: {resp}")
    for esc in escalations:
        if task_id and esc.get("ticket_id") != task_id:
            continue
        lines.append(
            f"  re-plan (escalation) — {esc.get('ticket_id', '?')}: "
            f"outcome={esc.get('outcome')}, reason={esc.get('reason', '')[:200]}"
        )
    return _section_header("Re-plan History") + "\n" + "\n".join(lines)


def _fmt_divergence_check(run_id: str, db_path: str) -> str:
    """Section 7: Divergence check (calls forensics.bisect)."""
    from engine.forensics import bisect
    result = bisect(run_id, db_path)
    report = _bisect_report(result)
    return _section_header("Divergence Check") + "\n" + report


def _bisect_report(result: dict) -> str:
    """Format a bisect result."""
    if result["diverged"]:
        lines = [
            f"DIVERGENCE FOUND at atom {result['atom_id']} (seq {result['seq']})",
            f"Atoms before divergence: {result['atoms_before_divergence']}",
        ]
        return "\n".join(lines)
    return f"ALL ATOMS MATCH — trace is deterministic ({result['atom_count']} atoms verified)"


# ── Main autopsy function ────────────────────────────────────────────────────

def autopsy(run_id: str, task_id: Optional[str] = None, full: bool = False,
            db_path: Optional[str] = None) -> str:
    """Run the full autopsy and return the report string.

    Args:
        run_id: The engine run ID.
        task_id: Optional task ID to deep-dive.
        full: If True, include full prompt/response payloads from sidecar.
        db_path: Override DB path.

    Returns:
        Multi-line report string.
    """
    start = time.time()
    db = db_path or _db_path()
    conn = _get_conn(db)
    try:
        sections = [
            _fmt_run_header(conn, run_id),
            _fmt_task_summary(conn, run_id),
            _fmt_session_log_timeline(conn, run_id, task_id),
            _fmt_validation_trace(conn, run_id, task_id),
            _fmt_gate_verdicts(conn, run_id),
            _fmt_replan_history(conn, run_id, task_id),
            _fmt_divergence_check(run_id, db),
        ]
        # If --full, include sidecar payloads.
        if full:
            run_dir = _run_dir(run_id)
            if run_dir:
                sidecar = os.path.join(run_dir, f"engine_run_{run_id}_session_payloads.jsonl")
                try:
                    with open(sidecar) as f:
                        payload_lines = f.readlines()
                    sections.append(
                        _section_header("Full Sidecar Payloads")
                        + f"\n{len(payload_lines)} payload(s) in {sidecar}"
                    )
                except FileNotFoundError:
                    sections.append(
                        _section_header("Full Sidecar Payloads")
                        + "\n[no sidecar found]"
                    )
        elapsed = time.time() - start
        sections.append(f"\n[autopsy completed in {elapsed:.2f}s]")
        return "\n".join(sections)
    finally:
        conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python3 engine/autopsy.py``."""
    parser = argparse.ArgumentParser(
        description="Read-only failure reconstruction over engine.db + run dir."
    )
    parser.add_argument("run", help="Run ID to autopsy")
    parser.add_argument("task", nargs="?", default=None,
                        help="Optional task ID to deep-dive")
    parser.add_argument("--full", action="store_true",
                        help="Include full prompt/response payloads from sidecar")
    parser.add_argument("--db", default=None,
                        help="Database path (default: engine.db or ENGINE_DB_PATH)")
    args = parser.parse_args(argv)

    report = autopsy(args.run, args.task, full=args.full, db_path=args.db)
    print(report)


if __name__ == "__main__":
    main()
