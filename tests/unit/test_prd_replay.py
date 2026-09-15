"""Unit tests for engine/prd_replay.py — full PRD replay runner + JSON verdict."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from engine.prd_replay import make_verdict, replay_prd
from engine.trace import AtomRecorder


# ── Verdict schema tests ──────────────────────────────────────────────────


class TestMakeVerdict:
    """Test make_verdict produces the correct schema."""

    SCHEMA_KEYS = {"run_id", "atoms", "replayed", "diverged_at", "gate_sensitivity", "pass"}

    def test_schema_keys_and_types(self):
        """All 6 keys present, types correct."""
        v = make_verdict("test", 5, 5, None, 1.0, True)
        assert set(v.keys()) == self.SCHEMA_KEYS
        assert isinstance(v["run_id"], str)
        assert isinstance(v["atoms"], int)
        assert isinstance(v["replayed"], int)
        assert v["diverged_at"] is None
        assert isinstance(v["gate_sensitivity"], float)
        assert v["pass"] is True

    def test_passing_verdict(self):
        """No divergence, sensitivity >= 0.95 → pass."""
        v = make_verdict("golden-test-prd", 12, 12, None, 1.0, True)
        assert v["pass"] is True
        assert v["diverged_at"] is None

    def test_failing_verdict_diverged(self):
        """Divergence at atom r1-0003 → fail."""
        v = make_verdict("golden-test-prd", 12, 11, "r1-0003", 0.8, False)
        assert v["pass"] is False
        assert v["diverged_at"] == "r1-0003"
        assert v["gate_sensitivity"] == 0.8

    def test_failing_verdict_low_sensitivity(self):
        """Low sensitivity even without divergence → fail."""
        v = make_verdict("test", 10, 10, None, 0.6, False)
        assert v["pass"] is False
        assert v["diverged_at"] is None
        assert v["gate_sensitivity"] == 0.6


# ── CLI tests ──────────────────────────────────────────────────────────────


class TestCLI:
    """Test CLI entry point."""

    def test_help_exits_zero(self):
        """--help should exit 0."""
        result = subprocess.run(
            [sys.executable, "-m", "engine.prd_replay", "--help"],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0

    def test_help_shows_flags(self):
        """--help output should mention --prd, --all, --db, --verbose, --json."""
        result = subprocess.run(
            [sys.executable, "-m", "engine.prd_replay", "--help"],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        for flag in ["--prd", "--all", "--db", "--verbose", "--json"]:
            assert flag in result.stdout, f"Flag {flag} missing from --help output"


# ── replay_prd integration test ────────────────────────────────────────────


class TestReplayPRD:
    """Test replay_prd returns a valid verdict dict."""

    def test_replay_prd_returns_verdict_keys(self):
        """replay_prd() with a golden PRD returns all 6 verdict keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            # Create golden PRD + atom trace
            prd_path = os.path.join(tmpdir, "golden-trivial.md")
            with open(prd_path, "w") as f:
                f.write("# Test PRD\n\nA trivial test PRD.\n")

            # Record atoms to the temp DB — use from/to/meta as outputs
            # so that replay_atom's hash recomputation matches the recorded hash.
            rec = AtomRecorder("golden-trivial", db_path)
            rec.record(
                task_id="T01", seq=1,
                from_state="IDLE", to_state="PARSING",
                inputs={"prompt": "test"},
                outputs={"from_state": "IDLE", "to_state": "PARSING", "meta": {}},
            )
            rec.close()

            # Mock run_corpus to avoid real validator execution in unit tests
            from unittest.mock import patch
            with patch("engine.prd_replay.run_corpus") as mock_corpus:
                mock_corpus.return_value = {
                    "total": 5, "detected": 5, "sensitivity": 1.0,
                    "per_gate": {}, "results": [],
                }
                verdict = replay_prd(prd_path, db_path, verbose=False)

            assert isinstance(verdict, dict)
            assert set(verdict.keys()) == {"run_id", "atoms", "replayed",
                                           "diverged_at", "gate_sensitivity", "pass"}
            assert verdict["run_id"] == "golden-trivial"
            assert verdict["atoms"] >= 1
            assert verdict["pass"] is True

    def test_replay_prd_all_keys_match_schema(self):
        """Verdict from replay_prd matches the exact BACKTEST-DESIGN schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            prd_path = os.path.join(tmpdir, "golden-smoke.md")
            with open(prd_path, "w") as f:
                f.write("# Smoke\n\nMinimal.\n")

            # Record 2 atoms — use from/to/meta as outputs so replay hash matches
            rec = AtomRecorder("golden-smoke", db_path)
            for i in range(1, 3):
                rec.record(
                    task_id="T01", seq=i,
                    from_state="IDLE", to_state="DONE",
                    inputs={"step": i},
                    outputs={"from_state": "IDLE", "to_state": "DONE", "meta": {}},
                )
            rec.close()

            from unittest.mock import patch
            with patch("engine.prd_replay.run_corpus") as mock:
                mock.return_value = {
                    "total": 5, "detected": 5, "sensitivity": 1.0,
                    "per_gate": {}, "results": [],
                }
                verdict = replay_prd(prd_path, db_path)

            # Check all required keys exist and have correct types
            assert isinstance(verdict["run_id"], str)
            assert isinstance(verdict["atoms"], int)
            assert isinstance(verdict["replayed"], int)
            assert verdict["diverged_at"] is None or isinstance(verdict["diverged_at"], str)
            assert isinstance(verdict["gate_sensitivity"], float)
            assert isinstance(verdict["pass"], bool)
