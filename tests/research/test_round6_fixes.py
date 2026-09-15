"""Regression tests for round-6 beellama dogfood fixes.

Run 6 went 0/8 (including 4 previously-working atoms) because source
verification failed: the 9B model paraphrases evidence spans, so citation
spans like "preview-v0.4.7 tag" don't verbatim-match the source files
(which contain raw git logs / diffs, not synthesized facts).

Root causes fixed:
  1. _collect_file_sources had a DOTALL regex bug — only src-1 was ever
     collected because (.*) with DOTALL greedily matched to end of section.
  2. _collect_file_sources only handled file:// URIs, not bare relative
     paths the model sometimes emits.
  3. _repair_verdict_output added placeholder citation spans
     ("placeholder evidence span") that fail source verification.  Now it
     extracts REAL spans from the cited source files.
  4. _repair_verdict_output now also repairs the model's paraphrased spans
     by replacing them with real excerpts from the source files.
  5. _commit_git: identical-content re-commit ("nothing to commit") returns
     success with the existing HEAD sha instead of falling through to a
     redundant push.
"""

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig
from engine.committer import commit_research_artifacts
from engine.research.evidence import EvidenceValidator
from engine.research.sources import verify_file_sources


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A source file with real content the model might try to cite paraphrased.
DRIFT_LOG_CONTENT = (
    "78af83265 Merge branch 'v0.4.6'\n"
    "881d60bb2 Update README.md\n"
    "e6c87dfa8 Updated documentation\n"
    "470ae8e5e Fix v0.4.6 release metadata\n"
    "22376955c Merge upstream llama.cpp through 465e49b9c\n"
)

DRIFT_STAT_CONTENT = (
    " src/llama-graph.cpp                        |   310 +-\n"
    " src/llama.cpp                              |    45 +-\n"
    " common/arg.cpp                             |   157 +-\n"
)

DIFF_CONTENT = (
    "diff --git a/src/llama-graph.cpp b/src/llama-graph.cpp\n"
    "index abc123..def456 100644\n"
    "--- a/src/llama-graph.cpp\n"
    "+++ b/src/llama-graph.cpp\n"
    "@@ -100,5 +100,5 @@\n"
    " build_moe_ffn(expert_config);\n"
    "+expert-path caching layer;\n"
    " expert_GEMM_dispatch(weights);\n"
)


def _make_project(tmpdir, sources=None):
    """Create a minimal project with dogfood-sources and verdicts dir."""
    src_dir = os.path.join(tmpdir, "dogfood-sources")
    os.makedirs(src_dir, exist_ok=True)
    sources = sources or {}
    for name, content in sources.items():
        open(os.path.join(src_dir, name), "w").write(content)
    os.makedirs(os.path.join(tmpdir, "verdicts"), exist_ok=True)


@pytest.fixture(autouse=True)
def _mock_gitea(monkeypatch):
    """Mock Gitea repo existence check for all tests."""
    monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: True)


# ---------------------------------------------------------------------------
# Test: _collect_file_sources DOTALL regex bug
# ---------------------------------------------------------------------------

class TestCollectFileSourcesDotallFix:
    """_collect_file_sources must collect ALL sources, not just src-1."""

    def test_collects_all_three_sources(self):
        from engine.research import ResearchTaskHandler
        verdict = (
            "## Verdict\n\nPosition: supported\nConfidence: 0.85\n"
            "Reasoning here.\n\n"
            "## Sources\n\n"
            "- [src-1] type: file uri: file://dogfood-sources/a.log title: A\n"
            "- [src-2] type: file uri: file://dogfood-sources/b.log title: B\n"
            "- [src-3] type: file uri: file://dogfood-sources/c.log title: C\n\n"
            "## Claims\n\n- [claim-1] claim [src-1][src-2][src-3]\n\n"
            "## Contradictions\n\nNone identified.\n\n"
            "## Citations\n\n"
            '[claim:1, src:1, "span a", 0.9]\n'
            '[claim:1, src:2, "span b", 0.9]\n'
            '[claim:1, src:3, "span c", 0.9]\n'
        )
        ev = EvidenceValidator().validate(verdict, question="test question")
        handler = ResearchTaskHandler.__new__(ResearchTaskHandler)
        file_sources = handler._collect_file_sources(ev)
        assert len(file_sources) == 3, (
            f"Expected 3 file sources, got {len(file_sources)}")
        paths = [fs["path"] for fs in file_sources]
        assert "dogfood-sources/a.log" in paths
        assert "dogfood-sources/b.log" in paths
        assert "dogfood-sources/c.log" in paths

    def test_collects_bare_path_sources(self):
        """Bare relative paths (no file://) must also be collected."""
        from engine.research import ResearchTaskHandler
        verdict = (
            "## Verdict\n\nPosition: supported\nConfidence: 0.85\n"
            "Reasoning.\n\n"
            "## Sources\n\n"
            "- [src-1] type: file uri: dogfood-sources/a.log title: A\n"
            "- [src-2] type: file uri: dogfood-sources/b.log title: B\n\n"
            "## Claims\n\n- [claim-1] claim [src-1][src-2]\n\n"
            "## Contradictions\n\nNone identified.\n\n"
            "## Citations\n\n"
            '[claim:1, src:1, "span a", 0.9]\n'
            '[claim:1, src:2, "span b", 0.9]\n'
        )
        ev = EvidenceValidator().validate(verdict, question="test question")
        handler = ResearchTaskHandler.__new__(ResearchTaskHandler)
        file_sources = handler._collect_file_sources(ev)
        assert len(file_sources) == 2

    def test_skips_http_placeholder_sources(self):
        """http/https URIs (repair placeholders) must be skipped."""
        from engine.research import ResearchTaskHandler
        verdict = (
            "## Verdict\n\nPosition: supported\nConfidence: 0.85\n"
            "Reasoning.\n\n"
            "## Sources\n\n"
            "- [src-1] type: web uri: https://example.com/placeholder title: Placeholder\n\n"
            "## Claims\n\n- [claim-1] claim [src-1]\n\n"
            "## Contradictions\n\nNone identified.\n\n"
            "## Citations\n\n"
            '[claim:1, src:1, "placeholder evidence span", 0.5]\n'
        )
        ev = EvidenceValidator().validate(verdict, question="test question")
        handler = ResearchTaskHandler.__new__(ResearchTaskHandler)
        file_sources = handler._collect_file_sources(ev)
        assert len(file_sources) == 0


# ---------------------------------------------------------------------------
# Test: citation span repair (paraphrased spans -> real spans)
# ---------------------------------------------------------------------------

class TestCitationSpanRepair:
    """_repair_verdict_output must replace paraphrased spans with real ones."""

    def test_paraphrased_span_replaced_with_real_span(self):
        from engine.research import ResearchTaskHandler
        tmpdir = tempfile.mkdtemp(prefix="ace-span-repair-")
        try:
            _make_project(tmpdir, {"a.log": DRIFT_LOG_CONTENT})
            verdict = (
                "## Verdict\n\nPosition: supported\nConfidence: 0.85\n"
                "Reasoning.\n\n"
                "## Sources\n\n"
                "- [src-1] type: file uri: file://dogfood-sources/a.log title: A\n\n"
                "## Claims\n\n- [claim-1] claim [src-1]\n\n"
                "## Contradictions\n\nNone identified.\n\n"
                "## Citations\n\n"
                '[claim:1, src:1, "preview-v0.4.7 tag at commit 53a68d3c", 0.95]\n'
            )
            repaired = ResearchTaskHandler._repair_verdict_output(
                verdict, project_path=tmpdir)
            # The paraphrased span must be replaced with a real span.
            assert "preview-v0.4.7 tag" not in repaired
            # The replacement span must actually exist in the source file.
            ev = EvidenceValidator().validate(repaired, question="test")
            handler = ResearchTaskHandler.__new__(ResearchTaskHandler)
            file_sources = handler._collect_file_sources(ev)
            assert len(file_sources) == 1
            results = verify_file_sources(
                file_sources, project_root=tmpdir, sampled=True)
            assert all(r.status.value == "passed" for r in results), (
                f"Source verification must pass after span repair; "
                f"results={[(r.source, r.status.value) for r in results]}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_matching_span_left_untouched(self):
        """A span that already matches the source must not be modified."""
        from engine.research import ResearchTaskHandler
        tmpdir = tempfile.mkdtemp(prefix="ace-span-ok-")
        try:
            _make_project(tmpdir, {"a.log": DRIFT_LOG_CONTENT})
            real_span = "78af83265 Merge branch 'v0.4.6'"
            verdict = (
                "## Verdict\n\nPosition: supported\nConfidence: 0.85\n"
                "Reasoning.\n\n"
                "## Sources\n\n"
                "- [src-1] type: file uri: file://dogfood-sources/a.log title: A\n\n"
                "## Claims\n\n- [claim-1] claim [src-1]\n\n"
                "## Contradictions\n\nNone identified.\n\n"
                "## Citations\n\n"
                f'[claim:1, src:1, "{real_span}", 0.95]\n'
            )
            repaired = ResearchTaskHandler._repair_verdict_output(
                verdict, project_path=tmpdir)
            assert real_span in repaired
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_placeholder_citation_gets_real_span(self):
        """When repair adds placeholder Citations, the span must be real."""
        from engine.research import ResearchTaskHandler
        tmpdir = tempfile.mkdtemp(prefix="ace-placeholder-span-")
        try:
            _make_project(tmpdir, {
                "a.log": DRIFT_LOG_CONTENT,
                "b.log": DRIFT_STAT_CONTENT,
            })
            # Verdict missing Claims and Citations, with file:// sources.
            verdict = (
                "## Verdict\n\nPosition: supported\nConfidence: 0.85\n"
                "Reasoning.\n\n"
                "## Sources\n\n"
                "- [src-1] type: file uri: file://dogfood-sources/a.log title: A\n"
                "- [src-2] type: file uri: file://dogfood-sources/b.log title: B\n\n"
                "## Contradictions\n\nNone identified.\n"
            )
            repaired = ResearchTaskHandler._repair_verdict_output(
                verdict, project_path=tmpdir)
            # The placeholder citation must NOT contain the old fabricated span.
            assert "placeholder evidence span" not in repaired
            # Source verification must pass on the repaired verdict.
            ev = EvidenceValidator().validate(repaired, question="test question")
            handler = ResearchTaskHandler.__new__(ResearchTaskHandler)
            file_sources = handler._collect_file_sources(ev)
            if file_sources:
                results = verify_file_sources(
                    file_sources, project_root=tmpdir, sampled=True)
                assert all(r.status.value != "failed" for r in results), (
                    f"Placeholder citation span must verify; "
                    f"results={[(r.source, r.status.value) for r in results]}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test: committer identical-content re-commit returns success with existing sha
# ---------------------------------------------------------------------------

class TestCommitterIdenticalContent:
    """commit_research_artifacts must treat 'nothing to commit' as success."""

    def test_nothing_to_commit_returns_success_with_sha(self, monkeypatch):
        """When git commit says 'nothing to commit', return success + HEAD sha."""
        tmpdir = tempfile.mkdtemp(prefix="ace-commit-idemp-")
        try:
            _make_project(tmpdir, {"a.log": DRIFT_LOG_CONTENT})
            # Initialize a git repo with an initial commit so HEAD exists.
            import subprocess
            subprocess.run(["git", "init"], cwd=tmpdir,
                           capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "<EMAIL>"],
                           cwd=tmpdir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "test"],
                           cwd=tmpdir, capture_output=True, check=True)
            subprocess.run(["git", "add", "."], cwd=tmpdir,
                           capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmpdir,
                           capture_output=True, check=True)
            sha_out = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=tmpdir,
                capture_output=True, text=True, check=True)
            existing_sha = sha_out.stdout.strip()

            # Mock transport that simulates "nothing to commit" and a
            # successful push of the existing commit.
            transport = MagicMock()
            transport.check_beellama_health.return_value = True

            def _run_git(project_path, *args):
                if args[0] == "checkout":
                    return ("", "", 0)
                if args[0] == "add":
                    return ("", "", 0)
                if args[0] == "commit":
                    # Simulate identical content — nothing to commit.
                    return ("", "nothing to commit, working tree clean", 1)
                if args[0] == "remote":
                    return ("", "", 0)
                if args[0] == "push":
                    return ("", "", 0)
                if args[0] == "rev-parse":
                    return (existing_sha, "", 0)
                return ("", "", 0)

            transport.run_git.side_effect = _run_git

            # Monkeypatch _build_origin_url to return a dummy URL.
            monkeypatch.setattr("engine.committer._build_origin_url",
                                lambda p: "http://dummy/x/y")

            task = MagicMock()
            task.module = "verdicts/R01-verdict.md"
            task.title = "Test"
            task.id = "R01"

            result = commit_research_artifacts(
                project_path=tmpdir,
                task=task,
                files={"verdicts/R01-verdict.md": "some content"},
                transport=transport,
                run_id="run-12345",
            )
            assert result.success, (
                f"Identical-content re-commit must succeed; "
                f"error={result.error_message}")
            assert result.sha == existing_sha, (
                f"commit_sha must be the existing HEAD; got {result.sha}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test: end-to-end — verdict with paraphrased file:// spans commits
# ---------------------------------------------------------------------------

class TestEndToEndParaphrasedSpansCommit:
    """Full pipeline: model emits paraphrased file:// spans -> repair -> commit.

    This is the exact run-6 failure mode: the 9B model produces a verdict
    with file:// URIs but paraphrased evidence spans that don't match the
    source files.  After the repair step fixes the spans, the atom must
    commit successfully.
    """

    def _run_atom(self, tmpdir, task_id, title, question, module,
                  verdict_sequence, sources):
        _make_project(tmpdir, sources)
        prd_dir = os.path.join(tmpdir, ".run-prds")
        os.makedirs(prd_dir, exist_ok=True)
        prd_path = os.path.join(prd_dir, f"PRD-{task_id}.md")
        sidecar_path = os.path.join(prd_dir, f"PRD-{task_id}.json")
        prd_text = (
            f"# PRD: {title}\n\n"
            f"## Task {task_id}: {title}\n\n"
            f"{question}\n\n"
            f"This is a RESEARCH task. The engine writes the verdict to: {module}\n"
            f"Cite sources as [src-N] with file:// spans.\n"
        )
        open(prd_path, "w").write(prd_text)
        json.dump({
            "id": task_id, "files": [module], "module": module,
            "acceptance_criteria": ["verdict.md has ## Verdict section"],
            "dependencies": [], "category": "research",
            "title": title, "description": question,
        }, open(sidecar_path, "w"), indent=1)

        transport = MagicMock()
        transport.check_beellama_health.return_value = True
        transport.get_model_list.return_value = ["mock-model"]
        transport.run_command.return_value = ("", "", 0)
        _fake_sha = "a" * 40

        def _run_git(project_path, *args):
            if args[0] == "rev-parse":
                return (_fake_sha, "", 0)
            if args[0] == "commit":
                return ("", "", 0)
            return ("", "", 0)

        transport.run_git.side_effect = _run_git

        _calls = {"gen_idx": 0}

        def _curl_beellama(port, messages, max_tokens=512, temperature=0.3):
            prompt_text = messages[0].get("content", "") if messages else ""
            if "evidence quality judge" in prompt_text or "JSON object" in prompt_text:
                return {
                    "content": json.dumps({
                        "citation_coverage": 8, "claim_traceability": 7,
                        "contradiction_handling": 8, "verdict_justification": 7,
                        "source_quality": 8, "reasoning": "ok"}),
                    "total_tokens": 300, "thinking_tokens": 0,
                    "finish_reason": "stop", "reasoning_content": "",
                    "prompt_tokens": 200, "completion_tokens": 100,
                }
            idx = _calls["gen_idx"] % len(verdict_sequence)
            _calls["gen_idx"] += 1
            return {
                "content": verdict_sequence[idx],
                "total_tokens": 500, "thinking_tokens": 0,
                "finish_reason": "stop", "reasoning_content": "",
                "prompt_tokens": 200, "completion_tokens": 300,
            }

        transport.curl_beellama.side_effect = _curl_beellama

        cfg = EngineConfig(
            subject_port=8082, judge_port=8080, judge_mode="off",
            max_llm_calls=10, max_total_tokens=200_000,
            max_wall_clock_s=120, archive_results=False,
            enable_memory_recall=False, max_research_generate=5,
            enforce_research_sources=False)  # mock transport: no real sources
        from engine.engine import Engine
        engine = Engine(transport=transport, config=cfg)

        import engine.engine as eng_mod
        original = eng_mod.parse_prd

        def patched(p):
            tasks = original(p)
            for tt in tasks:
                sc_path = p.replace(".md", ".json")
                if os.path.exists(sc_path):
                    sc = json.load(open(sc_path))
                    if sc.get("files"):
                        tt.files = sc["files"]
                        tt.module = sc["module"]
                    cat = sc.get("category", "")
                    if cat:
                        from engine.prd import CATEGORY_TO_TYPE
                        tt.task_type = CATEGORY_TO_TYPE.get(cat, "implementation")
            return tasks

        eng_mod.parse_prd = patched
        try:
            result = engine.run(prd_path, tmpdir, config=cfg)
        finally:
            eng_mod.parse_prd = original
        return result

    def test_paraphrased_file_spans_commit(self):
        """Verdict with file:// URIs + paraphrased spans must commit after repair."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r6-span-")
        try:
            # Model emits a verdict with file:// URIs but paraphrased spans
            # (the run-6 failure mode).  The question shares keywords with
            # the verdict so the answer_question TF-IDF check passes.
            model_verdict = (
                "## Verdict\n\n"
                "Position: supported\n"
                "Confidence: 0.85\n"
                "The port should track preview-v0.4.7 because it captures the "
                "latest expert-path work and 220 commits of drift while avoiding "
                "the instability of main.\n\n"
                "## Sources\n\n"
                "- [src-1] type: file uri: file://dogfood-sources/upstream-drift.log title: Drift Log\n"
                "- [src-2] type: file uri: file://dogfood-sources/upstream-drift-stat.txt title: Drift Stat\n\n"
                "## Claims\n\n"
                "- [claim-1] Upstream provides a preview-v0.4.7 tag [src-1]\n"
                "- [claim-2] llama-graph.cpp has drifted 310 changed lines [src-2]\n\n"
                "## Contradictions\n\n"
                "None identified.\n\n"
                "## Citations\n\n"
                '[claim:1, src:1, "preview-v0.4.7 tag at commit 53a68d3c", 0.95]\n'
                '[claim:2, src:2, "310 changed lines in llama-graph.cpp", 0.9]\n'
            )
            result = self._run_atom(
                tmpdir, "R01", "Branch selection",
                "Which upstream base should the beellama port track "
                "given 220 commits of drift since the local merge-base "
                "and active expert-path work upstream?",
                "verdicts/R01-verdict.md",
                verdict_sequence=[model_verdict],
                sources={
                    "upstream-drift.log": DRIFT_LOG_CONTENT,
                    "upstream-drift-stat.txt": DRIFT_STAT_CONTENT,
                })
            assert result.success, (
                f"Atom with paraphrased spans must commit after repair; "
                f"success={result.success}, error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
            assert result.tasks[0].commit_sha is not None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_bare_path_verdict_commits(self):
        """Verdict with bare-path sources (run-5 style) must still commit."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r6-bare-")
        try:
            model_verdict = (
                "## Verdict\n\n"
                "Position: supported\n"
                "Confidence: 0.85\n"
                "The port should track preview-v0.4.7 because it captures the "
                "latest expert-path work and 220 commits of drift.\n\n"
                "## Sources\n\n"
                "- [src-1] type: file uri: dogfood-sources/upstream-drift.log title: Drift Log\n"
                "- [src-2] type: file uri: dogfood-sources/upstream-drift-stat.txt title: Drift Stat\n\n"
                "## Claims\n\n"
                "- [claim-1] Upstream provides a preview-v0.4.7 tag [src-1]\n"
                "- [claim-2] llama-graph.cpp drifted 310 lines [src-2]\n\n"
                "## Contradictions\n\n"
                "None identified.\n\n"
                "## Citations\n\n"
                '[claim:1, src:1, "preview-v0.4.7 tag", 0.95]\n'
                '[claim:2, src:2, "310 changed lines", 0.9]\n'
            )
            result = self._run_atom(
                tmpdir, "R01", "Branch selection",
                "Which upstream base should the beellama port track "
                "given 220 commits of drift since the local merge-base "
                "and active expert-path work upstream?",
                "verdicts/R01-verdict.md",
                verdict_sequence=[model_verdict],
                sources={
                    "upstream-drift.log": DRIFT_LOG_CONTENT,
                    "upstream-drift-stat.txt": DRIFT_STAT_CONTENT,
                })
            assert result.success, (
                f"Bare-path verdict must commit; "
                f"success={result.success}, error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
