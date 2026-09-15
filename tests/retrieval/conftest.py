"""Shared fixtures for retrieval tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def fixture_tree(tmp_path):
    """Create a small C+Python fixture tree in a temp dir."""
    # Python file
    (tmp_path / "math.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "class Multiplier:\n    def mul(self, a, b):\n        return a * b\n\n"
        "def sub(a, b):\n    return a - b\n"
    )
    # C file
    (tmp_path / "core.c").write_text(
        "int foo(int x) { return x + 1; }\n"
        "void bar(void) { return; }\n"
    )
    # C++ file
    (tmp_path / "app.cpp").write_text(
        "class App {\npublic:\n    void run() { }\n};\n"
    )
    # Ignored file
    (tmp_path / "generated.pyc").write_text("bytecode")
    # .aceignore
    (tmp_path / ".aceignore").write_text("*.pyc\n")
    return tmp_path


@pytest.fixture
def fixture_db(tmp_path):
    """Create a minimal engine.db with session_logs for baseline tests."""
    db_path = str(tmp_path / "engine.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE session_logs (
        session_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        stage TEXT NOT NULL,
        atom_id TEXT,
        model TEXT NOT NULL,
        port INTEGER NOT NULL,
        prompt_hash TEXT NOT NULL,
        prompt_truncated TEXT NOT NULL,
        prompt_payload_path TEXT,
        system_message TEXT,
        user_message TEXT NOT NULL,
        response_content TEXT NOT NULL,
        response_reasoning TEXT,
        finish_reason TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        total_tokens INTEGER NOT NULL,
        thinking_tokens INTEGER DEFAULT 0,
        latency_ms INTEGER NOT NULL,
        exhausted INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def fixture_sidecar(tmp_path):
    """Create a session payloads sidecar with known file paths."""
    sidecar_path = str(tmp_path / "engine_run_test_session_payloads.jsonl")
    import json
    entry = {
        "session_id": "test-T01-1-generate",
        "run_id": "test",
        "system_message": None,
        "user_message": "Build math.py",
        "prompt_text": (
            "You are a developer.\n\n"
            "## Task\n\n"
            "**Files to create:**\n"
            "  - `@math.py`\n"
            "  - `@core.c`\n"
            "  - `@app.cpp`\n"
            "\n## Context Files\n\n"
            "  - `helper.py`\n"
            "  - `utils.py`\n"
        ),
        "response_content": "def add(a, b): pass",
        "response_reasoning": "",
        "meta": {},
        "written_at": 1700000000.0,
    }
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return sidecar_path
