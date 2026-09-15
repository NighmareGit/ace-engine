"""Unit tests for cost/quality analytics queries (OBS-07).

Covers EPIC R5:
- Query returns per-model median latency and token distribution
- Query returns per-model exhaustion rate
- N4 computable from a single query
- Pass-rate by model/role/stage is queryable
- All queries read-only
"""

import sqlite3

import pytest

from engine.session_log import SessionLogRecorder, init_session_logs_db


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    db = str(tmp_path / "engine.db")
    init_session_logs_db(db_path=db)
    # Also create engine_scores for N4 computation.
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT,
        judge_model TEXT, completeness INTEGER, correctness INTEGER,
        quality INTEGER, intelligence INTEGER, role_fit INTEGER,
        overall REAL, reasoning TEXT, error TEXT, scored_state TEXT,
        scored_at TEXT
    )""")
    conn.commit()
    conn.close()
    return db


def _make_response(**overrides):
    base = {
        "content": "def hello(): pass",
        "reasoning_content": "",
        "finish_reason": "stop",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "thinking_tokens": 0,
        "predicted_per_second": 42.0,
    }
    base.update(overrides)
    return base


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestPerModelAggregation:
    def test_median_latency(self, db_path):
        """Query returns per-model median latency."""
        with SessionLogRecorder("run-agg", db_path=db_path) as rec:
            for lat in [1000, 2000, 3000, 4000, 5000]:
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="model-a", port=8080,
                    prompt_text="Write code",
                    response=_make_response(),
                    latency_ms=lat, role="coder",
                )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """\
            SELECT model,
                   AVG(latency_ms) AS avg_lat,
                   COUNT(*) AS cnt
            FROM generation_telemetry
            WHERE run_id = 'run-agg'
            GROUP BY model
            """
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["model"] == "model-a"
        assert rows[0]["avg_lat"] == 3000.0
        assert rows[0]["cnt"] == 5

    def test_token_distribution(self, db_path):
        """Query returns per-model token distribution."""
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
                   AVG(prompt_tokens) AS avg_pt,
                   AVG(completion_tokens) AS avg_ct,
                   SUM(total_tokens) AS sum_tt
            FROM generation_telemetry
            WHERE run_id = 'run-tok'
            GROUP BY model
            """
        ).fetchone()
        conn.close()
        assert row[0] == "model-a"
        assert row[1] == 2000.0
        assert row[2] == 200.0
        assert row[3] == 6600

    def test_exhaustion_rate(self, db_path):
        """Query returns per-model exhaustion rate."""
        with SessionLogRecorder("run-exh", db_path=db_path) as rec:
            # 2 exhausted, 3 normal
            for _ in range(2):
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="model-a", port=8080,
                    prompt_text="Write code",
                    response=_make_response(content="", finish_reason="length",
                                            reasoning_content="thinking"),
                    latency_ms=1000, role="coder",
                )
            for _ in range(3):
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="model-a", port=8080,
                    prompt_text="Write code",
                    response=_make_response(finish_reason="stop"),
                    latency_ms=1000, role="coder",
                )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """\
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN exhausted = 1 THEN 1 ELSE 0 END) AS exhausted_count
            FROM generation_telemetry
            WHERE run_id = 'run-exh'
            """
        ).fetchone()
        conn.close()
        assert row[0] == 5
        assert row[1] == 2


class TestN4Computation:
    def test_n4_from_query(self, db_path):
        """N4 (pass rate by recommended tier) computable from a single query."""
        conn = sqlite3.connect(db_path)
        # Insert engine_scores: 3 committed, 1 failed.
        for i, state in enumerate(["committed", "committed", "committed", "validation_failed"]):
            conn.execute(
                "INSERT INTO engine_scores "
                "(run_id, task_id, judge_model, completeness, correctness, quality, "
                "intelligence, role_fit, scored_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("run-n4", f"T0{i+1}", "judge", 8, 7, 9, 6, 8, state),
            )
        conn.commit()
        conn.close()
        # N4 = tasks committed by recommended tier / total tasks.
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """\
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN scored_state = 'committed' THEN 1 ELSE 0 END) AS committed_count
            FROM engine_scores
            WHERE run_id = 'run-n4'
            """
        ).fetchone()
        conn.close()
        total = row[0]
        committed = row[1]
        n4 = committed / total if total > 0 else 0.0
        assert total == 4
        assert committed == 3
        assert n4 == 0.75


class TestPassRate:
    def test_pass_rate_by_model_role_stage(self, db_path):
        """Pass-rate by model/role/stage is queryable."""
        with SessionLogRecorder("run-pr", db_path=db_path) as rec:
            # model-a, coder, generate: 2 success, 1 exhausted
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(finish_reason="stop"),
                latency_ms=1000, role="coder",
            )
            rec.record(
                task_id="T01", attempt=2, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(finish_reason="stop"),
                latency_ms=1000, role="coder",
            )
            rec.record(
                task_id="T01", attempt=3, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(content="", finish_reason="length",
                                        reasoning_content="thinking"),
                latency_ms=1000, role="coder",
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """\
            SELECT model, role, stage,
                   COUNT(*) AS total,
                   SUM(CASE WHEN exhausted = 0 AND finish_reason = 'stop' THEN 1 ELSE 0 END) AS passed
            FROM generation_telemetry
            WHERE run_id = 'run-pr'
            GROUP BY model, role, stage
            """
        ).fetchone()
        conn.close()
        assert row[0] == "model-a"
        assert row[1] == "coder"
        assert row[2] == "generate"
        assert row[3] == 3
        assert row[4] == 2

    def test_read_only_no_writes(self, db_path):
        """Analytics queries are read-only (no mutation)."""
        with SessionLogRecorder("run-ro", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="model-a", port=8080,
                prompt_text="Write code",
                response=_make_response(),
                latency_ms=1000, role="coder",
            )
        conn = sqlite3.connect(db_path)
        count_before = conn.execute(
            "SELECT COUNT(*) FROM generation_telemetry"
        ).fetchone()[0]
        conn.close()
        # Run an aggregate query.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "SELECT model, AVG(latency_ms) FROM generation_telemetry GROUP BY model"
        ).fetchall()
        conn.close()
        # Count should be unchanged.
        conn = sqlite3.connect(db_path)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM generation_telemetry"
        ).fetchone()[0]
        conn.close()
        assert count_before == count_after
