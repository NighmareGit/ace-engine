"""Prompt Compiler — manifest-driven prompt assembly for the autonomous coding engine.

Loads a PROMPTS_MANIFEST.json, resolves task prompts from built-in templates,
fills placeholders with context data, applies rails (format constraints, token
budget, temperature), and returns a ready-to-use prompt string.  Also provides
ManifestGenerator for creating a manifest from a PRD parse result.

Schema
------
PROMPTS_MANIFEST.json must conform to ManifestSchema (validated on load).

Usage::

    from prompt_compiler import PromptCompiler, ManifestGenerator

    # Compile a prompt from an existing manifest
    compiler = PromptCompiler("PROMPTS_MANIFEST.json")
    prompt = compiler.compile("T01", context={"project_path": "/workspace"})

    # Generate a manifest from a PRD parse result
    gen = ManifestGenerator()
    manifest = gen.generate(prd_result, project_context={"project_path": "/ws"})
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Schema dataclasses — mirrors the PROMPTS_MANIFEST.json structure
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    """Detailed specification for a single task."""

    goal: str = ""
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    architecture_notes: str = ""


@dataclass
class ExtraSection:

    heading: str = "H2"
    content: str = ""


@dataclass
class TemplateOverrides:

    system_prompt_override: Optional[str] = None
    extra_sections: List[ExtraSection] = field(default_factory=list)


@dataclass
class ContextRef:

    path: str = ""
    type: str = "full_file"  # full_file | first_50_lines | pattern_match
    pattern: Optional[str] = None


@dataclass
class Rails:

    max_context_tokens: int = 12000
    temperature: float = 0.3
    require_type_hints: bool = True
    require_docstrings: bool = True
    require_tests: bool = False


@dataclass
class TaskEntry:

    title: str = ""
    task_type: str = "implement"  # implement | refactor | fix | test | config | migrate | document
    spec: TaskSpec = field(default_factory=TaskSpec)
    template_overrides: TemplateOverrides = field(default_factory=TemplateOverrides)
    context_refs: List[ContextRef] = field(default_factory=list)
    imports_from_project: List[str] = field(default_factory=list)
    rails: Rails = field(default_factory=Rails)


@dataclass
class ManifestSchema:

    campaign_id: str = ""
    generated_by: str = "orchestrator"  # orchestrator | manual
    prd_title: str = ""
    tasks: Dict[str, TaskEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _manifest_to_dict(manifest: ManifestSchema) -> dict:
    """Convert a ManifestSchema to a plain dict (JSON-safe)."""
    raw = asdict(manifest)
    return raw


def _manifest_from_dict(data: dict) -> ManifestSchema:
    """Build a ManifestSchema from a raw dict (e.g. loaded from JSON)."""

    def _build_task_entry(raw: dict) -> TaskEntry:
        spec_raw = raw.get("spec", {})
        spec = TaskSpec(
            goal=spec_raw.get("goal", ""),
            inputs=spec_raw.get("inputs", []),
            outputs=spec_raw.get("outputs", []),
            constraints=spec_raw.get("constraints", []),
            success_criteria=spec_raw.get("success_criteria", []),
            architecture_notes=spec_raw.get("architecture_notes", ""),
        )
        overrides_raw = raw.get("template_overrides", {})
        extra_raw = overrides_raw.get("extra_sections", [])
        extra_sections = [
            ExtraSection(heading=s.get("heading", "H2"), content=s.get("content", ""))
            for s in extra_raw
        ]
        overrides = TemplateOverrides(
            system_prompt_override=overrides_raw.get("system_prompt_override"),
            extra_sections=extra_sections,
        )
        ctx_refs = [
            ContextRef(
                path=c.get("path", ""),
                type=c.get("type", "full_file"),
                pattern=c.get("pattern"),
            )
            for c in raw.get("context_refs", [])
        ]
        rails_raw = raw.get("rails", {})
        rails = Rails(
            max_context_tokens=rails_raw.get("max_context_tokens", 12000),
            temperature=rails_raw.get("temperature", 0.3),
            require_type_hints=rails_raw.get("require_type_hints", True),
            require_docstrings=rails_raw.get("require_docstrings", True),
            require_tests=rails_raw.get("require_tests", False),
        )
        return TaskEntry(
            title=raw.get("title", ""),
            task_type=raw.get("task_type", "implement"),
            spec=spec,
            template_overrides=overrides,
            context_refs=ctx_refs,
            imports_from_project=raw.get("imports_from_project", []),
            rails=rails,
        )

    tasks = {k: _build_task_entry(v) for k, v in data.get("tasks", {}).items()}
    return ManifestSchema(
        campaign_id=data.get("campaign_id", ""),
        generated_by=data.get("generated_by", "orchestrator"),
        prd_title=data.get("prd_title", ""),
        tasks=tasks,
    )


# ---------------------------------------------------------------------------
# Built-in prompt templates — {{placeholder}} syntax
# ---------------------------------------------------------------------------

SYSTEM_BASE = """\
You are an expert software developer. You produce clean, well-documented, \
production-ready code.

## Your Role in This Multi-Module Project

You are implementing task {task_id} of a multi-module project.
Sibling modules ({sibling_list}) are declared in the project DAG; import them,
never inline or reimplement them.
Create EXACTLY the files listed under files_to_create.
If an import fails validation as 'unknown', prefer stdlib; if it is a DAG
sibling, code against its contract.
Before finishing, verify every file_to_create exists and contains your
implementation.

## Output Format

Output each file in this exact XML format:

<file path="relative/path/to/file.py">
```python
# Your code here — complete, runnable, no placeholders
```
</file>

You may output multiple `<file>` blocks for multi-file tasks.

## Code Quality Requirements
1. **Type hints** — All function signatures must have type hints
2. **Docstrings** — All public functions/classes must have docstrings (Google style)
3. **Error handling** — Handle expected errors explicitly
4. **No placeholders** — Every function must be fully implemented
5. **No invented imports** — Only import existing libraries
6. **Follow existing patterns** — Match the code style of the project
7. **Single responsibility** — Each function does one thing well
"""

IMPLEMENT_TEMPLATE = """\
{{system_prompt}}

## Task Specification

**Goal:** {{goal}}

**Files to create:**
{{outputs_block}}

Create EXACTLY these files: {{files_to_create}}. Do not place code in __init__.py or other files.

**Files to reference:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Architecture Notes

{{architecture_notes}}

## Project Imports

{{imports_block}}

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] Every function has type hints
- [ ] Every public function has a docstring
- [ ] No file contains `pass`, `TODO`, or `NotImplementedError`
- [ ] All imports resolve to available libraries
- [ ] Code matches existing project patterns
"""

REFACTOR_TEMPLATE = """\
{{system_prompt}}

## Refactoring Task

**Goal:** {{goal}}

**Files to modify:**
{{outputs_block}}

**Files to reference:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Architecture Notes

{{architecture_notes}}

## Refactoring Rules

- **Preserve all existing behavior** — no functional changes.
- **Preserve all public APIs** — signatures, return types, exceptions.
- **Improve** readability, naming, structure, or performance.
- **Remove dead code** and simplify overly complex logic.
- **Add type hints** if missing.
- **Break down** large functions/classes into smaller cohesive units.

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] All public APIs remain unchanged
- [ ] No new external dependencies were introduced
- [ ] Dead code was removed
- [ ] Readability improved (shorter functions, better names)
"""

FIX_TEMPLATE = """\
{{system_prompt}}

## Bug Fix Task

**Goal:** {{goal}}

**Files to fix:**
{{outputs_block}}

**Files to reference:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Debugging Process

1. Read the error message and traceback carefully.
2. Identify the **root cause** — not just the symptom.
3. Fix the root cause.
4. Verify the fix handles the edge case that caused the bug.
5. Output only the corrected file — unchanged files must be omitted.

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] The fix addresses the root cause, not just the symptom
- [ ] The fix does not introduce new failures in adjacent code
- [ ] Edge cases related to the bug are handled
- [ ] No unrelated changes were introduced
"""

TEST_TEMPLATE = """\
{{system_prompt}}

## Test Generation Task

**Goal:** {{goal}}

**Files to create:**
{{outputs_block}}

**Files to test:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Test Quality Requirements

- **Descriptive test names** — describe scenario and expected outcome.
- **Arrange / Act / Assert** structure with clear section comments.
- **One logical assertion per test**.
- **Cover** happy path, edge cases, and error/exception paths.
- **Use fixtures or factories** for shared setup.
- **Mock external boundaries** (network, filesystem, databases).
- **No test interdependencies** — every test must pass in isolation.
- **Use parametrize** for data-driven cases.

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] Happy path is covered
- [ ] Edge cases (empty, None, boundary values) are covered
- [ ] Error/exception paths are covered
- [ ] Tests are isolated — no shared mutable state
- [ ] All imports resolve to existing modules
"""

CONFIG_TEMPLATE = """\
{{system_prompt}}

## Configuration Task

**Goal:** {{goal}}

**Files to create:**
{{outputs_block}}

**Files to reference:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Configuration Standards

- **Minimal base images** — prefer slim/alpine variants.
- **Security** — run as non-root user, pin versions.
- **Reproducibility** — pin dependency versions where possible.
- **Comments** — explain non-obvious choices.
- **Defaults** — provide sensible defaults that work out of the box.
- **Secrets** — never hardcode secrets; use environment variables.

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] No secrets or credentials are hardcoded
- [ ] Image/tool versions are pinned (not `latest`)
- [ ] Comments explain non-obvious decisions
- [ ] The configuration is self-contained and runnable
"""

MIGRATE_TEMPLATE = """\
{{system_prompt}}

## Migration Task

**Goal:** {{goal}}

**Files to migrate:**
{{outputs_block}}

**Files to reference:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Migration Rules

- **Preserve all existing behavior** — migrations must not change semantics.
- **Handle deprecations** — replace deprecated APIs with their replacements.
- **Update imports** — move to the new import paths.
- **Update syntax** — adopt new language/framework syntax where required.
- **Document breaking changes** in MIGRATION_NOTES.md.
- **Incremental approach** — prefer small, verifiable changes.

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] All deprecated API calls have been replaced
- [ ] Import paths are updated
- [ ] No functionality was accidentally removed
- [ ] Migration notes document breaking changes
"""

DOCUMENT_TEMPLATE = """\
{{system_prompt}}

## Documentation Task

**Goal:** {{goal}}

**Files to create:**
{{outputs_block}}

**Files to reference:**
{{inputs_block}}

**Constraints:**
{{constraints_block}}

**Success Criteria:**
{{success_block}}

## Documentation Standards

- **Accuracy** — document what the code actually does.
- **Clarity** — write for a developer who knows the language.
- **Completeness** — cover parameters, return values, exceptions.
- **Examples** — include at least one usage example per public function.
- **Conventions** — follow the project's existing documentation style.

## Context Files

{{context_block}}

## Verification Checklist

Before outputting code, verify:
- [ ] Every documented parameter has a type and description
- [ ] Return values are documented
- [ ] Exceptions / error cases are documented
- [ ] At least one example is provided per public symbol
"""

# Map task_type string → template string
_TEMPLATE_MAP: Dict[str, str] = {
    "implement": IMPLEMENT_TEMPLATE,
    "refactor": REFACTOR_TEMPLATE,
    "fix": FIX_TEMPLATE,
    "test": TEST_TEMPLATE,
    "config": CONFIG_TEMPLATE,
    "migrate": MIGRATE_TEMPLATE,
    "document": DOCUMENT_TEMPLATE,
}

# Map task_type string → default system prompt
_SYSTEM_PROMPT_OVERRIDES: Dict[str, str] = {
    "implement": SYSTEM_BASE,
    "refactor": (
        "You are a senior software engineer performing a careful refactoring.\n"
        "Preserve all existing behavior while improving readability and structure."
    ),
    "fix": (
        "You are an expert debugger. Fix the code error and output the corrected file.\n"
        "Focus on the root cause, not just the symptom."
    ),
    "test": (
        "You are a meticulous QA engineer writing comprehensive tests.\n"
        "Cover happy paths, edge cases, and error conditions."
    ),
    "config": (
        "You are a DevOps engineer generating configuration.\n"
        "Follow security best practices and provide sensible defaults."
    ),
    "migrate": (
        "You are a migration specialist carefully upgrading code.\n"
        "Preserve all existing behavior while adopting new APIs."
    ),
    "document": (
        "You are a technical writer producing clear documentation.\n"
        "Be accurate, complete, and follow project conventions."
    ),
}


# ---------------------------------------------------------------------------
# Placeholder fill helpers
# ---------------------------------------------------------------------------

def _render_list_block(items: List[str], bullet: str = "-") -> str:
    """Render a list of items as a markdown bullet block."""
    if not items:
        return "  (none)"
    return "\n".join(f"  {bullet} `{i}`" if i.startswith("@") else f"  {bullet} {i}" for i in items)


def _render_acceptance_block(criteria: List[str]) -> str:
    """Render success criteria as a checklist."""
    if not criteria:
        return "  (none)"
    return "\n".join(f"  - [ ] {c}" for c in criteria)


def _resolve_context_ref(ref: ContextRef, project_path: Optional[str] = None) -> str:
    """Read a context file from disk and return its content.

    Resolves ``@``-prefixed paths relative to *project_path* (if given),
    otherwise relative to cwd.  Returns a placeholder string on failure.
    """
    raw_path = ref.path.lstrip("@")
    if project_path and not os.path.isabs(raw_path):
        full = os.path.join(project_path, raw_path)
    else:
        full = raw_path

    try:
        p = Path(full)
        if not p.exists():
            return f"<!-- {ref.path}: file not found -->"
        text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError) as exc:
        return f"<!-- {ref.path}: {exc} -->"

    if ref.type == "first_50_lines":
        lines = text.splitlines()
        text = "\n".join(lines[:50])
        if len(lines) > 50:
            text += f"\n... ({len(lines) - 50} more lines)"
    elif ref.type == "pattern_match" and ref.pattern:
        try:
            matches = re.findall(ref.pattern, text, re.MULTILINE)
            if matches:
                text = "\n".join(matches[:30])
            else:
                text = f"<!-- no matches for pattern: {ref.pattern} -->"
        except re.error:
            text = f"<!-- invalid regex: {ref.pattern} -->"

    lang = raw_path.rsplit(".", 1)[-1] if "." in raw_path else ""
    return f"### {ref.path}\n```{lang}\n{text}\n```"


# ---------------------------------------------------------------------------
# PromptCompiler
# ---------------------------------------------------------------------------

class PromptCompiler:
    """Load a PROMPTS_MANIFEST.json and compile prompts for individual tasks.

    Parameters
    ----------
    manifest_path : str
        Path to a PROMPTS_MANIFEST.json file.
    project_root : str or None
        Base directory for resolving ``@``-prefixed context_refs.
        Defaults to the current working directory.
    """

    def __init__(self, manifest_path: str, project_root: Optional[str] = None):
        self._manifest_path = manifest_path
        self._project_root = project_root or os.getcwd()
        self._manifest: ManifestSchema = self._load_manifest()

    # -- loading -----------------------------------------------------------

    def _load_manifest(self) -> ManifestSchema:
        """Read and parse the manifest file into a ManifestSchema."""
        path = Path(self._manifest_path)
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found: {self._manifest_path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _manifest_from_dict(raw)

    @property
    def manifest(self) -> ManifestSchema:
        """Return the loaded manifest schema."""
        return self._manifest

    @property
    def campaign_id(self) -> str:
        return self._manifest.campaign_id

    @property
    def prd_title(self) -> str:
        return self._manifest.prd_title

    def list_task_ids(self) -> List[str]:
        """Return all task IDs present in the manifest."""
        return list(self._manifest.tasks.keys())

    def get_task(self, task_id: str) -> TaskEntry:
        """Return the TaskEntry for *task_id*."""
        if task_id not in self._manifest.tasks:
            raise KeyError(f"Task {task_id} not found in manifest")
        return self._manifest.tasks[task_id]

    def get_rails(self, task_id: str) -> Rails:
        """Return the Rails for *task_id*."""
        return self.get_task(task_id).rails

    # -- compilation -------------------------------------------------------

    def compile(
        self,
        task_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Compile a ready-to-use prompt string for *task_id*.

        Steps:
          1. Select the built-in template for the task's type.
          2. Resolve context_refs by reading files from disk.
          3. Fill ``{{placeholder}}`` variables from the task spec + context.
          4. Apply template_overrides (system prompt, extra sections).
          5. Append rails metadata as an invisible constraint block.

        Parameters
        ----------
        task_id : str
            The task identifier (e.g. ``"T01"``).
        context : dict or None
            Additional context variables.  Merged on top of the spec fields.
            Common keys: ``project_path``, ``project_tree``, ``conventions``.

        Returns
        -------
        str
            The assembled prompt string.
        """
        task = self.get_task(task_id)
        ctx = context or {}

        # 1. Select template
        template_str = _TEMPLATE_MAP.get(task.task_type, IMPLEMENT_TEMPLATE)

        # 2. Resolve context_refs
        context_parts: List[str] = []
        project_path = ctx.get("project_path", self._project_root)
        for ref in task.context_refs:
            context_parts.append(_resolve_context_ref(ref, project_path))
        context_block = "\n\n".join(context_parts) if context_parts else "  (no context files referenced)"

        # 3. Build list blocks
        outputs_block = _render_list_block(task.spec.outputs)
        inputs_block = _render_list_block(task.spec.inputs)
        constraints_block = _render_list_block(task.spec.constraints)
        success_block = _render_acceptance_block(task.spec.success_criteria)

        imports_block = (
            "\n".join(f"  - `{i}`" for i in task.imports_from_project)
            if task.imports_from_project
            else "  (none)"
        )

        # 4. System prompt
        system_prompt = _SYSTEM_PROMPT_OVERRIDES.get(task.task_type, SYSTEM_BASE)
        if task.template_overrides.system_prompt_override:
            system_prompt = task.template_overrides.system_prompt_override

        # 5. Fill all {{placeholder}} variables via regex (no str.format)
        # D3: files_to_create is the verbatim list of declared target files
        # (pinned from the task's files[] — the targeting rail). spec.outputs
        # entries are "@{path}" — strip the "@" for the human-readable list.
        _files_to_create_list = [o.lstrip("@") for o in task.spec.outputs]
        _files_to_create_str = ", ".join(_files_to_create_list) if _files_to_create_list else task.title
        # D4: sibling_list = other tasks' modules in the DAG (threaded the
        # same way as dag_files). The worker persona uses it to warn the model
        # not to reimplement siblings.
        _sibling_modules = [
            t.spec.outputs[0].lstrip("@") if t.spec.outputs else t.title
            for tid, t in self._manifest.tasks.items()
            if tid != task_id
        ]
        _sibling_str = ", ".join(_sibling_modules) if _sibling_modules else "(none)"
        placeholder_values: Dict[str, str] = {
            "system_prompt": system_prompt,
            "outputs_block": outputs_block,
            "inputs_block": inputs_block,
            "constraints_block": constraints_block,
            "success_block": success_block,
            "imports_block": imports_block,
            "context_block": context_block,
            "goal": task.spec.goal or task.title,
            "architecture_notes": task.spec.architecture_notes,
            "title": task.title,
            "task_type": task.task_type,
            "campaign_id": self._manifest.campaign_id,
            "prd_title": self._manifest.prd_title,
            "files_to_create": _files_to_create_str,
            "task_id": task_id,
            "sibling_list": _sibling_str,
        }
        # Merge context dict values
        for k, v in ctx.items():
            if isinstance(v, str):
                placeholder_values[k] = v
            else:
                placeholder_values[k] = json.dumps(v, default=str)

        # Replace {{key}} with values
        def _replace_placeholder(match: re.Match) -> str:
            key = match.group(1).strip()
            return placeholder_values.get(key, match.group(0))

        filled = re.sub(r"\{\{(\w+)\}\}", _replace_placeholder, template_str)

        # 7. Apply extra_sections from template_overrides
        extra_parts: List[str] = []
        for sec in task.template_overrides.extra_sections:
            extra_parts.append(f"\n## {sec.heading}\n{sec.content}")
        if extra_parts:
            filled += "\n" + "\n".join(extra_parts)

        # 8. Append rails as an invisible constraint block
        rails = task.rails
        rails_block = (
            "\n\n<!-- RAILS\n"
            f"max_context_tokens: {rails.max_context_tokens}\n"
            f"temperature: {rails.temperature}\n"
            f"require_type_hints: {str(rails.require_type_hints).lower()}\n"
            f"require_docstrings: {str(rails.require_docstrings).lower()}\n"
            f"require_tests: {str(rails.require_tests).lower()}\n"
            "-->"
        )
        filled += rails_block

        return filled

    def compile_all(
        self,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """Compile prompts for every task in the manifest.

        Returns a dict mapping task_id → prompt string.
        """
        return {tid: self.compile(tid, context=context) for tid in self.list_task_ids()}


# ---------------------------------------------------------------------------
# ManifestGenerator
# ---------------------------------------------------------------------------

class ManifestGenerator:
    """Generate a PROMPTS_MANIFEST.json from a PRD parse result + project context.

    This is the bridge between ``PRDParser.parse()`` output and the prompt
    compilation pipeline.  The orchestrator calls ``generate()`` to produce
    a manifest that can then be consumed by ``PromptCompiler``.
    """

    # Default rails per task type
    _DEFAULT_RAILS: Dict[str, Rails] = {
        "implement": Rails(max_context_tokens=12000, temperature=0.3),
        "refactor":  Rails(max_context_tokens=10000, temperature=0.2),
        "fix":       Rails(max_context_tokens=8000,  temperature=0.2),
        "test":      Rails(max_context_tokens=10000, temperature=0.3),
        "config":    Rails(max_context_tokens=6000,  temperature=0.1),
        "migrate":   Rails(max_context_tokens=10000, temperature=0.2),
        "document":  Rails(max_context_tokens=8000,  temperature=0.5),
    }

    # Map PRDParser category → task_type
    _CATEGORY_MAP: Dict[str, str] = {
        "data": "implement",
        "api": "implement",
        "ui": "implement",
        "logic": "implement",
        "test": "test",
        "infra": "config",
    }

    def generate(
        self,
        prd_result: Dict[str, Any],
        project_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate a PROMPTS_MANIFEST dict from a PRD parse result.

        Parameters
        ----------
        prd_result : dict
            Output of ``PRDParser.parse()``.  Keys: ``prd_title``, ``tasks``,
            ``metadata``.
        project_context : dict or None
            Optional project-level information.  Keys: ``project_path``,
            ``available_libs``, ``conventions``, ``imports``.

        Returns
        -------
        dict
            A JSON-serializable dict matching the ManifestSchema.
        """
        ctx = project_context or {}
        now = datetime.now(timezone.utc).isoformat()

        tasks_out: Dict[str, TaskEntry] = {}
        for raw_task in prd_result.get("tasks", []):
            task_id = raw_task.get("id", f"T{len(tasks_out) + 1:02d}")
            task_type = self._infer_task_type(raw_task)
            default_rails = self._DEFAULT_RAILS.get(task_type, Rails())

            # Build spec from PRD task fields
            files_to_create = raw_task.get("files_to_create", [])
            files_to_modify = raw_task.get("files_to_modify", [])
            outputs = [f"@{f}" for f in files_to_create]
            inputs = [f"@{f}" for f in files_to_modify]

            constraints: List[str] = []
            if raw_task.get("complexity"):
                constraints.append(f"Complexity: {raw_task['complexity']}")
            if raw_task.get("estimated_tokens"):
                constraints.append(f"Estimated token budget: {raw_task['estimated_tokens']}")
            constraints.extend(ctx.get("constraints", []))

            success_criteria = list(raw_task.get("acceptance_criteria", []))

            # Architecture notes from spec
            spec_text = raw_task.get("spec", "")
            arch_notes = ""
            if spec_text:
                arch_notes = spec_text[:2000]  # cap at 2000 chars

            spec = TaskSpec(
                goal=raw_task.get("description", raw_task.get("title", "")),
                inputs=inputs,
                outputs=outputs,
                constraints=constraints,
                success_criteria=success_criteria,
                architecture_notes=arch_notes,
            )

            # Context refs: reference existing files the task touches
            context_refs: List[ContextRef] = []
            for f in files_to_modify:
                context_refs.append(ContextRef(path=f, type="full_file"))
            # If there are existing files in context, add first_50_lines refs
            existing = ctx.get("existing_files", {})
            if isinstance(existing, dict):
                for fpath in list(existing.keys())[:10]:
                    context_refs.append(ContextRef(path=fpath, type="first_50_lines"))

            # Imports
            imports = list(raw_task.get("imports", []))
            imports.extend(ctx.get("imports", []))

            entry = TaskEntry(
                title=raw_task.get("title", ""),
                task_type=task_type,
                spec=spec,
                template_overrides=TemplateOverrides(),
                context_refs=context_refs,
                imports_from_project=imports,
                rails=Rails(
                    max_context_tokens=default_rails.max_context_tokens,
                    temperature=default_rails.temperature,
                    require_type_hints=default_rails.require_type_hints,
                    require_docstrings=default_rails.require_docstrings,
                    require_tests=default_rails.require_tests,
                ),
            )
            tasks_out[task_id] = entry

        manifest = ManifestSchema(
            campaign_id=ctx.get("campaign_id", f"campaign-{int(datetime.now(timezone.utc).timestamp())}"),
            generated_by="orchestrator",
            prd_title=prd_result.get("prd_title", "Untitled PRD"),
            tasks=tasks_out,
        )

        return _manifest_to_dict(manifest)

    def _infer_task_type(self, raw_task: dict) -> str:
        """Infer task_type from a PRD task's description and title.

        Checks keyword heuristics first (more specific), then falls back
        to the PRD category mapping.

        Word-boundary matching: keywords are matched as whole words
        (``\\b`` regex) so that "test" does not fire on file paths like
        ``test_gates.py`` or words like "tester"/"contest".  The "test"
        task-type additionally requires the title to signal a test-writing
        intent (the description alone mentioning tests is not enough —
        implementation tasks routinely reference their test files).
        """
        import re
        text = f"{raw_task.get('title', '')} {raw_task.get('description', '')}".lower()
        title = raw_task.get("title", "").lower()

        def _word_in(word, string):
            return re.search(rf"\b{re.escape(word)}\b", string) is not None

        # Keyword heuristics — checked first (more specific than category)
        if any(_word_in(w, text) for w in ("fix", "bug", "error", "broken", "crash")):
            return "fix"
        if any(_word_in(w, text) for w in ("refactor", "restructure", "reorganize", "clean")):
            return "refactor"
        if any(_word_in(w, text) for w in ("migrate", "upgrade", "convert")):
            return "migrate"
        # "test" persona: only when the TITLE signals test-writing intent.
        # Description-only mentions (e.g. "test_gates.py", "all tests pass")
        # are not sufficient — they appear in every implementation PRD.
        # Match stems: "test"/"tests"/"testing", "spec"/"specs", etc.
        _test_title_words = ("test", "tests", "testing", "spec", "specs",
                             "assert", "mock")
        if any(_word_in(w, title) for w in _test_title_words):
            return "test"
        if any(_word_in(w, text) for w in ("config", "dockerfile", "compose", "yaml")):
            return "config"
        if any(_word_in(w, text) for w in ("doc", "readme", "guide")):
            return "document"

        # Fall back to category-based mapping
        category = raw_task.get("category", "")
        if category in self._CATEGORY_MAP:
            return self._CATEGORY_MAP[category]

        return "implement"

    def write_manifest(
        self,
        manifest_dict: Dict[str, Any],
        output_path: str,
    ) -> str:
        """Write a manifest dict to a JSON file.

        Returns the absolute path of the written file.
        """
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(manifest_dict, indent=2, default=str), encoding="utf-8")
        return str(p.resolve())


# ---------------------------------------------------------------------------
# CLI — quick validation / generation helper
# ---------------------------------------------------------------------------

def _cli():
    """CLI entry point for validation and quick manifest generation."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Prompt Compiler — validate or generate PROMPTS_MANIFEST.json",
    )
    sub = parser.add_subparsers(dest="command")

    # validate
    val_p = sub.add_parser("validate", help="Validate a manifest file")
    val_p.add_argument("manifest", help="Path to PROMPTS_MANIFEST.json")

    # compile
    cmp_p = sub.add_parser("compile", help="Compile a task prompt")
    cmp_p.add_argument("manifest", help="Path to PROMPTS_MANIFEST.json")
    cmp_p.add_argument("--task", "-t", required=True, help="Task ID (e.g. T01)")
    cmp_p.add_argument("--project-root", "-p", help="Project root for context resolution")

    # generate
    gen_p = sub.add_parser("generate", help="Generate manifest from PRD parse result")
    gen_p.add_argument("prd_json", help="Path to PRD parse result JSON")
    gen_p.add_argument("--output", "-o", default="PROMPTS_MANIFEST.json")
    gen_p.add_argument("--campaign-id", help="Campaign identifier")
    gen_p.add_argument("--project-root", help="Project root")

    args = parser.parse_args()

    if args.command == "validate":
        try:
            compiler = PromptCompiler(args.manifest)
            task_ids = compiler.list_task_ids()
            print(f"✅ Valid manifest: {len(task_ids)} tasks")
            print(f"   Campaign: {compiler.campaign_id}")
            print(f"   PRD:      {compiler.prd_title}")
            for tid in task_ids:
                t = compiler.get_task(tid)
                print(f"   {tid}: [{t.task_type}] {t.title[:60]}")
        except Exception as exc:
            print(f"❌ Validation failed: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "compile":
        compiler = PromptCompiler(args.manifest, args.project_root)
        prompt = compiler.compile(args.task)
        print(prompt)

    elif args.command == "generate":
        prd_data = json.loads(Path(args.prd_json).read_text())
        gen = ManifestGenerator()
        ctx: Dict[str, Any] = {}
        if args.campaign_id:
            ctx["campaign_id"] = args.campaign_id
        if args.project_root:
            ctx["project_path"] = args.project_root
        manifest = gen.generate(prd_data, project_context=ctx)
        out_path = gen.write_manifest(manifest, args.output)
        print(f"✅ Manifest written to {out_path}")
        print(f"   {len(manifest['tasks'])} tasks generated")
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
