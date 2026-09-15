#!/usr/bin/env python3
"""Test Execution Engine — runs tests on Triton and parses results.

Executes pytest and unittest suites over SSH, parses output into structured
JSON suitable for agent consumption and telemetry recording.

Usage:
    python3 test_runner.py run /path/to/project
    python3 test_runner.py run /path/to/project --pattern "test_auth.py"
    python3 test_runner.py single /path/to/project tests/test_user.py::test_create
    python3 test_runner.py syntax /path/to/project --pattern "*.py"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from transport import get_transport

# Streaming event emission (fire-and-forget)
from streaming_client import emit_event_fire_and_forget as _emit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 60  # seconds per test run


# ---------------------------------------------------------------------------
# Pytest Output Parsing
# ---------------------------------------------------------------------------

# Matches individual test result lines like:
#   tests/test_user.py::test_create_user PASSED
#   tests/test_user.py::TestAuth::test_login PASSED                          [ 50%]
#   tests/test_user.py::test_auth ERROR
_RE_PYTEST_LINE = re.compile(
    r'^(?P<file>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAILED|XPASSED)'
    r'(?:\s+\[\s*\d+%\])?\s*$'
)

# Matches the summary line like:
#   === 2 passed, 1 failed in 0.5s ===
#   === 5 passed, 2 skipped, 1 error in 1.23s ===
_RE_PYTEST_SUMMARY = re.compile(
    r'^=+\s*(.+?)\s+in\s+([\d.]+)s\s*=+\s*$'
)

# Parses individual counts from summary: "2 passed", "1 failed", "3 skipped"
# Stop at comma, newline, or end-of-string — NOT at "in" (as in "skipped in 0.5s")
_RE_COUNT = re.compile(r'(\d+)\s+(passed|failed|error|skipped|xfailed|xpassed|warnings)\b(?=[,\n]|$)')

# Collects traceback blocks that appear after FAILED lines
_RE_TRACEBACK_START = re.compile(r'^(={3,}\s*)?(FAILURES|ERRORS)(\s*={3,})?$')
# Traceback ends at a line of pure dashes/underscores
_RE_TRACEBACK_END = re.compile(r'^[-_]{5,}\s*$')


def _extract_test_name(test_id: str) -> str:
    """Extract the test function/method name from a pytest test id.

    Input:  'tests/test_user.py::TestAuth::test_login'
    Output: 'TestAuth::test_login'

    Input:  'tests/test_user.py::test_create_user'
    Output: 'test_create_user'
    """
    parts = test_id.split("::")
    if len(parts) >= 3:
        return "::".join(parts[1:])  # skip the file path
    return parts[-1] if parts else test_id


def parse_pytest_output(output: str) -> Dict[str, Any]:
    """Parse pytest -v output into structured results.

    Args:
        output: Raw stdout from ``python3 -m pytest -v --tb=short``.

    Returns:
        Structured dict with status counts, duration, and per-test results.
    """
    lines = output.split("\n")

    test_results: List[Dict[str, Any]] = []
    passed = failed = error = skipped = xfailed = xpassed = 0
    duration = 0.0
    summary_text = ""

    # State for traceback collection
    in_traceback = False
    current_traceback: List[str] = []
    traceback_map: Dict[str, str] = {}  # test_id -> traceback text

    for line in lines:
        stripped = line.strip()

        # Detect traceback section boundaries
        if _RE_TRACEBACK_START.match(stripped):
            in_traceback = True
            current_traceback = []
            continue

        if in_traceback and _RE_TRACEBACK_END.match(stripped):
            # End of traceback block — associate with last failed test
            if current_traceback and test_results:
                for tr in reversed(test_results):
                    if tr["status"] in ("failed", "error") and "traceback" not in tr:
                        tr["traceback"] = "\n".join(current_traceback)
                        traceback_map[tr["name"]] = "\n".join(current_traceback)
                        break
            in_traceback = False
            current_traceback = []
            continue

        if in_traceback:
            # Also break out of traceback if we hit the summary line
            if _RE_PYTEST_SUMMARY.match(stripped):
                in_traceback = False
                current_traceback = []
                # Fall through to summary processing below
            else:
                current_traceback.append(line)
                continue

        # Match individual test result lines
        m = _RE_PYTEST_LINE.match(stripped)
        if m:
            test_id = m.group("file")
            status_raw = m.group("status").lower()
            name = _extract_test_name(test_id)
            file_path = test_id.split("::")[0] if "::" in test_id else test_id

            # Normalize status
            status_map = {
                "passed": "passed",
                "failed": "failed",
                "error": "error",
                "skipped": "skipped",
                "xfailed": "skipped",
                "xpassed": "passed",
            }
            status = status_map.get(status_raw, status_raw)

            tr: Dict[str, Any] = {
                "name": name,
                "file": file_path,
                "status": status,
            }

            test_results.append(tr)

            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
            elif status == "error":
                error += 1
            elif status == "skipped":
                skipped += 1
            continue

        # Match summary line
        sm = _RE_PYTEST_SUMMARY.match(stripped)
        if sm:
            summary_text = sm.group(1).strip()
            try:
                duration = float(sm.group(2))
            except ValueError:
                duration = 0.0

            # Parse counts from summary text
            for cm in _RE_COUNT.finditer(summary_text):
                count = int(cm.group(1))
                kind = cm.group(2).lower()
                if kind == "passed":
                    passed = count
                elif kind == "failed":
                    failed = count
                elif kind == "error":
                    error = count
                elif kind in ("skipped", "xfailed"):
                    skipped = count
                elif kind == "xpassed":
                    passed = count

    # Attach error messages for failed/error tests
    for tr in test_results:
        if tr["status"] == "failed":
            tr.setdefault("error", f"Test {tr['name']} failed")
        elif tr["status"] == "error":
            tr.setdefault("error", f"Test {tr['name']} raised an error")

    total = passed + failed + error + skipped

    # Build human-readable summary
    if not summary_text:
        parts = []
        if passed:
            parts.append(f"{passed} passed")
        if failed:
            parts.append(f"{failed} failed")
        if error:
            parts.append(f"{error} error")
        if skipped:
            parts.append(f"{skipped} skipped")
        summary_text = ", ".join(parts) + f" in {duration:.1f}s" if parts else "No tests collected"

    # Determine overall status
    if failed > 0:
        overall_status = "failed"
    elif error > 0:
        overall_status = "error"
    elif total == 0:
        overall_status = "error"
    else:
        overall_status = "passed"

    return {
        "status": overall_status,
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "duration_seconds": duration,
        "test_results": test_results,
        "summary": summary_text,
    }


# ---------------------------------------------------------------------------
# Unittest Output Parsing
# ---------------------------------------------------------------------------

# Matches unittest result lines like:
#   test_create_user (tests.test_user.TestUser) ... ok
#   test_login (tests.test_user.TestAuth) ... FAIL
#   test_bad_input (tests.test_user.TestValidation) ... ERROR
_RE_UNITTEST_LINE = re.compile(
    r'^(test_\w+)\s+\((\S+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skippedR?\s*(?:\(.*\))?|expectedFail|unexpectedSuccess)\s*$'
)

# Matches unittest summary:
#   Ran 10 tests in 0.123s
_RE_UNITTEST_RAN = re.compile(r'^Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s')

# Matches: OK / FAILED (errors=1, failures=2) / OK (1 skipped)
_RE_UNITTEST_VERDICT = re.compile(
    r'^(OK|FAILED|ERROR)\s*(?:\((.+)\))?$'
)


def parse_unittest_output(output: str) -> Dict[str, Any]:
    """Parse unittest output into structured results.

    Args:
        output: Raw stdout from ``python3 -m unittest -v``.

    Returns:
        Structured dict with status counts, duration, and per-test results.
    """
    lines = output.split("\n")

    test_results: List[Dict[str, Any]] = []
    passed = failed = error = skipped = 0
    duration = 0.0

    for line in lines:
        stripped = line.strip()

        m = _RE_UNITTEST_LINE.match(stripped)
        if m:
            test_name = m.group(1)
            test_class = m.group(2)
            result_raw = m.group(3).strip()

            status = "passed"
            tr: Dict[str, Any] = {
                "name": f"{test_class}::{test_name}",
                "file": test_class.rsplit(".", 1)[0] if "." in test_class else test_class,
                "status": status,
            }

            if result_raw == "ok" or result_raw.startswith("expectedFail"):
                status = "passed"
            elif result_raw == "FAIL":
                status = "failed"
                failed += 1
            elif result_raw == "ERROR":
                status = "error"
                error += 1
            elif "skipped" in result_raw.lower():
                status = "skipped"
                skipped += 1
            elif result_raw == "unexpectedSuccess":
                status = "passed"
            else:
                status = "passed"

            if status == "passed":
                passed += 1

            tr["status"] = status
            test_results.append(tr)
            continue

        m_ran = _RE_UNITTEST_RAN.match(stripped)
        if m_ran:
            total_from_run = int(m_ran.group(1))
            try:
                duration = float(m_ran.group(2))
            except ValueError:
                duration = 0.0
            continue

        m_verdict = _RE_UNITTEST_VERDICT.match(stripped)
        if m_verdict:
            verdict = m_verdict.group(1)
            detail = m_verdict.group(2) or ""

            # Parse detail counts: "errors=1, failures=2"
            if detail:
                for part in detail.split(","):
                    part = part.strip()
                    kv = part.split("=", 1)
                    if len(kv) == 2:
                        key = kv[0].strip()
                        val = int(kv[1].strip())
                        if key == "errors":
                            error = val
                        elif key == "failures":
                            failed = val
                        elif key == "skipped":
                            skipped = val
            continue

    total = passed + failed + error + skipped

    # Build summary
    parts = []
    if passed:
        parts.append(f"{passed} passed")
    if failed:
        parts.append(f"{failed} failed")
    if error:
        parts.append(f"{error} error")
    if skipped:
        parts.append(f"{skipped} skipped")
    summary_text = ", ".join(parts) + f" in {duration:.1f}s" if parts else "No tests collected"

    if failed > 0 or error > 0:
        overall_status = "failed"
    elif total == 0:
        overall_status = "error"
    else:
        overall_status = "passed"

    return {
        "status": overall_status,
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "duration_seconds": duration,
        "test_results": test_results,
        "summary": summary_text,
    }


# ---------------------------------------------------------------------------
# TestRunner
# ---------------------------------------------------------------------------


class TestRunner:
    """Runs tests on Triton and parses results into structured dicts.

    All test execution happens over SSH on the Triton machine.  The runner
    supports pytest (preferred) and falls back to unittest discovery when
    pytest is not installed.
    """

    def __init__(self, host: str = None, user: str = None,
                 key_path: str = None, timeout: int = DEFAULT_TIMEOUT,
                 transport=None):
        """Initialise the runner.

        Args:
            host: Deprecated — kept for backward compatibility. Ignored when
                  *transport* is provided.
            user: Deprecated — kept for backward compatibility. Ignored when
                  *transport* is provided.
            key_path: Deprecated — kept for backward compatibility.
            timeout: Default timeout in seconds for commands.
            transport: Optional transport instance (from ``transport.get_transport()``).
                       When *None*, a default transport is obtained automatically.
        """
        self.host = host
        self.user = user
        self.key_path = key_path
        self.timeout = timeout
        self.transport = transport if transport is not None else get_transport()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ssh(self, remote_cmd: str, timeout: int = None) -> Tuple[str, str, int]:
        """Run a command via the transport layer.

        Thin wrapper kept for backward compatibility — callers inside this
        class can continue to use ``self._ssh(...)``.
        """
        return self.transport.run_command(remote_cmd, timeout=timeout or self.timeout)

    def _check_pytest(self) -> bool:
        """Return True if pytest is installed on Triton."""
        stdout, _, rc = self._ssh("python3 -m pytest --version 2>&1")
        return rc == 0 and "pytest" in stdout.lower()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, project_path: str, test_pattern: str = "test_*.py",
            extra_args: str = "") -> Dict[str, Any]:
        """Run all tests matching a pattern in the project directory.

        Convenience alias for :meth:`run_tests`.

        Args:
            project_path: Absolute path to the project on Triton.
            test_pattern: Glob pattern for test files (default: ``test_*.py``).
            extra_args: Additional arguments passed to the test runner.

        Returns:
            Structured result dict.
        """
        return self.run_tests(project_path, test_pattern=test_pattern,
                              extra_args=extra_args)

    def run_tests(self, project_path: str, test_pattern: str = "test_*.py",
                  extra_args: str = "") -> Dict[str, Any]:
        """Run all tests matching a pattern in the project directory.

        Tries pytest first; falls back to unittest discovery if pytest is
        not installed.

        Args:
            project_path: Absolute path to the project on Triton.
            test_pattern: Glob pattern for test files (default: ``test_*.py``).
            extra_args: Additional arguments passed to the test runner.

        Returns:
            Structured result dict.
        """
        start = time.monotonic()

        if self._check_pytest():
            result = self._run_pytest_discovery(project_path, test_pattern, extra_args)
        else:
            result = self._run_unittest_discovery(project_path, test_pattern, extra_args)

        # Ensure duration is set (may already be parsed from output)
        if result.get("duration_seconds", 0) == 0:
            result["duration_seconds"] = round(time.monotonic() - start, 2)

        # Emit test result event
        try:
            _emit("engine", "test.result", {
                "passed": result.get("passed", 0),
                "failed": result.get("failed", 0),
                "total": result.get("total", 0),
                "duration_s": result.get("duration_seconds", 0),
            })
        except Exception:
            pass

        return result

    def run_single_test(self, project_path: str, test_file: str,
                        test_name: str = None) -> Dict[str, Any]:
        """Run a single test file or test function.

        Args:
            project_path: Absolute path to the project on Triton.
            test_file: Path to the test file relative to project root,
                       or a pytest-style ``file::test_name`` identifier.
            test_name: Optional test function/method name.

        Returns:
            Structured result dict.
        """
        start = time.monotonic()

        # Handle pytest-style "file::test_name" notation
        if "::" in test_file and test_name is None:
            parts = test_file.split("::", 1)
            test_file = parts[0]
            test_name = parts[1]

        if self._check_pytest():
            result = self._run_pytest_single(project_path, test_file, test_name)
        else:
            result = self._run_unittest_single(project_path, test_file, test_name)

        if result.get("duration_seconds", 0) == 0:
            result["duration_seconds"] = round(time.monotonic() - start, 2)

        return result

    def run_pytest(self, project_path: str, args: str = "") -> Dict[str, Any]:
        """Run pytest with custom arguments.

        This always uses pytest; it will fail if pytest is not installed.

        Args:
            project_path: Absolute path to the project on Triton.
            args: Extra arguments for pytest (e.g. ``-x -k test_auth``).

        Returns:
            Structured result dict.
        """
        start = time.monotonic()

        if not self._check_pytest():
            return {
                "status": "error",
                "total": 0,
                "passed": 0,
                "failed": 0,
                "error": 0,
                "skipped": 0,
                "duration_seconds": 0,
                "test_results": [],
                "summary": "pytest is not installed on Triton",
            }

        # Build the remote command
        escaped_args = args.replace("'", "'\\''")
        remote_cmd = (
            f"cd '{project_path}' && "
            f"python3 -m pytest {escaped_args} -v --tb=short 2>&1"
        )

        stdout, stderr, rc = self._ssh(remote_cmd, timeout=self.timeout)

        # Combine stdout and stderr — pytest often writes to both
        combined = stdout
        if stderr and stderr not in stdout:
            combined = stdout + "\n" + stderr

        result = parse_pytest_output(combined)
        result["duration_seconds"] = round(time.monotonic() - start, 2)

        # If no tests were collected, mark as error
        if result["total"] == 0:
            result["status"] = "error"
            if not result["summary"] or result["summary"] == "No tests collected":
                result["summary"] = _nonempty_or(combined, "No tests collected")

        return result

    def check_syntax(self, project_path: str, file_pattern: str = "*.py") -> Dict[str, Any]:
        """Check Python syntax without running tests.

        Compiles each matching file with ``py_compile`` on Triton.

        Args:
            project_path: Absolute path to the project on Triton.
            file_pattern: Glob for files to check (default: ``*.py``).

        Returns:
            Dict with per-file syntax results.
        """
        # Find matching files
        remote_cmd = (
            f"cd '{project_path}' && "
            f"find . -name '{file_pattern}' -type f | sort"
        )
        stdout, stderr, rc = self._ssh(remote_cmd, timeout=30)

        if rc != 0 or not stdout.strip():
            return {
                "status": "error",
                "total": 0,
                "passed": 0,
                "failed": 0,
                "files": [],
                "summary": _nonempty_or(stderr, "No matching files found"),
            }

        files = [f.strip() for f in stdout.strip().split("\n") if f.strip()]

        results = []
        passed = 0
        failed = 0

        for filepath in files:
            # py_compile doesn't require the file to be a module; it works on paths
            check_cmd = (
                f"cd '{project_path}' && "
                f"python3 -m py_compile '{filepath}' 2>&1"
            )
            out, err, rc = self._ssh(check_cmd, timeout=15)

            file_result: Dict[str, Any] = {
                "file": filepath,
                "status": "passed" if rc == 0 else "failed",
            }

            if rc == 0:
                passed += 1
            else:
                failed += 1
                error_msg = out.strip() or err.strip()
                file_result["error"] = error_msg

            results.append(file_result)

        total = passed + failed
        status = "passed" if failed == 0 else "failed"

        summary_parts = []
        if passed:
            summary_parts.append(f"{passed} passed")
        if failed:
            summary_parts.append(f"{failed} failed")
        summary_text = ", ".join(summary_parts) + f" ({total} files checked)"

        return {
            "status": status,
            "total": total,
            "passed": passed,
            "failed": failed,
            "files": results,
            "summary": summary_text,
        }

    # ------------------------------------------------------------------
    # Internal execution methods
    # ------------------------------------------------------------------

    def _run_pytest_discovery(self, project_path: str, test_pattern: str,
                              extra_args: str) -> Dict[str, Any]:
        """Run pytest with test discovery on a pattern."""
        escaped_pattern = test_pattern.replace("'", "'\\''")
        escaped_extra = extra_args.replace("'", "'\\''") if extra_args else ""

        remote_cmd = (
            f"cd '{project_path}' && "
            f"python3 -m pytest {escaped_pattern} -v --tb=short {escaped_extra} 2>&1"
        )

        stdout, stderr, rc = self._ssh(remote_cmd, timeout=self.timeout)

        combined = stdout
        if stderr and stderr not in stdout:
            combined = stdout + "\n" + stderr

        result = parse_pytest_output(combined)

        if result["total"] == 0:
            result["status"] = "error"
            result["summary"] = _nonempty_or(combined, "No tests collected")

        return result

    def _run_pytest_single(self, project_path: str, test_file: str,
                           test_name: str = None) -> Dict[str, Any]:
        """Run a single test file or function with pytest."""
        if test_name:
            target = f"{test_file}::{test_name}"
        else:
            target = test_file

        target = target.replace("'", "'\\''")

        remote_cmd = (
            f"cd '{project_path}' && "
            f"python3 -m pytest '{target}' -v --tb=long 2>&1"
        )

        stdout, stderr, rc = self._ssh(remote_cmd, timeout=self.timeout)

        combined = stdout
        if stderr and stderr not in stdout:
            combined = stdout + "\n" + stderr

        result = parse_pytest_output(combined)

        if result["total"] == 0:
            # pytest may not find the test — try to surface why
            result["status"] = "error"
            result["summary"] = _nonempty_or(combined, f"Test not found: {target}")

        return result

    def _run_unittest_discovery(self, project_path: str, test_pattern: str,
                                extra_args: str) -> Dict[str, Any]:
        """Fall back to unittest discovery when pytest is not available."""
        # unittest discover uses a directory, not a glob pattern.
        # Derive the test directory from the pattern, or use current dir.
        test_dir = "."
        escaped_extra = extra_args.replace("'", "'\\''") if extra_args else ""

        remote_cmd = (
            f"cd '{project_path}' && "
            f"python3 -m unittest discover -s '{test_dir}' -p '{test_pattern}' "
            f"-v {escaped_extra} 2>&1"
        )

        stdout, stderr, rc = self._ssh(remote_cmd, timeout=self.timeout)

        combined = stdout
        if stderr and stderr not in stdout:
            combined = stdout + "\n" + stderr

        result = parse_unittest_output(combined)

        if result["total"] == 0:
            result["status"] = "error"
            result["summary"] = _nonempty_or(combined, "No tests discovered (unittest)")

        return result

    def _run_unittest_single(self, project_path: str, test_file: str,
                             test_name: str = None) -> Dict[str, Any]:
        """Run a single test via unittest."""
        # Convert file path to module path: tests/test_user.py -> tests.test_user
        module = test_file.replace("/", ".").replace(".py", "")
        if module.startswith("."):
            module = module[1:]

        if test_name:
            target = f"{module}.{test_name}"
        else:
            target = module

        target = target.replace("'", "'\\''")

        remote_cmd = (
            f"cd '{project_path}' && "
            f"python3 -m unittest '{target}' -v 2>&1"
        )

        stdout, stderr, rc = self._ssh(remote_cmd, timeout=self.timeout)

        combined = stdout
        if stderr and stderr not in stdout:
            combined = stdout + "\n" + stderr

        result = parse_unittest_output(combined)

        if result["total"] == 0:
            result["status"] = "error"
            result["summary"] = _nonempty_or(combined, f"Test not found: {target}")

        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _nonempty_or(text: str, default: str) -> str:
    """Return *text* stripped if non-empty, otherwise *default*."""
    stripped = text.strip()
    return stripped if stripped else default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test_runner",
        description="Test Execution Engine — run tests via transport layer",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"Timeout in seconds per test run (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print JSON output",
    )

    sub = parser.add_subparsers(dest="command", help="Test command")

    # --- run ---
    run_p = sub.add_parser("run", help="Run all tests in a project")
    run_p.add_argument("project_path", help="Absolute path to the project on Triton")
    run_p.add_argument(
        "--pattern", default="test_*.py",
        help="Test file glob pattern (default: test_*.py)",
    )
    run_p.add_argument(
        "--extra", default="",
        help="Extra arguments for the test runner",
    )

    # --- single ---
    single_p = sub.add_parser("single", help="Run a single test file or function")
    single_p.add_argument("project_path", help="Absolute path to the project on Triton")
    single_p.add_argument(
        "test_target",
        help="Test file or pytest-style 'file::test_name' identifier",
    )

    # --- pytest ---
    pytest_p = sub.add_parser("pytest", help="Run pytest with custom arguments")
    pytest_p.add_argument("project_path", help="Absolute path to the project on Triton")
    pytest_p.add_argument(
        "args", nargs="?", default="",
        help="Extra arguments for pytest",
    )

    # --- syntax ---
    syntax_p = sub.add_parser("syntax", help="Check Python syntax without running tests")
    syntax_p.add_argument("project_path", help="Absolute path to the project on Triton")
    syntax_p.add_argument(
        "--pattern", default="*.py",
        help="File glob pattern (default: *.py)",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    runner = TestRunner(timeout=args.timeout)

    if args.command == "run":
        result = runner.run_tests(
            project_path=args.project_path,
            test_pattern=args.pattern,
            extra_args=args.extra,
        )
    elif args.command == "single":
        # Split "file::name" if present
        parts = args.test_target.split("::", 1)
        test_file = parts[0]
        test_name = parts[1] if len(parts) > 1 else None
        result = runner.run_single_test(
            project_path=args.project_path,
            test_file=test_file,
            test_name=test_name,
        )
    elif args.command == "pytest":
        result = runner.run_pytest(
            project_path=args.project_path,
            args=args.args,
        )
    elif args.command == "syntax":
        result = runner.check_syntax(
            project_path=args.project_path,
            file_pattern=args.pattern,
        )
    else:
        parser.print_help()
        sys.exit(1)

    # Output
    indent = 2 if args.pretty else None
    print(json.dumps(result, indent=indent))

    # Exit code: non-zero if tests failed
    if result.get("status") in ("failed", "error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
