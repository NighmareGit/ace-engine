"""Tool-Gap Detector (TGD) — report renderer (ORCH-7).

Renders a GapReport as:
  * JSON (machine consumption, the structured gap report)
  * Markdown (human reading, appended to the run report)

Mechanical only — no LLM, no network.
"""

import json

from engine.orchestrator.toolgap_types import GapReport, SignalId, GATED_SIGNALS


def render_gap_report_json(report: GapReport) -> str:
    """Render the gap report as a JSON string."""
    return json.dumps(report.to_dict(), indent=2)


def render_gap_report_markdown(report: GapReport) -> str:
    """Render the gap report as a markdown section."""
    lines = []
    header = "# Tool-Gap Detector Report"
    if report.run_id:
        header += f" — {report.run_id}"
    lines.append(header)
    lines.append("")
    lines.append(f"- **Scanned runs:** {report.scanned_runs}")
    lines.append(f"- **Findings:** {len(report.findings)}")
    lines.append(f"- **Generated:** {report.generated_at}")
    lines.append(f"- **Detector version:** {report.detector_version}")
    lines.append("")

    if not report.findings:
        lines.append("_No tool-gap signals detected._")
        lines.append("")
    else:
        # Summary table.
        lines.append("## Summary")
        lines.append("")
        lines.append("| Signal | Severity | Confidence | Triage | Message |")
        lines.append("|--------|----------|------------|--------|---------|")
        for f in report.findings:
            lines.append(
                f"| {f.signal.value} | {f.severity.value} | {f.confidence:.2f} "
                f"| {f.triage.value} | {f.message[:80]} |"
            )
        lines.append("")

        # Detail per finding.
        lines.append("## Findings")
        lines.append("")
        for i, f in enumerate(report.findings, 1):
            lines.append(f"### {i}. {f.signal.value} — {f.triage.value}")
            lines.append("")
            lines.append(f"- **Severity:** {f.severity.value}")
            lines.append(f"- **Confidence:** {f.confidence:.2f}")
            lines.append(f"- **Message:** {f.message}")
            if f.triage_outcome:
                lines.append(f"- **Triage outcome:** {f.triage_outcome}")
            if f.triage_note:
                lines.append(f"- **Triage note:** {f.triage_note}")
            lines.append("")
            lines.append("**Evidence:**")
            lines.append("")
            lines.append("| run_id | task_id | signature | count | detail |")
            lines.append("|--------|---------|-----------|-------|--------|")
            for e in f.evidence[:20]:
                detail = (e.detail or "").replace("|", "\\|").replace("\n", " ")[:80]
                lines.append(
                    f"| {e.run_id} | {e.task_id or ''} | {e.signature} "
                    f"| {e.count} | {detail} |"
                )
            if len(f.evidence) > 20:
                lines.append(f"| ... | | | | ({len(f.evidence) - 20} more rows) |")
            lines.append("")

    # Gated signals note.
    gated = [s.value for s in GATED_SIGNALS]
    lines.append("## Gated signals")
    lines.append("")
    lines.append(
        f"Signals {', '.join(gated)} are IIL/skills-gated and degrade to "
        f"no-op until the corresponding machinery ships. They appear in the "
        f"catalogue for completeness but produce no findings pre-IIL."
    )
    lines.append("")
    return "\n".join(lines)
