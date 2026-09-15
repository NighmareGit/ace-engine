"""Tests for engine.workflows.ralph.config — RalphConfig dataclass."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from engine.workflows.ralph.config import RalphConfig


class TestRalphConfig:
    def test_defaults_match_epic(self):
        """All defaults match the EPIC's config table."""
        c = RalphConfig()
        assert c.max_ralph_rounds == 5
        assert c.max_round_retries == 3
        assert c.ideation_candidates == 5
        assert c.ideation_model_port == 8082
        assert c.review_model_port == 8080
        assert c.redteam_model_port == 8080
        assert c.redteam_escalate_to_oracle is False
        assert c.pattern_approval_required is True
        assert c.max_pattern_length_bytes == 8192
        assert c.round_timeout_s == 1800

    def test_g12_defaults(self):
        c = RalphConfig()
        assert c.g12_similarity_threshold == 0.75
        assert c.g12_consecutive_rounds == 2

    def test_budget_defaults(self):
        """S9 FIX #2: budget cap fields exist on RalphConfig with sane defaults."""
        c = RalphConfig()
        assert c.max_llm_calls == 40
        assert c.max_total_tokens == 2_000_000
        assert c.max_wall_clock_s == 1800

    def test_ideation_max_clamped(self):
        """ideation_candidates > 8 is clamped to 8."""
        c = RalphConfig(ideation_candidates=20)
        assert c.ideation_candidates == 8

    def test_ideation_min_clamped(self):
        """ideation_candidates < 1 is clamped to 1."""
        c = RalphConfig(ideation_candidates=0)
        assert c.ideation_candidates == 1

    def test_max_rounds_min_clamped(self):
        c = RalphConfig(max_ralph_rounds=0)
        assert c.max_ralph_rounds == 1

    def test_threshold_bounds(self):
        c = RalphConfig(g12_similarity_threshold=1.5)
        assert c.g12_similarity_threshold == 1.0
        c2 = RalphConfig(g12_similarity_threshold=-0.5)
        assert c2.g12_similarity_threshold == 0.0

    def test_workspace_denylist_default_empty(self):
        c = RalphConfig()
        assert c.workspace_denylist == []

    def test_workspace_denylist_custom(self):
        c = RalphConfig(workspace_denylist=[".scratch", "tmp"])
        assert c.workspace_denylist == [".scratch", "tmp"]

    def test_max_patterns_per_run(self):
        c = RalphConfig()
        assert c.max_patterns_per_run == 8

    def test_enable_memory_recall_flag_propagates_to_ideation(self):
        """M2 regression: enable_memory_recall set on RalphConfig is the
        value ideation.reads via getattr(config, 'enable_memory_recall', False).

        Before the fix, cmd_ralph_run never copied the engine flag into
        RalphConfig, so recall_for_objective() was never called regardless of
        the CLI flag.  This test asserts the flag reaches ideation's gate.
        """
        from engine.workflows.ralph.ideation import generate_candidate_plans

        # Flag ON → recall_for_objective must be invoked (captured below).
        cfg_on = RalphConfig(enable_memory_recall=True)
        assert getattr(cfg_on, "enable_memory_recall", False) is True

        # Flag OFF (default) → recall must NOT fire.
        cfg_off = RalphConfig()
        assert getattr(cfg_off, "enable_memory_recall", False) is False

        # Drive generate_candidate_plans and capture whether recall fired.
        recall_called = {"count": 0, "rendered": None}

        class _FakeRecallResult:
            rendered = ""

        def _fake_recall(objective):
            recall_called["count"] += 1
            return _FakeRecallResult()

        # Patch the recall import inside ideation.  The import is
        # `from engine.memory.recall import recall_for_objective`, so we
        # patch it on engine.memory.recall.
        from engine.memory import recall as recall_mod
        orig = getattr(recall_mod, "recall_for_objective", None)
        recall_mod.recall_for_objective = _fake_recall
        try:
            generate_candidate_plans(
                objective="test objective",
                previous_rounds=[],
                n_candidates=2,
                config=cfg_on,
                transport=None,
            )
            assert recall_called["count"] == 1, (
                "enable_memory_recall=True must trigger recall_for_objective()")
            recall_called["count"] = 0
            generate_candidate_plans(
                objective="test objective",
                previous_rounds=[],
                n_candidates=2,
                config=cfg_off,
                transport=None,
            )
            assert recall_called["count"] == 0, (
                "enable_memory_recall=False must NOT trigger recall_for_objective()")
        finally:
            if orig is not None:
                recall_mod.recall_for_objective = orig

    def test_cmd_ralph_run_copies_memory_flag(self):
        """M2 regression: cmd_ralph_run must propagate --enable-memory-recall
        into RalphConfig so the flag reaches ideation."""
        import argparse

        from engine.cli import cmd_ralph_run

        args = argparse.Namespace(
            objective="test",
            project="/tmp/nonexistent",
            max_rounds=1,
            enable_memory_recall=True,
        )
        # run_ralph will fail on a nonexistent project, but we can intercept
        # RalphConfig construction by patching run_ralph.
        captured = {}

        def _fake_run_ralph(*, objective, project_path, config, **kw):
            captured["config"] = config
            from engine.workflows.ralph.round_driver import RalphResult
            return RalphResult(ralph_run_id="x", objective=objective)

        # run_ralph is imported lazily inside cmd_ralph_run via
        # `from engine.workflows.ralph import run_ralph, RalphConfig`;
        # patch it on the ralph package before calling the command.
        from engine.workflows import ralph as ralph_pkg
        orig = getattr(ralph_pkg, "run_ralph", None)
        ralph_pkg.run_ralph = _fake_run_ralph
        try:
            cmd_ralph_run(args)
        finally:
            if orig is not None:
                ralph_pkg.run_ralph = orig
        assert "config" in captured, "run_ralph was not called"
        assert getattr(captured["config"], "enable_memory_recall", False) is True, (
            "cmd_ralph_run must copy --enable-memory-recall into RalphConfig")
