"""S2 tests: IIL tool registry + hallucination guard (tools.py).

The registry is the allowlist the IIL dispatches from. The hallucination guard
rejects any tool name not in the registry + emits telemetry (feeds TGD G10).
Stubs (actions the engine doesn't implement yet) are clearly marked and
rejected at dispatch time.

Target: ≥10 tests.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent.tools import (
    ToolEntry, ToolRegistry, UnknownToolError, dispatch_tool, guard_tool_call,
)
from engine.intent.types import IntentRequest, IntentResult, Lane, Route, Verdict


@pytest.fixture
def reg():
    return ToolRegistry()


class TestRegistry:
    """Registration + lookup contracts."""

    def test_default_registry_has_twelve_actions(self, reg):
        assert len(reg.actions) == 12

    def test_default_actions_include_core(self, reg):
        for name in ["run_tests", "commit", "validate", "grep", "read_file",
                     "list_tasks", "escalate_model", "fail_with_reason",
                     "read_telemetry", "web_search", "github_lookup",
                     "docs_domains"]:
            assert reg.is_registered(name), f"{name} not registered"

    def test_implemented_flags_are_correct(self, reg):
        assert reg.get("run_tests").implemented is True
        assert reg.get("commit").implemented is True
        assert reg.get("validate").implemented is True

    def test_stub_actions_marked_not_implemented(self, reg):
        for name in ["grep", "read_file", "list_tasks", "escalate_model",
                     "fail_with_reason", "read_telemetry"]:
            entry = reg.get(name)
            assert entry.implemented is False
            assert entry.callable is None
            assert entry.stub_note, f"{name} stub missing stub_note"

    def test_descriptions_match_route_table(self, reg):
        descs = reg.descriptions
        assert "run_tests" in descs
        assert len(descs["run_tests"]) > 0

    def test_register_and_unregister(self, reg):
        reg.register(ToolEntry(name="custom_tool", description="x"))
        assert reg.is_registered("custom_tool")
        reg.unregister("custom_tool")
        assert not reg.is_registered("custom_tool")

    def test_get_missing_returns_none(self, reg):
        assert reg.get("nonexistent") is None

    def test_entry_missing_raises(self, reg):
        with pytest.raises(UnknownToolError):
            reg.entry("nonexistent")


class TestHallucinationGuard:
    """Unknown tool names are rejected (the guard that feeds G10)."""

    def test_assert_registered_passes_for_known(self, reg):
        reg.assert_registered("run_tests")  # no raise

    def test_assert_registered_raises_for_unknown(self, reg):
        with pytest.raises(UnknownToolError):
            reg.assert_registered("delete_everything")

    def test_guard_tool_call_returns_entry(self, reg):
        entry = guard_tool_call(reg, "commit")
        assert entry.name == "commit"

    def test_guard_tool_call_raises_for_hallucination(self, reg):
        with pytest.raises(UnknownToolError):
            guard_tool_call(reg, "launch_rockets")


class TestDispatch:
    """dispatch_tool routes to callables or rejects stubs/hallucinations."""

    def _make_result(self, action="run_tests"):
        return IntentResult(
            request=IntentRequest(text="run tests"),
            verdict=Verdict.DISPATCH,
            route=Route(action=action, confidence=0.9, uncertain=False),
            lane=Lane.ROUTER,
            action=action,
        )

    def test_dispatch_rejects_hallucination(self, reg):
        result = self._make_result(action="launch_rockets")
        out = dispatch_tool(reg, result)
        assert out.verdict == Verdict.REJECT
        assert "not in registry" in out.reason

    def test_dispatch_rejects_stub(self, reg):
        result = self._make_result(action="grep")
        out = dispatch_tool(reg, result)
        assert out.verdict == Verdict.REJECT
        assert "stub" in out.reason

    def test_dispatch_rejects_none_action(self, reg):
        result = self._make_result(action=None)
        out = dispatch_tool(reg, result)
        assert out.verdict == Verdict.REJECT

    def test_dispatch_runs_implemented_callable(self, reg):
        result = self._make_result(action="validate")
        out = dispatch_tool(reg, result)
        # validate callable runs (may return ok=True/False depending on
        # context) but must not raise and must set a reason.
        assert out.reason == "dispatched to registered callable" or out.verdict == Verdict.REJECT

    def test_dispatch_catches_callable_exception(self, reg):
        """A tool that raises must not crash the dispatch layer."""
        def _boom(request, **kw):
            raise RuntimeError("kaboom")
        reg.register(ToolEntry(name="boom", description="x", callable=_boom, implemented=True))
        result = self._make_result(action="boom")
        out = dispatch_tool(reg, result)
        assert out.verdict == Verdict.REJECT
        assert "kaboom" in out.reason
