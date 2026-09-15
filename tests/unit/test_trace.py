"""Unit tests for engine/trace.py — AtomRecorder and atoms table DDL."""

import sqlite3

import pytest

from engine.trace import ATOMS_DDL, AtomRecorder, _hash_dict, init_atoms_db


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh temporary engine.db."""
    return str(tmp_path / "engine.db")


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestInitAtomsDb:
    def test_creates_atoms_table(self, db_path):
        """init_atoms_db creates the atoms table with correct columns."""
        init_atoms_db(db_path)
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(atoms)")]
        conn.close()
        expected = [
            "atom_id", "run_id", "task_id", "seq", "from_state", "to_state",
            "input_hash", "output_hash", "gate_hash", "duration_ms",
            "meta_json", "created_at",
        ]
        assert cols == expected

    def test_idempotent(self, db_path):
        """Calling init_atoms_db twice is safe."""
        init_atoms_db(db_path)
        init_atoms_db(db_path)  # no error


class TestRecord:
    def test_inserts_row_and_returns_atom_id(self, db_path):
        """record() inserts a row and returns a valid atom_id."""
        with AtomRecorder("run-test", db_path) as rec:
            aid = rec.record(
                task_id="T01",
                seq=1,
                from_state="IDLE",
                to_state="PARSING",
                inputs={"prompt": "hello"},
                outputs={"parsed": True},
            )
        assert aid == "run-test-0001"

        # Verify row exists in DB
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT * FROM atoms WHERE atom_id = ?", (aid,)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_atom_id_format(self, db_path):
        """atom_id matches {run_id}-{seq:04d} format."""
        with AtomRecorder("run-abc", db_path) as rec:
            aid = rec.record(
                task_id="T01", seq=3,
                from_state="IDLE", to_state="PARSING",
                inputs={}, outputs={},
            )
        assert aid == "run-abc-0003"


class TestGetTrace:
    def test_returns_atoms_in_seq_order(self, db_path):
        """get_trace returns atoms ordered by seq."""
        with AtomRecorder("run-order", db_path) as rec:
            rec.record(
                task_id="T02", seq=2, from_state="IDLE", to_state="PARSING",
                inputs={}, outputs={},
            )
            rec.record(
                task_id="T01", seq=1, from_state="IDLE", to_state="QUEUED",
                inputs={}, outputs={},
            )
            rec.record(
                task_id="T03", seq=3, from_state="PARSING", to_state="QUEUED",
                inputs={}, outputs={},
            )
        with AtomRecorder("run-order", db_path) as rec:
            trace = rec.get_trace()

        seqs = [a["seq"] for a in trace]
        assert seqs == [1, 2, 3]

    def test_get_trace_default_run_id(self, db_path):
        """get_trace without run_id argument uses self.run_id."""
        with AtomRecorder("run-default", db_path) as rec:
            rec.record(
                task_id="T01", seq=1, from_state="IDLE", to_state="PARSING",
                inputs={}, outputs={},
            )
            trace = rec.get_trace()  # no argument
        assert len(trace) == 1
        assert trace[0]["run_id"] == "run-default"


class TestHashDict:
    def test_deterministic(self):
        """_hash_dict produces identical SHA-256 hex for same input."""
        d = {"a": 1, "b": [2, 3]}
        h1 = _hash_dict(d)
        h2 = _hash_dict(d)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex length


class TestContextManager:
    def test_works_as_context_manager(self, db_path):
        """AtomRecorder functions correctly as a context manager."""
        with AtomRecorder("r1", db_path) as rec:
            aid = rec.record(
                task_id="T01", seq=1, from_state="IDLE", to_state="PARSING",
                inputs={"x": 1}, outputs={"y": 2},
            )
            assert aid == "r1-0001"
            trace = rec.get_trace()
            assert len(trace) == 1
        # After exit, connection should be closed
        assert rec._conn is None
