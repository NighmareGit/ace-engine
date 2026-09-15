"""T3 tests: orchestrator patch contract (patch.py).

apply_orchestrator_patch() consumes a strict sub-object:
  {action, task_id, spec_patch:{hypothesis, constraint_updates[],
   dependency_allowlist_additions[], acceptance_amendments[]},
   model_override?, reason}

Validator rejects: free text outside fields, code-like tokens in any string,
acceptance amendments that ADD or STRENGTHEN criteria (weaken-only), unknown
task_ids. 6 valid + 6 invalid fixtures.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import patch


KNOWN_TASKS = ["T01", "T02", "T03"]


def _base_patch(**overrides):
    """Return a valid patch, with optional overrides merged in."""
    p = {
        "action": "re-spec",
        "task_id": "T01",
        "spec_patch": {
            "hypothesis": "The import fails because flask is not available.",
            "constraint_updates": ["Use stdlib http.server instead of flask."],
            "dependency_allowlist_additions": [],
            "acceptance_amendments": ["Relax the flask requirement."],
        },
        "reason": "validation exhausted on import 'flask'.",
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# 6 valid fixtures
# ---------------------------------------------------------------------------

def test_valid_minimal_re_spec():
    ok, err = patch.validate_patch(_base_patch(), KNOWN_TASKS)
    assert ok, err


def test_valid_full_spec_patch():
    p = _base_patch(
        spec_patch={
            "hypothesis": "x",
            "constraint_updates": ["a", "b"],
            "dependency_allowlist_additions": ["T02"],
            "acceptance_amendments": ["Remove the strict latency bound."],
        },
    )
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert ok, err


def test_valid_escalate_model():
    p = _base_patch(action="escalate-model", model_override="3090-qwen36-35b",
                    spec_patch={})
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert ok, err


def test_valid_reorder():
    p = _base_patch(action="reorder", spec_patch={})
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert ok, err


def test_valid_fail_action():
    p = _base_patch(action="fail", spec_patch={})
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert ok, err


def test_valid_empty_spec_patch():
    p = _base_patch(spec_patch={})
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert ok, err


# ---------------------------------------------------------------------------
# 6 invalid fixtures
# ---------------------------------------------------------------------------

def test_invalid_unknown_top_level_key():
    """Free text / unknown keys outside the schema are rejected."""
    p = _base_patch()
    p["extra_field"] = "surprise"
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert not ok
    assert "extra" in err.lower() or "unknown" in err.lower()


def test_invalid_code_token_import():
    """Code-like tokens (import) in any string are rejected."""
    p = _base_patch()
    p["reason"] = "import os; os.system('rm -rf /')"
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert not ok
    assert "code" in err.lower() or "import" in err.lower()


def test_invalid_code_token_def():
    p = _base_patch()
    p["spec_patch"]["hypothesis"] = "def exploit(): pass"
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert not ok


def test_invalid_code_token_backtick_fence():
    """Markdown code fences in any string are rejected."""
    p = _base_patch()
    p["reason"] = "here is code ```python\nx=1\n```"
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert not ok
    assert "code" in err.lower() or "fence" in err.lower() or "```" in err


def test_invalid_acceptance_strengthen():
    """Acceptance amendments that ADD or STRENGTHEN criteria are rejected
    (weaken-only)."""
    p = _base_patch()
    p["spec_patch"]["acceptance_amendments"] = [
        "The handler MUST also return a JSON body with a 'status' field."
    ]
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert not ok
    assert "acceptance" in err.lower() or "strengthen" in err.lower()


def test_invalid_unknown_task_id():
    p = _base_patch(task_id="T99")
    ok, err = patch.validate_patch(p, KNOWN_TASKS)
    assert not ok
    assert "task_id" in err.lower() or "unknown" in err.lower()


# ---------------------------------------------------------------------------
# apply_orchestrator_patch: applies a VALID patch to task definitions
# ---------------------------------------------------------------------------

def _make_task(task_id="T01"):
    from engine import Task
    return Task(id=task_id, title="t", description="d", module="m.py",
                dependencies=[], acceptance_criteria=["must pass tests"])


def test_apply_re_spec_mutates_task():
    task = _make_task()
    p = _base_patch(
        action="re-spec",
        spec_patch={
            "hypothesis": "flask missing",
            "constraint_updates": ["use stdlib"],
            "dependency_allowlist_additions": ["T02"],
            "acceptance_amendments": ["relax latency"],
        },
    )
    applied = patch.apply_orchestrator_patch(task, p)
    assert applied is True
    # dependency allowlist applied
    assert "T02" in task.dependencies
    # hypothesis + constraints recorded on the task
    assert "flask missing" in task.description or getattr(task, "hypothesis", None) == "flask missing"


def test_apply_fail_marks_task():
    task = _make_task()
    p = _base_patch(action="fail", spec_patch={})
    applied = patch.apply_orchestrator_patch(task, p)
    assert applied is True
    assert getattr(task, "orchestrator_state", None) == "FAILED"


def test_apply_invalid_patch_raises():
    task = _make_task()
    p = _base_patch(task_id="T99")  # invalid
    with pytest.raises(ValueError):
        patch.apply_orchestrator_patch(task, p)


def test_apply_unknown_action_noop():
    task = _make_task()
    p = _base_patch(action="re-spec", spec_patch={})
    # a known action works; an unknown one is a safe no-op
    p2 = _base_patch(action="bogus-action", spec_patch={})
    ok, err = patch.validate_patch(p2, KNOWN_TASKS)
    assert not ok  # action not in allowed set
