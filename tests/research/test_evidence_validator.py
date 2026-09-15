"""Unit tests for the staged EvidenceValidator (T04/T09).

Covers each stage's short-circuit behavior, the report-all-within-stage
pattern, and the no-LLM/no-network import contract.
"""

import ast
import pytest

from engine.research.evidence import (
    EvidenceValidator,
    EvidenceResult,
    CitationRecord,
    _extract_section,
    _tfidf_cosine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_verdict():
    return """## Verdict

Position: supported
Confidence: 0.85
The best approach for caching is a three-tier expert cache because it
measured the lowest latency and highest throughput across all benchmarks.

## Sources

- [src-1] type: file uri: /x/a.txt title: Source A
- [src-2] type: file uri: /x/b.txt title: Source B

## Claims

- [claim-1] Three-tier caching reduces latency [src-1]
- [claim-2] Expert cache improves throughput [src-2]

## Contradictions

- between [1, 2] resolution: explained by context X

## Citations

[claim:1, src:1, "three tier caching reduces latency significantly", 0.9]
[claim:2, src:2, "expert cache improves throughput measurably", 0.8]
"""


# ---------------------------------------------------------------------------
# Stage tests
# ---------------------------------------------------------------------------

class TestStage0Empty:
    def test_empty_rejected(self):
        r = EvidenceValidator().validate("", question="q")
        assert not r.passed
        assert r.checks[0]["stage"] == 0
        assert r.checks[0]["name"] == "empty"
        assert not r.checks[0]["passed"]

    def test_whitespace_rejected(self):
        r = EvidenceValidator().validate("   \n\n  ", question="q")
        assert not r.passed
        assert r.checks[0]["name"] == "empty"

    def test_nonempty_passes_stage0(self):
        r = EvidenceValidator().validate(_valid_verdict(), question="q")
        assert r.checks[0]["passed"]


class TestStage1Structure:
    def test_missing_position(self):
        md = _valid_verdict().replace("Position: supported", "Position: maybe")
        r = EvidenceValidator().validate(md, question="q")
        assert not r.passed
        stage1 = next(c for c in r.checks if c["stage"] == 1)
        assert not stage1["passed"]
        assert "Position" in stage1["error"]

    def test_missing_confidence(self):
        md = _valid_verdict().replace("Confidence: 0.85", "")
        r = EvidenceValidator().validate(md, question="q")
        assert not r.passed
        stage1 = next(c for c in r.checks if c["stage"] == 1)
        assert not stage1["passed"]
        assert "Confidence" in stage1["error"]

    def test_missing_sources_section(self):
        md = _valid_verdict().replace("## Sources\n\n- [src-1]", "## NotSources\n\n- [src-1]")
        r = EvidenceValidator().validate(md, question="q")
        assert not r.passed
        stage1 = next(c for c in r.checks if c["stage"] == 1)
        assert not stage1["passed"]

    def test_short_circuit_stages_2_5_not_run(self):
        r = EvidenceValidator().validate("", question="q")
        assert len(r.checks) == 1  # only stage 0


class TestStage2CitationFormat:
    def test_hallucinated_citation(self):
        md = _valid_verdict().replace("[claim:1, src:1", "[claim:1, src:99")
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage2 = next(c for c in r.checks if c["stage"] == 2)
        assert not stage2["passed"]
        assert "unknown src" in stage2["error"]

    def test_malformed_marker_skipped_with_warning(self):
        md = _valid_verdict().replace("[claim-1]", "[claim-]")
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage2 = next(c for c in r.checks if c["stage"] == 2)
        assert not stage2["passed"]
        assert "malformed" in stage2["error"]


class TestStage3ClaimCoverage:
    def test_orphan_claim(self):
        md = _valid_verdict().replace(
            "- [claim-2] Expert cache improves throughput [src-2]",
            "- [claim-2] Expert cache improves throughput [src-2]\n"
            "- [claim-99] orphan claim with no source"
        )
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage3 = next(c for c in r.checks if c["stage"] == 3)
        assert not stage3["passed"]
        assert "orphan" in stage3["error"]


class TestStage4Sources:
    def test_self_support(self):
        md = _valid_verdict().replace(
            '[claim:1, src:1, "three tier caching reduces latency significantly", 0.9]',
            '[claim:1, src:1, "Three-tier caching reduces latency", 0.9]'
        )
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "self-support" in stage4["error"]

    def test_cyclic_sources(self):
        md = _valid_verdict().replace(
            "- [src-1] type: file uri: /x/a.txt title: Source A\n"
            "- [src-2] type: file uri: /x/b.txt title: Source B",
            "- [src-1] title: Source A cites: [src-2]\n"
            "- [src-2] title: Source B cites: [src-1]"
        )
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "cycle" in stage4["error"]

    def test_unresolved_contradiction(self):
        md = _valid_verdict().replace(
            "resolution: explained by context X",
            "no resolution"
        )
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "resolution" in stage4["error"]

    def test_low_claim_density(self):
        fluff = "filler " * 1000
        md = _valid_verdict() + "\n\n## Appendix\n\n" + fluff
        r = EvidenceValidator().validate(md, question="caching approach")
        assert not r.passed
        stage4 = next(c for c in r.checks if c["stage"] == 4)
        assert not stage4["passed"]
        assert "density" in stage4["error"]


class TestStage5AnswerQuestion:
    def test_relevant_verdict_passes(self):
        r = EvidenceValidator().validate(
            _valid_verdict(), question="what is the best approach for caching")
        stage5 = next(c for c in r.checks if c["stage"] == 5)
        assert stage5["passed"]

    def test_unrelated_verdict_fails(self):
        md = _valid_verdict().replace(
            "The best approach for caching is a three-tier expert cache because it\n"
            "measured the lowest latency and highest throughput across all benchmarks.",
            "The quadratic formula is negative b plus or minus the square root of "
            "b squared minus four a c all over two a."
        )
        r = EvidenceValidator().validate(md, question="what is the best approach for caching")
        stage5 = next(c for c in r.checks if c["stage"] == 5)
        assert not stage5["passed"]
        assert "TF-IDF" in stage5["error"]

    def test_no_question_skips(self):
        r = EvidenceValidator().validate(_valid_verdict(), question="")
        stage5 = next(c for c in r.checks if c["stage"] == 5)
        assert stage5["passed"]


# ---------------------------------------------------------------------------
# Valid case passes all stages
# ---------------------------------------------------------------------------

class TestValidCase:
    def test_all_6_stages_pass(self):
        r = EvidenceValidator().validate(
            _valid_verdict(), question="what is the best approach for caching")
        assert r.passed
        assert len(r.checks) == 6
        assert all(c["passed"] for c in r.checks)

    def test_verdict_position_parsed(self):
        r = EvidenceValidator().validate(_valid_verdict(), question="q")
        assert r.verdict_position == "supported"

    def test_verdict_confidence_parsed(self):
        r = EvidenceValidator().validate(_valid_verdict(), question="q")
        assert r.verdict_confidence == 0.85

    def test_citations_parsed(self):
        r = EvidenceValidator().validate(_valid_verdict(), question="q")
        assert len(r.citations) == 2
        assert isinstance(r.citations[0], CitationRecord)


# ---------------------------------------------------------------------------
# TF-IDF helper
# ---------------------------------------------------------------------------

class TestTfidfCosine:
    def test_identical(self):
        assert _tfidf_cosine("hello world", "hello world") == pytest.approx(1.0, abs=1e-6)

    def test_disjoint(self):
        assert _tfidf_cosine("aaa bbb", "ccc ddd") == 0.0

    def test_empty(self):
        assert _tfidf_cosine("", "hello") == 0.0
        assert _tfidf_cosine("hello", "") == 0.0


# ---------------------------------------------------------------------------
# No LLM / no network imports
# ---------------------------------------------------------------------------

class TestNoLlmNetwork:
    def test_no_sklearn_import(self):
        with open("engine/research/evidence.py", "r") as f:
            tree = ast.parse(f.read())
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        for imp in imports:
            if isinstance(imp, ast.ImportFrom) and imp.module:
                assert "sklearn" not in imp.module
                assert "openai" not in imp.module
            if isinstance(imp, ast.Import):
                for alias in imp.names:
                    assert "sklearn" not in alias.name
                    assert "openai" not in alias.name

    def test_no_network_calls(self):
        with open("engine/research/evidence.py", "r") as f:
            source = f.read()
        assert "urllib" not in source
        assert "requests" not in source
        assert "curl_beellama" not in source
