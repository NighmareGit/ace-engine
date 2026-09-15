"""Tests for engine.workflows.ralph.report_types — RoundReport + JSON schema."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from engine.workflows.ralph.report_types import (
    RoundReport, TaskSummary, GateVerdict, GateResult, IdeationSummary,
    PatternProposal, BudgetSnapshot,
    MAX_OBJECTIVE, MAX_PLAN, MAX_TASKS_EXECUTED, MAX_PATTERN_PROPOSALS,
    MAX_SERIALIZED,
)


def _make_report(**overrides) -> RoundReport:
    """Build a minimal valid RoundReport, with optional overrides."""
    base = dict(
        round_id=1,
        objective="implement a cache layer",
        plan="use an LRU dict with a max size",
        tasks_executed=[TaskSummary(task_id="T1", title="add cache", state="completed")],
        gate_verdict=GateVerdict(overall_pass=True, gates=[
            GateResult(gate="dev", passed=True),
            GateResult(gate="review", passed=True, model="qwen3.6-35b"),
        ]),
        ideation_summary=IdeationSummary(candidates_considered=5, selected_idx=2),
        pattern_proposals=[],
        budget_consumed=BudgetSnapshot(llm_calls=3, total_tokens=1500, wall_clock_s=12.5),
        workspace_sha="abc1234",
        state="completed",
    )
    base.update(overrides)
    return RoundReport(**base)


class TestRoundReportConstruction:
    def test_minimal_construction(self):
        r = _make_report()
        assert r.round_id == 1
        assert r.objective == "implement a cache layer"
        assert r.state == "completed"

    def test_objective_truncated(self):
        """S5 #6: objective > 4096 chars is truncated, not rejected."""
        big = "x" * 10000
        r = _make_report(objective=big)
        assert len(r.objective) == MAX_OBJECTIVE

    def test_plan_truncated(self):
        """S5 #6: plan > 16384 chars is truncated."""
        big = "y" * 50000
        r = _make_report(plan=big)
        assert len(r.plan) == MAX_PLAN

    def test_error_truncated(self):
        big = "z" * 10000
        r = _make_report(error=big)
        assert len(r.error) == 4096

    def test_tasks_bounded(self):
        """tasks_executed capped at 32."""
        tasks = [TaskSummary(task_id=f"T{i}", title=f"task {i}", state="completed") for i in range(50)]
        r = _make_report(tasks_executed=tasks)
        assert len(r.tasks_executed) == MAX_TASKS_EXECUTED

    def test_pattern_proposals_bounded(self):
        """pattern_proposals capped at 8."""
        pps = [PatternProposal(pattern_id=f"p{i}", title=f"pat{i}", body="b") for i in range(15)]
        r = _make_report(pattern_proposals=pps)
        assert len(r.pattern_proposals) == MAX_PATTERN_PROPOSALS


class TestRoundReportJson:
    def test_to_json_roundtrip(self):
        r = _make_report()
        raw = r.to_json()
        parsed = RoundReport.from_json(raw)
        assert parsed.round_id == 1
        assert parsed.objective == "implement a cache layer"
        assert parsed.state == "completed"
        assert parsed.gate_verdict.overall_pass is True
        assert len(parsed.gate_verdict.gates) == 2

    def test_serialized_within_64kb(self):
        """to_json output must be ≤64KB."""
        r = _make_report()
        raw = r.to_json()
        assert len(raw.encode("utf-8")) <= MAX_SERIALIZED

    def test_64kb_bound_with_huge_fields(self):
        """Even with massive fields, serialized output ≤64KB."""
        big_plan = "p" * 100000
        big_obj = "o" * 100000
        r = _make_report(plan=big_plan, objective=big_obj)
        raw = r.to_json()
        assert len(raw.encode("utf-8")) <= MAX_SERIALIZED

    def test_from_json_invalid_json(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            RoundReport.from_json("not json {{{")

    def test_from_json_missing_required(self):
        with pytest.raises(ValueError, match="missing required field"):
            RoundReport.from_json(json.dumps({"round_id": 1}))

    def test_from_json_bad_state(self):
        with pytest.raises(ValueError, match="state must be one of"):
            RoundReport.from_json(json.dumps({
                "round_id": 1, "objective": "o", "plan": "p", "state": "banana",
            }))

    def test_from_json_bad_sha(self):
        with pytest.raises(ValueError, match="workspace_sha is not a valid git SHA"):
            RoundReport.from_json(json.dumps({
                "round_id": 1, "objective": "o", "plan": "p", "state": "completed",
                "workspace_sha": "not-sha!",
            }))

    def test_from_json_truncates_strings(self):
        """from_json applies maxLength truncation (S5 #6)."""
        raw = json.dumps({
            "round_id": 1,
            "objective": "x" * 10000,
            "plan": "y" * 50000,
            "state": "completed",
            "ideation_summary": {
                "candidates_considered": 5,
                "selected_idx": 0,
                "selection_reason": "z" * 5000,
            },
        })
        r = RoundReport.from_json(raw)
        assert len(r.objective) == MAX_OBJECTIVE
        assert len(r.plan) == MAX_PLAN
        assert len(r.ideation_summary.selection_reason) == 2048

    def test_gate_verdict_model_preserved(self):
        """R4: judge model identity is preserved in gate results."""
        r = _make_report()
        raw = r.to_json()
        parsed = RoundReport.from_json(raw)
        review_gates = [g for g in parsed.gate_verdict.gates if g.gate == "review"]
        assert len(review_gates) == 1
        assert review_gates[0].model == "qwen3.6-35b"

    def test_to_dict_structure(self):
        r = _make_report()
        d = r.to_dict()
        assert "round_id" in d
        assert "gate_verdict" in d
        assert isinstance(d["gate_verdict"]["gates"], list)

    def test_empty_error_is_none(self):
        r = _make_report(error=None)
        assert r.error is None
