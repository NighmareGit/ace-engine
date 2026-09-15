"""Engine test runner — run pytest via transport and parse results."""

import re
from dataclasses import dataclass


def run_tests(project_path, transport, timeout=180):
    """
    Run pytest via transport, parse results.

    pytest exit code 5 means "no tests collected" — that is NOT a pass.
    It means the generated code has no tests, which is a quality failure.

    Args:
        project_path: str — absolute path to the project directory
        transport: transport object with run_command() method
        timeout: int — timeout in seconds for the pytest command

    Returns:
        TestResult dataclass with passed, tests_passed, tests_failed, failures, raw_output
    """
    stdout, stderr, rc = transport.run_command(
        f"cd {project_path} && python3 -m pytest --tb=short -q",
        timeout=timeout
    )
    if rc == 5:
        # No tests found — treat as failure with actionable feedback
        return TestResult(passed=False, tests_passed=0, tests_failed=0,
                          failures=[{"test": "collection", "error":
                                     "No tests found. Generated code must include tests."}],
                          raw_output=stdout + stderr)

    # Parse pytest output: "X passed, Y failed"
    output = stdout + stderr
    passed_match = re.search(r'(\d+) passed', output)
    failed_match = re.search(r'(\d+) failed', output)
    errors_match = re.search(r'(\d+) error', output)

    tests_passed = int(passed_match.group(1)) if passed_match else 0
    tests_failed = int(failed_match.group(1)) if failed_match else 0
    if errors_match:
        tests_failed += int(errors_match.group(1))

    # Parse failure details — look for FAILED lines
    failures = []
    for line in output.split("\n"):
        if "FAILED" in line:
            # Format: "tests/test_foo.py::test_bar FAILED" or "FAILED tests/test_foo.py::test_bar"
            parts = line.strip().split()
            test_name = parts[1] if len(parts) > 1 else parts[0]
            failures.append({"test": test_name, "error": line.strip()})

    # rc != 0 and rc != 5 means actual test failures
    passed = (rc == 0 and tests_failed == 0)

    return TestResult(
        passed=passed,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        failures=failures,
        raw_output=output,
    )


@dataclass
class TestResult:
    """Result of running pytest on the project."""
    passed: bool
    tests_passed: int
    tests_failed: int
    failures: list[dict]  # [{test: "...", error: "..."}]
    raw_output: str
