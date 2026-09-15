"""Escalation JSONL projection — wires engine escalation events to a JSONL file.

The consumer half of decision D26 (docs/specs/RLM-TICKET-MAPPING-v1.md):
re-plan exhaustion, budget aborts, and oracle escalations are projected to
``engine_run_<run_id>_escalations.jsonl`` in the run directory, one JSON object
per line, aligned to the RLM observe execution-record schema (PRE-EPIC §12.3).

The projection is a small callable handler that wraps an optional upstream
``on_event`` callback: the engine calls the handler with ``(event_type, data)``;
the handler both appends the JSONL record (when the event is an escalation)
and forwards the event to the chained callback. This keeps the engine's event
surface decoupled from the consumer's persistence choice (D1): the engine emits
plain ``on_event`` calls, the adapter writes JSONL.

Tier names follow the engine's model_config strings (intent/types.py):
subject code-gen is the ``9b`` tier, the re-plan/oracle judge is ``35b``,
and the oracle is ``oracle``. The escalation ladder is 9b -> 35b -> oracle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from engine.adapters.rlm_tickets import append_escalation, escalation_event

# Event types that the engine emits for escalation situations (see
# Engine._run_task / budget_mod in engine/engine.py). The handler projects
# exactly these; other on_event calls are forwarded unchanged.
_REPLAN_EXHAUSTED = "replan_exhausted"
_BUDGET_ABORT = "budget_abort"
_ORACLE_ESCALATION = "oracle_escalation"

# tier_from / tier_to per escalation kind. The ladder is 9b -> 35b -> oracle;
# a budget abort stops the run at the running (9b subject) tier, so it points at
# the next tier (35b). Re-plan exhaustion sits at the 35b rung with NO further
# escalation taken by this event itself — tier_to is "none" (honesty: the event
# records the ladder fact, not an escalation that may never happen). An actual
# oracle escalation emits its own oracle_escalation event (35b -> oracle).
_TIER_TRANSITIONS: dict[str, tuple[str, str]] = {
    _REPLAN_EXHAUSTED: ("35b", "none"),
    _BUDGET_ABORT: ("9b", "35b"),
    _ORACLE_ESCALATION: ("35b", "oracle"),
}


class EscalationJsonlHandler:
    """An ``on_event`` callback that projects escalation events to a JSONL file.

    Instantiate with the run directory (``project_path/run/<run_id>``); the
    handler writes ``engine_run_<run_id>_escalations.jsonl`` there. Forwarding
    to an optional ``chain`` callback lets the handler wrap an existing
    ``on_event`` without the engine knowing about the projection.
    """

    def __init__(self, run_dir: str | Path, chain: Optional[Callable] = None):
        self.run_dir = Path(run_dir)
        self.chain = chain

    def __call__(self, event_type: str, data: dict) -> None:
        # Project escalation events to JSONL (best-effort: persistence must
        # never break the pipeline, mirroring emit_event's swallow-contract).
        transition = _TIER_TRANSITIONS.get(event_type)
        if transition is not None:
            try:
                tier_from, tier_to = transition
                event = escalation_event(
                    run_id=data.get("run_id", ""),
                    ticket_id=data.get("task_id", ""),
                    tier_from=tier_from,
                    tier_to=tier_to,
                    attempt=int(data.get("attempt", data.get("attempts", 0))),
                    outcome=data.get("outcome", ""),
                    reason=data.get("reason", ""),
                )
                # Ensure the run directory exists before appending (it may not
                # yet — e.g. a budget abort on the first task fires before the
                # report builder creates <project>/run/<run_id>).
                self.run_dir.mkdir(parents=True, exist_ok=True)
                append_escalation(self.run_dir, event)
            except Exception:
                pass  # projection failure must never crash the engine

        # Forward to the chained callback (the operator's original on_event).
        # Swallow failures here too: the projection must not break the
        # pipeline, and a broken operator callback must not break the
        # projection (emit_event swallow-contract on both paths).
        if self.chain is not None:
            try:
                self.chain(event_type, data)
            except Exception:
                pass


def escalation_jsonl_handler(
    run_dir: str | Path, chain: Optional[Callable] = None
) -> EscalationJsonlHandler:
    """Factory returning an on_event callback that projects escalations to JSONL.

    Convenience wrapper around :class:`EscalationJsonlHandler` for call sites
    that prefer a factory call over the class.
    """
    return EscalationJsonlHandler(run_dir, chain=chain)
