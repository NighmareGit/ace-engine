"""Regression tests: research tasks must NEVER get code-atom ast validation.

Proves the handler-dispatch decision is keyed on task_type == 'research'
(not file extension guessing).  A research task with a .md verdict file
containing markdown (em-dashes, numbers) passes validation; a code task
with the same file still fails ast (proving the key is task_type, not
extension).
"""

import ast
import os
import sys
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.task_handler import _resolve_handler, CodeTaskHandler
from engine.validator import check_ast


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A markdown verdict with em-dashes and numbers that fails ast.parse but
# is perfectly valid research output.  The citation spans are deliberately
# DIFFERENT from the claim text (verbatim span == claim text triggers the
# self-support detector in EvidenceValidator stage 4).
MARKDOWN_VERDICT = """\
## Verdict

Position: supported
Confidence: 0.85
The best approach — given 220 commits of drift and 310 changed lines — is
to track the preview-v0.4.7 tag (53a68d3c) because it captures the latest
expert-path work while the port's merge-base lags by 0.4.6.

## Sources

- [src-1] type: file uri: file://dogfood-sources/upstream-drift.log title: Drift Log
- [src-2] type: file uri: file://dogfood-sources/upstream-drift-stat.txt title: Drift Stat

## Claims

- [claim-1] Upstream has drifted 220 commits from the local merge-base [src-1]
- [claim-2] llama-graph.cpp drifted 310 changed lines [src-2]

## Contradictions

- between [1, 2] resolution: both claims are complementary — commit count and line count measure different aspects of drift.

## Citations

[claim:1, src:1, "220 commits between merge-base and main per upstream-drift.log", 0.9]
[claim:2, src:2, "310 changed lines in llama-graph.cpp per drift-stat.txt", 0.85]
"""


def _make_task(task_type="research", module="verdicts/R01-verdict.md",
                files=None, category="research"):
    t = Task(
        id="R01",
        title="Branch selection research",
        description="Which upstream base should the port track?",
        module=module,
        task_type=task_type,
        files=files or [module],
        acceptance_criteria=["verdict.md has ## Verdict section"],
    )
    # category is the PRD-level field that maps to task_type; keep it on the
    # task so inject_sidecar-style propagation can read it.
    t.category = category
    return t


# ---------------------------------------------------------------------------
# Test: handler dispatch keys on task_type, not file extension
# ---------------------------------------------------------------------------

class TestHandlerDispatchOnTaskType:
    """_resolve_handler must return ResearchTaskHandler for research tasks
    even when the target file is .md (not .py)."""

    def test_research_task_type_routes_to_research_handler(self):
        task = _make_task(task_type="research")
        handler = _resolve_handler(task, None)
        assert not isinstance(handler, CodeTaskHandler), (
            "research task must NOT get CodeTaskHandler (would run ast on markdown)")

    def test_implementation_task_type_routes_to_code_handler(self):
        task = _make_task(task_type="implementation")
        handler = _resolve_handler(task, None)
        assert isinstance(handler, CodeTaskHandler)

    def test_research_with_md_file_gets_research_handler(self):
        """The critical regression: a research task whose module is a .md file
        must still get the ResearchTaskHandler, not CodeTaskHandler."""
        task = _make_task(task_type="research",
                          module="verdicts/R01-verdict.md",
                          files=["verdicts/R01-verdict.md"])
        handler = _resolve_handler(task, None)
        assert not isinstance(handler, CodeTaskHandler)

    def test_code_task_with_md_file_gets_code_handler(self):
        """A code task with a .md file still gets CodeTaskHandler (the ast
        failure is expected — markdown is not code)."""
        task = _make_task(task_type="implementation",
                          module="notes.md",
                          files=["notes.md"],
                          category="api")
        handler = _resolve_handler(task, None)
        assert isinstance(handler, CodeTaskHandler)


# ---------------------------------------------------------------------------
# Test: ast.parse fails on markdown (proving the failure mode is real)
# ---------------------------------------------------------------------------

class TestAstFailsOnMarkdown:
    """Prove that ast.parse rejects markdown verdict content — this is the
    failure that research tasks must avoid by routing to ResearchTaskHandler."""

    def test_markdown_with_em_dash_fails_ast(self):
        """Em-dash (U+2014) is invalid Python syntax."""
        md = "The best approach — given the evidence — is supported."
        passed, error = check_ast(md)
        assert not passed
        assert "invalid character" in error or "invalid" in error

    def test_markdown_with_version_number_fails_ast(self):
        """Version numbers like 0.4.6 are invalid decimal literals."""
        md = "The port tracks v0.4.6 — the latest release tag."
        passed, error = check_ast(md)
        assert not passed
        # "invalid decimal literal" or similar syntax error
        assert error is not None

    def test_markdown_verdict_fails_ast(self):
        """The full markdown verdict fails ast.parse (proving the bug)."""
        passed, error = check_ast(MARKDOWN_VERDICT)
        assert not passed, "markdown verdict must fail ast.parse (it's not Python)"

    def test_valid_python_passes_ast(self):
        """Sanity: valid Python code passes ast.parse."""
        code = "x = 1 + 2\nprint(x)\n"
        passed, error = check_ast(code)
        assert passed


# ---------------------------------------------------------------------------
# Test: EvidenceValidator accepts the markdown verdict (research validation)
# ---------------------------------------------------------------------------

class TestEvidenceValidatorAcceptsMarkdown:
    """The EvidenceValidator (research validation) must accept markdown
    verdict content — it never calls ast.parse."""

    def test_valid_verdict_passes_evidence_validator(self):
        from engine.research.evidence import EvidenceValidator
        r = EvidenceValidator().validate(MARKDOWN_VERDICT,
                                         question="Which upstream base should the port track?")
        assert r.passed, f"EvidenceValidator must pass valid verdict; checks={r.checks}"

    def test_verdict_with_em_dashes_passes_evidence_validator(self):
        """Em-dashes in verdict text are fine for EvidenceValidator."""
        from engine.research.evidence import EvidenceValidator
        verdict = MARKDOWN_VERDICT.replace(
            "The best approach — given 220 commits",
            "The best approach — given 220 commits"
        )
        # Provide a question with enough overlap to pass stage 5 (TF-IDF).
        r = EvidenceValidator().validate(
            verdict,
            question="Which upstream base should the port track given 220 commits of drift and 310 changed lines?")
        assert r.passed, f"Em-dashes must not fail EvidenceValidator; checks={r.checks}"


# ---------------------------------------------------------------------------
# Test: inject_sidecar propagates task_type from category
# ---------------------------------------------------------------------------

class TestInjectSidecarPropagatesTaskType:
    """The beellama driver's inject_sidecar must propagate task_type from
    the sidecar's category so research tasks get the right handler."""

    def test_category_research_sets_task_type_research(self):
        """Simulate inject_sidecar: category='research' -> task_type='research'."""
        task = _make_task(task_type="implementation")  # default from parse_prd
        # Simulate what inject_sidecar does after the fix
        category = "research"
        from engine.prd import CATEGORY_TO_TYPE
        task.task_type = CATEGORY_TO_TYPE.get(category, "implementation")
        assert task.task_type == "research"
        handler = _resolve_handler(task, None)
        assert not isinstance(handler, CodeTaskHandler)

    def test_category_api_sets_task_type_implementation(self):
        """category='api' -> task_type='implementation' (code path)."""
        task = _make_task(task_type="implementation")
        category = "api"
        from engine.prd import CATEGORY_TO_TYPE
        task.task_type = CATEGORY_TO_TYPE.get(category, "implementation")
        assert task.task_type == "implementation"
        handler = _resolve_handler(task, None)
        assert isinstance(handler, CodeTaskHandler)
