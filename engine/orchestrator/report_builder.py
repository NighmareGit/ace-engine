"""Final report — builder layer (T7 runtime).

Assembles a RunReport from the engine's RunResult + orchestrator session
state. Mechanical only: reads DB state, derives AC checkmarks from task
definitions and their terminal states. No LLM, no network.
"""

from engine.orchestrator.report_types import (RunReport, TaskOutcome,
                                              ACChecklistItem)
from engine.orchestrator import session as sess


def _derive_stop_reason(result, budget_counters: dict) -> str | None:
    """Derive a human-readable stop reason from the result."""
    if result.error_message:
        return result.error_message
    # Check for budget cap from the last budget check (stored in session).
    return None


def build_run_report(result, tasks: list, budget_max_llm_calls: int = 0,
                     budget_max_total_tokens: int = 0,
                     budget_max_wall_clock_s: int = 0,
                     stop_reason: str | None = None) -> RunReport:
    """Assemble a RunReport from engine state.

    Args:
        result: engine.engine.RunResult
        tasks: list[Task] (the task definitions, for AC criteria)
        budget_max_*: configured caps (0 = unbounded)
        stop_reason: explicit stop reason (budget cap, stop-file, ...)
    """
    run_id = result.run_id

    # Task outcomes.
    task_outcomes = []
    for tr in result.tasks:
        task_outcomes.append(TaskOutcome(
            task_id=tr.task_id, title=tr.title, state=tr.state,
            attempts=tr.attempts, commit_sha=tr.commit_sha,
            tokens=tr.tokens, error=tr.error_message,
        ))

    # Budget.
    budget = {
        "llm_calls": sess.get_budget_counters(run_id).get("llm_calls", 0),
        "max_llm_calls": budget_max_llm_calls,
        "total_tokens": sess.get_budget_counters(run_id).get("total_tokens", 0),
        "max_total_tokens": budget_max_total_tokens,
        "wall_clock_s": round(result.total_time_s, 2),
        "max_wall_clock_s": budget_max_wall_clock_s,
    }

    # Re-plan history.
    replan_history = sess.get_replan_history(run_id)

    # Checkpoint.
    checkpoint_sha = sess.get_last_pushed_sha(run_id)

    # AC checklist: mechanical — a criterion is "met" iff its task reached
    # a terminal PASS state (COMMIT). No LLM judgment.
    ac_checklist = []
    task_states = {tr.task_id: tr.state for tr in result.tasks}
    for task in tasks:
        criteria = getattr(task, "acceptance_criteria", []) or []
        met = task_states.get(task.id) == "COMMIT"
        if criteria:
            for c in criteria:
                ac_checklist.append(
                    ACChecklistItem(task_id=task.id, criterion=c, met=met))
        else:
            # Always emit at least one row per task (the task itself).
            ac_checklist.append(
                ACChecklistItem(task_id=task.id,
                                criterion=f"{task.id} reached terminal state",
                                met=met))

    success = result.success
    state = "DONE" if success else (
        "CANCELLED" if any(t.state == "CANCELLED" for t in result.tasks)
        else "FAILED")

    return RunReport(
        run_id=run_id,
        success=success,
        state=state,
        prd_path=result.prd_path,
        project_path=result.project_path,
        wall_clock_s=round(result.total_time_s, 2),
        total_tokens=result.total_tokens,
        budget=budget,
        task_outcomes=task_outcomes,
        replan_history=replan_history,
        checkpoint_sha=checkpoint_sha,
        stop_reason=stop_reason,
        ac_checklist=ac_checklist,
    )
