"""Tests for the ralph CLI subcommands (M4)."""

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "engine.db")
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        import importlib
        import engine.state
        importlib.reload(engine.state)
        yield db_path


class TestRalphCli:
    def test_status_lists_runs(self, _tmp_db):
        """ralph status with no --run-id lists runs."""
        from engine.cli import cmd_ralph_status
        class _Args:
            run_id = None
        result = cmd_ralph_status(_Args())
        assert "runs" in result

    def test_approve_pattern(self, _tmp_db):
        """ralph approve-pattern transitions a proposed pattern."""
        from engine.workflows.ralph.pattern_gate import (
            propose_pattern_update, persist_pattern,
        )
        from engine.cli import cmd_ralph_approve_pattern

        class _FakeRound:
            ralph_run_id = "r1"
            round_id = 1

        prop = propose_pattern_update(_FakeRound(), body="test pattern")
        persist_pattern(prop)

        class _Args:
            pattern_id = prop.pattern_id

        result = cmd_ralph_approve_pattern(_Args())
        assert result["approved"] is True

    def test_reject_pattern(self, _tmp_db):
        from engine.workflows.ralph.pattern_gate import (
            propose_pattern_update, persist_pattern,
        )
        from engine.cli import cmd_ralph_reject_pattern

        class _FakeRound:
            ralph_run_id = "r1"
            round_id = 1

        prop = propose_pattern_update(_FakeRound(), body="bad pattern")
        persist_pattern(prop)

        class _Args:
            pattern_id = prop.pattern_id

        result = cmd_ralph_reject_pattern(_Args())
        assert result["rejected"] is True

    def test_dispatch_has_ralph_entries(self, _tmp_db):
        """Verify ralph commands are registered in dispatch."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "engine.cli", "ralph", "--help"],
            capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), "..", "..", ".."),
        )
        assert result.returncode == 0
        assert "run" in result.stdout
        assert "resume" in result.stdout
        assert "status" in result.stdout
        assert "approve-pattern" in result.stdout
        assert "reject-pattern" in result.stdout
