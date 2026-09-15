"""Engine facade — the ONE entry point for the autonomous coding engine."""

import re
import time
import signal
import json
import os
from dataclasses import dataclass, field
from datetime import datetime

from engine import EngineConfig, Task
from engine.state import State, init_db, load_run, list_runs, save_judge_verdict, save_task_result
from engine.pipeline import Pipeline, MAX_TOTAL_ATTEMPTS_PER_TASK, _recheck_transport, _with_timeout, TimeoutError, _checkpoint_lock
from engine.events import emit_event
from engine.orchestrator.stop import check_stop_file, raise_if_stopped, CancelledError
from engine.orchestrator import budget as budget_mod
from engine.orchestrator import session as sess

import logging
logger = logging.getLogger(__name__)


def _get_transport():
    """Auto-detect transport. Import lazily."""
    from transport import get_transport
    return get_transport()


class Engine:
    """The one entry point for the autonomous coding engine."""

    def __init__(self, transport=None, on_event=None, config=None):
        """
        Args:
            transport: Auto-detected if None. From transport.get_transport().
            on_event: Optional callback. Signature: (event_type: str, data: dict) -> None.
            config: EngineConfig dataclass. Defaults if None.
        """
        self.transport = transport or _get_transport()  # call once, cache
        self.on_event = on_event
        self.config = config or EngineConfig()
        self.pipeline = None  # set by run()

        # Proactive health probe — fail fast with a clear error
        if not self.transport.check_health():
            raise ConnectionError(
                "Transport unreachable: engine_service (:3082) down, not on Triton, "
                "and SSH to Triton failed. Run 'python3 cli.py health' to diagnose.")

        # SIGINT handler — checkpoint + graceful cancel
        signal.signal(signal.SIGINT, lambda s, f: self.cancel())

    def run(self, prd_path, project_path, config=None, dry_run=False):
        """Execute a PRD end-to-end."""
        start_time = time.time()
        init_db()

        # 0. Project bootstrap — cycle-2 finding: run() never initialized the
        #    project repo, so commit failed with "not a git repository" unless
        #    the operator had hand-run git init. Idempotent.
        if not dry_run and project_path:
            bootstrap_project(project_path, self.transport)

        # 1. Parse PRD into Task list
        tasks = parse_prd(prd_path)

        # 2. Create pipeline
        pipeline = Pipeline(self)
        pipeline.run_id = f"run-{int(time.time())}"
        self.pipeline = pipeline

        # D26: wire the escalation JSONL projection. The handler wraps the
        # operator's on_event so escalation events are projected to
        # engine_run_<run_id>_escalations.jsonl in the run directory AND
        # forwarded to the original callback. Always wired (the consumer-side
        # projection is not opt-in); with no operator callback the chain is a
        # no-op and the JSONL is still written. Best-effort: a missing project
        # path (dry-run) means no JSONL is written (the handler swallows the
        # same way emit_event does).
        run_dir = os.path.join(project_path, "run", pipeline.run_id)
        from engine.adapters.escalation_jsonl import EscalationJsonlHandler
        self.on_event = EscalationJsonlHandler(run_dir, chain=self.on_event)

        # 3. Transition to PARSING
        pipeline.transition(State.PARSING)
        pipeline.tasks = tasks

        # 4. Dry run — parse only, return task plan
        if dry_run:
            pipeline.transition(State.QUEUED)
            return RunResult(
                run_id=pipeline.run_id,
                success=True,
                prd_path=prd_path,
                project_path=project_path,
                total_time_s=time.time() - start_time,
                tasks=[TaskResult(task_id=t.id, title=t.title, state=State.QUEUED.value,
                                  attempts=0, commit_sha=None, time_s=0.0, tokens=0,
                                  error_message=None) for t in tasks],
                states_visited=list(pipeline.states_visited),
                total_tokens=0,
                error_message=None,
            )

        # 5. Transition to QUEUED
        pipeline.transition(State.QUEUED)

        # T1/T8: create the orchestrator session (budget caps, task states).
        # Only when orchestrator caps are configured (all-zero = disabled).
        _orch_cfg = self.config
        sess.create_session(pipeline.run_id, [t.id for t in tasks], config=_orch_cfg)

        # D2: contract-stub pre-pass — scaffold placeholder files for every
        # declared file[] entry not already on disk. Idempotent. A scaffold
        # failure degrades to a warning event, never crashes the run.
        if project_path and not dry_run:
            try:
                from engine.orchestrator.stubs import scaffold_dag_stubs
                _stubbed = scaffold_dag_stubs(project_path, tasks)
                if _stubbed:
                    logger.info("D2: scaffolded %d stub(s): %s",
                                len(_stubbed), _stubbed)
                    emit_event(self.on_event, "stubs_scaffolded", {
                        "run_id": pipeline.run_id,
                        "created": _stubbed,
                        "count": len(_stubbed),
                    })
            except Exception as e:  # noqa: BLE001 — scaffold must never crash the run
                logger.warning("D2: scaffold_dag_stubs failed (degraded): %s", e)
                emit_event(self.on_event, "scaffold_warning", {
                    "run_id": pipeline.run_id,
                    "error": str(e),
                })

        # 6. Loop through tasks in topological order
        task_results = []
        total_tokens = 0
        for task in tasks:
            # T8: budget check before each task. Stop gracefully if a cap was
            # exceeded by a previous task.
            _counters = sess.get_budget_counters(pipeline.run_id)
            _stopped, _reason = budget_mod.check_budget(pipeline.run_id, _counters)
            if _stopped:
                # D26: project the pre-task budget abort to JSONL.
                emit_event(self.on_event, "budget_abort", {
                    "run_id": pipeline.run_id,
                    "task_id": task.id,
                    "attempt": 0,
                    "attempts": 0,
                    "outcome": "budget_cancelled",
                    "reason": _reason,
                })
                task_results.append(TaskResult(
                    task_id=task.id, title=task.title, state=State.CANCELLED.value,
                    attempts=0, commit_sha=None, time_s=0.0, tokens=0,
                    error_message=_reason,
                ))
                pipeline.transition(State.CANCELLED)
                break

            pipeline.current_task = task
            pipeline.current_task_idx = tasks.index(task)

            try:
                task_result = self._run_task(task, pipeline, project_path)
                total_tokens += task_result.tokens
                task_results.append(task_result)
            except CancelledError as e:
                # T5: stop-file cancellation — mark CANCELLED, not FAILED.
                task_results.append(TaskResult(
                    task_id=task.id, title=task.title, state=State.CANCELLED.value,
                    attempts=0, commit_sha=None, time_s=0.0, tokens=0,
                    error_message=str(e),
                ))
                pipeline.transition(State.CANCELLED)
                break
            except Exception as e:
                task_results.append(TaskResult(
                    task_id=task.id, title=task.title, state=State.FAILED.value,
                    attempts=0, commit_sha=None, time_s=0.0, tokens=0,
                    error_message=str(e),
                ))
                pipeline.transition(State.FAILED)
                break

            # Epic-5 bugfix: task-level failure already transitioned to FAILED
            # (inside _run_task) — transitioning NEXT from FAILED is illegal.
            # T5/T8: same for CANCELLED (stop-file or budget cap).
            if task_result.state in (State.FAILED.value, State.CANCELLED.value):
                break

            # When a task returns COMMIT but the pipeline is still in QUEUED
            # (research handler bypasses the normal
            # CONTEXT→GENERATE→VALIDATE→TEST→COMMIT pipeline), the pipeline
            # stays QUEUED — research tasks never drive the pipeline state
            # machine.  Transitioning to DONE here then trying to go back to
            # QUEUED for the next task is illegal (DONE has no outgoing edges,
            # VALID_TRANSITIONS[DONE] = []).  Just continue; the pipeline
            # remains QUEUED and the next task is dequeued normally.
            # For code-atom tasks the pipeline is already in COMMIT state and
            # the existing logic below handles it.
            if task_result.state == State.COMMIT.value and pipeline.state == State.QUEUED:
                continue

            # NEXT → QUEUED for next task
            pipeline.transition(State.NEXT)
            if tasks.index(task) < len(tasks) - 1:
                pipeline.transition(State.QUEUED)

        # 7. DONE — Epic-5 bugfix: success derives from task results, not the
        # pipeline state (COMMIT has no direct FAILED transition; a commit
        # failure previously fell through to an illegal COMMIT→DONE and a
        # false success=True).
        failed = any(t.state == State.FAILED.value for t in task_results)
        cancelled = any(t.state == State.CANCELLED.value for t in task_results)
        if not failed and not cancelled and pipeline.state not in (State.FAILED, State.CANCELLED):
            if pipeline.state == State.COMMIT:
                pipeline.transition(State.NEXT)  # COMMIT has no DONE edge
            if pipeline.state != State.DONE:
                # Pass task_states so the QUEUED→DONE guard can verify all
                # tasks are terminal (research tasks return COMMIT while the
                # pipeline stays QUEUED — the guard needs the states to
                # confirm completion).
                _task_states = {t.task_id: t.state for t in task_results}
                pipeline.transition(State.DONE, task_states=_task_states)
        success = (pipeline.state == State.DONE and not failed and not cancelled)

        result = RunResult(
            run_id=pipeline.run_id,
            success=success,
            prd_path=prd_path,
            project_path=project_path,
            total_time_s=time.time() - start_time,
            tasks=task_results,
            states_visited=list(pipeline.states_visited),
            total_tokens=total_tokens,
            error_message=None if success else "Pipeline failed",
        )

        # T7: final report — always emitted on DONE/CANCELLED/FAILED.
        # Best-effort: report failure must never fail the run.
        try:
            from engine.orchestrator.report_builder import build_run_report as _orch_build_report
            from engine.orchestrator.report_persistence import (persist_report,
                                                              write_report_markdown)
            _stop_reason = None if success else (
                "cancelled" if cancelled else result.error_message)
            orch_report = _orch_build_report(
                result, tasks,
                budget_max_llm_calls=self.config.max_llm_calls,
                budget_max_total_tokens=self.config.max_total_tokens,
                budget_max_wall_clock_s=self.config.max_wall_clock_s,
                stop_reason=_stop_reason,
            )
            persist_report(orch_report)
            # Write markdown under the project's run/<run_id>/ dir (no branch
            # writes). Best-effort: unwritable path = log-and-continue.
            try:
                run_dir = os.path.join(project_path, "run", pipeline.run_id)
                write_report_markdown(orch_report, run_dir)
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            logger.warning("T7 report emission failed (best-effort): %s", e)
            emit_event(self.on_event, "report_error", {
                "run_id": pipeline.run_id, "error": str(e),
            })

        # ORCH-7: Tool-Gap Detector — scan this run's telemetry for missing
        # capability signals. Cheap, same-process, best-effort: a detector
        # failure must never fail the run.
        try:
            from engine.orchestrator.toolgap_detector import scan_run
            from engine.orchestrator.toolgap_persistence import persist_gap_report
            tgd_report = scan_run(pipeline.run_id)
            if tgd_report.findings:
                persist_gap_report(tgd_report)
        except Exception as e:  # noqa: BLE001
            logger.warning("TGD scan failed (best-effort): %s", e)

        # Judge hook — score the completed run if enabled
        # Legacy --judge flag maps to "full" (deterministic gate + LLM scores)
        judge_mode = self.config.judge_mode
        if self.config.judge and judge_mode == "off":
            judge_mode = "full"
        if judge_mode in ("deterministic", "full"):
            try:
                from engine.judge import judge_run as _judge_run
                verdict = _judge_run(result)
                result.judge = verdict.to_dict()
                save_judge_verdict(pipeline.run_id, result.judge)
            except Exception as e:
                # P3: corrected 3-arg form — callback is arg 0 (was dead, V8).
                emit_event(self.on_event, "judge_error", {
                    "run_id": pipeline.run_id,
                    "error": str(e),
                })

        # P4: results archive — automatic at end of run() (DONE or FAILED).
        # Best-effort: a snapshot/commit failure must never fail the run.
        if self.config.archive_results:
            try:
                from engine.archive import archive_run
                report = build_run_report(result, self.config).to_dict()
                archive_run(result.run_id, project_path, report=report,
                            archive_keep=self.config.archive_keep)
            except Exception as e:  # noqa: BLE001 — archival is best-effort
                logger.warning("archive failed (best-effort, run continues): %s", e)
                emit_event(self.on_event, "archive_error", {
                    "run_id": result.run_id, "error": str(e),
                })

        return result

    def run_matrix(self, configs, prds, project_path, repetitions=1,
                   dry_run=True):
        """Programmatic API for CCBS (AC5.2) — configs × prds × repetitions.

        Returns a JSON-serializable dict with per-run RunReport dicts and a
        variance-aware summary (spec §5): scores_mean/scores_std per cell,
        flagged "insufficient" when repetitions < 3 (REAP instability lesson).
        """
        import statistics
        from pathlib import Path as _Path
        matrix = []
        cells = {}
        # Cycle-2 finding: all cells sharing one project dir bleeds context
        # across PRDs (sim-PRD leftovers steered a kvstore task into
        # regenerating api/health.py) and collides task ids (T01/T02 in both
        # PRDs). Each (config, prd) cell gets its own isolated project dir.
        # The Gitea push target stays the top-level project's repo.
        os.environ.setdefault("GITEA_REPO", _Path(project_path).name)
        for cfg in configs:
            for prd in prds:
                cell_dir = os.path.join(
                    project_path,
                    f"{_Path(prd).stem}__{cfg}")
                if not dry_run:
                    os.makedirs(cell_dir, exist_ok=True)
                for rep in range(1, repetitions + 1):
                    config = EngineConfig(
                        model_config=cfg,
                        judge_mode=self.config.judge_mode,
                        judge_port=self.config.judge_port,
                        subject_port=self.config.subject_port,
                        judge_temperature=self.config.judge_temperature,
                        max_tokens_by_role=self.config.max_tokens_by_role,
                        dry_run=dry_run,
                    )
                    sub = Engine(transport=self.transport,
                                 on_event=self.on_event, config=config)
                    result = sub.run(prd, project_path=cell_dir,
                                     config=config, dry_run=dry_run)
                    report = build_run_report(result, config).to_dict()
                    report["repetition"] = rep
                    matrix.append(report)

                    cell = (cfg, prd)
                    cell_scores = [t["scores"]["overall"]
                                   for t in report["per_task"]
                                   if t.get("scores")]
                    cells.setdefault(cell, []).append({
                        "scores": cell_scores,
                        "success": report["success"],
                        "total_tokens": report["total_tokens"],
                        "wall_clock_s": report["wall_clock_s"],
                    })

        all_scores = [s for runs in cells.values() for r in runs
                      for s in r["scores"]]
        summary = {
            "schema_version": 1,
            "total_runs": len(matrix),
            "variance": ("insufficient" if repetitions < 3
                         else "reported"),
            "cells": {},
        }
        for (cfg, prd), runs in cells.items():
            scores = [s for r in runs for s in r["scores"]]
            summary["cells"][f"{cfg}|{prd}"] = {
                "runs": len(runs),
                "pass_rate": (sum(1 for r in runs if r["success"])
                              / len(runs)) if runs else 0.0,
                "scores_mean": (round(statistics.mean(scores), 2)
                                if scores else None),
                "scores_std": (round(statistics.stdev(scores), 2)
                               if len(scores) > 1 else None),
                "tokens_mean": (round(statistics.mean(
                    r["total_tokens"] for r in runs), 1) if runs else 0),
                "wall_clock_mean_s": (round(statistics.mean(
                    r["wall_clock_s"] for r in runs), 2) if runs else 0.0),
            }
        if all_scores:
            summary["overall_scores_mean"] = round(
                statistics.mean(all_scores), 2)
            summary["overall_scores_std"] = (
                round(statistics.stdev(all_scores), 2)
                if len(all_scores) > 1 else None)
        return {"matrix": matrix, "summary": summary}

    def _maybe_replan(self, task, pipeline, validation_result,
                      last_error: str, cancel_check) -> dict | None:
        """T4: evaluate the T2 trigger and, if it fires, run the re-plan brain.

        Returns a dict with keys {applied, reason, error} or None if no re-plan
        was attempted (trigger did not fire or brain unavailable). All state is
        persisted to engine.db via session.record_replan.
        """
        from engine.orchestrator.trigger import evaluate_trigger
        from engine.orchestrator.replan_brain import RePlanBrain
        from engine.orchestrator.replan_types import RePlanRequest

        run_id = pipeline.run_id
        retries = pipeline.task_retries.get(task.id,
                                            {"generate": 0, "test": 0, "commit": 0})
        attempts = retries.get("generate", 0) + 1
        budget = self.config.max_retries_generate

        # Build the error history from the session (simplified: use last error).
        sig = ("validation",
               validation_result.stages[0].get("stage", "unknown")
               if validation_result.stages and not validation_result.stages[0].get("passed")
               else "unknown")
        # Evaluate trigger against a synthetic history of identical signatures
        # (the dominant failure class). This mirrors T2's condition (a).
        history = [sig] * attempts
        should_fire, trigger_reason = evaluate_trigger(history, budget)
        if not should_fire:
            return None

        # Build the re-plan request.
        req = RePlanRequest(
            run_id=run_id, task_id=task.id,
            trigger_signature=sig, trigger_reason=trigger_reason,
            task_title=task.title, task_description=task.description,
            task_dependencies=list(task.dependencies),
            error_history=[{"stage": sig[0], "error_class": sig[1]}] * attempts,
            hypothesis=getattr(task, "hypothesis", None),
            replan_depth=0,
        )

        # Create the brain and run (wrapped in timeout + cancel_check).
        brain = RePlanBrain(
            self.transport,
            replan_model_port=self.config.replan_model_port,
            max_replans_per_task=self.config.max_replans_per_task,
        )
        try:
            result = _with_timeout(
                lambda: brain.replan(req, known_task_ids=[t.id for t in pipeline.tasks]),
                self.config.timeout_inference, label=f"replan {task.id}",
                cancel_check=cancel_check,
            )
        except Exception as e:
            sess.record_replan(run_id, task.id, sig, trigger_reason,
                               None, False, False, f"timeout/cancel: {e}", 0)
            return {"applied": False, "error": str(e)}

        # T8: count the re-plan LLM call.
        budget_mod.increment_and_check(run_id, tokens=result.tokens)

        if not result.ok or not result.patch:
            sess.record_replan(run_id, task.id, sig, trigger_reason,
                               result.raw_response, False, False,
                               result.error, result.tokens)
            return {"applied": False, "error": result.error}

        # Apply the validated patch.
        try:
            applied = brain.apply_to_task(task, result)
        except Exception as e:
            sess.record_replan(run_id, task.id, sig, trigger_reason,
                               result.raw_response, True, False,
                               f"apply error: {e}", result.tokens)
            return {"applied": False, "error": str(e)}

        sess.record_replan(run_id, task.id, sig, trigger_reason,
                           result.raw_response, True, True, None,
                           result.tokens)
        return {"applied": True, "reason": result.patch.get("reason", "")}

    def _run_task(self, task, pipeline, project_path):
        """Execute a single task through the full pipeline stages."""
        task_start = time.time()
        task_tokens = 0

        # T07c: dispatch research tasks to the ResearchTaskHandler. Only
        # non-CodeTaskHandler handlers divert here; the code-atom path below
        # stays 100% inline and unchanged (lazy import avoids a cycle).
        from engine.task_handler import _resolve_handler, CodeTaskHandler
        handler = _resolve_handler(task, self)
        if not isinstance(handler, CodeTaskHandler):
            return handler.run(task, pipeline, project_path)

        # T5: cancel-check bound to this run's stop-file. Consulted at every
        # stage transition AND inside long calls via _with_timeout.
        _stopped = lambda: check_stop_file(pipeline.run_id)

        # a. CONTEXT — build project context
        raise_if_stopped(pipeline.run_id)
        pipeline.transition(State.CONTEXT)
        from engine.context import build_context
        ctx = build_context(project_path, task, self.transport)

        # D1: thread the DAG's declared files into the context so the import
        # classifier can recognize dag-siblings (modules that map to a file
        # declared in any task's files[] of the current DAG).
        if not getattr(ctx, "dag_files", None):
            ctx.dag_files = set()
        for _t in pipeline.tasks:
            for _f in (getattr(_t, "files", None) or []):
                ctx.dag_files.add(_f)

        # b. GENERATE — generate code with retry.
        # T5: wrap the blocking HTTP call with _with_timeout + cancel_check so
        # a stop-file can abort a hang within one poll interval (<< 2*timeout).
        raise_if_stopped(pipeline.run_id)
        pipeline.transition(State.GENERATE)
        from engine.generator import generate_code, ErrorContext
        from engine.committer import write_project_file
        error_ctx = None
        code = None
        validation_ok = False
        # P1: reasoning-budget escalation state. At most ONE escalate-retry per
        # task (a generation-level retry, distinct from validation retries).
        reasoning_escalated = False
        transport_rechecked = False
        for attempt in range(self.config.max_retries_generate):
            try:
                code = _with_timeout(
                    lambda: generate_code(ctx, task, self.config, self.transport,
                                          error_ctx=error_ctx),
                    self.config.timeout_inference, label=f"generate {task.id}",
                    cancel_check=_stopped,
                )
            except CancelledError:
                pipeline.transition(State.CANCELLED)
                return TaskResult(
                    task_id=task.id, title=task.title, state=State.CANCELLED.value,
                    attempts=attempt, commit_sha=None, time_s=time.time() - task_start,
                    tokens=task_tokens, error_message="cancelled by stop-file",
                )
            except Exception as e:  # noqa: BLE001 — transport failure, not a code failure
                # P3: on first transport failure, re-detect transport once (cheap
                # recovery from a transient blip) then retry this attempt.
                if not transport_rechecked:
                    logger.warning("task %s transport failure (%s); re-checking transport",
                                   task.id, e)
                    _recheck_transport(self)
                    transport_rechecked = True
                    continue
                raise
            task_tokens += code.tokens

            # T8: count this LLM call and check budget caps. Stop gracefully
            # (CANCELLED) if a cap is exceeded — same path as the stop-file.
            _counters, _bstopped, _breason = budget_mod.increment_and_check(
                pipeline.run_id, tokens=code.tokens)
            if _bstopped:
                # D26: project the budget abort to JSONL (9b->35b tier signal)
                # before bailing — the run is stopping, so this is the last
                # escalation we can emit for this task.
                emit_event(self.on_event, "budget_abort", {
                    "run_id": pipeline.run_id,
                    "task_id": task.id,
                    "attempt": attempt + 1,
                    "attempts": attempt + 1,
                    "outcome": "budget_cancelled",
                    "reason": _breason,
                })
                pipeline.transition(State.CANCELLED)
                return TaskResult(
                    task_id=task.id, title=task.title, state=State.CANCELLED.value,
                    attempts=attempt + 1, commit_sha=None,
                    time_s=time.time() - task_start, tokens=task_tokens,
                    error_message=_breason,
                )

            # P1: on reasoning exhaustion, escalate max_tokens ×2 (cap 16384) for
            # exactly one retry. Does not consume a generate-retry attempt.
            if code.exhausted and not reasoning_escalated:
                escalated_tokens = min(code.max_tokens_used * 2, 16384)
                logger.warning(
                    "task %s reasoning exhausted at %d tokens; escalating to %d",
                    task.id, code.max_tokens_used, escalated_tokens)
                reasoning_escalated = True
                code = _with_timeout(
                    lambda: generate_code(ctx, task, self.config, self.transport,
                                          error_ctx=error_ctx,
                                          max_tokens_override=escalated_tokens),
                    self.config.timeout_inference, label=f"generate-escalate {task.id}",
                    cancel_check=_stopped,
                )
                task_tokens += code.tokens

            # P1: a second exhaustion even after escalation is a distinct failure
            # (not a generic validation failure) — surface a clear error and stop.
            if code.exhausted:
                pipeline.transition(State.FAILED)
                logger.warning(
                    "task %s reasoning exhausted even after escalation to %d tokens",
                    task.id, code.max_tokens_used)
                task_result = TaskResult(
                    task_id=task.id, title=task.title,
                    state=State.FAILED.value,
                    attempts=pipeline.get_retry_count(task.id, "generate") + 1,
                    commit_sha=None, time_s=time.time() - task_start,
                    tokens=task_tokens,
                    error_message="reasoning exhausted: empty output after "
                                  f"escalated budget ({code.max_tokens_used} tokens)",
                    prompt_tokens=code.prompt_tokens,
                    completion_tokens=code.completion_tokens,
                    thinking_tokens=code.thinking_tokens,
                    tokens_per_sec=code.tokens_per_sec,
                    role=code.role,
                    validation_stages=None,
                )
                if self.config.judge_mode in ("llm", "full") and code.files:
                    try:
                        from engine.llm_judge import score_task as _score_task
                        task_result.scores = _score_task(
                            pipeline.run_id, task.id, task.title,
                            "\n\n".join(code.files.values()),
                            self.transport, role=code.role, save=True,
                            scored_state="validation_failed")   # P5
                        # T8: judge/scoring LLM call counts toward budget.
                        budget_mod.increment_and_check(pipeline.run_id, tokens=0)
                    except Exception as e:  # noqa: BLE001
                        # P3: corrected 3-arg form (callback is arg 0).
                        emit_event(self.on_event, "llm_judge_error", {
                            "run_id": pipeline.run_id, "task_id": task.id,
                            "error": str(e)})
                _save_terminal_result(pipeline, task, code, None, None, task_result)
                return task_result

            # c. VALIDATE
            pipeline.transition(State.VALIDATE)
            from engine.validator import validate
            result = validate(code, ctx, task=task)
            if result.passed:
                validation_ok = True
                break
            # Stage telemetry: persist the per-stage trace on every failed attempt
            # so a forensic read shows how far each attempt got (P0).
            save_task_result(pipeline.run_id, task.id, {
                "state": State.VALIDATE.value,
                # Store the RAW response (not the extracted files): on a
                # validation failure this is the only ground truth for
                # reproducing extraction bugs (cycle-3 lesson).
                "generated_code": code.raw_response,
                "validation_result": json.dumps(result.stages),
                "attempts": attempt + 1,
                "error_message": _last_stage_error(result.stages),
            })
            # Retry with error context
            pipeline.increment_retry(task.id, "generate")
            error_ctx = ErrorContext(
                previous_code="\n".join(code.files.values()),
                validation_errors=[s.get("error", "") for s in result.stages if not s.get("passed")],
                test_failures=[],
                attempt_number=attempt + 1,
            )
            pipeline.transition(State.GENERATE)

        # d. STAGE FILES TO DISK — write generated files BEFORE testing
        #    Save originals for rollback on test failure
        # Bugfix (Epic-5): when generate retries are exhausted without a valid
        # validation, the loop leaves state=GENERATE and GENERATE→TEST is an
        # illegal transition (it previously crashed as InvalidTransition and
        # zeroed the telemetry). Return a clean FAILED TaskResult instead.
        if not validation_ok:
            last_error = _last_stage_error(result.stages) if result else "validation failed"

            # T4: re-plan brain — loop re-plan→apply→retry up to
            # max_replans_per_task times. Each cycle is budget-counted and
            # stop-file checked. If any cycle produces a passing validation,
            # we fall through to TEST. If all cycles exhaust, the task fails.
            from engine.validator import validate as _validate
            _repl_count = 0
            while not validation_ok and _repl_count < self.config.max_replans_per_task:
                # Stop-file check between cycles.
                if _stopped():
                    pipeline.transition(State.CANCELLED)
                    return TaskResult(
                        task_id=task.id, title=task.title,
                        state=State.CANCELLED.value,
                        attempts=attempt + 1, commit_sha=None,
                        time_s=time.time() - task_start,
                        tokens=task_tokens,
                        error_message="cancelled by stop-file",
                    )
                _repl_count += 1
                _repl_result = self._maybe_replan(task, pipeline, result,
                                                  last_error, _stopped)
                if _repl_result is None:
                    # Trigger did not fire — no re-plan, break out.
                    break
                if not _repl_result.get("applied"):
                    logger.info("task %s re-plan %d did not apply: %s",
                                task.id, _repl_count,
                                _repl_result.get("error", ""))
                    break
                logger.info("task %s re-plan %d applied; retrying generation",
                            task.id, _repl_count)
                # Retry generation with the amended task definition.
                # State fix (run-1788913851): on re-plan cycle N>1 the state is
                # already VALIDATE (set by the previous re-plan retry), and
                # VALIDATE→VALIDATE is an illegal transition. Re-entering
                # generation semantically means going back to GENERATE
                # (VALIDATE→GENERATE is legal); guard so re-entry from
                # GENERATE is a no-op.
                if pipeline.state != State.GENERATE:
                    pipeline.transition(State.GENERATE)
                try:
                    code = _with_timeout(
                        lambda: generate_code(ctx, task, self.config,
                                              self.transport,
                                              error_ctx=None),
                        self.config.timeout_inference,
                        label=f"generate-replan-{_repl_count} {task.id}",
                        cancel_check=_stopped,
                    )
                except CancelledError:
                    pipeline.transition(State.CANCELLED)
                    return TaskResult(
                        task_id=task.id, title=task.title,
                        state=State.CANCELLED.value,
                        attempts=attempt + 1, commit_sha=None,
                        time_s=time.time() - task_start,
                        tokens=task_tokens,
                        error_message="cancelled by stop-file",
                    )
                task_tokens += code.tokens
                budget_mod.increment_and_check(pipeline.run_id,
                                               tokens=code.tokens)
                pipeline.transition(State.VALIDATE)
                result = _validate(code, ctx, task=task)
                if result.passed:
                    validation_ok = True
                    break
                last_error = _last_stage_error(result.stages)
                logger.warning(
                    "task %s re-plan %d retry still failed: %s",
                    task.id, _repl_count, last_error)

            if not validation_ok:
                # T4: emit re-plan exhaustion escalation when the re-plan ladder
                # was actually used (_repl_count > 0) and still did not produce a
                # passing validation. The projection handler (D26) writes this to
                # the run's escalations JSONL as a 35b->oracle tier transition.
                if _repl_count > 0:
                    emit_event(self.on_event, "replan_exhausted", {
                        "run_id": pipeline.run_id,
                        "task_id": task.id,
                        "attempt": pipeline.get_retry_count(task.id, "generate") + 1,
                        "attempts": pipeline.get_retry_count(task.id, "generate") + 1,
                        "outcome": "exhausted",
                        "reason": f"re-plan ladder exhausted after {_repl_count} "
                                  f"re-plan(s): {last_error}",
                    })
                # P6: oracle escalation — when the re-plan ladder is exhausted
                # AND the oracle is enabled, escalate the TASK (not the run)
                # to the oracle. The oracle digests the task into atoms; the
                # engine's existing pipeline executes them as sub-tasks; the
                # results aggregate back. One oracle escalation per task per
                # run (no recursive oracle). Graceful degradation if disabled.
                _oracle_escalated = False
                try:
                    from engine import oracle_config_from_engine
                    from engine.orchestrator.oracle_escalate import (
                        should_escalate_to_oracle,
                        escalate_task_to_oracle,
                    )
                    _oracle_cfg = oracle_config_from_engine(self.config)
                    _should, _reason = should_escalate_to_oracle(
                        pipeline.run_id, task.id,
                        replan_exhausted=True, cfg=_oracle_cfg)
                    if _should:
                        _esc = escalate_task_to_oracle(
                            task, pipeline, _oracle_cfg, self.transport,
                            original_task_ids=[task.id])
                        if _esc.get("escalated") and _esc.get("atoms"):
                            _oracle_escalated = True
                            logger.info(
                                "task %s oracle escalation produced %d atoms",
                                task.id, len(_esc["atoms"]))
                            # D26: project the oracle escalation to JSONL
                            # (35b->oracle tier transition).
                            emit_event(self.on_event, "oracle_escalation", {
                                "run_id": pipeline.run_id,
                                "task_id": task.id,
                                "attempt": pipeline.get_retry_count(task.id, "generate") + 1,
                                "attempts": pipeline.get_retry_count(task.id, "generate") + 1,
                                "outcome": "escalated",
                                "reason": f"oracle digest produced {len(_esc['atoms'])} atoms",
                            })
                            # Atoms execute through the existing pipeline as
                            # sub-tasks; results aggregate back. The atom
                            # dispatch is handled by the caller's task loop
                            # (the atoms are persisted in oracle_atoms and the
                            # engine re-queues them). For now, record success
                            # and fall through to the FAILED path — the atom
                            # execution path is wired by the engine's task
                            # graph (oracle atoms are additive sub-tasks).
                except Exception as e:  # noqa: BLE001
                    # Oracle escalation is best-effort; never crash the pipeline.
                    logger.info("task %s oracle escalation skipped: %s",
                                task.id, e)

                # P3: emit escalation on validation exhaustion — includes
                # scores and judge reasoning when the LLM judge ran (T5.5).
                pipeline.transition(State.FAILED)
                logger.warning(
                    "task %s validation exhausted after %d attempts%s: %s",
                    task.id, self.config.max_retries_generate,
                    f" + {_repl_count} re-plans" if _repl_count else "",
                    last_error)
                task_result = TaskResult(
                    task_id=task.id, title=task.title,
                    state=State.FAILED.value,
                    attempts=pipeline.get_retry_count(task.id, "generate") + 1,
                    commit_sha=None, time_s=time.time() - task_start,
                    tokens=task_tokens,
                    error_message="validation exhausted after "
                                  f"{self.config.max_retries_generate} attempts"
                                  f" + {_repl_count} re-plans: {last_error}",
                    prompt_tokens=code.prompt_tokens if code else 0,
                    completion_tokens=code.completion_tokens if code else 0,
                    thinking_tokens=code.thinking_tokens if code else 0,
                    tokens_per_sec=code.tokens_per_sec if code else 0.0,
                    role=code.role if code else "coder",
                    validation_stages=result.stages if result else None,
                )
                if self.config.judge_mode in ("llm", "full") and code and code.files:
                    try:
                        from engine.llm_judge import score_task as _score_task
                        task_result.scores = _score_task(
                            pipeline.run_id, task.id, task.title,
                            "\n\n".join(code.files.values()),
                            self.transport, role=code.role, save=True,
                            scored_state="validation_failed")   # P5
                        # T8: judge/scoring LLM call counts toward budget.
                        budget_mod.increment_and_check(pipeline.run_id, tokens=0)
                    except Exception as e:  # noqa: BLE001
                        # P3: corrected 3-arg form (callback is arg 0).
                        emit_event(self.on_event, "llm_judge_error", {
                            "run_id": pipeline.run_id, "task_id": task.id,
                            "error": str(e)})
                _emit_escalation(pipeline, task, "validation",
                                 last_errors=[last_error],
                                 validation_stages=result.stages if result else None,
                                 scores=task_result.scores,
                                 judge_reasoning=task_result.scores.get("reasoning")
                                 if task_result.scores else None)
                return task_result
        originals = {}
        for filename, content in code.files.items():
            full_path = f"{project_path}/{filename}"
            originals[full_path] = content  # no originals on first write
            write_project_file(self.transport, full_path, content)

        # e. TEST — run tests with retry.
        # T5: stop-file check before entering the test stage.
        raise_if_stopped(pipeline.run_id)
        pipeline.transition(State.TEST)
        from engine.tester import run_tests
        test = None
        for attempt in range(self.config.max_retries_test):
            test = _with_timeout(
                lambda: run_tests(project_path, self.transport,
                                  timeout=self.config.timeout_test),
                self.config.timeout_test, label=f"test {task.id}",
                cancel_check=_stopped,
            )
            if test.passed:
                break
            # Restore originals and retry generate
            pipeline.increment_retry(task.id, "test")
            pipeline.transition(State.GENERATE)
            error_ctx = ErrorContext(
                previous_code="\n".join(code.files.values()),
                validation_errors=[],
                test_failures=[f.get("error", "") for f in test.failures],
                attempt_number=attempt + 1,
            )
            code = generate_code(ctx, task, self.config, self.transport, error_ctx=error_ctx)
            task_tokens += code.tokens
            # Re-stage files
            for filename, content in code.files.items():
                full_path = f"{project_path}/{filename}"
                write_project_file(self.transport, full_path, content)
            pipeline.transition(State.VALIDATE)
            validate(code, ctx, task=task)  # quick re-validate
            pipeline.transition(State.TEST)

        # P3: escalate on test exhaustion (emit-only). Tests exhausted without
        # passing — surface the failure trace for the orchestrator/autopsy.
        if test is not None and not test.passed:
            _emit_escalation(pipeline, task, "test",
                             last_errors=[f.get("error", "") for f in test.failures],
                             validation_stages=None)

        # f. COMMIT.
        # T5: stop-file check before entering the commit stage.
        raise_if_stopped(pipeline.run_id)
        pipeline.transition(State.COMMIT)
        from engine.committer import commit_code
        commit = _with_timeout(
            lambda: commit_code(project_path, task, code, self.transport,
                                run_id=pipeline.run_id),
            self.config.timeout_commit, label=f"commit {task.id}",
            cancel_check=_stopped,
        )
        # P2: honor the PERMANENT/retryable classification. A retryable push
        # failure re-enters the commit loop (bounded by max_retries_commit and
        # the cross-stage cap in P3); a PERMANENT failure fails immediately.
        while (not commit.success and not commit.permanent
               and pipeline.get_retry_count(task.id, "commit")
               < self.config.max_retries_commit):
            logger.warning("task %s commit retryable failure (%s); retrying",
                           task.id, commit.error_message)
            pipeline.increment_retry(task.id, "commit")
            commit = commit_code(project_path, task, code, self.transport,
                                 run_id=pipeline.run_id)

        # P3: cross-stage cap — when generate+test+commit attempts across this
        # task reach the cap, escalate and fail (closes COMMIT→QUEUED loop risk).
        # Note: COMMIT has no FAILED edge in the state machine, so the pipeline
        # stays in COMMIT; the TaskResult state carries the failure (emit-only).
        if (not commit.success and commit.permanent
                and pipeline.total_attempts(task.id) >= MAX_TOTAL_ATTEMPTS_PER_TASK):
            _emit_escalation(pipeline, task, "commit",
                             last_errors=[commit.error_message],
                             validation_stages=None,
                             commit_sha=commit.sha)
            task_result = TaskResult(
                task_id=task.id, title=task.title,
                state=State.FAILED.value,
                attempts=pipeline.total_attempts(task.id) + 1,
                commit_sha=commit.sha, time_s=time.time() - task_start,
                tokens=task_tokens,
                error_message=f"cross-stage cap ({MAX_TOTAL_ATTEMPTS_PER_TASK}) "
                              f"reached; {commit.error_message}",
                prompt_tokens=code.prompt_tokens,
                completion_tokens=code.completion_tokens,
                thinking_tokens=code.thinking_tokens,
                tokens_per_sec=code.tokens_per_sec,
                role=code.role,
            )
            _save_terminal_result(pipeline, task, code, test, commit, task_result)
            return task_result

        # P3: escalate on a terminal commit PERMANENT failure (even under cap).
        if not commit.success and commit.permanent:
            _emit_escalation(pipeline, task, "commit",
                             last_errors=[commit.error_message],
                             validation_stages=None,
                             commit_sha=commit.sha)

        # T6: record the last pushed SHA after a successful push. Best-effort:
        # a recording failure must never fail the run.
        if commit.success and commit.sha:
            try:
                sess.record_push(pipeline.run_id, commit.sha)
            except Exception as e:  # noqa: BLE001
                logger.warning("T6 record_push failed (best-effort): %s", e)

        elapsed = time.time() - task_start
        task_result = TaskResult(
            task_id=task.id,
            title=task.title,
            state=State.COMMIT.value if commit.success else State.FAILED.value,
            attempts=pipeline.get_retry_count(task.id, "generate") + 1,
            commit_sha=commit.sha,
            time_s=elapsed,
            tokens=task_tokens,
            error_message=commit.error_message,
            prompt_tokens=code.prompt_tokens,
            completion_tokens=code.completion_tokens,
            thinking_tokens=code.thinking_tokens,
            tokens_per_sec=code.tokens_per_sec,
            role=code.role,
        )

        # Epic-5: LLM judge scoring after TEST/COMMIT (spec §2)
        if self.config.judge_mode in ("llm", "full"):
            try:
                from engine.llm_judge import score_task as _score_task
                code_text = "\n\n".join(code.files.values())
                # P5: flag the state the code was in when scored.
                scored_state = "committed" if commit.success else "test_failed"
                task_result.scores = _score_task(
                    pipeline.run_id, task.id, task.title, code_text,
                    self.transport, role=code.role, save=True,
                    scored_state=scored_state)
                # T8: judge/scoring LLM call counts toward budget.
                budget_mod.increment_and_check(pipeline.run_id, tokens=0)
            except Exception as e:  # noqa: BLE001 — scoring failure is data, not fatal
                # P3: corrected 3-arg form (callback is arg 0).
                emit_event(self.on_event, "llm_judge_error", {
                    "run_id": pipeline.run_id, "task_id": task.id,
                    "error": str(e),
                })

        # Terminal-state capture: persist the final test/commit outcome so
        # engine_task_results always has a terminal row per task (P0 telemetry).
        _save_terminal_result(pipeline, task, code, test, commit, task_result)
        return task_result

    def step(self, task_id):
        """Execute a single task (manual control)."""
        if self.pipeline is None:
            raise ValueError("No active pipeline. Call run() first or resume from checkpoint.")

        # Find the task by ID
        task = None
        for t in self.pipeline.tasks:
            if t.id == task_id:
                task = t
                break
        if task is None:
            raise ValueError(f"Task {task_id} not found in pipeline tasks.")

        self.pipeline.current_task = task
        self.pipeline.current_task_idx = self.pipeline.tasks.index(task)

        task_result = self._run_task(task, self.pipeline, "")  # project_path needed from pipeline
        return StepResult(
            task_id=task_id,
            state=task_result.state,
            success=task_result.error_message is None,
            detail={"commit_sha": task_result.commit_sha, "tokens": task_result.tokens},
        )

    def status(self, run_id=None):
        """Get pipeline status. If run_id is None, return current run."""
        if run_id is None and self.pipeline is not None:
            run_id = self.pipeline.run_id
        if run_id is None:
            raise ValueError("No run_id provided and no active pipeline.")

        data = load_run(run_id)
        if data is None:
            raise ValueError(f"No checkpoint found for run_id={run_id}")

        # Parse tasks to count completed
        tasks_json = data.get("tasks_json", "[]")
        tasks = json.loads(tasks_json) if tasks_json else []
        total = len(tasks)
        completed = sum(
            1 for t in tasks
            if t.get("state") in ("DONE", "FAILED", "CANCELLED")
        )
        progress = (completed / total * 100) if total > 0 else 0.0

        # Calculate elapsed time
        started = data.get("started_at", "")
        elapsed_s = 0.0
        if started:
            try:
                start_dt = datetime.fromisoformat(started)
                elapsed_s = (datetime.now() - start_dt).total_seconds()
            except (ValueError, TypeError):
                pass

        return RunStatus(
            run_id=run_id,
            state=data.get("state", "IDLE"),
            progress_pct=progress,
            current_task=data.get("current_task_id"),
            tasks_completed=completed,
            tasks_total=total,
            elapsed_s=elapsed_s,
        )

    def cancel(self):
        """Cancel gracefully. Checkpoints state."""
        if self.pipeline is not None:
            self.pipeline.cancel()
        return True


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_id: str
    success: bool
    prd_path: str
    project_path: str
    total_time_s: float
    tasks: list           # list[TaskResult]
    states_visited: list  # list[State]
    total_tokens: int
    error_message: str | None
    judge: dict | None = None


@dataclass
class TaskResult:
    task_id: str
    title: str
    state: str            # State.value
    attempts: int
    commit_sha: str | None
    time_s: float
    tokens: int
    error_message: str | None
    # Epic-5 AC5.3 telemetry + per-task LLM scores
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    tokens_per_sec: float = 0.0
    role: str = "coder"
    scores: dict | None = None   # {5 dims, overall, judge_model, error}
    validation_stages: list | None = None   # [{stage,file,passed,error}] last failed attempt


@dataclass
class RunReport:
    """AC5.2/5.3 contract artifact — JSON-serializable via to_dict()."""
    run_id: str
    config: str
    prd_path: str
    success: bool
    wall_clock_s: float
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    thinking_tokens: int
    tokens_per_sec: float
    per_task: list
    judge_verdict: dict | None
    error_message: str | None
    project_path: str = ""     # P4 — needed for per-project archive pruning

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "config": self.config,
            "prd_path": self.prd_path, "project_path": self.project_path,
            "success": self.success,
            "wall_clock_s": round(self.wall_clock_s, 3),
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "thinking_tokens": self.thinking_tokens,
            "tokens_per_sec": round(self.tokens_per_sec, 2),
            "per_task": self.per_task,
            "judge_verdict": self.judge_verdict,
            "error_message": self.error_message,
        }


def build_run_report(result: "RunResult", config: "EngineConfig") -> RunReport:
    """Convert a RunResult into the RunReport contract (AC5.2/5.3)."""
    per_task = []
    prompt_tokens = completion_tokens = thinking_tokens = 0
    tps_sum = 0.0
    tps_n = 0
    for t in result.tasks:
        prompt_tokens += t.prompt_tokens
        completion_tokens += t.completion_tokens
        thinking_tokens += t.thinking_tokens
        if t.tokens_per_sec:
            tps_sum += t.tokens_per_sec
            tps_n += 1
        per_task.append({
            "task_id": t.task_id, "state": t.state, "attempts": t.attempts,
            "commit_sha": t.commit_sha, "time_s": round(t.time_s, 3),
            "tokens": t.tokens,
            "prompt_tokens": t.prompt_tokens,
            "completion_tokens": t.completion_tokens,
            "thinking_tokens": t.thinking_tokens,
            "tokens_per_sec": round(t.tokens_per_sec, 2),
            "role": t.role,
            "scores": t.scores,
            "validation_stages": t.validation_stages,
        })
    return RunReport(
        run_id=result.run_id, config=config.model_config,
        prd_path=result.prd_path, project_path=result.project_path,
        success=result.success,
        wall_clock_s=result.total_time_s, total_tokens=result.total_tokens,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        thinking_tokens=thinking_tokens,
        tokens_per_sec=(tps_sum / tps_n) if tps_n else 0.0,
        per_task=per_task, judge_verdict=result.judge,
        error_message=result.error_message,
    )


@dataclass
class StepResult:
    task_id: str
    state: str            # State.value
    success: bool
    detail: dict


@dataclass
class RunStatus:
    run_id: str
    state: str            # State.value
    progress_pct: float
    current_task: str | None
    tasks_completed: int
    tasks_total: int
    elapsed_s: float


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _save_terminal_result(pipeline, task, code, test, commit, task_result):
    """Persist the terminal test/commit row for a task (P0 stage telemetry)."""
    result_state = task_result.state
    test_result = None
    if test is not None:
        test_result = json.dumps({
            "passed": test.passed,
            "tests_passed": test.tests_passed,
            "tests_failed": test.tests_failed,
            "failures": test.failures,
        })
    save_task_result(pipeline.run_id, task.id, {
        "state": result_state,
        "generated_code": "\n".join(code.files.values()) if code else None,
        "test_result": test_result,
        "commit_sha": commit.sha if commit else None,
        "attempts": task_result.attempts,
        "completed_at": datetime.now().isoformat(),
        "error_message": task_result.error_message,
    })


def _last_stage_error(stages):
    """Return a human-readable '<stage> on <file>: <error>' for the first
    failing stage in a validation trace (P0 stage telemetry)."""
    for stage in stages:
        if not stage.get("passed"):
            return f"{stage.get('stage')} on {stage.get('file')}: {stage.get('error')}"
    return "validation failed"


def _emit_escalation(pipeline, task, stage, last_errors, validation_stages,
                     scores=None, judge_reasoning=None, commit_sha=None):
    """Emit a fire-and-forget task_escalation event (P3 escalation contract).

    Carries the full forensic payload: stage trace, last errors, scores and
    judge reasoning. Never blocks or crashes the pipeline.
    """
    emit_event(pipeline.engine.on_event, "task_escalation", {
        "event_type": "task_escalation",
        "run_id": pipeline.run_id,
        "task_id": task.id,
        "stage": stage,
        "attempts": pipeline.total_attempts(task.id) + 1,
        "last_errors": list(last_errors),
        "validation_stages": validation_stages or [],
        "scores": scores,
        "judge_reasoning": judge_reasoning,
        "commit_sha": commit_sha,
        "ts": datetime.now().isoformat(),
    })


def bootstrap_project(project_path, transport):
    """Create project skeleton + git repo + Gitea remote if the project
    directory doesn't exist. Idempotent: skips steps that are already done."""
    # 1. Directory exists?
    stdout, stderr, rc = transport.run_command(f"test -d {project_path} && echo yes")
    if "yes" not in stdout:
        transport.run_command(f"mkdir -p {project_path}")
        transport.run_command(f"cd {project_path} && git init")
        transport.run_command(f"cd {project_path} && git config user.email ace@local && git config user.name ace")
    # 2. Git repo initialized?
    stdout, _, rc = transport.run_command(f"cd {project_path} && git rev-parse --git-dir")
    if rc != 0:
        transport.run_command(f"cd {project_path} && git init")
    # 3. Origin remote configured? (wire Gitea token auth)
    stdout, _, _ = transport.run_command(f"cd {project_path} && git remote get-url origin")
    if rc != 0 and stdout.strip() == "":
        token = os.environ.get("GITEA_TOKEN", "")
        repo_name = os.path.basename(project_path)
        remote_url = f"http://<user>:{token}@<LAN_IP>:3000/<user>/{repo_name}.git"
        transport.run_command(f"cd {project_path} && git remote add origin {remote_url}")


def parse_prd(prd_path: str) -> list:
    """
    Parse a PRD markdown file into a list of Task objects.

    Supports multiple PRD formats:
    - Numbered lists: "1. As a user, I want..."
    - Bullet lists: "- Create health check endpoint"
    - Tables: "| T01 | Health Check | api/health.py |"
    - Headers: "## Task 1: Health Check" followed by description

    Returns list[Task] in dependency order (topological sort).
    """
    # Delegate to engine.prd which has the full implementation with
    # PRDParser fallback and 4 regex strategies.
    from engine.prd import parse_prd as _prd_parse_prd
    return _prd_parse_prd(prd_path)


def _topological_sort(tasks: list) -> list:
    """Sort tasks by dependency order (topological sort)."""
    task_map = {t.id: t for t in tasks}
    visited = set()
    result = []

    def visit(task_id):
        if task_id in visited:
            return
        visited.add(task_id)
        task = task_map.get(task_id)
        if task:
            for dep in task.dependencies:
                visit(dep)
            result.append(task)

    for task in tasks:
        visit(task.id)

    return result
