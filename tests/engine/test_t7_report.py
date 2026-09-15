"""T7 tests: final report + PRD gate.

On run DONE/CANCELLED/FAILED: emit a run summary report to engine.db + a
markdown file under run/<run_id>/report.md (no branch writes). Report covers:
task outcomes, budget consumed vs caps, trigger/re-plan history, checkpoint
state, stop reason. PRD gate: AC checklist section driven by task definitions'
acceptance criteria — mechanical checkmarks only, no LLM judgment.

Three-layer split: report types / renderer runtime / no network.
"""

import os
import sys
import json

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# (a) Report types
# ---------------------------------------------------------------------------

def test_run_report_dataclass():
    from engine.orchestrator.report_types import RunReport, TaskOutcome
    t = TaskOutcome(task_id="T01", title="T", state="COMMIT", attempts=1,
                    commit_sha="abc", tokens=100, error=None)
    r = RunReport(
        run_id="run-1", success=True, state="DONE",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=10.5, total_tokens=500,
        budget={"llm_calls": 3, "max_llm_calls": 0},
        task_outcomes=[t],
        replan_history=[{"task_id": "T01", "validated": True, "applied": True}],
        checkpoint_sha="abc",
        stop_reason=None,
        ac_checklist=[{"task_id": "T01", "criterion": "must pass", "met": True}],
    )
    assert r.run_id == "run-1"
    assert len(r.task_outcomes) == 1


def test_run_report_to_dict():
    from engine.orchestrator.report_types import RunReport, TaskOutcome
    r = RunReport(
        run_id="run-1", success=True, state="DONE",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=1.0, total_tokens=100,
        budget={}, task_outcomes=[],
        replan_history=[], checkpoint_sha=None,
        stop_reason=None, ac_checklist=[],
    )
    d = r.to_dict()
    assert d["run_id"] == "run-1"
    assert "task_outcomes" in d
    assert "budget" in d
    assert "ac_checklist" in d


# ---------------------------------------------------------------------------
# (b) Renderer — markdown output
# ---------------------------------------------------------------------------

def test_renderer_produces_markdown():
    from engine.orchestrator.report_types import (RunReport, TaskOutcome,
                                                  ACChecklistItem)
    from engine.orchestrator.report_renderer import render_report_markdown
    r = RunReport(
        run_id="run-1", success=True, state="DONE",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=5.0, total_tokens=500,
        budget={"llm_calls": 3, "max_llm_calls": 0, "total_tokens": 500,
                "max_total_tokens": 0},
        task_outcomes=[
            TaskOutcome("T01", "Create API", "COMMIT", 1, "abc", 200, None),
            TaskOutcome("T02", "Add tests", "COMMIT", 2, "def", 300, None),
        ],
        replan_history=[],
        checkpoint_sha="abc",
        stop_reason=None,
        ac_checklist=[
            ACChecklistItem("T01", "must pass tests", True),
            ACChecklistItem("T02", "must cover edge cases", True),
        ],
    )
    md = render_report_markdown(r)
    assert "# Run Report: run-1" in md
    assert "DONE" in md
    assert "T01" in md
    assert "T02" in md
    assert "abc" in md  # checkpoint sha
    assert "must pass tests" in md
    assert "AC Checklist" in md or "Acceptance Criteria" in md


def test_renderer_includes_stop_reason_on_cancel():
    from engine.orchestrator.report_types import RunReport, ACChecklistItem
    from engine.orchestrator.report_renderer import render_report_markdown
    r = RunReport(
        run_id="run-2", success=False, state="CANCELLED",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=1.0, total_tokens=100,
        budget={}, task_outcomes=[], replan_history=[],
        checkpoint_sha=None, stop_reason="budget cap: llm_calls 10 >= 10",
        ac_checklist=[],
    )
    md = render_report_markdown(r)
    assert "CANCELLED" in md
    assert "budget cap" in md


def test_renderer_includes_replan_history():
    from engine.orchestrator.report_types import RunReport, ACChecklistItem
    from engine.orchestrator.report_renderer import render_report_markdown
    r = RunReport(
        run_id="run-3", success=False, state="FAILED",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=1.0, total_tokens=100,
        budget={}, task_outcomes=[], replan_history=[
            {"task_id": "T01", "validated": True, "applied": True,
             "reason": "flask missing"},
            {"task_id": "T01", "validated": False, "applied": False,
             "error": "code-like token found"},
        ],
        checkpoint_sha=None, stop_reason=None, ac_checklist=[],
    )
    md = render_report_markdown(r)
    assert "Re-plan History" in md or "replan" in md.lower()
    assert "flask missing" in md


# ---------------------------------------------------------------------------
# (c) Report builder — assembles a RunReport from engine state
# ---------------------------------------------------------------------------

def test_build_run_report_from_engine_state(tmp_db):
    """build_run_report assembles a complete report from the engine result
    + orchestrator session state."""
    from engine import Task
    from engine.engine import RunResult, TaskResult
    from engine.orchestrator import report_builder as rb
    from engine.orchestrator import session as sess

    # Seed session state.
    sess.create_session("run-1", ["T01", "T02"])
    sess.update_task_state("run-1", "T01", "DONE")
    sess.update_task_state("run-1", "T02", "DONE")
    sess.record_push("run-1", "sha-abc")
    sess.record_replan("run-1", "T01", ("validation", "syntax"),
                       "repeated syntax", "raw", True, True, None, 100)

    tasks = [
        Task(id="T01", title="Create API", description="d", module="api.py",
             acceptance_criteria=["must pass tests"]),
        Task(id="T02", title="Add tests", description="d", module="test.py",
             acceptance_criteria=["must cover edge cases"]),
    ]
    result = RunResult(
        run_id="run-1", success=True, prd_path="/tmp/prd.md",
        project_path="/tmp/proj", total_time_s=5.0,
        tasks=[
            TaskResult("T01", "Create API", "COMMIT", 1, "sha-abc", 1.0, 200,
                       error_message=None),
            TaskResult("T02", "Add tests", "COMMIT", 1, "sha-def", 1.0, 300,
                       error_message=None),
        ],
        states_visited=[], total_tokens=500, error_message=None,
    )
    report = rb.build_run_report(result, tasks, budget_max_llm_calls=0,
                                 budget_max_total_tokens=0,
                                 budget_max_wall_clock_s=0)
    assert report.run_id == "run-1"
    assert report.success is True
    assert report.checkpoint_sha == "sha-abc"
    assert len(report.task_outcomes) == 2
    assert len(report.replan_history) == 1
    # AC checklist derived from task acceptance_criteria.
    assert len(report.ac_checklist) >= 2


# ---------------------------------------------------------------------------
# Persistence: engine.db + markdown file
# ---------------------------------------------------------------------------

def test_report_persisted_to_db(tmp_db):
    from engine.orchestrator.report_types import RunReport
    from engine.orchestrator import report_persistence as rp
    r = RunReport(
        run_id="run-1", success=True, state="DONE",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=1.0, total_tokens=100,
        budget={}, task_outcomes=[], replan_history=[],
        checkpoint_sha="abc", stop_reason=None, ac_checklist=[],
    )
    rp.persist_report(r)
    rows = rp.get_report_history("run-1")
    assert len(rows) == 1
    assert rows[0]["success"] == 1


def test_report_written_to_markdown_file(tmp_path):
    from engine.orchestrator.report_types import RunReport
    from engine.orchestrator import report_persistence as rp
    r = RunReport(
        run_id="run-1", success=True, state="DONE",
        prd_path="/tmp/prd.md", project_path="/tmp/proj",
        wall_clock_s=1.0, total_tokens=100,
        budget={}, task_outcomes=[], replan_history=[],
        checkpoint_sha="abc", stop_reason=None, ac_checklist=[],
    )
    run_dir = str(tmp_path / "run" / "run-1")
    rp.write_report_markdown(r, run_dir)
    report_file = os.path.join(run_dir, "report.md")
    assert os.path.exists(report_file)
    content = open(report_file).read()
    assert "run-1" in content
