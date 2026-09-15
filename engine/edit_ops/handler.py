"""EditTaskHandler — orchestrates the edit-ops pipeline.

Pipeline: generate (LLM) -> extract (EditOpExtractor) -> staged
pre-checks (stages 0, 1, 5) -> apply_blocks (pure seam, stage 3) ->
post-apply ast check (stage 4) -> write -> commit-gate read-back ->
commit_code -> TaskResult.

The generate step mirrors engine/research/verdict.py VerdictGenerator: build
a prompt describing the edit task, call transport.curl_beellama(), capture
the raw response.  Extraction, validation, and apply are fully
deterministic (no LLM/network).
"""

from __future__ import annotations

import ast
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from engine.edit_ops.extract import EditOpExtractor, EditOp
from engine.edit_ops.apply import apply_blocks
from engine.edit_ops.schema import EditErrorContext

log = logging.getLogger("engine.edit_ops.handler")


@dataclass
class EditGenerateResult:
    """Result of the edit generate step (mirrors GeneratedVerdict shape)."""
    raw_response: str
    tokens: int = 0
    thinking_tokens: int = 0
    latency_ms: float = 0.0
    role: str = "coder"
    max_tokens_used: int = 0


class EditTaskHandler:
    """Handler for ``task_type='edit'``.  Orchestrates the full edit pipeline."""

    def __init__(self, engine: object) -> None:
        self.engine = engine

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------
    def _generate(self, task: object, error_ctx: Optional[EditErrorContext] = None) -> EditGenerateResult:
        """Build an edit prompt and call the LLM via transport.

        Mirrors VerdictGenerator.generate() (engine/research/verdict.py).
        """
        from engine import task_role as _task_role

        config = getattr(self.engine, "config", None)
        transport = getattr(self.engine, "transport", None)

        prompt_text = self._build_prompt(task, error_ctx)

        role = _task_role(task)
        max_tokens = int(
            getattr(config, "max_tokens_by_role", {}).get(role, 4096)
        ) if config is not None else 4096

        messages = [{"role": "user", "content": prompt_text}]

        raw_response = ""
        tokens = 0
        thinking_tokens = 0
        latency_ms = 0.0

        if transport is not None:
            port = 8080
            try:
                start = time.time()
                response = transport.curl_beellama(
                    port, messages, max_tokens=max_tokens, temperature=0.2)
                latency_ms = (time.time() - start) * 1000
                raw_response = response.get("content", "") or ""
                tokens = response.get("total_tokens", 0)
                thinking_tokens = response.get("thinking_tokens", 0)
            except Exception as e:  # noqa: BLE001 — transport failure
                log.warning("edit generate transport failure: %s", e)
                raw_response = ""

        return EditGenerateResult(
            raw_response=raw_response,
            tokens=tokens,
            thinking_tokens=thinking_tokens,
            latency_ms=latency_ms,
            role=role,
            max_tokens_used=max_tokens,
        )

    def _build_prompt(self, task: object, error_ctx: Optional[EditErrorContext] = None) -> str:
        """Build the edit-task prompt (Aider SEARCH/REPLACE fence format)."""
        title = getattr(task, "title", "") or getattr(task, "id", "edit")
        description = getattr(task, "description", "") or ""
        module = getattr(task, "module", "") or ""

        prompt = (
            f"# Edit Task\n\n"
            f"## File\n{module}\n\n"
            f"## Title\n{title}\n\n"
            f"## Description\n{description}\n\n"
            f"## Instructions\n\n"
            f"You are a precise code editor. Produce SEARCH/REPLACE edits "
            f"to the file `{module}` that accomplish the task above.\n\n"
            f"Use EXACTLY this format for each edit block:\n\n"
            f"```\n"
            f"<<<<<<< SEARCH\n"
            f"<exact text to find in the file>\n"
            f"=======\n"
            f"<replacement text>\n"
            f">>>>>>> REPLACE\n"
            f"```\n\n"
            f"RULES:\n"
            f"1. The SEARCH block must match the target text EXACTLY "
            f"(including whitespace).\n"
            f"2. Output one or more SEARCH/REPLACE blocks.\n"
            f"3. Do NOT include any commentary outside the fence blocks.\n"
        )

        if error_ctx is not None:
            errs = error_ctx.validation_errors or []
            if errs:
                prompt += "\n## Previous attempt feedback\n"
                prompt += "A previous edit was rejected. Address these issues:\n"
                for err in errs:
                    prompt += f"- {err}\n"
        return prompt

    # ------------------------------------------------------------------
    # Single-attempt body (extracted from the retry loop for flatness)
    # ------------------------------------------------------------------
    def _try_once(
        self,
        task: object,
        attempt: int,
        error_ctx: Optional[EditErrorContext],
        project_path: str,
        run_id: str,
        task_id: str,
        title: str,
        task_start: float,
        task_tokens: int,
    ) -> Tuple[Optional[object], Optional[EditErrorContext]]:
        """Execute one attempt of the edit pipeline.

        Returns:
            (TaskResult, None) on a terminal outcome (COMMIT or non-retryable
            failure such as missing file / path traversal / write error).
            (None, EditErrorContext) on a retryable validation/apply failure —
            the returned context feeds the next attempt's prompt.
        """
        from engine.engine import TaskResult, State

        # 1. Generate raw LLM response.
        gen_result = self._generate(task, error_ctx=error_ctx)
        task_tokens += gen_result.tokens

        # 2. Extract edit blocks.
        target_file = getattr(task, "module", "") or ""
        edit_op: EditOp = EditOpExtractor.extract(
            gen_result.raw_response, target_file)

        # 3. Build the dict form for apply_blocks.
        edit_dict = {
            "target_file": edit_op.target_file,
            "blocks": [
                {"path": b.path, "old_string": b.old_string, "new_string": b.new_string}
                for b in edit_op.blocks
            ],
        }

        root_dir = Path(project_path)
        target_path = root_dir / target_file
        blocks_list = edit_dict["blocks"]

        # --- Stage 5: path traversal (runs BEFORE any filesystem touch or
        #                block validation — a malicious path must never reach
        #                the disk even if extraction yielded no blocks). ---
        if ".." in target_file or os.path.isabs(target_file) or target_file.startswith("~"):
            return TaskResult(
                task_id=task_id, title=title,
                state=State.FAILED.value, attempts=attempt,
                commit_sha=None, time_s=time.time() - task_start,
                tokens=task_tokens,
                error_message=f"path traversal rejected: {target_file}",
            ), None

        # --- Stage 0: blocks non-empty. ---
        if not blocks_list:
            return None, EditErrorContext(
                previous_edit_op=edit_op,
                validation_errors=["no edit blocks found"],
                attempt_number=attempt,
            )

        # --- Stage 1: each block has non-empty old_string. ---
        for i, b in enumerate(blocks_list):
            if not b.get("old_string"):
                return None, EditErrorContext(
                    previous_edit_op=edit_op,
                    validation_errors=[f"block {i}: empty old_string"],
                    attempt_number=attempt,
                )

        # --- Stage 2: target file exists. ---
        if not target_path.exists():
            return TaskResult(
                task_id=task_id, title=title,
                state=State.FAILED.value, attempts=attempt,
                commit_sha=None, time_s=time.time() - task_start,
                tokens=task_tokens,
                error_message=f"target file not found: {target_file}",
            ), None

        # Read original file content.
        file_content = target_path.read_text()

        # --- Stage 3 + apply via the pure seam (exact-match + atomic apply) ---
        new_content, apply_errors = apply_blocks(file_content, blocks_list)
        if new_content is None:
            log.warning("edit task %s attempt %d apply failed: %s",
                        task_id, attempt, apply_errors)
            return None, EditErrorContext(
                previous_edit_op=edit_op,
                validation_errors=apply_errors,
                attempt_number=attempt,
            )

        # --- Stage 4: post-apply ast.parse for .py files ---
        if target_path.suffix == ".py":
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                return None, EditErrorContext(
                    previous_edit_op=edit_op,
                    validation_errors=[f"post-apply syntax error: {e}"],
                    attempt_number=attempt,
                )

        # 5. Write file via transport (committer seam).
        full_path = f"{project_path}/{target_file}"
        try:
            from engine.committer import write_project_file
            transport = getattr(self.engine, "transport", None)
            if transport is not None:
                write_project_file(transport, full_path, new_content)
        except Exception as e:  # noqa: BLE001
            return TaskResult(
                task_id=task_id, title=title,
                state=State.FAILED.value, attempts=attempt,
                commit_sha=None, time_s=time.time() - task_start,
                tokens=task_tokens,
                error_message=f"edit write failed: {e}",
            ), None

        # 6. Commit-gate read-back (Gitea #44): verify disk == applied.
        try:
            transport = getattr(self.engine, "transport", None)
            if transport is not None:
                read_back = self._read_back(transport, full_path)
                if read_back != new_content:
                    return TaskResult(
                        task_id=task_id, title=title,
                        state=State.FAILED.value, attempts=attempt,
                        commit_sha=None, time_s=time.time() - task_start,
                        tokens=task_tokens,
                        error_message=(
                            "commit-gate: written content != applied content"
                        ),
                    ), None
        except Exception as e:  # noqa: BLE001
            log.warning("edit task %s commit-gate read-back error: %s",
                        task_id, e)

        # 7. Commit.
        commit_sha = None
        try:
            from engine.committer import commit_code
            transport = getattr(self.engine, "transport", None)
            if transport is not None:
                result = commit_code(
                    project_path, task, new_content, transport,
                    run_id=run_id)
                commit_sha = result.sha
        except Exception as e:  # noqa: BLE001
            log.warning("edit task %s commit error: %s", task_id, e)

        # S2: record the successful attempt's edit-op metrics.
        # applied=True, match_count=None on success (match_count is only
        # populated on multi-match / zero-match failures).
        try:
            from engine.edit_ops.metrics import record_edit_op_result
            record_edit_op_result(
                run_id=run_id, task_id=task_id, attempt=attempt,
                applied=True, match_count=None)
        except Exception as exc:  # noqa: BLE001 — telemetry must not break pipeline
            log.warning("record_edit_op_result failed (non-blocking): %s", exc)

        return TaskResult(
            task_id=task_id, title=title,
            state=State.COMMIT.value, attempts=attempt,
            commit_sha=commit_sha,
            time_s=time.time() - task_start,
            tokens=task_tokens,
            error_message=None,
            role="coder",
        ), None

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self, task: object, pipeline: object, project_path: str) -> object:
        """generate -> extract -> validate -> apply -> write -> commit-gate -> TaskResult."""
        from engine.engine import TaskResult, State

        task_id = getattr(task, "id", "T00")
        title = getattr(task, "title", "edit")
        run_id = getattr(pipeline, "run_id", "edit-run")
        task_start = time.time()
        task_tokens = 0
        max_retries = 3

        error_ctx: Optional[EditErrorContext] = None

        for attempt in range(1, max_retries + 1):
            result, error_ctx = self._try_once(
                task, attempt, error_ctx, project_path, run_id,
                task_id, title, task_start, task_tokens,
            )
            if result is not None:
                return result
            # Retryable failure (result is None) — record the failed attempt
            # so edit_op_results captures match_count=0 / applied=0 for the
            # first-apply-rate computation (S2).
            try:
                from engine.edit_ops.metrics import record_edit_op_result
                record_edit_op_result(
                    run_id=run_id, task_id=task_id, attempt=attempt,
                    applied=False, match_count=0)
            except Exception as exc:  # noqa: BLE001 — telemetry must not break
                log.warning("record_edit_op_result failed (non-blocking): %s", exc)

        # Retries exhausted.
        return TaskResult(
            task_id=task_id, title=title,
            state=State.FAILED.value, attempts=max_retries,
            commit_sha=None, time_s=time.time() - task_start,
            tokens=task_tokens,
            error_message=f"edit task {task_id}: retries exhausted",
        )

    @staticmethod
    def _read_back(transport: object, full_path: str) -> str:
        """Read a file back from the transport host for commit-gate verification."""
        stdout, _stderr, rc = transport.run_command(
            f"cat {full_path}", timeout=10)
        if rc != 0:
            raise RuntimeError(f"read-back failed: rc={rc}")
        return stdout
