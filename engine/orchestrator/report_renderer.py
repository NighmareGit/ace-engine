"""Final report — renderer layer (T7b).

Renders a RunReport to human-readable markdown. No network, no DB — pure
string rendering. The persistence layer writes the output to disk/engine.db.
"""

from engine.orchestrator.report_types import RunReport


def _fmt_bool(b: bool) -> str:
    return "PASS" if b else "FAIL"


def render_report_markdown(report: RunReport) -> str:
    """Render a RunReport to markdown."""
    lines = []
    lines.append(f"# Run Report: {report.run_id}")
    lines.append("")
    lines.append(f"- **State**: {report.state}")
    lines.append(f"- **Success**: {_fmt_bool(report.success)}")
    lines.append(f"- **PRD**: `{report.prd_path}`")
    lines.append(f"- **Project**: `{report.project_path}`")
    lines.append(f"- **Wall clock**: {report.wall_clock_s:.2f}s")
    lines.append(f"- **Total tokens**: {report.total_tokens}")
    lines.append("")

    # Budget section.
    lines.append("## Budget")
    lines.append("")
    b = report.budget
    if b:
        for key, val in b.items():
            lines.append(f"- `{key}`: {val}")
    else:
        lines.append("(no budget caps configured)")
    lines.append("")

    # Checkpoint.
    lines.append("## Checkpoint")
    lines.append("")
    lines.append(f"- **Last pushed SHA**: `{report.checkpoint_sha or '(none)'}`")
    lines.append("")

    # Stop reason.
    if report.stop_reason:
        lines.append("## Stop Reason")
        lines.append("")
        lines.append(f"> {report.stop_reason}")
        lines.append("")

    # Task outcomes.
    lines.append("## Task Outcomes")
    lines.append("")
    lines.append("| Task | State | Attempts | Commit | Tokens | Error |")
    lines.append("|------|-------|----------|--------|--------|-------|")
    for t in report.task_outcomes:
        err = (t.error or "")[:60]
        lines.append(f"| {t.task_id} | {t.state} | {t.attempts} | "
                     f"`{t.commit_sha or '-'}` | {t.tokens} | {err} |")
    lines.append("")

    # Re-plan history.
    lines.append("## Re-plan History")
    lines.append("")
    if report.replan_history:
        for i, r in enumerate(report.replan_history, 1):
            lines.append(f"### Re-plan {i}")
            lines.append("")
            lines.append(f"- **Task**: {r.get('task_id', '?')}")
            lines.append(f"- **Validated**: {_fmt_bool(bool(r.get('validated')))}")
            lines.append(f"- **Applied**: {_fmt_bool(bool(r.get('applied')))}")
            if r.get("reason"):
                lines.append(f"- **Reason**: {r['reason']}")
            if r.get("error"):
                lines.append(f"- **Error**: {r['error']}")
            lines.append("")
    else:
        lines.append("(no re-plans attempted)")
        lines.append("")

    # AC checklist (PRD gate — mechanical).
    lines.append("## Acceptance Criteria Checklist")
    lines.append("")
    if report.ac_checklist:
        lines.append("| Task | Criterion | Met |")
        lines.append("|------|-----------|-----|")
        for c in report.ac_checklist:
            mark = "PASS" if c.met else "FAIL"
            lines.append(f"| {c.task_id} | {c.criterion} | {mark} |")
    else:
        lines.append("(no acceptance criteria defined)")
    lines.append("")

    return "\n".join(lines)
