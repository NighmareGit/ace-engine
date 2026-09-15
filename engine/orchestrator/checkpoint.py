"""Git checkpoint — last-pushed SHA recording + resume reconciliation (T6).

On each successful push the orchestrator records the SHA in the session
table (via session.record_push). On resume it reconciles git state:

  * If a local commit exists that was never pushed (orphan — e.g. the process
    was killed between commit and push), it pushes the orphan.
  * If a task's file-content hash is already committed AND pushed, the task
    is skipped (content-hash dedup) — exactly one commit per task by content.

The reconciliation uses a transport's run_git() to inspect local vs. remote
state and to push orphans. All state is persisted in engine.db so a crash
between commit and push is recoverable.
"""

from engine.orchestrator import session as sess


def record_push(run_id: str, sha: str) -> None:
    """Record the last pushed SHA for the run (delegates to session)."""
    sess.record_push(run_id, sha)


def record_commit_pending(run_id: str, task_id: str, content_hash: str,
                          commit_sha: str) -> None:
    """Record a local commit that has not yet been pushed."""
    sess.record_commit(run_id, task_id, content_hash, commit_sha=commit_sha,
                       pushed=False)


def mark_pushed(run_id: str, task_id: str, content_hash: str,
                push_sha: str) -> None:
    """Mark a previously-pending commit as pushed."""
    sess.record_commit(run_id, task_id, content_hash, commit_sha=push_sha,
                       pushed=True)


def should_skip_task(run_id: str, task_id: str, content_hash: str) -> bool:
    """True if this exact content was already committed AND pushed — skip
    re-committing on resume (content-hash dedup)."""
    return sess.is_task_committed(run_id, task_id, content_hash)


def _branch_for_run(run_id: str) -> str:
    """Derive the run feature branch name from a run id (matches committer)."""
    # run/<run_id> with the "run-" prefix stripped, matching committer.py.
    return f"run/{str(run_id).removeprefix('run-')}"


def reconcile_git_state(run_id: str, project_path: str, transport) -> bool:
    """Reconcile git state on resume. Returns True if a push was issued.

    Strategy:
      1. Determine the run branch.
      2. Ask the transport for the local HEAD sha and the remote tracking sha.
      3. If local has commits not on remote (orphan), push them.
      4. Record the newly pushed SHA in the session.
    """
    branch = _branch_for_run(run_id)

    # Local HEAD.
    stdout, _, rc = transport.run_git(project_path, "rev-parse", "HEAD")
    if rc != 0:
        return False
    local_sha = stdout.strip()
    if not local_sha:
        return False

    # What the remote knows (via ls-remote or log of the tracking ref).
    remote_sha = _remote_sha(project_path, branch, transport)

    if local_sha == remote_sha:
        # Already in sync — nothing to push.
        return False

    # Orphan commit(s): push the branch.
    stdout, stderr, rc = transport.run_git(project_path, "push", "origin", branch)
    if rc != 0:
        return False

    # Record the pushed SHA.
    sess.record_push(run_id, local_sha)
    return True


def _parse_ls_remote_line(line: str) -> str | None:
    """Defensive parse of an ls-remote output line. Accepts both
    '<sha>\\t<ref>' (tab, the documented form) and '<sha> <ref>' (space)
    whitespace variants. Returns the sha or None on parse failure."""
    line = line.strip()
    if not line:
        return None
    # Split on any whitespace (covers tab and/or space variants).
    parts = line.split()
    if not parts:
        return None
    sha = parts[0]
    # Basic sha sanity check: hex, 7-40 chars.
    if not all(c in "0123456789abcdefABCDEF" for c in sha):
        return None
    if len(sha) < 7:
        return None
    return sha


def _remote_sha(project_path: str, branch: str, transport) -> str | None:
    """Return the SHA the remote has for `branch`, or None if unknown/not
    pushed yet. Tries ls-remote first, falls back to the tracking ref log."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Try ls-remote (works even before any push).
    stdout, _, rc = transport.run_git(
        project_path, "ls-remote", "--heads", "origin", branch
    )
    if rc == 0 and stdout.strip():
        sha = _parse_ls_remote_line(stdout.strip())
        if sha is None:
            _log.warning("T6 ls-remote parse failure for %s: %r", branch,
                         stdout.strip())
        return sha

    # Fallback: local tracking ref (set after first fetch/push).
    stdout, _, rc = transport.run_git(
        project_path, "rev-parse", f"refs/remotes/origin/{branch}"
    )
    if rc == 0 and stdout.strip():
        return stdout.strip()

    return None
