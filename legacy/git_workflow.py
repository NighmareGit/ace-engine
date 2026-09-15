#!/usr/bin/env python3
"""
ACE-08: Git Workflow
Manages git operations on Triton (branch, commit, push, PR) via SSH.
Part of the autonomous coding engine.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Optional

from transport import get_transport

# Streaming event emission (fire-and-forget)
from streaming_client import emit_event_fire_and_forget as _emit


class GitWorkflow:
    """Manages git operations on Triton via transport abstraction layer."""

    def __init__(self, host: str = "<LAN_IP>", user: str = "<user>",
                 transport=None):
        self.host = host
        self.user = user
        self.gitea_token = "<GITEA_TOKEN>"
        self.gitea_url = "http://localhost:3000"
        self.transport = transport if transport is not None else get_transport()

    def _ssh_exec(self, command: str, verbose: bool = False) -> dict:
        """Execute a command via the transport layer.

        Thin wrapper that delegates to ``self.transport.run_command`` and
        converts the result to the legacy dict format expected by callers.
        """
        if verbose:
            print(f"[transport] {command}", file=sys.stderr)

        stdout, stderr, rc = self.transport.run_command(command, timeout=30)
        return {
            "success": rc == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": rc,
        }

    def create_branch(self, project_path: str, branch_name: str, verbose: bool = False, dry_run: bool = False) -> dict:
        """Create and checkout a new branch."""
        escaped_branch = branch_name.replace("'", "'\\''")

        if dry_run:
            command = f"git -C '{project_path}' checkout -b '{escaped_branch}'"
            return {"success": True, "dry_run": True, "command": command}

        stdout, stderr, rc = self.transport.run_git(
            project_path, "checkout", "-b", branch_name, timeout=30
        )
        if verbose:
            print(f"[transport] git checkout -b {branch_name} → rc={rc}", file=sys.stderr)
        return {
            "success": rc == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": rc,
        }

    def commit_all(self, project_path: str, message: str, verbose: bool = False, dry_run: bool = False) -> dict:
        """Stage all changes and commit."""
        escaped_path = project_path.replace("'", "'\\''")
        escaped_msg = message.replace("'", "'\\''")
        command = f"cd '{escaped_path}' && git add -A && git commit -m '{escaped_msg}'"

        if dry_run:
            return {"success": True, "dry_run": True, "command": command}

        result = self._ssh_exec(command, verbose)
        if result.get("success"):
            _emit("engine", "git.commit", {
                "message": message,
                "stdout": result.get("stdout", "")[:500],
            })
        return result

    def push(self, project_path: str, branch: Optional[str] = None, verbose: bool = False, dry_run: bool = False) -> dict:
        """Push to Gitea remote."""
        if branch is None:
            # Get current branch via transport
            stdout, stderr, rc = self.transport.run_git(
                project_path, "branch", "--show-current", timeout=30
            )
            if rc != 0:
                return {
                    "success": False,
                    "stdout": stdout.strip(),
                    "stderr": stderr.strip(),
                    "returncode": rc,
                }
            branch = stdout.strip()

        escaped_branch = branch.replace("'", "'\\''")

        if dry_run:
            command = f"git -C '{project_path}' push origin '{escaped_branch}'"
            return {"success": True, "dry_run": True, "command": command}

        stdout, stderr, rc = self.transport.run_git(
            project_path, "push", "origin", branch, timeout=30
        )
        if verbose:
            print(f"[transport] git push origin {branch} → rc={rc}", file=sys.stderr)
        return {
            "success": rc == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": rc,
        }

    def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        verbose: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Create a pull request on Gitea via API."""
        # repo is "owner/name" format
        payload = json.dumps({
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
        })

        # Escape single quotes in payload for shell
        escaped_payload = payload.replace("'", "'\\''")
        escaped_repo = repo.replace("'", "'\\''")

        curl_cmd = (
            f"curl -sf -X POST "
            f"'{self.gitea_url}/api/v1/repos/{escaped_repo}/pulls' "
            f"-H 'Authorization: token {self.gitea_token}' "
            f"-H 'Content-Type: application/json' "
            f"-d '{escaped_payload}'"
        )

        if dry_run:
            return {"success": True, "dry_run": True, "command": curl_cmd}

        result = subprocess.run(
            ["bash", "-c", curl_cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            return {
                "success": False,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }

        try:
            pr_data = json.loads(result.stdout)
            return {
                "success": True,
                "pr_url": pr_data.get("html_url", ""),
                "pr_number": pr_data.get("number"),
                "pr_data": pr_data,
            }
        except json.JSONDecodeError:
            return {"success": False, "error": "Failed to parse PR response"}

    def get_diff(self, project_path: str, verbose: bool = False) -> str:
        """Get current diff of staged+unstaged changes."""
        stdout, stderr, rc = self.transport.run_git(
            project_path, "diff", "HEAD", timeout=30
        )
        if verbose:
            print(f"[transport] git diff HEAD → rc={rc}", file=sys.stderr)
        if rc == 0:
            return stdout.strip()
        return f"Error: {stderr.strip()}"

    def get_status(self, project_path: str, verbose: bool = False) -> dict:
        """Get git status (modified, added, deleted files)."""
        stdout, stderr, rc = self.transport.run_git(
            project_path, "status", "--porcelain", timeout=30
        )
        if verbose:
            print(f"[transport] git status --porcelain → rc={rc}", file=sys.stderr)

        if rc != 0:
            return {
                "success": False,
                "stdout": stdout.strip(),
                "stderr": stderr.strip(),
                "returncode": rc,
            }

        files = {"modified": [], "added": [], "deleted": [], "untracked": []}
        for line in stdout.strip().splitlines():
            if not line.strip():
                continue
            status = line[:2].strip()
            filename = line[3:]
            if "M" in status:
                files["modified"].append(filename)
            elif "A" in status:
                files["added"].append(filename)
            elif "D" in status:
                files["deleted"].append(filename)
            elif "?" in status:
                files["untracked"].append(filename)

        return {"success": True, "files": files, "raw": stdout.strip()}

    def rollback(self, project_path: str, verbose: bool = False, dry_run: bool = False) -> dict:
        """Revert all uncommitted changes."""
        if dry_run:
            command = f"git -C '{project_path}' checkout -- ."
            return {"success": True, "dry_run": True, "command": command}

        stdout, stderr, rc = self.transport.run_git(
            project_path, "checkout", "--", ".", timeout=30
        )
        if verbose:
            print(f"[transport] git checkout -- . → rc={rc}", file=sys.stderr)
        return {
            "success": rc == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": rc,
        }

    def stash(self, project_path: str, verbose: bool = False, dry_run: bool = False) -> dict:
        """Stash current changes."""
        if dry_run:
            command = f"git -C '{project_path}' stash"
            return {"success": True, "dry_run": True, "command": command}

        stdout, stderr, rc = self.transport.run_git(
            project_path, "stash", timeout=30
        )
        if verbose:
            print(f"[transport] git stash → rc={rc}", file=sys.stderr)
        return {
            "success": rc == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": rc,
        }

    def stash_pop(self, project_path: str, verbose: bool = False, dry_run: bool = False) -> dict:
        """Pop stashed changes."""
        if dry_run:
            command = f"git -C '{project_path}' stash pop"
            return {"success": True, "dry_run": True, "command": command}

        stdout, stderr, rc = self.transport.run_git(
            project_path, "stash", "pop", timeout=30
        )
        if verbose:
            print(f"[transport] git stash pop → rc={rc}", file=sys.stderr)
        return {
            "success": rc == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": rc,
        }

    def get_current_branch(self, project_path: str, verbose: bool = False) -> Optional[str]:
        """Get the current branch name."""
        stdout, stderr, rc = self.transport.run_git(
            project_path, "branch", "--show-current", timeout=30
        )
        if verbose:
            print(f"[transport] git branch --show-current → rc={rc}", file=sys.stderr)
        if rc == 0:
            return stdout.strip()
        return None

    def _generate_branch_name(self, task: dict) -> str:
        """Generate branch name from task."""
        slug = re.sub(r'[^a-z0-9]+', '-', task['title'].lower())[:40]
        return f"ace/{task['id']}-{slug}"

    def _generate_commit_message(self, task: dict, files: list) -> str:
        """Generate conventional commit message."""
        return (
            f"feat({task.get('category', 'core')}): {task['title']}\n\n"
            f"Generated by autonomous coding engine.\n"
            f"Files: {', '.join(files)}"
        )


def cmd_branch(args: argparse.Namespace) -> None:
    """Handle branch subcommand."""
    gw = GitWorkflow()
    branch_name = args.name
    if not branch_name:
        print("Error: --name is required", file=sys.stderr)
        sys.exit(1)
    result = gw.create_branch(args.project_path, branch_name, args.verbose, args.dry_run)
    _print_result(result, args.dry_run)


def cmd_commit(args: argparse.Namespace) -> None:
    """Handle commit subcommand."""
    gw = GitWorkflow()
    message = args.message
    if not message:
        print("Error: --message is required", file=sys.stderr)
        sys.exit(1)
    result = gw.commit_all(args.project_path, message, args.verbose, args.dry_run)
    _print_result(result, args.dry_run)


def cmd_push(args: argparse.Namespace) -> None:
    """Handle push subcommand."""
    gw = GitWorkflow()
    result = gw.push(args.project_path, args.branch, args.verbose, args.dry_run)
    _print_result(result, args.dry_run)


def cmd_pr(args: argparse.Namespace) -> None:
    """Handle PR subcommand."""
    gw = GitWorkflow()
    result = gw.create_pr(
        args.repo,
        args.title,
        args.body or "",
        args.head,
        args.base,
        args.verbose,
        args.dry_run,
    )
    if args.dry_run:
        print(json.dumps(result, indent=2))
    elif result["success"]:
        print(f"PR created: {result['pr_url']}")
        print(json.dumps(result, indent=2))
    else:
        print(f"PR creation failed: {result.get('stderr', result.get('error', 'unknown error'))}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    """Handle status subcommand."""
    gw = GitWorkflow()
    result = gw.get_status(args.project_path, args.verbose)
    if result["success"]:
        files = result["files"]
        print("Git Status:")
        for category in ["modified", "added", "deleted", "untracked"]:
            if files[category]:
                print(f"  {category}:")
                for f in files[category]:
                    print(f"    {f}")
    else:
        print(f"Error: {result['stderr']}", file=sys.stderr)
        sys.exit(1)


def cmd_diff(args: argparse.Namespace) -> None:
    """Handle diff subcommand."""
    gw = GitWorkflow()
    diff = gw.get_diff(args.project_path, args.verbose)
    print(diff)


def cmd_rollback(args: argparse.Namespace) -> None:
    """Handle rollback subcommand."""
    gw = GitWorkflow()
    result = gw.rollback(args.project_path, args.verbose, args.dry_run)
    _print_result(result, args.dry_run)


def _print_result(result: dict, dry_run: bool = False) -> None:
    """Print command result."""
    if dry_run:
        print(f"[dry-run] Command: {result.get('command', 'N/A')}")
        return

    if result["success"]:
        print("Success")
        if result["stdout"]:
            print(result["stdout"])
    else:
        print(f"Error (exit {result.get('returncode', '?')}): {result.get('stderr', 'unknown error')}", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="ACE Git Workflow — manage git operations on Triton",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Dry run (print commands, don't execute)")

    subparsers = parser.add_subparsers(dest="command", help="Git operation to perform")

    # branch
    branch_parser = subparsers.add_parser("branch", help="Create and checkout a new branch")
    branch_parser.add_argument("project_path", help="Path to project on Triton")
    branch_parser.add_argument("--name", "-b", help="Branch name to create")
    branch_parser.set_defaults(func=cmd_branch)

    # commit
    commit_parser = subparsers.add_parser("commit", help="Stage all changes and commit")
    commit_parser.add_argument("project_path", help="Path to project on Triton")
    commit_parser.add_argument("--message", "-m", help="Commit message")
    commit_parser.set_defaults(func=cmd_commit)

    # push
    push_parser = subparsers.add_parser("push", help="Push to Gitea remote")
    push_parser.add_argument("project_path", help="Path to project on Triton")
    push_parser.add_argument("--branch", "-b", help="Branch to push (default: current branch)")
    push_parser.set_defaults(func=cmd_push)

    # pr
    pr_parser = subparsers.add_parser("pr", help="Create a pull request on Gitea")
    pr_parser.add_argument("--repo", "-r", required=True, help="Repository in owner/name format")
    pr_parser.add_argument("--title", "-t", required=True, help="PR title")
    pr_parser.add_argument("--body", help="PR body/description")
    pr_parser.add_argument("--head", required=True, help="Head branch (feature branch)")
    pr_parser.add_argument("--base", default="main", help="Base branch (default: main)")
    pr_parser.set_defaults(func=cmd_pr)

    # status
    status_parser = subparsers.add_parser("status", help="Get git status")
    status_parser.add_argument("project_path", help="Path to project on Triton")
    status_parser.set_defaults(func=cmd_status)

    # diff
    diff_parser = subparsers.add_parser("diff", help="Get current diff")
    diff_parser.add_argument("project_path", help="Path to project on Triton")
    diff_parser.set_defaults(func=cmd_diff)

    # rollback
    rollback_parser = subparsers.add_parser("rollback", help="Revert all uncommitted changes")
    rollback_parser.add_argument("project_path", help="Path to project on Triton")
    rollback_parser.set_defaults(func=cmd_rollback)

    # stash
    stash_parser = subparsers.add_parser("stash", help="Stash current changes")
    stash_parser.add_argument("project_path", help="Path to project on Triton")
    stash_parser.set_defaults(func=lambda args: _print_result(
        GitWorkflow().stash(args.project_path, args.verbose, args.dry_run), args.dry_run
    ))

    # stash-pop
    stash_pop_parser = subparsers.add_parser("stash-pop", help="Pop stashed changes")
    stash_pop_parser.add_argument("project_path", help="Path to project on Triton")
    stash_pop_parser.set_defaults(func=lambda args: _print_result(
        GitWorkflow().stash_pop(args.project_path, args.verbose, args.dry_run), args.dry_run
    ))

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
