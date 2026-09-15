"""Unit tests for engine/atom_replay.py — replay_atom, replay_trace, compare_hashes."""

import json
import sqlite3
import subprocess
import sys

import pytest

from engine.atom_replay import _hash_dict, compare_hashes, replay_atom, replay_trace
from engine.trace import AtomRecorder, init_atoms_db


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh temporary engine.db."""
    p = str(tmp_path / "engine.db")
    init_atoms_db(p)
    return p


def _record_atoms(db_path, run_id, count=3, match=True):
    """Record *count* atoms with self-consistent hashes.

    After recording via AtomRecorder, we update each atom's ``output_hash``
    to equal ``_hash_dict({"from_state", "to_state", "meta"})`` so that
    ``replay_atom`` finds a match — simulating a deterministic mock replay.
    """
    rec = AtomRecorder(run_id, db_path)
    for i in range(1, count + 1):
        rec.record(
            task_id="T01",
            seq=i,
            from_state="IDLE",
            to_state="PARSING",
            inputs={"input": f"data_{i}"},
            outputs={"output": f"result_{i}"},
        )
    rec.close()

    # Patch output_hash so replay_atom's recomputed hash matches.
    conn = sqlite3.connect(db_path)
    for i in range(1, count + 1):
        atom_id = f"{run_id}-{i:04d}"
        expected_hash = _hash_dict({
            "from_state": "IDLE",
            "to_state": "PARSING",
            "meta": {},
        })
        conn.execute(
            "UPDATE atoms SET output_hash = ? WHERE atom_id = ?",
            (expected_hash, atom_id),
        )
    conn.commit()
    conn.close()


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestHashDict:
    def test_deterministic(self):
        """Same input always produces the same SHA-256 hex."""
        d = {"a": 1, "b": [2, 3]}
        assert _hash_dict(d) == _hash_dict(d)

    def test_hex_length(self):
        """Output is a 64-char hex string (SHA-256)."""
        assert len(_hash_dict({"x": 1})) == 64

    def test_key_order_irrelevant(self):
        """Dict key order does not affect the hash."""
        assert _hash_dict({"b": 2, "a": 1}) == _hash_dict({"a": 1, "b": 2})


class TestCompareHashes:
    def test_matching(self):
        """compare_hashes returns (True, None) for identical hashes."""
        h = _hash_dict({"x": 1})
        match, diff = compare_hashes(h, h)
        assert match is True
        assert diff is None

    def test_mismatching(self):
        """compare_hashes returns (False, diff_str) for different hashes."""
        match, diff = compare_hashes("aaa", "bbb")
        assert match is False
        assert "RECORDED: aaa" in diff
        assert "RECOMPUTED: bbb" in diff


class TestReplayAtom:
    def test_match_returns_true(self, db_path):
        """replay_atom returns match=True when hashes are consistent."""
        _record_atoms(db_path, "run-ok", count=1)
        rec = AtomRecorder("run-ok", db_path)
        atoms = rec.get_trace()
        rec.close()

        result = replay_atom(atoms[0])
        assert result["match"] is True
        assert result["atom_id"] == "run-ok-0001"
        assert result["diff"] is None

    def test_mismatch_returns_false(self, db_path):
        """replay_atom returns match=False when output_hash is tampered."""
        _record_atoms(db_path, "run-bad", count=1)

        # Tamper with the output_hash in the DB
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE atoms SET output_hash = ? WHERE atom_id = ?",
            ("0000tampered", "run-bad-0001"),
        )
        conn.commit()
        conn.close()

        rec = AtomRecorder("run-bad", db_path)
        atoms = rec.get_trace()
        rec.close()

        result = replay_atom(atoms[0])
        assert result["match"] is False
        assert result["diff"] is not None

    def test_verbose_diff_includes_atom_dump(self, db_path):
        """With verbose=True and mismatch, diff includes full atom JSON."""
        _record_atoms(db_path, "run-verb", count=1)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE atoms SET output_hash = ? WHERE atom_id = ?",
            ("bad", "run-verb-0001"),
        )
        conn.commit()
        conn.close()

        rec = AtomRecorder("run-verb", db_path)
        atoms = rec.get_trace()
        rec.close()

        result = replay_atom(atoms[0], verbose=True)
        assert "ATOM:" in result["diff"]
        assert "run-verb-0001" in result["diff"]


class TestReplayTrace:
    def test_returns_results_for_all_atoms(self, db_path):
        """replay_trace returns one result per atom in seq order."""
        _record_atoms(db_path, "run-trace", count=4)
        results = replay_trace("run-trace", db_path)

        # 4 matching atoms — no sentinel
        assert len(results) == 4
        assert all(r["match"] for r in results)

    def test_stops_at_first_divergence(self, db_path):
        """replay_trace stops at first mismatch and appends sentinel."""
        _record_atoms(db_path, "run-stop", count=5)

        # Tamper atom at seq=2
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE atoms SET output_hash = ? WHERE atom_id = ?",
            ("bad-hash", "run-stop-0002"),
        )
        conn.commit()
        conn.close()

        results = replay_trace("run-stop", db_path)

        # seq=1 PASS, seq=2 FAIL, sentinel — 3 items total
        assert len(results) == 3
        assert results[0]["match"] is True
        assert results[1]["match"] is False
        assert results[1]["atom_id"] == "run-stop-0002"
        assert "stopped_at" in results[2]
        assert results[2]["stopped_at"] == "run-stop-0002"


class TestCLI:
    def test_help_exits_0(self):
        """CLI --help exits with code 0."""
        result = subprocess.run(
            [sys.executable, "engine/atom_replay.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(pytest.config.rootdir) if hasattr(pytest, "config") else ".",
        )
        # Also try from the coder-harness dir
        import os
        cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Go up two levels from tests/unit/ to coder-harness/
        harness_dir = os.path.dirname(cwd)
        result = subprocess.run(
            [sys.executable, "engine/atom_replay.py", "--help"],
            capture_output=True,
            text=True,
            cwd=harness_dir,
        )
        assert result.returncode == 0
        assert "--atom" in result.stdout or "--help" in result.stdout
