"""End-to-end verification: a minimal research engine.run() with mock
transport producing a valid cited verdict.md completes WITHOUT the ast error.

This is the actual beellama-dogfood scenario reproduced in-process:
- PRD parses to a research task (task_type='research')
- Mock transport returns a valid cited verdict (markdown with em-dashes, numbers)
- Engine.run() routes to ResearchTaskHandler (NOT code-atom pipeline)
- EvidenceValidator validates the verdict (no ast.parse)
- Run completes successfully
"""

import json
import os
import sys
import tempfile
import shutil
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.engine import Engine
from engine.research.sources import _verify_span_match


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A valid cited verdict with em-dashes and numbers (the beellama failure mode).
VALID_VERDICT_MD = """\
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


def _make_mock_transport():
    """Create a mock transport that returns the valid verdict for the
    generate call and a valid JSON score for the scoring call."""
    t = MagicMock()

    def _curl_beellama(port, messages, max_tokens=512, temperature=0.3):
        # The scoring call asks for JSON scores; the generate call asks for
        # a cited verdict.  Distinguish by inspecting the prompt content.
        prompt_text = messages[0].get("content", "") if messages else ""
        if "evidence quality judge" in prompt_text or "JSON object" in prompt_text:
            # Scoring call — return a JSON score response.
            return {
                "content": json.dumps({
                    "citation_coverage": 8,
                    "claim_traceability": 7,
                    "contradiction_handling": 8,
                    "verdict_justification": 7,
                    "source_quality": 8,
                    "reasoning": "Well-cited verdict with clear claim-source edges.",
                }),
                "total_tokens": 300,
                "thinking_tokens": 0,
                "finish_reason": "stop",
                "reasoning_content": "",
                "prompt_tokens": 200,
                "completion_tokens": 100,
            }
        # Generate call — return the cited verdict.
        return {
            "content": VALID_VERDICT_MD,
            "total_tokens": 500,
            "thinking_tokens": 0,
            "finish_reason": "stop",
            "reasoning_content": "",
            "prompt_tokens": 200,
            "completion_tokens": 300,
        }

    t.curl_beellama.side_effect = _curl_beellama
    t.check_beellama_health.return_value = True
    t.get_model_list.return_value = ["mock-model"]
    t.run_command.return_value = ("", "", 0)

    # run_git: return (stdout, stderr, rc).  rev-parse returns a fake SHA.
    _fake_sha = "a" * 40

    def _run_git(project_path, *args):
        if args[0] == "rev-parse":
            return (_fake_sha, "", 0)
        if args[0] == "commit":
            # nothing to commit → rc=1 with "nothing to commit" in stderr
            # (the committer treats this as success).
            return ("", "nothing to commit, working tree clean", 1)
        return ("", "", 0)

    t.run_git.side_effect = _run_git
    return t


def _make_research_prd_and_sidecar(tmpdir):
    """Write a PRD + sidecar JSON that parses to a research task with
    module='verdicts/R01-verdict.md' (the beellama scenario)."""
    prd_dir = os.path.join(tmpdir, ".run-prds")
    os.makedirs(prd_dir, exist_ok=True)
    prd_path = os.path.join(prd_dir, "PRD-R01.md")
    sidecar_path = os.path.join(prd_dir, "PRD-R01.json")

    # PRD content — parsed by regex fallback in parse_prd.
    # The description must have enough keyword overlap with the verdict text
    # to pass EvidenceValidator stage 5 (TF-IDF cosine >= 0.15).
    # NOTE: only ONE ## Task header — additional ## headers would be parsed
    # as separate tasks by the regex fallback in parse_prd.
    prd_text = (
        "# PRD: Branch selection research\n\n"
        "## Task R01: Branch selection — which upstream base should the port track\n\n"
        "Research question: Which upstream base should the port track — "
        "the v0.4.6 release tag, the preview-v0.4.7 tag (53a68d3c), or a pinned merge commit — "
        "given 220 commits of drift since the local merge-base and "
        "310 changed lines in llama-graph.cpp?\n\n"
        "This is a RESEARCH task. The engine writes the verdict to: "
        "verdicts/R01-verdict.md\n"
        "Cite sources as [src-N] with file:// spans inside the "
        "dogfood-sources/ files ONLY.\n"
    )
    open(prd_path, "w").write(prd_text)

    # Sidecar JSON — inject_sidecar reads this to set files/module/category
    json.dump({
        "id": "R01",
        "files": ["verdicts/R01-verdict.md"],
        "module": "verdicts/R01-verdict.md",
        "acceptance_criteria": [
            "verdict.md has ## Verdict section",
            "every claim cites [src-N] with file:// span",
        ],
        "dependencies": [],
        "category": "research",
        "title": "Branch selection research",
        "description": (
            "Which upstream base should the port track given 220 commits "
            "of drift and 310 changed lines in llama-graph.cpp?"),
    }, open(sidecar_path, "w"), indent=1)

    return prd_dir


def _make_project_with_sources(tmpdir):
    """Create a minimal project root with dogfood-sources/ files so the
    file:// span verification in the research handler resolves."""
    src_dir = os.path.join(tmpdir, "dogfood-sources")
    os.makedirs(src_dir, exist_ok=True)
    # The citation spans in VALID_VERDICT_MD must match these file contents
    # (the research handler's verify_file_sources checks span membership).
    open(os.path.join(src_dir, "upstream-drift.log"), "w").write(
        "220 commits between merge-base and main per upstream-drift.log\n")
    open(os.path.join(src_dir, "upstream-drift-stat.txt"), "w").write(
        "310 changed lines in llama-graph.cpp per drift-stat.txt\n")
    # verdicts dir for the output
    os.makedirs(os.path.join(tmpdir, "verdicts"), exist_ok=True)


# ---------------------------------------------------------------------------
# Test: end-to-end research run completes without ast error
# ---------------------------------------------------------------------------

class TestEndToEndResearchRun:
    """Reproduce the beellama-dogfood scenario: research task with .md verdict
    file completes via ResearchTaskHandler (not code-atom pipeline)."""

    def test_research_run_completes_without_ast_error(self):
        """The core regression: engine.run() on a research task with a .md
        verdict file must complete WITHOUT the ast.parse error."""
        tmpdir = tempfile.mkdtemp(prefix="ace-research-e2e-")
        # Mock Gitea repo existence check (offline test).
        self._gitea_patch = patch("engine.committer._gitea_repo_exists",
                                   lambda u, r: True)
        self._gitea_patch.start()
        try:
            prd_dir = _make_research_prd_and_sidecar(tmpdir)
            _make_project_with_sources(tmpdir)
            prd_path = os.path.join(prd_dir, "PRD-R01.md")

            transport = _make_mock_transport()
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080,
                judge_mode="off",  # skip LLM judge scoring
                max_llm_calls=5, max_total_tokens=100_000,
                max_wall_clock_s=60, archive_results=False,
                enable_memory_recall=False,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            # Patch parse_prd to inject sidecar (like the beellama driver)
            import engine.engine as eng_mod
            original_parse = eng_mod.parse_prd

            def patched_parse(p):
                tasks = original_parse(p)
                for tt in tasks:
                    # inject_sidecar logic (now includes task_type propagation)
                    sidecar_path = p.replace(".md", ".json")
                    if os.path.exists(sidecar_path):
                        sidecar = json.load(open(sidecar_path))
                        if sidecar.get("files"):
                            tt.files = sidecar["files"]
                            tt.module = sidecar["module"]
                        if sidecar.get("acceptance_criteria"):
                            tt.acceptance_criteria = sidecar["acceptance_criteria"]
                        category = sidecar.get("category", "")
                        if category:
                            from engine.prd import CATEGORY_TO_TYPE
                            tt.task_type = CATEGORY_TO_TYPE.get(category, "implementation")
                return tasks

            eng_mod.parse_prd = patched_parse
            try:
                result = engine.run(prd_path, tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original_parse

            # The run must succeed (not fail with ast error)
            assert result.success, (
                f"Research run must succeed; got success={result.success}, "
                f"error={result.error_message}")
            assert len(result.tasks) > 0, "Must have at least one task"

            # The task must be in COMMIT state (research handler returns COMMIT)
            task_result = result.tasks[0]
            assert task_result.state == "COMMIT", (
                f"Research task should reach COMMIT state; "
                f"got state={task_result.state}, error={task_result.error_message}")

            # Round-3 regression: the task MUST have a commit_sha (the bug was
            # that the research handler wrote the verdict but never committed).
            assert task_result.commit_sha is not None, (
                f"Research task must have commit_sha after commit; "
                f"got commit_sha={task_result.commit_sha}")

            # The verdict file must exist and contain the cited verdict.
            verdict_path = os.path.join(tmpdir, "verdicts", "R01-verdict.md")
            assert os.path.exists(verdict_path), (
                f"Verdict file must exist at {verdict_path}")
            content = open(verdict_path).read()
            assert "## Verdict" in content, "Verdict file must have ## Verdict section"
            assert "ACE_STUB" not in content, "Verdict file must not be a stub"

        finally:
            self._gitea_patch.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_research_handler_validates_with_evidence_not_ast(self):
        """Directly verify the ResearchTaskHandler uses EvidenceValidator
        (not the code validator with ast.parse)."""
        from engine.research import ResearchTaskHandler
        from engine.research.evidence import EvidenceValidator

        task = Task(
            id="R01",
            title="Branch selection research",
            description="Which upstream base should the port track?",
            module="verdicts/R01-verdict.md",
            task_type="research",
            files=["verdicts/R01-verdict.md"],
        )

        # EvidenceValidator must accept the verdict
        ev = EvidenceValidator()
        result = ev.validate(
            VALID_VERDICT_MD,
            question="Which upstream base should the port track given 220 commits of drift?")
        assert result.passed, (
            f"EvidenceValidator must pass valid cited verdict; checks={result.checks}")

        # The code validator (ast) must reject it — proving the failure mode
        from engine.validator import check_ast
        ast_passed, ast_error = check_ast(VALID_VERDICT_MD)
        assert not ast_passed, (
            "ast.parse must reject markdown verdict (this is the bug we avoid)")


class TestResearchCommitShaRound3:
    """Round-3 regression: research handler must commit artifacts and set commit_sha.

    The bug: R01/R04/R07 produced real well-formed verdicts (content_ok='ok')
    but the run marked them FAILED because commit_sha was None — the research
    handler wrote the verdict file but never committed it.
    """

    def test_research_run_produces_commit_sha(self, monkeypatch):
        # Mock Gitea repo existence check (offline test).
        monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: True)
        tmpdir = tempfile.mkdtemp(prefix="ace-research-commit-")
        """A research atom whose model output mirrors the real R01 verdict must
        complete engine.run() with a non-null commit_sha AND pass EvidenceValidator."""
        import subprocess
        tmpdir = tempfile.mkdtemp(prefix="ace-research-commit-")
        try:
            prd_dir = _make_research_prd_and_sidecar(tmpdir)
            _make_project_with_sources(tmpdir)
            prd_path = os.path.join(prd_dir, "PRD-R01.md")

            transport = _make_mock_transport()
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080,
                judge_mode="off",
                max_llm_calls=5, max_total_tokens=100_000,
                max_wall_clock_s=60, archive_results=False,
                enable_memory_recall=False,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original_parse = eng_mod.parse_prd

            def patched_parse(p):
                tasks = original_parse(p)
                for tt in tasks:
                    sidecar_path = p.replace(".md", ".json")
                    if os.path.exists(sidecar_path):
                        sidecar = json.load(open(sidecar_path))
                        if sidecar.get("files"):
                            tt.files = sidecar["files"]
                            tt.module = sidecar["module"]
                        category = sidecar.get("category", "")
                        if category:
                            from engine.prd import CATEGORY_TO_TYPE
                            tt.task_type = CATEGORY_TO_TYPE.get(category, "implementation")
                return tasks

            eng_mod.parse_prd = patched_parse
            try:
                result = engine.run(prd_path, tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original_parse

            # Run must succeed
            assert result.success, (
                f"Research run must succeed; got success={result.success}, "
                f"error={result.error_message}")

            task_result = result.tasks[0]

            # Must be COMMIT state
            assert task_result.state == "COMMIT", (
                f"Expected COMMIT; got {task_result.state}, "
                f"error={task_result.error_message}")

            # KEY REGRESSION: commit_sha must be set (was None in round 2)
            assert task_result.commit_sha is not None, (
                f"commit_sha must not be None after research commit; "
                f"error={task_result.error_message}")
            assert len(task_result.commit_sha) == 40, (
                f"commit_sha must be a 40-char SHA; got {task_result.commit_sha!r}")

            # Verdict file must exist and be real (not stub)
            verdict_path = os.path.join(tmpdir, "verdicts", "R01-verdict.md")
            assert os.path.exists(verdict_path)
            content = open(verdict_path).read()
            assert "## Verdict" in content
            assert "ACE_STUB" not in content

            # Evidence sidecar JSON must exist
            sidecar_path = os.path.join(tmpdir, "verdicts", "R01-verdict.json")
            assert os.path.exists(sidecar_path), (
                f"Evidence sidecar must exist at {sidecar_path}")
            sidecar = json.load(open(sidecar_path))
            assert sidecar["verdict"] == "supported"
            assert sidecar["confidence"] == 0.85
            assert len(sidecar["claims"]) >= 2

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Diff-marker span accommodation (round-3 fix for R02/R03/R06/R08)
# ---------------------------------------------------------------------------

class TestDiffMarkerSpanMatching:
    """Models copying spans from .diff files often drop or mangle the
    '+', '-', '+++ b/path', '@@ ... @@' line prefixes.  The span matcher
    must accommodate this during normalized comparison."""

    def test_span_with_plus_marker_matches_diff(self, tmp_path):
        """A span prefixed with '+' must match the corresponding diff line."""
        diff_file = tmp_path / "test.diff"
        diff_file.write_text(
            "diff --git a/src/foo.cpp b/src/foo.cpp\n"
            "index 0000001..0000002 100644\n"
            "--- a/src/foo.cpp\n"
            "+++ b/src/foo.cpp\n"
            "@@ -1,3 +1,3 @@\n"
            " context line\n"
            "-removed line\n"
            "+added line here\n"
            " another context\n"
        )
        # Span with '+' prefix (model copied verbatim from diff).
        assert _verify_span_match(
            str(diff_file), "+added line here", str(tmp_path))
        # Span WITHOUT '+' prefix (model dropped the marker).
        assert _verify_span_match(
            str(diff_file), "added line here", str(tmp_path))

    def test_span_with_minus_marker_matches_diff(self, tmp_path):
        """A span prefixed with '-' must match the corresponding diff line."""
        diff_file = tmp_path / "test.diff"
        diff_file.write_text(
            "diff --git a/src/bar.cpp b/src/bar.cpp\n"
            "--- a/src/bar.cpp\n"
            "+++ b/src/bar.cpp\n"
            "@@ -1 +1 @@\n"
            "-old implementation\n"
            "+new implementation\n"
        )
        assert _verify_span_match(
            str(diff_file), "-old implementation", str(tmp_path))
        assert _verify_span_match(
            str(diff_file), "old implementation", str(tmp_path))

    def test_non_diff_file_not_affected(self, tmp_path):
        """For non-diff files, the diff-marker stripping must NOT be applied
        (no false positives from stripping '+' from regular text)."""
        normal_file = tmp_path / "notes.txt"
        normal_file.write_text(
            "Some regular text without diff content.\n"
            "Another line of plain text.\n"
        )
        # Exact substring match works.
        assert _verify_span_match(
            str(normal_file), "regular text without", str(tmp_path))
        # A span that would only match after stripping a leading '+' from
        # normal text should NOT match (this is not a diff file, so the
        # diff-marker accommodation does not apply).
        assert not _verify_span_match(
            str(normal_file), "+ regular text", str(tmp_path))



