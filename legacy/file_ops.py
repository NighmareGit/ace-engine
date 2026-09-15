#!/usr/bin/env python3
"""
ACE-02: File Operations on Triton
Module for reading, writing, listing, and grepping files on a remote Triton machine via SSH.

Python stdlib only (subprocess, json, base64, argparse, pathlib).
All operations go through SSH to the Triton machine.
"""

import subprocess
import json
import base64
import argparse
import os
import sys
from pathlib import Path
from datetime import datetime

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"

# Default source extensions for grep_code
DEFAULT_GREP_INCLUDES = (
    "*.py", "*.js", "*.ts", "*.tsx", "*.jsx",
    "*.java", "*.go", "*.rs", "*.c", "*.h",
    "*.cpp", "*.hpp", "*.rb", "*.sh",
)

# Directories to exclude from project structure
STRUCTURE_EXCLUDES = (
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", "dist", "build", ".mypy_cache", ".pytest_cache",
    "egg-info", ".eggs",
)


class FileOps:
    """File operations on a remote Triton machine via SSH."""

    def __init__(self, host: str = TRITON_HOST, user: str = TRITON_USER):
        self.host = host
        self.user = user

    # ── internal helpers ──────────────────────────────────────────────

    def _ssh(self, cmd: str, timeout: int = 30) -> tuple[str, int]:
        """Execute a command on Triton via SSH. Returns (stdout, returncode)."""
        r = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{self.user}@{self.host}",
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout.strip(), r.returncode

    def _ssh_stdin(self, cmd: str, stdin_data: str, timeout: int = 30) -> tuple[str, int]:
        """Execute a command on Triton via SSH, passing *stdin_data* on stdin.

        This avoids shell-argument-length limits for large payloads.
        Returns (stdout, returncode).
        """
        r = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{self.user}@{self.host}",
                cmd,
            ],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout.strip(), r.returncode

    def _ssh_rc(self, cmd: str, timeout: int = 30) -> int:
        """Execute a command and return only the return code."""
        _, rc = self._ssh(cmd, timeout=timeout)
        return rc

    def _is_binary(self, remote_path: str) -> bool:
        """Detect if a remote file is binary using the ``file`` command."""
        out, rc = self._ssh(f"file --mime-type -b {_q(remote_path)}")
        if rc != 0:
            return False
        # anything that isn't text/* is considered binary
        return not out.startswith("text/")

    # ── public API ────────────────────────────────────────────────────

    def read_file(
        self,
        remote_path: str,
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Read a file from Triton.

        Returns ``{content, size, modified, encoding, path}``.

        For binary files the content is base64-encoded and *encoding* is
        ``"base64"``; otherwise encoding is ``"utf-8"``.

        Handles large files (>1 MB) by streaming through ``cat`` with a
        generous timeout.
        """
        stat_cmd = f"stat -c '%s|%Y' {_q(remote_path)} 2>/dev/null"
        if dry_run:
            return {
                "path": remote_path,
                "dry_run": True,
                "command": f"{stat_cmd} && cat {_q(remote_path)}",
            }
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {stat_cmd}")

        # stat first — get size and mtime
        stat_out, stat_rc = self._ssh(stat_cmd)
        if stat_rc != 0 or "|" not in stat_out:
            raise FileNotFoundError(
                f"Remote file not found or not readable: {remote_path}"
            )

        size_str, mtime_str = stat_out.split("|", 1)
        size = int(size_str)
        modified = datetime.fromtimestamp(int(mtime_str)).isoformat()

        # Choose timeout based on file size
        timeout = 300 if size > 1_000_000 else 120

        if self._is_binary(remote_path):
            content_out, rc = self._ssh(
                f"base64 {_q(remote_path)}", timeout=timeout
            )
            if rc != 0:
                raise IOError(f"Failed to read remote file: {remote_path}")
            return {
                "content": content_out,
                "size": size,
                "modified": modified,
                "encoding": "base64",
                "path": remote_path,
            }

        # Text file — stream via cat
        content_out, rc = self._ssh(
            f"cat {_q(remote_path)}", timeout=timeout
        )
        if rc != 0:
            raise IOError(f"Failed to read remote file: {remote_path}")
        return {
            "content": content_out,
            "size": size,
            "modified": modified,
            "encoding": "utf-8",
            "path": remote_path,
        }

    def write_file(
        self,
        remote_path: str,
        content: str,
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Write *content* to a remote file atomically (write-to-tmp + mv).

        For content >1 MB the payload is piped via stdin to avoid
        shell-argument-length limits.

        Returns ``{success, bytes_written, path}``.
        """
        byte_len = len(content.encode("utf-8"))
        tmp = f"{remote_path}.tmp.{os.getpid()}"

        # For large content, pipe via stdin; for small, use echo | base64
        if byte_len > 500_000:
            # Pipe through stdin — avoids shell arg limits
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            cmd = f"base64 -d > {_q(tmp)} && mv {_q(tmp)} {_q(remote_path)}"
            if dry_run:
                return {
                    "success": True,
                    "bytes_written": byte_len,
                    "path": remote_path,
                    "dry_run": True,
                    "command": f"<base64-pipe> | {cmd}",
                }
            if verbose:
                print(f"[file_ops] {self.user}@{self.host}: <pipe {byte_len} bytes> | {cmd}")
            _, rc = self._ssh_stdin(cmd, encoded, timeout=300)
        else:
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            cmd = (
                f"echo {_q(encoded)} | base64 -d > {_q(tmp)} "
                f"&& mv {_q(tmp)} {_q(remote_path)}"
            )
            if dry_run:
                return {
                    "success": True,
                    "bytes_written": byte_len,
                    "path": remote_path,
                    "dry_run": True,
                    "command": cmd,
                }
            if verbose:
                print(f"[file_ops] {self.user}@{self.host}: {cmd}")
            _, rc = self._ssh(cmd, timeout=120)

        if rc != 0:
            # cleanup tmp if it lingers
            self._ssh(f"rm -f {_q(tmp)}")
            raise IOError(f"Failed to write remote file: {remote_path}")
        return {
            "success": True,
            "bytes_written": byte_len,
            "path": remote_path,
            "dry_run": False,
        }

    def list_files(
        self,
        remote_path: str,
        pattern: str = "*",
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> list[dict]:
        """List files matching *pattern* at *remote_path*.

        Returns ``[{name, size, modified, is_dir, path}]``.

        Uses ``find -maxdepth 1`` with ``-printf`` to resolve the directory
        flag in a single SSH call (no N+1 round-trips).
        """
        cmd = (
            f"find {_q(remote_path)} -maxdepth 1 "
            f"-name {_q(pattern)} "
            f'-printf "%y|%p|%s|%T@\\n"'
        )
        if dry_run:
            return [{"dry_run": True, "command": cmd}]
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        out, rc = self._ssh(cmd, timeout=30)
        if rc != 0:
            return []

        entries: list[dict] = []
        for line in out.splitlines():
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            ftype, fpath, size_str, mtime_str = parts

            # Skip the directory itself (the root of the listing)
            if fpath.rstrip("/") == remote_path.rstrip("/"):
                continue

            is_dir = ftype == "d"
            name = os.path.basename(fpath)
            try:
                mtime = datetime.fromtimestamp(int(float(mtime_str))).isoformat()
            except (ValueError, OSError):
                mtime = ""

            entries.append(
                {
                    "name": name,
                    "path": fpath,
                    "size": int(size_str) if size_str.isdigit() else 0,
                    "modified": mtime,
                    "is_dir": is_dir,
                }
            )
        return entries

    def grep_code(
        self,
        remote_path: str,
        pattern: str,
        *,
        include: str | None = None,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> list[dict]:
        """Search for *pattern* in source files under *remote_path*.

        Returns ``[{file, line, match}]``.

        By default searches all common source extensions
        (``DEFAULT_GREP_INCLUDES``).  Pass *include* to override with a
        single glob (e.g. ``"*.py"``).
        """
        if include and include != "*":
            includes_str = f'--include="{include}"'
        else:
            includes_str = " ".join(
                f'--include="{ext}"' for ext in DEFAULT_GREP_INCLUDES
            )

        cmd = (
            f"grep -rn {_q(pattern)} "
            f"{_q(remote_path)} "
            f"{includes_str} 2>/dev/null || true"
        )
        if dry_run:
            return [{"dry_run": True, "command": cmd}]
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        out, _ = self._ssh(cmd, timeout=60)
        if not out:
            return []

        results: list[dict] = []
        for line in out.splitlines():
            # Format: filepath:lineno:content
            if ":" not in line:
                continue
            try:
                first_colon = line.index(":")
                second_colon = line.index(":", first_colon + 1)
                fpath = line[:first_colon]
                lineno = int(line[first_colon + 1 : second_colon])
                content = line[second_colon + 1 :]
                results.append(
                    {"file": fpath, "line": lineno, "match": content.strip()}
                )
            except (ValueError, IndexError):
                continue
        return results

    def file_exists(
        self,
        remote_path: str,
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> bool:
        """Check if *remote_path* exists on Triton."""
        cmd = f"[ -e {_q(remote_path)} ]"
        if dry_run:
            # For dry-run, report the command — existence unknown
            if verbose:
                print(f"[file_ops] {self.user}@{self.host}: {cmd}")
            return False  # can't know without SSH
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        rc = self._ssh_rc(cmd)
        return rc == 0

    def mkdir(
        self,
        remote_path: str,
        *,
        parents: bool = True,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Create a directory (and parents by default) on Triton."""
        flag = "-p" if parents else ""
        cmd = f"mkdir {flag} {_q(remote_path)}"
        if dry_run:
            return {
                "success": True, "path": remote_path,
                "dry_run": True, "command": cmd,
            }
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        _, rc = self._ssh(cmd)
        if rc != 0:
            raise IOError(f"Failed to create directory: {remote_path}")
        return {"success": True, "path": remote_path, "dry_run": False}

    def append_file(
        self,
        remote_path: str,
        content: str,
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Append *content* to a remote file.

        Returns ``{success, bytes_appended, path}``.
        """
        byte_len = len(content.encode("utf-8"))
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        cmd = f"echo {_q(encoded)} | base64 -d >> {_q(remote_path)}"
        if dry_run:
            return {
                "success": True,
                "bytes_appended": byte_len,
                "path": remote_path,
                "dry_run": True,
                "command": cmd,
            }
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        _, rc = self._ssh(cmd, timeout=60)
        if rc != 0:
            raise IOError(f"Failed to append to remote file: {remote_path}")
        return {
            "success": True,
            "bytes_appended": byte_len,
            "path": remote_path,
            "dry_run": False,
        }

    def delete_file(
        self,
        remote_path: str,
        *,
        recursive: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Delete a remote file or directory."""
        flag = "-rf" if recursive else "-f"
        cmd = f"rm {flag} {_q(remote_path)}"
        if dry_run:
            return {
                "success": True, "path": remote_path,
                "dry_run": True, "command": cmd,
            }
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        _, rc = self._ssh(cmd)
        if rc != 0:
            raise IOError(f"Failed to delete: {remote_path}")
        return {"success": True, "path": remote_path, "dry_run": False}

    def copy_file(
        self,
        src: str,
        dst: str,
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Copy a file on Triton."""
        cmd = f"cp {_q(src)} {_q(dst)}"
        if dry_run:
            return {
                "success": True, "src": src, "dst": dst,
                "dry_run": True, "command": cmd,
            }
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        _, rc = self._ssh(cmd)
        if rc != 0:
            raise IOError(f"Failed to copy {src} -> {dst}")
        return {"success": True, "src": src, "dst": dst, "dry_run": False}

    def get_project_structure(
        self,
        remote_path: str,
        depth: int = 3,
        *,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Return the directory tree under *remote_path* up to *depth* levels.

        Returns ``{path, tree: [str], entry_count}``.
        Excludes common noise directories (.git, node_modules, __pycache__, etc.).
        """
        excludes = " ".join(
            f"-not -path '*/{d}/*'" for d in STRUCTURE_EXCLUDES
        )
        cmd = (
            f"find {_q(remote_path)} -maxdepth {depth} "
            f"{excludes} "
            f"| head -100"
        )
        if dry_run:
            return {"path": remote_path, "dry_run": True, "command": cmd}
        if verbose:
            print(f"[file_ops] {self.user}@{self.host}: {cmd}")
        out, rc = self._ssh(cmd, timeout=30)
        if rc != 0:
            return {"path": remote_path, "tree": [], "entry_count": 0}
        entries = [e for e in out.splitlines() if e]
        return {
            "path": remote_path,
            "tree": entries,
            "entry_count": len(entries),
        }


# ── helpers ───────────────────────────────────────────────────────────


def _q(s: str) -> str:
    """Minimal POSIX shell quoting (single-quote everything).

    Handles embedded single quotes by breaking out of the quote, inserting
    an escaped quote, and re-entering:  ``'it'\''s'``
    """
    return "'" + s.replace("'", "'\\''") + "'"


# Keep the old name as an alias for backward compatibility
shlex_quote = _q


# ── CLI ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="file_ops",
        description="File operations on Triton via SSH",
    )
    p.add_argument("--host", default=TRITON_HOST, help="Triton host")
    p.add_argument("--user", default=TRITON_USER, help="SSH user")
    p.add_argument("--dry-run", action="store_true",
                    help="Show command without executing")
    p.add_argument("--verbose", "-v", action="store_true",
                    help="Print SSH commands")

    sub = p.add_subparsers(dest="command", required=True)

    # read
    sp = sub.add_parser("read", help="Read a file")
    sp.add_argument("path", help="Remote file path")

    # write
    sp = sub.add_parser("write", help="Write content to a file")
    sp.add_argument("path", help="Remote file path")
    sp.add_argument("content", help="Content to write")

    # list
    sp = sub.add_parser("list", help="List files in a directory")
    sp.add_argument("path", help="Remote directory path")
    sp.add_argument("--pattern", default="*", help="Glob pattern")

    # grep
    sp = sub.add_parser("grep", help="Search for pattern in source files")
    sp.add_argument("path", help="Remote directory to search")
    sp.add_argument("pattern", help="Regex or literal pattern")
    sp.add_argument("--include", default="*",
                    help="File glob (default: all source extensions)")

    # exists
    sp = sub.add_parser("exists", help="Check if a file exists")
    sp.add_argument("path", help="Remote file path")

    # mkdir
    sp = sub.add_parser("mkdir", help="Create a directory")
    sp.add_argument("path", help="Remote directory path")
    sp.add_argument("--no-parents", action="store_true",
                    help="Do not create parent directories")

    # append
    sp = sub.add_parser("append", help="Append content to a file")
    sp.add_argument("path", help="Remote file path")
    sp.add_argument("content", help="Content to append")

    # delete
    sp = sub.add_parser("delete", help="Delete a file or directory")
    sp.add_argument("path", help="Remote path to delete")
    sp.add_argument("--recursive", "-r", action="store_true",
                    help="Recursive delete")

    # copy
    sp = sub.add_parser("copy", help="Copy a file")
    sp.add_argument("src", help="Source path")
    sp.add_argument("dst", help="Destination path")

    # tree
    sp = sub.add_parser("tree", help="Show project directory tree")
    sp.add_argument("path", help="Remote project root")
    sp.add_argument("--depth", type=int, default=3, help="Max depth")

    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    ops = FileOps(host=args.host, user=args.user)
    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)

    try:
        if args.command == "read":
            result = ops.read_file(
                args.path, dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "write":
            result = ops.write_file(
                args.path, args.content,
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "list":
            result = ops.list_files(
                args.path, pattern=args.pattern,
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "grep":
            result = ops.grep_code(
                args.path, args.pattern,
                include=args.include,
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "exists":
            exists = ops.file_exists(
                args.path, dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps({"path": args.path, "exists": exists}))

        elif args.command == "mkdir":
            parents = not getattr(args, "no_parents", False)
            result = ops.mkdir(
                args.path, parents=parents,
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "append":
            result = ops.append_file(
                args.path, args.content,
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "delete":
            result = ops.delete_file(
                args.path,
                recursive=getattr(args, "recursive", False),
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "copy":
            result = ops.copy_file(
                args.src, args.dst,
                dry_run=dry_run, verbose=verbose,
            )
            print(json.dumps(result, indent=2))

        elif args.command == "tree":
            result = ops.get_project_structure(
                args.path, depth=args.depth,
                dry_run=dry_run, verbose=verbose,
            )
            # Print tree lines for readability
            if "dry_run" in result:
                print(json.dumps(result, indent=2))
            elif result["tree"]:
                for entry in result["tree"]:
                    print(entry)
                print(f"\n({result['entry_count']} entries)")
            else:
                print("(empty tree)")

    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    except IOError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print(
            json.dumps({"error": "SSH command timed out"}),
            file=sys.stderr,
        )
        sys.exit(3)


if __name__ == "__main__":
    main()
