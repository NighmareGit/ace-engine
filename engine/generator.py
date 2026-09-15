"""Engine code generation — build prompt, call BeeLlama, extract code blocks."""

import re
import time
from dataclasses import dataclass

from engine import Task, EngineConfig


def _is_reasoning_exhaustion(response: dict) -> bool:
    """Detect reasoning exhaustion: a thinking-style model that emits reasoning
    first and, at too-low max_tokens, returns empty content.

    Fires when content is empty AND the model produced reasoning (non-empty
    reasoning_content or thinking_tokens > 0).  finish_reason="length" is the
    classic signal, but some servers (e.g. llama-server with MTP heads) omit
    finish_reason entirely while still exhausting their token budget — so we
    also escalate when finish_reason is blank/missing (bounded to ONE retry
    by the caller in engine.py).
    """
    content = response.get("content", "") or ""
    if content.strip():
        return False
    has_reasoning = bool(response.get("reasoning_content")
                         or response.get("thinking_tokens", 0))
    if not has_reasoning:
        return False
    finish = response.get("finish_reason", "")
    # Epic-5: escalate on finish_reason="length" (classic) OR when
    # finish_reason is absent/empty (server-side omission — same symptom).
    return finish in ("length", "", None)


def _strip_stray_fences(code):
    """Belt-and-braces (cycle-3, run-863126): no file content should ever
    start with a markdown fence line — if one survives extraction, drop it
    (and a trailing fence) so AST validation sees pure code."""
    lines = code.split("\n")
    while lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def generate_code(context, task, config, transport, error_ctx=None,
                  max_tokens_override=None):
    """
    Build prompt, call BeeLlama, extract code blocks.

    Args:
        context: Context dataclass (from engine.context)
        task: Task dataclass
        config: EngineConfig dataclass
        transport: transport object with curl_beellama() method
        error_ctx: Optional ErrorContext for retry feedback
        max_tokens_override: Optional max_tokens escalation (P1 reasoning budget).

    Returns:
        GeneratedCode dataclass with files, raw_response, tokens, thinking_tokens, latency_ms
    """
    from engine.prompts import build_prd_result, build_project_context, compile_prompt, append_error_context

    # 1. Build prompt via engine/prompts.py
    prd_result = build_prd_result(task, context)
    project_context = build_project_context(context)
    prompt_text = compile_prompt(task.id, prd_result, project_context)

    # 2. If error_ctx, append error context to prompt
    if error_ctx is not None:
        prompt_text = append_error_context(prompt_text, error_ctx)

    # 2b. Environment constraints on EVERY attempt (cycle-3d: the model
    # picked flask / importlib_metadata — packages that do not exist in the
    # execution environment — and the whitelist was never stated).
    prompt_text += (
        "\n\n## Environment constraints (MANDATORY)\n"
        "Available packages: the Python standard library ONLY, plus: "
        "fastapi, uvicorn, pytest.\n"
        "Any other import WILL fail validation. Use standard-library "
        "equivalents (e.g. http.server instead of flask, "
        "importlib.metadata instead of importlib_metadata).\n"
        "Cross-task imports must use the module path from the PRD "
        "(e.g. `from api.health import x`, `from kvstore import KVStore`).\n")

    # D5: skills wiring — append matched skill docs as context. One seam,
    # already built (engine/intent/skills.py). If no skills dir / no match,
    # the prompt is byte-identical to the pre-D5 prompt.
    try:
        from engine.intent.skills import SkillStore
        _skills = SkillStore()  # uses DEFAULT_SKILLS_DIR
        _skill_ctx = _skills.context_for_prompt("multi-module-implementation")
        if not _skill_ctx:
            _skill_ctx = _skills.context_for_prompt("file-targeting-discipline")
        if _skill_ctx:
            prompt_text += "\n\n## Skill Context\n" + _skill_ctx + "\n"
    except Exception:  # noqa: BLE001 — skills must never crash generation
        pass

    # 3. Call BeeLlama via transport
    port = 8080 if config.model_config.startswith("3090") else 8082
    messages = [{"role": "user", "content": prompt_text}]
    # Epic-5: role-aware max_tokens (T08 — 2048 truncated orchestrator answers)
    from engine import task_role as _task_role
    role = _task_role(task)
    max_tokens = (max_tokens_override if max_tokens_override is not None
                  else int(getattr(config, "max_tokens_by_role", {}).get(role, 2048)))
    start = time.time()
    response = transport.curl_beellama(port, messages, max_tokens=max_tokens, temperature=0.3)
    latency_ms = (time.time() - start) * 1000

    # C1 FIX: transport returns FLAT dict — no nested "usage" key
    raw_response = response.get("content", "")
    tokens = response.get("total_tokens", 0)
    thinking_tokens = response.get("thinking_tokens", 0)
    # Epic-5 telemetry (AC5.3) — 0 when usage lacks the split (never fabricated)
    prompt_tokens = response.get("prompt_tokens", 0)
    completion_tokens = response.get("completion_tokens", 0)
    tokens_per_sec = response.get("predicted_per_second", 0.0)
    # P1: explicit finish_reason + reasoning_content for exhaustion detection
    finish_reason = response.get("finish_reason", "")
    reasoning_content = response.get("reasoning_content", "") or ""

    # --- Session log capture (OBS-01, additive) ---
    # Record every LLM call to session_logs + generation_telemetry.
    # Best-effort: wrapped in try/except so telemetry never breaks generation.
    _session_log_error = None
    if getattr(config, "session_log_recorder", None) is not None:
        try:
            recorder = config.session_log_recorder
            # Derive attempt from error_ctx if present, else 1.
            _attempt = (error_ctx.attempt_number if error_ctx is not None else 1)
            recorder.record(
                task_id=task.id,
                attempt=_attempt,
                stage="generate",
                model=config.model_config,
                port=port,
                prompt_text=prompt_text,
                response=response,
                latency_ms=latency_ms,
                role=role,
                max_tokens_used=max_tokens,
            )
        except Exception as _sl_exc:  # noqa: BLE001 — telemetry must not break pipeline
            _session_log_error = str(_sl_exc)

    # 4. Extract code blocks from response
    #    Two block formats tolerated:
    #    a) markdown fences:  ```lang\n<code>```
    #    b) XML file tags:    <file path="x.py">\n<code></file>
    #       (observed live on 9B-MTP, run-1788853635: the tag line became
    #       line 1 of the file → guaranteed AST failure)
    code_blocks = []  # list of (filename_or_None, code_text)
    for m in re.finditer(r'```[\w\-]*\s*\n(.*?)```', raw_response, re.DOTALL):
        code_blocks.append((None, m.group(1)))
    # Cycle-3 finding (run-1788861146): 9B-MTP sometimes emits an UNCLOSED
    # fence — no closing ``` — so the strict regex matched nothing and the
    # raw response (fence line included) became the file → line 1 = "```python".
    # Salvage: take everything after the opening fence line.
    if not code_blocks and re.search(r'```[\w\-]*\s*\n', raw_response):
        salvaged = re.split(r'```[\w\-]*\s*\n', raw_response, maxsplit=1)[1]
        if salvaged.rstrip().endswith("```"):
            salvaged = salvaged.rstrip()[:-3]
        code_blocks.append((None, salvaged))
    _xml_consumed = set()
    for m in re.finditer(
            r'<file\s+path=["\']([\w./\-]+\.[\w]+)["\']\s*>\s*\n(.*?)</file>',
            raw_response, re.DOTALL):
        code_blocks.append((m.group(1).lstrip("/"), m.group(2)))
        _xml_consumed.add(m.start())
    files = {}
    if code_blocks and task.module:
        named = [(f, b) for f, b in code_blocks if f]
        for i, (named_fname, block) in enumerate(code_blocks):
            block = block.strip()
            fname = named_fname
            if fname is None:
                first_line = block.split("\n", 1)[0]
                fname_match = re.match(r'#\s*(?:File:|filename:)?\s*([\w./\-]+\.[\w]+)\s*$', first_line)
                if fname_match:
                    fname = fname_match.group(1).lstrip("/")
                    block = block.split("\n", 1)[1] if "\n" in block else block
                if fname is None:
                    if i == 0:
                        fname = task.module
                    else:
                        base, ext = (task.module.rsplit(".", 1)
                                     if "." in task.module else (task.module, "py"))
                        fname = f"{base}_{i+1}.{ext}"
            fname = fname.replace("..", "_").lstrip("/")
            files[fname] = _strip_stray_fences(block)
    elif raw_response and task.module:
        files[task.module] = raw_response.strip()

    return GeneratedCode(
        files=files,
        raw_response=raw_response,
        tokens=tokens,
        thinking_tokens=thinking_tokens,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        tokens_per_sec=tokens_per_sec,
        role=role,
        finish_reason=finish_reason,
        reasoning_content=reasoning_content,
        exhausted=_is_reasoning_exhaustion(response),
        max_tokens_used=max_tokens,
    )


@dataclass
class GeneratedCode:
    """Result of code generation from BeeLlama."""
    files: dict          # filename -> code content (extracted, clean code)
    raw_response: str    # Full LLM response (including markdown, thinking)
    tokens: int
    thinking_tokens: int
    latency_ms: float
    prompt_tokens: int = 0        # Epic-5 AC5.3 telemetry
    completion_tokens: int = 0
    tokens_per_sec: float = 0.0
    role: str = "coder"           # resolved task role (max_tokens selection)
    finish_reason: str = ""       # P1 — explicit finish_reason (e.g. "length")
    reasoning_content: str = ""   # P1 — reasoning trace when thinking-style model
    exhausted: bool = False       # P1 — True when reasoning exhaustion detected
    max_tokens_used: int = 0      # P1 — max_tokens applied to this generation


@dataclass
class ErrorContext:
    """Error context for retry attempts."""
    previous_code: str | None
    validation_errors: list[str]
    test_failures: list[str]
    attempt_number: int
