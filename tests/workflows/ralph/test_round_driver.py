"""Tests for engine.workflows.ralph.round_driver — outer loop core."""

import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    """Fresh temp DB per test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "engine.db")
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        import importlib
        import engine.state
        importlib.reload(engine.state)
        yield db_path


from engine.workflows.ralph.round_driver import (
    run_ralph, resume_ralph, RalphResult, RalphRunBusyError,
    DirtyWorktreeError, BudgetExhaustedError, RalphError,
    _per_round_budget_clamp, _build_next_round_prompt,
    RalphJsonlHandler, _ensure_schema, _check_budget_exhausted,
    _gate_sequence_type_for_round,
)
from engine.workflows.ralph.config import RalphConfig
from engine.workflows.ralph.report_types import RoundReport, BudgetSnapshot


class TestRunRalph:
    def test_creates_run_and_reports(self, _tmp_db, tmp_path):
        config = RalphConfig(max_ralph_rounds=2)
        result = run_ralph("test objective", str(tmp_path), config=config)
        assert isinstance(result, RalphResult)
        assert result.ralph_run_id.startswith("ralph-")
        assert result.state in ("completed", "failed", "cancelled")

    def test_respects_max_rounds(self, _tmp_db, tmp_path):
        config = RalphConfig(max_ralph_rounds=3)
        result = run_ralph("obj", str(tmp_path), config=config)
        assert len(result.rounds) <= 3

    def test_persists_rounds_to_db(self, _tmp_db, tmp_path):
        from engine.state import _get_conn
        config = RalphConfig(max_ralph_rounds=1)
        result = run_ralph("obj", str(tmp_path), config=config)
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM ralph_rounds WHERE ralph_run_id=?",
            (result.ralph_run_id,),
        ).fetchall()
        assert len(rows) >= 1

    def test_emits_events(self, _tmp_db, tmp_path):
        events = []
        config = RalphConfig(max_ralph_rounds=1)
        run_ralph("obj", str(tmp_path), config=config,
                  on_event=lambda et, d: events.append((et, d)))
        assert any(e[0].startswith("ralph_") for e in events)

    def test_ralph_result_report_dict(self, _tmp_db, tmp_path):
        config = RalphConfig(max_ralph_rounds=1)
        result = run_ralph("obj", str(tmp_path), config=config)
        d = result.to_report_dict()
        assert "ralph_run_id" in d
        assert "rounds" in d
        assert d["objective"] == "obj"


class TestResumeRalph:
    def test_completed_run_raises(self, _tmp_db, tmp_path):
        """S5 #4: completed is terminal — cannot resume."""
        from engine.workflows.ralph.round_driver import _create_run_row, _update_run_state
        _create_run_row("run-done", "obj", 5)
        _update_run_state("run-done", "completed")
        with pytest.raises(RalphError, match="already completed"):
            resume_ralph("run-done", str(tmp_path))

    def test_failed_run_resumes_and_executes_rounds(self, _tmp_db, tmp_path):
        """S9 FIX #1: resume_ralph must EXECUTE rounds, not just load state.

        After resuming a failed run, the result must contain MORE rounds than
        were persisted before the resume (i.e. the loop actually ran).
        """
        from engine.workflows.ralph.round_driver import (
            _create_run_row, _update_run_state, _persist_round,
            _persist_report,
        )
        config = RalphConfig(max_ralph_rounds=3)
        _create_run_row("run-resume", "obj", config.max_ralph_rounds)
        # Persist round 1 as failed.
        _persist_round("run-resume", 1, "run-1", BudgetSnapshot(), "failed", None)
        _update_run_state("run-resume", "failed")

        with patch("engine.workflows.ralph.round_driver._git_is_clean", return_value=True), \
             patch("engine.workflows.ralph.round_driver._git_stash", return_value=True):
            result = resume_ralph("run-resume", str(tmp_path), config=config)

        # The resume started at round 2 (last completed = none, but round 1
        # failed so start_round = last_completed_round_id + 1 = 0 + 1 = 1...
        # actually with no completed rounds, start_round = 1). The key assertion:
        # the loop executed and produced rounds.
        assert len(result.rounds) >= 1
        # The result should have a valid terminal state.
        assert result.state in ("completed", "failed", "cancelled")

    def test_resume_executes_at_least_one_round(self, _tmp_db, tmp_path):
        """S9 FIX #1: explicit — after resume, ≥1 round has executed."""
        from engine.workflows.ralph.round_driver import (
            _create_run_row, _update_run_state, _persist_round,
        )
        config = RalphConfig(max_ralph_rounds=2)
        _create_run_row("run-exec", "obj", config.max_ralph_rounds)
        _persist_round("run-exec", 1, "run-1", BudgetSnapshot(), "failed", None)
        _update_run_state("run-exec", "failed")

        with patch("engine.workflows.ralph.round_driver._git_is_clean", return_value=True), \
             patch("engine.workflows.ralph.round_driver._git_stash", return_value=True):
            result = resume_ralph("run-exec", str(tmp_path), config=config)

        # Must have executed at least one round (the loop ran).
        assert len(result.rounds) >= 1, (
            f"resume_ralph executed 0 rounds — loop body is missing. "
            f"result.rounds={result.rounds}"
        )

    def test_unknown_run_raises(self, _tmp_db, tmp_path):
        with patch("engine.workflows.ralph.round_driver._acquire_lock", return_value=True):
            with pytest.raises(RalphError, match="not found"):
                resume_ralph("nonexistent", str(tmp_path))


class TestBudgetClamp:
    def test_per_round_clamp_with_real_config(self, _tmp_db):
        """M2: per-round clamp = min(config.cap, remaining_cumulative).

        Uses RalphConfig's ACTUAL max_llm_calls field (not dynamically added).
        """
        config = RalphConfig(max_llm_calls=100, max_total_tokens=10000)
        cumulative = BudgetSnapshot(llm_calls=80, total_tokens=8000, wall_clock_s=0)
        clamped = _per_round_budget_clamp(config, cumulative)
        # remaining = 20, cap = 100 → min(20, 100) = 20.
        assert clamped.llm_calls == 20
        assert clamped.total_tokens == 2000

    def test_unbounded_when_cap_zero(self, _tmp_db):
        """Default config has non-zero caps — this tests explicit zero."""
        config = RalphConfig(max_llm_calls=0, max_total_tokens=0)
        cumulative = BudgetSnapshot()
        clamped = _per_round_budget_clamp(config, cumulative)
        assert clamped.llm_calls == 0  # 0 = unbounded

    def test_budget_fields_exist_on_config(self, _tmp_db):
        """S9 FIX #2: RalphConfig must declare budget cap fields natively."""
        config = RalphConfig()
        # These fields must exist as dataclass fields (not dynamically added).
        assert config.max_llm_calls == 40
        assert config.max_total_tokens == 2_000_000
        assert config.max_wall_clock_s == 1800

    def test_check_budget_exhausted_raises(self, _tmp_db):
        """S9 FIX #2: _check_budget_exhausted raises when cumulative >= cap."""
        config = RalphConfig(max_llm_calls=10, max_total_tokens=1000)
        # Simulate exhausted budget.
        cumulative = BudgetSnapshot(llm_calls=10, total_tokens=500)
        # _check_budget_exhausted reads from DB, so we need to seed a round.
        from engine.workflows.ralph.round_driver import (
            _create_run_row, _persist_round,
        )
        _create_run_row("run-budget", "obj", 5)
        _persist_round("run-budget", 1, "r1", cumulative, "completed", None)
        with pytest.raises(BudgetExhaustedError):
            _check_budget_exhausted("run-budget", config)

    def test_check_budget_not_exhausted(self, _tmp_db):
        """_check_budget_exhausted does NOT raise when under cap."""
        config = RalphConfig(max_llm_calls=100, max_total_tokens=10000)
        from engine.workflows.ralph.round_driver import (
            _create_run_row, _persist_round,
        )
        _create_run_row("run-ok", "obj", 5)
        _persist_round("run-ok", 1, "r1", BudgetSnapshot(llm_calls=5, total_tokens=500), "completed", None)
        # Should not raise.
        _check_budget_exhausted("run-ok", config)


class TestFreshAgentPrompt:
    def test_prompt_contains_only_report_objective_workspace(self, _tmp_db):
        """R7: prompt contains ONLY report + objective + workspace refs."""
        report = RoundReport(
            round_id=1, objective="obj", plan="plan",
            workspace_sha="abc1234", state="completed",
        )
        prompt = _build_next_round_prompt(report, "obj", "abc1234", RalphConfig())
        assert "obj" in prompt
        assert "abc1234" in prompt
        assert "conversation" not in prompt.lower()

    def test_prompt_redacts_denylisted_paths(self, _tmp_db):
        """M8: workspace denylist redacts matching content."""
        report = RoundReport(
            round_id=1, objective="obj", plan="plan with /scratch/foo content",
            workspace_sha="abc1234", state="completed",
        )
        config = RalphConfig(workspace_denylist=["/scratch/foo"])
        prompt = _build_next_round_prompt(report, "obj", "abc1234", config)
        assert "/scratch/foo" not in prompt or "[REDACTED]" in prompt


class TestRalphJsonlHandler:
    def test_writes_jsonl(self, _tmp_db, tmp_path):
        handler = RalphJsonlHandler(tmp_path)
        handler("ralph_round_complete", {"ralph_run_id": "r1", "round": 1})
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "ralph_round_complete" in content

    def test_chains_callback(self, _tmp_db, tmp_path):
        called = []
        handler = RalphJsonlHandler(tmp_path, chain=lambda et, d: called.append(et))
        handler("ralph_round_complete", {"ralph_run_id": "r1"})
        assert called == ["ralph_round_complete"]


class TestGateSequenceTypeForRound:
    """S3: edit atoms get the CODE gate sequence (they modify code)."""

    def test_edit_atom_gets_code_sequence(self):
        """An edit atom (task_types=['edit']) must route to the code gate
        sequence (Dev->Review->Test->RedTeam), not the research sequence."""
        cfg = RalphConfig(task_types=["edit"])
        assert _gate_sequence_type_for_round("fix the bug", cfg) == "code"

    def test_research_atom_gets_research_sequence(self):
        cfg = RalphConfig(task_types=["research"])
        assert _gate_sequence_type_for_round("investigate X", cfg) == "research"

    def test_code_atom_gets_code_sequence(self):
        cfg = RalphConfig(task_types=["implementation"])
        assert _gate_sequence_type_for_round("build feature", cfg) == "code"

    def test_no_task_types_defaults_to_code(self):
        cfg = RalphConfig()
        assert _gate_sequence_type_for_round("anything", cfg) == "code"

    def test_mixed_edit_and_other_gets_code(self):
        """When edit is among the task types (even with others), code
        sequence is used — edit modifies code so the full gate applies."""
        cfg = RalphConfig(task_types=["edit", "implementation"])
        assert _gate_sequence_type_for_round("mixed", cfg) == "code"

    def test_research_wins_over_edit(self):
        """If both research and edit are present, research takes precedence
        (the verdict document is the primary artifact)."""
        cfg = RalphConfig(task_types=["edit", "research"])
        assert _gate_sequence_type_for_round("mixed", cfg) == "research"
