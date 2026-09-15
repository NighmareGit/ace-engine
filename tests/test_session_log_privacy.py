"""Privacy tests for session logs (OBS-06).

Covers EPIC R9:
- Prompts > 4000 chars truncated in-row; full prompt in sidecar only
- Responses capped at 16 KB in-row
- No secret patterns (API keys, tokens) appear in engine.db session_logs
"""

import json
import os
import sqlite3

import pytest

from engine.session_log import SessionLogRecorder, init_session_logs_db


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "engine.db")


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / "run-dir"
    d.mkdir()
    return str(d)


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


class TestTruncation:
    def test_prompt_truncated_in_row(self, db_path):
        """Prompts > 4000 chars are truncated in the DB row."""
        long_prompt = "A" * 5000
        with SessionLogRecorder("run-priv", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=long_prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_truncated FROM session_logs").fetchone()
        conn.close()
        assert len(row[0]) < 5000
        assert "truncated" in row[0]

    def test_full_prompt_in_sidecar_only(self, db_path, run_dir):
        """Full prompt is in the sidecar, NOT in the DB row."""
        long_prompt = "B" * 5000
        with SessionLogRecorder("run-side", db_path=db_path, run_dir=run_dir) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=long_prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        # DB row has truncated version.
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_truncated FROM session_logs").fetchone()
        conn.close()
        assert len(row[0]) < 5000
        # Sidecar has full version.
        sidecar = os.path.join(run_dir, "engine_run_run-side_session_payloads.jsonl")
        with open(sidecar) as f:
            entry = json.loads(f.readline())
        assert len(entry["prompt_text"]) == 5000


class TestResponseCapping:
    def test_response_capped_at_16kb(self, db_path):
        """Responses > 16 KB are capped in the DB row."""
        huge_response = "x" * 20_000
        with SessionLogRecorder("run-cap", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(content=huge_response),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT response_content FROM session_logs").fetchone()
        conn.close()
        assert len(row[0]) < 20_000
        assert "capped" in row[0]

    def test_short_response_not_capped(self, db_path):
        """Responses <= 16 KB are stored in full."""
        short_response = "def hello(): pass"
        with SessionLogRecorder("run-short", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text="Write code",
                response=_make_response(content=short_response),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT response_content FROM session_logs").fetchone()
        conn.close()
        assert row[0] == short_response


class TestSecretRedaction:
    def test_no_plaintext_secret_in_db(self, db_path):
        """A prompt containing a fake secret pattern does NOT appear in plaintext
        in the DB row (it's truncated + hashed)."""
        secret = "sk-fakeapikey1234567890abcdef"
        prompt = f"Use this API key: {secret} to authenticate. " + "x" * 5000
        with SessionLogRecorder("run-secret", db_path=db_path) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT prompt_truncated, prompt_hash, user_message FROM session_logs"
        ).fetchall()
        conn.close()
        # The secret should NOT appear in any text column.
        for row in rows:
            for col in row:
                if col and isinstance(col, str):
                    assert secret not in col, f"Secret found in DB column: {col[:100]}"

    def test_secret_in_sidecar_only(self, db_path, run_dir):
        """The full prompt (with secret) is in the sidecar, not the DB."""
        secret = "sk-fakeapikey1234567890abcdef"
        prompt = f"Use this API key: {secret} to authenticate."
        with SessionLogRecorder("run-sec-side", db_path=db_path, run_dir=run_dir) as rec:
            rec.record(
                task_id="T01", attempt=1, stage="generate",
                model="3090-qwen36-35b", port=8080,
                prompt_text=prompt,
                response=_make_response(),
                latency_ms=1000,
            )
        # DB row should not contain the secret.
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT prompt_truncated FROM session_logs").fetchone()
        conn.close()
        assert secret not in row[0]
        # Sidecar contains the full prompt (this is expected — sidecar lives
        # in the run dir, archived with it, not in engine.db).
        sidecar = os.path.join(run_dir, "engine_run_run-sec-side_session_payloads.jsonl")
        with open(sidecar) as f:
            entry = json.loads(f.readline())
        assert secret in entry["prompt_text"]
