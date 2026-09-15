"""P4 tests: results archive (design T1.2-T1.6).

Offline — git commit/push are mocked so no network/Gitea is touched. Covers
archive_run writing report.json + engine.db, idempotency, automatic archival at
end of run(), prune_archives retention, fetch_run local read + Gitea fallback,
and the Rule-6 tripwire (no write to benchmark-results.db).
"""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.engine import Engine, RunResult, TaskResult, build_run_report
from engine import archive as archive_mod
from engine import state as engine_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "engine.db")
    monkeypatch.setattr("engine.state.DB_PATH", test_db)
    engine_state.init_db()
    return test_db


def _make_report(run_id="run-123", project="/tmp/project"):
    result = RunResult(
        run_id=run_id, success=True, prd_path="/tmp/prd.md",
        project_path=project, total_time_s=1.0, total_tokens=100,
        tasks=[TaskResult(task_id="T01", title="t", state="COMMIT", attempts=1,
                           commit_sha="abc", time_s=1.0, tokens=100,
                           error_message=None)],
        states_visited=[], error_message=None,
    )
    return build_run_report(result, EngineConfig()).to_dict()


# ---------------------------------------------------------------------------
# T1.2 — archive_run writes archive/<run_id>/{engine.db,report.json}; idempotent
# ---------------------------------------------------------------------------

def test_archive_run_writes_report_and_db(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_mod, "_commit_and_push", lambda *a: None)
    db_path = str(tmp_path / "engine.db")
    # Create a minimal db file to snapshot
    open(db_path, "w").close()
    report = _make_report()
    out = archive_mod.archive_run("run-123", "/tmp/project",
                                   results_repo=str(tmp_path),
                                   db_path=db_path, report=report)
    assert out == str(tmp_path / "archive" / "run-123")
    assert os.path.exists(os.path.join(out, "report.json"))
    assert os.path.exists(os.path.join(out, "engine.db"))
    loaded = json.load(open(os.path.join(out, "report.json")))
    assert loaded["run_id"] == "run-123"


def test_archive_run_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_mod, "_commit_and_push", lambda *a: None)
    db_path = str(tmp_path / "engine.db")
    open(db_path, "w").close()
    report = _make_report()
    d1 = archive_mod.archive_run("run-123", "/tmp/project",
                                  results_repo=str(tmp_path),
                                  db_path=db_path, report=report)
    # second call with updated report
    report["success"] = False
    d2 = archive_mod.archive_run("run-123", "/tmp/project",
                                  results_repo=str(tmp_path),
                                  db_path=db_path, report=report)
    assert d1 == d2
    loaded = json.load(open(os.path.join(d2, "report.json")))
    assert loaded["success"] is False  # overwritten


# ---------------------------------------------------------------------------
# T1.3 — archive_run called automatically at end of run() (spy)
# ---------------------------------------------------------------------------

class _PassingTransport:
    def __init__(self):
        self.calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        return {"content": "```python\nx = 1\n```", "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        return ("", "", 0)


def test_auto_archive_at_end_of_run(tmp_path, monkeypatch):
    """Engine.run() auto-archives when archive_results=True (default)."""
    db_path = _tmp_db(tmp_path, monkeypatch)
    archive_root = tmp_path / "results"
    archive_root.mkdir()
    monkeypatch.setenv("ACE_ARCHIVE_DIR", str(archive_root))
    monkeypatch.setattr(archive_mod, "_commit_and_push", lambda *a: None)

    import engine.engine as eng_mod
    import engine.committer as cm
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py")]
    cm._ensure_repo_exists = lambda r, a: None
    try:
        eng = Engine(transport=_PassingTransport(),
                     config=EngineConfig(archive_keep=30))
        result = eng.run("/tmp/prd.md", "/tmp/project")
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure

    archive_dir = archive_root / "archive" / result.run_id
    assert archive_dir.is_dir(), "expected auto-archive dir to be created"
    assert (archive_dir / "report.json").exists()
    assert (archive_dir / "engine.db").exists()


# ---------------------------------------------------------------------------
# T1.4 — prune_archives retains exactly keep newest .db per project, removes older
# ---------------------------------------------------------------------------

def _seed_archive(results_repo, run_id, project, report, db_bytes=b"db"):
    a_dir = os.path.join(results_repo, "archive", run_id)
    os.makedirs(a_dir, exist_ok=True)
    with open(os.path.join(a_dir, "report.json"), "w") as f:
        json.dump({**report, "project_path": project}, f)
    with open(os.path.join(a_dir, "engine.db"), "wb") as f:
        f.write(db_bytes)
    return a_dir


def test_prune_archives_retains_keep_newest(tmp_path):
    results_repo = str(tmp_path)
    report = _make_report()
    # 5 snapshots for the same project
    for i in range(5):
        _seed_archive(results_repo, f"run-{i}", "/tmp/project", report)
        # ensure distinct mtimes
        import time
        time.sleep(0.01)
    removed = archive_mod.prune_archives("/tmp/project", keep=3,
                                          results_repo=results_repo)
    assert removed == 2
    remaining = [d for d in os.listdir(os.path.join(results_repo, "archive"))
                 if os.path.exists(os.path.join(results_repo, "archive", d, "engine.db"))]
    assert len(remaining) == 3
    # JSON files are never pruned
    json_files = [d for d in os.listdir(os.path.join(results_repo, "archive"))
                  if os.path.exists(os.path.join(results_repo, "archive", d, "report.json"))]
    assert len(json_files) == 5


def test_prune_archives_other_project_unaffected(tmp_path):
    results_repo = str(tmp_path)
    report = _make_report()
    for i in range(5):
        _seed_archive(results_repo, f"run-a-{i}", "/tmp/project", report)
    for i in range(2):
        _seed_archive(results_repo, f"run-b-{i}", "/tmp/OTHER", report)
    removed = archive_mod.prune_archives("/tmp/project", keep=2,
                                          results_repo=results_repo)
    assert removed == 3  # 5 - 2 for /tmp/project
    other = [d for d in os.listdir(os.path.join(results_repo, "archive"))
             if d.startswith("run-b-") and os.path.exists(
                 os.path.join(results_repo, "archive", d, "engine.db"))]
    assert len(other) == 2  # other project untouched


# ---------------------------------------------------------------------------
# T1.5 — fetch_run reads local JSON; falls back to Gitea API mock when absent
# ---------------------------------------------------------------------------

def test_fetch_run_reads_local(tmp_path, monkeypatch):
    monkeypatch.setenv("ACE_ARCHIVE_DIR", str(tmp_path))
    a_dir = os.path.join(tmp_path, "archive", "run-local")
    os.makedirs(a_dir)
    payload = {"run_id": "run-local", "success": True}
    with open(os.path.join(a_dir, "report.json"), "w") as f:
        json.dump(payload, f)
    assert archive_mod.fetch_run("run-local") == payload


def test_fetch_run_falls_back_to_gitea(tmp_path, monkeypatch):
    monkeypatch.setenv("ACE_ARCHIVE_DIR", str(tmp_path))
    fetched = {"run_id": "run-remote", "success": True}
    monkeypatch.setattr(archive_mod, "_fetch_from_gitea", lambda rid: fetched)
    assert archive_mod.fetch_run("run-remote") == fetched


# ---------------------------------------------------------------------------
# T1.6 — Rule-6 tripwire: no write to benchmark-results.db during archive
# ---------------------------------------------------------------------------

def test_archive_never_writes_benchmark_db(tmp_path, monkeypatch):
    """Archiving must never touch benchmark-results.db (Rule 6)."""
    monkeypatch.setattr(archive_mod, "_commit_and_push", lambda *a: None)
    db_path = str(tmp_path / "engine.db")
    open(db_path, "w").close()
    report = _make_report()
    archive_mod.archive_run("run-123", "/tmp/project",
                             results_repo=str(tmp_path),
                             db_path=db_path, report=report)
    bench = os.path.join(PROJECT_ROOT, "benchmark-results.db")
    # benchmark-results.db must not have gained an engine_scores table etc.
    if os.path.exists(bench):
        conn = sqlite3.connect(bench)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "engine_scores" not in tables
