#!/usr/bin/env python3
"""ACE-04: Code Generation Loop — the core engine for autonomous coding.

Takes a task, generates code via BeeLlama, writes it to Triton, and runs tests.
Includes error recovery with retry logic (max 3 attempts).

Architecture:
    Task JSON -> System Prompt + Context -> BeeLlama API -> Extract Code
    -> Write Files -> Run Tests -> Pass/Fail
                                                    |
                                          If fail -> Error Recovery -> Retry

Usage:
    python3 code_generator.py generate --task '{"title":"Create user model","description":"..."}'
    python3 code_generator.py generate --task-file task.json --project myproject
    python3 code_generator.py extract --response response.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from transport import get_transport, TRITON_HOST, TRITON_USER

from streaming_client import emit_event_fire_and_forget as _emit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRITON_PORT = 8080

# Model config ID → BeeLlama port mapping (mirrors work_engine.CONFIG_TO_PORT)
CONFIG_TO_PORT: Dict[str, int] = {
    "3090-qwen36-35b":   8080,
    "3090-muse-glimmer":  8080,
    "3090-laguna-xs":     8080,
    "3090-qwen35-9b":     8080,
    "3070-qwen35-9b":     8082,
    "3070-qwen35-4b":     8082,
}

SYSTEM_PROMPT = """\
You are an expert software developer. You produce clean, well-documented, production-ready code.

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
3. **Error handling** — Handle expected errors explicitly; never let exceptions propagate silently
4. **No placeholders** — Every function must be fully implemented. No `pass`, `TODO`, `NotImplementedError`
5. **No invented imports** — Only import libraries that exist in the project context or are Python/Node.js stdlib
6. **Follow existing patterns** — Match the code style of existing files in the project context
7. **Single responsibility** — Each function does one thing well

## Reasoning

Before writing code, briefly think through:
1. What exactly needs to be implemented
2. What existing code/patterns to follow
3. What edge cases to handle
4. What the acceptance criteria require

Then output the code. Keep reasoning minimal — focus on the implementation.

## Language Detection

Adapt to the project's language:
- Python: type hints, docstrings, PEP 8, f-strings
- TypeScript: interfaces, JSDoc, strict types, async/await
- JavaScript: JSDoc, const/let, template literals, async/await
- Rust: ownership, Result types, doc comments
"""

# ---------------------------------------------------------------------------
# Extended prompt for spec-driven tasks (PRD specs with file contracts)
# ---------------------------------------------------------------------------

SPEC_DRIVEN_PROMPT = """\
You are an expert software developer implementing a specific module as part of a larger system. \
You have been given a complete technical specification for this module, including its API contract, \
dependencies, and how it integrates with other modules.

## Output Format

Output each file in this exact XML format:

<file path="relative/path/to/file.py">
```python
# Your code here — complete, runnable, no placeholders
```
</file>

You may output multiple `<file>` blocks for multi-file tasks.

## Implementation Rules

1. **Implement exactly what the spec says** — the spec is the source of truth. Do not add features \
not described in the spec. Do not omit features that are described.
2. **Respect file dependencies** — if the spec says this module depends on another module that \
will be created separately, import it but do not implement it. Use the exact import paths given.
3. **Follow the API contracts** — function signatures, return types, and error handling must match \
the spec exactly. Downstream modules depend on these contracts.
4. **Use the listed libraries** — only import from the libraries listed in the "Available Libraries" \
section. Do not invent new dependencies.
5. **Self-contained** — each file must be complete and runnable. No stubs, no TODOs, no pass statements.
6. **Type hints and docstrings** — all public functions must have type hints and Google-style docstrings.
7. **Error handling** — handle expected failure modes explicitly. Return structured error dicts \
when the spec defines error returns.
8. **Match existing patterns** — if the project context shows existing code, match its style \
(naming, imports, structure, error patterns).

## Before Outputting Code

1. Read the spec carefully — understand every function, every parameter, every return type.
2. Check which other modules this one imports — use the exact paths given.
3. Plan the file structure — implement each file completely.
4. Verify: every function from the spec exists, every type hint is correct, every import resolves.
"""

# ---------------------------------------------------------------------------
# SSH Helpers (standalone, matches file_ops.py / test_runner.py patterns)
# ---------------------------------------------------------------------------


def _q(s: str) -> str:
    """Minimal POSIX shell quoting (single-quote everything).

    Handles embedded single quotes by breaking out of the quote, inserting
    an escaped quote, and re-entering: ``'it'\\''s'``
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _ssh_run(
    host: str, user: str, remote_cmd: str, timeout: int = 30
) -> Tuple[str, str, int]:
    """Execute a command on Triton via SSH. Returns (stdout, stderr, rc)."""
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        remote_cmd,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"SSH command timed out after {timeout}s", -1
    except Exception as e:
        return "", f"SSH error: {e}", -1


def _ssh_stdin(
    host: str, user: str, remote_cmd: str, stdin_data: str, timeout: int = 60
) -> Tuple[str, str, int]:
    """Execute a command on Triton via SSH, piping stdin_data on stdin.

    Avoids shell-argument-length limits for large payloads.
    Returns (stdout, stderr, rc).
    """
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        remote_cmd,
    ]
    try:
        r = subprocess.run(
            cmd, input=stdin_data, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"SSH command timed out after {timeout}s", -1
    except Exception as e:
        return "", f"SSH error: {e}", -1


# ---------------------------------------------------------------------------
# Code Extraction
# ---------------------------------------------------------------------------


def extract_code(response_text: str) -> Dict[str, str]:
    """Extract code blocks from LLM response.

    Handles three formats in priority order:
    1. ``<file path="src/module.py">`` — explicit path with optional code fence
    2. ``<file>`` — no path (extracts code, infers filename)
    3. Raw code fences — ````python...```` without any <file> wrapper

    Also strips BeeLlama ``<thinking>`` blocks before extraction.

    Returns:
        Dict mapping file paths to their content strings.
    """
    files: Dict[str, str] = {}

    # Strip thinking tags if present (BeeLlama thinking mode)
    clean_text = re.sub(r'<thinking>.*?</thinking>\s*', '', response_text, flags=re.DOTALL)

    # Primary pattern: <file path="..."> with explicit path (quoted or unquoted)
    # Handles both fenced (```python) and unfenced code blocks
    pattern = r'<file\s+path="?([^">\s]+)"?>\s*(?:```(?:python|typescript|javascript|bash|sh|rust|go|c|cpp|java)?\s*\n)?(.*?)(?:\n\s*```)?\s*</file>'
    for match in re.finditer(pattern, clean_text, re.DOTALL):
        path, content = match.groups()
        files[path.strip()] = content.strip()

    # Fallback: <file> without path
    if not files:
        fallback_pattern = r'<file>\s*(?:```(?:python|typescript|javascript|bash|sh|rust|go|c|cpp|java)?\s*\n)?(.*?)(?:\n\s*```)?\s*</file>'
        for idx, match in enumerate(re.finditer(fallback_pattern, clean_text, re.DOTALL)):
            content = match.group(1).strip()
            filename = f"generated_code_{idx + 1}.py"
            class_match = re.search(r'class\s+(\w+)', content)
            func_match = re.search(r'def\s+(\w+)', content)
            if class_match:
                filename = f"{class_match.group(1).lower()}.py"
            elif func_match:
                filename = f"{func_match.group(1).lower()}.py"
            files[filename] = content

    # Fallback: raw code fences without <file> tags
    if not files:
        code_pattern = r'```(?:python|typescript|javascript)?\s*\n(.*?)\n```'
        matches = re.findall(code_pattern, clean_text, re.DOTALL)
        if matches:
            if len(matches) == 1:
                code = matches[0].strip()
                filename = "generated_code.py"
                class_match = re.search(r'class\s+(\w+)', code)
                func_match = re.search(r'def\s+(\w+)', code)
                if class_match:
                    filename = f"{class_match.group(1).lower()}.py"
                elif func_match:
                    filename = f"{func_match.group(1).lower()}.py"
                files[filename] = code
            else:
                for idx, code in enumerate(matches):
                    files[f"generated_code_{idx + 1}.py"] = code.strip()

    return files


def extract_code_from_file(file_path: str) -> Dict[str, str]:
    """Extract code blocks from a response file on disk."""
    with open(file_path, "r", encoding="utf-8") as f:
        return extract_code(f.read())


# ---------------------------------------------------------------------------
# CodeGenerator
# ---------------------------------------------------------------------------


class CodeGenerator:
    """Core code generation engine: prompt -> inference -> extract -> write -> test.

    Orchestrates the full pipeline from task definition to verified code on
    Triton, with automatic retry on test failures.

    Args:
        host: Triton hostname or IP.
        user: SSH user for Triton.
        port: BeeLlama API port.  Overridden by *config* when that is set.
        config: Model config ID (e.g. ``"3090-qwen36-35b"``).  When provided,
            the correct BeeLlama port is looked up from ``CONFIG_TO_PORT``
            automatically — no need to pass *port* separately.
        max_retries: Maximum number of retry attempts on test failure.
        max_tokens: Maximum tokens for LLM generation.
        temperature: Sampling temperature.
        timeout: SSH command timeout in seconds.
    """

    def __init__(
        self,
        host: str = TRITON_HOST,
        user: str = TRITON_USER,
        port: int = TRITON_PORT,
        max_retries: int = 3,
        max_tokens: int = 8192,
        temperature: float = 0.3,
        timeout: int = 120,
        config: Optional[str] = None,
        transport=None,
    ):
        self.host = host
        self.user = user
        self.config = config
        if config and isinstance(config, str):
            self.port = CONFIG_TO_PORT.get(config, port)
        else:
            self.port = port
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.transport = transport or get_transport()

    # ── SSH helpers (thin wrappers — delegate to transport) ───────────

    def _ssh(self, cmd: str, timeout: int = None) -> Tuple[str, str, int]:
        """Run a command on Triton via the transport layer."""
        return self.transport.run_command(cmd, timeout=timeout or self.timeout)

    def _ssh_stdin(self, cmd: str, data: str, timeout: int = 60) -> Tuple[str, str, int]:
        """Run a command on Triton via the transport layer, piping data on stdin.
        
        Note: The transport layer's run_command() does not natively support
        stdin piping. For large file writes, use echo + base64 pipe instead.
        This method falls back to local subprocess for stdin piping.
        """
        import subprocess
        try:
            r = subprocess.run(
                ["bash", "-c", cmd],
                input=data, capture_output=True, text=True,
                timeout=timeout,
            )
            return r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            return "", f"Command timed out after {timeout}s", -1
        except Exception as e:
            return "", f"Error: {e}", -1

    # ── Prompt Building ───────────────────────────────────────────────

    def _build_prompt(self, task: dict, context: dict = None) -> str:
        """Build the full prompt (system + context + task) for code generation.

        Supports two modes:
        - **Standard mode**: Generic task with project context.
        - **Spec-driven mode**: Task includes a ``spec`` key with the full
          technical specification text.  Uses ``SPEC_DRIVEN_PROMPT`` for
          richer, contract-aware code generation.

        Args:
            task: Task dict with keys: title, description, acceptance_criteria,
                  files_to_create, files_to_modify.  Optional: ``spec`` (str —
                  full spec text), ``imports`` (list[str] — import paths this
                  module depends on), ``available_libs`` (list[str] — libraries
                  available in the runtime), ``depends_on`` (list[str] — task
                  IDs this task depends on).
            context: Optional context dict with keys: project_path, project_tree,
                     existing_files (dict of path -> content).

        Returns:
            Complete prompt string ready to send to the LLM.
        """
        parts: List[str] = []

        # Detect spec-driven mode
        has_spec = bool(task.get("spec"))
        system_prompt = SPEC_DRIVEN_PROMPT if has_spec else SYSTEM_PROMPT

        # Layer 1: System prompt
        parts.append("## System Instructions\n")
        parts.append(system_prompt)
        parts.append("")

        # Layer 2: Runtime environment (when available)
        available_libs = task.get("available_libs", [])
        if available_libs:
            parts.append("## Available Libraries\n")
            parts.append("You may import from these libraries (already installed):")
            for lib in available_libs:
                parts.append(f"  - `{lib}`")
            parts.append("")

        # Layer 3: Context (project structure + existing files)
        if context:
            parts.append("## Project Context\n")

            if context.get("project_path"):
                parts.append(f"Project root: {context['project_path']}\n")

            # Support both ContextManager keys and legacy keys
            tree = context.get("project_tree") or context.get("project_structure")
            if tree:
                parts.append("Project structure:")
                if isinstance(tree, dict):
                    # ContextManager returns a dict tree
                    self._render_tree(parts, tree, indent=2)
                else:
                    # Legacy: flat list
                    for entry in tree:
                        parts.append(f"  {entry}")
                parts.append("")

            # Support both ContextManager keys and legacy keys
            files = context.get("existing_files") or context.get("relevant_files")
            if files:
                parts.append("Existing files:")
                for fpath, content in files.items():
                    # Handle both dict-style (ContextManager) and string content
                    if isinstance(content, dict):
                        file_content = content.get("content", "")
                        file_size = content.get("size", 0)
                    else:
                        file_content = str(content)
                        file_size = len(file_content)
                    if not file_content:
                        parts.append(f"\n{fpath} ({file_size} bytes, empty)")
                        continue
                    # Show first 50 lines of each existing file
                    lines = file_content.split("\n")
                    preview = "\n".join(lines[:50])
                    if len(lines) > 50:
                        preview += f"\n... ({len(lines) - 50} more lines)"
                    lang = fpath.rsplit(".", 1)[-1] if "." in fpath else ""
                    parts.append(f"\n{fpath}:\n```{lang}\n{preview}\n```")
                parts.append("")

        # Layer 4: Spec text (spec-driven mode)
        if has_spec:
            parts.append("## Technical Specification\n")
            parts.append(task["spec"])
            parts.append("")

        # Layer 5: Task description
        parts.append("## Task\n")
        parts.append(f"**Title:** {task.get('title', 'Untitled')}\n")
        parts.append(f"**Description:** {task.get('description', 'No description')}\n")

        if task.get("files_to_create"):
            parts.append("**Files to create:**")
            for f in task["files_to_create"]:
                parts.append(f"  - `{f}`")
            parts.append("")

        if task.get("files_to_modify"):
            parts.append("**Files to modify:**")
            for f in task["files_to_modify"]:
                parts.append(f"  - `{f}`")
            parts.append("")

        # Import dependencies (what this module imports from others)
        imports = task.get("imports", [])
        if imports:
            parts.append("**Module imports (from other project modules):**")
            for imp in imports:
                parts.append(f"  - `{imp}`")
            parts.append("")

        # Task dependencies (what must complete before this task)
        depends_on = task.get("depends_on", [])
        if depends_on:
            parts.append("**Depends on (complete before this task):**")
            for dep in depends_on:
                parts.append(f"  - {dep}")
            parts.append("")

        if task.get("acceptance_criteria"):
            parts.append("**Acceptance criteria:**")
            for ac in task["acceptance_criteria"]:
                parts.append(f"- [ ] {ac}")
            parts.append("")

        # Layer 6: Project conventions (from context)
        if context and context.get("conventions"):
            parts.append("## Project Conventions\n")
            parts.append(context["conventions"])
            parts.append("")

        # Layer 7: Existing code patterns (from context)
        if context and context.get("code_patterns"):
            parts.append("## Existing Code Patterns\n")
            parts.append("Follow these patterns from the existing codebase:")
            parts.append(context["code_patterns"])
            parts.append("")

        # Layer 8: Verification checklist
        parts.append("## Verification Checklist\n")
        parts.append("Before outputting code, verify:")
        parts.append("- [ ] All functions have type hints")
        parts.append("- [ ] All public functions have docstrings")
        parts.append("- [ ] Every function from the spec is implemented")
        parts.append("- [ ] All imports resolve to available libraries")
        parts.append("- [ ] No placeholders, TODOs, or pass statements")
        for ac in task.get("acceptance_criteria", []):
            parts.append(f"- [ ] {ac}")
        parts.append("")

        return "\n".join(parts)

    @staticmethod
    def _render_tree(parts: list, tree: dict, indent: int = 2):
        """Render a dict-based project tree into the parts list."""
        prefix = " " * indent
        for key, value in tree.items():
            if isinstance(value, dict):
                parts.append(f"{prefix}{key}/")
                CodeGenerator._render_tree(parts, value, indent + 2)
            elif isinstance(value, (int, float)):
                parts.append(f"{prefix}{key}  ({value} bytes)")
            else:
                parts.append(f"{prefix}{key}")

    def _build_retry_prompt(
        self, task: dict, error: dict, previous_files: Dict[str, str],
        attempt: int,
    ) -> str:
        """Build a retry prompt with error context from a previous failed attempt.

        Args:
            task: Original task dict.
            error: Error dict from test_runner with keys: status, summary,
                   test_results (list with traceback).
            previous_files: Dict of {filepath: content} from the failed attempt.
            attempt: Current retry attempt number (1-based).

        Returns:
            Retry prompt string.
        """
        parts: List[str] = []

        has_spec = bool(task.get("spec"))
        parts.append("## System Instructions\n")
        parts.append(SPEC_DRIVEN_PROMPT if has_spec else SYSTEM_PROMPT)
        parts.append("")

        # Include spec if available
        if has_spec:
            parts.append("## Technical Specification\n")
            parts.append(task["spec"])
            parts.append("")

        # Include the original task so the LLM doesn't lose context
        parts.append("## Original Task\n")
        parts.append(f"**Title:** {task.get('title', 'Untitled')}")
        parts.append(f"**Description:** {task.get('description', 'No description')}\n")

        if task.get("acceptance_criteria"):
            parts.append("**Acceptance criteria:**")
            for ac in task["acceptance_criteria"]:
                parts.append(f"- {ac}")
            parts.append("")

        parts.append("## Error Recovery — Retry Prompt\n")
        parts.append(
            f"The previous code had errors (attempt {attempt} of {self.max_retries}). "
            "Fix the errors and output the corrected code in the same format.\n"
        )

        # Targeted error analysis based on error type
        if error.get("status") == "failed":
            parts.append("## Error Analysis\n")
            parts.append("The code compiled but tests failed. Analyze the test failures:")
            parts.append("- Which test failed and why?")
            parts.append("- Is the logic correct but the assertion wrong?")
            parts.append("- Is there an edge case not handled?\n")
        elif error.get("status") == "error":
            parts.append("## Error Analysis\n")
            parts.append("The code has syntax or runtime errors. Fix the specific error:")
            parts.append("- Check imports (are they available?)")
            parts.append("- Check syntax (valid Python/JS/TS?)")
            parts.append("- Check runtime errors (None access, index out of range, etc.)\n")

        # Include error details
        parts.append("### Test Results\n")
        parts.append(f"**Status:** {error.get('status', 'unknown')}")
        parts.append(f"**Summary:** {error.get('summary', 'No summary')}\n")

        # Include per-test failures with tracebacks
        test_results = error.get("test_results", [])
        failures = [t for t in test_results if t.get("status") in ("failed", "error")]
        if failures:
            parts.append("### Failed Tests\n")
            for t in failures:
                parts.append(f"- **{t.get('name', 'unknown')}** ({t.get('status')})")
                if t.get("traceback"):
                    parts.append(f"  ```\n  {t['traceback']}\n  ```")
                elif t.get("error"):
                    parts.append(f"  Error: {t['error']}")
            parts.append("")

        # Include the code that failed so the LLM can fix it
        parts.append("### Previous Code That Failed\n")
        for fpath, content in previous_files.items():
            parts.append(f"**{fpath}:**")
            parts.append(f"```python\n{content}\n```\n")

        parts.append(
            "Fix the errors above and output the corrected code. "
            "Keep the same file paths and format."
        )

        return "\n".join(parts)

    # ── BeeLlama Inference ────────────────────────────────────────────

    def _run_inference(self, prompt: str, task_id: str = None) -> dict:
        """Call BeeLlama API via the transport layer.

        Sends a chat completion request to the BeeLlama endpoint.
        Uses transport.curl_beellama() which handles both local and remote execution.

        Args:
            prompt: The full prompt string (sent as a single user message).

        Returns:
            Dict with keys: content, reasoning_content, predicted_per_second,
            prompt_per_second, predicted_ms, prompt_ms, predicted_n,
            thinking_tokens, total_tokens, raw_response.
        """
        messages = [{"role": "user", "content": prompt}]

        try:
            _emit("engine", "inference.start", {
                "task_id": task_id,
                "port": self.port,
                "model": self.config or "unknown",
            })
        except Exception:
            pass

        try:
            response = self.transport.curl_beellama(
                self.port,
                messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        except RuntimeError as e:
            raise RuntimeError(f"BeeLlama request failed: {e}")

        # Extract content
        content = ""
        reasoning_content = ""
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            reasoning_content = message.get("reasoning_content", "")

        # Extract timings
        timings = response.get("timings", {})

        # Extract token counts
        usage = response.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)

        # Count thinking tokens
        thinking_tokens = timings.get("thinking_tokens", 0)
        if thinking_tokens == 0 and reasoning_content:
            thinking_tokens = len(reasoning_content) // 4

        try:
            _emit("engine", "inference.complete", {
                "task_id": task_id,
                "tok_s": timings.get("predicted_per_second", 0),
                "tokens_per_sec": timings.get("predicted_per_second", 0),  # dashboard alias
                "tokens": usage.get("total_tokens", 0),
                "prompt_ms": timings.get("prompt_ms", 0),
                "gen_ms": timings.get("predicted_ms", 0),
            })
        except Exception:
            pass

        # Emit cache efficiency event
        cache_n = timings.get("cache_n", 0)
        cache_lcp_n = timings.get("cache_lcp_n", 0)
        if cache_n > 0 or cache_lcp_n > 0:
            total_prompt = timings.get("prompt_n", 0) or (cache_n + cache_lcp_n + timings.get("cache_reprocessed_n", 0))
            hit_rate = cache_n / max(total_prompt, 1) if total_prompt > 0 else 0
            try:
                _emit("engine", "cache.update", {
                    "hit_rate": round(hit_rate, 3),
                    "cache_n": cache_n,
                    "cache_lcp_n": cache_lcp_n,
                })
            except Exception:
                pass

        return {
            "content": content,
            "reasoning_content": reasoning_content,
            "predicted_per_second": timings.get("predicted_per_second", 0.0),
            "prompt_per_second": timings.get("prompt_per_second", 0.0),
            "predicted_ms": timings.get("predicted_ms", 0.0),
            "prompt_ms": timings.get("prompt_ms", 0.0),
            "predicted_n": timings.get("predicted_n", 0),
            "thinking_tokens": thinking_tokens,
            "total_tokens": total_tokens,
            "raw_response": response,
        }

    # ── Code Extraction ───────────────────────────────────────────────

    def _extract_code(self, response: dict) -> Dict[str, str]:
        """Extract code blocks from LLM response.

        Uses the module-level extract_code function on the response content.

        Args:
            response: Dict from _run_inference with 'content' key.

        Returns:
            Dict mapping file paths to code content.
        """
        content = response.get("content", "")
        if not content:
            return {}

        files = extract_code(content)

        # Fallback: if no <file> tags found, try to detect a single code block
        if not files:
            # Look for a single markdown code block with a path hint
            single_pattern = r'```(?:python|typescript|javascript)?\s*\n(.*?)\n```'
            matches = re.findall(single_pattern, content, re.DOTALL)
            if matches and len(matches) == 1:
                # Use a default filename based on common conventions
                files["generated_code.py"] = matches[0].strip()

        return files

    # ── File Writing ──────────────────────────────────────────────────

    def _write_files(
        self, files: Dict[str, str], project_path: str, dry_run: bool = False,
    ) -> dict:
        """Write generated files to Triton via SSH.

        Creates parent directories as needed. Writes atomically via
        base64-encoded stdin pipe (large) or echo pipe (small).

        Args:
            files: Dict of {relative_path: content}.
            project_path: Absolute path to project root on Triton.
            dry_run: If True, return commands without executing.

        Returns:
            Dict with keys: success, files_written (list), errors (list),
            bytes_total, dry_run.
        """
        results: List[dict] = []
        errors: List[str] = []
        bytes_total = 0

        for rel_path, content in files.items():
            # Resolve full path — ensure it stays within project_path
            if rel_path.startswith("/"):
                # Absolute path: use as-is but warn
                full_path = rel_path
            else:
                full_path = f"{project_path.rstrip('/')}/{rel_path}"

            byte_len = len(content.encode("utf-8"))
            bytes_total += byte_len

            # Ensure parent directory exists
            parent_dir = full_path.rsplit("/", 1)[0]
            if parent_dir:
                mkdir_cmd = f"mkdir -p {_q(parent_dir)}"
                if dry_run:
                    results.append({
                        "path": full_path,
                        "bytes": byte_len,
                        "dry_run": True,
                        "mkdir_command": mkdir_cmd,
                    })
                    continue

                _, _, mkdir_rc = self.transport.run_command(mkdir_cmd)
                if mkdir_rc != 0:
                    errors.append(f"Failed to create directory: {parent_dir}")
                    continue

            # Write file atomically via echo + base64 pipe
            tmp_path = f"{full_path}.tmp.{os.getpid()}"
            encoded = __import__("base64").b64encode(
                content.encode("utf-8")
            ).decode("ascii")
            write_cmd = (
                f"echo '{encoded}' | base64 -d > {_q(tmp_path)} "
                f"&& mv {_q(tmp_path)} {_q(full_path)}"
            )

            if dry_run:
                results.append({
                    "path": full_path,
                    "bytes": byte_len,
                    "dry_run": True,
                    "write_command": f"<base64-echo ({byte_len} bytes)> | base64 -d",
                })
                continue

            _, _, write_rc = self.transport.run_command(write_cmd, timeout=120)

            if write_rc != 0:
                # Cleanup tmp file
                self.transport.run_command(f"rm -f {_q(tmp_path)}", timeout=10)
                errors.append(f"Failed to write file: {full_path}")
            else:
                results.append({
                    "path": full_path,
                    "bytes": byte_len,
                    "success": True,
                })

        return {
            "success": len(errors) == 0,
            "files_written": results,
            "errors": errors,
            "bytes_total": bytes_total,
            "dry_run": dry_run,
        }

    # ── Test Execution ────────────────────────────────────────────────

    def _run_tests(self, project_path: str) -> dict:
        """Run tests on generated code via the test_runner pattern.

        Tries pytest first, falls back to unittest discovery.

        Args:
            project_path: Absolute path to the project on Triton.

        Returns:
            Structured test result dict with keys: status, total, passed,
            failed, error, skipped, duration_seconds, test_results, summary.
        """
        start = time.monotonic()

        # Check if pytest is available
        check_cmd = "python3 -m pytest --version 2>&1"
        check_out, _, check_rc = self.transport.run_command(check_cmd, timeout=15)
        has_pytest = check_rc == 0 and "pytest" in check_out.lower()

        if has_pytest:
            remote_cmd = (
                f"cd {_q(project_path)} && "
                f"python3 -m pytest -v --tb=short 2>&1"
            )
        else:
            remote_cmd = (
                f"cd {_q(project_path)} && "
                f"python3 -m unittest discover -s . -p 'test_*.py' -v 2>&1"
            )

        stdout, stderr, rc = self.transport.run_command(remote_cmd, timeout=120)

        combined = stdout
        if stderr and stderr not in stdout:
            combined = stdout + "\n" + stderr

        # Parse the output
        if has_pytest:
            result = _parse_pytest_output(combined)
        else:
            result = _parse_unittest_output(combined)

        result["duration_seconds"] = round(time.monotonic() - start, 2)

        if result["total"] == 0:
            result["status"] = "error"
            if not result["summary"] or result["summary"] == "No tests collected":
                result["summary"] = _nonempty_or(combined, "No tests collected")

        return result

    # ── Error Recovery ────────────────────────────────────────────────

    def _retry_with_error(
        self, task: dict, error: dict, previous_files: Dict[str, str],
        attempt: int, dry_run: bool = False,
    ) -> dict:
        """Build retry prompt, run inference, extract, write, and test.

        Args:
            task: Original task dict.
            error: Error dict from _run_tests.
            previous_files: Files from the failed attempt.
            attempt: Current retry attempt number.
            dry_run: If True, return plan without executing.

        Returns:
            Result dict from the retry attempt.
        """
        retry_prompt = self._build_retry_prompt(task, error, previous_files, attempt)

        if dry_run:
            return {
                "status": "dry_run",
                "attempt": attempt,
                "retry_prompt_preview": retry_prompt[:500] + "..." if len(retry_prompt) > 500 else retry_prompt,
                "previous_error": error.get("summary", ""),
            }

        # Run inference with retry prompt
        inference_result = self._run_inference(retry_prompt, task_id=task.get("id"))

        # Extract code
        files = self._extract_code(inference_result)
        if not files:
            return {
                "status": "failed",
                "attempt": attempt,
                "error": "No code blocks extracted from retry response",
                "raw_response_preview": inference_result.get("content", "")[:500],
            }

        return {
            "status": "retry_attempt",
            "attempt": attempt,
            "files": files,
            "inference_result": {
                "tokens": inference_result.get("total_tokens", 0),
                "thinking_tokens": inference_result.get("thinking_tokens", 0),
                "predicted_per_second": inference_result.get("predicted_per_second", 0.0),
                "predicted_ms": inference_result.get("predicted_ms", 0.0),
            },
        }

    # ── Main Pipeline ─────────────────────────────────────────────────

    def generate(
        self,
        task: dict,
        context: dict = None,
        project_path: str = "/home/<user>/projects/default",
        run_tests: bool = True,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> dict:
        """Full pipeline: prompt -> inference -> extract -> write -> test -> retry.

        Args:
            task: Task dict with keys: title, description, acceptance_criteria,
                  files_to_create, files_to_modify.
            context: Optional context dict with keys: project_path, project_tree,
                     existing_files.
            project_path: Absolute path to project on Triton.
            run_tests: Whether to run tests after writing files.
            dry_run: If True, show what would happen without executing.
            verbose: If True, print progress to stderr.

        Returns:
            Result dict with keys:
                status: "success" | "failed" | "dry_run"
                files: Dict of {path: content} generated
                write_result: Result from _write_files
                test_result: Result from _run_tests (if run)
                attempts: Number of attempts taken
                inference_metrics: Timing and token info
                error: Error message if failed
        """
        start_time = time.monotonic()
        attempts = 0
        previous_files: Dict[str, str] = {}
        last_error: Optional[dict] = None

        for attempt in range(1, self.max_retries + 1):
            attempts = attempt

            if verbose:
                print(
                    f"[code_generator] Attempt {attempt}/{self.max_retries}",
                    file=sys.stderr,
                )

            # Build prompt
            if attempt == 1:
                prompt = self._build_prompt(task, context)
            else:
                # Subsequent attempts use retry prompt (built in _retry_with_error)
                retry_info = self._retry_with_error(
                    task, last_error, previous_files, attempt, dry_run=dry_run,
                )
                if retry_info.get("status") == "dry_run":
                    return {
                        "status": "dry_run",
                        "attempts": attempts,
                        "plan": {
                            "prompt": self._build_prompt(task, context),
                            "retry_prompt": retry_info.get("retry_prompt_preview", ""),
                            "project_path": project_path,
                            "run_tests": run_tests,
                        },
                    }
                elif retry_info.get("status") == "failed":
                    return {
                        "status": "failed",
                        "attempts": attempts,
                        "error": retry_info.get("error", "Unknown error"),
                        "files": {},
                    }
                else:
                    # Retry succeeded in getting code
                    previous_files = retry_info.get("files", {})
                    # Now write and test these files
                    write_result = self._write_files(
                        previous_files, project_path, dry_run=dry_run,
                    )
                    if dry_run:
                        return {
                            "status": "dry_run",
                            "attempts": attempts,
                            "write_result": write_result,
                        }
                    if not write_result["success"]:
                        last_error = {
                            "status": "failed",
                            "summary": f"File write errors: {'; '.join(write_result['errors'])}",
                            "test_results": [],
                        }
                        continue

                    if run_tests:
                        test_result = self._run_tests(project_path)
                        if test_result["status"] == "passed":
                            return {
                                "status": "success",
                                "attempts": attempts,
                                "files": previous_files,
                                "write_result": write_result,
                                "test_result": test_result,
                                "inference_metrics": retry_info.get("inference_result", {}),
                                "elapsed_seconds": round(time.monotonic() - start_time, 2),
                            }
                        else:
                            last_error = test_result
                            continue
                    else:
                        return {
                            "status": "success",
                            "attempts": attempts,
                            "files": previous_files,
                            "write_result": write_result,
                            "inference_metrics": retry_info.get("inference_result", {}),
                            "elapsed_seconds": round(time.monotonic() - start_time, 2),
                        }
                # If we get here, the retry path continued — loop will handle next retry
                continue

            # First attempt: run inference
            if dry_run:
                return {
                    "status": "dry_run",
                    "attempts": attempts,
                    "plan": {
                        "prompt": prompt[:500] + "..." if len(prompt) > 500 else prompt,
                        "project_path": project_path,
                        "run_tests": run_tests,
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "port": self.port,
                    },
                }

            try:
                inference_result = self._run_inference(prompt, task_id=task.get("id"))
            except (RuntimeError, ValueError) as e:
                last_error = {
                    "status": "failed",
                    "summary": f"Inference error: {e}",
                    "test_results": [],
                }
                if verbose:
                    print(f"[code_generator] Inference error: {e}", file=sys.stderr)
                continue

            # Extract code
            files = self._extract_code(inference_result)
            if not files:
                last_error = {
                    "status": "failed",
                    "summary": "No code blocks extracted from LLM response",
                    "test_results": [],
                }
                if verbose:
                    print(
                        "[code_generator] No code blocks in response",
                        file=sys.stderr,
                    )
                continue

            previous_files = files

            if verbose:
                print(
                    f"[code_generator] Extracted {len(files)} file(s): "
                    + ", ".join(files.keys()),
                    file=sys.stderr,
                )

            # Write files
            write_result = self._write_files(files, project_path, dry_run=dry_run)
            if not write_result["success"]:
                last_error = {
                    "status": "failed",
                    "summary": f"File write errors: {'; '.join(write_result['errors'])}",
                    "test_results": [],
                }
                continue

            # Run tests
            if run_tests:
                test_result = self._run_tests(project_path)

                if test_result["status"] == "passed":
                    return {
                        "status": "success",
                        "attempts": attempts,
                        "files": files,
                        "write_result": write_result,
                        "test_result": test_result,
                        "inference_metrics": {
                            "tokens": inference_result.get("total_tokens", 0),
                            "thinking_tokens": inference_result.get("thinking_tokens", 0),
                            "predicted_per_second": inference_result.get("predicted_per_second", 0.0),
                            "predicted_ms": inference_result.get("predicted_ms", 0.0),
                            "prompt_ms": inference_result.get("prompt_ms", 0.0),
                        },
                        "elapsed_seconds": round(time.monotonic() - start_time, 2),
                    }
                else:
                    last_error = test_result
                    if verbose:
                        print(
                            f"[code_generator] Tests failed (attempt {attempt}): "
                            f"{test_result.get('summary', 'unknown')}",
                            file=sys.stderr,
                        )
                    continue
            else:
                # Tests skipped — success
                return {
                    "status": "success",
                    "attempts": attempts,
                    "files": files,
                    "write_result": write_result,
                    "inference_metrics": {
                        "tokens": inference_result.get("total_tokens", 0),
                        "thinking_tokens": inference_result.get("thinking_tokens", 0),
                        "predicted_per_second": inference_result.get("predicted_per_second", 0.0),
                        "predicted_ms": inference_result.get("predicted_ms", 0.0),
                        "prompt_ms": inference_result.get("prompt_ms", 0.0),
                    },
                    "elapsed_seconds": round(time.monotonic() - start_time, 2),
                }

        # All retries exhausted
        return {
            "status": "failed",
            "attempts": attempts,
            "error": f"All {self.max_retries} attempts failed. "
                     f"Last error: {last_error.get('summary', 'unknown') if last_error else 'no error captured'}",
            "last_test_result": last_error,
            "files": previous_files,
            "elapsed_seconds": round(time.monotonic() - start_time, 2),
        }


# ---------------------------------------------------------------------------
# Output Parsers (shared with test_runner.py patterns)
# ---------------------------------------------------------------------------

_RE_PYTEST_LINE = re.compile(
    r'^(?P<file>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAILED|XPASSED)'
    r'(?:\s+\[\s*\d+%\])?\s*$'
)
_RE_PYTEST_SUMMARY = re.compile(
    r'^=+\s*(.+?)\s+in\s+([\d.]+)s\s*=+\s*$'
)
_RE_COUNT = re.compile(
    r'(\d+)\s+(passed|failed|error|skipped|xfailed|xpassed|warnings)\b'
    r'(?=[,\n]|$)'
)
_RE_TRACEBACK_START = re.compile(r'^(={3,}\s*)?(FAILURES|ERRORS)(\s*={3,})?$')
_RE_TRACEBACK_END = re.compile(r'^-{5,}\s*$')

_RE_UNITTEST_LINE = re.compile(
    r'^(test_\w+)\s+\((\S+)\)\s+\.\.\.\s+'
    r'(ok|FAIL|ERROR|skippedR?\s*(?:\(.*\))?|expectedFail|unexpectedSuccess)\s*$'
)
_RE_UNITTEST_RAN = re.compile(r'^Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s')
_RE_UNITTEST_VERDICT = re.compile(r'^(OK|FAILED|ERROR)\s*(?:\((.+)\))?$')


def _extract_test_name(test_id: str) -> str:
    parts = test_id.split("::")
    if len(parts) >= 3:
        return "::".join(parts[1:])
    return parts[-1] if parts else test_id


def _nonempty_or(text: str, default: str) -> str:
    stripped = text.strip()
    return stripped if stripped else default


def _parse_pytest_output(output: str) -> dict:
    """Parse pytest -v output into structured results."""
    lines = output.split("\n")
    test_results: List[dict] = []
    passed = failed = error = skipped = xfailed = xpassed = 0
    duration = 0.0
    summary_text = ""

    in_traceback = False
    current_traceback: List[str] = []

    for line in lines:
        stripped = line.strip()

        if _RE_TRACEBACK_START.match(stripped):
            in_traceback = True
            current_traceback = []
            continue
        if in_traceback and _RE_TRACEBACK_END.match(stripped):
            if current_traceback and test_results:
                for tr in reversed(test_results):
                    if tr["status"] in ("failed", "error") and "traceback" not in tr:
                        tr["traceback"] = "\n".join(current_traceback)
                        break
            in_traceback = False
            current_traceback = []
            continue
        if in_traceback:
            current_traceback.append(line)
            continue

        m = _RE_PYTEST_LINE.match(stripped)
        if m:
            test_id = m.group("file")
            status_raw = m.group("status").lower()
            name = _extract_test_name(test_id)
            file_path = test_id.split("::")[0] if "::" in test_id else test_id

            status_map = {
                "passed": "passed", "failed": "failed", "error": "error",
                "skipped": "skipped", "xfailed": "skipped", "xpassed": "passed",
            }
            status = status_map.get(status_raw, status_raw)

            tr: dict = {"name": name, "file": file_path, "status": status}
            test_results.append(tr)

            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
            elif status == "error":
                error += 1
            elif status == "skipped":
                skipped += 1
            continue

        sm = _RE_PYTEST_SUMMARY.match(stripped)
        if sm:
            summary_text = sm.group(1).strip()
            try:
                duration = float(sm.group(2))
            except ValueError:
                duration = 0.0
            for cm in _RE_COUNT.finditer(summary_text):
                count = int(cm.group(1))
                kind = cm.group(2).lower()
                if kind == "passed":
                    passed = count
                elif kind == "failed":
                    failed = count
                elif kind == "error":
                    error = count
                elif kind in ("skipped", "xfailed"):
                    skipped += count
                elif kind == "xpassed":
                    passed += count

    for tr in test_results:
        if tr["status"] == "failed":
            tr.setdefault("error", f"Test {tr['name']} failed")
        elif tr["status"] == "error":
            tr.setdefault("error", f"Test {tr['name']} raised an error")

    total = passed + failed + error + skipped
    if not summary_text:
        parts = []
        if passed:
            parts.append(f"{passed} passed")
        if failed:
            parts.append(f"{failed} failed")
        if error:
            parts.append(f"{error} error")
        if skipped:
            parts.append(f"{skipped} skipped")
        summary_text = ", ".join(parts) + f" in {duration:.1f}s" if parts else "No tests collected"

    if failed > 0 or error > 0:
        overall_status = "failed"
    elif total == 0:
        overall_status = "error"
    else:
        overall_status = "passed"

    return {
        "status": overall_status,
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "duration_seconds": duration,
        "test_results": test_results,
        "summary": summary_text,
    }


def _parse_unittest_output(output: str) -> dict:
    """Parse unittest output into structured results."""
    lines = output.split("\n")
    test_results: List[dict] = []
    passed = failed = error = skipped = 0
    duration = 0.0

    for line in lines:
        stripped = line.strip()

        m = _RE_UNITTEST_LINE.match(stripped)
        if m:
            test_name = m.group(1)
            test_class = m.group(2)
            result_raw = m.group(3).strip()

            status = "passed"
            tr: dict = {
                "name": f"{test_class}::{test_name}",
                "file": test_class.rsplit(".", 1)[0] if "." in test_class else test_class,
                "status": status,
            }

            if result_raw == "ok" or result_raw.startswith("expectedFail"):
                status = "passed"
            elif result_raw == "FAIL":
                status = "failed"
                failed += 1
            elif result_raw == "ERROR":
                status = "error"
                error += 1
            elif "skipped" in result_raw.lower():
                status = "skipped"
                skipped += 1
            elif result_raw == "unexpectedSuccess":
                status = "passed"
            else:
                status = "passed"

            if status == "passed":
                passed += 1

            tr["status"] = status
            test_results.append(tr)
            continue

        m_ran = _RE_UNITTEST_RAN.match(stripped)
        if m_ran:
            try:
                duration = float(m_ran.group(2))
            except ValueError:
                duration = 0.0
            continue

        m_verdict = _RE_UNITTEST_VERDICT.match(stripped)
        if m_verdict:
            detail = m_verdict.group(2) or ""
            if detail:
                for part in detail.split(","):
                    part = part.strip()
                    kv = part.split("=", 1)
                    if len(kv) == 2:
                        key = kv[0].strip()
                        val = int(kv[1].strip())
                        if key == "errors":
                            error = val
                        elif key == "failures":
                            failed = val
                        elif key == "skipped":
                            skipped = val

    total = passed + failed + error + skipped
    parts = []
    if passed:
        parts.append(f"{passed} passed")
    if failed:
        parts.append(f"{failed} failed")
    if error:
        parts.append(f"{error} error")
    if skipped:
        parts.append(f"{skipped} skipped")
    summary_text = ", ".join(parts) + f" in {duration:.1f}s" if parts else "No tests collected"

    if failed > 0 or error > 0:
        overall_status = "failed"
    elif total == 0:
        overall_status = "error"
    else:
        overall_status = "passed"

    return {
        "status": overall_status,
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "duration_seconds": duration,
        "test_results": test_results,
        "summary": summary_text,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_generator",
        description="ACE-04: Code Generation Loop — generate, write, and test code on Triton",
    )
    parser.add_argument(
        "--host", default=TRITON_HOST,
        help=f"Triton host (default: {TRITON_HOST})",
    )
    parser.add_argument(
        "--user", default=TRITON_USER,
        help=f"SSH user (default: {TRITON_USER})",
    )
    parser.add_argument(
        "--port", type=int, default=TRITON_PORT,
        help=f"BeeLlama port (default: {TRITON_PORT})",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=8192,
        help="Max tokens for generation (default: 8192)",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.3,
        help="Sampling temperature (default: 0.3)",
    )
    parser.add_argument(
        "--project", default="/home/<user>/projects/default",
        help="Project path on Triton (default: /home/<user>/projects/default)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without executing",
    )
    parser.add_argument(
        "--no-tests", action="store_true",
        help="Skip test execution after writing files",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print progress to stderr",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- generate ---
    gen_p = sub.add_parser("generate", help="Generate code from a task")
    gen_p.add_argument(
        "--task", type=str, default=None,
        help="Task as JSON string",
    )
    gen_p.add_argument(
        "--task-file", type=str, default=None,
        help="Path to task JSON file",
    )
    gen_p.add_argument(
        "--context-file", type=str, default=None,
        help="Path to context JSON file (project tree, existing files)",
    )

    # --- extract ---
    ext_p = sub.add_parser("extract", help="Extract code blocks from a response file")
    ext_p.add_argument(
        "response_file",
        help="Path to file containing LLM response with <file> tags",
    )

    # --- prompt ---
    prompt_p = sub.add_parser("prompt", help="Build and print the prompt without running inference")
    prompt_p.add_argument(
        "--task", type=str, default=None,
        help="Task as JSON string",
    )
    prompt_p.add_argument(
        "--task-file", type=str, default=None,
        help="Path to task JSON file",
    )
    prompt_p.add_argument(
        "--context-file", type=str, default=None,
        help="Path to context JSON file",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    generator = CodeGenerator(
        host=args.host,
        user=args.user,
        port=args.port,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )

    try:
        if args.command == "generate":
            # Load task
            task = _load_json_arg(args.task, args.task_file, "--task")
            if task is None:
                print(
                    json.dumps({"error": "Provide --task (JSON string) or --task-file"}),
                    file=sys.stderr,
                )
                sys.exit(1)

            # Load optional context
            context = None
            if args.context_file:
                with open(args.context_file, "r", encoding="utf-8") as f:
                    context = json.load(f)

            result = generator.generate(
                task=task,
                context=context,
                project_path=args.project,
                run_tests=not args.no_tests,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            print(json.dumps(result, indent=2))

            # Exit non-zero on failure
            if result.get("status") == "failed":
                sys.exit(1)

        elif args.command == "extract":
            files = extract_code_from_file(args.response_file)
            if not files:
                print(
                    json.dumps({"error": "No code blocks found in response"}),
                    file=sys.stderr,
                )
                sys.exit(1)
            print(json.dumps(files, indent=2))

        elif args.command == "prompt":
            task = _load_json_arg(args.task, args.task_file, "--task")
            if task is None:
                print(
                    json.dumps({"error": "Provide --task (JSON string) or --task-file"}),
                    file=sys.stderr,
                )
                sys.exit(1)

            context = None
            if args.context_file:
                with open(args.context_file, "r", encoding="utf-8") as f:
                    context = json.load(f)

            prompt = generator._build_prompt(task, context)
            print(prompt)

    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(json.dumps({"error": "Interrupted"}), file=sys.stderr)
        sys.exit(130)


def _load_json_arg(
    json_str: Optional[str], file_path: Optional[str], flag_name: str,
) -> Optional[dict]:
    """Load a JSON value from either a string argument or a file path."""
    if json_str:
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {flag_name}: {e}")
    elif file_path:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


if __name__ == "__main__":
    main()
