"""Unit tests for TGD G4 signal upgrade (OBS-05).

Covers EPIC R7:
- _extract_g4 reads prompt_tokens from session_logs
- A run with prompt_tokens >= 0.9 * model_cap triggers G4 with real data
- No false positives from large generated code blobs
- Graceful degradation: if session_logs has no rows, G4 returns []
"""

import sqlite3

import pytest

from engine.orchestrator.toolgap_detector import _extract_g4
from engine.orchestrator.toolgap_types import SignalId


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def conn(tmp_path):
    """Create a fresh engine.db with session_logs + engine_task_results."""
    db = str(tmp_path / "engine.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # session_logs
    conn.execute("""CREATE TABLE IF NOT EXISTS session_logs (
        session_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT,
        attempt INTEGER, stage TEXT, atom_id TEXT,
        model TEXT, port INTEGER, prompt_hash TEXT, prompt_truncated TEXT,
        prompt_payload_path TEXT, system_message TEXT, user_message TEXT,
        response_content TEXT, response_reasoning TEXT, finish_reason TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
        thinking_tokens INTEGER, latency_ms INTEGER, exhausted INTEGER,
        error TEXT, created_at TEXT
    )""")
    # engine_task_results (for the old code-blob proxy — should NOT trigger G4 now)
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_task_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT,
        state TEXT, generated_code TEXT, validation_result TEXT,
        test_result TEXT, commit_sha TEXT, attempts INTEGER,
        started_at TEXT, completed_at TEXT, error_message TEXT
    )""")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestG4Upgrade:
    def test_high_prompt_tokens_triggers_g4(self, conn):
        """A run with prompt_tokens >= 14400 triggers G4 with real data."""
        conn.execute(
            "INSERT INTO session_logs "
            "(session_id, run_id, task_id, attempt, stage, model, port, "
            "prompt_hash, prompt_truncated, response_content, finish_reason, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "run-1", "T01", 1, "generate", "3090-qwen36-35b", 8080,
             "hash1", "prompt...", "code...", "stop", 15000, 100, 15100, 3000),
        )
        conn.commit()
        findings = _extract_g4("run-1", conn)
        assert len(findings) == 1
        assert findings[0].signal == SignalId.G4_CONTEXT_BLOAT
        assert "prompt_tokens" in findings[0].message
        assert "15000" in str(findings[0].evidence[0].count)

    def test_low_prompt_tokens_no_g4(self, conn):
        """A run with prompt_tokens < 14400 does NOT trigger G4."""
        conn.execute(
            "INSERT INTO session_logs "
            "(session_id, run_id, task_id, attempt, stage, model, port, "
            "prompt_hash, prompt_truncated, response_content, finish_reason, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "run-1", "T01", 1, "generate", "3090-qwen36-35b", 8080,
             "hash1", "prompt...", "code...", "stop", 5000, 100, 5100, 3000),
        )
        conn.commit()
        findings = _extract_g4("run-1", conn)
        assert len(findings) == 0

    def test_no_false_positive_from_large_code_blob(self, conn):
        """Large generated_code in engine_task_results does NOT trigger G4."""
        # Insert a task with huge generated_code but low prompt_tokens.
        conn.execute(
            "INSERT INTO engine_task_results "
            "(run_id, task_id, state, generated_code) "
            "VALUES (?, ?, ?, ?)",
            ("run-1", "T01", "COMMIT", "x" * 60_000),  # 60K chars
        )
        # No session_logs rows → G4 should return [].
        conn.commit()
        findings = _extract_g4("run-1", conn)
        assert len(findings) == 0

    def test_graceful_degradation_no_session_logs(self, conn):
        """If session_logs has no rows, G4 returns []."""
        findings = _extract_g4("run-empty", conn)
        assert len(findings) == 0

    def test_threshold_boundary(self, conn):
        """prompt_tokens exactly at threshold triggers G4."""
        conn.execute(
            "INSERT INTO session_logs "
            "(session_id, run_id, task_id, attempt, stage, model, port, "
            "prompt_hash, prompt_truncated, response_content, finish_reason, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "run-1", "T01", 1, "generate", "3090-qwen36-35b", 8080,
             "hash1", "prompt...", "code...", "stop", 14400, 100, 14500, 3000),
        )
        conn.commit()
        findings = _extract_g4("run-1", conn)
        assert len(findings) == 1

    def test_just_below_threshold(self, conn):
        """prompt_tokens just below threshold does NOT trigger G4."""
        conn.execute(
            "INSERT INTO session_logs "
            "(session_id, run_id, task_id, attempt, stage, model, port, "
            "prompt_hash, prompt_truncated, response_content, finish_reason, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "run-1", "T01", 1, "generate", "3090-qwen36-35b", 8080,
             "hash1", "prompt...", "code...", "stop", 14399, 100, 14499, 3000),
        )
        conn.commit()
        findings = _extract_g4("run-1", conn)
        assert len(findings) == 0

    def test_scan_all_runs(self, conn):
        """_extract_g4 with run_id=None scans all runs."""
        conn.execute(
            "INSERT INTO session_logs "
            "(session_id, run_id, task_id, attempt, stage, model, port, "
            "prompt_hash, prompt_truncated, response_content, finish_reason, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "run-1", "T01", 1, "generate", "3090-qwen36-35b", 8080,
             "hash1", "prompt...", "code...", "stop", 15000, 100, 15100, 3000),
        )
        conn.execute(
            "INSERT INTO session_logs "
            "(session_id, run_id, task_id, attempt, stage, model, port, "
            "prompt_hash, prompt_truncated, response_content, finish_reason, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s2", "run-2", "T01", 1, "generate", "3090-qwen36-35b", 8080,
             "hash2", "prompt...", "code...", "stop", 16000, 100, 16100, 3000),
        )
        conn.commit()
        findings = _extract_g4(None, conn)
        assert len(findings) == 1
        # Both runs should be in evidence.
        evidence_tasks = [e.task_id for e in findings[0].evidence]
        assert "T01" in evidence_tasks
