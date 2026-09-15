"""IIL tool registry + hallucination guard (S2).

The registry is the single source of truth for ACE's real actions — the
contract the router's route table, the native lane's tool schema, and the
hallucination guard all read from. Each entry carries:
  * name: the action name (matches the route table + native tool schema).
  * description: human-readable capability (injected into native prompts).
  * callable: the implementation, OR a stub for not-yet-built actions.
  * implemented: False for stubs (clearly marked, per spec).

Hallucination guard: any tool name NOT in the registry is rejected + logged
as a telemetry event that feeds TGD G10 (tool call not in registry). This is
the enforcement of invariant #4 (egress-allowlist) — the registry IS the
allowlist; nothing dispatches without a registry entry.

NO network egress anywhere in this module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.intent.types import IntentRequest, IntentResult, Lane, Route, Verdict

log = logging.getLogger("engine.intent.tools")


@dataclass
class ToolEntry:
    """One registered tool."""
    name: str
    description: str
    callable: Callable[..., Any] | None = None
    implemented: bool = True
    # network_egress: does this tool make network calls? (invariant #4)
    network_egress: bool = False
    # stub_note: explanation when implemented=False.
    stub_note: str = ""


class ToolRegistry:
    """Registry of ACE's real actions — the allowlist the IIL dispatches from.

    The registry is the contract between the router (which names actions),
    the native lane (which advertises tool schemas), and the guard (which
    rejects unknown names). Add a tool here before routing can resolve it.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._register_defaults()

    # -- registration --------------------------------------------------------

    def register(self, entry: ToolEntry) -> None:
        """Register (or override) a tool."""
        self._tools[entry.name] = entry

    def unregister(self, name: str) -> None:
        """Remove a tool (feeds G4 router-drift: registry change → re-embed)."""
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    @property
    def actions(self) -> list[str]:
        return sorted(self._tools.keys())

    @property
    def descriptions(self) -> dict[str, str]:
        """{name: description} for native-lane tool schema building."""
        return {name: e.description for name, e in self._tools.items()}

    def entry(self, name: str) -> ToolEntry:
        """Return the entry, raising if not registered (guard path)."""
        if name not in self._tools:
            raise UnknownToolError(name)
        return self._tools[name]

    # -- guard ---------------------------------------------------------------

    def assert_registered(self, name: str) -> None:
        """Raise UnknownToolError if the tool is not in the registry.

        The native lane calls this on every tool name the model emits — this
        is the hallucination guard that feeds G10 telemetry.
        """
        if not self.is_registered(name):
            raise UnknownToolError(name)

    # -- defaults -----------------------------------------------------------

    def _register_defaults(self) -> None:
        """Register ACE's real actions.

        Implemented = real callable wired to engine code. Stub = action the
        engine doesn't implement yet (clearly marked, callable=None).
        """
        # --- Implemented actions (real callables) ---
        self.register(ToolEntry(
            name="run_tests",
            description="Run the test suite or tests for a specific task.",
            callable=_RunTestsCallable(),
            implemented=True,
        ))
        self.register(ToolEntry(
            name="commit",
            description="Commit staged changes to the repository.",
            callable=_CommitCallable(),
            implemented=True,
        ))
        self.register(ToolEntry(
            name="validate",
            description="Validate the current task's code (AST/imports/exec).",
            callable=_ValidateCallable(),
            implemented=True,
        ))

        # --- Stub actions (engine doesn't implement yet — clearly marked) ---
        self.register(ToolEntry(
            name="grep",
            description="Search the codebase for a pattern or string.",
            callable=None,
            implemented=False,
            stub_note="Engine has no grep tool yet; wire to transport.run_command('grep ...').",
        ))
        self.register(ToolEntry(
            name="read_file",
            description="Read and return the contents of a file.",
            callable=None,
            implemented=False,
            stub_note="Engine has no read_file tool yet; wire to transport.run_command('cat ...').",
        ))
        self.register(ToolEntry(
            name="list_tasks",
            description="List all tasks and their current states.",
            callable=None,
            implemented=False,
            stub_note="Engine has no list_tasks tool yet; wire to engine.db task query.",
        ))
        self.register(ToolEntry(
            name="escalate_model",
            description="Escalate the task to a more capable model (e.g. 35B).",
            callable=None,
            implemented=False,
            stub_note="Escalation is an orchestrator concern; wire to trigger/replan path.",
        ))
        self.register(ToolEntry(
            name="fail_with_reason",
            description="Mark a task as failed with a human-readable reason.",
            callable=None,
            implemented=False,
            stub_note="Wire to orchestrator task-state mutation (FAILED).",
        ))
        self.register(ToolEntry(
            name="read_telemetry",
            description="Read engine telemetry or a previous run's results.",
            callable=None,
            implemented=False,
            stub_note="Wire to engine.db telemetry read (engine_scores / engine_runs).",
        ))

        # --- Reach-tier egress tools (Wave 4) ---
        # Each is an allowlisted-impl tool (invariant #4): network_egress=True,
        # availability-gated at call time (returns ok=False + telemetry when
        # offline/unconfigured, never raises). See each module's docstring
        # for the exact egress surface.
        self.register(ToolEntry(
            name="web_search",
            description="Query a SearXNG instance for web results (P2 egress tool).",
            callable=_WebSearchCallable(),
            implemented=True,
            network_egress=True,
        ))
        self.register(ToolEntry(
            name="github_lookup",
            description="Read-only GitHub REST API: repo metadata, file contents, issue search (P3 egress tool).",
            callable=_GithubLookupCallable(),
            implemented=True,
            network_egress=True,
        ))
        self.register(ToolEntry(
            name="docs_domains",
            description="Fetch + text-extract allowlisted documentation pages (P4 egress tool).",
            callable=_DocsDomainsCallable(),
            implemented=True,
            network_egress=True,
        ))


class UnknownToolError(Exception):
    """Raised when a tool name is not in the registry (hallucination guard)."""


# ---------------------------------------------------------------------------
# Real callables — thin adapters over the engine's existing functions.
#
# Each callable has the same signature so the dispatcher can invoke them
# uniformly. They catch broadly and return a result dict (never raise past
# the dispatch layer) — a broken tool must not crash the IIL.
# ---------------------------------------------------------------------------

class _RunTestsCallable:
    """Wraps engine.tester.run_tests (real implementation)."""

    def __call__(self, request: IntentRequest, **kwargs: Any) -> dict[str, Any]:
        from engine.tester import run_tests
        project_path = kwargs.get("project_path") or request.context.get("project_path") or "/tmp/project"
        timeout = kwargs.get("timeout", 180)
        try:
            from transport import get_transport
            transport = get_transport()
            result = run_tests(project_path, transport, timeout=timeout)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 — tool must not crash IIL
            return {"ok": False, "error": str(exc)}


class _CommitCallable:
    """Wraps engine.committer.commit_code (real implementation)."""

    def __call__(self, request: IntentRequest, **kwargs: Any) -> dict[str, Any]:
        from engine.committer import commit_code
        try:
            from transport import get_transport
            transport = get_transport()
            project_path = kwargs.get("project_path") or request.context.get("project_path") or "/tmp/project"
            task = kwargs.get("task") or request.context.get("task")
            code = kwargs.get("code") or request.context.get("code") or ""
            run_id = request.run_id
            result = commit_code(project_path, task, code, transport, run_id=run_id)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


class _ValidateCallable:
    """Wraps engine.validator.validate (real implementation)."""

    def __call__(self, request: IntentRequest, **kwargs: Any) -> dict[str, Any]:
        from engine.validator import validate
        code = kwargs.get("code") or request.context.get("code") or ""
        context = kwargs.get("context") or request.context
        try:
            result = validate(code, context)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Reach-tier egress callables (Wave 4) — availability-gated adapters.
#
# These wrap the T1-T3 tool modules. Each checks availability at call time:
# an offline/unconfigured tool returns {ok: False, error: ...} + a telemetry
# event (feeds G11), never a raised exception. They are allowlisted impls per
# invariant #4 — the registry marks network_egress=True.
# ---------------------------------------------------------------------------

class _WebSearchCallable:
    """Wraps engine.intent.websearch (T1, P2)."""

    def __call__(self, request: IntentRequest, **kwargs: Any) -> dict[str, Any]:
        from engine.intent.websearch import WebSearchCallable
        return WebSearchCallable()(request, **kwargs)


class _GithubLookupCallable:
    """Wraps engine.intent.github (T2, P3)."""

    def __call__(self, request: IntentRequest, **kwargs: Any) -> dict[str, Any]:
        from engine.intent.github import GithubLookupCallable
        return GithubLookupCallable()(request, **kwargs)


class _DocsDomainsCallable:
    """Wraps engine.intent.docs (T3, P4)."""

    def __call__(self, request: IntentRequest, **kwargs: Any) -> dict[str, Any]:
        from engine.intent.docs import DocsDomainsCallable
        return DocsDomainsCallable()(request, **kwargs)


# ---------------------------------------------------------------------------
# Hallucination guard — the function the native lane calls.
# ---------------------------------------------------------------------------

def guard_tool_call(registry: ToolRegistry, tool_name: str) -> ToolEntry:
    """Assert a tool name is registered; raise UnknownToolError if not.

    This is the hallucination guard. Callers log the rejection as a G10
    telemetry event (tool call not in registry).
    """
    return registry.entry(tool_name)


def dispatch_tool(registry: ToolRegistry, result: IntentResult) -> IntentResult:
    """Dispatch a validated IntentResult to its registered callable.

    If the action is not registered (hallucination) → reject + G10 telemetry.
    If the action is a stub (not implemented) → reject with reason.
    """
    action = result.action
    if action is None or not registry.is_registered(action):
        # Hallucination guard: tool not in registry.
        _log_hallucination(result)
        return IntentResult(
            request=result.request,
            verdict=Verdict.REJECT,
            route=result.route,
            lane=result.lane,
            action=action,
            reason=f"rejected: tool '{action}' not in registry (hallucination guard)",
        )

    entry = registry.get(action)
    if not entry.implemented or entry.callable is None:
        # Stub action — not yet built.
        return IntentResult(
            request=result.request,
            verdict=Verdict.REJECT,
            route=result.route,
            lane=result.lane,
            action=action,
            reason=f"rejected: tool '{action}' is a stub (not implemented)",
        )

    try:
        call_result = entry.callable(result.request, **(result.arguments or {}))
        result.arguments = call_result
        result.reason = "dispatched to registered callable"
    except Exception as exc:  # noqa: BLE001 — tool must not crash IIL
        result.verdict = Verdict.REJECT
        result.reason = f"tool raised: {exc}"

    return result


def _log_hallucination(result: IntentResult) -> None:
    """Log a hallucinated tool call (feeds G10 telemetry)."""
    log.warning(
        "hallucinated tool call: action=%s model=%s text=%r",
        result.action, result.request.model, result.request.text[:200],
    )
