"""D4 tests: personas — re-plan brain + worker.

1. patch.py ALLOWED_ACTIONS includes 'create-stub' and 'swap-targeting'.
2. patch.py validate_patch accepts/rejects the new actions correctly.
3. replan_brain.py system prompt contains the classification instruction block.
4. Worker prompt (IMPLEMENT_TEMPLATE / SYSTEM_BASE) contains the 5 persona
   lines with real {task_id}/{sibling_list}/{files} interpolation.
5. Regression: a create-stub re-plan does not crash the ladder (engine handles
   unknown-to-patch action gracefully).
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# patch.py: ALLOWED_ACTIONS
# ---------------------------------------------------------------------------

def test_allowed_actions_include_new_actions():
    """ALLOWED_ACTIONS must include create-stub and swap-targeting."""
    from engine.orchestrator.patch import ALLOWED_ACTIONS
    assert "create-stub" in ALLOWED_ACTIONS
    assert "swap-targeting" in ALLOWED_ACTIONS


def test_create_stub_action_validates():
    """A minimal create-stub patch validates."""
    from engine.orchestrator.patch import validate_patch
    p = {
        "action": "create-stub",
        "task_id": "T01",
        "spec_patch": {
            "hypothesis": "Sibling module missing.",
            "constraint_updates": [],
            "dependency_allowlist_additions": [],
            "acceptance_amendments": [],
        },
        "reason": "import failed on missing sibling.",
    }
    ok, err = validate_patch(p, ["T01", "T02"])
    assert ok, err


def test_swap_targeting_action_validates():
    """A minimal swap-targeting patch validates."""
    from engine.orchestrator.patch import validate_patch
    p = {
        "action": "swap-targeting",
        "task_id": "T01",
        "spec_patch": {
            "hypothesis": "Targeting miss — wrong file paths.",
            "constraint_updates": [],
            "dependency_allowlist_additions": [],
            "acceptance_amendments": [],
        },
        "reason": "declared files differ from generated output.",
    }
    ok, err = validate_patch(p, ["T01", "T02"])
    assert ok, err


def test_unknown_action_still_rejected():
    """An action not in ALLOWED_ACTIONS is still rejected."""
    from engine.orchestrator.patch import validate_patch
    p = {
        "action": "delete-everything",
        "task_id": "T01",
        "spec_patch": {
            "hypothesis": "x",
            "constraint_updates": [],
            "dependency_allowlist_additions": [],
            "acceptance_amendments": [],
        },
        "reason": "y",
    }
    ok, err = validate_patch(p, ["T01"])
    assert not ok
    assert "delete-everything" in err


# ---------------------------------------------------------------------------
# replan_brain.py: classification instruction block
# ---------------------------------------------------------------------------

def test_replan_system_prompt_has_classification_block():
    """The re-plan brain system prompt must contain the classification
    instruction block (TARGETING | MISSING-SIBLING | LOGIC | MODEL-LIMIT |
    UNRECOVERABLE)."""
    from engine.orchestrator.replan_protocol import _SYSTEM_PROMPT
    assert "TARGETING" in _SYSTEM_PROMPT
    assert "MISSING-SIBLING" in _SYSTEM_PROMPT
    assert "LOGIC" in _SYSTEM_PROMPT
    assert "MODEL-LIMIT" in _SYSTEM_PROMPT
    assert "UNRECOVERABLE" in _SYSTEM_PROMPT


def test_replan_system_prompt_has_matching_action_instruction():
    """The classification block instructs the brain to pick the matching
    action and re-spec only for LOGIC."""
    from engine.orchestrator.replan_protocol import _SYSTEM_PROMPT
    assert "matching action" in _SYSTEM_PROMPT.lower()
    assert "Re-spec" in _SYSTEM_PROMPT and "only for LOGIC" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Worker persona lines
# ---------------------------------------------------------------------------

def test_worker_prompt_has_persona_lines():
    """SYSTEM_BASE or IMPLEMENT_TEMPLATE must contain the 5 persona lines."""
    from prompt_compiler import SYSTEM_BASE, IMPLEMENT_TEMPLATE
    combined = SYSTEM_BASE + IMPLEMENT_TEMPLATE
    assert "implementing task" in combined.lower()
    assert "sibling" in combined.lower()
    assert "Create EXACTLY the files" in combined
    assert "import fails validation" in combined.lower()
    assert "every file_to_create exists" in combined.lower()


# ---------------------------------------------------------------------------
# Regression: create-stub re-plan does not crash
# ---------------------------------------------------------------------------

def test_create_stub_apply_does_not_crash():
    """apply_orchestrator_patch handles create-stub without crashing.
    create-stub is a valid action; apply records it as a state change."""
    from engine.orchestrator.patch import apply_orchestrator_patch
    from engine import Task
    task = Task(id="T01", title="T", description="d", module="a.py")
    p = {
        "action": "create-stub",
        "task_id": "T01",
        "spec_patch": {
            "hypothesis": "Sibling missing.",
            "constraint_updates": [],
            "dependency_allowlist_additions": [],
            "acceptance_amendments": [],
        },
        "reason": "import failed.",
    }
    # Should not raise.
    result = apply_orchestrator_patch(task, p)
    assert result is True


# ---------------------------------------------------------------------------
# Regression: persona routing (run2-RL1-2 bug)
# Implementation tasks must NOT get the QA-engineer persona just because
# the description mentions "test" (file paths, acceptance criteria, etc.).
# ---------------------------------------------------------------------------

def test_implementation_task_does_not_get_qa_persona():
    """RL1-2 regression: title 'Mechanical gate enforcement' with description
    referencing test files/paths must route to 'implement', not 'test'."""
    from prompt_compiler import ManifestGenerator
    gen = ManifestGenerator()
    prd_result = {
        "prd_title": "test",
        "tasks": [{
            "id": "T01",
            "title": "Mechanical gate enforcement — gates.py (R1, R4, ADR-0004)",
            "description": (
                "Mechanical gate enforcement module. Test uses tester.py. "
                "Files: test_gates.py. All tests pass under pytest."),
            "module": "engine/workflows/ralph/gates.py",
            "files_to_create": [
                "engine/workflows/ralph/gates.py",
                "tests/workflows/ralph/test_gates.py",
            ],
        }],
    }
    manifest = gen.generate(prd_result, project_context={})
    assert manifest["tasks"]["T01"]["task_type"] == "implement"


def test_test_writing_task_gets_qa_persona():
    """A task whose TITLE signals test-writing intent must get 'test' persona."""
    from prompt_compiler import ManifestGenerator
    gen = ManifestGenerator()
    prd_result = {
        "prd_title": "test",
        "tasks": [{
            "id": "T02",
            "title": "Write comprehensive tests for gates.py",
            "description": "Create unit tests.",
            "module": "tests/test_gates.py",
            "files_to_create": ["tests/test_gates.py"],
        }],
    }
    manifest = gen.generate(prd_result, project_context={})
    assert manifest["tasks"]["T02"]["task_type"] == "test"


def test_substring_test_does_not_trigger_persona():
    """Words containing 'test' as substring (contest, tester, protest)
    must NOT trigger the QA persona."""
    from prompt_compiler import ManifestGenerator
    gen = ManifestGenerator()
    for title in ("Contest runner module", "Build the tester",
                  "Protest handler"):
        prd_result = {
            "prd_title": "t",
            "tasks": [{
                "id": "T01",
                "title": title,
                "description": "Build it.",
                "module": "app.py",
                "files_to_create": ["app.py"],
            }],
        }
        manifest = gen.generate(prd_result, project_context={})
        assert manifest["tasks"]["T01"]["task_type"] == "implement", (
            f"Title '{title}' wrongly got persona: "
            f"{manifest['tasks']['T01']['task_type']}")
