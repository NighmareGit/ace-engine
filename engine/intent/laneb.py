"""Lane B+ executor — capped RLM bypass (code-as-intent with llm_query()).

Sits BEHIND lane A's escalation path (the pipeline routes to B+ only when lane
A misses AND lane_b_enabled). It is NOT the orchestrator — budgets are binding,
depth is binding, and it composes REGISTRY TOOLS only (ADR-0002: never edits
tasks/code/files).

Three-layer split (matches the sandbox module's discipline):
   types    — LaneBConfig / LaneBResult / LaneBEventType (engine/intent/types.py)
   runtime  — THIS FILE: the executor + the sandboxed-code harness
   protocol — code-gen prompt + parser (engine/intent/laneb_protocol.py)

The queued-intent mechanism (grill G3):
   The generated code may call ``ctx.llm_query(question)``. That call does NOT
   run inside the sandbox — instead the executor intercepts it: the question is
   written to an in-memory queue, the executor pauses the code, answers it
   OUTSIDE the sandbox via a separate LLM call to the judge model, then resumes
   the code with the answer injected. Depth <= 2: a queued answer's own
   follow-up may recurse once, no deeper — enforced in the executor.

Docker-only hard rule:
   Lane-B code runs ONLY via the docker backend. If docker is unavailable the
   executor REFUSES with a clear error + telemetry event — it NEVER falls back
   to the subprocess backend for untrusted code. (The subprocess backend exists
   in the sandbox module for registry-tool callables on a dev box; lane B does
   not use it.)

Budgets (binding):
   - max_llm_queries per execution (call-count budget, default 4)
   - max_wall_clock_ms overall wall-clock budget (default 30s)
   - max_depth for llm_query recursion (default 2)
   Exceeding any aborts with a partial result + telemetry. All lane-B LLM calls
   count against the engine budget counters (T8 contract) via the same
   increment_llm_calls site when a run_id is supplied.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Callable

from engine.intent.laneb_protocol import (
    build_code_gen_prompt, build_judge_prompt, parse_code_gen_response,
)
from engine.intent.sandbox import SandboxConfig, SandboxRunner, get_sandbox_runner
from engine.intent.types import LaneBConfig, LaneBEventType, LaneBResult

log = logging.getLogger("engine.intent.laneb")


class LaneBError(Exception):
    """Raised for lane-B executor-internal failures (never past the pipeline)."""


class _Ctx:
    """The ``ctx`` object exposed to generated lane-B code (spec §B2).

    Read-only access to intent params + run_id, and the two callable primitives:
       ctx.tool(name, **kwargs)  -> dispatch a registry tool (in-process)
       ctx.llm_query(question)   -> queue a question for the judge (queued intent)

    ``llm_query`` raises ``_QueueQuestion`` (a control-flow signal caught by the
    executor loop) — generated code never sees that exception type.
    """

    def __init__(
        self,
        params: dict[str, Any],
        run_id: str | None,
        tool_fn: Callable[..., Any],
        query_fn: Callable[[str], str],
    ) -> None:
        self._params = params
        self._run_id = run_id
        self._tool_fn = tool_fn
        self._query_fn = query_fn

    def param(self, name: str) -> Any:
        return self._params.get(name)

    def tool(self, name: str, **kwargs: Any) -> Any:
        return self._tool_fn(name, **kwargs)

    def llm_query(self, question: str) -> str:
        return self._query_fn(question)


class _QueueQuestion(Exception):
    """Control-flow signal: generated code called llm_query().

    Carries the question. Caught by the executor loop — never propagates to
    generated code.
    """

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


class LaneBExecutor:
    """Execute a complex/ambiguous intent via code-as-intent (lane B+).

    Receives an intent (text + params) that lane A escalated with a
    "complex/ambiguous" flag, or a direct call. Asks the subject model to write
    a SHORT ``run(ctx)`` function, executes it via the docker sandbox, and
    returns a LaneBResult.

    The executor is stateless across calls except for the per-call budget
    counters (fresh per execute()).
    """

    def __init__(
        self,
        config: LaneBConfig | None = None,
        registry_tools: dict[str, str] | None = None,
        sandbox_runner: SandboxRunner | None = None,
        # Injected LLM callables (port, payload) -> response dict. Tests pass
        # mocks; production passes a transport-bound closure.
        code_gen_caller: Callable[..., dict] | None = None,
        judge_caller: Callable[..., dict] | None = None,
        # Budget-counter integration (T8). Called as increment(tokens) -> dict
        # when a run_id is supplied. None disables counter integration.
        budget_increment: Callable[[int], dict] | None = None,
    ) -> None:
        self.config = config or LaneBConfig()
        self.tools = registry_tools or {}
        self._sandbox = sandbox_runner
        self._code_gen_caller = code_gen_caller
        self._judge_caller = judge_caller
        self._budget_increment = budget_increment
        # Per-execution harness injection state (set in execute()).
        self._current_run_id: str | None = None
        self._current_params: dict[str, Any] = {}

    # -- sandbox access (lazy) ------------------------------------------------

    @property
    def _runner(self) -> SandboxRunner:
        if self._sandbox is None:
            self._sandbox = get_sandbox_runner(
                SandboxConfig(
                    timeout_sec=self.config.sandbox_timeout_sec,
                    allow_fallback=not self.config.docker_required,
                )
            )
        return self._sandbox

    # -- public API -----------------------------------------------------------

    def execute(
        self,
        intent_text: str,
        intent_params: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> LaneBResult:
        """Run one lane-B+ execution.

        Returns a LaneBResult. Never raises past the pipeline: all failures
        (budget abort, docker refusal, code rejection) normalize into
        LaneBResult(ok=False, ...) with telemetry events.
        """
        t0 = time.perf_counter()
        intent_params = intent_params or {}
        events: list[str] = []
        # Stash for the harness to read (module-level injection).
        self._current_run_id = run_id
        self._current_params = intent_params

        # --- docker-only enforcement (hard rule) ----------------------------
        if self.config.docker_required and not self._runner.available:
            log.warning(
                "lane B+: docker backend unavailable — refusing "
                "(docker_required=True). intent=%r", intent_text[:120],
            )
            events.append(LaneBEventType.DOCKER_REFUSED.value)
            return LaneBResult(
                ok=False,
                error="lane B+ refused: docker backend unavailable "
                      "(untrusted code runs only in the docker sandbox)",
                events=events,
                latency_ms=self._ms(t0),
                run_id=run_id,
            )

        # --- 1. code-gen: ask the subject model to write run(ctx) ----------
        try:
            code = self._generate_code(intent_text, intent_params, run_id)
        except LaneBError as exc:
            events.append(LaneBEventType.CODE_REJECTED.value)
            return LaneBResult(
                ok=False,
                error=f"code-gen failed: {exc}",
                events=events,
                latency_ms=self._ms(t0),
                run_id=run_id,
            )
        events.append(LaneBEventType.CODE_ACCEPTED.value)

        # --- 2. execute run(ctx) in the sandbox with queued-intent support -
        # Single-shot-restart model: the harness runs the generated code ONCE.
        # tool()/llm_query() calls with no injected answer raise _Stop (halting
        # the code at the first unanswered primitive) and report all pending
        # requests. The executor fulfills them on the host (in order), then
        # re-runs the code with the answer list injected. Because the code is
        # deterministic, re-running reproduces the same prefix of answered
        # calls and stops at the next unanswered one. This repeats until the
        # code completes (kind=result) or a budget/depth cap is hit.
        query_count = 0
        tool_count = 0
        max_depth = 0
        partial = False
        value = None
        error: str | None = None
        answers: list[Any] = []         # accumulated answers across runs
        # Tool results accumulated so far (for judge context).
        tool_results: list[dict[str, Any]] = []

        deadline = (
            time.perf_counter() + self.config.max_wall_clock_ms / 1000.0
            if self.config.max_wall_clock_ms > 0
            else float("inf")
        )

        # Check wall-clock budget after code-gen too (a single-shot run can
        # exceed the budget before the loop's first check).
        if time.perf_counter() >= deadline:
            events.append(LaneBEventType.WALL_CLOCK_ABORT.value)
            return LaneBResult(
                ok=False, code=code, error="wall-clock budget exceeded",
                events=events, latency_ms=self._ms(t0), run_id=run_id,
            )

        while True:
            # Check wall-clock budget before each sandbox run.
            if time.perf_counter() >= deadline:
                events.append(LaneBEventType.WALL_CLOCK_ABORT.value)
                partial = value is not None
                error = error or "wall-clock budget exceeded"
                break

            # Build + run the harness with the current answer list injected.
            code_payload = self._build_harness(code, answers=answers)
            if self._runner.available:
                result = self._runner.run_code(code_payload)
            else:
                result = self._run_harness_subprocess(code_payload)

            if result.timed_out or result.failed:
                error = error or f"sandbox error: {result.error or result.stderr[:200]}"
                events.append(LaneBEventType.RESULT_FAIL.value)
                break

            # Check wall-clock budget after the run too (a single long run can
            # exceed the budget between the pre-run check and completion).
            if time.perf_counter() >= deadline:
                events.append(LaneBEventType.WALL_CLOCK_ABORT.value)
                partial = value is not None
                error = error or "wall-clock budget exceeded"
                break

            # Decode the harness output.
            outcome = self._decode_harness_output(result)
            if outcome is None:
                # No structured output — treat the raw value as the result.
                value = result.value
                events.append(LaneBEventType.RESULT_OK.value)
                break

            kind = outcome.get("kind")
            reported_depth = outcome.get("depth", 0)
            max_depth = max(max_depth, reported_depth)

            if kind == "result":
                value = outcome.get("value")
                events.append(LaneBEventType.RESULT_OK.value)
                break

            if kind == "error":
                error = outcome.get("error", "unknown harness error")
                events.append(LaneBEventType.RESULT_FAIL.value)
                break

            if kind == "pending":
                # The code stopped at one or more unanswered primitives.
                requests = outcome.get("requests", [])
                if not requests:
                    # No requests but not a result — should not happen.
                    error = "harness stopped with no pending requests"
                    events.append(LaneBEventType.RESULT_FAIL.value)
                    break

                # Fulfill each pending request in order.
                stopped = False
                for req in requests:
                    req_type = req[0]

                    if req_type == "queue":
                        question = req[1]

                        # Depth check (binding).
                        if reported_depth > self.config.max_depth:
                            events.append(LaneBEventType.DEPTH_REFUSED.value)
                            error = (
                                f"llm_query depth exceeded: "
                                f"{reported_depth} > "
                                f"max_depth {self.config.max_depth}"
                            )
                            stopped = True
                            break

                        # Call-count budget check (binding).
                        if query_count >= self.config.max_llm_queries:
                            events.append(LaneBEventType.BUDGET_ABORT.value)
                            partial = value is not None
                            error = error or (
                                f"llm_query call-count budget exceeded: "
                                f"{query_count} >= max_llm_queries "
                                f"{self.config.max_llm_queries}"
                            )
                            stopped = True
                            break

                        events.append(LaneBEventType.LLM_QUERY_QUEUED.value)
                        try:
                            answer = self._answer_question(
                                question, intent_text, tool_results, run_id,
                            )
                        except LaneBError as exc:
                            error = error or f"judge call failed: {exc}"
                            events.append(LaneBEventType.RESULT_FAIL.value)
                            stopped = True
                            break

                        query_count += 1
                        answers.append(answer)
                        events.append(LaneBEventType.LLM_QUERY_ANSWERED.value)

                    elif req_type == "tool":
                        tool_name = req[1]
                        tool_kwargs = req[2]

                        try:
                            tool_fn = _HOST_TOOL_DISPATCH.get(tool_name)
                            if tool_fn is None:
                                raise LaneBError(
                                    f"tool '{tool_name}' has no host-side "
                                    f"dispatch"
                                )
                            tool_result = tool_fn(**tool_kwargs)
                        except LaneBError as exc:
                            error = error or f"tool call failed: {exc}"
                            events.append(LaneBEventType.RESULT_FAIL.value)
                            stopped = True
                            break

                        tool_count += 1
                        answers.append(tool_result)
                        tool_results.append({
                            "tool": tool_name, "kwargs": tool_kwargs,
                            "result": tool_result,
                        })

                if stopped:
                    break
                # Re-run with the new answers injected.
                continue

            # Unknown outcome kind — treat as final result.
            value = outcome
            events.append(LaneBEventType.RESULT_OK.value)
            break

        latency_ms = self._ms(t0)
        ok = error is None
        return LaneBResult(
            ok=ok and not partial,
            value=value,
            code=code,
            llm_query_count=query_count,
            depth_used=max_depth,
            partial=partial,
            error=error,
            events=events,
            latency_ms=latency_ms,
            run_id=run_id,
        )

    # -- code-gen ------------------------------------------------------------

    def _generate_code(
        self,
        intent_text: str,
        intent_params: dict[str, Any],
        run_id: str | None,
    ) -> str:
        """Ask the subject model to write run(ctx). Retry once on parse fail."""
        messages = build_code_gen_prompt(
            intent_text, intent_params, self.tools, self.config,
        )
        last_err: str | None = None
        for attempt in range(self.config.max_code_gen_retries + 1):
            try:
                resp = self._call_code_gen(messages)
            except LaneBError as exc:
                last_err = str(exc)
                log.warning("lane B+ code-gen transport error (attempt %d): %s",
                            attempt, exc)
                continue
            text = _extract_text(resp)
            try:
                return parse_code_gen_response(text)
            except ValueError as exc:
                last_err = str(exc)
                log.warning("lane B+ code-gen parse failed (attempt %d): %s",
                            attempt, exc)
                continue
        raise LaneBError(
            f"code-gen rejected after {self.config.max_code_gen_retries + 1} "
            f"attempts: {last_err}"
        )

    def _call_code_gen(self, messages: list[dict[str, str]]) -> dict:
        """Dispatch the code-gen LLM call (or the injected caller)."""
        if self._code_gen_caller is not None:
            resp = self._code_gen_caller(
                self.config.code_gen_port, messages,
            )
        else:
            # Default: use the transport layer (raw_chat_completion).
            from transport import get_transport  # type: ignore[import-untyped]
            transport = get_transport()
            payload = {
                "model": self.config.code_gen_model,
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0.2,
            }
            resp = transport.raw_chat_completion(
                self.config.code_gen_port, payload,
            )
        # Every code-gen call counts against the budget (T8) — injected or not.
        self._bump_budget(resp)
        return resp

    def _answer_question(
        self,
        question: str,
        intent_text: str,
        tool_results: list[dict[str, Any]],
        run_id: str | None,
    ) -> str:
        """Answer a queued llm_query() question via the judge model."""
        messages = build_judge_prompt(question, intent_text, tool_results)
        if self._judge_caller is not None:
            resp = self._judge_caller(self.config.judge_port, messages)
        else:
            from transport import get_transport  # type: ignore[import-untyped]
            transport = get_transport()
            payload = {
                "model": self.config.judge_model,
                "messages": messages,
                "max_tokens": 512,
                "temperature": 0.3,
            }
            resp = transport.raw_chat_completion(self.config.judge_port, payload)
        self._bump_budget(resp)
        return _extract_text(resp) or ""

    def _bump_budget(self, resp: dict) -> None:
        """Count an LLM call against the engine budget counters (T8).

        Integration point: engine.orchestrator.session.increment_llm_calls.
        We call the injected budget_increment closure when present (tests +
        pipeline wiring). The closure signature matches increment_llm_calls'
        effect: increment(tokens) -> counters dict. This keeps lane-B LLM
        calls on the same tally as the orchestrator's.
        """
        if self._budget_increment is None:
            return
        tokens = 0
        usage = resp.get("usage") if isinstance(resp, dict) else None
        if isinstance(usage, dict):
            tokens = usage.get("total_tokens", 0)
        try:
            self._budget_increment(tokens)
        except Exception:  # noqa: BLE001 — budget accounting must not break the lane
            log.warning("lane B+ budget increment failed", exc_info=True)

    # -- sandbox harness ------------------------------------------------------

    def _build_harness(self, code: str,
                       answers: list | None = None) -> str:
        """Build the sandbox payload that drives the generated ``run()``.

        WORKS WITHIN THE SANDBOX CONSTRAINTS (no exec/classes in payload):
          - The entrypoint calls ``run()`` with zero args — so the generated
            code MUST be ``def run():`` (no ctx). We inject the primitives
            (tool, llm_query, param) as GLOBAL functions via a preamble that
            runs BEFORE the generated code. The generated code calls them as
            bare globals: ``tool(...)``, ``llm_query(...)``, ``param(...)``.
          - tool()/llm_query() with no injected answer append a request to a
            module-level ``_pending`` list and ``return None`` (the generated
            code continues, but its result is discarded). After run() returns,
            the preamble checks _pending: if non-empty, it prints a structured
            ``__ACE_LANEB__`` line describing the pending requests and the
            depth. The executor reads this, fulfills the requests on the host,
            and re-runs with the answer list injected via the SANDBOX_ANSWER
            env var (JSON). On re-run, the primitives return the injected
            answers in order instead of appending to _pending.
          - No exec/compile/classes in the payload — the sandbox entrypoint's
            own exec runs the concatenated preamble + generated code.

        Intent params + run_id are JSON-literal-injected so the payload is a
        pure string (no pickling — pickling is itself a prompt-injection
        surface, cf. smolagents #2320).
        """
        # Use repr() for safe Python literal injection (avoids json.loads which
        # chokes on control characters that may appear in generated code).
        answers_literal = repr(answers or [])
        params_literal = repr(self._current_params)
        run_id_literal = repr(self._current_run_id)
        return (
            "import json as _json\n"
            "\n"
            f"_params = {params_literal}\n"
            f"_run_id = {run_id_literal}\n"
            f"_answers = {answers_literal}\n"
            "_answer_idx = 0\n"
            "_depth = 0\n"
            "_pending = []\n"
            "\n"
            "def param(_n):\n"
            "    return _params.get(_n)\n"
            "\n"
            "def tool(_name, **_kw):\n"
            "    global _answer_idx\n"
            "    _i = _answer_idx\n"
            "    _answer_idx += 1\n"
            "    if _i < len(_answers):\n"
            "        return _answers[_i]\n"
            "    _pending.append(('tool', _name, _kw))\n"
            "    raise StopIteration\n"
            "\n"
            "def llm_query(_q):\n"
            "    global _answer_idx, _depth\n"
            "    _depth += 1\n"
            "    _i = _answer_idx\n"
            "    _answer_idx += 1\n"
            "    if _i < len(_answers):\n"
            "        return _answers[_i]\n"
            "    _pending.append(('queue', _q, None))\n"
            "    raise StopIteration\n"
            "\n"
            "# ---- generated code follows (defines def run():) ----\n"
            + code.replace("\r", "") + "\n"
            + "# ---- end generated code ----\n"
            "\n"
            "def _wrap():\n"
            "    try:\n"
            "        _r = run()\n"
            "        return ('result', _r)\n"
            "    except StopIteration:\n"
            "        return ('pending', None)\n"
            "\n"
            "_kind, _r = _wrap()\n"
            "if _kind == 'pending':\n"
            "    print('__ACE_LANEB__:' + _json.dumps({'kind': 'pending', 'depth': _depth, 'requests': _pending}))\n"
            "else:\n"
            "    print('__ACE_LANEB__:' + _json.dumps({'kind': 'result', 'value': _r, 'depth': _depth}))\n"
            "\n"
            "# Satisfy the entrypoint's callable(g.get('run')) check — the real\n"
            "# work already ran during exec (above). This no-op prevents the\n"
            "# entrypoint from re-executing the generated code.\n"
            "def run():\n"
            "    return None\n"
        )

    def _run_harness_subprocess(self, code_payload: str):
        """Run the harness directly via subprocess (dev/test box, no docker).

        The sandbox module's subprocess backend wraps payloads in a zero-arg
        `run()` harness that is incompatible with our `run(ctx)` model, so lane
        B runs its harness directly here. This path is NOT a security boundary
        (documented) — lane B code only runs in production via the docker
        backend; this path exists for logic tests on a dev box.
        """
        import subprocess
        from engine.intent.sandbox import SandboxResult, SandboxBackend
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code_payload],
                capture_output=True,
                text=True,
                timeout=self.config.sandbox_timeout_sec,
            )
            latency_ms = self._ms(t0)
            return SandboxResult(
                ok=True,
                backend=SandboxBackend.SUBPROCESS,
                returncode=proc.returncode,
                stdout=self._cap(proc.stdout, 256 * 1024),
                stderr=self._cap(proc.stderr, 256 * 1024),
                timed_out=False,
                value=None,
                error=None if proc.returncode == 0 else f"exit {proc.returncode}",
                latency_ms=latency_ms,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False, backend=SandboxBackend.SUBPROCESS, returncode=124,
                stdout="", stderr="", timed_out=True,
                error=f"subprocess timed out after {self.config.sandbox_timeout_sec}s",
                latency_ms=self._ms(t0),
            )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                ok=False, backend=SandboxBackend.SUBPROCESS, returncode=1,
                stdout="", stderr="", timed_out=False,
                error=f"subprocess error: {exc}",
                latency_ms=self._ms(t0),
            )

    @staticmethod
    def _cap(text: str, limit: int) -> str:
        b = text.encode("utf-8")
        if len(b) <= limit:
            return text
        return b[:limit].decode("utf-8", errors="replace")

    # -- harness output decoding ---------------------------------------------

    def _decode_harness_output(self, result) -> dict | None:
        """Decode the structured __ACE_LANEB__ line from sandbox output.

        Returns a dict with 'kind' in {'result','queue'} on success, or None
        if no structured line was emitted (raw value path).
        """
        if not result.stdout:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("__ACE_LANEB__:"):
                try:
                    return json.loads(line[len("__ACE_LANEB__:"):])
                except json.JSONDecodeError:
                    continue
        return None

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _ms(t0: float) -> float:
        return round((time.perf_counter() - t0) * 1000.0, 2)


# Module-level state for the current run id + params (injected into the
# harness). The executor sets these before each sandbox run; the harness reads
# them. Thread-local would be cleaner, but lane B is single-threaded per
# execution, and the executor holds no concurrent executions.
intent_params_global: dict[str, Any] = {}
_current_run_id: str | None = None


def _dispatch_tool(name: str, kwargs: dict[str, Any],
                   tool_results: list[dict[str, Any]]) -> Any:
    """Dispatch a registry tool call from generated lane-B code.

    In production this calls the registry callable in-process (tool() is a
    host-side primitive, NOT sandboxed — the sandbox only isolates the
    generated code; tool dispatch runs on the host). Tests inject a fake via
    ctx; the harness here uses a module-level override for host-side dispatch.
    """
    fn = _HOST_TOOL_DISPATCH.get(name)
    if fn is None:
        raise LaneBError(f"tool '{name}' has no host-side dispatch")
    r = fn(**kwargs)
    tool_results.append({"tool": name, "kwargs": kwargs, "result": r})
    return r


# Host-side tool dispatch table. The pipeline wires this to the registry.
_HOST_TOOL_DISPATCH: dict[str, Callable[..., Any]] = {}


def register_host_tool(name: str, fn: Callable[..., Any]) -> None:
    """Register a host-side dispatch for a registry tool (called by pipeline)."""
    _HOST_TOOL_DISPATCH[name] = fn


def _extract_text(resp: dict) -> str:
    """Extract the text content from an OpenAI-style completion response."""
    choices = resp.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content:
            return content
    # Fallback: some transports return {content: ...} directly.
    if "content" in resp:
        return resp["content"]
    return ""
    return ""
