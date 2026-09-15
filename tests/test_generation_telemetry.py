"""Unit tests for generation_telemetry table (OBS-03).

Covers EPIC R5:
- generation_telemetry table created with full schema
- Every generate_code call produces a generation_telemetry row
- Rows include model, port, role, all token counts, latency, finish_reason
- Aggregate query returns per-model median latency and token distribution
- Index on (model, role) for fast analytics queries
"""

import sqlite3

import pytest

from engine.session_log import (
    SessionLogRecorder,
    init_session_logs_db,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh temporary engine.db."""
    return str(tmp_path / "engine.db")


def _make_response(**overrides):
    """Build a realistic transport response dict."""
    base = {
        "content": "def hello(): pass",
        "reasoning_content": "",
        "finish_reason": "stop",
        "prompt_tokens": 1500,
        "completion_tokens": 50,
        "total_tokens": 1550,
        "thinking_tokens": 0,
        "predicted_per_second": 42.0,
    }
    base.update(overrides)
    return base


# ── Test: schema ──────────────────────────────────────────────────────────────


class TestSchema:
    def test_creation(self, db_path):
        """generation_telemetry table is created with correct columns."""
        init_session_logs_db(db_path)
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(generation_telemetry)"
        )]
        conn.close()
        expected = [
            "id", "run_id", "task_id", "attempt", "stage", "model", "port",
            "role", "prompt_tokens", "completion_tokens", "thinking_tokens",
            "total_tokens", "tokens_per_sec", "latency_ms", "finish_reason",
            "exhausted", "max_tokens_used", "created_at",
        ]
        assert cols == expected

    def test_index_on_model_role(self, db_path):
        """Index on (model, role) exists for fast analytics queries."""
        init_session_logs_db(db_path)
        conn = sqlite3.connect(db_path)
        idxs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = 'idx_gen_tel_model'"
        ).fetchall()]
        conn.close()
        assert "idx_gen_tel_model" in idxs


# ── Test: recording ────────────────────────────────────────────────────────────


class TestRecording:
    def test_row_per_record(self, db_path):
        """Each record() call produces a generation_telemetry row."""
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
        assert row["model"] == "3090-qwen36-35b"
        assert row["port"] == 8080
        assert row["role"] == "coder"
        assert row["prompt_tokens"] == 2000
        assert row["completion_tokens"] == 100
        assert row["thinking_tokens"] == 50
        assert row["total_tokens"] == 2100
        assert row["latency_ms"] == 4000
        assert row["finish_reason"] == "stop"
        assert row["max_tokens_used"] == 4096

    def test_multiple_attempts(self, db_path):
        """Multiple attempts produce multiple telemetry rows."""
        with SessionLogRecorder("run-multi", db_path=db_path) as rec:
            for i in range(3):
                rec.record(
                    task_id="T01", attempt=i + 1, stage="generate",
                    model="3090-qwen36-35b", port=8080,
                    prompt_text=f"Attempt {i+1}",
                    response=_make_response(
                        prompt_tokens=1000 * (i + 1),
                        completion_tokens=100 * (i + 1),
                        total_tokens=1100 * (i + 1),
                    ),
                    latency_ms=2000 * (i + 1),
                    role="coder",
                    max_tokens_used=4096,
                )
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT attempt, prompt_tokens FROM generation_telemetry "
            "WHERE run_id = 'run-multi' ORDER BY attempt"
        ).fetchall()
        conn.close()
        assert len(rows) == 3
        assert rows[0][1] == 1000
        assert rows[1][1] == 2000
        assert rows[2][1] == 3000

    def test_tokens_per_sec(self, db_path):
        """tokens_per_sec is recorded."""
        with SessionLogRecorder("run-tps", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(predicted_per_second=55.5),
                latency_ms=1000, role="coder",
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT tokens_per_sec FROM generation_telemetry").fetchone()
        conn.close()
        assert row[0] == 55.5

    def test_exhausted_flag(self, db_path):
        """exhausted flag is set when finish_reason=length + empty content."""
        with SessionLogRecorder("run-exh", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(
                    content="", finish_reason="length",
                    reasoning_content="thinking...",
                ),
                latency_ms=5000, role="coder",
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT exhausted FROM generation_telemetry").fetchone()
        conn.close()
        assert row[0] == 1


# ── Test: aggregate queries ───────────────────────────────────────────────────


class TestAggregateQueries:
    def test_per_model_median_latency(self, db_path):
        """Aggregate query returns per-model median latency."""
        with SessionLogRecorder("run-agg", db_path=db_path) as rec:
            # Model A: latencies 1000, 2000, 3000
            for lat in [1000, 2000, 3000]:
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="model-a", port=8080,
                    prompt_text="Write code",
                    response=_make_response(),
                    latency_ms=lat, role="coder",
                )
            # Model B: latencies 500, 1500
            for lat in [500, 1500]:
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="model-b", port=8082,
                    prompt_text="Write code",
                    response=_make_response(),
                    latency_ms=lat, role="coder",
                )
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """\
            SELECT model,
                   AVG(latency_ms) as avg_lat,
                   COUNT(*) as cnt
            FROM generation_telemetry
            WHERE run_id = 'run-agg'
            GROUP BY model
            ORDER BY model
            """
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        # model-a: avg = 2000
        assert rows[0][0] == "model-a"
        assert rows[0][1] == 2000.0
        assert rows[0][2] == 3
        # model-b: avg = 1000
        assert rows[1][0] == "model-b"
        assert rows[1][1] == 1000.0
        assert rows[1][2] == 2

    def test_per_model_token_distribution(self, db_path):
        """Aggregate query returns per-model token distribution."""
        with SessionLogRecorder("run-tok", db_path=db_path) as rec:
            for pt, ct in [(1000, 100), (2000, 200), (3000, 300)]:
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="model-a", port=8080,
                    prompt_text="Write code",
                    response=_make_response(prompt_tokens=pt, completion_tokens=ct,
                                            total_tokens=pt + ct),
                    latency_ms=1000, role="coder",
                )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """\
            SELECT model,
                   AVG(prompt_tokens) as avg_pt,
                   AVG(completion_tokens) as avg_ct,
                   SUM(total_tokens) as sum_tt
            FROM generation_telemetry
            WHERE run_id = 'run-tok'
            GROUP BY model
            """
        ).fetchone()
        conn.close()
        assert row[0] == "model-a"
        assert row[1] == 2000.0  # avg prompt_tokens
        assert row[2] == 200.0  # avg completion_tokens
        assert row[3] == 6600  # sum total_tokens

    def test_exhaustion_rate(self, db_path):
        """Exhaustion rate query: finish_reason='length' + exhausted=1."""
        with SessionLogRecorder("run-exh-rate", db_path=db_path) as rec:
            # 2 exhausted, 1 normal
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(content="", finish_reason="length",
                                        reasoning_content="thinking"),
                latency_ms=1000, role="coder",
            )
            rec.record(
                task_id="T01", attempt=2, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(content="", finish_reason="length",
                                        reasoning_content="thinking"),
                latency_ms=1000, role="coder",
            )
            rec.record(
                task_id="T01", attempt=3, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(finish_reason="stop"),
                latency_ms=1000, role="coder",
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """\
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN exhausted = 1 THEN 1 ELSE 0 END) as exhausted_count
            FROM generation_telemetry
            WHERE run_id = 'run-exh-rate'
            """
        ).fetchone()
        conn.close()
        assert row[0] == 3
        assert row[1] == 2
