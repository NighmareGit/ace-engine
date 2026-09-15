"""D1 tests: 3-tier import classifier (validator.check_imports).

Classification tiers (checked in order):
  1. workspace — module exists in project_modules -> PASS.
  2. dag-sibling — module maps to a file declared in any task's files[] of the
     current DAG -> PASS with directive message containing 'sibling' and
     'Do NOT reimplement'.
  3. unknown — today's stdlib rule, message byte-identical to the old one.

When dag_files=None the behavior is identical to today.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.validator import check_imports


# ---------------------------------------------------------------------------
# Tier 1: workspace pass (unchanged behavior)
# ---------------------------------------------------------------------------

def test_workspace_pass_stdlib():
    """Stdlib imports pass regardless of dag_files."""
    passed, err = check_imports("import os\nimport sys\n", {})
    assert passed is True
    assert err is None


def test_workspace_pass_project_module():
    """A project module import passes."""
    passed, err = check_imports(
        "from myproject.helper import h\n", {"myproject.helper": ["h"]})
    assert passed is True
    assert err is None


# ---------------------------------------------------------------------------
# Tier 2: dag-sibling pass
# ---------------------------------------------------------------------------

def test_dag_sibling_pass_with_directive_message():
    """Importing a module that maps to a declared DAG file passes with the
    sibling directive message."""
    dag_files = {"engine/workflows/ralph/config.py", "api/health.py"}
    passed, err = check_imports(
        "from engine.workflows.ralph.config import SETTINGS\n",
        {},  # not in project_modules (not yet on disk)
        dag_files=dag_files,
    )
    assert passed is True
    assert err is not None
    assert "sibling" in err.lower(), f"expected 'sibling' in message, got: {err}"
    assert "Do NOT reimplement" in err, f"expected 'Do NOT reimplement' in message, got: {err}"


def test_dag_sibling_message_names_the_path():
    """The sibling message names the declared DAG path."""
    dag_files = {"engine/workflows/ralph/config.py"}
    passed, err = check_imports(
        "from engine.workflows.ralph.config import X\n",
        {},
        dag_files=dag_files,
    )
    assert passed is True
    assert "engine/workflows/ralph/config.py" in err


def test_dag_sibling_top_level_import():
    """Top-level `import engine.workflows.ralph.config` also resolves."""
    dag_files = {"engine/workflows/ralph/config.py"}
    passed, err = check_imports(
        "import engine.workflows.ralph.config\n",
        {},
        dag_files=dag_files,
    )
    assert passed is True
    assert "sibling" in err.lower()


def test_dag_sibling_path_to_module_conversion():
    """Paths with hyphens/slashes map to dotted module names correctly."""
    dag_files = {"my_pkg/sub_module.py"}
    passed, err = check_imports(
        "from my_pkg.sub_module import thing\n",
        {},
        dag_files=dag_files,
    )
    assert passed is True
    assert "sibling" in err.lower()


# ---------------------------------------------------------------------------
# Tier 3: unknown (byte-identical to old message)
# ---------------------------------------------------------------------------

def test_unknown_message_unchanged():
    """An unknown import gets the original stdlib message — byte-identical."""
    code = "import nonexistent_fake_module_12345\n"
    passed, err = check_imports(code, {})
    assert passed is False
    assert err == (
        "Import 'nonexistent_fake_module_12345' is not available. "
        "Do NOT use third-party packages — reimplement the needed "
        "functionality with the Python standard library only "
        "(e.g. http.server instead of flask)."
    )


def test_unknown_message_unchanged_even_with_dag_files():
    """Unknown import still fails even when dag_files is provided."""
    passed, err = check_imports(
        "import nonexistent_fake_module_12345\n",
        {},
        dag_files={"api/health.py"},
    )
    assert passed is False
    assert "nonexistent_fake_module_12345" in err


# ---------------------------------------------------------------------------
# dag_files=None -> identical to today
# ---------------------------------------------------------------------------

def test_dag_files_none_behaves_like_today_unknown():
    """dag_files=None: unknown import fails exactly as before."""
    code = "import nonexistent_fake_module_12345\n"
    passed, err = check_imports(code, {}, dag_files=None)
    assert passed is False
    assert err == (
        "Import 'nonexistent_fake_module_12345' is not available. "
        "Do NOT use third-party packages — reimplement the needed "
        "functionality with the Python standard library only "
        "(e.g. http.server instead of flask)."
    )


def test_dag_files_none_behaves_like_today_stdlib():
    """dag_files=None: stdlib import passes."""
    passed, err = check_imports("import os\n", {}, dag_files=None)
    assert passed is True


# ---------------------------------------------------------------------------
# Tier ordering: workspace wins over sibling
# ---------------------------------------------------------------------------

def test_workspace_wins_over_sibling():
    """If a module is BOTH a project module AND a dag-sibling, workspace wins
    (no directive message needed — it's already importable)."""
    dag_files = {"api/health.py"}
    passed, err = check_imports(
        "from api.health import bp\n",
        {"api.health": ["bp"]},  # on disk
        dag_files=dag_files,
    )
    assert passed is True
    assert err is None  # clean pass, no message
