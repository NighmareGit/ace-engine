"""Prompt templates for the autonomous coding engine.

Each template is a function that takes a task dict and context dict,
and returns a complete prompt string. Templates are self-contained,
handle missing context gracefully, and produce prompts ready for
model inference.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(content: str, max_lines: int = 40) -> str:
    """Keep the first *max_lines* lines and add a truncation notice."""
    lines = content.split("\n")
    if len(lines) <= max_lines:
        return content
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"


def _render_file_tree(entries: List[str]) -> str:
    """Render a project-tree list into an indented block."""
    return "\n".join(f"  {e}" for e in entries)


def _render_files(files: Dict[str, str], *, max_files: int = 5, max_lines: int = 40) -> str:
    """Render existing-file previews as fenced code blocks."""
    parts: list[str] = []
    for idx, (path, content) in enumerate(files.items()):
        if idx >= max_files:
            parts.append(f"\n({len(files) - max_files} additional files omitted)")
            break
        preview = _truncate(content, max_lines)
        # Guess language from extension
        lang = path.rsplit(".", 1)[-1] if "." in path else ""
        parts.append(f"\n### {path}\n```{lang}\n{preview}\n```")
    return "\n".join(parts)


def _render_acceptance(criteria: List[str]) -> str:
    """Render an acceptance-criteria checklist."""
    lines = ["\n## Acceptance Criteria"]
    for c in criteria:
        lines.append(f"- [ ] {c}")
    return "\n".join(lines)


def _render_context_section(context: Optional[Dict[str, Any]]) -> str:
    """Render the shared context block (project structure + existing code)."""
    if not context:
        return ""

    parts: list[str] = []

    if context.get("project_tree"):
        parts.append("\n## Project Structure")
        parts.append(_render_file_tree(context["project_tree"]))

    if context.get("existing_files"):
        parts.append("\n## Existing Code")
        parts.append(_render_files(context["existing_files"]))

    if context.get("conventions"):
        parts.append("\n## Project Conventions")
        for k, v in context["conventions"].items():
            parts.append(f"- **{k}:** {v}")

    if context.get("style_guide"):
        parts.append(f"\n## Style Guide\n{context['style_guide']}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Task type detection
# ---------------------------------------------------------------------------

def detect_task_type(task: dict) -> str:
    """Classify *task* into one of the 7 supported types based on keywords
    in its ``description`` field.

    Returns one of:
        bug_fix | refactor | test | documentation | config | migration | code_generation
    """
    desc = task.get("description", "").lower()

    # Config keywords are checked before documentation so that "dockerfile"
    # is not incorrectly captured by the shorter "doc" substring check.
    def _contains(keywords: tuple[str, ...]) -> bool:
        return any(w in desc for w in keywords)

    if _contains(("fix", "bug", "error", "broken", "crash")):
        return "bug_fix"
    if _contains(("refactor", "restructure", "reorganize", "clean")):
        return "refactor"
    if _contains(("test", "spec", "assert", "mock")):
        return "test"
    if _contains(("config", "dockerfile", "docker-compose", "yaml", "env")):
        return "config"
    if _contains(("doc", "readme", "comment", "docstring")):
        return "documentation"
    if _contains(("migrate", "upgrade", "update", "convert")):
        return "migration"
    return "code_generation"


# ---------------------------------------------------------------------------
# Template 1 — Code generation
# ---------------------------------------------------------------------------

def code_generation_prompt(task: dict, context: Optional[dict] = None) -> str:
    """Generate code for a new feature or module.

    Parameters
    ----------
    task:
        Must contain ``description`` (str) and optionally
        ``acceptance_criteria`` (list[str]).
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``,
        ``style_guide``.
    """
    parts: list[str] = []

    # ── System prompt ──────────────────────────────────────────────────
    parts.append("""\
You are an expert software developer implementing a new feature.

## Output Format
Return each new or modified file as:

<file path="relative/path/to/file.py">
```python
# complete implementation
```
</file>

Multiple files may be returned. Every file must use this exact XML wrapper.

## Requirements
- **Fully implemented** — no placeholders, TODOs, or pass statements.
- **Type hints** on all function signatures.
- **Docstrings** on all public functions and classes.
- **Error handling** for expected failure modes.
- **Follow existing project patterns** — match naming, imports, and
  structure you see in the context.
- **No unused imports.**
- **Prefer composition over inheritance** unless the codebase style says
  otherwise.""")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    if task.get("acceptance_criteria"):
        parts.append(_render_acceptance(task["acceptance_criteria"]))

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] Every function has a type-annotated signature.
- [ ] Every public symbol has a docstring.
- [ ] No file contains `pass`, `TODO`, or `NotImplementedError`.
- [ ] Imports are sorted and complete.
- [ ] The code is consistent with the project conventions above.""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Template 2 — Bug fix
# ---------------------------------------------------------------------------

def bug_fix_prompt(
    task: dict,
    context: Optional[dict] = None,
    error_info: Optional[dict] = None,
) -> str:
    """Fix a bug based on error information.

    Parameters
    ----------
    task:
        Must contain ``description``.
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``.
    error_info:
        Optional keys: ``status``, ``summary``, ``test_results`` (list of
        dicts with ``name``, ``status``, ``traceback``).
    """
    parts: list[str] = []

    # ── System prompt ──────────────────────────────────────────────────
    parts.append("""\
You are an expert debugger. Fix the code error and output the corrected file.

## Output Format
Return **only** the corrected file(s):

<file path="path/to/file.py">
```python
# corrected implementation
```
</file>

Do **not** output files that were not changed.

## Debugging Process
1. Read the error message and traceback carefully.
2. Identify the **root cause** — not just the symptom.
3. Fix the root cause.
4. Verify the fix handles the edge case that caused the bug.
5. Output only the corrected file — unchanged files must be omitted.""")

    # ── Error information ──────────────────────────────────────────────
    if error_info:
        parts.append("\n## Error Information")
        parts.append(f"**Status:** {error_info.get('status', 'unknown')}")
        parts.append(f"**Error:** {error_info.get('summary', 'No summary')}")

        if error_info.get("traceback"):
            parts.append(f"\n### Traceback\n```\n{error_info['traceback']}\n```")

        if error_info.get("test_results"):
            failures = [
                t
                for t in error_info["test_results"]
                if t.get("status") in ("failed", "error")
            ]
            if failures:
                parts.append("\n### Failed Tests")
                for t in failures:
                    parts.append(f"\n**{t.get('name', 'unknown')}** ({t.get('status')})")
                    if t.get("traceback"):
                        parts.append(f"```\n{t['traceback']}\n```")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] The fix addresses the root cause, not just the symptom.
- [ ] The fix does not introduce new failures in adjacent code.
- [ ] Edge cases related to the bug are handled.
- [ ] No unrelated changes were introduced.
- [ ] The corrected file is complete — not a partial diff.""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Template 3 — Refactoring
# ---------------------------------------------------------------------------

def refactoring_prompt(task: dict, context: Optional[dict] = None) -> str:
    """Refactor existing code without changing behavior.

    Parameters
    ----------
    task:
        Must contain ``description``.
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``,
        ``style_guide``.
    """
    parts: list[str] = []

    # ── System prompt ──────────────────────────────────────────────────
    parts.append("""\
You are a senior software engineer performing a careful refactoring.

## Output Format
Return each modified file as:

<file path="relative/path/to/file.py">
```python
# refactored implementation
```
</file>

## Refactoring Rules
- **Preserve all existing behavior** — no functional changes.
- **Preserve all public APIs** — signatures, return types, exceptions.
- **Preserve docstrings** unless the refactoring makes them inaccurate.
- **Improve** readability, naming, structure, or performance.
- **Remove dead code** and simplify overly complex logic.
- **Add type hints** if missing.
- **Break down** large functions/classes into smaller cohesive units.
- **Extract** repeated logic into shared helpers.

## Safety Constraints
- Do not rename public symbols unless the task explicitly asks.
- Do not change import paths that other modules depend on.
- Do not alter serialization formats or database schemas.""")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    if task.get("acceptance_criteria"):
        parts.append(_render_acceptance(task["acceptance_criteria"]))

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] All public APIs remain unchanged.
- [ ] No new external dependencies were introduced.
- [ ] Dead code was removed.
- [ ] Readability improved (shorter functions, better names).
- [ ] The refactored code is self-consistent (no dangling references).""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Template 4 — Test generation
# ---------------------------------------------------------------------------

def test_generation_prompt(task: dict, context: Optional[dict] = None) -> str:
    """Generate tests for existing code.

    Parameters
    ----------
    task:
        Must contain ``description``.  Optional: ``target_files`` (list of
        file paths to test), ``test_framework`` (e.g. ``"pytest"``).
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``.
    """
    parts: list[str] = []

    framework = task.get("test_framework", "pytest")

    # ── System prompt ──────────────────────────────────────────────────
    parts.append(f"""\
You are a meticulous QA engineer writing {framework} tests.

## Output Format
Return each test file as:

<file path="relative/path/to/test_file.py">
```python
# complete test implementation
```
</file>

## Test Quality Requirements
- **Descriptive test names** — the name should describe the scenario and
  expected outcome (e.g. ``test_empty_input_returns_default``).
- **Arrange / Act / Assert** structure with clear section comments.
- **One logical assertion per test** (multiple `assert` lines are fine if
  they verify one concept).
- **Cover** happy path, edge cases, and error/exception paths.
- **Use fixtures or factories** for shared setup — no copy-pasted setup.
- **Mock external boundaries** (network, filesystem, databases) but
  prefer real objects for pure logic.
- **No test interdependencies** — every test must pass in isolation.
- **Use parametrize** for data-driven cases.""")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    if task.get("target_files"):
        parts.append("\n### Files to test")
        for p in task["target_files"]:
            parts.append(f"- `{p}`")

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] Happy path is covered.
- [ ] Edge cases (empty, None, boundary values) are covered.
- [ ] Error/exception paths are covered.
- [ ] Tests are isolated — no shared mutable state.
- [ ] All imports resolve to existing modules.
- [ ] No print statements or commented-out assertions.""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Template 5 — Documentation
# ---------------------------------------------------------------------------

def documentation_prompt(task: dict, context: Optional[dict] = None) -> str:
    """Generate documentation for code.

    Parameters
    ----------
    task:
        Must contain ``description``.  Optional: ``doc_type`` (one of
        ``"api"``, ``"readme"``, ``"inline"``, ``"guide"``).
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``,
        ``style_guide``.
    """
    parts: list[str] = []

    doc_type = task.get("doc_type", "api")

    # ── System prompt ──────────────────────────────────────────────────
    parts.append(f"""\
You are a technical writer producing {doc_type} documentation.

## Output Format
Return each documentation file as:

<file path="relative/path/to/doc.md">
```markdown
# complete documentation
```
</file>

## Documentation Standards
- **Accuracy** — document what the code actually does, not what it was
  intended to do.
- **Clarity** — write for a developer who knows the language but not this
  specific codebase.
- **Completeness** — cover parameters, return values, exceptions, and
  side effects.
- **Examples** — include at least one usage example per public function
  or class.
- **Conventions** — follow the project's existing documentation style.

## Type-Specific Guidance""")
    if doc_type == "api":
        parts.append("""\
- Document every public function, class, and method.
- Include parameter types, return types, and exceptions raised.
- Add code examples showing typical usage.
- Note any side effects or non-obvious behavior.""")
    elif doc_type == "readme":
        parts.append("""\
- Project name, one-sentence description.
- Quick-start / installation.
- Usage examples.
- Configuration options.
- Contributing guidelines (brief).""")
    elif doc_type == "inline":
        parts.append("""\
- Add or improve docstrings on public functions and classes.
- Use Google-style or NumPy-style docstrings (match the project).
- Keep docstrings concise — one line summary, then details if needed.""")
    else:  # guide
        parts.append("""\
- Write a step-by-step guide for a specific workflow.
- Include prerequisites and setup instructions.
- Use code snippets to illustrate each step.
- Cover common pitfalls and troubleshooting.""")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] Every documented parameter has a type and description.
- [ ] Return values are documented.
- [ ] Exceptions / error cases are documented.
- [ ] At least one example is provided per public symbol.
- [ ] Markdown renders correctly (no unclosed fences or broken links).""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Template 6 — Configuration
# ---------------------------------------------------------------------------

def config_generation_prompt(task: dict, context: Optional[dict] = None) -> str:
    """Generate configuration files (Dockerfile, docker-compose, CI, etc.).

    Parameters
    ----------
    task:
        Must contain ``description``.  Optional: ``config_type`` (e.g.
        ``"dockerfile"``, ``"docker-compose"``, ``"ci"``, ``"env"``).
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``.
    """
    parts: list[str] = []

    config_type = task.get("config_type", "general")

    # ── System prompt ──────────────────────────────────────────────────
    parts.append(f"""\
You are a DevOps engineer generating {config_type} configuration.

## Output Format
Return each configuration file as:

<file path="relative/path/to/Dockerfile">
```dockerfile
# complete configuration
```
</file>

## Configuration Standards
- **Minimal base images** — prefer slim/alpine variants.
- **Security** — run as non-root user, pin versions, avoid `latest` tags.
- **Reproducibility** — pin dependency versions where possible.
- **Comments** — explain non-obvious choices with inline comments.
- **Defaults** — provide sensible defaults that work out of the box.
- **Secrets** — never hardcode secrets; use environment variables.

## Best Practices by Type""")

    if config_type in ("dockerfile", "docker"):
        parts.append("""\
- Use multi-stage builds to reduce final image size.
- Order layers from least to most frequently changing.
- Add a .dockerignore to exclude unnecessary files.
- Set WORKDIR before COPY to avoid absolute paths.
- Use COPY instead of ADD unless tarball extraction is needed.""")
    elif config_type in ("docker-compose", "compose"):
        parts.append("""\
- Define all services with explicit image tags.
- Use volumes for persistent data.
- Define networks for service isolation.
- Use health checks for dependent services.
- Include environment variables with sensible defaults.""")
    elif config_type in ("ci", "cicd", "github-actions"):
        parts.append("""\
- Cache dependencies for faster builds.
- Run linting before tests.
- Separate build, test, and deploy stages.
- Include status badges.
- Use matrix builds for multi-platform testing.""")
    elif config_type in ("env", "environment"):
        parts.append("""\
- Provide .env.example with all required variables documented.
- Never commit real secrets.
- Group variables by purpose (database, API keys, feature flags).
- Use descriptive variable names.""")
    else:
        parts.append("""\
- Follow the tool's official documentation for syntax.
- Include comments explaining each section.
- Provide sensible defaults.
- Support environment-specific overrides where applicable.""")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    if task.get("acceptance_criteria"):
        parts.append(_render_acceptance(task["acceptance_criteria"]))

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] No secrets or credentials are hardcoded.
- [ ] Image/tool versions are pinned (not `latest`).
- [ ] Comments explain non-obvious decisions.
- [ ] The configuration is self-contained and runnable.
- [ ] File paths and references are correct relative to the project root.""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Template 7 — Migration
# ---------------------------------------------------------------------------

def migration_prompt(task: dict, context: Optional[dict] = None) -> str:
    """Migrate code from one framework/version to another.

    Parameters
    ----------
    task:
        Must contain ``description``.  Optional: ``source_version``,
        ``target_version``, ``migration_type`` (e.g. ``"api"``,
        ``"syntax"``, ``"dependency"``).
    context:
        Optional keys: ``project_tree``, ``existing_files``, ``conventions``,
        ``migration_guide`` (str — changelog or migration notes).
    """
    parts: list[str] = []

    # ── System prompt ──────────────────────────────────────────────────
    parts.append("""\
You are a migration specialist carefully upgrading code from one version or
framework to another.

## Output Format
Return each modified file as:

<file path="relative/path/to/file.py">
```python
# migrated implementation
```
</file>

Return a migration summary at the end as:

<file path="MIGRATION_NOTES.md">
```markdown
# Migration Summary
...
```
</file>

## Migration Rules
- **Preserve all existing behavior** — migrations must not change semantics.
- **Handle deprecations** — replace deprecated APIs with their replacements.
- **Update imports** — move to the new import paths.
- **Update syntax** — adopt new language/framework syntax where required.
- **Document breaking changes** — if the migration forces API changes,
  document them clearly in MIGRATION_NOTES.md.
- **Incremental approach** — prefer small, verifiable changes over
  wholesale rewrites.

## Safety Constraints
- Never drop data or functionality.
- If a direct migration path is unclear, add a TODO with an explanation
  and keep the old code working with a deprecation warning if possible.""")

    # ── Context ────────────────────────────────────────────────────────
    parts.append(_render_context_section(context))

    if context and context.get("migration_guide"):
        parts.append(f"\n## Migration Guide / Changelog\n{context['migration_guide']}")

    # ── Task ───────────────────────────────────────────────────────────
    parts.append(f"\n## Task\n{task.get('description', '')}")

    if task.get("source_version") and task.get("target_version"):
        parts.append(
            f"\n**From:** {task['source_version']}  \n"
            f"**To:** {task['target_version']}"
        )

    if task.get("acceptance_criteria"):
        parts.append(_render_acceptance(task["acceptance_criteria"]))

    # ── Verification checklist ─────────────────────────────────────────
    parts.append("""\
## Verification Checklist
Before returning your answer, confirm:
- [ ] All deprecated API calls have been replaced.
- [ ] Import paths are updated.
- [ ] No functionality was accidentally removed.
- [ ] MIGRATION_NOTES.md documents breaking changes.
- [ ] The migrated code is syntactically valid.
- [ ] Configuration files (if any) are also updated.""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Convenience dispatcher
# ---------------------------------------------------------------------------

_TEMPLATE_MAP = {
    "bug_fix": bug_fix_prompt,
    "refactor": refactoring_prompt,
    "test": test_generation_prompt,
    "documentation": documentation_prompt,
    "config": config_generation_prompt,
    "migration": migration_prompt,
    "code_generation": code_generation_prompt,
}


def build_prompt(
    task: dict,
    context: Optional[dict] = None,
    error_info: Optional[dict] = None,
    task_type: Optional[str] = None,
) -> str:
    """High-level dispatcher: detect task type and build the prompt.

    Parameters
    ----------
    task:
        Task description and metadata.
    context:
        Shared project context.
    error_info:
        Error / test-failure details (used only for ``bug_fix``).
    task_type:
        Override the auto-detected task type.

    Returns
    -------
    str
        A complete prompt string ready for model inference.
    """
    ttype = task_type or detect_task_type(task)

    if ttype == "bug_fix":
        return bug_fix_prompt(task, context, error_info)

    template_fn = _TEMPLATE_MAP.get(ttype)
    if template_fn is None:
        # Fallback to code generation
        template_fn = code_generation_prompt

    return template_fn(task, context)
