"""Tests for engine.workflows.ralph.ideation — bounded divergent ideation."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from engine.workflows.ralph.config import RalphConfig
from engine.workflows.ralph.ideation import (
    generate_candidate_plans, ScoredPlan, IdeationResult,
    _select, _detect_collapse, DEFAULT_N, MAX_N,
)
from engine.workflows.ralph.report_types import RoundReport, IdeationSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRound:
    """Minimal stand-in for a prior RoundReport."""
    def __init__(self, plan_text: str, selected_idx: int = 0):
        self.plan = plan_text
        self.ideation_summary = IdeationSummary(
            candidates_considered=5, selected_idx=selected_idx,
        )


# ---------------------------------------------------------------------------
# Bounded generation
# ---------------------------------------------------------------------------

class TestGenerateCandidatePlans:
    def test_default_n_candidates(self):
        result = generate_candidate_plans("build a cache", [])
        assert len(result.candidates) == DEFAULT_N

    def test_n_clamped_to_max(self):
        """n_candidates > 8 is clamped to 8."""
        result = generate_candidate_plans("build a cache", [], n_candidates=20)
        assert len(result.candidates) == MAX_N

    def test_n_clamped_to_min(self):
        result = generate_candidate_plans("build a cache", [], n_candidates=0)
        assert len(result.candidates) == 1

    def test_config_overrides_n(self):
        cfg = RalphConfig(ideation_candidates=3)
        result = generate_candidate_plans("build a cache", [], config=cfg)
        assert len(result.candidates) == 3

    def test_each_candidate_scored(self):
        result = generate_candidate_plans("build a cache", [])
        for c in result.candidates:
            assert 0.0 <= c.feasibility <= 10.0
            assert 0.0 <= c.alignment <= 10.0
            assert 0.0 <= c.novelty <= 10.0

    def test_selected_idx_in_range(self):
        result = generate_candidate_plans("build a cache", [])
        assert 0 <= result.selected_idx < len(result.candidates)

    def test_to_summary(self):
        result = generate_candidate_plans("build a cache", [])
        summary = result.to_summary()
        assert summary.candidates_considered == DEFAULT_N
        assert summary.selected_idx == result.selected_idx


# ---------------------------------------------------------------------------
# G12 collapse detection (S5 #8)
# ---------------------------------------------------------------------------

class TestG12Collapse:
    def test_no_collapse_on_dissimilar_plans(self):
        prev = [_FakeRound("implement a cache layer")]
        result = generate_candidate_plans(
            "build a cache", prev, n_candidates=3,
        )
        # The stub candidates are "candidate N: approach N for build a cache"
        # which should be dissimilar to "implement a cache layer".
        assert result.collapse_detected is False

    def test_collapse_on_similar_plans(self):
        """G12: similarity ≥ 0.75 vs last 3 rounds, 2 consecutive → collapse."""
        # Create prior rounds with plans very similar to what stub candidates
        # would be. The stub candidate 0 is "candidate 0: approach 0 for <obj>".
        objective = "implement a queue"
        # Make prior plans that match the stub format.
        prev = [
            _FakeRound("candidate 0: approach 0 for implement a queue"),
            _FakeRound("candidate 0: approach 0 for implement a queue"),
        ]
        result = generate_candidate_plans(objective, prev, n_candidates=3)
        # The selected plan should be similar to prior → collapse.
        assert result.collapse_detected is True
        assert result.forced_diversity is True

    def test_collapse_respects_threshold(self):
        cfg = RalphConfig(g12_similarity_threshold=0.99)
        prev = [_FakeRound("completely different topic about databases")]
        result = generate_candidate_plans("build a cache", prev, config=cfg)
        assert result.collapse_detected is False

    def test_collapse_respects_consecutive_rounds(self):
        cfg = RalphConfig(g12_consecutive_rounds=3)
        objective = "implement a stack"
        prev = [
            _FakeRound("candidate 0: approach 0 for implement a stack"),
            _FakeRound("candidate 0: approach 0 for implement a stack"),
        ]
        result = generate_candidate_plans(objective, prev, config=cfg)
        # Only 2 consecutive similar, but 3 needed → no collapse.
        assert result.collapse_detected is False


# ---------------------------------------------------------------------------
# Selection tie-break (S5 #1)
# ---------------------------------------------------------------------------

class TestSelection:
    def test_highest_feasibility_wins(self):
        scored = [
            ScoredPlan("a", feasibility=3.0, alignment=9.0, novelty=9.0),
            ScoredPlan("b", feasibility=8.0, alignment=1.0, novelty=1.0),
        ]
        idx, reason = _select(scored)
        assert idx == 1  # feasibility dominates

    def test_alignment_tiebreak(self):
        scored = [
            ScoredPlan("a", feasibility=5.0, alignment=3.0, novelty=9.0),
            ScoredPlan("b", feasibility=5.0, alignment=8.0, novelty=1.0),
        ]
        idx, _ = _select(scored)
        assert idx == 1  # same feasibility, higher alignment wins

    def test_novelty_tiebreak(self):
        scored = [
            ScoredPlan("a", feasibility=5.0, alignment=5.0, novelty=2.0),
            ScoredPlan("b", feasibility=5.0, alignment=5.0, novelty=9.0),
        ]
        idx, _ = _select(scored)
        assert idx == 1  # same feasibility+alignment, higher novelty wins

    def test_empty_candidates(self):
        idx, reason = _select([])
        assert idx == 0
        assert "no candidates" in reason
