"""RLM ticket adapter — projects RLM Engine rev-2 tickets onto ace-engine Tasks.

The consumer-side half of the joint schema contract v1:
- schema:  schemas/rlm-ticket-v1.schema.json   (mirrored in both repos)
- mapping: docs/specs/RLM-TICKET-MAPPING-v1.md  (mirrored in both repos)

The RLM engine core stays consumer-agnostic (its D1); ALL format churn is
absorbed here. Bumps to the contract require paired changes in both repos.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# competence -> task_type (closed mapping; original preserved in metadata)
_COMPETENCE_TO_TYPE = {
    "implement": "implementation",
    "compile": "implementation",
    "test": "test",
    "attack": "test",
    "investigate": "debug",
    "explore": "implementation",
    "research": "implementation",
    "plan": "implementation",
    "review": "implementation",
}

_COMPLEXITY_TO_PRIORITY = {
    "critical": 1,
    "high": 1,
    "medium": 2,
    "low": 3,
}

_ESCALATION_SCHEMA_VERSION = 1

# Required-field set from schemas/rlm-ticket-v1.schema.json — keep in lockstep
# (a schema bump without this update fails the lockstep test).
_REQUIRED_FIELDS = frozenset([
    "id", "epic_id", "title", "description", "skill", "competence", "files",
    "tests", "dependencies", "deliverable", "contract", "complexity",
    "recommended_model", "confidence", "metadata",
])


@dataclass
class RlmProjection:
    """Result of projecting RLM tickets, with contract-reported anomalies."""

    task_dicts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _require(ticket: dict, key: str) -> object:
    if key not in ticket or ticket[key] is None:
        raise ValueError(f"rlm ticket missing required field: {key}")
    return ticket[key]


def rlm_ticket_to_task_dict(ticket: dict) -> dict:
    """Project one RLM rev-2 ticket dict onto an ace-engine task dict.

    Raises ValueError on contract violations (missing required fields,
    unknown competence, empty contract.success_criteria). The caller's
    pipeline treats a projection failure like any other task-parse
    failure — it never silently drops a ticket.
    """
    tid = str(_require(ticket, "id"))
    missing = {k for k in _REQUIRED_FIELDS if ticket.get(k) is None}
    if missing:
        raise ValueError(f"rlm ticket {tid}: missing required fields: {sorted(missing)}")
    competence = str(_require(ticket, "competence"))
    if competence not in _COMPETENCE_TO_TYPE:
        raise ValueError(f"rlm ticket {tid}: unknown competence {competence!r}")
    contract = _require(ticket, "contract")
    criteria = list((contract or {}).get("success_criteria") or [])
    if not criteria:
        raise ValueError(f"rlm ticket {tid}: contract.success_criteria is empty")
    files = list(_require(ticket, "files") or [])
    if not files:
        raise ValueError(f"rlm ticket {tid}: files is empty")
    paths = [str(f.get("path", "")).strip() for f in files if f.get("path")]
    if not paths:
        raise ValueError(f"rlm ticket {tid}: no file paths resolvable")

    description = str(_require(ticket, "description"))
    eats = [str(e) for e in (ticket.get("eats") or [])]
    prd_section = description
    if eats:
        prd_section += "\nEvidence inputs:\n" + "\n".join(f"- {e}" for e in eats)

    complexity = str(_require(ticket, "complexity"))
    if complexity not in _COMPLEXITY_TO_PRIORITY:
        raise ValueError(f"rlm ticket {tid}: unknown complexity {complexity!r}")

    return {
        "id": tid,
        "title": str(_require(ticket, "title")),
        "description": description,
        "module": paths[0],
        "dependencies": [str(d) for d in (ticket.get("dependencies") or [])],
        "task_type": _COMPETENCE_TO_TYPE[competence],
        "priority": _COMPLEXITY_TO_PRIORITY[complexity],
        "prd_section": prd_section,
        "files": paths,
        "acceptance_criteria": criteria,
        "metadata": {
            "rlm": {
                "skill": ticket.get("skill", ""),
                "competence": competence,
                "recommended_model": ticket.get("recommended_model", ""),
                "confidence": ticket.get("confidence", 0.0),
                "deliverable": str(_require(ticket, "deliverable")),
                "tests": list(ticket.get("tests") or []),
                "eats": eats,
                "mission_intent": ticket.get("mission_intent"),
                "metadata": dict(ticket.get("metadata") or {}),
            }
        },
    }


def is_witness(ticket_id: str) -> bool:
    """Witness tickets (RLM appends one W### per epic) dispatch as tests."""
    return "W" in ticket_id.rsplit("-", 1)[-1][:1] or ticket_id.rsplit("-", 1)[-1].startswith("W")


def project_rlm_tickets(tickets: list[dict]) -> RlmProjection:
    """Project a full ticket set; witness tickets normalize to task_type=test.

    Contract violations do NOT abort the batch — they are surfaced as
    warnings and the offending ticket is skipped, so one malformed ticket
    cannot wedge an epic. Callers decide policy off `warnings`.
    """
    out = RlmProjection()
    for ticket in tickets:
        try:
            d = rlm_ticket_to_task_dict(ticket)
        except ValueError as exc:
            out.warnings.append(str(exc))
            continue
        if is_witness(d["id"]) and d["task_type"] != "test":
            d["task_type"] = "test"
        out.task_dicts.append(d)
    return out


def load_rlm_tickets_dir(ticket_dir: str | Path) -> RlmProjection:
    """Load per-ticket T###.json files from an RLM emit directory."""
    root = Path(ticket_dir)
    tickets: list[dict] = []
    for path in sorted(root.glob("*.json")):
        if path.name in ("graph.json",):
            continue
        try:
            tickets.append(json.loads(path.read_text()))
        except json.JSONDecodeError as exc:
            tickets_err = f"rlm ticket file {path.name}: invalid JSON ({exc})"
            # surfaced via the projection's warnings below
            tickets.append({"id": path.stem, "_invalid": tickets_err})
    proj = project_rlm_tickets([t for t in tickets if not t.get("_invalid")])
    proj.warnings.extend(str(t["_invalid"]) for t in tickets if t.get("_invalid"))
    return proj


def escalation_event(
    run_id: str,
    ticket_id: str,
    tier_from: str,
    tier_to: str,
    attempt: int,
    outcome: str,
    reason: str,
) -> dict:
    """One escalation record, aligned to the RLM observe execution-record
    schema (PRE-EPIC §12.3). The consumer appends these as JSONL to
    engine_run_<run_id>_escalations.jsonl in the run directory.
    """
    return {
        "schema_version": _ESCALATION_SCHEMA_VERSION,
        "run_id": run_id,
        "ticket_id": ticket_id,
        "tier_from": tier_from,
        "tier_to": tier_to,
        "attempt": attempt,
        "outcome": outcome,
        "reason": reason,
    }


def append_escalation(run_dir: str | Path, event: dict) -> None:
    """Append one escalation event as a JSONL line (atomic line append)."""
    path = Path(run_dir) / f"engine_run_{event['run_id']}_escalations.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
