"""Tests for scripts/retrieval_baseline.py — Phase 0 metrics math."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.retrieval_baseline import (
    run_baseline, _extract_ground_truth, _recall_at_k,
    _grep_recall, _repo_map_recall, _pearson, _verdict,
    THRESHOLD_SKIP, THRESHOLD_ENHANCE,
)


class TestGroundTruthExtraction:
    """_extract_ground_truth parses file paths from prompt text."""

    def test_extracts_backtick_paths(self):
        text = "Files:\n  - `engine/foo.py`\n  - `tests/bar.py`\n"
        gt = _extract_ground_truth(text)
        assert "engine/foo.py" in gt
        assert "tests/bar.py" in gt

    def test_extracts_at_prefixed_paths(self):
        text = "Create `@api/health.py` and `@api/utils.py`"
        gt = _extract_ground_truth(text)
        assert "api/health.py" in gt
        assert "api/utils.py" in gt

    def test_ignores_http_urls(self):
        text = "See https://example.com/foo/bar.py for details"
        gt = _extract_ground_truth(text)
        assert "example.com/foo/bar.py" not in gt

    def test_empty_prompt(self):
        assert _extract_ground_truth("") == set()


class TestRecallMetrics:
    """_recall_at_k computation."""

    def test_perfect_recall(self):
        retrieved = ["a.py", "b.py", "c.py"]
        gt = {"a.py", "b.py"}
        assert _recall_at_k(retrieved, gt, 10) == 1.0

    def test_partial_recall(self):
        retrieved = ["a.py", "b.py", "c.py"]
        gt = {"a.py", "z.py"}
        assert _recall_at_k(retrieved, gt, 10) == 0.5

    def test_zero_recall(self):
        retrieved = ["x.py", "y.py"]
        gt = {"a.py", "b.py"}
        assert _recall_at_k(retrieved, gt, 10) == 0.0

    def test_empty_ground_truth(self):
        assert _recall_at_k(["a.py"], set(), 10) == 0.0

    def test_respects_k(self):
        retrieved = ["a.py", "b.py", "c.py"]
        gt = {"a.py", "c.py"}
        assert _recall_at_k(retrieved, gt, 1) == 0.5  # only a.py in top-1


class TestPearson:
    """Pearson correlation."""

    def test_perfect_positive(self):
        xs = [1.0, 2.0, 3.0]
        ys = [1.0, 2.0, 3.0]
        assert _pearson(xs, ys) == pytest.approx(1.0)

    def test_perfect_negative(self):
        xs = [1.0, 2.0, 3.0]
        ys = [3.0, 2.0, 1.0]
        assert _pearson(xs, ys) == pytest.approx(-1.0)

    def test_degenerate_input(self):
        assert _pearson([1.0], [1.0]) == 0.0
        assert _pearson([], []) == 0.0


class TestThresholds:
    """Pre-registered threshold gating."""

    def test_verdict_skip(self):
        assert _verdict(0.90) == "skip_embeddings"

    def test_verdict_enhancement(self):
        assert _verdict(0.75) == "enhancement"

    def test_verdict_required(self):
        assert _verdict(0.50) == "required"

    def test_verdict_boundary_skip(self):
        assert _verdict(THRESHOLD_SKIP) == "skip_embeddings"

    def test_verdict_boundary_enhancement(self):
        assert _verdict(THRESHOLD_ENHANCE) == "enhancement"


class TestBaselineReport:
    """run_baseline produces a valid report on a fixture db."""

    def test_report_structure(self, fixture_db, fixture_sidecar, tmp_path):
        # Insert a session_log row pointing to the fixture sidecar
        conn = sqlite3.connect(fixture_db)
        conn.execute(
            """INSERT INTO session_logs
               (session_id, run_id, task_id, attempt, stage, model, port,
                prompt_hash, prompt_truncated, prompt_payload_path,
                user_message, response_content, finish_reason,
                prompt_tokens, completion_tokens, total_tokens, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("test-T01-1-generate", "test", "T01", 1, "generate",
             "model", 8080, "hash", "trunc", fixture_sidecar,
             "msg", "resp", "stop", 100, 200, 300, 1000),
        )
        conn.commit()
        conn.close()

        report = run_baseline(db_path=fixture_db, top_k=10,
                              project_path=str(tmp_path))
        assert report["schema"] == "retrieval_baseline_v1"
        assert report["n_atoms"] == 1
        assert "aggregate" in report
        assert "verdict" in report["aggregate"]
        assert report["aggregate"]["verdict"] in (
            "skip_embeddings", "enhancement", "required")
        # Atom-level fields present
        atom = report["atoms"][0]
        assert "tokens_spent" in atom
        assert "grep_recall@10" in atom
        assert "repo_map_recall@10" in atom
        assert "ground_truth_files" in atom
