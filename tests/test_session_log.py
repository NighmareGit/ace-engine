"""Unit + integration tests for engine/session_log.py — SessionLogRecorder.

Covers EPIC R1, R2, R3, R8:
- session_logs table created with correct schema
- SessionLogRecorder mirrors AtomRecorder context-manager API
- Prompt hashing is deterministic
- Truncation: prompt > 4000 chars → prompt_truncated = first 4000 chars
- Sidecar: full prompt written to engine_run_<id>_session_payloads.jsonl
- Context manager: close() on __exit__
- atom_id foreign key links to atoms table
"""

import json
import os
import sqlite3

import pytest

from engine.session_log import (
    SESSION_LOGS_DDL,
    SessionLogRecorder,
    _hash_text,
    _truncate,
    _cap_response,
    init_session_logs_db,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh temporary engine.db."""
    return str(tmp_path / "engine.db")


@pytest.fixture()
def run_dir(tmp_path):
    """Return a temporary run directory."""
    d = tmp_path / "run-dir"
    d.mkdir()
    return str(d)


def _make_response(**overrides):
    """Build a realistic transport response dict."""
    base = {
        "content": "def hello(): pass",
        "reasoning_content": "I need to write a hello function...",
        "finish_reason": "stop",
        "prompt_tokens": 1500,
        "completion_tokens": 50,
        "total_tokens": 1550,
        "thinking_tokens": 0,
        "predicted_per_second": 42.0,
    }
    base.update(overrides)
    return base


# ── Test: DDL / schema ────────────────────────────────────────────────────────


class TestSchema:
    def test_creates_session_logs_table(self, db_path):
        """init_session_logs_db creates the session_logs table."""
        init_session_logs_db(db_path)
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(session_logs)")]
        conn.close()
        expected = [
            "session_id", "run_id", "task_id", "attempt", "stage", "atom_id",
            "model", "port", "prompt_hash", "prompt_truncated",
            "prompt_payload_path", "system_message", "user_message",
            "response_content", "response_reasoning", "finish_reason",
            "prompt_tokens", "completion_tokens", "total_tokens",
            "thinking_tokens", "latency_ms", "exhausted", "error", "created_at",
        ]
        assert cols == expected

    def test_creates_generation_telemetry_table(self, db_path):
        """init_session_logs_db creates the generation_telemetry table."""
        init_session_logs_db(db_path)
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(generation_telemetry)")]
        conn.close()
        expected = [
            "id", "run_id", "task_id", "attempt", "stage", "model", "port",
            "role", "prompt_tokens", "completion_tokens", "thinking_tokens",
            "total_tokens", "tokens_per_sec", "latency_ms", "finish_reason",
            "exhausted", "max_tokens_used", "created_at",
        ]
        assert cols == expected

    def test_creates_indexes(self, db_path):
        """Indexes on (run_id, task_id) and (atom_id) exist."""
        init_session_logs_db(db_path)
        conn = sqlite3.connect(db_path)
        idxs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_session_logs%'"
        ).fetchall()]
        conn.close()
        assert "idx_session_logs_run_task" in idxs
        assert "idx_session_logs_atom" in idxs

    def test_idempotent(self, db_path):
        """Calling init_session_logs_db twice is safe."""
        init_session_logs_db(db_path)
        init_session_logs_db(db_path)  # no error


# ── Test: record() ────────────────────────────────────────────────────────────


class TestRecord:
    def test_inserts_row_and_returns_session_id(self, db_path):
        """record() inserts a row and returns a valid session_id."""
        with SessionLogRecorder("run-test", db_path=db_path) as rec:
            sid = rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write a hello function",
                response=_make_response(),
                latency_ms=3200,
            )
        assert sid == "run-test-T01-1-generate"

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT * FROM session_logs WHERE session_id = ?", (sid,)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_session_id_format(self, db_path):
        """session_id matches {run_id}-{task_id}-{attempt}-{stage} format."""
        with SessionLogRecorder("run-abc", db_path=db_path) as rec:
            sid = rec.record(
                task_id="T02", attempt=3, stage="replan",
                model="9b-mtp", port=8082,
                prompt_text="Fix the import",
                response=_make_response(),
                latency_ms=1500,
            )
        assert sid == "run-abc-T02-3-replan"

    def test_writes_generation_telemetry_row(self, db_path):
        """record() also writes a generation_telemetry row."""
        with SessionLogRecorder("run-tel", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(prompt_tokens=2000, completion_tokens=100,
                                        total_tokens=2100, thinking_tokens=50),
                latency_ms=4000, role="coder", max_tokens_used=4096,
            )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM generation_telemetry WHERE run_id = 'run-tel'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["prompt_tokens"] == 2000
        assert row["completion_tokens"] == 100
        assert row["thinking_tokens"] == 50
        assert row["role"] == "coder"
        assert row["max_tokens_used"] == 4096

    def test_captures_reasoning_content(self, db_path):
        """reasoning_content and finish_reason are captured."""
        with SessionLogRecorder("run-reason", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(
                    reasoning_content="Let me think step by step...",
                    finish_reason="stop",
                ),
                latency_ms=2000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT response_reasoning, finish_reason FROM session_logs"
        ).fetchone()
        conn.close()
        assert row[0] == "Let me think step by step..."
        assert row[1] == "stop"

    def test_exhaustion_flag(self, db_path):
        """Exhaustion detected: empty content + finish_reason=length + thinking."""
        with SessionLogRecorder("run-exh", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(
                    content="",
                    finish_reason="length",
                    reasoning_content="I was thinking...",
                ),
                latency_ms=5000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT exhausted FROM session_logs").fetchone()
        conn.close()
        assert row[0] == 1

    def test_error_field(self, db_path):
        """Transport error is recorded."""
        with SessionLogRecorder("run-err", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response={"content": "", "finish_reason": "stop",
                          "prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "thinking_tokens": 0},
                latency_ms=0,
                error="Connection refused",
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT error FROM session_logs").fetchone()
        conn.close()
        assert row[0] == "Connection refused"


# ── Test: hashing ─────────────────────────────────────────────────────────────


class TestHashing:
    def test_deterministic(self):
        """Same prompt → same hash."""
        h1 = _hash_text("Write a hello function")
        h2 = _hash_text("Write a hello function")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_prompts_different_hashes(self):
        """Different prompts → different hashes."""
        h1 = _hash_text("Write a hello function")
        h2 = _hash_text("Write a goodbye function")
        assert h1 != h2

    def test_prompt_hash_stored(self, db_path):
        """prompt_hash in DB matches SHA-256 of the full prompt."""
        prompt = "Write a hello function"
        with SessionLogRecorder("run-hash", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_hash FROM session_logs").fetchone()
        conn.close()
        assert row[0] == _hash_text(prompt)


# ── Test: truncation ──────────────────────────────────────────────────────────


class TestTruncation:
    def test_short_prompt_not_truncated(self, db_path):
        """Prompt <= 4000 chars is stored in full."""
        prompt = "short prompt"
        with SessionLogRecorder("run-short", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_truncated FROM session_logs").fetchone()
        conn.close()
        assert row[0] == prompt

    def test_long_prompt_truncated(self, db_path):
        """Prompt > 4000 chars → prompt_truncated = first 4000 chars + marker."""
        prompt = "A" * 5000
        with SessionLogRecorder("run-long", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_truncated FROM session_logs").fetchone()
        conn.close()
        assert len(row[0]) < 5000  # truncated
        assert row[0].startswith("A" * 100)
        assert "truncated" in row[0]

    def test_truncate_helper(self):
        """_truncate truncates at the limit."""
        assert _truncate("abc", 4000) == "abc"
        long = "x" * 5000
        result = _truncate(long, 4000)
        assert len(result) == 4000 + len("\n... [truncated, 5000 chars total]")

    def test_cap_response_helper(self):
        """_cap_response caps at 16 KB."""
        assert _cap_response("short", 16_384) == "short"
        huge = "y" * 20_000
        result = _cap_response(huge, 16_384)
        assert len(result) < 20_000
        assert "capped" in result


# ── Test: sidecar ─────────────────────────────────────────────────────────────


class TestSidecar:
    def test_sidecar_written(self, db_path, run_dir):
        """Full prompt written to sidecar JSONL."""
        with SessionLogRecorder("run-side", db_path=db_path, run_dir=run_dir) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write a hello function",
                response=_make_response(content="def hello(): pass"),
                latency_ms=1000,
            )
        sidecar = os.path.join(run_dir, "engine_run_run-side_session_payloads.jsonl")
        assert os.path.exists(sidecar)
        with open(sidecar) as f:
            entry = json.loads(f.readline())
        assert entry["prompt_text"] == "Write a hello function"
        assert entry["response_content"] == "def hello(): pass"
        assert entry["session_id"] == "run-side-T01-1-generate"

    def test_sidecar_path_in_row(self, db_path, run_dir):
        """prompt_payload_path in DB row points to the sidecar."""
        with SessionLogRecorder("run-path", db_path=db_path, run_dir=run_dir) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_payload_path FROM session_logs").fetchone()
        conn.close()
        assert "engine_run_run-path_session_payloads.jsonl" in row[0]

    def test_sidecar_multiple_records(self, db_path, run_dir):
        """Multiple records append to the same sidecar."""
        with SessionLogRecorder("run-multi", db_path=db_path, run_dir=run_dir) as rec:
            for i in range(3):
                rec.record(
                    task_id="T01", attempt=i + 1, stage="generate",
                    model="3090-qwen36-35b", port=8080,
                    prompt_text=f"Attempt {i+1}",
                    response=_make_response(content=f"# attempt {i+1}"),
                    latency_ms=1000,
                )
        sidecar = os.path.join(run_dir, "engine_run_run-multi_session_payloads.jsonl")
        with open(sidecar) as f:
            lines = f.readlines()
        assert len(lines) == 3


# ── Test: context manager ─────────────────────────────────────────────────────


class TestContextManager:
    def test_works_as_context_manager(self, db_path):
        """SessionLogRecorder functions correctly as a context manager."""
        with SessionLogRecorder("r1", db_path=db_path) as rec:
            sid = rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000,
            )
            assert sid == "r1-T01-1-generate"
            logs = rec.get_logs()
            assert len(logs) == 1
        # After exit, connection should be closed
        assert rec._conn is None

    def test_record_after_close_raises(self, db_path):
        """record() after close raises RuntimeError."""
        rec = SessionLogRecorder("r2", db_path=db_path)
        rec.close()
        with pytest.raises(RuntimeError, match="closed"):
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000,
            )


# ── Test: atom link ───────────────────────────────────────────────────────────


class TestAtomLink:
    def test_atom_id_foreign_key(self, db_path):
        """atom_id links to atoms table (R8)."""
        # Create atoms table + a row first.
        from engine.trace import ATOMS_DDL, AtomRecorder
        conn = sqlite3.connect(db_path)
        conn.execute(ATOMS_DDL)
        conn.commit()
        conn.close()
        with AtomRecorder("run-atom", db_path=db_path) as atom_rec:
            atom_id = atom_rec.record(
                task_id="T01", seq=1,
                from_state="GENERATE", to_state="VALIDATE",
                inputs={"prompt": "x"}, outputs={"code": "y"},
            )
        # Now record a session log with that atom_id.
        with SessionLogRecorder("run-atom", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000,
                atom_id=atom_id,
            )
        # Join atoms NATURAL JOIN session_logs.
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """\
            SELECT a.atom_id, s.session_id, s.prompt_truncated
            FROM atoms a
            JOIN session_logs s ON a.atom_id = s.atom_id
            """
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == atom_id


# ── Test: get_logs ─────────────────────────────────────────────────────────────


class TestGetLogs:
    def test_get_logs_by_run(self, db_path):
        """get_logs returns all logs for the run."""
        with SessionLogRecorder("run-get", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000,
            )
            rec.record(
                task_id="T02", attempt=1, stage="generate",
                model="9b-mtp", port=8082,
                prompt_text="Write more code",
                response=_make_response(),
                latency_ms=2000,
            )
        with SessionLogRecorder("run-get", db_path=db_path) as rec:
            logs = rec.get_logs()
        assert len(logs) == 2

    def test_get_logs_by_task(self, db_path):
        """get_logs filters by task_id."""
        with SessionLogRecorder("run-task", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000,
            )
            rec.record(
                task_id="T02", attempt=1, stage="generate",
                model="9b-mtp", port=8082,
                prompt_text="Write more code",
                response=_make_response(),
                latency_ms=2000,
            )
        with SessionLogRecorder("run-task", db_path=db_path) as rec:
            logs = rec.get_logs(task_id="T01")
        assert len(logs) == 1
        assert logs[0]["task_id"] == "T01"
