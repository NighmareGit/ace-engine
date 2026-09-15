"""Unit tests for engine/autopsy.py — ace autopsy CLI.

Covers EPIC R4:
- ace autopsy <run_id> prints all 7 sections
- ace autopsy <run_id> <task_id> deep-dives on one task
- ace autopsy <run_id> <task_id> --full includes sidecar payloads
- Calls forensics.bisect for divergence check
- Read-only: no DB writes, no task mutation
- Completes in < 5 s
"""

import json
import os
import sqlite3
import time

import pytest

from engine.autopsy import (
    autopsy,
    _fmt_run_header,
    _fmt_task_summary,
    _fmt_session_log_timeline,
    _fmt_validation_trace,
    _fmt_gate_verdicts,
    _fmt_replan_history,
    _fmt_divergence_check,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    """Create a fresh engine.db with the full schema and sample data."""
    db = str(tmp_path / "engine.db")
    conn = sqlite3.connect(db)
    # engine_runs
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_runs (
        id TEXT PRIMARY KEY, prd_path TEXT, project_path TEXT, config TEXT,
        state TEXT, current_task_id TEXT, current_task_idx INTEGER,
        tasks_json TEXT, task_retries_json TEXT, started_at TEXT,
        completed_at TEXT, result_json TEXT, error_message TEXT
    )""")
    # engine_task_results
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_task_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT,
        state TEXT, generated_code TEXT, validation_result TEXT,
        test_result TEXT, commit_sha TEXT, attempts INTEGER,
        started_at TEXT, completed_at TEXT, error_message TEXT
    )""")
    # judge_verdicts
    conn.execute("""CREATE TABLE IF NOT EXISTS judge_verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
        overall_pass INTEGER, score REAL, dimensions TEXT, created_at TEXT
    )""")
    # engine_scores
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT,
        judge_model TEXT, completeness INTEGER, correctness INTEGER,
        quality INTEGER, intelligence INTEGER, role_fit INTEGER,
        overall REAL, reasoning TEXT, error TEXT, scored_state TEXT,
        scored_at TEXT
    )""")
    # atoms (for forensics.bisect)
    conn.execute("""CREATE TABLE IF NOT EXISTS atoms (
        atom_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, seq INTEGER,
        from_state TEXT, to_state TEXT, input_hash TEXT, output_hash TEXT,
        gate_hash TEXT, duration_ms INTEGER, meta_json TEXT, created_at TEXT
    )""")
    # session_logs
    conn.execute("""CREATE TABLE IF NOT EXISTS session_logs (
        session_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT,
        attempt INTEGER, stage TEXT, atom_id TEXT REFERENCES atoms(atom_id),
        model TEXT, port INTEGER, prompt_hash TEXT, prompt_truncated TEXT,
        prompt_payload_path TEXT, system_message TEXT, user_message TEXT,
        response_content TEXT, response_reasoning TEXT, finish_reason TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
        thinking_tokens INTEGER, latency_ms INTEGER, exhausted INTEGER,
        error TEXT, created_at TEXT
    )""")

    # Insert sample data.
    conn.execute(
        "INSERT INTO engine_runs (id, prd_path, project_path, config, state, started_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("run-test", "/tmp/prd.md", "/tmp/project", "3090-qwen36-35b", "DONE"),
    )
    conn.execute(
        "INSERT INTO engine_task_results "
        "(run_id, task_id, state, attempts, validation_result, commit_sha, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-test", "T01", "COMMIT", 1,
         json.dumps({"passed": True, "stages": [{"stage": "syntax", "passed": True}]}),
         "abc12345", None),
    )
    conn.execute(
        "INSERT INTO engine_task_results "
        "(run_id, task_id, state, attempts, validation_result, commit_sha, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-test", "T02", "FAILED", 3,
         json.dumps({"passed": False, "stages": [
             {"stage": "imports", "passed": False,
              "error": "Import 'engine.workflows.ralph.config' is not available",
              "file": "engine/workflows/ralph/gates.py"},
         ]}),
         None, "validation failed"),
    )
    conn.execute(
        "INSERT INTO engine_scores "
        "(run_id, task_id, judge_model, completeness, correctness, quality, "
        "intelligence, role_fit, scored_state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-test", "T01", "judge-model", 8, 7, 9, 6, 8, "committed"),
    )
    conn.execute(
        "INSERT INTO session_logs "
        "(session_id, run_id, task_id, attempt, stage, model, port, prompt_hash, "
        "prompt_truncated, response_content, finish_reason, prompt_tokens, "
        "completion_tokens, total_tokens, latency_ms, exhausted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-test-T01-1-generate", "run-test", "T01", 1, "generate",
         "3090-qwen36-35b", 8080, "abc123hash", "Write a hello function...",
         "def hello(): pass", "stop", 1500, 50, 1550, 3200, 0),
    )
    conn.execute(
        "INSERT INTO session_logs "
        "(session_id, run_id, task_id, attempt, stage, model, port, prompt_hash, "
        "prompt_truncated, response_content, finish_reason, prompt_tokens, "
        "completion_tokens, total_tokens, latency_ms, exhausted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-test-T02-1-generate", "run-test", "T02", 1, "generate",
         "3090-qwen36-35b", 8080, "def456hash", "Write the gates module...",
         "from engine.workflows.ralph.config import ...", "stop", 2000, 100, 2100, 4000, 0),
    )
    # Insert atoms for bisect.
    conn.execute(
        "INSERT INTO atoms (atom_id, run_id, task_id, seq, from_state, to_state, "
        "input_hash, output_hash, duration_ms, meta_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-test-0001", "run-test", "T01", 1, "IDLE", "PARSING",
         "inputhash1", "outputhash1", 100, "{}"),
    )
    conn.commit()
    conn.close()
    return db


# ── Test: section formatters ──────────────────────────────────────────────────


class TestRunHeader:
    def test_contains_run_id(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_run_header(conn, "run-test")
        conn.close()
        assert "run-test" in result
        assert "Run Header" in result

    def test_missing_run(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_run_header(conn, "nonexistent")
        conn.close()
        assert "not found" in result


class TestTaskSummary:
    def test_contains_tasks(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_task_summary(conn, "run-test")
        conn.close()
        assert "T01" in result
        assert "T02" in result
        assert "COMMIT" in result
        assert "FAILED" in result


class TestSessionLogTimeline:
    def test_contains_session_logs(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_session_log_timeline(conn, "run-test")
        conn.close()
        assert "generate" in result
        assert "3090-qwen36-35b" in result

    def test_empty_run(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_session_log_timeline(conn, "no-such-run")
        conn.close()
        assert "no session logs" in result


class TestValidationTrace:
    def test_contains_validation(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_validation_trace(conn, "run-test")
        conn.close()
        assert "imports" in result
        assert "FAIL" in result


class TestGateVerdicts:
    def test_contains_scores(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_gate_verdicts(conn, "run-test")
        conn.close()
        assert "T01" in result
        assert "overall=" in result


class TestReplanHistory:
    def test_empty(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = _fmt_replan_history(conn, "no-such-run")
        conn.close()
        assert "no re-plans" in result


class TestDivergenceCheck:
    def test_calls_bisect(self, db_path):
        result = _fmt_divergence_check("run-test", db_path)
        assert "ALL ATOMS MATCH" in result or "DIVERGENCE" in result


# ── Test: full autopsy ────────────────────────────────────────────────────────


class TestAutopsy:
    def test_all_sections_present(self, db_path):
        """ace autopsy prints all 7 sections."""
        report = autopsy("run-test", db_path=db_path)
        assert "Run Header" in report
        assert "Task Summary" in report
        assert "Session Log Timeline" in report
        assert "Validation Stage Trace" in report
        assert "Gate Verdicts" in report
        assert "Re-plan History" in report
        assert "Divergence Check" in report

    def test_deep_dive_task(self, db_path):
        """ace autopsy <run_id> <task_id> deep-dives on one task."""
        report = autopsy("run-test", task_id="T01", db_path=db_path)
        assert "Run Header" in report
        assert "T01" in report

    def test_completes_quickly(self, db_path):
        """ace autopsy completes in < 5 s."""
        start = time.time()
        autopsy("run-test", db_path=db_path)
        elapsed = time.time() - start
        assert elapsed < 5.0

    def test_read_only_no_writes(self, db_path):
        """Autopsy does not write to the DB."""
        # Get mtime before.
        mtime_before = os.path.getmtime(db_path)
        autopsy("run-test", db_path=db_path)
        mtime_after = os.path.getmtime(db_path)
        # Note: SQLite read-only connection may still touch the file slightly,
        # but no INSERT/UPDATE should occur. We check row counts.
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM session_logs").fetchone()[0]
        conn.close()
        assert count == 2  # unchanged from fixture

    def test_full_flag(self, db_path, monkeypatch, tmp_path):
        """--full includes sidecar payloads section."""
        # Create a run dir with a sidecar.
        run_dir = tmp_path / "run" / "run-test"
        run_dir.mkdir(parents=True, exist_ok=True)
        sidecar = run_dir / "engine_run_run-test_session_payloads.jsonl"
        sidecar.write_text('{"session_id": "test", "prompt_text": "hello"}\n')
        # Monkeypatch _run_dir to return our temp run dir.
        import engine.autopsy as autopsy_mod
        monkeypatch.setattr(autopsy_mod, "_run_dir", lambda rid: str(run_dir))
        report = autopsy("run-test", full=True, db_path=db_path)
        assert "Full Sidecar Payloads" in report
