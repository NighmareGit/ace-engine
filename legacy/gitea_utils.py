#!/usr/bin/env python3
"""
gitea_utils.py — Gitea API operations for the coder-harness platform.

All interactions with the Gitea instance on Triton (http://localhost:3000)
are executed via SSH since Gitea is local to the Triton host.

Usage:
    python -m coder-harness.gitea_utils --help
    python gitea_utils.py list-repos
    python gitea_utils.py create-repo my-project -d "My project"
    python gitea_utils.py get-file my-repo README.md
    python gitea_utils.py create-file my-repo docs/guide.md "Hello" -m "Add guide"
    python gitea_utils.py --dry-run list-repos
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Any, Optional


# ---------------------------------------------------------------------------
# SSH helper (module-level convenience)
# ---------------------------------------------------------------------------

def ssh(
    cmd: str,
    host: str = "<LAN_IP>",
    user: str = "<user>",
    timeout: int = 30,
) -> tuple[str, int]:
    """Execute *cmd* on the remote host via SSH and return (stdout, rc)."""
    r = subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            f"{user}@{host}",
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return r.stdout.strip(), r.returncode


# ---------------------------------------------------------------------------
# GiteaClient
# ---------------------------------------------------------------------------

class GiteaClient:
    """Client for the Gitea REST API, executed via SSH on Triton.

    Every API call is tunnelled through SSH because Gitea only listens on
    ``localhost:3000`` inside the Triton host.  Authentication uses a
    personal access token.

    Attributes:
        host: Triton SSH hostname / IP.
        user: SSH username on Triton.
        token: Gitea personal-access-token.
        base_url: Gitea HTTP base URL *as seen from Triton*.
    """

    def __init__(
        self,
        host: str = "<LAN_IP>",
        user: str = "<user>",
        token: str = "<GITEA_TOKEN>",
        base_url: str = "http://localhost:3000",
    ) -> None:
        self.host = host
        self.user = user
        self.token = token
        self.base_url = base_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        timeout: int = 30,
    ) -> dict:
        """Execute a Gitea REST API call via SSH on Triton.

        Parameters:
            method: HTTP verb (GET, POST, PUT, DELETE, …).
            path:   API path appended after ``/api/v1`` (e.g. ``/repos``).
            data:   Optional JSON body – serialised automatically.
            timeout: SSH command timeout in seconds.

        Returns:
            Parsed JSON dict on success, or ``{"error": "<message>"}``.
        """
        url = f"{self.base_url}/api/v1{path}"
        cmd = (
            f'curl -sf -X {method} {url}'
            f' -H "Authorization: token {self.token}"'
        )
        if data is not None:
            # Escape single quotes inside the JSON payload
            payload = json.dumps(data).replace("'", "'\\''")
            cmd += f" -H 'Content-Type: application/json' -d '{payload}'"

        out, rc = self.ssh(cmd, timeout=timeout)
        if rc != 0:
            return {"error": out or f"SSH command failed with rc={rc}"}
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON response: {out[:200]}"}

    @staticmethod
    def _b64(content: str) -> str:
        """Base-64 encode a UTF-8 string (required by Gitea file API)."""
        return base64.b64encode(content.encode("utf-8")).decode("ascii")

    # ------------------------------------------------------------------
    # SSH execution
    # ------------------------------------------------------------------

    def ssh(self, cmd: str, timeout: int = 30) -> tuple[str, int]:
        """Execute *cmd* on Triton via SSH."""
        return ssh(cmd, host=self.host, user=self.user, timeout=timeout)

    # ------------------------------------------------------------------
    # Repository operations
    # ------------------------------------------------------------------

    def list_repos(self) -> list:
        """List all repos for the authenticated user."""
        repos, rc = self._paginate("/repos/search?limit=50")
        if isinstance(repos, dict) and "error" in repos:
            return [repos]
        if isinstance(repos, list):
            return repos
        return repos.get("data", []) if isinstance(repos, dict) else []

    def _paginate(self, path: str, max_pages: int = 10) -> tuple:
        """Helper: fetch paginated results, returning raw response."""
        # Try simple single-page first
        result = self._api("GET", path)
        return result, 0

    def get_repo(self, name: str) -> dict:
        """Get details for repository *name*."""
        return self._api("GET", f"/repos/{self.user}/{name}")

    def create_repo(
        self,
        name: str,
        description: str = "",
        auto_init: bool = False,
    ) -> dict:
        """Create a new repository.

        Parameters:
            name:        Repository name.
            description: Optional description.
            auto_init:   If *True* create an empty initial commit.

        Returns:
            Created-repository dict from Gitea.
        """
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "auto_init": auto_init,
            "default_branch": "main",
        }
        return self._api("POST", "/user/repos", data=body)

    def delete_repo(self, name: str) -> bool:
        """Delete repository *name*.  Returns *True* on success."""
        result = self._api("DELETE", f"/repos/{self.user}/{name}")
        # Gitea returns 204 No Content on successful delete → empty dict
        return not result.get("error")

    # ------------------------------------------------------------------
    # Branch operations
    # ------------------------------------------------------------------

    def list_branches(self, repo: str) -> list:
        """List branches in *repo*."""
        result = self._api("GET", f"/repos/{self.user}/{repo}/branches")
        if isinstance(result, list):
            return result
        return result.get("branches", []) if isinstance(result, dict) else []

    def create_branch(
        self,
        repo: str,
        branch: str,
        from_branch: str = "main",
    ) -> dict:
        """Create a new branch in *repo* starting from *from_branch*.

        Gitea doesn't have a dedicated branch-creation endpoint via the REST
        API, so we create the branch on Triton via ``git`` over SSH.
        """
        git_dir = f"/tmp/_gitea_branch_{repo}"
        clone_url = self._clone_url(repo)
        cmd = (
            f"cd /tmp && rm -rf {git_dir} && "
            f"git clone {clone_url} {git_dir} && "
            f"cd {git_dir} && "
            f"git branch {from_branch} 2>/dev/null; "
            f"git checkout {from_branch} 2>/dev/null; "
            f"git checkout -b {branch} && "
            f"git push origin {branch} && "
            f"rm -rf {git_dir}"
        )
        out, rc = self.ssh(cmd)
        if rc != 0:
            return {"error": out or "Branch creation failed"}
        return {"branch": branch, "from": from_branch}

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete *branch* in *repo*.

        Gitea exposes ``DELETE /repos/{owner}/{repo}/branches/{branch}``.
        """
        result = self._api(
            "DELETE", f"/repos/{self.user}/{repo}/branches/{branch}"
        )
        return not result.get("error")

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def get_file(
        self,
        repo: str,
        path: str,
        branch: str = "main",
    ) -> dict:
        """Get file content from *repo* at *path* on *branch*.

        Returns a dict with keys ``content`` (decoded UTF-8), ``sha``,
        ``size``, and ``encoding`` on success.
        """
        encoded_path = path.lstrip("/")
        result = self._api(
            "GET",
            f"/repos/{self.user}/{repo}/contents/{encoded_path}?ref={branch}",
        )
        if result.get("error"):
            return result
        # Gitea returns content as base64
        if "content" in result and result.get("encoding") == "base64":
            try:
                result["content"] = base64.b64decode(
                    result["content"]
                ).decode("utf-8")
            except Exception:
                pass  # keep original if decode fails
        return result

    def create_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
    ) -> dict:
        """Create or update a file in *repo*.

        Parameters:
            repo:    Repository name.
            path:    File path within the repo.
            content: UTF-8 file content.
            message: Commit message.
            branch:  Target branch.

        Returns:
            Gitea file-creation response dict.
        """
        encoded_path = path.lstrip("/")
        # First try to get the existing file SHA (needed for updates)
        existing = self._api(
            "GET",
            f"/repos/{self.user}/{repo}/contents/{encoded_path}?ref={branch}",
        )
        body: dict[str, Any] = {
            "message": message,
            "content": self._b64(content),
            "branch": branch,
        }
        # If the file exists, include its SHA so Gitea performs an update
        if existing and not existing.get("error") and "sha" in existing:
            body["sha"] = existing["sha"]

        return self._api(
            "POST",
            f"/repos/{self.user}/{repo}/contents/{encoded_path}",
            data=body,
        )

    def list_files(
        self,
        repo: str,
        path: str = "",
        branch: str = "main",
    ) -> list:
        """List files in a directory within *repo*.

        Parameters:
            repo:   Repository name.
            path:   Directory path (empty string = root).
            branch: Ref to list from.

        Returns:
            List of file-entry dicts from Gitea.
        """
        encoded_path = path.strip("/")
        url_path = (
            f"/repos/{self.user}/{repo}/contents/{encoded_path}"
            if encoded_path
            else f"/repos/{self.user}/{repo}/contents"
        )
        result = self._api("GET", f"{url_path}?ref={branch}")
        if isinstance(result, list):
            return result
        return result.get("entries", []) if isinstance(result, dict) else []

    # ------------------------------------------------------------------
    # Commit operations
    # ------------------------------------------------------------------

    def list_commits(
        self,
        repo: str,
        branch: str = "main",
        limit: int = 10,
    ) -> list:
        """List recent commits on *branch* in *repo*."""
        result = self._api(
            "GET",
            f"/repos/{self.user}/{repo}/commits?sha={branch}&limit={limit}",
        )
        if isinstance(result, list):
            return result
        return result.get("commits", []) if isinstance(result, dict) else []

    def get_commit(self, repo: str, sha: str) -> dict:
        """Get details for a specific commit by *sha*."""
        return self._api(
            "GET", f"/repos/{self.user}/{repo}/commits/{sha}"
        )

    # ------------------------------------------------------------------
    # Clone / push (via HTTP + token)
    # ------------------------------------------------------------------

    def _clone_url(self, repo: str) -> str:
        """Build the HTTP clone URL with embedded token."""
        return f"http://{self.user}:{self.token}@localhost:3000/{self.user}/{repo}.git"

    def clone(self, repo: str, target_dir: str) -> bool:
        """Clone *repo* to *target_dir* on Triton.

        Uses the HTTP clone URL with the access token so no interactive
        authentication is required.

        Returns:
            *True* on success.
        """
        clone_url = self._clone_url(repo)
        cmd = (
            f"rm -rf {target_dir} && "
            f"git clone {clone_url} {target_dir}"
        )
        out, rc = self.ssh(cmd)
        return rc == 0

    def push(
        self,
        local_dir: str,
        repo: str,
        branch: str = "main",
    ) -> bool:
        """Push local changes from *local_dir* on Triton to *repo*.

        The remote is set to the token-authenticated HTTP URL so no
        interactive prompts appear.

        Returns:
            *True* on success.
        """
        clone_url = self._clone_url(repo)
        cmd = (
            f"cd {local_dir} && "
            f"git remote set-url origin {clone_url} && "
            f"git push origin {branch}"
        )
        out, rc = self.ssh(cmd)
        return rc == 0

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_code(self, query: str, repo: Optional[str] = None) -> list:
        """Search code across repos (or within *repo* if given)."""
        if repo:
            result = self._api(
                "GET",
                f"/repos/{self.user}/{repo}/search?q={query}",
            )
        else:
            result = self._api("GET", f"/codesearch?q={query}")
        if isinstance(result, list):
            return result
        return result.get("data", []) if isinstance(result, dict) else []


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gitea_utils",
        description="Gitea API operations for the coder-harness platform.",
    )
    p.add_argument(
        "--host",
        default="<LAN_IP>",
        help="Triton SSH host (default: <LAN_IP>)",
    )
    p.add_argument(
        "--user",
        default="<user>",
        help="SSH user on Triton (default: <user>)",
    )
    p.add_argument(
        "--token",
        default="<GITEA_TOKEN>",
        help="Gitea personal-access token",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the SSH command that would be executed, without running it",
    )

    sub = p.add_subparsers(dest="command", help="Gitea operation to perform")

    # list-repos
    sub.add_parser("list-repos", help="List all repositories")

    # get-repo
    sp = sub.add_parser("get-repo", help="Get repo details")
    sp.add_argument("name", help="Repository name")

    # create-repo
    sp = sub.add_parser("create-repo", help="Create a new repository")
    sp.add_argument("name", help="Repository name")
    sp.add_argument("-d", "--description", default="", help="Description")
    sp.add_argument(
        "--auto-init",
        action="store_true",
        help="Create initial empty commit",
    )

    # delete-repo
    sp = sub.add_parser("delete-repo", help="Delete a repository")
    sp.add_argument("name", help="Repository name")

    # list-branches
    sp = sub.add_parser("list-branches", help="List branches in a repo")
    sp.add_argument("repo", help="Repository name")

    # create-branch
    sp = sub.add_parser("create-branch", help="Create a new branch")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("branch", help="New branch name")
    sp.add_argument(
        "--from",
        dest="from_branch",
        default="main",
        help="Source branch (default: main)",
    )

    # delete-branch
    sp = sub.add_parser("delete-branch", help="Delete a branch")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("branch", help="Branch name to delete")

    # get-file
    sp = sub.add_parser("get-file", help="Get file content")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("path", help="File path in the repo")
    sp.add_argument("--branch", default="main", help="Branch (default: main)")

    # create-file
    sp = sub.add_parser("create-file", help="Create or update a file")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("path", help="File path in the repo")
    sp.add_argument("content", help="File content (UTF-8 string)")
    sp.add_argument("-m", "--message", required=True, help="Commit message")
    sp.add_argument("--branch", default="main", help="Branch (default: main)")

    # list-files
    sp = sub.add_parser("list-files", help="List files in a directory")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument(
        "--path", default="", help="Directory path (empty = root)"
    )
    sp.add_argument("--branch", default="main", help="Branch (default: main)")

    # list-commits
    sp = sub.add_parser("list-commits", help="List recent commits")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("--branch", default="main", help="Branch (default: main)")
    sp.add_argument(
        "--limit", type=int, default=10, help="Max commits to show"
    )

    # get-commit
    sp = sub.add_parser("get-commit", help="Get commit details")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("sha", help="Commit SHA")

    # clone
    sp = sub.add_parser("clone", help="Clone a repo on Triton")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("target_dir", help="Local target directory on Triton")

    # push
    sp = sub.add_parser("push", help="Push local changes to Gitea")
    sp.add_argument("local_dir", help="Local directory on Triton")
    sp.add_argument("repo", help="Repository name")
    sp.add_argument("--branch", default="main", help="Branch (default: main)")

    # search
    sp = sub.add_parser("search", help="Search code")
    sp.add_argument("query", help="Search query")
    sp.add_argument("--repo", default=None, help="Restrict to this repo")

    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point.  Returns 0 on success, 1 on error."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    client = GiteaClient(
        host=args.host, user=args.user, token=args.token
    )

    # --dry-run: just print the command
    if getattr(args, "dry_run", False):
        print(f"[dry-run] Would execute command: {args.command}")
        _print_dry_run(args)
        return 0

    result: Any = None

    try:
        if args.command == "list-repos":
            result = client.list_repos()
        elif args.command == "get-repo":
            result = client.get_repo(args.name)
        elif args.command == "create-repo":
            result = client.create_repo(
                args.name,
                description=args.description,
                auto_init=args.auto_init,
            )
        elif args.command == "delete-repo":
            ok = client.delete_repo(args.name)
            result = {"deleted": ok, "repo": args.name}
        elif args.command == "list-branches":
            result = client.list_branches(args.repo)
        elif args.command == "create-branch":
            result = client.create_branch(
                args.repo, args.branch, from_branch=args.from_branch
            )
        elif args.command == "delete-branch":
            ok = client.delete_branch(args.repo, args.branch)
            result = {"deleted": ok, "repo": args.repo, "branch": args.branch}
        elif args.command == "get-file":
            result = client.get_file(args.repo, args.path, branch=args.branch)
        elif args.command == "create-file":
            result = client.create_file(
                args.repo,
                args.path,
                args.content,
                args.message,
                branch=args.branch,
            )
        elif args.command == "list-files":
            result = client.list_files(
                args.repo, path=args.path, branch=args.branch
            )
        elif args.command == "list-commits":
            result = client.list_commits(
                args.repo, branch=args.branch, limit=args.limit
            )
        elif args.command == "get-commit":
            result = client.get_commit(args.repo, args.sha)
        elif args.command == "clone":
            ok = client.clone(args.repo, args.target_dir)
            result = {"cloned": ok, "repo": args.repo, "target": args.target_dir}
        elif args.command == "push":
            ok = client.push(
                args.local_dir, args.repo, branch=args.branch
            )
            result = {"pushed": ok, "repo": args.repo, "branch": args.branch}
        elif args.command == "search":
            result = client.search_code(args.query, repo=args.repo)
        else:
            parser.print_help()
            return 1
    except Exception as exc:
        result = {"error": str(exc)}
        print(json.dumps(result, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


def _print_dry_run(args: argparse.Namespace) -> None:
    """Show the SSH command a sub-command would issue."""
    if args.command == "list-repos":
        print("  curl -sf -X GET http://localhost:3000/api/v1/repos/search?limit=50 ...")
    elif args.command == "get-repo":
        print(f"  curl -sf -X GET .../repos/{args.user}/{args.name}")
    elif args.command == "create-repo":
        print(f"  curl -sf -X POST .../user/repos  name={args.name}")
    elif args.command == "clone":
        print(f"  git clone http://{args.user}:***@localhost:3000/{args.user}/{args.repo}.git {args.target_dir}")
    elif args.command == "push":
        print(f"  cd {args.local_dir} && git push origin {args.branch}")
    else:
        print(f"  [dry-run] {args.command}")


# Allow ``python gitea_utils.py …`` directly.
if __name__ == "__main__":
    sys.exit(main())
