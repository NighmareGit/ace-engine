"""Regression tests for round-2 beellama dogfood fixes.

Covers:
  - R06: research task after another research task must NOT crash with
    DONE→QUEUED illegal transition (engine.py task loop stays QUEUED).
  - Verdict file written to disk by ResearchTaskHandler (not just stub).
  - Research handler retry loop: recovers when first attempt fails validation.
  - Prompt contract: build_prompt includes concrete marker examples matching
    EvidenceValidator expectations.
"""

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.engine import Engine
from engine.research.evidence import EvidenceValidator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_VERDICT_MD = """\
## Verdict

Position: supported
Confidence: 0.85
The port should track the upstream base at preview-v0.4.7 to track the latest
expert-path work, given 220 commits of drift since the local merge-base and
310 changed lines in llama-graph.cpp. Tracking this upstream base is the
best approach to track the upstream base going forward.

## Sources

- [src-1] type: file uri: file://dogfood-sources/upstream-drift.log title: Drift Log
- [src-2] type: file uri: file://dogfood-sources/upstream-drift-stat.txt title: Drift Stat

## Claims

- [claim-1] Upstream has drifted 220 commits from merge-base [src-1]
- [claim-2] llama-graph.cpp drifted 310 changed lines [src-2]

## Contradictions

- between claims 1 and 2 resolution: complementary — different aspects of drift.

## Citations

[claim:1, src:1, "220 commits between merge-base and main", 0.9]
[claim:2, src:2, "310 changed lines in llama-graph.cpp", 0.85]
"""

INVALID_VERDICT_MD = """\
# My Research Notes

The answer is maybe. Some sources say one thing, others say another.

## Conclusion

It depends.
"""


def _make_mock_transport(verdict_sequence=None):
    """Create a mock transport.

    verdict_sequence: list of verdict texts to return in order.  If None,
    returns VALID_VERDICT_MD for generate calls.
    """
    t = MagicMock()
    t.check_beellama_health.return_value = True
    t.get_model_list.return_value = ["mock-model"]
    t.run_command.return_value = ("", "", 0)

    # run_git: rev-parse returns a fake SHA; commit returns "nothing to commit"
    # (the committer treats rc=1 with "nothing to commit" as success).
    _fake_sha = "b" * 40

    def _run_git(project_path, *args):
        if args[0] == "rev-parse":
            return (_fake_sha, "", 0)
        if args[0] == "commit":
            return ("", "nothing to commit, working tree clean", 1)
        return ("", "", 0)

    t.run_git.side_effect = _run_git

    _calls = {"gen_idx": 0}

    def _curl_beellama(port, messages, max_tokens=512, temperature=0.3):
        prompt_text = messages[0].get("content", "") if messages else ""
        if "evidence quality judge" in prompt_text or "JSON object" in prompt_text:
            return {
                "content": json.dumps({
                    "citation_coverage": 8, "claim_traceability": 7,
                    "contradiction_handling": 8, "verdict_justification": 7,
                    "source_quality": 8,
                    "reasoning": "Well-cited verdict."}),
                "total_tokens": 300, "thinking_tokens": 0,
                "finish_reason": "stop", "reasoning_content": "",
                "prompt_tokens": 200, "completion_tokens": 100,
            }
        # Generate call
        if verdict_sequence is not None:
            idx = _calls["gen_idx"] % len(verdict_sequence)
            _calls["gen_idx"] += 1
            content = verdict_sequence[idx]
        else:
            content = VALID_VERDICT_MD
        return {
            "content": content,
            "total_tokens": 500, "thinking_tokens": 0,
            "finish_reason": "stop", "reasoning_content": "",
            "prompt_tokens": 200, "completion_tokens": 300,
        }

    t.curl_beellama.side_effect = _curl_beellama
    return t


def _make_project(tmpdir, sources=None):
    """Create a minimal project with dogfood-sources and verdicts dir."""
    src_dir = os.path.join(tmpdir, "dogfood-sources")
    os.makedirs(src_dir, exist_ok=True)
    sources = sources or {}
    for name, content in sources.items():
        open(os.path.join(src_dir, name), "w").write(content)
    os.makedirs(os.path.join(tmpdir, "verdicts"), exist_ok=True)


def _make_research_prd(tmpdir, task_id, title, question, module):
    """Write a PRD + sidecar for a research task in tmpdir/.run-prds/."""
    prd_dir = os.path.join(tmpdir, ".run-prds")
    os.makedirs(prd_dir, exist_ok=True)
    assert prd_dir.endswith(".run-prds")  # sanity: no double nesting
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
    return prd_dir


def _patch_parse_prd(tmpdir):
    """Return a context manager that patches parse_prd to inject sidecar."""
    import engine.engine as eng_mod
    original = eng_mod.parse_prd

    def patched(p):
        tasks = original(p)
        for tt in tasks:
            sidecar_path = p.replace(".md", ".json")
            if os.path.exists(sidecar_path):
                sc = json.load(open(sidecar_path))
                if sc.get("files"):
                    tt.files = sc["files"]
                    tt.module = sc["module"]
                cat = sc.get("category", "")
                if cat:
                    from engine.prd import CATEGORY_TO_TYPE
                    tt.task_type = CATEGORY_TO_TYPE.get(cat, "implementation")
        return tasks

    eng_mod.parse_prd = patched
    return original, patched


# ---------------------------------------------------------------------------
# Test: R06 fix — two research tasks in sequence, no DONE→QUEUED crash
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_gitea(monkeypatch):
    """Mock Gitea repo existence check for all tests in this module."""
    monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: True)


class TestR06DoneToQueuedFix:
    """Research tasks must not trigger DONE→QUEUED illegal transition."""

    def test_two_research_tasks_no_crash(self):
        """Running two research atoms back-to-back must succeed without
        the DONE→QUEUED ValueError (R06 root cause)."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r06-")
        try:
            _make_project(tmpdir, {
                "upstream-drift.log": "220 commits between merge-base and main\n",
                "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            })
            _make_research_prd(tmpdir, "R01", "Branch selection",
                               "Which upstream base to track?",
                               "verdicts/R01-verdict.md")
            _make_research_prd(tmpdir, "R02", "Patch portability",
                               "Which portability mechanism?",
                               "verdicts/R02-verdict.md")

            # Combined PRD with both tasks
            combined_prd = os.path.join(tmpdir, ".run-prds", "PRD-combined.md")
            open(combined_prd, "w").write(
                "# PRD: Combined\n\n"
                "## Task R01: Branch selection\n\n"
                "Which upstream base to track?\n\n"
                "## Task R02: Patch portability\n\n"
                "Which portability mechanism?\n"
            )

            transport = _make_mock_transport()
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080, judge_mode="off",
                max_llm_calls=10, max_total_tokens=200_000,
                max_wall_clock_s=120, archive_results=False,
                enable_memory_recall=False,
                max_research_generate=3,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original, _ = _patch_parse_prd(tmpdir)
            try:
                # Must NOT raise ValueError("Invalid transition: DONE → QUEUED")
                result = engine.run(combined_prd, tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original

            # Both tasks should complete
            assert len(result.tasks) >= 1, "At least one task should complete"
            # The run should not crash — success depends on validation
            # but the pipeline must reach a terminal state without exception
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_research_task_pipeline_stays_queued(self):
        """After a research task returns COMMIT, pipeline stays QUEUED
        (not transitioned to DONE then back)."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r06b-")
        try:
            _make_project(tmpdir, {
                "upstream-drift.log": "220 commits between merge-base and main\n",
                "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            })
            _make_research_prd(tmpdir, "R01", "Branch selection",
                               "Which upstream base to track?",
                               "verdicts/R01-verdict.md")

            transport = _make_mock_transport()
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080, judge_mode="off",
                max_llm_calls=10, max_total_tokens=200_000,
                max_wall_clock_s=120, archive_results=False,
                enable_memory_recall=False,
                max_research_generate=3,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original, _ = _patch_parse_prd(tmpdir)
            try:
                result = engine.run(
                    os.path.join(tmpdir, ".run-prds", "PRD-R01.md"), tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original

            assert result.success, (
                f"Run should succeed; got success={result.success}, "
                f"error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
            # Round-3 regression: commit_sha must be set.
            assert result.tasks[0].commit_sha is not None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test: verdict file written to disk
# ---------------------------------------------------------------------------

class TestVerdictFileWritten:
    """Research handler must write the verdict to disk (not leave stub)."""

    def test_verdict_file_written_to_disk(self):
        """After a successful research run, the verdict file on disk must
        contain the generated verdict text (not the scaffold stub)."""
        tmpdir = tempfile.mkdtemp(prefix="ace-verdict-write-")
        try:
            _make_project(tmpdir, {
                "upstream-drift.log": "220 commits between merge-base and main\n",
                "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            })
            _make_research_prd(tmpdir, "R01", "Branch selection",
                               "Which upstream base to track?",
                               "verdicts/R01-verdict.md")

            transport = _make_mock_transport()
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080, judge_mode="off",
                max_llm_calls=10, max_total_tokens=200_000,
                max_wall_clock_s=120, archive_results=False,
                enable_memory_recall=False,
                max_research_generate=3,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original, _ = _patch_parse_prd(tmpdir)
            try:
                engine.run(
                    os.path.join(tmpdir, ".run-prds", "PRD-R01.md"), tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original

            verdict_path = os.path.join(tmpdir, "verdicts", "R01-verdict.md")
            assert os.path.exists(verdict_path), "Verdict file must exist"
            content = open(verdict_path).read()
            # Must NOT be a scaffold stub
            assert "# ACE_STUB" not in content, (
                "Verdict file must not be a scaffold stub")
            # Must contain the generated verdict
            assert "Position: supported" in content, (
                "Verdict file must contain the generated verdict")
            assert "[claim-1]" in content, (
                "Verdict file must contain claim markers")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test: research handler retry loop
# ---------------------------------------------------------------------------

class TestResearchRetry:
    """Research handler retries generation when validation fails."""

    def test_retry_recovers_from_invalid_first_attempt(self):
        """When the first verdict fails validation but a retry succeeds,
        the handler should return COMMIT."""
        tmpdir = tempfile.mkdtemp(prefix="ace-retry-")
        try:
            _make_project(tmpdir, {
                "upstream-drift.log": "220 commits between merge-base and main\n",
                "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            })
            _make_research_prd(tmpdir, "R01", "Branch selection",
                               "Which upstream base to track?",
                               "verdicts/R01-verdict.md")

            # First attempt returns invalid verdict, subsequent return valid
            transport = _make_mock_transport(
                verdict_sequence=[INVALID_VERDICT_MD, VALID_VERDICT_MD, VALID_VERDICT_MD])
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080, judge_mode="off",
                max_llm_calls=10, max_total_tokens=200_000,
                max_wall_clock_s=120, archive_results=False,
                enable_memory_recall=False,
                max_research_generate=3,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original, _ = _patch_parse_prd(tmpdir)
            try:
                result = engine.run(
                    os.path.join(tmpdir, ".run-prds", "PRD-R01.md"), tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original

            assert result.success, (
                f"Run should succeed after retry; got success={result.success}, "
                f"error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_exhausted_attempts_returns_failed(self):
        """When all attempts fail validation, the handler returns FAILED."""
        tmpdir = tempfile.mkdtemp(prefix="ace-retry-exhaust-")
        try:
            _make_project(tmpdir, {
                "upstream-drift.log": "220 commits between merge-base and main\n",
                "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            })
            _make_research_prd(tmpdir, "R01", "Branch selection",
                               "Which upstream base to track?",
                               "verdicts/R01-verdict.md")

            # All attempts return invalid verdict
            transport = _make_mock_transport(
                verdict_sequence=[INVALID_VERDICT_MD] * 5)
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080, judge_mode="off",
                max_llm_calls=10, max_total_tokens=200_000,
                max_wall_clock_s=120, archive_results=False,
                enable_memory_recall=False,
                max_research_generate=3,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original, _ = _patch_parse_prd(tmpdir)
            try:
                result = engine.run(
                    os.path.join(tmpdir, ".run-prds", "PRD-R01.md"), tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original

            assert not result.success, "Run should fail when all attempts fail"
            assert result.tasks[0].state == "FAILED"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test: prompt contract includes concrete examples
# ---------------------------------------------------------------------------

class TestPromptContract:
    """build_prompt must include concrete marker examples."""

    def test_prompt_includes_position_line_example(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        assert "Position: supported" in prompt, (
            "Prompt must show concrete Position: line example")

    def test_prompt_includes_claim_marker_example(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        assert "[claim-1]" in prompt, (
            "Prompt must show [claim-N] marker example")
        assert "[src-1]" in prompt, (
            "Prompt must show [src-N] marker example")

    def test_prompt_includes_citation_line_example(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        assert "[claim:1, src:1" in prompt, (
            "Prompt must show concrete citation line example")

    def test_prompt_includes_verdict_section_example(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        assert "## Verdict" in prompt
        assert "## Sources" in prompt
        assert "## Claims" in prompt
        assert "## Contradictions" in prompt
        assert "## Citations" in prompt


# ---------------------------------------------------------------------------
# Test: verdict heading normalization (R05 fix — ## Final Verdict → ## Verdict)
# ---------------------------------------------------------------------------

class TestVerdictHeadingNormalization:
    """_normalize_verdict_heading must rewrite close-variant headings to
    the canonical ## Verdict before validation."""

    def test_final_verdict_normalized(self):
        from engine.research import ResearchTaskHandler
        text = "## Final Verdict\n\nPosition: supported\nConfidence: 0.8\n..."
        result = ResearchTaskHandler._normalize_verdict_heading(text)
        assert "## Verdict" in result
        assert "## Final Verdict" not in result

    def test_verdict_allcaps_normalized(self):
        from engine.research import ResearchTaskHandler
        text = "## VERDICT\n\nPosition: refuted\nConfidence: 0.5\n..."
        result = ResearchTaskHandler._normalize_verdict_heading(text)
        assert "## Verdict" in result
        assert "## VERDICT" not in result

    def test_verdict_lowercase_normalized(self):
        from engine.research import ResearchTaskHandler
        text = "## verdict\n\nPosition: inconclusive\nConfidence: 0.3\n..."
        result = ResearchTaskHandler._normalize_verdict_heading(text)
        assert "## Verdict" in result

    def test_exact_verdict_unchanged(self):
        from engine.research import ResearchTaskHandler
        text = "## Verdict\n\nPosition: supported\nConfidence: 0.9\n..."
        result = ResearchTaskHandler._normalize_verdict_heading(text)
        assert result == text

    def test_final_verdict_with_triple_hash(self):
        from engine.research import ResearchTaskHandler
        text = "### Final Verdict\n\nPosition: supported\nConfidence: 0.8\n..."
        result = ResearchTaskHandler._normalize_verdict_heading(text)
        assert "### Verdict" in result
        assert "### Final Verdict" not in result

    def test_normalization_allows_evidence_validator_to_pass(self):
        """The full pipeline: a verdict with ## Final Verdict must pass
        EvidenceValidator after normalization."""
        from engine.research import ResearchTaskHandler
        from engine.research.evidence import EvidenceValidator
        verdict = """\
## Final Verdict

Position: supported
Confidence: 0.85
The port should track the upstream base at preview-v0.4.7 to track the latest
expert-path work, given 220 commits of drift since the local merge-base and
310 changed lines in llama-graph.cpp. Tracking this upstream base is the
best approach to track the upstream base going forward.

## Sources

- [src-1] type: file uri: file://dogfood-sources/upstream-drift.log title: Drift Log
- [src-2] type: file uri: file://dogfood-sources/upstream-drift-stat.txt title: Drift Stat

## Claims

- [claim-1] Upstream has drifted 220 commits from merge-base [src-1]
- [claim-2] llama-graph.cpp drifted 310 changed lines [src-2]

## Contradictions

- between claims 1 and 2 resolution: complementary — different aspects of drift.

## Citations

[claim:1, src:1, "220 commits between merge-base and main", 0.9]
[claim:2, src:2, "310 changed lines in llama-graph.cpp", 0.85]
"""
        normalized = ResearchTaskHandler._normalize_verdict_heading(verdict)
        assert "## Verdict" in normalized
        assert "## Final Verdict" not in normalized
        r = EvidenceValidator().validate(
            normalized,
            question="Which upstream base should the port track given 220 commits of drift and 310 changed lines?")
        assert r.passed, f"Normalized verdict must pass EvidenceValidator; checks={r.checks}"


# ---------------------------------------------------------------------------
# Test: inject_sidecar task_type propagation (R01/R04/R02/R06 fix)
# ---------------------------------------------------------------------------

class TestInjectSidecarTaskTypePropagation:
    """The beellama driver's inject_sidecar must set task_type from the
    sidecar's category field so research tasks route to ResearchTaskHandler."""

    def test_category_research_sets_task_type(self):
        """category='research' in sidecar -> task_type='research' on task."""
        from engine import Task
        from engine.prd import CATEGORY_TO_TYPE
        task = Task(id="R01", title="T", description="Q", module="v.md",
                    task_type="implementation")
        # Simulate the fixed inject_sidecar
        cat = "research"
        task.task_type = CATEGORY_TO_TYPE.get(cat, "implementation")
        assert task.task_type == "research"
        from engine.task_handler import _resolve_handler, CodeTaskHandler
        handler = _resolve_handler(task, None)
        assert not isinstance(handler, CodeTaskHandler)

    def test_full_research_atom_commits_with_mock_transport(self):
        """End-to-end: a research task with category='research' in sidecar
        must route through ResearchTaskHandler and produce a COMMIT with
        a commit_sha (the R01/R04 failure was: code handler, no commit)."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r01-fix-")
        try:
            _make_project(tmpdir, {
                "upstream-drift.log": "220 commits between merge-base and main\n",
                "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            })
            _make_research_prd(tmpdir, "R01", "Branch selection",
                               "Which upstream base to track?",
                               "verdicts/R01-verdict.md")

            transport = _make_mock_transport()
            cfg = EngineConfig(
                subject_port=8082, judge_port=8080, judge_mode="off",
                max_llm_calls=10, max_total_tokens=200_000,
                max_wall_clock_s=120, archive_results=False,
                enable_memory_recall=False,
                max_research_generate=3,
                enforce_research_sources=False)  # mock transport: no real sources
            engine = Engine(transport=transport, config=cfg)

            import engine.engine as eng_mod
            original, _ = _patch_parse_prd(tmpdir)
            try:
                result = engine.run(
                    os.path.join(tmpdir, ".run-prds", "PRD-R01.md"), tmpdir, config=cfg)
            finally:
                eng_mod.parse_prd = original

            assert result.success, (
                f"R01 research atom must succeed; got success={result.success}, "
                f"error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
            assert result.tasks[0].commit_sha is not None, (
                "commit_sha must be set — the R01 failure was no commit_sha")
            # Verdict file on disk must contain real content
            verdict_path = os.path.join(tmpdir, "verdicts", "R01-verdict.md")
            content = open(verdict_path).read()
            assert "Position: supported" in content
            assert "# ACE_STUB" not in content
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
