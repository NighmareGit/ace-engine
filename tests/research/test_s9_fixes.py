"""Regression tests for adversarial review findings (S9).

Each test reproduces the reviewer's failing scenario for the numbered
finding it covers.  Naming: test_s9_fix_N_*.
"""

import ast
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure the ace-engine root is on sys.path for imports.
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)


# ---------------------------------------------------------------------------
# Fix #1: EvidenceScorer implementation
# ---------------------------------------------------------------------------

class TestS9Fix1EvidenceScorer:
    """Fix #1 — scoring.py was a stub.  Verify EvidenceScorer is implemented."""

    def test_scorer_is_not_stub(self):
        """scoring.py must have a real EvidenceScorer class with score()."""
        from engine.research import scoring
        # The module must define EvidenceScorer (not just import it).
        assert hasattr(scoring, "EvidenceScorer")
        assert hasattr(scoring, "EVIDENCE_PASS_THRESHOLD")
        assert scoring.EVIDENCE_PASS_THRESHOLD == 6.0
        # Must have the 5 research dimensions.
        assert hasattr(scoring, "DIMENSIONS_RESEARCH")
        assert len(scoring.DIMENSIONS_RESEARCH) == 5
        assert "citation_coverage" in scoring.DIMENSIONS_RESEARCH

    def test_scorer_score_returns_expected_shape(self):
        """score() must return dict with scores, overall, judge_model, etc."""
        from engine.research.scoring import EvidenceScorer, DIMENSIONS_RESEARCH

        # Build a mock transport that returns a valid JSON response.
        mock_transport = MagicMock()
        mock_transport.curl_beellama.return_value = {
            "content": '{"citation_coverage": 7, "claim_traceability": 6, '
                       '"contradiction_handling": 8, "verdict_justification": 7, '
                       '"source_quality": 5, "reasoning": "ok"}',
            "model": "test-judge",
        }
        mock_transport.get_model_list.return_value = ["test-judge"]

        scorer = EvidenceScorer()
        result = scorer.score(
            verdict="## Verdict\nPosition: supported\n...",
            task=None,
            transport=mock_transport,
            run_id="r1",
            save=False,
        )
        assert "scores" in result
        assert "overall" in result
        assert "judge_model" in result
        assert "reasoning" in result
        assert "error" in result
        for dim in DIMENSIONS_RESEARCH:
            assert dim in result["scores"]
            assert 0 <= result["scores"][dim] <= 10
        # overall = mean of 5 dims
        expected_overall = sum(result["scores"][d] for d in DIMENSIONS_RESEARCH) / 5.0
        assert result["overall"] == pytest.approx(expected_overall, abs=0.01)

    def test_scorer_never_sets_passed(self):
        """The scorer must NEVER set a 'passed' field (ADR-0004)."""
        from engine.research.scoring import EvidenceScorer

        mock_transport = MagicMock()
        mock_transport.curl_beellama.return_value = {
            "content": '{"citation_coverage": 5, "claim_traceability": 5, '
                       '"contradiction_handling": 5, "verdict_justification": 5, '
                       '"source_quality": 5}',
            "model": "test-judge",
        }
        mock_transport.get_model_list.return_value = ["test-judge"]

        scorer = EvidenceScorer()
        result = scorer.score(verdict="x", transport=mock_transport, save=False)
        assert "passed" not in result

    def test_scorer_one_retry_on_transient(self):
        """ONE retry on transient failure, then zeros + error."""
        from engine.research.scoring import EvidenceScorer

        mock_transport = MagicMock()
        # First call raises, second call succeeds.
        mock_transport.curl_beellama.side_effect = [
            Exception("timeout"),
            {"content": '{"citation_coverage": 3, "claim_traceability": 3, '
                        '"contradiction_handling": 3, "verdict_justification": 3, '
                        '"source_quality": 3}', "model": "judge"},
        ]
        mock_transport.get_model_list.return_value = ["judge"]

        scorer = EvidenceScorer()
        result = scorer.score(verdict="x", transport=mock_transport, save=False)
        assert result["error"] is None  # retry succeeded
        assert mock_transport.curl_beellama.call_count == 2

    def test_scorer_port_guard(self):
        """score() raises if judge port == subject port."""
        from engine.research.scoring import EvidenceScorer

        mock_transport = MagicMock()
        scorer = EvidenceScorer()
        with patch("engine.research.scoring.JUDGE_PORT", 8080), \
             patch("engine.research.scoring.SUBJECT_PORT", 8080):
            with pytest.raises(ValueError, match="judge.*subject"):
                scorer.score(verdict="x", transport=mock_transport, save=False)

    def test_research_scores_table_exists(self):
        """init_db() must create research_scores table."""
        from engine.state import _get_conn, init_db
        init_db()
        conn = _get_conn()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "research_scores" in tables
        assert "research_verdicts" in tables

    def test_save_research_scores(self):
        """save_research_scores() must persist to research_scores."""
        from engine.state import save_research_scores, _get_conn, init_db
        init_db()
        scores = {
            "citation_coverage": 7, "claim_traceability": 6,
            "contradiction_handling": 8, "verdict_justification": 7,
            "source_quality": 5,
        }
        row_id = save_research_scores(
            run_id="test-run", task_id="T01", judge_model="test-judge",
            scores=scores, reasoning="ok", error=None)
        assert row_id is not None
        # Verify the row was inserted.
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM research_scores WHERE id=?", (row_id,)).fetchone()
        conn.close()
        assert row is not None
        assert row["citation_coverage"] == 7
        assert row["overall"] == pytest.approx(6.6, abs=0.1)


# ---------------------------------------------------------------------------
# Fix #2: gate_verdict wiring in round_driver.py
# ---------------------------------------------------------------------------

class TestS9Fix2GateVerdictWiring:
    """Fix #2 — gate_verdict was hardcoded True.  Must call evaluate_gate_sequence."""

    def test_gate_verdict_not_hardcoded(self):
        """round_driver must NOT hardcode GateVerdict(overall_pass=True, gates=[])."""
        import inspect
        from engine.workflows.ralph import round_driver
        src = inspect.getsource(round_driver)
        # The hardcoded pattern must be gone.
        assert "GateVerdict(overall_pass=True, gates=[])" not in src

    def test_evaluate_gate_sequence_called(self):
        """_execute_round must call evaluate_gate_sequence."""
        import inspect
        from engine.workflows.ralph import round_driver
        src = inspect.getsource(round_driver._execute_round)
        assert "evaluate_gate_sequence" in src

    def test_gate_failure_marks_round_failed(self):
        """When evaluate_gate_sequence returns overall_pass=False, round is failed."""
        from engine.workflows.ralph import round_driver
        from engine.workflows.ralph.report_types import GateVerdict, GateResult

        # Mock evaluate_gate_sequence to return a failing verdict.
        failing_verdict = GateVerdict(overall_pass=False, gates=[
            GateResult(gate="dev", passed=False, detail="mock failure")
        ])

        with patch.object(round_driver, "evaluate_gate_sequence",
                          return_value=failing_verdict), \
             patch.object(round_driver, "_git_sha", return_value="abc123"), \
             patch.object(round_driver, "_persist_round"), \
             patch.object(round_driver, "_persist_report"), \
             patch("engine.workflows.ralph.ideation.generate_candidate_plans") as mock_ideation:

            # Make ideation return a minimal result.
            mock_ideation_result = MagicMock()
            mock_ideation_result.candidates = []
            mock_ideation.return_value = mock_ideation_result

            config = round_driver.RalphConfig()
            report, state = round_driver._execute_round(
                ralph_run_id="test-ralph",
                round_num=1,
                objective="test objective",
                project_path="/tmp",
                config=config,
                previous_reports=[],
                on_event=None,
            )
            assert state == "failed"
            assert report.gate_verdict.overall_pass is False


# ---------------------------------------------------------------------------
# Fix #3: Circular citation detection (replaces dead claim->source check)
# ---------------------------------------------------------------------------

class TestS9Fix3CircularCitation:
    """Fix #3 — claim->source cycle check was dead code (bipartite graph).
    The real semantic detects circular citation structures.
    """

    def test_circular_citation_detected(self):
        """Reviewer's scenario: claim-1->src-2, claim-2->src-1 where srcs
        reference claims.  This must be detected as a cycle."""
        from engine.research.evidence import EvidenceValidator

        # Build a verdict where src-1 references claim-2 and src-2
        # references claim-1, creating a circular citation.
        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: claim uri: claim-2 title: Source A
- [src-2] type: claim uri: claim-1 title: Source B

## Claims

- [claim-1] First claim [src-2]
- [claim-2] Second claim [src-1]

## Contradictions

- between [1, 2] resolution: explained by different contexts X and Y

## Citations

[claim:1, src:2, "evidence span one", 0.9]
[claim:2, src:1, "evidence span two", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert any("circular" in e.lower() for e in [stage4.get("error", "")])

    def test_legitimate_shared_namespace_not_cyclic(self):
        """claim-1 citing src-1 (shared namespace) must NOT be flagged as cycle."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: explained by different contexts X and Y

## Citations

[claim:1, src:1, "evidence span one", 0.9]
[claim:2, src:2, "evidence span two", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert r.passed, f"Should pass but failed: {r.checks[-1]}"

    def test_source_source_cycle_still_works(self):
        """Source->source citation rings must still be detected."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] title: Source A cites: [src-2]
- [src-2] title: Source B cites: [src-1]

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: explained by context X

## Citations

[claim:1, src:1, "evidence span one", 0.9]
[claim:2, src:2, "evidence span two", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert any("cycle" in e.lower() for e in [stage4.get("error", "")])


# ---------------------------------------------------------------------------
# Fix #4: Non-empty evidence spans
# ---------------------------------------------------------------------------

class TestS9Fix4EmptySpans:
    """Fix #4 — every citation must have a non-empty span."""

    def test_empty_span_fails_stage4(self):
        """A citation with an empty span must FAIL stage 4 with 'empty_span'."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: explained by context X

## Citations

[claim:1, src:1, "", 0.9]
[claim:2, src:2, "valid span", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "empty_span" in stage4["error"]

    def test_whitespace_only_span_fails(self):
        """A citation with a whitespace-only span must FAIL."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: explained by context X

## Citations

[claim:1, src:1, "   ", 0.9]
[claim:2, src:2, "valid span", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "empty_span" in stage4["error"]

    def test_nonempty_spans_pass(self):
        """Citations with non-empty spans must pass."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: explained by context X

## Citations

[claim:1, src:1, "valid evidence span", 0.9]
[claim:2, src:2, "another valid span", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert r.passed, f"Should pass but failed: {r.checks[-1]}"


# ---------------------------------------------------------------------------
# Fix #5: verify_file_sources wired into ResearchTaskHandler.run()
# ---------------------------------------------------------------------------

class TestS9Fix5VerifyFileSources:
    """Fix #5 — verify_file_sources() must be called in ResearchTaskHandler.run()."""

    def test_verify_file_sources_called_in_run(self):
        """ResearchTaskHandler.run() must call verify_file_sources for file sources."""
        import inspect
        from engine.research import ResearchTaskHandler
        src = inspect.getsource(ResearchTaskHandler.run)
        assert "verify_file_sources" in src

    def test_file_source_failure_fails_atom(self):
        """When verify_file_sources finds a failure, run() returns FAILED."""
        from engine.research import ResearchTaskHandler
        from engine.engine import State

        # Create a temporary project with a file that doesn't contain the span.
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file that does NOT contain the expected span.
            with open(os.path.join(tmpdir, "source.txt"), "w") as f:
                f.write("This is some content that does not match.")

            handler = ResearchTaskHandler(engine=MagicMock())

            # Mock the generator to produce a verdict with a file:// source.
            mock_vresult = MagicMock()
            mock_vresult.verdict_text = "x" * 100
            mock_generator = MagicMock()
            mock_generator.generate.return_value = mock_vresult
            handler._generator = mock_generator

            # Mock the validator to pass, with a file:// source in the result.
            from engine.research.evidence import EvidenceResult, CitationRecord
            ev_result = EvidenceResult(
                passed=True,
                checks=[
                    {"stage": 0, "name": "empty", "passed": True, "error": None},
                    {"stage": 1, "name": "structure", "passed": True, "error": None},
                    {"stage": 2, "name": "citation_format", "passed": True, "error": None},
                    {"stage": 3, "name": "claim_coverage", "passed": True, "error": None},
                    {"stage": 4, "name": "sources", "passed": True, "error": None},
                    {"stage": 5, "name": "answer_question", "passed": True, "error": None},
                ],
                sources=[{"id": "1"}],
                citations=[CitationRecord(claim_id="1", source_ids=["1"],
                                            span="this text does not exist")],
                _verdict_text="""## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: file://source.txt title: Source A

## Claims

- [claim-1] First claim [src-1]

## Contradictions

- none resolution: no contradictions found in the evidence

## Citations

[claim:1, src:1, "this text does not exist", 0.9]
""",
            )
            mock_validator = MagicMock()
            mock_validator.validate.return_value = ev_result
            handler._validator = mock_validator

            # Mock the scorer (should not be called if source verify fails).
            mock_scorer = MagicMock()
            handler._scorer = mock_scorer

            task = MagicMock()
            task.id = "T01"
            task.title = "test"
            task.description = "test question"

            pipeline = MagicMock()
            pipeline.run_id = "test-run"

            result = handler.run(task, pipeline, tmpdir)
            assert result.state == State.FAILED.value
            assert "file source" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# Fix #6: Self-support uses exact text equality
# ---------------------------------------------------------------------------

class TestS9Fix6SelfSupportExactEquality:
    """Fix #6 — self-support uses exact normalized text equality, not ratio."""

    def test_verbatim_self_support_detected(self):
        """When span IS the claim text verbatim, self-support is detected."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A

## Claims

- [claim-1] Three-tier caching reduces latency [src-1]

## Contradictions

- none resolution: no contradictions found

## Citations

[claim:1, src:1, "Three-tier caching reduces latency", 0.9]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "self-support" in stage4["error"]

    def test_restating_span_not_false_positive(self):
        """A longer evidence span that merely contains the claim text must
        NOT be flagged as self-support (the old 0.8 ratio would have flagged it)."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A

## Claims

- [claim-1] Three-tier caching reduces latency [src-1]

## Contradictions

- none resolution: no contradictions found

## Citations

[claim:1, src:1, "Three-tier caching reduces latency significantly because it minimizes memory overhead", 0.9]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        # This should PASS — the span is NOT verbatim the claim text.
        assert r.passed, f"Should pass but failed: {r.checks[-1]}"


# ---------------------------------------------------------------------------
# Fix #7: Contradiction resolution anti-gaming
# ---------------------------------------------------------------------------

class TestS9Fix7ContradictionAntiGaming:
    """Fix #7 — reject gaming in contradiction resolution."""

    def test_boilerplate_resolution_rejected(self):
        """A resolution that is just 'none' or 'n/a' must be rejected."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: none

## Citations

[claim:1, src:1, "evidence span one", 0.9]
[claim:2, src:2, "evidence span two", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "boilerplate" in stage4["error"]

    def test_short_resolution_rejected(self):
        """A resolution with < 10 chars of non-boilerplate content is rejected."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: ok

## Citations

[claim:1, src:1, "evidence span one", 0.9]
[claim:2, src:2, "evidence span two", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "boilerplate" in stage4["error"]

    def test_valid_resolution_passes(self):
        """A real resolution (> 10 chars, non-boilerplate) must pass."""
        from engine.research.evidence import EvidenceValidator

        md = """## Verdict

Position: supported
Confidence: 0.85
The approach is sound.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] First claim [src-1]
- [claim-2] Second claim [src-2]

## Contradictions

- between [1, 2] resolution: explained by different experimental contexts

## Citations

[claim:1, src:1, "evidence span one", 0.9]
[claim:2, src:2, "evidence span two", 0.8]
"""
        r = EvidenceValidator().validate(md, question="the approach is sound")
        assert r.passed, f"Should pass but failed: {r.checks[-1]}"


# ---------------------------------------------------------------------------
# Fix #8: EVIDENCE_PASS_THRESHOLD gating in ResearchTaskHandler.run()
# ---------------------------------------------------------------------------

class TestS9Fix8EvidencePassThreshold:
    """Fix #8 — ResearchTaskHandler.run() must compare overall to threshold."""

    def test_low_score_fails_atom(self):
        """When scorer overall < 6.0, run() returns FAILED."""
        from engine.research import ResearchTaskHandler, EVIDENCE_PASS_THRESHOLD
        from engine.engine import State

        handler = ResearchTaskHandler(engine=MagicMock())

        # Mock generator.
        mock_vresult = MagicMock()
        mock_vresult.verdict_text = "x" * 100
        mock_generator = MagicMock()
        mock_generator.generate.return_value = mock_vresult
        handler._generator = mock_generator

        # Mock validator to pass.
        from engine.research.evidence import EvidenceResult
        ev_result = EvidenceResult(passed=True, checks=[
            {"stage": 0, "name": "empty", "passed": True, "error": None},
            {"stage": 1, "name": "structure", "passed": True, "error": None},
            {"stage": 2, "name": "citation_format", "passed": True, "error": None},
            {"stage": 3, "name": "claim_coverage", "passed": True, "error": None},
            {"stage": 4, "name": "sources", "passed": True, "error": None},
            {"stage": 5, "name": "answer_question", "passed": True, "error": None},
        ])
        mock_validator = MagicMock()
        mock_validator.validate.return_value = ev_result
        handler._validator = mock_validator

        # Mock scorer to return a low overall.
        mock_scorer = MagicMock()
        mock_scorer.score.return_value = {
            "scores": {"citation_coverage": 3, "claim_traceability": 4,
                       "contradiction_handling": 5, "verdict_justification": 3,
                       "source_quality": 4},
            "overall": 3.8,
            "judge_model": "test",
            "reasoning": "low quality",
            "error": None,
        }
        handler._scorer = mock_scorer

        task = MagicMock()
        task.id = "T01"
        task.title = "test"
        task.description = "test question"

        pipeline = MagicMock()
        pipeline.run_id = "test-run"

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.run(task, pipeline, tmpdir)

        assert result.state == State.FAILED.value
        assert "threshold" in (result.error_message or "").lower()

    def test_scoring_error_fails_atom(self):
        """When scoring raises, run() returns FAILED (no silent swallow)."""
        from engine.research import ResearchTaskHandler
        from engine.engine import State

        handler = ResearchTaskHandler(engine=MagicMock())

        mock_vresult = MagicMock()
        mock_vresult.verdict_text = "x" * 100
        mock_generator = MagicMock()
        mock_generator.generate.return_value = mock_vresult
        handler._generator = mock_generator

        from engine.research.evidence import EvidenceResult
        ev_result = EvidenceResult(passed=True, checks=[
            {"stage": 0, "name": "empty", "passed": True, "error": None},
            {"stage": 1, "name": "structure", "passed": True, "error": None},
            {"stage": 2, "name": "citation_format", "passed": True, "error": None},
            {"stage": 3, "name": "claim_coverage", "passed": True, "error": None},
            {"stage": 4, "name": "sources", "passed": True, "error": None},
            {"stage": 5, "name": "answer_question", "passed": True, "error": None},
        ])
        mock_validator = MagicMock()
        mock_validator.validate.return_value = ev_result
        handler._validator = mock_validator

        # Mock scorer to raise.
        mock_scorer = MagicMock()
        mock_scorer.score.side_effect = RuntimeError("judge model unavailable")
        handler._scorer = mock_scorer

        task = MagicMock()
        task.id = "T01"
        task.title = "test"
        task.description = "test question"

        pipeline = MagicMock()
        pipeline.run_id = "test-run"

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.run(task, pipeline, tmpdir)

        assert result.state == State.FAILED.value
        assert "scoring" in (result.error_message or "").lower()

    def test_high_score_commits(self, monkeypatch):
        """When scorer overall >= 6.0, run() returns COMMIT."""
        from engine.research import ResearchTaskHandler
        from engine.engine import State

        # Mock Gitea repo existence check (offline test).
        monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: True)

        engine_mock = MagicMock()
        # Provide a transport mock with run_git returning proper tuples.
        fake_sha = "c" * 40

        def _run_git(project_path, *args):
            if args[0] == "rev-parse":
                return (fake_sha, "", 0)
            if args[0] == "commit":
                return ("", "nothing to commit, working tree clean", 1)
            return ("", "", 0)

        engine_mock.transport.run_git.side_effect = _run_git
        engine_mock.transport.run_command.return_value = ("", "", 0)
        handler = ResearchTaskHandler(engine=engine_mock)

        mock_vresult = MagicMock()
        mock_vresult.verdict_text = "x" * 100
        mock_generator = MagicMock()
        mock_generator.generate.return_value = mock_vresult
        handler._generator = mock_generator

        from engine.research.evidence import EvidenceResult
        ev_result = EvidenceResult(passed=True, checks=[
            {"stage": 0, "name": "empty", "passed": True, "error": None},
            {"stage": 1, "name": "structure", "passed": True, "error": None},
            {"stage": 2, "name": "citation_format", "passed": True, "error": None},
            {"stage": 3, "name": "claim_coverage", "passed": True, "error": None},
            {"stage": 4, "name": "sources", "passed": True, "error": None},
            {"stage": 5, "name": "answer_question", "passed": True, "error": None},
        ])
        mock_validator = MagicMock()
        mock_validator.validate.return_value = ev_result
        handler._validator = mock_validator

        # Mock scorer to return a high overall.
        mock_scorer = MagicMock()
        mock_scorer.score.return_value = {
            "scores": {"citation_coverage": 8, "claim_traceability": 7,
                       "contradiction_handling": 9, "verdict_justification": 8,
                       "source_quality": 7},
            "overall": 7.8,
            "judge_model": "test",
            "reasoning": "good quality",
            "error": None,
        }
        handler._scorer = mock_scorer

        task = MagicMock()
        task.id = "T01"
        task.title = "test"
        task.description = "test question"

        pipeline = MagicMock()
        pipeline.run_id = "test-run"

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.run(task, pipeline, tmpdir)

        assert result.state == State.COMMIT.value


# ---------------------------------------------------------------------------
# Cheap notes: SourceCheckResult rename + fence extraction doc
# ---------------------------------------------------------------------------

class TestS9CheapNotes:
    """Cheap notes: SourceCheckResult rename, fence extraction doc."""

    def test_source_check_result_exists(self):
        """sources.py must export SourceCheckResult (renamed from EvidenceResult)."""
        from engine.research.sources import SourceCheckResult
        assert SourceCheckResult is not None
        # It should be a dataclass with source, status, message fields.
        r = SourceCheckResult(source="x", status=MagicMock())
        assert r.source == "x"

    def test_evidence_result_alias_still_works(self):
        """The EvidenceResult alias must still work for backward compat."""
        from engine.research.sources import EvidenceResult, SourceCheckResult
        assert EvidenceResult is SourceCheckResult

    def test_fence_extraction_documented(self):
        """verdict.py _extract_verdict docstring must document the disobedience issue."""
        import inspect
        from engine.research.verdict import VerdictGenerator
        doc = inspect.getdoc(VerdictGenerator._extract_verdict) or ""
        assert "disobedi" in doc.lower() or "forbid" in doc.lower()
