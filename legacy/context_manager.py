#!/usr/bin/env python3
"""
ACE-06: Context Manager — Tracks project state, file contents,
modifications, and conversation history for the autonomous coding engine.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from difflib import unified_diff
from pathlib import Path
from typing import Any, Optional

from transport import get_transport, LocalTransport


class ContextManager:
    """Tracks project state, file contents, modifications, and conversation history."""

    def __init__(self, host: str = "<LAN_IP>", user: str = "<user>",
                 transport=None, **kwargs: object):
        self.host: str = host
        self.user: str = user
        self.transport = transport if transport is not None else get_transport()
        self.project_files: dict[str, dict] = {}       # {path: {content, size, modified, hash}}
        self.modified_files: dict[str, dict] = {}      # {path: {old_content, new_content, task_id}}
        self.test_results: dict[str, dict] = {}        # {task_id: {status, passed, failed}}
        self.conversation: list[dict] = []             # [{role, content, timestamp}]
        self.task_history: list[dict] = []             # [{task_id, status, files_changed, errors}]
        self.project_path: str = ""
        self.created_at: str = datetime.now().isoformat()
        self.updated_at: str = self.created_at

    # ── File I/O Helpers ──────────────────────────────────────

    _SKIP_DIRS = frozenset({
        "__pycache__", ".git", "node_modules", ".venv",
        "venv", ".tox", ".mypy_cache", ".pytest_cache",
        "dist", "build", ".eggs",
    })
    _SKIP_EXTS = frozenset({".pyc", ".pyo", ".so", ".o", ".class", ".jar"})

    def _list_files(self, project_path: str) -> list[tuple[str, str, int, float]]:
        """List all scannable files in *project_path*.

        Returns ``[(rel_path, abs_path, size, mtime), ...]`` with skipped
        directories and extensions already filtered out.

        * **Local mode** (Docker / volume-mount) — uses ``os.walk``.
        * **Remote mode** — shells out via ``transport.run_command``
          using ``find -printf``.
        """
        if isinstance(self.transport, LocalTransport):
            return self._list_files_local(project_path)
        return self._list_files_remote(project_path)

    def _list_files_local(self, project_path: str) -> list[tuple[str, str, int, float]]:
        """os.walk-based file listing (local / Docker mode)."""
        result: list[tuple[str, str, int, float]] = []
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
            for fname in files:
                ext = os.path.splitext(fname)[1]
                if ext in self._SKIP_EXTS:
                    continue
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, project_path)
                try:
                    st = os.stat(full)
                    if st.st_size > 1_000_000:  # skip files >1 MB
                        continue
                    result.append((rel, full, st.st_size, st.st_mtime))
                except (PermissionError, OSError):
                    continue
        return result

    def _list_files_remote(self, project_path: str) -> list[tuple[str, str, int, float]]:
        """SSH-based file listing via ``find -printf`` (remote / nightmare mode)."""
        find_parts = [f"find '{project_path}'"]
        for d in self._SKIP_DIRS:
            find_parts.append(f"-not -path '*/{d}/*'")
        for ext in self._SKIP_EXTS:
            find_parts.append(f"-not -name '*{ext}'")
        # %s = size in bytes, %T@ = mtime as epoch float, %p = path
        find_parts.append("-type f -printf '%s\\t%T@\\t%p\\n'")
        find_cmd = " ".join(find_parts)

        stdout, _stderr, rc = self.transport.run_command(find_cmd)
        if rc != 0:
            return []

        result: list[tuple[str, str, int, float]] = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            try:
                size = int(parts[0])
                mtime = float(parts[1])
                full = parts[2]
            except (ValueError, IndexError):
                continue
            if size > 1_000_000:
                continue
            rel = os.path.relpath(full, project_path)
            result.append((rel, full, size, mtime))
        return result

    def _read_file(self, path: str) -> Optional[str]:
        """Read file content.

        * **Local mode** — uses Python ``open()``.
        * **Remote mode** — uses ``transport.run_command("cat …")``.

        Returns ``None`` on failure.
        """
        if isinstance(self.transport, LocalTransport):
            return self._read_file_local(path)
        return self._read_file_remote(path)

    def _read_file_local(self, path: str) -> Optional[str]:
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except (PermissionError, OSError):
            return None

    def _read_file_remote(self, path: str) -> Optional[str]:
        stdout, _stderr, rc = self.transport.run_command(f"cat '{path}'")
        if rc != 0:
            return None
        return stdout

    # ── Snapshot ──────────────────────────────────────────────

    def snapshot_project(self, project_path: str) -> dict:
        """Take a snapshot of all project files.

        Uses :meth:`_list_files` and :meth:`_read_file` so that the same
        logic works in both local (Docker / volume-mount) and remote (SSH)
        execution environments.
        """
        self.project_path = os.path.abspath(project_path)
        self.project_files = {}
        skipped = 0

        for rel, full, size, mtime in self._list_files(self.project_path):
            content = self._read_file(full)
            if content is None:
                skipped += 1
                continue

            file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
            self.project_files[rel] = {
                "content": content[:2000],  # truncate for storage
                "size": size,
                "modified": datetime.fromtimestamp(mtime).isoformat(),
                "hash": file_hash,
            }

        self.updated_at = datetime.now().isoformat()
        summary = {
            "project_path": self.project_path,
            "files_captured": len(self.project_files),
            "files_skipped": skipped,
            "snapshot_time": self.updated_at,
        }
        return summary

    # ── Modification Tracking ─────────────────────────────────

    def record_modification(
        self, path: str, old_content: str, new_content: str, task_id: str
    ):
        """Record a file modification."""
        self.modified_files[path] = {
            "old_content": old_content[:2000],
            "new_content": new_content[:2000],
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
        }
        # Update the project file cache
        new_hash = hashlib.md5(new_content.encode("utf-8")).hexdigest()
        self.project_files[path] = {
            "content": new_content[:2000],
            "size": len(new_content),
            "modified": datetime.now().isoformat(),
            "hash": new_hash,
        }
        self.updated_at = datetime.now().isoformat()

    def record_file_created(self, path: str, content: str, task_id: str):
        """Record a newly created file."""
        self.record_modification(path, "", content, task_id)

    # ── Test Results ──────────────────────────────────────────

    def record_test_result(self, task_id: str, result: dict):
        """Record test results for a task."""
        self.test_results[task_id] = {
            "status": result.get("status", "unknown"),
            "passed": result.get("passed", 0),
            "failed": result.get("failed", 0),
            "errors": result.get("errors", []),
            "timestamp": datetime.now().isoformat(),
        }
        self.updated_at = datetime.now().isoformat()

    # ── Task History ──────────────────────────────────────────

    def record_task(self, task_id: str, status: str, files_changed: list[str] = None, errors: list[str] = None):
        """Record a task in history."""
        self.task_history.append({
            "task_id": task_id,
            "status": status,
            "files_changed": files_changed or [],
            "errors": errors or [],
            "timestamp": datetime.now().isoformat(),
        })
        self.updated_at = datetime.now().isoformat()

    # ── Context Building ──────────────────────────────────────

    def get_context_for_task(self, task: dict) -> dict:
        """Build context payload for a code generation task."""
        return {
            "project_structure": self._get_tree_summary(),
            "relevant_files": self._get_relevant_files(task),
            "recent_modifications": self._get_recent_changes(5),
            "test_history": self._get_test_summary(),
            "conversation_summary": self._summarize_conversation(),
        }

    def _get_tree_summary(self) -> dict:
        """Summarize project directory tree."""
        tree: dict[str, Any] = {}
        for path in sorted(self.project_files):
            parts = path.split(os.sep)
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = self.project_files[path]["size"]
        return tree

    def _get_relevant_files(self, task: dict) -> dict:
        """Get files relevant to the task."""
        relevant: dict[str, dict] = {}
        task_desc = task.get("description", "").lower()
        task_files = task.get("files_to_create", []) + task.get("files_to_modify", [])

        for path, info in self.project_files.items():
            # Direct file match
            if path in task_files:
                relevant[path] = info
                continue

            # Import / reference match
            basename = os.path.basename(path)
            stem = os.path.splitext(basename)[0].lower()
            if stem and stem in task_desc:
                relevant[path] = info

        # Limit to 10 most relevant
        return dict(list(relevant.items())[:10])

    def _get_recent_changes(self, limit: int = 5) -> list[dict]:
        """Get the most recent file modifications."""
        sorted_mods = sorted(
            self.modified_files.items(),
            key=lambda x: x[1].get("timestamp", ""),
            reverse=True,
        )
        return [
            {"path": p, "task_id": m["task_id"], "timestamp": m["timestamp"]}
            for p, m in sorted_mods[:limit]
        ]

    def _get_test_summary(self) -> dict:
        """Summarize test results."""
        if not self.test_results:
            return {"total_tasks": 0, "passed": 0, "failed": 0, "tasks": {}}

        passed = sum(1 for r in self.test_results.values() if r["status"] == "passed")
        failed = sum(1 for r in self.test_results.values() if r["status"] == "failed")
        return {
            "total_tasks": len(self.test_results),
            "passed": passed,
            "failed": failed,
            "tasks": dict(list(self.test_results.items())[-10:]),  # last 10
        }

    def _summarize_conversation(self) -> list[dict]:
        """Return recent conversation messages."""
        return self.conversation[-10:]  # last 10 messages

    # ── Conversation ──────────────────────────────────────────

    def add_conversation(self, role: str, content: str):
        """Add a message to conversation history."""
        self.conversation.append({
            "role": role,  # "user" or "assistant"
            "content": content[:2000],  # Truncate long messages
            "timestamp": datetime.now().isoformat(),
        })
        # Keep only last 20 messages
        self.conversation = self.conversation[-20:]
        self.updated_at = datetime.now().isoformat()

    # ── Project Summary ───────────────────────────────────────

    def get_project_summary(self) -> dict:
        """Summarize current project state."""
        total_size = sum(f["size"] for f in self.project_files.values())
        extensions: dict[str, int] = {}
        for path in self.project_files:
            ext = os.path.splitext(path)[1] or "(none)"
            extensions[ext] = extensions.get(ext, 0) + 1

        return {
            "project_path": self.project_path,
            "total_files": len(self.project_files),
            "total_size_bytes": total_size,
            "extensions": extensions,
            "modifications_made": len(self.modified_files),
            "tasks_recorded": len(self.task_history),
            "tests_recorded": len(self.test_results),
            "conversation_turns": len(self.conversation),
            "snapshot_time": self.created_at,
            "last_updated": self.updated_at,
        }

    # ── Diff ──────────────────────────────────────────────────

    def get_modification_diff(self, task_id: str = None) -> str:
        """Get unified diff of all modifications."""
        lines: list[str] = []
        items = self.modified_files.items()
        if task_id:
            items = [(p, m) for p, m in items if m["task_id"] == task_id]

        for path, mod in sorted(items):
            old_lines = mod["old_content"].splitlines(keepends=True)
            new_lines = mod["new_content"].splitlines(keepends=True)
            diff = unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
            diff_text = "".join(diff)
            if diff_text:
                lines.append(diff_text)

        return "\n".join(lines) if lines else "(no changes)"

    # ── Persistence ───────────────────────────────────────────

    def save_state(self, path: str):
        """Save context to JSON file."""
        state = {
            "version": 1,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "project_path": self.project_path,
            "project_files": self.project_files,
            "modified_files": self.modified_files,
            "test_results": self.test_results,
            "conversation": self.conversation,
            "task_history": self.task_history,
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(state, fh, indent=2)

    def load_state(self, path: str):
        """Load context from JSON file."""
        with open(path, "r") as fh:
            state = json.load(fh)

        self.created_at = state.get("created_at", self.created_at)
        self.updated_at = state.get("updated_at", self.updated_at)
        self.project_path = state.get("project_path", "")
        self.project_files = state.get("project_files", {})
        self.modified_files = state.get("modified_files", {})
        self.test_results = state.get("test_results", {})
        self.conversation = state.get("conversation", [])
        self.task_history = state.get("task_history", [])

    # ── Formatting ────────────────────────────────────────────

    def format_context(self, ctx: dict, max_chars: int = 4000) -> str:
        """Format a context payload into a readable string for prompts."""
        parts: list[str] = []

        parts.append("## Project Structure")
        tree = ctx.get("project_structure", {})
        parts.append(self._format_tree(tree))

        relevant = ctx.get("relevant_files", {})
        if relevant:
            parts.append("\n## Relevant Files")
            for path, info in list(relevant.items())[:5]:
                content = info.get("content", "")[:500]
                parts.append(f"\n### {path}\n```\n{content}\n```")

        recent = ctx.get("recent_modifications", [])
        if recent:
            parts.append("\n## Recent Modifications")
            for mod in recent:
                parts.append(f"- {mod['path']} (task {mod['task_id']})")

        tests = ctx.get("test_history", {})
        if tests.get("total_tasks", 0) > 0:
            parts.append(f"\n## Tests: {tests['passed']}/{tests['total_tasks']} passed")

        result = "\n".join(parts)
        return result[:max_chars]

    def _format_tree(self, tree: dict, prefix: str = "", depth: int = 0) -> str:
        """Format a nested dict as an ASCII tree."""
        if depth > 4:
            return ""
        lines: list[str] = []
        items = sorted(tree.items())
        for i, (key, val) in enumerate(items):
            connector = "└── " if i == len(items) - 1 else "├── "
            if isinstance(val, dict):
                lines.append(f"{prefix}{connector}{key}/")
                ext = "    " if i == len(items) - 1 else "│   "
                lines.append(self._format_tree(val, prefix + ext, depth + 1))
            else:
                lines.append(f"{prefix}{connector}{key} ({val} bytes)")
        return "\n".join(lines)

    # ── Project Summary as Text ───────────────────────────────

    def format_project_summary(self) -> str:
        """Format the project summary as readable text."""
        s = self.get_project_summary()
        lines = [
            f"Project: {s['project_path'] or '(not set)'}",
            f"Files:   {s['total_files']}",
            f"Size:    {s['total_size_bytes']:,} bytes",
            f"Mods:    {s['modifications_made']}",
            f"Tasks:   {s['tasks_recorded']}",
            f"Tests:   {s['tests_recorded']}",
        ]
        if s["extensions"]:
            exts = ", ".join(f"{k}:{v}" for k, v in sorted(s["extensions"].items(), key=lambda x: -x[1]))
            lines.append(f"Exts:    {exts}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="ACE-06 Context Manager — project state tracker"
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Snapshot project files")
    p_snap.add_argument("project_path", help="Path to project root")
    p_snap.add_argument("--output", help="Save state to JSON (enables chaining with summary/diff/context)")
    p_snap.add_argument("--dry-run", action="store_true", help="Show what would be captured")

    # summary
    p_sum = sub.add_parser("summary", help="Show project summary")
    p_sum.add_argument("--input", help="Load state from JSON first")

    # context
    p_ctx = sub.add_parser("context", help="Build context for a task")
    p_ctx.add_argument("--task", required=True, help="Path to task JSON file")
    p_ctx.add_argument("--input", help="Load state from JSON first")
    p_ctx.add_argument("--dry-run", action="store_true", help="Show context without saving")
    p_ctx.add_argument("--output", help="Save context to JSON")

    # save
    p_save = sub.add_parser("save", help="Save context state")
    p_save.add_argument("--output", required=True, help="Output JSON path")
    p_save.add_argument("--dry-run", action="store_true", help="Show what would be saved")

    # load
    p_load = sub.add_parser("load", help="Load context state")
    p_load.add_argument("--input", required=True, help="Input JSON path")

    # diff
    p_diff = sub.add_parser("diff", help="Show modification diff")
    p_diff.add_argument("--task-id", help="Filter by task ID")
    p_diff.add_argument("--input", help="Load state from JSON first")

    # conversation
    p_conv = sub.add_parser("conversation", help="Add a conversation message")
    p_conv.add_argument("--role", required=True, choices=["user", "assistant"])
    p_conv.add_argument("--content", required=True, help="Message content")
    p_conv.add_argument("--input", help="Load state from JSON first")
    p_conv.add_argument("--output", help="Save state after adding")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    mgr = ContextManager()

    # ── snapshot ──────────────────────────────────────
    if args.command == "snapshot":
        if args.dry_run:
            print(f"[dry-run] Would snapshot: {args.project_path}")
            # Count files that would be captured
            count = 0
            skip_dirs = {"__pycache__", ".git", "node_modules", ".venv"}
            for root, dirs, files in os.walk(args.project_path):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                count += len(files)
            print(f"[dry-run] ~{count} files would be scanned")
        else:
            result = mgr.snapshot_project(args.project_path)
            if args.output:
                mgr.save_state(args.output)
                print(json.dumps(result, indent=2))
                print(f"State saved to {args.output}", file=sys.stderr)
            else:
                print(json.dumps(result, indent=2))

    # ── summary ───────────────────────────────────────
    elif args.command == "summary":
        if args.input:
            mgr.load_state(args.input)
        print(mgr.format_project_summary())

    # ── context ───────────────────────────────────────
    elif args.command == "context":
        if args.input:
            mgr.load_state(args.input)
        with open(args.task, "r") as fh:
            task = json.load(fh)
        ctx = mgr.get_context_for_task(task)
        formatted = mgr.format_context(ctx)
        if args.dry_run:
            print(formatted)
        elif args.output:
            with open(args.output, "w") as fh:
                json.dump(ctx, fh, indent=2)
            print(f"Context saved to {args.output}")
        else:
            print(formatted)

    # ── save ──────────────────────────────────────────
    elif args.command == "save":
        if args.dry_run:
            print(f"[dry-run] Would save state to: {args.output}")
        else:
            mgr.save_state(args.output)
            print(f"State saved to {args.output}")

    # ── load ──────────────────────────────────────────
    elif args.command == "load":
        mgr.load_state(args.input)
        print(f"State loaded from {args.input}")
        print(mgr.format_project_summary())

    # ── diff ──────────────────────────────────────────
    elif args.command == "diff":
        if args.input:
            mgr.load_state(args.input)
        diff_text = mgr.get_modification_diff(args.task_id)
        print(diff_text)

    # ── conversation ──────────────────────────────────
    elif args.command == "conversation":
        if args.input:
            mgr.load_state(args.input)
        mgr.add_conversation(args.role, args.content)
        print(f"Message added ({len(mgr.conversation)} total)")
        if args.output:
            mgr.save_state(args.output)
            print(f"State saved to {args.output}")


if __name__ == "__main__":
    main()
