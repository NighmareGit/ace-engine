"""Performance tests for session logs (OBS-06).

Covers EPIC R10:
- Session log write adds < 50 ms per LLM call
- 100 concurrent inserts (distinct run_id) complete with zero SQLITE_BUSY
- WAL mode + busy_timeout=5000 for concurrent safety
"""

import sqlite3
import time
import threading

import pytest

from engine.session_log import SessionLogRecorder, init_session_logs_db


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    db = str(tmp_path / "engine.db")
    init_session_logs_db(db_path=db)
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


class TestWritePerformance:
    def test_write_under_50ms(self, db_path):
        """Session log write adds < 50 ms per LLM call."""
        with SessionLogRecorder("run-perf", db_path=db_path) as rec:
            start = time.time()
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write a hello function",
                response=_make_response(),
                latency_ms=1000,
            )
            elapsed_ms = (time.time() - start) * 1000
        assert elapsed_ms < 50, f"Write took {elapsed_ms:.1f}ms (must be < 50ms)"

    def test_1000_inserts_under_5s(self, db_path):
        """1000 inserts in < 5 s (WAL mode, autocommit)."""
        with SessionLogRecorder("run-1k", db_path=db_path) as rec:
            start = time.time()
            for i in range(1000):
                rec.record(
                    task_id="T01", attempt=1, stage="generate",
                    model="3090-qwen36-35b", port=8080,
                    prompt_text=f"Prompt {i}" * 100,  # ~700 chars each
                    response=_make_response(content=f"Response {i}"),
                    latency_ms=1000,
                )
            elapsed = time.time() - start
        assert elapsed < 5.0, f"1000 inserts took {elapsed:.1f}s (must be < 5s)"


class TestConcurrentSafety:
    def test_100_concurrent_inserts_no_sqlite_busy(self, db_path):
        """100 concurrent inserts (distinct run_id) complete with zero SQLITE_BUSY."""
        errors = []
        barrier = threading.Barrier(100)

        def worker(i):
            try:
                barrier.wait(timeout=10)
                with SessionLogRecorder(f"run-conc-{i}", db_path=db_path) as rec:
                    rec.record(
                        task_id="T01", attempt=1, stage="generate",
                        model="3090-qwen36-35b", port=8080,
                        prompt_text=f"Concurrent prompt {i}",
                        response=_make_response(),
                        latency_ms=1000,
                    )
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Concurrent errors: {errors[:5]}"

    def test_wal_mode_enabled(self, db_path):
        """WAL mode is enabled on the connection."""
        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_busy_timeout_configured(self, db_path):
        """busy_timeout=5000 is configured."""
        conn = sqlite3.connect(db_path)
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.close()
        assert bt == 5000
