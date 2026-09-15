"""Engine git commit — branch, write files, commit, push via transport."""

import base64 as _base64
import os
import time
from dataclasses import dataclass

from engine import Task


def _gitea_api(method, path, data=None):
    """Minimal Gitea REST API helper (P2 commit hardening). Returns parsed JSON
    or raises on network/HTTP error. Tests mock this function to stay offline."""
    import requests
    gitea_url = os.environ.get("GITEA_URL", "http://localhost:3000")
    gitea_token = os.environ.get("GITEA_TOKEN", "")
    gitea_user = os.environ.get("GITEA_USER", "<user>")
    url = f"{gitea_url}/api/v1{path}"
    headers = {"Authorization": f"token {gitea_token}"} if gitea_token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, json=data, headers=headers, timeout=30)
    resp.raise_for_status()
    # 204 No Content (e.g. delete) returns empty body
    if resp.status_code == 204 or not resp.text:
        return {}
    return resp.json()


def _gitea_repo_exists(user, repo):
    """True if the Gitea repo already exists (HTTP 200)."""
    try:
        _gitea_api("GET", f"/repos/{user}/{repo}")
        return True
    except Exception:
        return False


def _gitea_create_repo(user, repo):
    """Create a repo via the Gitea API (opt-in autocreate)."""
    _gitea_api("POST", "/user/repos",
               {"name": repo, "auto_init": True, "default_branch": "main"})


def _build_origin_url(project_path):
    """Single correct authenticated-URL form (P2 — fixes the inverted ternary).

    Embeds the token iff present; never embeds an empty token. Derives the repo
    name from the project basename unless GITEA_REPO is set."""
    gitea_url = os.environ.get("GITEA_URL", "http://localhost:3000")
    gitea_user = os.environ.get("GITEA_USER", "<user>")
    gitea_token = os.environ.get("GITEA_TOKEN", "")
    repo_name = os.environ.get("GITEA_REPO") or os.path.basename(project_path)
    if not repo_name:
        raise ValueError(
            "Cannot determine Gitea repo name: set GITEA_REPO or use a "
            "non-empty project path.")
    host = gitea_url.split("://", 1)[-1]
    auth = f"{gitea_user}:{gitea_token}@" if gitea_token else ""
    return f"http://{auth}{host}/{gitea_user}/{repo_name}.git"


def _ensure_repo_exists(repo_name, autocreate):
    """Pre-flight repo existence check (P2). Fail fast with a clear error
    naming the repo unless autocreate (GITEA_AUTOCREATE=1) creates it."""
    gitea_user = os.environ.get("GITEA_USER", "<user>")
    if not repo_name or _gitea_repo_exists(gitea_user, repo_name):
        return
    if str(os.environ.get("GITEA_AUTOCREATE", "0")) == "1":
        _gitea_create_repo(gitea_user, repo_name)
        return
    raise ValueError(
        f"Gitea repo '{gitea_user}/{repo_name}' does not exist. "
        f"Create it in Gitea or set GITEA_AUTOCREATE=1 to auto-create.")


def _commit_git(project_path, commit_msg, transport, run_id=None, task_id="T00"):
    """Shared git core: branch → git add → commit → push → rev-parse.

    Assumes the files to commit are already on disk at project_path.
    Used by both commit_code (which writes files via write_project_file)
    and commit_research_artifacts (which writes files directly).

    Returns:
        CommitResult dataclass with success, sha, branch, error_message
    """
    branch = (f"run/{str(run_id).removeprefix('run-')}" if run_id
              else f"ace/{task_id}-{int(time.time())}")
    try:
        # 1. git checkout -B {branch}.
        transport.run_git(project_path, "checkout", "-B", branch)

        # 2. git add .
        transport.run_git(project_path, "add", ".")

        # 3. git commit
        stdout, stderr, rc = transport.run_git(project_path, "commit", "-m", commit_msg)
        if rc != 0:
            if "nothing to commit" in (stdout + stderr).lower():
                # Identical content already committed on this branch — not a
                # failure.  Return success with the existing HEAD sha so the
                # caller gets a valid commit_sha instead of None.
                sha_stdout, _, _ = transport.run_git(
                    project_path, "rev-parse", "HEAD")
                return CommitResult(
                    success=True, sha=sha_stdout.strip(), branch=branch,
                    error_message=None)
            transport.run_git(project_path, "reset", "--hard", "HEAD")
            return CommitResult(success=False, sha=None, branch=branch,
                              error_message=f"commit failed: {stderr}")

        # 3b. Ensure an origin remote exists (push target: Gitea).
        origin_url = _build_origin_url(project_path)
        repo_name = os.environ.get("GITEA_REPO") or os.path.basename(project_path)
        _ensure_repo_exists(repo_name,
                            autocreate=str(os.environ.get("GITEA_AUTOCREATE", "0")) == "1")
        _, _, rc_remote = transport.run_git(project_path, "remote", "add", "origin", origin_url)
        if rc_remote != 0:
            transport.run_git(project_path, "remote", "set-url", "origin", origin_url)

        # 4. git push
        stdout, stderr, rc = transport.run_git(project_path, "push", "origin", branch)
        if rc != 0:
            transport.run_git(project_path, "reset", "--hard", "HEAD~1")
            permanent = any(s in stderr.lower() for s in
                            ("authentication", "401", "403", "permission denied",
                             "not found", "404"))
            if permanent:
                return CommitResult(success=False, sha=None, branch=branch,
                                    error_message=f"PERMANENT push failure (do not retry): {stderr}",
                                    permanent=True)
            return CommitResult(success=False, sha=None, branch=branch,
                                error_message=f"push failed (retryable): {stderr}",
                                permanent=False)

        # 5. Get SHA
        stdout, _, _ = transport.run_git(project_path, "rev-parse", "HEAD")
        sha = stdout.strip()

        return CommitResult(success=True, sha=sha, branch=branch, error_message=None,
                            permanent=False)

    except Exception as e:
        try:
            transport.run_git(project_path, "reset", "--hard", "HEAD")
            if not branch.startswith("run/"):
                transport.run_git(project_path, "checkout", "-")
                transport.run_git(project_path, "branch", "-D", branch)
        except Exception:
            pass
        return CommitResult(success=False, sha=None, branch=branch, error_message=str(e))


def commit_code(project_path, task, code, transport, run_id=None):
    """
    Branch, write files, commit, push via transport.

    Args:
        project_path: str — absolute path to the project directory
        task: Task dataclass
        code: GeneratedCode dataclass (files dict maps filename -> content)
        transport: transport object with run_git() and run_command() methods

    Returns:
        CommitResult dataclass with success, sha, branch, error_message
    """
    branch = (f"run/{str(run_id).removeprefix('run-')}" if run_id
              else f"ace/{task.id}-{int(time.time())}")
    try:
        # 1. git checkout -B {branch}.
        transport.run_git(project_path, "checkout", "-B", branch)

        # 2. Write each file from code.files.
        for filename, content in code.files.items():
            full_path = f"{project_path}/{filename}"
            write_project_file(transport, full_path, content)
    except Exception as e:
        return CommitResult(success=False, sha=None, branch=branch, error_message=str(e))

    # 3. Stage, commit, push via the shared git core.
    commit_msg = f"feat({task.module}): {task.title}"
    return _commit_git(project_path, commit_msg, transport,
                       run_id=run_id, task_id=task.id)


def commit_research_artifacts(project_path, task, files, transport, run_id=None):
    """Branch, write research artifacts (verdict + evidence sidecar), commit, push.

    Args:
        project_path: str — absolute path to the project directory
        task: Task dataclass (used for commit message + branch naming)
        files: dict mapping relative filename -> content (verdict.md + sidecar.json).
            Files are written directly to disk (the caller is responsible for
            ensuring the write path is correct for their transport), then the
            committer stages + commits them.
        transport: transport object with run_git() and run_command() methods
        run_id: optional run identifier for branch naming

    Returns:
        CommitResult dataclass with success, sha, branch, error_message

    Uses the shared _commit_git core for branch → add → commit → push.
    """
    verdict_rel = getattr(task, "module", None) or f"verdicts/{getattr(task, 'id', 'T00')}-verdict.md"
    commit_msg = f"research({verdict_rel}): {getattr(task, 'title', 'research')}"
    # Write files directly to disk (local path).  For docker transports the
    # caller must ensure files land on the shared volume; the research handler
    # runs in local mode so direct writes are correct.
    for filename, content in files.items():
        full_path = os.path.join(project_path, filename)
        parent = os.path.dirname(full_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    # Now stage, commit, push via the shared git core.
    return _commit_git(project_path, commit_msg, transport,
                       run_id=run_id, task_id=getattr(task, 'id', 'T00'))


def cleanup_sandbox(container_name, transport):
    """Destroy orphaned Docker container on resume."""
    transport.run_command(f"docker rm -f {container_name}", timeout=30)


def write_project_file(transport, path, content):
    """Write a file on the remote/local project host via transport.run_command.
    Base64 encoding avoids all shell-quoting issues with generated code."""
    b64 = _base64.b64encode(content.encode("utf-8")).decode("ascii")
    stdout, stderr, rc = transport.run_command(
        f"mkdir -p $(dirname {path}) && echo {b64} | base64 -d > {path}", timeout=30)
    return rc == 0


@dataclass
class CommitResult:
    """Result of a git commit+push operation."""
    success: bool
    sha: str | None
    branch: str
    error_message: str | None
    permanent: bool = False  # P2 — True when push failure is non-retryable
