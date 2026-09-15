"""Unit tests for engine/research/verdict.py — marker parsing, shape, mock transport.

Mock transport only — no GPU, no inference, no network calls.
"""

import sys
import os
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.research.verdict import (
    VerdictGenerator, GeneratedVerdict, CitationRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CANNED_VERDICT = """\
## Claims

[claim-1] The sky is blue due to Rayleigh scattering [src-1][src-2].

[claim-2] Water boils at 100C at sea level [src-3].

## Sources

- [src-1] NASA: "Rayleigh scattering causes the blue color of the sky."
- [src-2] Physics Today, 2020.
- [src-3] NIST reference data.

## Contradictions

None identified.

## Verdict

**supported** (confidence: 0.92)
"""


def _mock_transport_with(content):
    """Return a MagicMock transport whose curl_beellama returns a flat dict."""
    t = MagicMock()
    t.curl_beellama.return_value = {
        "content": content,
        "total_tokens": 500,
        "thinking_tokens": 0,
        "finish_reason": "stop",
        "reasoning_content": "",
    }
    return t


def _make_config():
    return EngineConfig()


# ---------------------------------------------------------------------------
# TEST GROUP A — Marker parsing (happy path)
# ---------------------------------------------------------------------------

def test_parse_markers_happy_path():
    """[claim-N]/[src-N] markers are parsed into CitationRecords."""
    claims, sources, citations = VerdictGenerator._parse_markers(CANNED_VERDICT)
    assert "claim-1" in claims
    assert "claim-2" in claims
    assert "src-1" in sources
    assert "src-2" in sources
    assert "src-3" in sources
    # claim-1 cites src-1 and src-2.
    c1 = next(c for c in citations if c.claim_id == "claim-1")
    assert "src-1" in c1.source_ids
    assert "src-2" in c1.source_ids
    # claim-2 cites src-3.
    c2 = next(c for c in citations if c.claim_id == "claim-2")
    assert "src-3" in c2.source_ids


def test_parse_markers_sources_line_format():
    """Explicit 'Sources: [src-1], [src-2]' line associates with last claim."""
    text = "[claim-1] Something is true.\nSources: [src-1], [src-2]\n"
    claims, sources, citations = VerdictGenerator._parse_markers(text)
    assert claims == ["claim-1"]
    c1 = next(c for c in citations if c.claim_id == "claim-1")
    assert c1.source_ids == ["src-1", "src-2"]


def test_parse_markers_malformed_skipped():
    """Malformed markers (non-numeric id) are skipped, never crash."""
    text = "[claim-abc] bad\n[claim-1] good [src-1]\n[src-] also bad\n"
    claims, sources, citations = VerdictGenerator._parse_markers(text)
    assert claims == ["claim-1"]
    assert sources == ["src-1"]
    assert len(citations) == 1


def test_parse_markers_no_markers():
    """Text with no markers returns empty lists, no crash."""
    claims, sources, citations = VerdictGenerator._parse_markers("plain text only")
    assert claims == []
    assert sources == []
    assert citations == []


# ---------------------------------------------------------------------------
# TEST GROUP B — Verdict extraction
# ---------------------------------------------------------------------------

def test_extract_verdict_largest_fenced_block():
    """Takes the largest fenced block when fences are present."""
    raw = f"intro\n```md\n{CANNED_VERDICT}\n```\noutro"
    extracted = VerdictGenerator._extract_verdict(raw)
    assert "Rayleigh scattering" in extracted
    assert "intro" not in extracted


def test_extract_verdict_missing_fence_fallback():
    """When no fenced block, returns raw minus fence lines."""
    raw = "line one\n```\nline two\n"
    extracted = VerdictGenerator._extract_verdict(raw)
    assert "line one" in extracted
    assert "line two" in extracted
    assert "```" not in extracted


# ---------------------------------------------------------------------------
# TEST GROUP C — GeneratedVerdict shape
# ---------------------------------------------------------------------------

def test_generated_verdict_default_fields():
    """GeneratedVerdict has correct defaults."""
    gv = GeneratedVerdict(
        verdict_text="x", verdict_path="v.md", claims=[], sources=[],
        citations=[], raw_response="r")
    assert gv.tokens == 0
    assert gv.thinking_tokens == 0
    assert gv.latency_ms == 0.0
    assert gv.role == "researcher"
    assert gv.finish_reason == ""
    assert gv.exhausted is False
    assert gv.max_tokens_used == 0


def test_citation_record_shape():
    """CitationRecord holds claim_id, source_ids, optional span/confidence."""
    cr = CitationRecord(claim_id="claim-1", source_ids=["src-1"], span="quote", confidence=0.8)
    assert cr.claim_id == "claim-1"
    assert cr.source_ids == ["src-1"]
    assert cr.span == "quote"
    assert cr.confidence == 0.8
    # Defaults.
    cr2 = CitationRecord(claim_id="c", source_ids=[])
    assert cr2.span is None
    assert cr2.confidence == 0.0


# ---------------------------------------------------------------------------
# TEST GROUP D — generate() with mock transport
# ---------------------------------------------------------------------------

def test_generate_returns_generated_verdict_with_citations():
    """generate() returns GeneratedVerdict with parsed citations."""
    transport = _mock_transport_with(CANNED_VERDICT)
    gen = VerdictGenerator(_make_config(), transport)
    task = Task(id="R01", title="Test", description="Does X?", module="verdict.md")
    result = gen.generate(context=None, task=task)
    assert isinstance(result, GeneratedVerdict)
    assert "claim-1" in result.claims
    assert "src-1" in result.sources
    assert len(result.citations) >= 1
    assert result.tokens == 500
    assert result.verdict_path == "verdict.md"


def test_generate_records_session_log():
    """generate() attempts to write to session_log_recorder.record()."""
    transport = _mock_transport_with(CANNED_VERDICT)
    config = _make_config()
    recorder = MagicMock()
    config.session_log_recorder = recorder
    gen = VerdictGenerator(config, transport)
    task = Task(id="R01", title="T", description="d", module="verdict.md")
    gen.generate(context=None, task=task)
    recorder.record.assert_called_once()
    call_kwargs = recorder.record.call_args
    # task_id is passed positionally (index 0) or as kwarg.
    assert call_kwargs.kwargs.get("task_id") == "R01" or call_kwargs.args[0] == "R01"
    assert call_kwargs.kwargs.get("stage") == "generate"


def test_generate_prompt_includes_researcher_persona():
    """The prompt instructs citation markers and forbids triple-backticks."""
    transport = _mock_transport_with(CANNED_VERDICT)
    gen = VerdictGenerator(_make_config(), transport)
    task = Task(id="R01", title="T", description="question?", module="verdict.md")
    gen.generate(context=None, task=task)
    # Inspect the messages sent to transport.
    call_args = transport.curl_beellama.call_args
    msgs = call_args.kwargs.get("messages") or call_args.args[1]
    prompt = msgs[0]["content"]
    assert "[claim-N]" in prompt
    assert "[src-N]" in prompt
    assert "triple-backticks" in prompt.lower() or "```" in prompt
    assert "supported" in prompt


def test_generate_with_error_ctx_feedback():
    """error_ctx.validation_errors are appended to the prompt."""
    transport = _mock_transport_with(CANNED_VERDICT)
    gen = VerdictGenerator(_make_config(), transport)
    task = Task(id="R01", title="T", description="q", module="verdict.md")
    error_ctx = MagicMock()
    error_ctx.validation_errors = ["claim-1 had no sources"]
    error_ctx.attempt_number = 2
    gen.generate(context=None, task=task, error_ctx=error_ctx)
    call_args = transport.curl_beellama.call_args
    msgs = call_args.kwargs.get("messages") or call_args.args[1]
    prompt = msgs[0]["content"]
    assert "claim-1 had no sources" in prompt


def test_generate_session_log_failure_is_nonfatal():
    """A failing recorder must not break generate()."""
    transport = _mock_transport_with(CANNED_VERDICT)
    config = _make_config()
    recorder = MagicMock()
    recorder.record.side_effect = RuntimeError("disk full")
    config.session_log_recorder = recorder
    gen = VerdictGenerator(config, transport)
    task = Task(id="R01", title="T", description="d", module="verdict.md")
    result = gen.generate(context=None, task=task)
    assert isinstance(result, GeneratedVerdict)
    assert result.tokens == 500
