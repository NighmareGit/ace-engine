"""D26 escalation JSONL projection — wiring + handler tests.

Covers the consumer-side projection of re-plan exhaustion, budget aborts,
and oracle escalations to ``engine_run_<run_id>_escalations.jsonl``
(docs/specs/RLM-TICKET-MAPPING-v1.md decision 4 / D26).

Two layers:
  1. Handler-level: the EscalationJsonlHandler translates on_event calls into
     JSONL records (tier mapping, ordering, no-file-when-no-events, error
     swallowing, callback chaining). Fast and deterministic.
  2. Engine-level: the Engine wires the handler and emits the new events, so a
     real budget-abort run writes a JSONL line end-to-end.
"""

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.adapters.escalation_jsonl import (
    EscalationJsonlHandler,
    escalation_jsonl_handler,
)
from engine.adapters.rlm_tickets import escalation_event
from engine.engine import Engine


# ---------------------------------------------------------------------------
# Layer 1 — handler unit tests
# ---------------------------------------------------------------------------


class TestHandlerNoFileWhenNoEvents:
    """No escalation events -> no JSONL file is created."""

    def test_no_file_when_only_non_escalation_events(self, tmp_path):
        h = EscalationJsonlHandler(tmp_path)
        h("task_escalation", {"run_id": "r1", "task_id": "T01", "stage": "validation"})
        h("judge_error", {"run_id": "r1", "task_id": "T01", "error": "x"})
        assert not (tmp_path / "engine_run_r1_escalations.jsonl").exists()

    def test_no_file_when_handler_never_called(self, tmp_path):
        EscalationJsonlHandler(tmp_path)
        assert not list(tmp_path.glob("*_escalations.jsonl"))


class TestHandlerTierMapping:
    """Each escalation kind maps to the correct tier_from / tier_to."""

    def test_replan_exhausted_is_35b_to_none(self, tmp_path):
        """Honesty (D26): replan_exhausted records the LADDER FACT — tier_to is
        'none' because this event itself escalates nowhere. An actual oracle
        escalation emits its own oracle_escalation event (35b -> oracle)."""
        h = EscalationJsonlHandler(tmp_path)
        h("replan_exhausted", {
            "run_id": "r1", "task_id": "T01", "attempt": 3,
            "outcome": "exhausted", "reason": "re-plan ladder exhausted"})
        lines = (tmp_path / "engine_run_r1_escalations.jsonl").read_text().splitlines()
        rec = json.loads(lines[0])
        assert rec["tier_from"] == "35b"
        assert rec["tier_to"] == "none"
        assert rec["outcome"] == "exhausted"
        assert rec["ticket_id"] == "T01"

    def test_budget_abort_is_9b_to_35b(self, tmp_path):
        h = EscalationJsonlHandler(tmp_path)
        h("budget_abort", {
            "run_id": "r1", "task_id": "T02", "attempt": 1,
            "outcome": "budget_cancelled", "reason": "budget cap: llm_calls 4 >= max_llm_calls 4"})
        lines = (tmp_path / "engine_run_r1_escalations.jsonl").read_text().splitlines()
        rec = json.loads(lines[0])
        assert rec["tier_from"] == "9b"
        assert rec["tier_to"] == "35b"
        assert rec["ticket_id"] == "T02"

    def test_oracle_escalation_is_35b_to_oracle(self, tmp_path):
        h = EscalationJsonlHandler(tmp_path)
        h("oracle_escalation", {
            "run_id": "r1", "task_id": "T03", "attempt": 2,
            "outcome": "escalated", "reason": "oracle digest produced 5 atoms"})
        lines = (tmp_path / "engine_run_r1_escalations.jsonl").read_text().splitlines()
        rec = json.loads(lines[0])
        assert rec["tier_from"] == "35b"
        assert rec["tier_to"] == "oracle"


class TestHandlerOrdering:
    """Multiple escalation events append in emission order."""

    def test_multiple_events_append_in_order(self, tmp_path):
        h = EscalationJsonlHandler(tmp_path)
        h("budget_abort", {"run_id": "r", "task_id": "T01", "attempt": 1,
                           "outcome": "budget_cancelled", "reason": "cap"})
        h("replan_exhausted", {"run_id": "r", "task_id": "T01", "attempt": 3,
                               "outcome": "exhausted", "reason": "replans"})
        h("oracle_escalation", {"run_id": "r", "task_id": "T01", "attempt": 4,
                                "outcome": "escalated", "reason": "atoms"})
        lines = (tmp_path / "engine_run_r_escalations.jsonl").read_text().splitlines()
        assert len(lines) == 3
        assert [json.loads(l)["tier_from"] for l in lines] == ["9b", "35b", "35b"]
        assert [json.loads(l)["tier_to"] for l in lines] == ["35b", "none", "oracle"]
        assert [json.loads(l)["ticket_id"] for l in lines] == ["T01", "T01", "T01"]


class TestHandlerChaining:
    """The handler forwards events to the chained callback."""

    def test_chained_callback_receives_events(self, tmp_path):
        seen = []
        h = EscalationJsonlHandler(tmp_path, chain=lambda et, d: seen.append((et, d)))
        h("budget_abort", {"run_id": "r", "task_id": "T01", "attempt": 1,
                           "outcome": "budget_cancelled", "reason": "cap"})
        # escalation forwarded
        assert seen[0][0] == "budget_abort"
        # non-escalation forwarded too
        h("judge_error", {"run_id": "r", "task_id": "T01", "error": "x"})
        assert seen[1] == ("judge_error", {"run_id": "r", "task_id": "T01", "error": "x"})


class TestHandlerRobustness:
    """Projection failures must never propagate (emit_event swallow-contract)."""

    def test_bad_event_data_does_not_raise(self, tmp_path):
        h = EscalationJsonlHandler(tmp_path)
        # Missing fields -> .get() defaults; must not raise.
        h("budget_abort", {})
        h("replan_exhausted", {"run_id": None, "task_id": None})

    def test_chained_callback_failure_does_not_break_projection(self, tmp_path):
        def bad_chain(et, d):
            raise RuntimeError("callback exploded")
        h = EscalationJsonlHandler(tmp_path, chain=bad_chain)
        # Must not raise despite the bad chain.
        h("budget_abort", {"run_id": "r", "task_id": "T01", "attempt": 1,
                           "outcome": "budget_cancelled", "reason": "cap"})
        lines = (tmp_path / "engine_run_r_escalations.jsonl").read_text().splitlines()
        assert len(lines) == 1


class TestFactory:
    """The factory returns a working handler."""

    def test_factory_returns_callable_handler(self, tmp_path):
        h = escalation_jsonl_handler(tmp_path)
        assert isinstance(h, EscalationJsonlHandler)
        h("oracle_escalation", {"run_id": "r", "task_id": "T01", "attempt": 1,
                                "outcome": "escalated", "reason": "x"})
        assert (tmp_path / "engine_run_r_escalations.jsonl").exists()


# ---------------------------------------------------------------------------
# Layer 2 — engine-level integration: budget abort writes JSONL end-to-end
# ---------------------------------------------------------------------------


class _BudgetBurnerTransport:
    """Produces valid code but counts LLM calls so a low budget cap aborts."""

    def __init__(self):
        self.calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        return {"content": "```python\nx = 1\n```", "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def _run_engine(transport, config, prd_path="/tmp/prd.md", project_path="/tmp/proj-escalation"):
    import engine.engine as eng_mod
    import engine.committer as cm
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py")]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    try:
        eng = Engine(transport=transport, config=config)
        result = eng.run(prd_path, project_path)
        return result, eng
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


class TestEngineBudgetAbortWritesJsonl:
    """A real budget-abort run projects a JSONL line via the wired handler."""

    def test_budget_abort_writes_jsonl_line(self, tmp_path):
        project_path = str(tmp_path / "project")
        os.makedirs(project_path, exist_ok=True)
        # Tiny call budget: the first LLM call pushes us over -> budget_abort.
        cfg = EngineConfig(max_llm_calls=1, max_retries_generate=1)
        transport = _BudgetBurnerTransport()
        result, eng = _run_engine(transport, cfg, project_path=project_path)
        # The handler writes to <project>/run/<run_id>/engine_run_<run_id>_escalations.jsonl
        run_dir = os.path.join(project_path, "run", eng.pipeline.run_id)
        jsonl = os.path.join(run_dir, f"engine_run_{eng.pipeline.run_id}_escalations.jsonl")
        assert os.path.exists(jsonl), f"expected JSONL at {jsonl}"
        lines = open(jsonl).read().splitlines()
        # At least one budget_abort line.
        recs = [json.loads(l) for l in lines]
        assert any(r["tier_from"] == "9b" and r["tier_to"] == "35b" for r in recs), \
            f"expected a 9b->35b budget-abort record, got: {recs}"
