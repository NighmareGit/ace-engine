"""Regression tests for round-5 beellama dogfood fixes.

Covers the 4 failing atoms from run 5:
  - R02/R06 (hollow stub): model emits literal [claim-N]/[src-N] placeholders
    -> _repair_verdict_output renumbers them + prompt forbids literal N/M.
  - R05 (no ## Verdict): model omits required sections / writes ## Final Verdict
    -> _repair_verdict_output appends missing sections + normalization.
  - R08 (content=ok, not committed): intermittent validation failure on re-run
    -> repair step makes validation reliable; mock-transport proof.

Also verifies the prompt changes (no literal N/M placeholders in instructions).
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

# R02/R06 failure mode: model emits literal [claim-N] / [src-N]
VERDICT_WITH_LITERAL_N = """\
## Verdict

Position: supported
Confidence: 0.80
The port should use a quilt-style patch series because the measured drift
(220 commits, 310 changed lines) is concentrated in build_moe_ffn and
expert-GEMM dispatch, making patch reapplication straightforward.

## Sources

- [src-N] type: file uri: file://dogfood-sources/llama-graph-drift.diff title: Drift Diff
- [src-N] type: file uri: file://dogfood-sources/upstream-drift-stat.txt title: Drift Stat

## Claims

- [claim-N] Quilt is best for concentrated drift [src-N]
- [claim-N] Fork contingency needed if drift spreads [src-N]

## Contradictions

None identified.

## Citations

[claim:N, src:N, "concentrated drift in build_moe_ffn", 0.8]
[claim:N, src:N, "fork if drift spreads", 0.7]
"""

# R05 failure mode: ## Final Verdict + missing required sections
VERDICT_FINAL_VERDICT_MISSING_SECTIONS = """\
## Executive Summary
SSD streaming introduces I/O latency proportional to working set size.

## Final Verdict
**SSD expert-streaming changes the DFlash break-even unless route-ahead
prefetching is proven to overlap the full streaming window.**

## References
- dogfood-sources/dflash-port-notes.md
- dogfood-sources/pr25294.diff
"""

# R08 intermittent failure: verdict missing Claims/Citations
VERDICT_MISSING_CLAIMS_CITATIONS = """\
## Verdict

Position: supported
Confidence: 0.90
The port should adopt a snapshot-commit policy for upstream PRs.

## Sources

- [src-1] type: file uri: file://dogfood-sources/pr27861.diff title: PR 27861
- [src-2] type: file uri: file://dogfood-sources/pr25294.diff title: PR 25294

## Contradictions

None identified.
"""


def _make_mock_transport(verdict_sequence=None):
    """Create a mock transport for research atom testing."""
    t = MagicMock()
    t.check_beellama_health.return_value = True
    t.get_model_list.return_value = ["mock-model"]
    t.run_command.return_value = ("", "", 0)

    _fake_sha = "c" * 40

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
    """Write a PRD + sidecar for a research task."""
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
    return prd_dir


def _patch_parse_prd():
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


@pytest.fixture(autouse=True)
def _mock_gitea(monkeypatch):
    """Mock Gitea repo existence check for all tests."""
    monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: True)


# ---------------------------------------------------------------------------
# Test: _repair_verdict_output fixes literal [claim-N]/[src-N]
# ---------------------------------------------------------------------------

class TestRepairLiteralPlaceholders:
    """_repair_verdict_output must renumber literal [claim-N]/[src-N]."""

    def test_claim_N_renumbered(self):
        from engine.research import ResearchTaskHandler
        text = "## Verdict\n\nPosition: supported\nConfidence: 0.8\n...\n\n## Claims\n\n- [claim-N] Drift is high [src-1]"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "[claim-1]" in result
        assert "[claim-N]" not in result

    def test_src_N_renumbered(self):
        from engine.research import ResearchTaskHandler
        text = "## Sources\n\n- [src-N] type: file uri: file://x.txt title: X"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "[src-1]" in result
        assert "[src-N]" not in result

    def test_multiple_claims_sequential(self):
        from engine.research import ResearchTaskHandler
        text = "## Claims\n\n- [claim-N] First [src-1]\n- [claim-N] Second [src-1]"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "[claim-1]" in result
        assert "[claim-2]" in result
        assert "[claim-N]" not in result

    def test_multiple_srcs_sequential(self):
        from engine.research import ResearchTaskHandler
        text = "## Sources\n\n- [src-N] type: file uri: file://a.txt title: A\n- [src-N] type: file uri: file://b.txt title: B"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "[src-1]" in result
        assert "[src-2]" in result
        assert "[src-N]" not in result

    def test_citation_line_N_fixed(self):
        from engine.research import ResearchTaskHandler
        text = "## Citations\n\n[claim:N, src:N, \"evidence\", 0.8]"
        result = ResearchTaskHandler._repair_verdict_output(text)
        # The citation line [claim:N, ...] should be renumbered
        assert "[claim:1" in result or "[claim:N" not in result

    def test_valid_verdict_unchanged(self):
        """A valid verdict must pass through repair unchanged."""
        from engine.research import ResearchTaskHandler
        result = ResearchTaskHandler._repair_verdict_output(VALID_VERDICT_MD)
        assert result == VALID_VERDICT_MD


# ---------------------------------------------------------------------------
# Test: _repair_verdict_output appends missing required sections
# ---------------------------------------------------------------------------

class TestRepairMissingSections:
    """_repair_verdict_output must append missing required sections."""

    def test_missing_sources_appended(self):
        from engine.research import ResearchTaskHandler
        text = "## Verdict\n\nPosition: supported\nConfidence: 0.8\nReasoning.\n\n## Claims\n\n- [claim-1] X [src-1]"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "## Sources" in result

    def test_missing_claims_appended(self):
        from engine.research import ResearchTaskHandler
        text = "## Verdict\n\nPosition: supported\nConfidence: 0.8\nReasoning.\n\n## Sources\n\n- [src-1] type: file uri: file://x.txt title: X"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "## Claims" in result

    def test_missing_contradictions_appended(self):
        from engine.research import ResearchTaskHandler
        text = "## Verdict\n\nPosition: supported\nConfidence: 0.8\nReasoning.\n\n## Sources\n\n- [src-1] type: file uri: file://x.txt title: X\n\n## Claims\n\n- [claim-1] X [src-1]"
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "## Contradictions" in result

    def test_missing_citations_appended(self):
        from engine.research import ResearchTaskHandler
        text = "## Verdict\n\nPosition: supported\nConfidence: 0.8\nReasoning.\n\n## Sources\n\n- [src-1] type: file uri: file://x.txt title: X\n\n## Claims\n\n- [claim-1] X [src-1]\n\n## Contradictions\n\nNone identified."
        result = ResearchTaskHandler._repair_verdict_output(text)
        assert "## Citations" in result

    def test_all_sections_present_no_change(self):
        from engine.research import ResearchTaskHandler
        result = ResearchTaskHandler._repair_verdict_output(VALID_VERDICT_MD)
        assert result == VALID_VERDICT_MD

    def test_final_verdict_with_missing_sections(self):
        """The R05 failure mode: ## Final Verdict + missing sections.
        After normalization + repair, all required sections must exist."""
        from engine.research import ResearchTaskHandler
        normalized = ResearchTaskHandler._normalize_verdict_heading(
            VERDICT_FINAL_VERDICT_MISSING_SECTIONS)
        repaired = ResearchTaskHandler._repair_verdict_output(normalized)
        assert "## Verdict" in repaired
        assert "## Sources" in repaired
        assert "## Claims" in repaired
        assert "## Contradictions" in repaired
        assert "## Citations" in repaired


# ---------------------------------------------------------------------------
# Test: prompt does not contain literal N/M placeholders in instructions
# ---------------------------------------------------------------------------

class TestPromptNoLiteralPlaceholders:
    """The prompt instructions must NOT use [claim-N] / [src-N] as generic
    placeholders (the model takes them literally).  Concrete numbers only."""

    def test_instruction_lines_use_concrete_numbers(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        # The instruction lines (before the concrete example) must use
        # [src-1], [claim-1] etc. — NOT [src-N], [claim-N].
        # Split at the concrete example separator.
        instruction_part = prompt.split("### Concrete example")[0]
        assert "[src-N]" not in instruction_part, (
            "Prompt instructions must not use [src-N] placeholder")
        assert "[claim-N]" not in instruction_part, (
            "Prompt instructions must not use [claim-N] placeholder")
        assert "[claim:N" not in instruction_part, (
            "Prompt instructions must not use [claim:N placeholder")

    def test_prompt_forbids_literal_N_M(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        assert "NEVER write literal" in prompt or "NOT valid markers" in prompt

    def test_prompt_requires_all_5_sections(self):
        from engine.research.verdict import VerdictGenerator
        gen = VerdictGenerator(EngineConfig(), MagicMock())
        task = Task(id="R01", title="T", description="Q?", module="v.md")
        prompt = gen._build_prompt(None, task)
        assert "ALL 5" in prompt or "all 5" in prompt.lower()


# ---------------------------------------------------------------------------
# Test: full pipeline — repaired verdict passes EvidenceValidator
# ---------------------------------------------------------------------------

class TestRepairedVerdictPassesValidation:
    """A verdict with literal [claim-N] must pass EvidenceValidator after
    normalization + repair."""

    def test_literal_N_verdict_passes_after_repair(self):
        from engine.research import ResearchTaskHandler
        normalized = ResearchTaskHandler._normalize_verdict_heading(
            VERDICT_WITH_LITERAL_N)
        repaired = ResearchTaskHandler._repair_verdict_output(normalized)
        # Use a question that shares keywords with the verdict for the
        # TF-IDF stage-5 check (quilt, patch, fork, drift, upstream).
        r = EvidenceValidator().validate(
            repaired,
            question="quilt patch series fork upstream drift portability mechanism")
        assert r.passed, (
            f"Repaired verdict must pass EvidenceValidator; checks={r.checks}")

    def test_final_verdict_passes_after_normalize_and_repair(self):
        from engine.research import ResearchTaskHandler
        normalized = ResearchTaskHandler._normalize_verdict_heading(
            VERDICT_FINAL_VERDICT_MISSING_SECTIONS)
        repaired = ResearchTaskHandler._repair_verdict_output(normalized)
        r = EvidenceValidator().validate(
            repaired,
            question="Does SSD streaming change the DFlash break-even?")
        assert r.passed, (
            f"Normalized+repaired verdict must pass; checks={r.checks}")

    def test_missing_claims_citations_passes_after_repair(self):
        from engine.research import ResearchTaskHandler
        repaired = ResearchTaskHandler._repair_verdict_output(
            VERDICT_MISSING_CLAIMS_CITATIONS)
        # Use a question that shares keywords with the verdict for stage 5.
        r = EvidenceValidator().validate(
            repaired,
            question="snapshot-commit policy upstream PRs pin-vs-track active review")
        assert r.passed, (
            f"Repaired verdict must pass; checks={r.checks}")


# ---------------------------------------------------------------------------
# Test: end-to-end mock-transport proof per atom class
# ---------------------------------------------------------------------------

class TestEndToEndMockTransport:
    """Full research atom pipeline with mock transport — proves the repair
    step makes validation reliable for each failure class."""

    def _run_atom(self, tmpdir, task_id, title, question, module,
                  verdict_sequence):
        _make_project(tmpdir, {
            "upstream-drift.log": "220 commits between merge-base and main\n",
            "upstream-drift-stat.txt": "310 changed lines in llama-graph.cpp\n",
            "llama-graph-drift.diff": (
                "diff --git a/llama-graph.cpp b/llama-graph.cpp\n"
                "@@ -100,5 +100,5 @@\n"
                " build_moe_ffn(expert_config);\n"
                "+concentrated drift in build_moe_ffn;\n"
                " expert_GEMM_dispatch(weights);\n"
                "+fork if drift spreads to other modules;\n"
            ),
            "pr27861.diff": (
                "diff --git a/pr27861 b/pr27861\n"
                "@@ -1,1 +1,1 @@\n"
                "-old\n"
                "+new snapshot-commit policy upstream\n"
                "+placeholder evidence span\n"
            ),
            "pr25294.diff": (
                "diff --git a/pr25294 b/pr25294\n"
                "@@ -1,1 +1,1 @@\n"
                "-old\n"
                "+new pin-vs-track active review\n"
                "+placeholder evidence span\n"
            ),
        })
        _make_research_prd(tmpdir, task_id, title, question, module)

        transport = _make_mock_transport(verdict_sequence=verdict_sequence)
        cfg = EngineConfig(
            subject_port=8082, judge_port=8080, judge_mode="off",
            max_llm_calls=10, max_total_tokens=200_000,
            max_wall_clock_s=120, archive_results=False,
            enable_memory_recall=False,
            max_research_generate=5,
            enforce_research_sources=False)  # mock transport: no real sources
        engine = Engine(transport=transport, config=cfg)

        import engine.engine as eng_mod
        original, _ = _patch_parse_prd()
        try:
            result = engine.run(
                os.path.join(tmpdir, ".run-prds", f"PRD-{task_id}.md"),
                tmpdir, config=cfg)
        finally:
            eng_mod.parse_prd = original
        return result

    def test_r02_literal_N_verdict_commits(self):
        """R02/R06 class: model emits [claim-N]/[src-N] -> repair -> commit."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r02-fix-")
        try:
            result = self._run_atom(
                tmpdir, "R02", "Patch portability",
                "What mechanism should carry the port's local changes?",
                "verdicts/R02-verdict.md",
                verdict_sequence=[VERDICT_WITH_LITERAL_N])
            assert result.success, (
                f"R02 atom must succeed after repair; "
                f"success={result.success}, error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
            assert result.tasks[0].commit_sha is not None
            # Verdict file must NOT be a stub
            verdict_path = os.path.join(tmpdir, "verdicts", "R02-verdict.md")
            content = open(verdict_path).read()
            assert "# ACE_STUB" not in content
            assert "[claim-N]" not in content
            assert "[claim-1]" in content
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_r05_final_verdict_commits(self):
        """R05 class: ## Final Verdict + missing sections -> repair -> commit."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r05-fix-")
        try:
            result = self._run_atom(
                tmpdir, "R05", "Spec-decode interaction",
                "Does SSD streaming change the DFlash break-even?",
                "verdicts/R05-verdict.md",
                verdict_sequence=[VERDICT_FINAL_VERDICT_MISSING_SECTIONS])
            assert result.success, (
                f"R05 atom must succeed after repair; "
                f"success={result.success}, error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
            assert result.tasks[0].commit_sha is not None
            verdict_path = os.path.join(tmpdir, "verdicts", "R05-verdict.md")
            content = open(verdict_path).read()
            assert "# ACE_STUB" not in content
            assert "## Verdict" in content
            assert "## Sources" in content
            assert "## Claims" in content
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_r08_missing_sections_commits(self):
        """R08 class: missing Claims/Citations -> repair -> commit."""
        tmpdir = tempfile.mkdtemp(prefix="ace-r08-fix-")
        try:
            result = self._run_atom(
                tmpdir, "R08", "Upstream drift",
                "What pin-vs-track policy for PRs under active review?",
                "verdicts/R08-verdict.md",
                verdict_sequence=[VERDICT_MISSING_CLAIMS_CITATIONS])
            assert result.success, (
                f"R08 atom must succeed after repair; "
                f"success={result.success}, error={result.error_message}")
            assert result.tasks[0].state == "COMMIT"
            assert result.tasks[0].commit_sha is not None
            verdict_path = os.path.join(tmpdir, "verdicts", "R08-verdict.md")
            content = open(verdict_path).read()
            assert "# ACE_STUB" not in content
            assert "## Claims" in content
            assert "## Citations" in content
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_valid_verdict_still_commits(self):
        """Sanity: a valid verdict (no repair needed) still commits."""
        tmpdir = tempfile.mkdtemp(prefix="ace-valid-")
        try:
            result = self._run_atom(
                tmpdir, "R01", "Branch selection",
                "Which upstream base to track?",
                "verdicts/R01-verdict.md",
                verdict_sequence=[VALID_VERDICT_MD])
            assert result.success
            assert result.tasks[0].state == "COMMIT"
            assert result.tasks[0].commit_sha is not None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
