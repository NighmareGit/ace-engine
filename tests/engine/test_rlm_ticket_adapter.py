"""Tests for the RLM ticket adapter — the consumer half of contract v1.

Gates on the frozen agreement artifacts:
- schemas/rlm-ticket-v1.schema.json
- docs/specs/RLM-TICKET-MAPPING-v1.md
"""

import json

import pytest

from engine.adapters.rlm_tickets import (
    append_escalation,
    escalation_event,
    is_witness,
    load_rlm_tickets_dir,
    project_rlm_tickets,
    rlm_ticket_to_task_dict,
)


def make_ticket(**over):
    t = {
        "id": "EPIC-001-T001",
        "epic_id": "EPIC-001",
        "title": "Build parser",
        "description": "Implement the ticket parser module.",
        "skill": "python-implement",
        "competence": "implement",
        "files": [{"path": "rlm_engine/parser.py", "action": "create"}],
        "tests": ["python -m pytest tests/test_parser.py -q"],
        "dependencies": [],
        "deliverable": "rlm_engine/parser.py",
        "contract": {"success_criteria": ["parser emits valid tickets", "0-LLM validate"]},
        "complexity": "medium",
        "recommended_model": "tier-36b",
        "confidence": 0.8,
        "metadata": {
            "route_reason": "medium+implement",
            "skill_why": "pure python module",
            "est_tokens": 4000,
            "est_minutes": 20,
        },
    }
    t.update(over)
    return t


class TestMapping:
    def test_full_projection(self):
        d = rlm_ticket_to_task_dict(make_ticket())
        assert d["id"] == "EPIC-001-T001"
        assert d["module"] == "rlm_engine/parser.py"
        assert d["files"] == ["rlm_engine/parser.py"]
        assert d["task_type"] == "implementation"
        assert d["priority"] == 2
        assert d["acceptance_criteria"] == ["parser emits valid tickets", "0-LLM validate"]
        assert d["metadata"]["rlm"]["deliverable"] == "rlm_engine/parser.py"
        assert d["metadata"]["rlm"]["competence"] == "implement"

    def test_long_epic_id_accepted_verbatim(self):
        d = rlm_ticket_to_task_dict(make_ticket(id="EPIC-042-T013"))
        assert d["id"] == "EPIC-042-T013"

    def test_competence_closed_mapping(self):
        expect = {
            "implement": "implementation", "compile": "implementation",
            "test": "test", "attack": "test", "investigate": "debug",
            "explore": "implementation", "research": "implementation",
            "plan": "implementation", "review": "implementation",
        }
        for comp, ttype in expect.items():
            assert rlm_ticket_to_task_dict(make_ticket(competence=comp))["task_type"] == ttype

    def test_unknown_competence_rejected(self):
        with pytest.raises(ValueError, match="unknown competence"):
            rlm_ticket_to_task_dict(make_ticket(competence="vibes"))

    def test_priority_from_complexity(self):
        assert rlm_ticket_to_task_dict(make_ticket(complexity="critical"))["priority"] == 1
        assert rlm_ticket_to_task_dict(make_ticket(complexity="high"))["priority"] == 1
        assert rlm_ticket_to_task_dict(make_ticket(complexity="low"))["priority"] == 3

    def test_prd_section_synthesizes_eats(self):
        d = rlm_ticket_to_task_dict(make_ticket(eats=["docs/spec.md", "graph.json"]))
        assert "Evidence inputs:" in d["prd_section"]
        assert "- docs/spec.md" in d["prd_section"]

    def test_rlm_fields_round_trip_in_metadata(self):
        t = make_ticket(mission_intent={"root_goal_hash": "abc", "invariants": ["x"]})
        d = rlm_ticket_to_task_dict(t)
        rlm = d["metadata"]["rlm"]
        assert rlm["skill"] == "python-implement"
        assert rlm["recommended_model"] == "tier-36b"
        assert rlm["mission_intent"]["root_goal_hash"] == "abc"
        assert rlm["metadata"]["route_reason"] == "medium+implement"

    def test_deliverable_required(self):
        t = make_ticket()
        del t["deliverable"]
        with pytest.raises(ValueError, match="deliverable"):
            rlm_ticket_to_task_dict(t)

    def test_empty_success_criteria_rejected(self):
        with pytest.raises(ValueError, match="success_criteria"):
            rlm_ticket_to_task_dict(make_ticket(contract={"success_criteria": []}))

    def test_missing_required_field_rejected(self):
        t = make_ticket()
        del t["epic_id"]
        with pytest.raises(ValueError, match="epic_id"):
            rlm_ticket_to_task_dict(t)


class TestWitness:
    def test_witness_detected_and_normalized(self):
        proj = project_rlm_tickets([make_ticket(id="EPIC-001-W001", competence="implement")])
        assert len(proj.task_dicts) == 1
        assert proj.task_dicts[0]["task_type"] == "test"

    def test_is_witness(self):
        assert is_witness("EPIC-001-W001")
        assert not is_witness("EPIC-001-T001")


class TestBatch:
    def test_malformed_ticket_does_not_wedge_batch(self):
        proj = project_rlm_tickets(
            [make_ticket(), make_ticket(id="EPIC-001-T002", competence="nonsense")]
        )
        assert len(proj.task_dicts) == 1
        assert len(proj.warnings) == 1
        assert "EPIC-001-T002" in proj.warnings[0]

    def test_load_dir_skips_graph_and_reports_bad_json(self, tmp_path):
        (tmp_path / "graph.json").write_text('{"nodes": []}')
        (tmp_path / "EPIC-001-T001.json").write_text(json.dumps(make_ticket()))
        (tmp_path / "EPIC-001-T002.json").write_text("{not json")
        proj = load_rlm_tickets_dir(tmp_path)
        assert [d["id"] for d in proj.task_dicts] == ["EPIC-001-T001"]
        assert any("EPIC-001-T002" in w for w in proj.warnings)


class TestEscalation:
    def test_event_shape(self):
        e = escalation_event("run-1", "EPIC-001-T001", "9b", "35b", 2, "ok", "exhaustion")
        assert e["schema_version"] == 1
        assert e["tier_from"] == "9b" and e["tier_to"] == "35b"
        assert e["attempt"] == 2 and e["outcome"] == "ok"

    def test_jsonl_append(self, tmp_path):
        e = escalation_event("run-1", "T01", "9b", "35b", 1, "fail", "budget")
        append_escalation(tmp_path, e)
        append_escalation(tmp_path, escalation_event("run-1", "T02", "35b", "oracle", 1, "ok", "x"))
        lines = (tmp_path / "engine_run_run-1_escalations.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["ticket_id"] == "T02"
