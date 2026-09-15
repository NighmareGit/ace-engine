"""D3 tests: targeting rail (prompts.py files_to_create + validator stage 5).

1. prompts.py build_prd_result: files_to_create = list(task.files) (not just
   [task.module]).
2. prompt contains the 'Create EXACTLY these files' line with all files.
3. validator targeting stage: flags missing files and untouched stubs in the
   generated output; passes when all declared files are present and real.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task
from engine.prompts import build_prd_result
from engine.validator import validate, check_targeting


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _task(id_, title, module, files=None, acceptance_criteria=None):
    return Task(
        id=id_, title=title,
        description=title + " description",
        module=module,
        files=files or [],
        acceptance_criteria=acceptance_criteria or [],
    )


class _Ctx:
    """Minimal context stand-in carrying what build_prd_result / validate need."""
    def __init__(self, imports=None, file_tree=None, project_path=None):
        self.imports = imports or {}
        self.file_tree = file_tree or {"root": project_path or "/tmp"}
        self.project_path = project_path
        self.framework = "unknown"
        self.relevant_files = {}
        self.existing_code = {}


class _Code:
    """Minimal GeneratedCode stand-in."""
    def __init__(self, files):
        self.files = files


# ---------------------------------------------------------------------------
# prompts.py: files_to_create
# ---------------------------------------------------------------------------

def test_files_to_create_uses_task_files():
    """files_to_create must list ALL of task.files, not just [task.module]."""
    task = _task("T01", "Build", "api/health.py",
                 files=["api/health.py", "api/routes.py"])
    ctx = _Ctx()
    prd = build_prd_result(task, ctx)
    assert prd["tasks"][0]["files_to_create"] == ["api/health.py", "api/routes.py"]


def test_files_to_create_falls_back_to_module_when_files_empty():
    """When task.files is empty, fall back to [task.module]."""
    task = _task("T01", "Build", "api/health.py")
    ctx = _Ctx()
    prd = build_prd_result(task, ctx)
    assert prd["tasks"][0]["files_to_create"] == ["api/health.py"]


# ---------------------------------------------------------------------------
# prompt template: 'Create EXACTLY these files'
# ---------------------------------------------------------------------------

def test_prompt_contains_create_exactly_line():
    """The compiled prompt must contain the 'Create EXACTLY these files' line
    listing every declared file."""
    from engine.prompts import build_project_context, compile_prompt
    task = _task("T01", "Build", "api/health.py",
                 files=["api/health.py", "api/routes.py"])
    ctx = _Ctx()
    prd = build_prd_result(task, ctx)
    pc = build_project_context(ctx)
    prompt = compile_prompt("T01", prd, pc)
    assert "Create EXACTLY these files" in prompt
    assert "api/health.py" in prompt
    assert "api/routes.py" in prompt


# ---------------------------------------------------------------------------
# validator: targeting stage (checks generated code, not workspace)
# ---------------------------------------------------------------------------

def test_targeting_passes_when_all_files_present_and_real():
    """When generated code covers all declared files with real content,
    targeting passes."""
    task = _task("T01", "Build", "api/health.py",
                 files=["api/health.py", "api/routes.py"])
    ctx = _Ctx()
    code = _Code({
        "api/health.py": "def health(): return 'ok'\n",
        "api/routes.py": "def routes(): return []\n",
    })
    result = validate(code, ctx, task=task)
    targeting = [s for s in result.stages if s["stage"] == "targeting"]
    assert len(targeting) == 1
    assert targeting[0]["passed"] is True


def test_targeting_flags_missing_file():
    """A declared file not present in the generated output fails targeting."""
    task = _task("T01", "Build", "api/health.py",
                 files=["api/health.py", "api/missing.py"])
    ctx = _Ctx()
    code = _Code({"api/health.py": "x = 1\n"})
    result = validate(code, ctx, task=task)
    targeting = [s for s in result.stages if s["stage"] == "targeting"]
    assert targeting[0]["passed"] is False
    assert "missing" in targeting[0]["error"].lower()
    assert "api/missing.py" in targeting[0]["error"]


def test_targeting_flags_untouched_stub():
    """A declared file whose generated content is still a D2 stub fails."""
    from engine.orchestrator.stubs import STUB_MARKER
    task = _task("T01", "Build", "api/health.py", files=["api/health.py"])
    ctx = _Ctx()
    code = _Code({"api/health.py": '"""Stub."""\n\n' + STUB_MARKER + '\n'})
    result = validate(code, ctx, task=task)
    targeting = [s for s in result.stages if s["stage"] == "targeting"]
    assert targeting[0]["passed"] is False
    assert "stub" in targeting[0]["error"].lower()
    assert "api/health.py" in targeting[0]["error"]


def test_targeting_no_task_skips_stage():
    """When task=None (backward compat), the targeting stage is skipped."""
    ctx = _Ctx()
    code = _Code({"x.py": "x = 1\n"})
    result = validate(code, ctx, task=None)
    targeting = [s for s in result.stages if s["stage"] == "targeting"]
    assert targeting == []


def test_targeting_falls_back_to_module():
    """When task.files is empty, targeting falls back to [task.module]."""
    task = _task("T01", "Build", "app.py")
    ctx = _Ctx()
    # app.py NOT in generated code -> missing
    code = _Code({"other.py": "x = 1\n"})
    result = validate(code, ctx, task=task)
    targeting = [s for s in result.stages if s["stage"] == "targeting"]
    assert targeting[0]["passed"] is False
    assert "app.py" in targeting[0]["error"]


def test_check_targeting_direct_missing():
    """check_targeting directly: missing file reported."""
    task = _task("T01", "Build", "a.py", files=["a.py", "b.py"])
    ctx = _Ctx()
    code = _Code({"a.py": "x = 1\n"})
    passed, err = check_targeting(task, ctx, code=code)
    assert passed is False
    assert "missing" in err
    assert "b.py" in err


def test_check_targeting_direct_stub():
    """check_targeting directly: stub content reported."""
    from engine.orchestrator.stubs import STUB_MARKER
    task = _task("T01", "Build", "a.py", files=["a.py"])
    ctx = _Ctx()
    code = _Code({"a.py": '"""Stub."""\n' + STUB_MARKER + '\n'})
    passed, err = check_targeting(task, ctx, code=code)
    assert passed is False
    assert "stub" in err
