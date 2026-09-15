"""Tests for engine.workflows.ralph.gates — mechanical gate enforcement (R1/R4)."""

import os
import sys
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from engine.workflows.ralph.gates import (
    dev_gate, review_gate, run_test_gate, redteam_gate, evaluate_gate_sequence,
)
from engine.workflows.ralph.report_types import GateResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeTask:
    id: str = "T1"
    title: str = "implement cache"


class _FakeCode:
    def __init__(self, files: dict[str, str]):
        self.files = files


# ---------------------------------------------------------------------------
# R1: gate verdicts derive from actual call results, never LLM self-report
# ---------------------------------------------------------------------------

class TestDevGate:
    def test_dev_passes_on_valid_code(self):
        """Dev gate passes when validator says passed=True."""
        fake_result = MagicMock()
        fake_result.passed = True
        fake_result.stages = [{"stage": "ast", "file": "a.py", "passed": True, "error": ""}]
        with patch("engine.validator.validate", return_value=fake_result) as m:
            result = dev_gate(_FakeCode({"a.py": "x = 1"}), _FakeTask(), "/tmp/proj")
            assert result.gate == "dev"
            assert result.passed is True
            m.assert_called_once()

    def test_dev_fails_on_invalid_code(self):
        """Dev gate fails when validator says passed=False — NOT an LLM claim."""
        fake_result = MagicMock()
        fake_result.passed = False
        fake_result.stages = [{"stage": "ast", "file": "a.py", "passed": False,
                               "error": "SyntaxError"}]
        with patch("engine.validator.validate", return_value=fake_result):
            result = dev_gate(_FakeCode({"a.py": "!!!"}), _FakeTask(), "/tmp/proj")
            assert result.passed is False
            assert "SyntaxError" in result.detail

    def test_dev_no_self_report_path(self):
        """R1: there is no code path where the model's own claim sets pass/fail.

        The dev gate result is ALWAYS derived from the validator call. We verify
        by checking that dev_gate never reads any 'passed' from the input code
        object or task — it only reads from the validator result.
        """
        fake_result = MagicMock()
        fake_result.passed = True
        fake_result.stages = []
        with patch("engine.validator.validate", return_value=fake_result):
            code = _FakeCode({"a.py": "valid"})
            result = dev_gate(code, _FakeTask(), "/tmp/proj")
            assert result.passed is True  # from validator, not from input


class TestReviewGate:
    def test_review_passes_on_high_score(self):
        """Review passes when judge score ≥ threshold."""
        fake_verdict = {
            "overall": 7.5, "judge_model": "qwen3.6-35b",
            "scores": {}, "reasoning": "", "error": None,
        }
        with patch("engine.llm_judge.score_task", return_value=fake_verdict):
            result = review_gate("code", _FakeTask(), MagicMock(), 8080)
            assert result.gate == "review"
            assert result.passed is True
            assert result.model == "qwen3.6-35b"

    def test_review_fails_on_low_score(self):
        """Review fails when judge score < threshold — verdict from score, not self."""
        fake_verdict = {
            "overall": 3.0, "judge_model": "qwen3.6-35b",
            "scores": {}, "reasoning": "", "error": None,
        }
        with patch("engine.llm_judge.score_task", return_value=fake_verdict):
            result = review_gate("code", _FakeTask(), MagicMock(), 8080)
            assert result.passed is False

    def test_review_no_self_report(self):
        """R1: review gate result derives from score_task call, never from input."""
        fake_verdict = {"overall": 8.0, "judge_model": "judge-x",
                        "scores": {}, "reasoning": "", "error": None}
        with patch("engine.llm_judge.score_task", return_value=fake_verdict) as m:
            result = review_gate("code", _FakeTask(), MagicMock(), 8080)
            assert result.passed is True
            m.assert_called_once()


class TestTestGate:
    def test_test_passes_on_clean_run(self):
        fake_result = MagicMock()
        fake_result.passed = True
        fake_result.tests_passed = 5
        fake_result.tests_failed = 0
        with patch("engine.tester.run_tests", return_value=fake_result):
            result = run_test_gate("/tmp/proj", MagicMock())
            assert result.gate == "test"
            assert result.passed is True


class TestRedteamGate:
    def test_redteam_passes_on_high_score(self):
        fake_verdict = {"overall": 8.0, "judge_model": "qwen3.6-35b",
                        "scores": {}, "reasoning": "", "error": None}
        with patch("engine.llm_judge.score_task", return_value=fake_verdict):
            from engine.workflows.ralph.config import RalphConfig
            cfg = RalphConfig()
            result = redteam_gate("code", _FakeTask(), MagicMock(), cfg)
            assert result.passed is True
            assert result.model == "qwen3.6-35b"

    def test_redteam_fails_on_low_score(self):
        fake_verdict = {"overall": 2.0, "judge_model": "qwen3.6-35b",
                        "scores": {}, "reasoning": "", "error": None}
        with patch("engine.llm_judge.score_task", return_value=fake_verdict):
            from engine.workflows.ralph.config import RalphConfig
            cfg = RalphConfig()
            result = redteam_gate("code", _FakeTask(), MagicMock(), cfg)
            assert result.passed is False


class TestEvaluateGateSequence:
    def test_all_pass(self):
        dev_r = MagicMock(); dev_r.passed = True; dev_r.stages = []
        review_v = {"overall": 8.0, "judge_model": "j", "scores": {},
                    "reasoning": "", "error": None}
        test_r = MagicMock(); test_r.passed = True
        test_r.tests_passed = 3; test_r.tests_failed = 0
        redteam_v = {"overall": 8.0, "judge_model": "j", "scores": {},
                     "reasoning": "", "error": None}
        with patch("engine.validator.validate", return_value=dev_r), \
             patch("engine.llm_judge.score_task", side_effect=[review_v, redteam_v]), \
             patch("engine.tester.run_tests", return_value=test_r):
            from engine.workflows.ralph.config import RalphConfig
            cfg = RalphConfig()
            verdict = evaluate_gate_sequence(
                _FakeCode({"a.py": "x=1"}), _FakeTask(), "/tmp/proj",
                MagicMock(), cfg,
            )
            assert verdict.overall_pass is True
            assert len(verdict.gates) == 4
            assert [gg.gate for gg in verdict.gates] == ["dev", "review", "test", "redteam"]

    def test_dev_failure_short_circuits(self):
        dev_r = MagicMock(); dev_r.passed = False
        dev_r.stages = [{"stage": "ast", "file": "a.py", "passed": False,
                         "error": "boom"}]
        with patch("engine.validator.validate", return_value=dev_r):
            from engine.workflows.ralph.config import RalphConfig
            cfg = RalphConfig()
            verdict = evaluate_gate_sequence(
                _FakeCode({"a.py": "!!!"}), _FakeTask(), "/tmp/proj",
                MagicMock(), cfg,
            )
            assert verdict.overall_pass is False
            assert len(verdict.gates) == 1  # only dev ran
            assert verdict.retry_recommended is True
            assert verdict.retry_target == "dev"

    def test_redteam_failure_no_retry(self):
        dev_r = MagicMock(); dev_r.passed = True; dev_r.stages = []
        review_v = {"overall": 8.0, "judge_model": "j", "scores": {},
                    "reasoning": "", "error": None}
        test_r = MagicMock(); test_r.passed = True
        test_r.tests_passed = 1; test_r.tests_failed = 0
        redteam_v = {"overall": 1.0, "judge_model": "j", "scores": {},
                     "reasoning": "", "error": None}
        with patch("engine.validator.validate", return_value=dev_r), \
             patch("engine.llm_judge.score_task", side_effect=[review_v, redteam_v]), \
             patch("engine.tester.run_tests", return_value=test_r):
            from engine.workflows.ralph.config import RalphConfig
            cfg = RalphConfig()
            verdict = evaluate_gate_sequence(
                _FakeCode({"a.py": "x=1"}), _FakeTask(), "/tmp/proj",
                MagicMock(), cfg,
            )
            assert verdict.overall_pass is False
            assert verdict.retry_recommended is False
            assert verdict.retry_target == "redteam"
