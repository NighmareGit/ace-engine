"""Unit tests for engine/forensics.py — bisect, bisect_report, CLI, NFR-B4."""

import sqlite3
import subprocess
import sys
import time

import pytest

from engine.atom_replay import _hash_dict
from engine.forensics import bisect, bisect_report
from engine.trace import AtomRecorder, init_atoms_db


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh temporary engine.db."""
    p = str(tmp_path / "engine.db")
    init_atoms_db(p)
    return p


def _record_atoms(db_path, run_id, count, mismatch_at=None):
    """Record *count* atoms with self-consistent hashes.

    After recording via AtomRecorder, we update each atom's ``output_hash``
    to equal ``_hash_dict({"from_state", "to_state", "meta"})`` so that
    ``replay_atom`` finds a match — simulating a deterministic mock replay.

    If *mismatch_at* is given, that atom's hash is replaced with a bad value.
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
    expected_hash = _hash_dict({
        "from_state": "IDLE",
        "to_state": "PARSING",
        "meta": {},
    })
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE atoms SET output_hash = ? WHERE run_id = ?",
        (expected_hash, run_id),
    )
    conn.commit()

    if mismatch_at is not None:
        conn.execute(
            "UPDATE atoms SET output_hash = ? WHERE run_id = ? AND seq = ?",
            ("0000tampered", run_id, mismatch_at),
        )
        conn.commit()
    conn.close()


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestBisectAllMatch:
    def test_no_divergence(self, db_path):
        """bisect returns diverged=False and all_match=True when all atoms match."""
        _record_atoms(db_path, "run-all-ok", count=5)
        result = bisect("run-all-ok", db_path)

        assert result["diverged"] is False
        assert result["all_match"] is True
        assert result["atom_count"] == 5


class TestBisectDivergence:
    def test_mismatch_at_seq3(self, db_path):
        """bisect finds divergence at the correct atom."""
        _record_atoms(db_path, "run-div3", count=5, mismatch_at=3)
        result = bisect("run-div3", db_path)

        assert result["diverged"] is True
        assert result["atom_id"] == "run-div3-0003"
        assert result["seq"] == 3
        assert result["atoms_before_divergence"] == 2

    def test_stops_at_first_divergence(self, db_path):
        """bisect only reports the first mismatch, not later ones."""
        # Tamper at seq=2 AND seq=5
        _record_atoms(db_path, "run-multi", count=6)
        conn = sqlite3.connect(db_path)
        for seq in (2, 5):
            conn.execute(
                "UPDATE atoms SET output_hash = ? WHERE run_id = ? AND seq = ?",
                ("bad", "run-multi", seq),
            )
        conn.commit()
        conn.close()

        result = bisect("run-multi", db_path)

        assert result["diverged"] is True
        assert result["seq"] == 2
        assert result["atom_id"] == "run-multi-0002"


class TestBisectEmpty:
    def test_empty_trace(self, db_path):
        """bisect on empty trace returns diverged=False, atom_count=0."""
        # No atoms recorded for this run_id
        result = bisect("nonexistent-run", db_path)
        assert result["diverged"] is False
        assert result["atom_count"] == 0
        assert result["all_match"] is True


class TestBisectReport:
    def test_divergence_report(self):
        """bisect_report contains 'DIVERGENCE FOUND' when diverged=True."""
        result = {
            "diverged": True,
            "atom_id": "run-0003",
            "seq": 3,
            "recorded_hash": "aaa",
            "recomputed_hash": "bbb",
            "diff": "RECORDED: aaa\nRECOMPUTED: bbb",
            "atoms_before_divergence": 2,
        }
        report = bisect_report(result)
        assert "DIVERGENCE FOUND" in report
        assert "run-0003" in report
        assert "seq 3" in report

    def test_all_match_report(self):
        """bisect_report contains 'ALL ATOMS MATCH' when diverged=False."""
        result = {
            "diverged": False,
            "atom_count": 10,
            "all_match": True,
        }
        report = bisect_report(result)
        assert "ALL ATOMS MATCH" in report
        assert "10 atoms verified" in report


class TestBisect50AtomsPerformance:
    def test_bisect_50_atoms_performance(self, tmp_path):
        """NFR-B4: Bisect finds first divergent atom in ≤2 min on a 50-atom trace."""
        db = str(tmp_path / "test.db")
        init_atoms_db(db)
        rec = AtomRecorder("perf-run", db)
        for i in range(50):
            rec.record(
                "T01", i + 1, "IDLE", "PARSING",
                {"input": f"data_{i}"}, {"output": f"result_{i}"},
            )
        rec.close()

        # Patch output_hash so replay_atom finds all atoms deterministic.
        expected_hash = _hash_dict({
            "from_state": "IDLE",
            "to_state": "PARSING",
            "meta": {},
        })
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE atoms SET output_hash = ? WHERE run_id = ?",
            (expected_hash, "perf-run"),
        )
        conn.commit()
        conn.close()

        start = time.time()
        result = bisect("perf-run", db)
        elapsed = time.time() - start

        assert result["atom_count"] == 50
        assert result["all_match"] is True
        assert elapsed < 120, f"Bisect took {elapsed:.1f}s, NFR-B4 requires < 120s"


class TestCLIBisectHelp:
    def test_help_exits_0(self):
        """CLI bisect --help exits 0."""
        import os
        harness_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        result = subprocess.run(
            [sys.executable, "engine/forensics.py", "bisect", "--help"],
            capture_output=True,
            text=True,
            cwd=harness_dir,
        )
        assert result.returncode == 0
        assert "--run" in result.stdout
        assert "--db" in result.stdout
        assert "--verbose" in result.stdout
