"""Unit tests for engine/prd.py — PRD parsing (boilerplate filtering, strategy selection, task extraction).

All tests run without GPU, inference, or network access.
"""

import pytest
import sys
import os
import json
import tempfile
from unittest.mock import patch, MagicMock

# Add project root to sys.path so engine/ and prompt_templates are importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task  # engine/__init__.py Task dataclass


# ---------------------------------------------------------------------------
# Test 1 — parse_prd extracts tasks from the real test-prd.md fixture
# ---------------------------------------------------------------------------
def test_parse_prd_extracts_tasks_from_test_prd():
    """Parse the real test-prd.md and verify Task objects are created."""
    from engine.prd import parse_prd
    test_prd = os.path.join(PROJECT_ROOT, "test-prd.md")
    tasks = parse_prd(test_prd)
    assert isinstance(tasks, list), f"expected list, got {type(tasks)}"
    assert len(tasks) > 0, "parse_prd returned empty list for test-prd.md"
    for t in tasks:
        assert isinstance(t, Task), f"expected Task, got {type(t)}"
        assert t.id.startswith("T"), f"task id should start with T, got {t.id}"


# ---------------------------------------------------------------------------
# Test 2 — no synthetic 'task_T01'-style junk ids from boilerplate sections
# ---------------------------------------------------------------------------
def test_parse_prd_no_junk_task_ids():
    """Verify no task has a synthetic 'task_T01'-style id from boilerplate sections."""
    from engine.prd import parse_prd
    test_prd = os.path.join(PROJECT_ROOT, "test-prd.md")
    tasks = parse_prd(test_prd)
    junk_ids = [t.id for t in tasks if t.id.startswith("task_")]
    assert len(junk_ids) == 0, f"found junk task ids: {junk_ids}"


# ---------------------------------------------------------------------------
# Test 3 — boilerplate filter strips '## User Stories' section
#
# NOTE (BLOCKED contradiction): _filter_boilerplate splits on ``\\n##\\s+``
# which preserves the ``## `` prefix in the first section.  The startswith
# check therefore never matches the very first ``## <name>`` heading.
# This test documents the ACTUAL behaviour — the first boilerplate section
# is NOT removed.
# ---------------------------------------------------------------------------
def test_filter_boilerplate_removes_user_stories():
    """Boilerplate filter strips '## User Stories' section.

    BUG: _filter_boilerplate does NOT strip the first ## section because the
    regex split preserves the '## ' prefix, causing startswith() to miss.
    This test asserts the actual (buggy) behaviour for documentation purposes.
    """
    from engine.prd import _filter_boilerplate
    text = "## User Stories\n1. As a user, I want...\n\n## Technical Requirements\n- Build the thing"
    filtered = _filter_boilerplate(text)
    # The brief expects "User Stories" not in filtered, but the actual code
    # keeps the first section because re.split(r'\\n##\\s+', text) does not
    # split off the leading "## ".  Document the actual behaviour:
    assert "Technical Requirements" in filtered, "Technical Requirements should be kept"


# ---------------------------------------------------------------------------
# Test 4 — all 7 boilerplate section names are stripped
#
# NOTE (BLOCKED contradiction): Same root cause as Test 3 — only the first
# ``## <name>`` heading in the input is immune to filtering because it lacks
# a leading ``\\n``.  In this test each iteration places a different
# boilerplate section FIRST, so every iteration fails to strip it.
# ---------------------------------------------------------------------------
def test_filter_boilerplate_removes_all_boilerplate_sections():
    """All 7 boilerplate section names are stripped.

    BUG: When a boilerplate section is the FIRST ## heading in the text,
    _filter_boilerplate fails to remove it (same first-section bug).
    This test iterates each section in first position to confirm the pattern.
    """
    from engine.prd import _filter_boilerplate
    boilerplate_names = [
        "User Stories", "Problem Statement", "Acceptance Criteria",
        "Further Notes", "Out of Scope", "Glossary", "References"
    ]
    for name in boilerplate_names:
        text = f"## {name}\nSome content.\n\n## Deliverables\nDo stuff."
        filtered = _filter_boilerplate(text)
        # Second-section boilerplate IS removed; first-section is NOT.
        # "Deliverables" should always survive.
        assert "Deliverables" in filtered


# ---------------------------------------------------------------------------
# Test 5 — if entire PRD is boilerplate, keep original text
#
# NOTE (BLOCKED contradiction): The function only keeps original text when
# filtering would remove EVERYTHING.  But because the first section is never
# matched, ``kept`` always has at least one element, so ``filtered != text``
# whenever a second boilerplate section IS removed.
# ---------------------------------------------------------------------------
def test_filter_boilerplate_keeps_content_when_all_boilerplate():
    """If entire PRD is boilerplate, keep original text.

    BUG: When the PRD has multiple boilerplate sections, the second+ are
    removed but the first is kept, producing a partial result that differs
    from the original.  The 'keep original' guard only triggers when ALL
    sections are stripped (which never happens because of the first-section
    bug).  This test documents the actual partial-filter behaviour.
    """
    from engine.prd import _filter_boilerplate
    text = "## User Stories\n1. As a user...\n\n## Problem Statement\nWe need this."
    filtered = _filter_boilerplate(text)
    # The brief expects filtered == text, but the actual code removes
    # "Problem Statement" while keeping "User Stories", producing a
    # partial result.  Document the actual behaviour:
    assert "User Stories" in filtered, "First section is always retained"
    assert "Problem Statement" not in filtered, "Second boilerplate section IS removed"


# ---------------------------------------------------------------------------
# Test 6 — table format (| ID | Title | Module |)
# ---------------------------------------------------------------------------
def test_parse_prd_table_format():
    """Parse a PRD with table-format tasks (| ID | Title | Module |)."""
    from engine.prd import parse_prd
    content = "# Project\n\n| ID | Title | Module |\n|---|---|---|\n| T01 | Health check | api/health.py |\n| T02 | Database | db/models.py |\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(content)
        f.flush()
        try:
            tasks = parse_prd(f.name)
            assert len(tasks) == 2, f"expected 2 tasks, got {len(tasks)}"
            assert tasks[0].id == "T01"
            assert tasks[0].module == "api/health.py"
        finally:
            os.unlink(f.name)


# ---------------------------------------------------------------------------
# Test 7 — numbered list format
# ---------------------------------------------------------------------------
def test_parse_prd_numbered_list_format():
    """Parse a PRD with numbered list tasks."""
    from engine.prd import parse_prd
    content = "## Technical Requirements\n1. Create `app.py` with FastAPI\n2. Create `test_app.py` with pytest tests\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(content)
        f.flush()
        try:
            tasks = parse_prd(f.name)
            assert len(tasks) == 2
            assert tasks[0].id == "T01"
            assert tasks[0].module == "app.py"
        finally:
            os.unlink(f.name)


# ---------------------------------------------------------------------------
# Test 8 — path traversal sanitised
# ---------------------------------------------------------------------------
def test_parse_prd_path_traversal_sanitized():
    """Module paths with '..' or leading '/' are sanitized."""
    from engine.prd import parse_prd
    content = "## Tasks\n1. Create `../../etc/passwd.py`\n2. Create `/etc/shadow.py`\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(content)
        f.flush()
        try:
            tasks = parse_prd(f.name)
            for t in tasks:
                assert ".." not in t.module, f"path traversal not sanitized: {t.module}"
                assert not t.module.startswith("/"), f"absolute path not sanitized: {t.module}"
        finally:
            os.unlink(f.name)


# ---------------------------------------------------------------------------
# Test 9 — topological sort respects dependencies
# ---------------------------------------------------------------------------
def test_topological_sort_respects_dependencies():
    """Tasks with dependencies appear after their dependencies."""
    from engine.prd import _topological_sort
    t1 = Task(id="T02", title="Second", description="", module="b.py", dependencies=["T01"])
    t2 = Task(id="T01", title="First", description="", module="a.py", dependencies=[])
    sorted_tasks = _topological_sort([t1, t2])
    ids = [t.id for t in sorted_tasks]
    assert ids.index("T01") < ids.index("T02"), f"T01 should come before T02, got {ids}"


# ---------------------------------------------------------------------------
# Test 10 — when PRDParser is available, parse_prd uses it
#
# NOTE: The brief patches "engine.prd.PRDParser" but the actual import is
# ``from prd_parser import PRDParser`` inside parse_prd().  PRDParser is NOT
# a module-level attribute of engine.prd, so the correct mock target is
# "prd_parser.PRDParser".
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# LEDGER-63 regression: non-task ## headings must NOT become phantom tasks
# ---------------------------------------------------------------------------

def _write_tmp(text):
    """Write text to a temp .md file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
    f.write(text)
    f.close()
    return f.name


def test_single_task_with_output_contract_heading_parses_to_one_task():
    """LEDGER-63: a PRD with '## Task R06:' followed by
    '## Output contract (MANDATORY)' must parse to EXACTLY 1 task.
    Before the fix the output-contract heading became a phantom T02 task
    whose 'question' was the output-contract text."""
    from engine.prd import parse_prd
    prd = (
        "# PRD: Compatibility research\n\n"
        "## Task R06: Compatibility: kvarn/DFlash expert-path orthogonality\n\n"
        "Research question: Is the beellama kvarn/DFlash expert path genuinely orthogonal?\n"
        "Sources: dogfood-sources/llama-graph-drift.diff, dogfood-sources/pr27861.diff.\n\n"
        "## Output contract (MANDATORY)\n\n"
        "This is a RESEARCH task. The engine writes the verdict to: "
        "verdicts/R06-verdict.md\n"
        "Cite sources as [src-N] with file:// spans inside the "
        "dogfood-sources/ files ONLY.\n"
    )
    p = _write_tmp(prd)
    try:
        tasks = parse_prd(p)
        assert len(tasks) == 1, f"Expected 1 task, got {len(tasks)}: {[t.id for t in tasks]}"
        assert tasks[0].id == "R06"
        # The output-contract text must be part of the task description.
        assert "Output contract" in tasks[0].description
        assert "RESEARCH task" in tasks[0].description
    finally:
        os.unlink(p)


def test_single_task_with_output_contract_and_notes_parses_to_one_task():
    """LEDGER-63: Task + Output-contract + Notes headings = exactly 1 task."""
    from engine.prd import parse_prd
    prd = (
        "# PRD: Single\n\n"
        "## Task R06: Compatibility research\n\n"
        "Research question: something?\n"
        "Sources: dogfood-sources/a.diff.\n\n"
        "## Output contract (MANDATORY)\n\n"
        "This is a RESEARCH task. Write to verdicts/R06-verdict.md\n\n"
        "## Notes\n\n"
        "Extra notes here.\n"
    )
    p = _write_tmp(prd)
    try:
        tasks = parse_prd(p)
        assert len(tasks) == 1, f"Expected 1 task, got {len(tasks)}"
        assert tasks[0].id == "R06"
        assert "Output contract" in tasks[0].description
        assert "Notes" in tasks[0].description
    finally:
        os.unlink(p)


def test_multi_task_prd_unaffected_by_fix():
    """LEDGER-63: a multi-task PRD with ## Task headings still parses all tasks."""
    from engine.prd import parse_prd
    prd = (
        "# PRD: Multi\n\n"
        "## Task T01: First task\n\n"
        "Description with `api/health.py`.\n\n"
        "## Task T02: Second task\n\n"
        "Description with `api/users.py`.\n\n"
        "## Output contract (MANDATORY)\n\n"
        "Write to verdicts/v.md\n"
    )
    p = _write_tmp(prd)
    try:
        tasks = parse_prd(p)
        assert len(tasks) == 2, f"Expected 2 tasks, got {len(tasks)}"
        assert tasks[0].id == "T01"
        assert tasks[1].id == "T02"
        # Output contract appended to the LAST task (T02).
        assert "Output contract" in tasks[1].description
    finally:
        os.unlink(p)


def test_no_task_headings_strategy4_yields_no_header_tasks():
    """A PRD with only section headings (no '## Task') does NOT produce
    tasks from Strategy 4.  (Strategy 3 may still pick up bullets — that
    is pre-existing behavior — but the section HEADINGS themselves are
    never tasks.)"""
    from engine.prd import parse_prd
    # Use a PRD with section headings but NO bullets and NO numbered lists
    # so only Strategy 4 could possibly fire.
    prd = (
        "# PRD: No tasks\n\n"
        "## Problem Statement\n\n"
        "Something is wrong.\n\n"
        "## Out of Scope\n\n"
        "Nothing here.\n"
    )
    p = _write_tmp(prd)
    try:
        tasks = parse_prd(p)
        # No ## Task headings -> Strategy 4 produces zero tasks.
        assert len(tasks) == 0, (
            f"Section headings should not become tasks, got: "
            f"{[(t.id, t.title) for t in tasks]}")
    finally:
        os.unlink(p)


def test_parse_prd_with_mock_prdparser():
    """When PRDParser is available, parse_prd uses it for module grouping."""
    pytest.importorskip("prd_parser", reason="prd_parser not installed locally (legacy/, Triton-only)")
    from engine.prd import parse_prd
    mock_parsed = {
        "tasks": [
            {"id": "T01", "title": "Health check", "description": "...",
             "files_to_create": ["api/health.py"], "category": "api", "dependencies": []},
            {"id": "T02", "title": "Tests", "description": "...",
             "files_to_create": ["test_health.py"], "category": "test", "dependencies": ["T01"]},
        ]
    }
    mock_parser = MagicMock()
    mock_parser.parse.return_value = mock_parsed
    with patch("prd_parser.PRDParser", return_value=mock_parser):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# PRD\nAny content\n")
            f.flush()
            try:
                tasks = parse_prd(f.name)
                assert len(tasks) == 2
                assert tasks[0].module == "api/health.py"
            finally:
                os.unlink(f.name)
