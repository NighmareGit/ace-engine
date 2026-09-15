#!/usr/bin/env python3
"""
ACE-07: Quality Gates
Runs linting, style checks, and basic security analysis on generated code.
All checks are Python stdlib only (ast, re, subprocess, json, argparse).
"""

import ast
import re
import subprocess
import json
import argparse
import os
import sys
from typing import Optional

from streaming_client import emit_event_fire_and_forget as _emit


# ---------------------------------------------------------------------------
# Security patterns (regex, severity, human message)
# ---------------------------------------------------------------------------
SECURITY_PATTERNS = [
    (r'eval\s*\(', "high", "Use of eval() — potential code injection"),
    (r'exec\s*\(', "high", "Use of exec() — potential code injection"),
    (r'__import__\s*\(', "medium", "Dynamic import — potential security risk"),
    (r'subprocess\.call.*shell=True', "high", "Shell injection risk"),
    (r'pickle\.loads?\s*\(', "medium", "Pickle deserialization — potential RCE"),
    (r'yaml\.load\s*\([^)]*\)', "medium", "Unsafe YAML loading"),
    (r'SQL.*\+.*format', "high", "Potential SQL injection"),
    (r'password\s*=\s*["\'][^"\']+', "high", "Hardcoded password detected"),
]


class QualityGates:
    """Orchestrate quality checks on Python project files."""

    def __init__(self, host: str = "<LAN_IP>", user: str = "<user>"):
        self.host = host
        self.user = user

    # ------------------------------------------------------------------
    # High-level entry point
    # ------------------------------------------------------------------

    def check_all(
        self,
        project_path: str,
        files: Optional[list] = None,
        task_id: Optional[str] = None,
    ) -> dict:
        """Run all quality checks. Returns overall pass/fail + per-check results.

        If *files* is None every ``.py`` file discovered under *project_path*
        is checked.  Each top-level check is keyed in the ``checks`` dict with
        an aggregated status and per-file details.

        If *files* is an empty list (nothing to check), returns ``"skip"``
        status with a ``None`` score so callers can distinguish "no code to
        verify" from "code passed all checks".
        """
        if files is None:
            files = self._discover_python_files(project_path)

        # ------------------------------------------------------------------
        # Guard: nothing to check → skip (not pass)
        # ------------------------------------------------------------------
        if not files:
            return {
                "overall": "skip",
                "score": None,
                "checks": {},
                "reason": "No files provided — nothing to check. "
                          "This likely means the code generation step "
                          "failed or produced no output.",
            }

        results: dict = {
            "overall": "pass",
            "score": 100,
            "checks": {},
        }

        syntax_issues: list = []
        import_issues: list = []
        style_issues: list = []
        security_issues: list = []

        for file_path in files:
            abs_path = os.path.join(project_path, file_path)

            if not os.path.isfile(abs_path):
                continue

            # --- syntax ---
            sr = self.check_syntax(project_path, file_path)
            syntax_issues.extend(sr.get("issues", []))

            # --- imports ---
            ir = self.check_imports(project_path, file_path)
            import_issues.extend(ir.get("missing", []))

            # --- style ---
            st = self.check_style(project_path, file_path)
            style_issues.extend(st.get("issues", []))

            # --- security ---
            sec = self.check_security(project_path, file_path)
            security_issues.extend(sec.get("issues", []))

        # Aggregate per-check status
        results["checks"]["syntax"] = {
            "status": "pass" if not syntax_issues else "fail",
            "issues": syntax_issues,
        }
        results["checks"]["imports"] = {
            "status": "pass" if not import_issues else "fail",
            "missing": import_issues,
        }
        severity_order = {"high": 0, "medium": 1, "low": 2, "warning": 3, "info": 4}
        worst_style = "pass"
        for issue in style_issues:
            s = issue.get("severity", "info")
            if severity_order.get(s, 5) < severity_order.get(worst_style, 5):
                worst_style = s
        if worst_style in ("high", "medium"):
            worst_style = "warn"
        results["checks"]["style"] = {
            "status": worst_style if style_issues else "pass",
            "issues": style_issues,
        }

        worst_sec = "pass"
        for issue in security_issues:
            sev = issue.get("severity", "low")
            if severity_order.get(sev, 5) < severity_order.get(worst_sec, 5):
                worst_sec = sev
        if worst_sec == "high":
            worst_sec = "fail"
        elif worst_sec in ("medium", "low"):
            worst_sec = "warn"
        results["checks"]["security"] = {
            "status": worst_sec if security_issues else "pass",
            "issues": security_issues,
        }

        # Completeness is task-driven; include a placeholder
        results["checks"]["completeness"] = {
            "status": "skip",
            "criteria_met": 0,
            "criteria_total": 0,
            "note": "call check_completeness() with a task dict",
        }

        # Compute overall
        statuses = [v["status"] for k, v in results["checks"].items() if k != "completeness"]
        if any(s == "fail" for s in statuses):
            results["overall"] = "fail"
        elif any(s in ("warn", "warning") for s in statuses):
            results["overall"] = "warn"
        else:
            results["overall"] = "pass"

        # Score: start at 100, deduct per issue severity
        score = 100
        for issue in syntax_issues:
            score -= 5
        for issue in import_issues:
            score -= 5
        for issue in style_issues:
            if issue.get("severity") == "warning":
                score -= 1
            else:
                score -= 0
        for issue in security_issues:
            sev = issue.get("severity", "low")
            if sev == "high":
                score -= 10
            elif sev == "medium":
                score -= 5
            else:
                score -= 2
        results["score"] = max(0, score)

        try:
            emit_data = {
                "overall": results["overall"],
                "details": {k: v for k, v in results["checks"].items()},
            }
            if task_id is not None:
                emit_data["task_id"] = task_id
            _emit("engine", "quality.result", emit_data)
        except Exception:
            pass

        return results

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_syntax(self, project_path: str, file_path: str) -> dict:
        """Check Python syntax via ``py_compile``.

        Returns ``{"status": "pass"|"fail", "issues": [...]}``.
        """
        abs_path = os.path.join(project_path, file_path)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", abs_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return {"status": "pass", "issues": []}

            # Parse error from stderr
            error_msg = proc.stderr.strip() if proc.stderr else proc.stdout.strip()
            return {
                "status": "fail",
                "issues": [{"message": error_msg, "file": file_path}],
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "fail",
                "issues": [{"message": "Syntax check timed out", "file": file_path}],
            }
        except Exception as exc:
            return {
                "status": "fail",
                "issues": [{"message": str(exc), "file": file_path}],
            }

    def check_imports(self, project_path: str, file_path: str) -> dict:
        """Check that imports resolve (no missing dependencies).

        Extracts imports via ``ast`` and verifies each top-level module is
        importable.  Returns ``{"status": "pass"|"fail", "missing": [...]}``.
        """
        abs_path = os.path.join(project_path, file_path)
        missing: list = []
        try:
            with open(abs_path) as fh:
                tree = ast.parse(fh.read(), filename=file_path)
        except SyntaxError as exc:
            # If syntax is broken we can't parse imports; syntax check catches this
            return {"status": "skip", "missing": []}

        imports = self._extract_imports(tree)

        for module_name in imports:
            top_level = module_name.split(".")[0]
            if self._is_stdlib(top_level):
                continue
            if not self._module_available(top_level):
                missing.append(module_name)

        status = "pass" if not missing else "fail"
        return {"status": status, "missing": missing}

    def check_style(self, project_path: str, file_path: str) -> dict:
        """Basic style checks (line length, naming conventions).

        No external linter required.
        """
        abs_path = os.path.join(project_path, file_path)
        issues: list = []
        try:
            with open(abs_path) as fh:
                lines = fh.readlines()
        except Exception as exc:
            return {"status": "fail", "issues": [{"message": str(exc)}]}

        for i, line in enumerate(lines, 1):
            rstripped = line.rstrip("\n").rstrip("\r").rstrip()

            # Line length (PEP 8: 79 chars)
            if len(rstripped) > 79:
                issues.append({
                    "line": i,
                    "severity": "warning",
                    "message": f"Line too long ({len(rstripped)} > 79)",
                })

            # Trailing whitespace
            if line.rstrip("\n").rstrip("\r") != rstripped:
                issues.append({
                    "line": i,
                    "severity": "info",
                    "message": "Trailing whitespace",
                })

            # Tabs vs spaces
            if "\t" in line:
                issues.append({
                    "line": i,
                    "severity": "warning",
                    "message": "Tab character used",
                })

            # Blank lines check (simplified): more than 2 consecutive blank lines
            # handled below

        # Consecutive blank lines (>2)
        blank_count = 0
        for i, line in enumerate(lines, 1):
            if line.strip() == "":
                blank_count += 1
                if blank_count > 2:
                    issues.append({
                        "line": i,
                        "severity": "info",
                        "message": f"Too many consecutive blank lines ({blank_count})",
                    })
            else:
                blank_count = 0

        status = "pass" if not issues else "warn"
        return {"status": status, "issues": issues}

    def check_security(self, project_path: str, file_path: str) -> dict:
        """Check for security anti-patterns via regex scanning."""
        abs_path = os.path.join(project_path, file_path)
        issues: list = []
        try:
            with open(abs_path) as fh:
                content = fh.read()
        except Exception as exc:
            return {"status": "fail", "issues": [{"message": str(exc)}]}

        for pattern, severity, message in SECURITY_PATTERNS:
            for match in re.finditer(pattern, content):
                # Compute line number from match position
                line_num = content[:match.start()].count("\n") + 1
                issues.append({
                    "pattern": pattern,
                    "severity": severity,
                    "message": message,
                    "match": match.group(),
                    "line": line_num,
                    "file": file_path,
                })

        status = "pass" if not issues else (
            "fail" if any(i["severity"] == "high" for i in issues) else "warn"
        )
        return {"status": status, "issues": issues}

    def check_completeness(self, task: dict, files: dict) -> dict:
        """Check that generated files address all acceptance criteria.

        *task* should contain an ``acceptance_criteria`` list of strings.
        *files* maps file paths to their contents (or can be ``{path: None}``
        to have them read from disk).

        Returns ``{"status": "pass"|"partial"|"fail",
                     "criteria_met": int, "criteria_total": int, "details": [...]}``.
        """
        criteria = task.get("acceptance_criteria", [])
        if not criteria:
            return {
                "status": "skip",
                "criteria_met": 0,
                "criteria_total": 0,
                "note": "No acceptance criteria provided",
                "details": [],
            }

        # Build a corpus of all file contents
        corpus_parts: list = []
        for path, content in files.items():
            if content is None:
                continue
            corpus_parts.append(content)
        corpus = "\n".join(corpus_parts).lower()

        details: list = []
        met = 0
        for criterion in criteria:
            # Simple heuristic: does any keyword from the criterion appear in the corpus
            keywords = [w for w in criterion.lower().split() if len(w) > 3]
            found = any(kw in corpus for kw in keywords)
            details.append({
                "criterion": criterion,
                "met": found,
            })
            if found:
                met += 1

        total = len(criteria)
        if met == total:
            status = "pass"
        elif met > 0:
            status = "partial"
        else:
            status = "fail"

        return {
            "status": status,
            "criteria_met": met,
            "criteria_total": total,
            "details": details,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_python_files(project_path: str) -> list:
        """Return a list of relative ``.py`` paths under *project_path*."""
        py_files: list = []
        for root, _dirs, filenames in os.walk(project_path):
            # Skip hidden dirs and __pycache__
            _dirs[:] = [
                d for d in _dirs
                if not d.startswith(".") and d != "__pycache__"
            ]
            for fname in filenames:
                if fname.endswith(".py"):
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, project_path)
                    py_files.append(rel)
        return sorted(py_files)

    @staticmethod
    def _extract_imports(tree: ast.Module) -> list:
        """Extract all imported module names from an AST."""
        modules: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.append(node.module)
        return modules

    @staticmethod
    def _is_stdlib(module_name: str) -> bool:
        """Best-effort check whether *module_name* is a stdlib module."""
        # Modules added to stdlib up to Python 3.12
        STDLIB = {
            "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
            "asyncore", "atexit", "audioop", "base64", "bdb", "binascii",
            "binhex", "bisect", "builtins", "bz2", "calendar", "cgi", "cgitb",
            "chunk", "cmath", "cmd", "code", "codecs", "codeop", "collections",
            "colorsys", "compileall", "concurrent", "configparser", "contextlib",
            "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
            "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal",
            "difflib", "dis", "distutils", "doctest", "email", "encodings",
            "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
            "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
            "getpass", "gettext", "glob", "grp", "gzip", "hashlib", "heapq",
            "hmac", "html", "http", "idlelib", "imaplib", "imghdr", "imp",
            "importlib", "inspect", "io", "ipaddress", "itertools", "json",
            "keyword", "lib2to3", "linecache", "locale", "logging", "lzma",
            "mailbox", "mailcap", "marshal", "math", "mimetypes", "mmap",
            "modulefinder", "multiprocessing", "netrc", "nis", "nntplib",
            "numbers", "operator", "optparse", "os", "ossaudiodev", "pathlib",
            "pdb", "pickle", "pickletools", "pipes", "pkgutil", "platform",
            "plistlib", "poplib", "posix", "posixpath", "pprint", "profile",
            "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc",
            "queue", "quopri", "random", "re", "readline", "reprlib",
            "resource", "rlcompleter", "runpy", "sched", "secrets", "select",
            "selectors", "shelve", "shlex", "shutil", "signal", "site",
            "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "sqlite3",
            "ssl", "stat", "statistics", "string", "struct", "subprocess",
            "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny",
            "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
            "threading", "time", "timeit", "tkinter", "token", "tokenize",
            "tomllib", "trace", "traceback", "tracemalloc", "tty", "turtle",
            "turtledemo", "types", "typing", "unicodedata", "unittest",
            "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref",
            "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib",
            "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
            "_thread", "__future__",
        }
        return module_name in STDLIB

    @staticmethod
    def _module_available(module_name: str) -> bool:
        """Try to import *module_name* and return True on success."""
        try:
            __import__(module_name)
            return True
        except (ImportError, ModuleNotFoundError):
            return False
        except Exception:
            # Some modules fail to import for reasons other than missing
            return True


# ======================================================================
# CLI
# ======================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quality_gates",
        description="ACE-07 Quality Gates — lint, style, security checks for generated code",
    )
    sub = parser.add_subparsers(dest="command")

    # --- check (all) ---
    check_p = sub.add_parser("check", help="Run all quality checks on a project")
    check_p.add_argument("project", help="Path to project root")
    check_p.add_argument("--file", dest="files", action="append",
                         help="Specific file(s) relative to project (repeatable)")
    check_p.add_argument("--json", dest="as_json", action="store_true",
                         default=True, help="Output JSON (default)")
    check_p.add_argument("--text", dest="as_json", action="store_false",
                         help="Output human-readable text")
    check_p.add_argument("--verbose", action="store_true", help="Verbose output")
    check_p.add_argument("--dry-run", action="store_true",
                         help="Show what would be checked without running")

    # --- syntax ---
    syntax_p = sub.add_parser("syntax", help="Check Python syntax")
    syntax_p.add_argument("project", help="Path to project root")
    syntax_p.add_argument("--file", required=True, help="File relative to project")
    syntax_p.add_argument("--json", dest="as_json", action="store_true", default=True)
    syntax_p.add_argument("--text", dest="as_json", action="store_false")
    syntax_p.add_argument("--verbose", action="store_true")
    syntax_p.add_argument("--dry-run", action="store_true")

    # --- security ---
    sec_p = sub.add_parser("security", help="Run security pattern checks")
    sec_p.add_argument("project", help="Path to project root")
    sec_p.add_argument("--file", required=True, help="File relative to project")
    sec_p.add_argument("--json", dest="as_json", action="store_true", default=True)
    sec_p.add_argument("--text", dest="as_json", action="store_false")
    sec_p.add_argument("--verbose", action="store_true")
    sec_p.add_argument("--dry-run", action="store_true")

    # --- style ---
    style_p = sub.add_parser("style", help="Run style checks")
    style_p.add_argument("project", help="Path to project root")
    style_p.add_argument("--file", required=True, help="File relative to project")
    style_p.add_argument("--json", dest="as_json", action="store_true", default=True)
    style_p.add_argument("--text", dest="as_json", action="store_false")
    style_p.add_argument("--verbose", action="store_true")
    style_p.add_argument("--dry-run", action="store_true")

    # --- imports ---
    imp_p = sub.add_parser("imports", help="Run import checks")
    imp_p.add_argument("project", help="Path to project root")
    imp_p.add_argument("--file", required=True, help="File relative to project")
    imp_p.add_argument("--json", dest="as_json", action="store_true", default=True)
    imp_p.add_argument("--text", dest="as_json", action="store_false")
    imp_p.add_argument("--verbose", action="store_true")
    imp_p.add_argument("--dry-run", action="store_true")

    return parser


def _format_text(results: dict, verbose: bool = False) -> str:
    """Pretty-print results as human-readable text."""
    lines: list = []
    overall = results["overall"].upper()
    score = results.get("score", "?")
    lines.append(f"Overall: {overall}  (score: {score}/100)")
    lines.append("")

    for name, check in results.get("checks", {}).items():
        status = check["status"].upper()
        lines.append(f"  [{status}] {name}")
        if verbose:
            issues = check.get("issues") or check.get("missing")
            if issues:
                for item in issues:
                    if isinstance(item, dict):
                        line_num = item.get("line", "?")
                        msg = item.get("message", item.get("match", str(item)))
                        sev = item.get("severity", "")
                        lines.append(f"         L{line_num}: [{sev}] {msg}")
                    else:
                        lines.append(f"         missing: {item}")

    return "\n".join(lines)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    gates = QualityGates()

    if getattr(args, "dry_run", False):
        print(json.dumps({"dry_run": True, "command": args.command}, indent=2))
        sys.exit(0)

    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        print(f"Error: {project} is not a directory", file=sys.stderr)
        sys.exit(1)

    result: dict

    if args.command == "check":
        result = gates.check_all(project, files=args.files)
    elif args.command == "syntax":
        result = gates.check_syntax(project, args.file)
    elif args.command == "security":
        result = gates.check_security(project, args.file)
    elif args.command == "style":
        result = gates.check_style(project, args.file)
    elif args.command == "imports":
        result = gates.check_imports(project, args.file)
    else:
        parser.print_help()
        sys.exit(1)

    # For single-check commands, wrap into the standard envelope
    if args.command != "check":
        result = {
            "overall": result.get("status", "skip"),
            "checks": {args.command: result},
        }

    if getattr(args, "as_json", True):
        print(json.dumps(result, indent=2))
    else:
        print(_format_text(result, verbose=getattr(args, "verbose", False)))

    # Exit code reflects overall status
    if result["overall"] == "fail":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
