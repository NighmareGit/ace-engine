"""Orchestrator patch contract — validate + apply re-plan patches (T3).

A patch is a strict sub-object validated before the Pipeline ever touches a
Task. The LLM edits TASK DEFINITIONS, never code or files (ADR-0002); the
deterministic gate still judges every artifact.

Patch schema::

    {
        "action": "re-spec" | "reorder" | "escalate-model" | "fail",
        "task_id": "<known task id>",
        "spec_patch": {
            "hypothesis": str,
            "constraint_updates": [str],
            "dependency_allowlist_additions": [str],
            "acceptance_amendments": [str],   # weaken-only
        },
        "model_override": str | None,         # for escalate-model
        "reason": str,
    }

Validation rejects:
* unknown / extra keys at any level (free text outside fields),
* code-like tokens (import / def / os.system / ```) in ANY string value,
* acceptance_amendments that ADD or STRENGTHEN criteria (weaken-only),
* unknown task_ids.
"""

import re

ALLOWED_ACTIONS = {"re-spec", "reorder", "escalate-model", "fail",
                   "create-stub", "swap-targeting"}
TOP_LEVEL_KEYS = {"action", "task_id", "spec_patch", "model_override", "reason"}
SPEC_PATCH_KEYS = {"hypothesis", "constraint_updates",
                   "dependency_allowlist_additions", "acceptance_amendments"}

# Unambiguous code statements. We deliberately avoid matching the bare word
# "import" in prose (e.g. "the import fails", "import 'flask'") — only real
# import STATEMENTS (module followed by statement terminator) count. This keeps
# natural-language reasons valid while rejecting smuggled executable code.
_IMPORT_STMT = re.compile(
    r"\bimport\s+[a-z_]\w*(?:\.[a-z_]\w*)*(?:\s+as\s+\w+)?\s*[;\n]",
    re.IGNORECASE,
)
_CODE_TOKEN_RE = re.compile(
    r"```"
    r"|\bdef\s+\w+\s*\("
    r"|\bclass\s+\w+[:(]"
    r"|\bos\.system\b"
    r"|\bsubprocess\.\w+"
    r"|\beval\s*\("
    r"|\bexec\s*\("
    r"|\b__import__\b"
    r"|" + _IMPORT_STMT.pattern,
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Acceptance-amendment weaken-only classifier.
#
# _STRENGTHEN_RE flags language that ADDS or TIGHTENS criteria; _WEAKEN_RE
# flags language that RELAXES them. An amendment matching strengthen BUT NOT
# weaken is rejected. "Remove the strict latency bound" matches both and is
# therefore allowed (it weakens).
#
# TUNING NOTE: these are a pragmatic first cut. Tune on real 35B re-plan
# outputs during the T4 observe phase — this is the one place to edit.
# ---------------------------------------------------------------------------
_STRENGTHEN_RE = re.compile(
    r"\b(MUST|shall|require[ds]?|mandatory|always|every|at least|"
    r"enforce[ds]?|add(s|ed|ing)?\s+(a\s+)?(new\s+)?(requirement|criterion|"
    r"criteria|constraint|test|check)|no longer optional)\b",
    re.IGNORECASE,
)
_WEAKEN_RE = re.compile(
    r"\b(remov(e|es|ing)|relax(e|es|ing)?|weaken(s|ing)?|drop(s|ping)?|"
    r"loosen(s|ing)?|ease[sd]?|optional|no longer|instead|reimplement|"
    r"no longer required|no longer needed)\b",
    re.IGNORECASE,
)


def _contains_code(s: str) -> bool:
    """True if a string value contains a code-like token (actual code, not prose)."""
    return bool(_CODE_TOKEN_RE.search(s))


def _collect_strings(obj, path="root"):
    """Yield (path, string_value) for every string in the nested structure."""
    if isinstance(obj, str):
        yield (path, obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _collect_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _collect_strings(v, f"{path}[{i}]")


def validate_patch(patch_obj, known_task_ids: list) -> tuple[bool, str]:
    """Validate a patch against the strict schema. Returns (ok, error_msg)."""
    if not isinstance(patch_obj, dict):
        return (False, "patch must be an object")

    # 1. No free text / unknown top-level keys.
    extra = set(patch_obj.keys()) - TOP_LEVEL_KEYS
    if extra:
        return (False, f"unknown top-level key(s): {sorted(extra)}")

    action = patch_obj.get("action")
    if action not in ALLOWED_ACTIONS:
        return (False, f"action must be one of {sorted(ALLOWED_ACTIONS)}, got {action!r}")

    task_id = patch_obj.get("task_id")
    if task_id not in known_task_ids:
        return (False, f"unknown task_id {task_id!r}")

    spec = patch_obj.get("spec_patch")
    if not isinstance(spec, dict):
        return (False, "spec_patch must be an object")

    extra_spec = set(spec.keys()) - SPEC_PATCH_KEYS
    if extra_spec:
        return (False, f"unknown spec_patch key(s): {sorted(extra_spec)}")

    # 2. Code-like tokens in ANY string value (deep scan of the whole patch).
    for path, s in _collect_strings(patch_obj):
        if _contains_code(s):
            return (False, f"code-like token found in {path}: {s!r}")

    # 3. Acceptance amendments must weaken-only (no adding/strengthening).
    amendments = spec.get("acceptance_amendments") or []
    if not isinstance(amendments, list):
        return (False, "acceptance_amendments must be a list")
    for a in amendments:
        if _STRENGTHEN_RE.search(a) and not _WEAKEN_RE.search(a):
            return (False,
                    f"acceptance_amendments must weaken-only (adds/strengthens): {a!r}")

    return (True, "")


def _validate_structure(patch_obj) -> tuple[bool, str]:
    """Structural + code-token validation that does NOT depend on the known
    task list. Used by apply_orchestrator_patch to stay task-list independent."""
    if not isinstance(patch_obj, dict):
        return (False, "patch must be an object")
    extra = set(patch_obj.keys()) - TOP_LEVEL_KEYS
    if extra:
        return (False, f"unknown top-level key(s): {sorted(extra)}")
    action = patch_obj.get("action")
    if action not in ALLOWED_ACTIONS:
        return (False, f"action must be one of {sorted(ALLOWED_ACTIONS)}, got {action!r}")
    spec = patch_obj.get("spec_patch")
    if not isinstance(spec, dict):
        return (False, "spec_patch must be an object")
    extra_spec = set(spec.keys()) - SPEC_PATCH_KEYS
    if extra_spec:
        return (False, f"unknown spec_patch key(s): {sorted(extra_spec)}")
    for path, s in _collect_strings(patch_obj):
        if _contains_code(s):
            return (False, f"code-like token found in {path}: {s!r}")
    amendments = spec.get("acceptance_amendments") or []
    if not isinstance(amendments, list):
        return (False, "acceptance_amendments must be a list")
    for a in amendments:
        if _STRENGTHEN_RE.search(a) and not _WEAKEN_RE.search(a):
            return (False, f"acceptance_amendments must weaken-only: {a!r}")
    return (True, "")


def apply_orchestrator_patch(task, patch_obj) -> bool:
    """Apply a VALIDATED patch to a Task's definition. Mutates ``task``.

    Raises ValueError if the patch fails structural validation or targets a
    different task.
    """
    ok, err = _validate_structure(patch_obj)
    if not ok:
        raise ValueError(f"invalid patch: {err}")

    if patch_obj.get("task_id") != task.id:
        raise ValueError(
            f"patch task_id {patch_obj.get('task_id')!r} does not match "
            f"task {task.id!r}")

    action = patch_obj["action"]
    spec = patch_obj.get("spec_patch") or {}

    if action == "fail":
        task.orchestrator_state = "FAILED"
        return True

    if action == "reorder":
        task.orchestrator_state = "REORDERED"
        return True

    if action == "escalate-model":
        task.model_override = patch_obj.get("model_override")
        task.orchestrator_state = "ESCALATED"
        return True

    # D4: create-stub — instruction to generate the missing sibling module
    # before retrying. Recorded as a state change; the engine interprets it.
    if action == "create-stub":
        task.orchestrator_state = "CREATE_STUB"
        if spec.get("hypothesis"):
            task.hypothesis = spec["hypothesis"]
        return True

    # D4: swap-targeting — move declared file paths (fix a targeting miss).
    if action == "swap-targeting":
        task.orchestrator_state = "SWAP_TARGETING"
        if spec.get("hypothesis"):
            task.hypothesis = spec["hypothesis"]
        return True

    # action == "re-spec": mutate the task definition.
    hypothesis = spec.get("hypothesis")
    if hypothesis:
        task.hypothesis = hypothesis
        task.description = (task.description or "") + "\n[re-spec] " + hypothesis

    for dep in spec.get("dependency_allowlist_additions") or []:
        if dep not in task.dependencies:
            task.dependencies.append(dep)

    for cu in spec.get("constraint_updates") or []:
        if not hasattr(task, "constraint_updates"):
            task.constraint_updates = []
        task.constraint_updates.append(cu)

    # acceptance_amendments (weaken-only, already validated) are recorded;
    # the deterministic gate still judges the artifact.
    for am in spec.get("acceptance_amendments") or []:
        if not hasattr(task, "acceptance_amendments"):
            task.acceptance_amendments = []
        task.acceptance_amendments.append(am)

    task.orchestrator_state = "RE_SPEC"
    return True
