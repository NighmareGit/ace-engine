"""Re-plan brain — runtime layer (T4c).

RePlanBrain orchestrates one re-plan attempt:
  1. gather trigger signature + session state + task defs (caller provides)
  2. build prompt (protocol)
  3. call LLM via transport (protocol)
  4. validate via T3 validate_patch
  5. (optionally) apply via T3 apply_orchestrator_patch

Depth cap: max_replans_per_task per task per run (configurable). Every call
is expected to be wrapped in _with_timeout + cancel_check by the engine and
to increment the T8 budget counters.
"""

from engine.orchestrator.replan_types import RePlanRequest, RePlanResult
from engine.orchestrator.replan_protocol import (build_replan_payload,
                                                 parse_replan_response)
from engine.orchestrator.patch import validate_patch, apply_orchestrator_patch


class RePlanBrain:
    """Orchestrates re-plan attempts for escalated tasks.

    Stateless across runs except for the per-task depth counter, which is
    keyed by task_id for the lifetime of this instance. The engine creates
    one brain per run.
    """

    def __init__(self, transport, replan_model_port: int = 8080,
                 max_replans_per_task: int = 2, max_tokens: int = 4096,
                 temperature: float = 0.2):
        self.transport = transport
        self.replan_model_port = replan_model_port
        self.max_replans_per_task = max_replans_per_task
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._depth: dict[str, int] = {}   # task_id -> count this run

    def replan(self, req: RePlanRequest,
               known_task_ids: list[str]) -> RePlanResult:
        """Run one re-plan attempt. Returns RePlanResult.

        The engine is responsible for wrapping this in _with_timeout +
        cancel_check and for incrementing budget counters.
        """
        # Depth cap check.
        depth = self._depth.get(req.task_id, 0)
        if depth >= self.max_replans_per_task:
            return RePlanResult(
                ok=False, validated=False,
                error=f"re-plan depth cap reached for {req.task_id}: "
                      f"{depth}/{self.max_replans_per_task}",
            )

        # Build prompt.
        payload = build_replan_payload(req, max_tokens=self.max_tokens,
                                       temperature=self.temperature)

        # Call LLM via transport (curl_beellama is OpenAI-compatible).
        import time as _time
        _replan_start = _time.time()
        try:
            response = self.transport.curl_beellama(
                self.replan_model_port,
                payload["messages"],
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        except Exception as e:
            return RePlanResult(ok=False, validated=False,
                                error=f"re-plan transport error: {e}")
        _replan_latency_ms = (_time.time() - _replan_start) * 1000

        # --- Session log capture for re-plan (OBS-01, additive) ---
        _sl_recorder = getattr(self, "_session_log_recorder", None)
        if _sl_recorder is not None:
            try:
                _sl_recorder.record(
                    task_id=req.task_id,
                    attempt=self._depth.get(req.task_id, 0) + 1,
                    stage="replan",
                    model=f"replan-port-{self.replan_model_port}",
                    port=self.replan_model_port,
                    prompt_text=payload.get("messages", [{}])[0].get("content", ""),
                    response=response,
                    latency_ms=_replan_latency_ms,
                    role="orchestrator",
                    max_tokens_used=self.max_tokens,
                )
            except Exception:
                pass  # telemetry must not break re-plan

        raw = response.get("content", "")
        tokens = response.get("total_tokens", 0)

        # Parse.
        try:
            patch_dict = parse_replan_response(raw)
        except (ValueError, Exception) as e:
            return RePlanResult(ok=False, validated=False, raw_response=raw,
                                tokens=tokens,
                                error=f"re-plan parse error: {e}")

        # Validate via T3 (code-token rejection, schema, weaken-only, etc.).
        ok, err = validate_patch(patch_dict, known_task_ids)
        if not ok:
            return RePlanResult(ok=False, validated=False, raw_response=raw,
                                tokens=tokens,
                                error=f"re-plan validation failed: {err}")

        # Record the attempt.
        self._depth[req.task_id] = depth + 1
        return RePlanResult(ok=True, validated=True, patch=patch_dict,
                            raw_response=raw, tokens=tokens)

    def apply_to_task(self, task, result: RePlanResult) -> bool:
        """Apply a validated re-plan patch to a task definition via T3.

        Returns True on success. Raises ValueError if the patch fails
        structural validation or targets a different task.
        """
        if not result.ok or not result.validated or result.patch is None:
            raise ValueError("cannot apply an invalid re-plan result")
        return apply_orchestrator_patch(task, result.patch)

    @property
    def depth(self) -> dict[str, int]:
        return dict(self._depth)
