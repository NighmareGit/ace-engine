"""D2 tests: contract-stub pre-pass (engine/orchestrator/stubs.py).

scaffold_dag_stubs() creates a stub file for every declared file[] entry
not already present in the workspace. Idempotent. Stub carries task title
+ acceptance criteria as a module docstring. Existing files untouched.
A scaffold failure degrades to a warning, never crashes the run.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import stubs
from engine.orchestrator.stubs import scaffold_dag_stubs, is_stub, STUB_MARKER


def _task(id_, title, files, acceptance_criteria=None):
    """Lightweight Task-like object (matches engine.Task fields used)."""
    from engine import Task
    return Task(
        id=id_, title=title,
        description=title + " description",
        module=files[0] if files else "x.py",
        files=files,
        acceptance_criteria=acceptance_criteria or [],
    )


def test_creates_stub_for_missing_file(tmp_path):
    """A declared file that does not exist gets a stub."""
    task = _task("T01", "Build API", ["api/health.py"])
    created = scaffold_dag_stubs(str(tmp_path), [task])
    assert created == ["api/health.py"]
    full = tmp_path / "api" / "health.py"
    assert full.is_file()
    text = full.read_text(encoding="utf-8")
    assert "Build API" in text
    assert STUB_MARKER in text


def test_stub_carries_acceptance_criteria(tmp_path):
    """The stub docstring carries the task's acceptance criteria."""
    task = _task("T01", "Build API", ["api/health.py"],
                 acceptance_criteria=["Returns 200", "No extra deps"])
    scaffold_dag_stubs(str(tmp_path), [task])
    text = (tmp_path / "api" / "health.py").read_text(encoding="utf-8")
    assert "Returns 200" in text
    assert "No extra deps" in text


def test_idempotent_second_call_creates_nothing(tmp_path):
    """A second call creates nothing (existing stub is left untouched)."""
    task = _task("T01", "Build API", ["api/health.py"])
    created1 = scaffold_dag_stubs(str(tmp_path), [task])
    created2 = scaffold_dag_stubs(str(tmp_path), [task])
    assert created1 == ["api/health.py"]
    assert created2 == []


def test_existing_real_file_untouched(tmp_path):
    """An existing file (even with stub-like content) is NOT overwritten."""
    task = _task("T01", "Build API", ["api/health.py"])
    full = tmp_path / "api"
    full.mkdir()
    (full / "health.py").write_text("# my real code\nx = 1\n", encoding="utf-8")
    created = scaffold_dag_stubs(str(tmp_path), [task])
    assert created == []
    text = (tmp_path / "api" / "health.py").read_text(encoding="utf-8")
    assert text == "# my real code\nx = 1\n"


def test_multiple_tasks_dedup(tmp_path):
    """The same file declared by two tasks gets exactly one stub."""
    t1 = _task("T01", "Build A", ["shared.py"])
    t2 = _task("T02", "Build B", ["shared.py"])
    created = scaffold_dag_stubs(str(tmp_path), [t1, t2])
    assert created == ["shared.py"]


def test_multiple_files_across_tasks(tmp_path):
    """Multiple files across multiple tasks all get stubs."""
    t1 = _task("T01", "Build A", ["api/a.py", "api/b.py"])
    t2 = _task("T02", "Build B", ["api/c.py"])
    created = sorted(scaffold_dag_stubs(str(tmp_path), [t1, t2]))
    assert created == ["api/a.py", "api/b.py", "api/c.py"]


def test_empty_tasks_returns_empty(tmp_path):
    """No tasks -> no stubs."""
    assert scaffold_dag_stubs(str(tmp_path), []) == []


def test_empty_workspace_path_returns_empty():
    """No workspace path -> no stubs."""
    assert scaffold_dag_stubs("", [_task("T01", "x", ["a.py"])]) == []


def test_is_stub_detects_marker():
    """is_stub() returns True for content containing STUB_MARKER."""
    assert is_stub('"""docstring"""\n\n' + STUB_MARKER + '\n') is True
    assert is_stub("# real code\nx = 1\n") is False


def test_is_stub_empty_string():
    """is_stub('') is False."""
    assert is_stub("") is False
