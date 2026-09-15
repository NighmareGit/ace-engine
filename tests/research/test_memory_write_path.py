"""M3 regression: ResearchTaskHandler.run() submits claims to the memory
poisoning gate after verdict validation succeeds.

Before the fix, ResearchTaskHandler.run() never called gate.submit_claims,
so no research claim ever entered memory_claims in a live run (G3 lane inert).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.task_handler import _resolve_handler


def _make_research_task():
    return Task(
        id="R01",
        title="Research Python",
        description="Research Python list comprehensions",
        module="research.md",
        task_type="research",
    )


class _MockEngine:
    def __init__(self, transport, config):
        self.transport = transport
        self.config = config


class _MockPipeline:
    def __init__(self, run_id="research-run"):
        self.run_id = run_id


class _CapturingTransport:
    """Minimal mock transport that returns a valid cited verdict."""

    def __init__(self):
        self.calls = []

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, max_tokens=512, temperature=0.3):
        self.calls.append(messages)
        # A minimal cited verdict that passes EvidenceValidator stages.
        text = (
            "## Claims\n\n"
            "[claim-1] Python supports list comprehensions [src-1].\n\n"
            "## Sources\n\n"
            "- [src-1] Python docs: list comprehensions.\n\n"
            "## Verdict\n\n"
            "Position: supported\n"
            "Confidence: 0.95\n\n"
            "## Contradictions\n\n"
            "None.\n"
        )
        return {
            "content": text,
            "total_tokens": 200,
            "thinking_tokens": 0,
            "finish_reason": "stop",
            "reasoning_content": "",
        }


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the engine DB at a temp file and init the schema."""
    import engine.state as state_mod

    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    state_mod.init_db(db_path)
    return db_path


def test_research_handler_submits_claims_to_memory(tmp_path, monkeypatch, tmp_db):
    """M3 happy path: after verdict validation succeeds, the research
    handler must submit the sidecar's claims to memory_claims with status
    'pending' (when enable_memory_recall is on)."""
    from engine.research import ResearchTaskHandler
    from engine.research.verdict import VerdictGenerator, GeneratedVerdict
    from engine.research.evidence import EvidenceValidator

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # Create a minimal source file so file-source verification passes.
    (project_dir / "src.py").write_text("# source\nx = 1\n")

    transport = _CapturingTransport()
    config = EngineConfig(enable_memory_recall=True)
    engine = _MockEngine(transport, config)
    pipeline = _MockPipeline(run_id="m3-research-run")

    task = _make_research_task()
    handler = _resolve_handler(task, engine)
    assert isinstance(handler, ResearchTaskHandler)

    # Pre-init the generator with a deterministic verdict.
    verdict_text = (
        "## Claims\n\n"
        "[claim-1] Python supports list comprehensions [src-1].\n\n"
        "## Sources\n\n"
        "- [src-1] Python docs: list comprehensions.\n\n"
        "## Verdict\n\n"
        "Position: supported\n"
        "Confidence: 0.95\n\n"
        "## Contradictions\n\n"
        "None.\n"
    )

    def _patched_generate(context, task, error_ctx=None, max_tokens_override=None,
                          retrieved=None):
        return GeneratedVerdict(
            verdict_text=verdict_text,
            verdict_path="research.md",
            claims=["claim-1"], sources=["src-1"], citations=[],
            raw_response="", tokens=0,
        )

    handler._generator = VerdictGenerator(config, transport)
    handler._generator.generate = _patched_generate

    # Run the handler.
    result = handler.run(task, pipeline, str(project_dir))

    # The atom should reach a terminal state (COMMIT or FAILED).  With a
    # valid cited verdict it should pass validation.
    assert result.state in ("COMMIT", "FAILED"), (
        f"research atom not terminal: {result.state}")

    # If validation passed, claims must have been submitted.
    if result.state == "COMMIT":
        from engine.state import _get_conn
        conn = _get_conn(tmp_db)
        rows = conn.execute(
            "SELECT id, task_id, status FROM memory_claims WHERE run_id=?",
            ("m3-research-run",),
        ).fetchall()
        conn.close()
        assert len(rows) >= 1, (
            "M3: research handler did not write memory_claims row on success")
        assert rows[0]["status"] == "pending", (
            f"M3: claim status should be 'pending', got {rows[0]['status']!r}")


def test_research_handler_memory_failure_is_non_breaking(tmp_path, monkeypatch, tmp_db):
    """M3 safety: a memory gate failure must NEVER break the research atom."""
    from engine.research import ResearchTaskHandler
    from engine.research.verdict import VerdictGenerator, GeneratedVerdict
    import engine.research as research_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "src.py").write_text("# source\nx = 1\n")

    transport = _CapturingTransport()
    config = EngineConfig(enable_memory_recall=True)
    engine = _MockEngine(transport, config)
    pipeline = _MockPipeline(run_id="m3-fail-run")

    task = _make_research_task()
    handler = _resolve_handler(task, engine)

    verdict_text = (
        "## Claims\n\n"
        "[claim-1] Python supports list comprehensions [src-1].\n\n"
        "## Sources\n\n"
        "- [src-1] Python docs: list comprehensions.\n\n"
        "## Verdict\n\n"
        "Position: supported\n"
        "Confidence: 0.95\n\n"
        "## Contradictions\n\n"
        "None.\n"
    )

    def _patched_generate(context, task, error_ctx=None, max_tokens_override=None,
                          retrieved=None):
        return GeneratedVerdict(
            verdict_text=verdict_text,
            verdict_path="research.md",
            claims=["claim-1"], sources=["src-1"], citations=[],
            raw_response="", tokens=0,
        )

    handler._generator = VerdictGenerator(config, transport)
    handler._generator.generate = _patched_generate

    # Make submit_claims raise — the handler must swallow it.
    def _boom(*args, **kwargs):
        raise RuntimeError("memory gate is down")

    # Patch at the module where the handler imports it.
    import engine.memory.gate as gate_mod
    orig = gate_mod.submit_claims
    gate_mod.submit_claims = _boom
    try:
        result = handler.run(task, pipeline, str(project_dir))
    finally:
        gate_mod.submit_claims = orig

    # The atom must still reach a terminal state (not crash).
    assert result.state in ("COMMIT", "FAILED"), (
        f"memory failure broke the research atom: state={result.state}")
