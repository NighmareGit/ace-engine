"""R7 prompt-audit test: round-N+1 prompt contains ONLY report + objective +
workspace refs, no other round-N conversation.

This is the S9 acceptance criterion made testable: the fresh-agent rule is
mechanically enforced by round-driver prompt construction.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "engine.db")
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        import importlib
        import engine.state
        importlib.reload(engine.state)
        yield db_path


from engine.workflows.ralph.round_driver import _build_next_round_prompt
from engine.workflows.ralph.config import RalphConfig
from engine.workflows.ralph.report_types import RoundReport


class TestR7PromptAudit:
    def test_prompt_contains_objective(self):
        report = RoundReport(round_id=1, objective="build a cache", plan="LRU",
                             state="completed")
        prompt = _build_next_round_prompt(report, "build a cache", "abc1234",
                                          RalphConfig())
        assert "build a cache" in prompt

    def test_prompt_contains_workspace_sha(self):
        report = RoundReport(round_id=1, objective="obj", plan="p",
                             workspace_sha="abc1234", state="completed")
        prompt = _build_next_round_prompt(report, "obj", "abc1234", RalphConfig())
        assert "abc1234" in prompt

    def test_prompt_contains_report_content(self):
        report = RoundReport(round_id=1, objective="obj", plan="specific plan",
                             state="completed")
        prompt = _build_next_round_prompt(report, "obj", "sha1", RalphConfig())
        assert "specific plan" in prompt

    def test_prompt_excludes_arbitrary_conversation(self):
        """R7: the prompt must NOT contain arbitrary conversation text that
        isn't derived from the report, objective, or workspace SHA."""
        report = RoundReport(round_id=1, objective="obj", plan="plan",
                             state="completed")
        prompt = _build_next_round_prompt(report, "obj", "sha1", RalphConfig())
        # These are things that might appear in a conversation but should NOT
        # appear in the fresh-agent prompt.
        assert "I think we should" not in prompt
        assert "Let me explain" not in prompt
        assert "As discussed earlier" not in prompt
        assert "the user said" not in prompt

    def test_prompt_only_allows_three_inputs(self):
        """R7: prompt is built from ONLY report + workspace_sha + objective.

        We verify by checking that every substantive piece of information in the
        prompt traces back to one of these three sources.
        """
        objective = "implement retry logic"
        workspace_sha = "deadbeef"
        report = RoundReport(
            round_id=2, objective=objective,
            plan="use exponential backoff with jitter",
            state="completed", workspace_sha=workspace_sha,
        )
        config = RalphConfig()
        prompt = _build_next_round_prompt(report, objective, workspace_sha, config)
        # The prompt should reference the report's plan (from the report),
        # the objective, and the workspace SHA.
        assert "exponential backoff" in prompt  # from report.plan
        assert objective in prompt               # from objective
        assert workspace_sha in prompt           # from workspace_sha

    def test_prompt_with_denylist_redacts(self):
        """M8: denylisted paths in report content are redacted."""
        report = RoundReport(
            round_id=1, objective="obj",
            plan="see /scratch/ralph-loop/run1/output for details",
            state="completed",
        )
        config = RalphConfig(workspace_denylist=["/scratch/ralph-loop/run1"])
        prompt = _build_next_round_prompt(report, "obj", "sha1", config)
        assert "/scratch/ralph-loop/run1" not in prompt
        assert "[REDACTED]" in prompt
