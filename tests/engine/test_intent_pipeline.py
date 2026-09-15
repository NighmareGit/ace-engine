"""S1 tests: IIL dispatch pipeline (pipeline.py) + native lane (native.py).

The pipeline is the single entry point: router first (mechanical path), then
optional native escalation. Tests assert the dispatch law, telemetry emission
to the additive intent_events table (the TGD G7/G10 contract), and that
telemetry never crashes the dispatch path.

Native-lane tests use the engine's MockTransport (no network, no GPU) to
exercise the OpenAI tool-call parsing path deterministically.
"""

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent.native import NativeLane
from engine.intent.pipeline import (
    INTENT_EVENTS_TABLE, IntentPipeline, _ensure_events_table, get_intent_events,
)
from engine.intent.router import load_default_router
from engine.intent.types import IntentRequest, Lane, Verdict


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    # Init the engine schema so foreign-key-ish reads don't fail.
    from engine.state import init_db
    init_db()
    yield db_path


class TestDispatchLaw:
    """The mechanical path returns fast; uncertain intents escalate."""

    def test_high_confident_hit_returns_router_lane(self, tmp_db):
        pipe = IntentPipeline()
        res = pipe.dispatch_text("Run the tests for T01.")
        assert res.lane == Lane.ROUTER
        assert res.action == "run_tests"
        assert res.verdict == Verdict.DISPATCH

    def test_clear_keyword_intent_dispatches_without_escalation(self, tmp_db):
        pipe = IntentPipeline()
        res = pipe.dispatch_text("Commit the staged changes.")
        assert res.lane == Lane.ROUTER
        assert res.action == "commit"

    def test_grep_intent_routes_correctly(self, tmp_db):
        pipe = IntentPipeline()
        res = pipe.dispatch_text("Search for 'TODO' across the codebase.")
        assert res.action == "grep"
        assert res.lane == Lane.ROUTER

    def test_escalate_model_intent_routes_correctly(self, tmp_db):
        pipe = IntentPipeline()
        res = pipe.dispatch_text("This needs more brain — escalate to 35B.")
        assert res.action == "escalate_model"

    def test_pipeline_exposes_actions(self, tmp_db):
        pipe = IntentPipeline()
        assert "run_tests" in pipe.actions
        assert len(pipe.actions) == 10


class TestTelemetry:
    """Every dispatch emits an intent_events row (TGD G7/G10 contract)."""

    def test_dispatch_writes_telemetry_row(self, tmp_db):
        pipe = IntentPipeline()
        res = pipe.dispatch_text("Run the tests for T01.", run_id="run-1")
        events = get_intent_events(run_id="run-1")
        assert len(events) >= 1
        row = events[0]
        assert row["action"] == "run_tests"
        assert row["lane"] == "router"
        assert row["uncertain"] == 0
        assert row["run_id"] == "run-1"

    def test_telemetry_row_has_confidence(self, tmp_db):
        pipe = IntentPipeline()
        pipe.dispatch_text("Commit the staged changes.", run_id="run-2")
        events = get_intent_events(run_id="run-2")
        assert events[0]["confidence"] > 0.0

    def test_telemetry_row_has_latency(self, tmp_db):
        pipe = IntentPipeline()
        pipe.dispatch_text("grep for TODO", run_id="run-3")
        events = get_intent_events(run_id="run-3")
        assert events[0]["latency_ms"] >= 0.0

    def test_telemetry_failure_does_not_crash_dispatch(self, tmp_db, monkeypatch):
        """A telemetry write failure must never crash the dispatch path."""
        import engine.intent.pipeline as pl

        def _boom(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(pl, "_write_event", _boom)
        pipe = IntentPipeline(emit_telemetry=True)
        # Should NOT raise — the layer survives.
        res = pipe.dispatch_text("Run the tests for T01.")
        assert res.action == "run_tests"

    def test_emit_telemetry_flag_disables_writes(self, tmp_db):
        pipe = IntentPipeline(emit_telemetry=False)
        pipe.dispatch_text("Run the tests for T01.", run_id="run-4")
        events = get_intent_events(run_id="run-4")
        assert events == []


class TestNativeLane:
    """Native lane-A adapter parses OpenAI tool_calls (mocked transport)."""

    def _make_native_with_tools(self):
        native = NativeLane()
        native.set_tools_from_registry({
            "run_tests": "Run the test suite.",
            "commit": "Commit staged changes.",
            "grep": "Search the codebase.",
        })
        return native

    def test_parse_tool_calls_extracts_action_and_args(self):
        native = self._make_native_with_tools()
        resp = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "run_tests",
                            "arguments": json.dumps({"task_id": "T01"}),
                        },
                    }],
                },
            }],
        }
        result = native._parse_tool_calls(resp)
        assert result["action"] == "run_tests"
        assert result["arguments"] == {"task_id": "T01"}

    def test_parse_tool_calls_no_choices_raises(self):
        native = self._make_native_with_tools()
        with pytest.raises(ValueError, match="no choices"):
            native._parse_tool_calls({})

    def test_parse_tool_calls_no_tool_calls_raises(self):
        native = self._make_native_with_tools()
        resp = {"choices": [{"message": {"content": "hello"}}]}
        with pytest.raises(ValueError, match="no tool_calls"):
            native._parse_tool_calls(resp)

    def test_set_tools_from_registry_builds_schema(self):
        native = self._make_native_with_tools()
        assert len(native._tool_definitions) == 3
        names = [t["function"]["name"] for t in native._tool_definitions]
        assert "run_tests" in names

    def test_resolve_returns_native_lane_result(self, tmp_db):
        """Full resolve() path with a stubbed _call_native (no network)."""
        native = self._make_native_with_tools()

        def _fake_call(req):
            return {
                "action": "run_tests",
                "arguments": {"task_id": "T01"},
                "raw": [{"function": {"name": "run_tests"}}],
            }

        native._call_native = _fake_call  # type: ignore[method-assign]
        from engine.intent.types import Route
        req = IntentRequest(text="Run the tests for T01.")
        result = native.resolve(req, Route(action="run_tests", confidence=0.9, uncertain=False))
        assert result.lane == Lane.NATIVE
        assert result.action == "run_tests"
        assert result.arguments == {"task_id": "T01"}
