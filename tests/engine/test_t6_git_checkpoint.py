"""T6 tests: git checkpoint — last-pushed SHA + resume reconciliation.

Session table records last-pushed SHA after each push; resume reconciles:
  - if a local commit exists but was never pushed -> push it (orphan push)
  - if a task's file-content hash is already committed+pushed -> skip re-commit
    (content-hash dedup)

Fixture: kill between commit and push (mockable transport that records
commit SHAs but lets us simulate a push failure / crash).
"""

import os
import sys
import json
import tempfile

import pytest

from engine.orchestrator.checkpoint import _parse_ls_remote_line

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import checkpoint as ckpt
from engine.orchestrator import session as sess


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# Recording pushes + last-pushed SHA
# ---------------------------------------------------------------------------

def test_record_push_updates_session(tmp_db):
    sess.create_session("run-1", ["T01"])
    ckpt.record_push("run-1", "sha-abc")
    assert sess.get_last_pushed_sha("run-1") == "sha-abc"


def test_record_commit_pending_then_pushed(tmp_db):
    sess.create_session("run-1", ["T01"])
    h = sess.content_hash("x = 1\n")
    ckpt.record_commit_pending("run-1", "T01", h, commit_sha="sha-1")
    # Not yet pushed.
    assert sess.is_task_committed("run-1", "T01", h) is False
    ckpt.mark_pushed("run-1", "T01", h, push_sha="sha-1")
    assert sess.is_task_committed("run-1", "T01", h) is True


# ---------------------------------------------------------------------------
# Reconciliation: orphan push (commit exists, never pushed)
# ---------------------------------------------------------------------------

class _MockGitTransport:
    """Records git operations so tests can inspect what would be pushed."""

    def __init__(self):
        self.ops = []          # list of (action, *args)
        self.committed = {}    # branch -> sha (simulates local commits)
        self.pushed = {}       # branch -> sha (simulates remote state)
        self._branches = {}

    def run_git(self, project_path, *args):
        self.ops.append(args)
        action = args[0]
        if action == "commit":
            branch = self._current_branch(project_path)
            sha = f"sha-{len(self.committed)}"
            self.committed[branch] = sha
            return ("", "", 0)
        if action == "push":
            branch = args[2] if len(args) > 2 else "main"
            sha = self.committed.get(branch)
            if sha:
                self.pushed[branch] = sha
            return ("", "", 0)
        if action == "rev-parse":
            # HEAD returns the local commit; a remote-tracking ref returns
            # the pushed sha (or empty if never pushed).
            if any("remotes/origin" in a for a in args):
                # refs/remotes/origin/<branch> — branch may itself contain '/'.
                ref = args[-1]
                prefix = "refs/remotes/origin/"
                branch = ref[len(prefix):] if ref.startswith(prefix) else ref.split("/")[-1]
                return (self.pushed.get(branch, ""), "", 0)
            sha = next(reversed(list(self.committed.values())), "")
            return (sha, "", 0)
        if action == "log":
            # Return commits not on remote (for orphan detection).
            branch = args[-1] if args else "main"
            sha = self.committed.get(branch)
            remote_sha = self.pushed.get(branch)
            if sha and sha != remote_sha:
                return (sha, "", 0)
            return ("", "", 0)
        if action == "checkout":
            self._set_branch(project_path, args[-1] if len(args) > 1 else "main")
            return ("", "", 0)
        if action == "ls-remote":
            # Simulate: branch not on remote yet -> empty.
            return ("", "", 0)
        return ("", "", 0)

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def _current_branch(self, project):
        return self._branches.get(project, "main")

    def _set_branch(self, project, branch):
        self._branches[project] = branch


def test_reconcile_pushes_orphan_commit(tmp_db):
    """A commit that was never pushed (kill between commit and push) gets
    pushed on resume."""
    sess.create_session("run-1", ["T01"])
    h = sess.content_hash("x = 1\n")
    # Simulate: commit happened (sha recorded) but push never did.
    ckpt.record_commit_pending("run-1", "T01", h, commit_sha="sha-orphan")
    # last_pushed_sha is None -> orphan exists.
    assert sess.get_last_pushed_sha("run-1") is None

    transport = _MockGitTransport()
    # Pre-seed the transport's committed state to simulate the local commit
    # that survived the crash.
    transport.committed["run/1"] = "sha-orphan"
    pushed = ckpt.reconcile_git_state("run-1", "/proj", transport)
    # The orphan should now be pushed.
    assert "sha-orphan" in transport.pushed.values()
    assert pushed is True


def test_reconcile_skip_already_pushed(tmp_db):
    """If the last-pushed SHA matches the remote, no push is needed."""
    sess.create_session("run-1", ["T01"])
    ckpt.record_push("run-1", "sha-done")
    h = sess.content_hash("x = 1\n")
    ckpt.record_commit_pending("run-1", "T01", h, commit_sha="sha-done")
    ckpt.mark_pushed("run-1", "T01", h, push_sha="sha-done")

    transport = _MockGitTransport()
    transport.committed["run/1"] = "sha-done"
    transport.pushed["run/1"] = "sha-done"
    pushed = ckpt.reconcile_git_state("run-1", "/proj", transport)
    assert pushed is False  # nothing to push


# ---------------------------------------------------------------------------
# Content-hash dedup: skip re-commit of already-pushed content
# ---------------------------------------------------------------------------

def test_skip_recommit_if_content_pushed(tmp_db):
    """If a task's file-content hash is already committed+pushed, the
    orchestrator must skip re-committing it on resume."""
    sess.create_session("run-1", ["T01", "T02"])
    code = "def foo(): return 1\n"
    h = sess.content_hash(code)
    # T01 was committed and pushed before the crash.
    ckpt.record_commit_pending("run-1", "T01", h, commit_sha="sha-t01")
    ckpt.mark_pushed("run-1", "T01", h, push_sha="sha-t01")
    ckpt.record_push("run-1", "sha-t01")

    assert ckpt.should_skip_task("run-1", "T01", h) is True
    # T02 (different content) must NOT be skipped.
    assert ckpt.should_skip_task("run-1", "T02", sess.content_hash("y")) is False


# ---------------------------------------------------------------------------
# Crash-sim fixture: kill between commit and push
# ---------------------------------------------------------------------------

def test_crash_between_commit_and_push(tmp_db):
    """Simulate kill -9 between commit and push: on resume, the orphan commit
    is pushed and the task is not re-committed (exactly one commit per task
    by content)."""
    sess.create_session("run-crash", ["T01"])
    code = "def bar(): return 2\n"
    h = sess.content_hash(code)

    # --- commit phase: local commit succeeds, sha recorded ---
    ckpt.record_commit_pending("run-crash", "T01", h, commit_sha="sha-local")
    # --- crash: push never happens, last_pushed_sha stays None ---
    assert sess.get_last_pushed_sha("run-crash") is None

    # --- resume: reconcile ---
    transport = _MockGitTransport()
    transport.committed["run/crash"] = "sha-local"
    pushed_ops_before = len(transport.ops)
    ckpt.reconcile_git_state("run-crash", "/proj", transport)
    # A push op must have been issued for the orphan.
    push_ops = [op for op in transport.ops[pushed_ops_before:] if op[0] == "push"]
    assert len(push_ops) >= 1, "orphan commit must be pushed on resume"
    # After reconcile, the task is marked pushed.
    ckpt.mark_pushed("run-crash", "T01", h, push_sha="sha-local")
    ckpt.record_push("run-crash", "sha-local")
    # Now should_skip_task is True (no re-commit).
    assert ckpt.should_skip_task("run-crash", "T01", h) is True


# ---------------------------------------------------------------------------
# _parse_ls_remote_line: defensive whitespace-variant parse
# ---------------------------------------------------------------------------

def test_parse_ls_remote_tab_separated():
    assert _parse_ls_remote_line("abc1234\trefs/heads/main") == "abc1234"


def test_parse_ls_remote_space_separated():
    assert _parse_ls_remote_line("abc1234 refs/heads/main") == "abc1234"


def test_parse_ls_remote_multiple_spaces():
    assert _parse_ls_remote_line("abc1234   refs/heads/run/1") == "abc1234"


def test_parse_ls_remote_full_sha():
    sha = "a" * 40
    assert _parse_ls_remote_line(f"{sha}\trefs/heads/main") == sha


def test_parse_ls_remote_empty():
    assert _parse_ls_remote_line("") is None


def test_parse_ls_remote_garbage():
    assert _parse_ls_remote_line("not-a-sha refs/heads/main") is None
