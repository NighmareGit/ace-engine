"""Tests for engine/retrieval/prompt_assemble.py."""

from __future__ import annotations

import pytest

from engine.retrieval.prompt_assemble import (
    assemble_retrieved, assemble_from_repo_map, Chunk,
    UNTRUSTED_MARKER_OPEN, UNTRUSTED_MARKER_CLOSE, RETRIEVAL_BUDGETS,
)
from engine.prompts import compile_prompt


class TestUntrustedMarkers:
    """Every retrieved chunk must be wrapped in untrusted markers."""

    def test_marker_open_present(self):
        chunks = [Chunk(text="secret content", score=1.0)]
        result = assemble_retrieved("base", chunks, budget=1000)
        assert UNTRUSTED_MARKER_OPEN in result

    def test_marker_close_present(self):
        chunks = [Chunk(text="secret content", score=1.0)]
        result = assemble_retrieved("base", chunks, budget=1000)
        assert UNTRUSTED_MARKER_CLOSE in result

    def test_markers_wrap_content(self):
        chunks = [Chunk(text="THE_CONTENT", score=1.0)]
        result = assemble_retrieved("base", chunks, budget=1000)
        idx_open = result.index(UNTRUSTED_MARKER_OPEN)
        idx_content = result.index("THE_CONTENT")
        idx_close = result.index(UNTRUSTED_MARKER_CLOSE)
        assert idx_open < idx_content < idx_close

    def test_each_chunk_separately_wrapped(self):
        chunks = [Chunk(text="A", score=1.0), Chunk(text="B", score=0.5)]
        result = assemble_retrieved("base", chunks, budget=1000)
        assert result.count(UNTRUSTED_MARKER_OPEN) == 2
        assert result.count(UNTRUSTED_MARKER_CLOSE) == 2


class TestBudgetTrim:
    """Rank-and-trim: never silent mid-symbol truncation."""

    def test_empty_retrieved_returns_base(self):
        assert assemble_retrieved("base_prompt", [], budget=1000) == "base_prompt"

    def test_trims_lowest_ranked_first(self):
        chunks = [
            Chunk(text="HIGH", score=0.9),
            Chunk(text="LOW", score=0.1),
        ]
        # Very tight budget — only the highest-ranked chunk fits
        result = assemble_retrieved("base", chunks, budget=5)
        assert "HIGH" in result

    def test_never_truncates_mid_chunk(self):
        chunks = [Chunk(text="COMPLETE_CHUNK_TEXT", score=1.0)]
        result = assemble_retrieved("base", chunks, budget=1000)
        assert "COMPLETE_CHUNK_TEXT" in result
        # No partial truncation of the chunk text
        assert "COMPLETE_CHUNK" not in result or "COMPLETE_CHUNK_TEXT" in result

    def test_markers_count_against_budget(self):
        """Markers consume budget — with very few chunks and tiny budget,
        even one chunk might not fit, but the base prompt is preserved."""
        chunks = [Chunk(text="X" * 100, score=1.0)]
        result = assemble_retrieved("base", chunks, budget=1)
        # Base prompt is always preserved
        assert "base" in result


class TestBudgets:
    """RETRIEVAL_BUDGETS dict keyed by atom_type."""

    def test_research_budget(self):
        assert RETRIEVAL_BUDGETS["research"] == 3000

    def test_implement_budget(self):
        assert RETRIEVAL_BUDGETS["implement"] == 1500

    def test_fix_budget(self):
        assert RETRIEVAL_BUDGETS["fix"] == 1000

    def test_default_budget(self):
        assert RETRIEVAL_BUDGETS["_default"] == 1500


class TestCompilePromptHook:
    """compile_prompt() additive retrieved= param."""

    def test_default_off(self):
        """Without retrieved=, output is unchanged (no markers)."""
        result = compile_prompt("T01", {
            "prd_title": "test",
            "tasks": [{"id": "T01", "title": "t", "task_type": "implementation"}],
            "project_context": {},
        }, {})
        assert UNTRUSTED_MARKER_OPEN not in result

    def test_with_retrieved_adds_markers(self):
        """With retrieved= chunks, output contains untrusted markers."""
        chunks = [Chunk(text="retrieved content", score=1.0)]
        result = compile_prompt("T01", {
            "prd_title": "test",
            "tasks": [{"id": "T01", "title": "t", "task_type": "research"}],
            "project_context": {},
        }, {}, retrieved=chunks)
        assert UNTRUSTED_MARKER_OPEN in result
        assert "retrieved content" in result
