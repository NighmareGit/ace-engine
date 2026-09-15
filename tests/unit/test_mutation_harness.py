"""Unit tests for engine/mutation_harness.py — gate sensitivity testing."""

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from engine.mutation_harness import (
    MUTATION_CORPUS,
    run_corpus,
    run_mutation_test,
)


class TestCorpusDetection:
    """Test that each mutation in the corpus is correctly handled."""

    def test_all_mutations_detected(self):
        """Each mutation should be correctly_detected by the validator."""
        for m in MUTATION_CORPUS:
            r = run_mutation_test(m)
            assert r["correctly_detected"], (
                f"Mutation '{m['id']}' was NOT correctly detected "
                f"(detected_by={r['detected_by']}, expected_gate={m['expected_gate']})"
            )

    def test_corpus_sensitivity(self):
        """Overall sensitivity should be >= 0.95 (NFR-B3)."""
        report = run_corpus()
        assert report["sensitivity"] >= 0.95, (
            f"Sensitivity {report['sensitivity']:.0%} < 95% threshold"
        )

    def test_per_gate_covers_all_expected(self):
        """Per-gate breakdown should include ast, imports, execution."""
        report = run_corpus()
        expected_gates = {"ast", "imports", "execution"}
        assert set(report["per_gate"].keys()) == expected_gates


class TestCleanCode:
    """Test that clean code passes validation."""

    def test_clean_code_passes(self):
        """Valid code should pass all stages with detected_by=None."""
        r = run_mutation_test({
            "id": "clean",
            "code": "x = 1\nprint(x)",
            "expected_gate": "none",
            "expected_catch": False,
        })
        assert r["passed_validation"] is True
        assert r["detected_by"] is None
        assert r["correctly_detected"] is True


class TestCLI:
    """Test CLI entry point."""

    def test_help_exits_zero(self):
        """--help should exit 0."""
        result = subprocess.run(
            [sys.executable, "-m", "engine.mutation_harness", "--help"],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0

    def test_json_output_valid(self):
        """--json should output valid JSON with 'sensitivity' key."""
        result = subprocess.run(
            [sys.executable, "-m", "engine.mutation_harness", "--json"],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        data = json.loads(result.stdout)
        assert "sensitivity" in data
        assert "per_gate" in data
        assert "results" in data
