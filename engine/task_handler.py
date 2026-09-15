"""TaskHandler strategy protocol + dispatch (T07a).

One ``task_type`` branch at the top of ``_run_task()`` (T07c) calls
``_resolve_handler(task, engine)`` to pick a handler:

  * ``task_type == 'research'`` → ``ResearchTaskHandler`` (lazy import from
    ``engine.research`` — the research facade, T07b).
  * anything else → ``CodeTaskHandler`` (the existing code-atom pipeline).

The existing code path stays 100% inline in ``engine.py``; ``CodeTaskHandler``
is a thin marker class whose ``run()`` raises ``NotImplementedImplementedError``
(the engine hook detects a non-CodeTaskHandler and diverts to the handler;
the CodeTaskHandler path is never actually invoked because ``_resolve_handler``
only returns it for non-research types, and the hook only diverts for
non-CodeTaskHandler handlers).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskHandler(Protocol):
    """Strategy protocol for task execution.

    Implementations handle one task end-to-end and return a TaskResult.
    """

    def run(self, task: object, pipeline: object, project_path: str) -> object:
        """Execute the task and return a TaskResult."""
        ...


class CodeTaskHandler:
    """Marker handler for the existing code-atom pipeline.

    The actual code-atom logic lives inline in ``engine.engine._run_task()``.
    This class exists so ``_resolve_handler`` has a uniform return type and
    the engine hook can distinguish "divert to handler" (research) from
    "run the legacy inline path" (everything else).
    """

    def __init__(self, engine: object) -> None:
        self.engine = engine

    def run(self, task: object, pipeline: object, project_path: str) -> object:
        # The legacy path is inline in engine.py; this is never called
        # because the hook only diverts non-CodeTaskHandler handlers.
        raise NotImplementedError(
            "CodeTaskHandler.run() is a no-op: the code-atom pipeline runs "
            "inline in engine.engine._run_task()."
        )


def _resolve_handler(task: object, engine: object) -> TaskHandler:
    """Select a handler for *task*.

    Returns ``ResearchTaskHandler`` for research tasks, ``CodeTaskHandler``
    otherwise. The research import is lazy to avoid an import cycle at
    module load (``engine.research`` imports from ``engine`` submodules).
    """
    task_type = getattr(task, "task_type", "")
    if str(task_type).lower() == "research":
        from engine.research import ResearchTaskHandler
        return ResearchTaskHandler(engine)
    # G1 §10 / DESIGN-G4 §2 row 1: edit-ops dispatch branch (lazy import).
    if str(task_type).lower() == "edit":
        from engine.edit_ops import EditTaskHandler
        return EditTaskHandler(engine)
    # PRD-4: scout/explore lane (lazy import keeps the ace ↔ scout_explore
    # dependency optional at engine import time — scout-explore may not be
    # installed on every ACE host).
    if str(task_type) in _SCOUT_TASK_TYPES:
        from scout_explore.adapters.ace_engine import ScoutExploreAdapter
        return ScoutExploreAdapter(engine)
    return CodeTaskHandler(engine)


# Task types owned by the scout-explore adapter (PRD-4 §4.4). Kept here so the
# dispatch table is the single source of truth for what ACE routes to scout.
_SCOUT_TASK_TYPES = frozenset(
    {
        "scout_span_fetch",
        "scout_file_survey",
        "scout_trace_flow",
        "scout_distill",
        "explore_find_definition",
        "explore_find_usage",
        "explore_bash",
    }
)
