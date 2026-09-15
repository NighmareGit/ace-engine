import unittest
import os
import sys
"""P2 tests: commit hardening (design T4.1-T4.6).

Mock transport only. Covers the URL builder, repo-name derivation, the
GITEA_AUTOCREATE opt-in, fail-fast on missing repo, and the
PERMANENT/retryable classification honored by _run_task.
"""

import os
import sys
import json

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig, Task
from engine.committer import (_build_origin_url, _ensure_repo_exists,
                              CommitResult, commit_code)
from engine.engine import Engine


# ---------------------------------------------------------------------------
# T4.1 — _build_origin_url embeds token iff present; never embeds empty token
# ---------------------------------------------------------------------------

def test_build_url_embeds_token_when_present(monkeypatch):
    monkeypatch.setenv("GITEA_URL", "http://localhost:3000")
    monkeypatch.setenv("GITEA_USER", "<user>")
    monkeypatch.setenv("GITEA_TOKEN", "secrettok")
    url = _build_origin_url("/home/<user>/myproject")
    assert url == "http://<user>:secrettok@localhost:3000/<user>/myproject.git"


def test_build_url_no_token_embeds_nothing(monkeypatch):
    monkeypatch.setenv("GITEA_URL", "http://localhost:3000")
    monkeypatch.setenv("GITEA_USER", "<user>")
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    url = _build_origin_url("/home/<user>/myproject")
    # No token => no credentials embedded at all (not even "user:@host")
    assert url == "http://localhost:3000/<user>/myproject.git"
    assert "secrettok" not in url
    assert "::" not in url  # never double-embedded


def test_build_url_https_host(monkeypatch):
    monkeypatch.setenv("GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("GITEA_USER", "<user>")
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    url = _build_origin_url("/projects/demo")
    assert url == "http://<user>:tok@git.example.com/<user>/demo.git"


# ---------------------------------------------------------------------------
# T4.2 — repo name derived from project basename; GITEA_REPO overrides
# ---------------------------------------------------------------------------

def test_repo_name_defaults_to_basename(monkeypatch):
    monkeypatch.delenv("GITEA_REPO", raising=False)
    monkeypatch.setenv("GITEA_URL", "http://localhost:3000")
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    url = _build_origin_url("/home/<user>/epic5-live-test")
    assert "/<user>/epic5-live-test.git" in url


def test_repo_name_gitea_repo_overrides(monkeypatch):
    monkeypatch.setenv("GITEA_REPO", "custom-repo")
    monkeypatch.setenv("GITEA_URL", "http://localhost:3000")
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    url = _build_origin_url("/home/<user>/epic5-live-test")
    assert "/<user>/custom-repo.git" in url


# ---------------------------------------------------------------------------
# T4.3 — missing repo + autocreate off -> clear ValueError naming the repo
# ---------------------------------------------------------------------------

def test_missing_repo_autocreate_off_raises(monkeypatch):
    monkeypatch.setenv("GITEA_USER", "<user>")
    monkeypatch.delenv("GITEA_AUTOCREATE", raising=False)
    monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: False)
    with pytest.raises(ValueError, match="does not exist"):
        _ensure_repo_exists("missing-repo", autocreate=False)


# ---------------------------------------------------------------------------
# T4.4 — missing repo + autocreate on -> create_repo called
# ---------------------------------------------------------------------------

def test_missing_repo_autocreate_on_creates(monkeypatch):
    monkeypatch.setenv("GITEA_AUTOCREATE", "1")
    monkeypatch.setattr("engine.committer._gitea_repo_exists", lambda u, r: False)
    called = {"count": 0}
    def _fake_create(user, repo):
        called["count"] += 1
    monkeypatch.setattr("engine.committer._gitea_create_repo", _fake_create)
    _ensure_repo_exists("new-repo", autocreate=True)
    assert called["count"] == 1


# ---------------------------------------------------------------------------
# T4.5 / T4.6 — PERMANENT fails task immediately; retryable retries (bounded)
# ---------------------------------------------------------------------------

class _CommitTransport:
    """Mock transport whose git push fails a controllable number of times."""

    def __init__(self, push_fail_times=0, permanent=False):
        self.push_fail_times = push_fail_times
        self.permanent = permanent
        self.push_calls = 0
        self.run_command_returns = ("", "", 0)

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        return {"content": "```python\nx = 1\n```", "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return self.run_command_returns

    def run_git(self, project_path, *args):
        # checkout -b, add, commit, remote add/set-url, push, rev-parse
        if args[0] == "push":
            self.push_calls += 1
            if self.push_calls <= self.push_fail_times:
                stderr = "fatal: 401 Authentication failed" if self.permanent \
                    else "fatal: transient network error"
                return ("", stderr, 1)
            return ("", "", 0)
        if args[0] == "remote":
            return ("", "", 0)
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def _run_with_commit_transport(transport, push_fail_times, permanent=False,
                               max_retries_commit=2, monkeypatch=None):
    import engine.engine as eng_mod
    transport.push_fail_times = push_fail_times
    transport.permanent = permanent
    transport.push_calls = 0
    orig_parse = eng_mod.parse_prd
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py")]
    # Make commit's pre-flight repo check a no-op success
    import engine.committer as cm
    orig_ensure = cm._ensure_repo_exists
    cm._ensure_repo_exists = lambda repo, autocreate: None
    try:
        eng = Engine(transport=transport,
                     config=EngineConfig(max_retries_generate=1,
                                         max_retries_commit=max_retries_commit))
        result = eng.run("/tmp/prd.md", "/tmp/project")
        return result, transport
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


def test_permanent_push_fails_immediately(monkeypatch):
    """PERMANENT failure -> task fails without exhausting commit retries (T4.5)."""
    transport = _CommitTransport(push_fail_times=99, permanent=True)
    result, transport = _run_with_commit_transport(
        transport, push_fail_times=99, permanent=True, monkeypatch=monkeypatch)
    tr = result.tasks[0]
    assert tr.state == "FAILED"
    assert "PERMANENT" in tr.error_message
    # Only one commit attempt for a PERMANENT failure (no retries)
    assert transport.push_calls == 1


def test_retryable_push_retries_then_succeeds(monkeypatch):
    """Retryable failure retries the commit; succeeds on second attempt (T4.5)."""
    transport = _CommitTransport(push_fail_times=1, permanent=False)
    result, transport = _run_with_commit_transport(
        transport, push_fail_times=1, permanent=False, monkeypatch=monkeypatch)
    tr = result.tasks[0]
    assert tr.state == "COMMIT"
    assert transport.push_calls == 2  # failed once, then succeeded


def test_commit_retry_bounded(monkeypatch):
    """Commit retries are bounded by max_retries_commit (T4.6)."""
    transport = _CommitTransport(push_fail_times=99, permanent=False)
    result, transport = _run_with_commit_transport(
        transport, push_fail_times=99, permanent=False,
        max_retries_commit=2, monkeypatch=monkeypatch)
    tr = result.tasks[0]
    assert tr.state == "FAILED"
    # initial attempt + max_retries_commit retries
    assert transport.push_calls == 1 + 2
    assert "retryable" in tr.error_message


# ---------------------------------------------------------------------------
# CommitResult carries permanent flag
# ---------------------------------------------------------------------------

def test_commit_result_permanent_flag():
    r = CommitResult(success=False, sha=None, branch="ace/T01-123",
                     error_message="PERMANENT", permanent=True)
    assert r.permanent is True
    r2 = CommitResult(success=True, sha="abc", branch="ace/T01-123",
                      error_message=None)
    assert r2.permanent is False  # default


class TestRunBranchPolicy(unittest.TestCase):
    """Issue #3 (owner model): all tasks of a run accumulate on ONE
    feature branch run/<run_id>; engine never merges to the default."""

    def _mk(self):
        from unittest.mock import patch
        from engine.committer import commit_code
        from engine import Task
        from engine.generator import GeneratedCode
        calls = []
        class T:
            def run_git(self, *a):
                calls.append(a)
                if a[1] == "rev-parse":
                    return ("abc123", "", 0)
                return ("", "", 0)
            def run_command(self, *a, **k):
                return ("", "", 0)  # repo-exists preflight passes
        t = Task(id="T01", title="t", description="d", module="m.py")
        code = GeneratedCode(files={"m.py": "x = 1\n"}, raw_response="r",
                             tokens=1, thinking_tokens=0, latency_ms=1)
        patcher = patch("engine.committer._gitea_repo_exists", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return commit_code, Task, T, t, code, calls

    def test_run_branch_used_when_run_id_given(self):
        commit_code, Task, T, t, code, calls = self._mk()
        r = commit_code("/proj", t, code, T(), run_id="run-123")
        self.assertEqual(r.branch, "run/123")
        self.assertIn(("checkout", "-B", "run/123"), [c[1:] for c in calls])
        self.assertIn(("push", "origin", "run/123"), [c[1:] for c in calls])

    def test_same_branch_reused_across_tasks(self):
        commit_code, Task, T, t, code, calls = self._mk()
        commit_code("/proj", t, code, T(), run_id="run-9")
        commit_code("/proj", t, code, T(), run_id="run-9")
        checkouts = [c for c in calls if c[1] == "checkout"]
        self.assertTrue(all(a[2] == "-B" for a in checkouts))

    def test_legacy_branch_without_run_id(self):
        commit_code, Task, T, t, code, calls = self._mk()
        r = commit_code("/proj", t, code, T())
        self.assertTrue(r.branch.startswith("ace/T01-"))
