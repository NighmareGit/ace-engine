"""Unit tests for engine/generator.py, validator.py, and tester.py.

Mock transport only — no GPU, no inference, no network calls.
"""

import sys
import os
import ast
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task, EngineConfig
from engine.generator import GeneratedCode, ErrorContext
from engine.validator import ValidationResult
from engine.tester import TestResult


# ---------------------------------------------------------------------------
# TEST GROUP A — Generator (token capture from flat response)
# ---------------------------------------------------------------------------


def test_generate_code_captures_total_tokens_from_flat_response():
    """Transport returns flat dict with total_tokens at top level (C1 FIX)."""
    from engine.generator import generate_code
    mock_transport = MagicMock()
    mock_transport.curl_beellama.return_value = {
        "content": "```python\nprint('hello')\n```",
        "reasoning_content": "",
        "predicted_per_second": 42.5,
        "prompt_per_second": 100.0,
        "predicted_ms": 1500.0,
        "prompt_ms": 200.0,
        "predicted_n": 600,
        "thinking_tokens": 0,
        "total_tokens": 850,  # FLAT, not nested under "usage"
    }
    task = Task(id="T01", title="Test", description="test", module="app.py")
    context = MagicMock()
    context.file_tree = {"root": "/tmp/project"}
    context.relevant_files = []
    context.imports = {}
    context.framework = None
    config = EngineConfig()
    result = generate_code(context, task, config, mock_transport)
    assert result.tokens == 850, f"expected 850, got {result.tokens}"
    assert result.raw_response != ""
    assert "app.py" in result.files


def test_generate_code_zero_tokens_when_transport_returns_no_tokens():
    """When transport response has no total_tokens key, tokens defaults to 0."""
    from engine.generator import generate_code
    mock_transport = MagicMock()
    mock_transport.curl_beellama.return_value = {
        "content": "```python\nx = 1\n```",
        "reasoning_content": "",
    }
    task = Task(id="T01", title="Test", description="test", module="mod.py")
    context = MagicMock()
    context.file_tree = {"root": "/tmp"}
    context.relevant_files = []
    context.imports = {}
    context.framework = None
    result = generate_code(context, task, EngineConfig(), mock_transport)
    assert result.tokens == 0


def test_generate_code_extracts_code_from_markdown_fences():
    """Code blocks inside ``` fences are extracted into files dict."""
    from engine.generator import generate_code
    mock_transport = MagicMock()
    mock_transport.curl_beellama.return_value = {
        "content": "# app.py\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```",
        "reasoning_content": "",
        "total_tokens": 100,
        "thinking_tokens": 0,
        "predicted_per_second": 50.0,
    }
    task = Task(id="T01", title="Test", description="test", module="app.py")
    context = MagicMock()
    context.file_tree = {"root": "/tmp"}
    context.relevant_files = []
    context.imports = {}
    context.framework = None
    result = generate_code(context, task, EngineConfig(), mock_transport)
    assert len(result.files) >= 1
    assert "FastAPI" in list(result.files.values())[0]


# ---------------------------------------------------------------------------
# TEST GROUP B — Validator (4 stages)
# ---------------------------------------------------------------------------


def test_check_ast_passes_valid_python():
    from engine.validator import check_ast
    passed, error = check_ast("x = 1\nprint(x)")
    assert passed is True
    assert error is None


def test_check_ast_fails_on_syntax_error():
    from engine.validator import check_ast
    passed, error = check_ast("def foo(")
    assert passed is False
    assert error is not None


def test_check_syntax_passes_valid_code():
    from engine.validator import check_syntax
    passed, error = check_syntax("x = 42")
    assert passed is True


def test_check_syntax_fails_on_invalid_code():
    from engine.validator import check_syntax
    passed, error = check_syntax("if if if")
    assert passed is False


def test_check_imports_rejects_unknown_module():
    """Import of a non-stdlib, non-project module fails validation."""
    from engine.validator import check_imports
    code = "import nonexistent_fake_module_12345\n"
    passed, error = check_imports(code, {})
    assert passed is False
    assert "nonexistent_fake_module_12345" in error


def test_check_imports_allows_stdlib():
    from engine.validator import check_imports
    code = "import os\nimport json\nimport sys\n"
    passed, error = check_imports(code, {})
    assert passed is True


def test_check_imports_allows_project_module():
    from engine.validator import check_imports
    code = "from myproject import helper\n"
    passed, error = check_imports(code, {"myproject": ["helper"]})
    assert passed is True


def test_check_execution_blocks_import_in_restricted_namespace():
    """Restricted namespace (empty __builtins__) blocks __import__ calls."""
    from engine.validator import check_execution
    namespace = {"__name__": "__test__", "__builtins__": {}}
    code = "__import__('os')"
    passed, error = check_execution(code, namespace)
    assert passed is False
    assert "import" in error.lower() or "name" in error.lower() or "builtins" in error.lower()


def test_check_execution_allows_benign_code():
    from engine.validator import check_execution
    namespace = {"__name__": "__test__", "__builtins__": {}}
    code = "x = 1 + 2"
    passed, error = check_execution(code, namespace)
    assert passed is True


def test_validate_returns_ValidationResult_with_all_stages():
    """validate() runs all 4 stages and returns a ValidationResult."""
    from engine.validator import validate
    from engine.generator import GeneratedCode
    code = GeneratedCode(
        files={"app.py": "x = 1"},
        raw_response="x = 1",
        tokens=10,
        thinking_tokens=0,
        latency_ms=100.0,
    )
    context = MagicMock()
    context.imports = {}
    result = validate(code, context)
    assert isinstance(result, ValidationResult)
    assert result.passed is True
    stage_names = [s["stage"] for s in result.stages]
    assert "ast" in stage_names
    assert "syntax" in stage_names
    assert "imports" in stage_names
    assert "execution" in stage_names


# ---------------------------------------------------------------------------
# TEST GROUP C — Tester (exit-5 handling)
# ---------------------------------------------------------------------------


def test_run_tests_exit_code_5_is_failure():
    """pytest exit code 5 (no tests collected) must be treated as failure."""
    from engine.tester import run_tests
    mock_transport = MagicMock()
    mock_transport.run_command.return_value = ("no tests ran", "", 5)
    result = run_tests("/tmp/project", mock_transport, timeout=10)
    assert result.passed is False
    assert result.tests_passed == 0
    assert result.tests_failed == 0
    assert len(result.failures) > 0
    assert "No tests found" in result.failures[0]["error"]


def test_run_tests_success():
    """pytest exit code 0 with passing output is a success."""
    from engine.tester import run_tests
    mock_transport = MagicMock()
    mock_transport.run_command.return_value = ("3 passed in 0.5s", "", 0)
    result = run_tests("/tmp/project", mock_transport, timeout=10)
    assert result.passed is True
    assert result.tests_passed == 3


def test_run_tests_failure_with_failures():
    """pytest exit code 1 with failing output is a failure."""
    from engine.tester import run_tests
    mock_transport = MagicMock()
    mock_transport.run_command.return_value = (
        "FAILED test_app.py::test_health - assert 500 == 200\n1 failed in 0.3s",
        "",
        1,
    )
    result = run_tests("/tmp/project", mock_transport, timeout=10)
    assert result.passed is False
    assert result.tests_failed >= 1
