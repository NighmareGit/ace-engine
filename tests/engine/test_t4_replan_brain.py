"""T4 tests: re-plan brain (types / protocol / runtime).

Three-layer separation:
  (a) types    — RePlanRequest / RePlanPatch dataclasses (transport-independent)
  (b) protocol — thin OpenAI-compatible client adapter over the engine's
                 existing transport.curl_beellama (no new deps)
  (c) runtime  — RePlanBrain orchestrating: gather trigger signature + session
                 state + task defs -> build prompt -> call LLM -> validate via
                 T3 validate_patch -> apply via apply_orchestrator_patch.

The LLM may ONLY emit a T3 patch object; code-token / file-edit attempts are
rejected by the existing validator. Max 2 re-plans per task per run.
"""

import os
import sys
import json

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Isolate every test in its own temp engine.db (WAL mode)."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# (a) Types
# ---------------------------------------------------------------------------

def test_replan_request_dataclass():
    from engine.orchestrator.replan_types import RePlanRequest
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "syntax"),
        trigger_reason="repeated syntax error",
        task_title="Create health check",
        task_description="Build api/health.py",
        task_dependencies=[],
        error_history=[{"stage": "validation", "error_class": "syntax"}],
        hypothesis=None,
    )
    assert req.task_id == "T01"
    assert req.trigger_signature == ("validation", "syntax")


def test_replan_patch_dataclass():
    from engine.orchestrator.replan_types import RePlanPatch
    patch = RePlanPatch(
        action="re-spec", task_id="T01",
        spec_patch={"hypothesis": "flask missing",
                    "constraint_updates": ["use stdlib"],
                    "dependency_allowlist_additions": [],
                    "acceptance_amendments": []},
        reason="validation exhausted on import flask",
    )
    assert patch.action == "re-spec"


def test_replan_request_to_dict_roundtrip():
    from engine.orchestrator.replan_types import RePlanRequest
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "import"),
        trigger_reason="x",
        task_title="t", task_description="d",
        task_dependencies=["T00"],
        error_history=[],
        hypothesis=None,
    )
    d = req.to_dict()
    assert d["task_id"] == "T01"
    assert isinstance(d["trigger_signature"], list)


# ---------------------------------------------------------------------------
# (b) Protocol — OpenAI-compatible client adapter
# ---------------------------------------------------------------------------

def test_protocol_builds_openai_payload():
    """The protocol adapter builds an OpenAI-compatible chat payload from a
    RePlanRequest (independent of transport)."""
    from engine.orchestrator.replan_types import RePlanRequest
    from engine.orchestrator.replan_protocol import build_replan_payload
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "syntax"),
        trigger_reason="repeated syntax",
        task_title="Create health check",
        task_description="Build api/health.py",
        task_dependencies=[],
        error_history=[{"stage": "validation", "error_class": "syntax"}],
        hypothesis=None,
    )
    payload = build_replan_payload(req, max_tokens=4096)
    assert "messages" in payload
    assert payload["max_tokens"] == 4096
    assert payload["temperature"] == 0.2
    # The payload must instruct JSON-only output conforming to the T3 schema
    # (the schema with "action"/"task_id" lives in the system prompt).
    system_content = payload["messages"][0]["content"]
    user_content = payload["messages"][-1]["content"]
    assert "JSON" in system_content
    assert "action" in system_content
    assert "task_id" in system_content
    # The user prompt carries the failure context.
    assert "T01" in user_content


def test_protocol_parses_valid_patch_response():
    """A well-formed JSON patch response is parsed into a dict."""
    from engine.orchestrator.replan_protocol import parse_replan_response
    raw = json.dumps({
        "action": "re-spec", "task_id": "T01",
        "spec_patch": {"hypothesis": "h", "constraint_updates": ["c"],
                       "dependency_allowlist_additions": [],
                       "acceptance_amendments": []},
        "reason": "r",
    })
    patch = parse_replan_response(raw)
    assert patch["action"] == "re-spec"
    assert patch["task_id"] == "T01"


def test_protocol_parses_fenced_json_response():
    """Tolerates markdown fences around the JSON (models do this)."""
    from engine.orchestrator.replan_protocol import parse_replan_response
    raw = 'Here is the patch:\n```json\n' + json.dumps({
        "action": "re-spec", "task_id": "T01",
        "spec_patch": {"hypothesis": "h", "constraint_updates": [],
                       "dependency_allowlist_additions": [],
                       "acceptance_amendments": []},
        "reason": "r",
    }) + '\n```'
    patch = parse_replan_response(raw)
    assert patch["task_id"] == "T01"


# ---------------------------------------------------------------------------
# (c) Runtime — RePlanBrain
# ---------------------------------------------------------------------------

class _MockReplanTransport:
    """Transport stub returning a canned LLM response for the re-plan call."""

    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []

    def curl_beellama(self, port, messages, **kwargs):
        self.calls.append((port, messages, kwargs))
        return {"content": self.response_text, "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 100, "prompt_tokens": 50,
                "completion_tokens": 50, "predicted_per_second": 5.0}


def _make_valid_replan_response(task_id="T01"):
    return json.dumps({
        "action": "re-spec", "task_id": task_id,
        "spec_patch": {
            "hypothesis": "The import fails because flask is not available.",
            "constraint_updates": ["Use stdlib http.server."],
            "dependency_allowlist_additions": [],
            "acceptance_amendments": [],
        },
        "reason": "validation exhausted on import 'flask'.",
    })


def test_replan_brain_returns_valid_patch():
    from engine.orchestrator.replan_types import RePlanRequest
    from engine.orchestrator.replan_brain import RePlanBrain
    transport = _MockReplanTransport(_make_valid_replan_response())
    brain = RePlanBrain(transport, replan_model_port=8080)
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "syntax"),
        trigger_reason="repeated syntax",
        task_title="t", task_description="d",
        task_dependencies=[], error_history=[], hypothesis=None,
    )
    result = brain.replan(req, known_task_ids=["T01"])
    assert result.ok is True
    assert result.patch["action"] == "re-spec"
    assert result.validated is True
    assert len(transport.calls) == 1
    # The call went to the configured port.
    assert transport.calls[0][0] == 8080


def test_replan_brain_rejects_code_token_response():
    """An LLM response containing code tokens is rejected by the T3 validator
    (the whole point — the deterministic gate stays authoritative)."""
    from engine.orchestrator.replan_types import RePlanRequest
    from engine.orchestrator.replan_brain import RePlanBrain
    bad = json.dumps({
        "action": "re-spec", "task_id": "T01",
        "spec_patch": {
            "hypothesis": "import os; os.system('rm -rf /')",
            "constraint_updates": [], "dependency_allowlist_additions": [],
            "acceptance_amendments": [],
        },
        "reason": "try this",
    })
    transport = _MockReplanTransport(bad)
    brain = RePlanBrain(transport, replan_model_port=8080)
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "syntax"),
        trigger_reason="r", task_title="t", task_description="d",
        task_dependencies=[], error_history=[], hypothesis=None,
    )
    result = brain.replan(req, known_task_ids=["T01"])
    assert result.ok is False
    assert result.validated is False
    assert "code" in result.error.lower() or "invalid" in result.error.lower()


def test_replan_brain_depth_cap():
    """After max_replans_per_task re-plans for the same task, further attempts
    are refused (returns ok=False with a depth-exhausted reason)."""
    from engine.orchestrator.replan_types import RePlanRequest
    from engine.orchestrator.replan_brain import RePlanBrain
    transport = _MockReplanTransport(_make_valid_replan_response())
    brain = RePlanBrain(transport, replan_model_port=8080,
                        max_replans_per_task=2)
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "syntax"),
        trigger_reason="r", task_title="t", task_description="d",
        task_dependencies=[], error_history=[], hypothesis=None,
    )
    r1 = brain.replan(req, known_task_ids=["T01"])
    r2 = brain.replan(req, known_task_ids=["T01"])
    r3 = brain.replan(req, known_task_ids=["T01"])
    assert r1.ok is True
    assert r2.ok is True
    assert r3.ok is False
    assert "re-plan" in r3.error.lower() or "depth" in r3.error.lower() or "cap" in r3.error.lower()


def test_replan_brain_applies_patch_to_task():
    """A valid, validated patch is applied to the task definition via
    T3 apply_orchestrator_patch."""
    from engine import Task
    from engine.orchestrator.replan_types import RePlanRequest
    from engine.orchestrator.replan_brain import RePlanBrain
    transport = _MockReplanTransport(_make_valid_replan_response())
    brain = RePlanBrain(transport, replan_model_port=8080)
    req = RePlanRequest(
        run_id="run-1", task_id="T01",
        trigger_signature=("validation", "syntax"),
        trigger_reason="r", task_title="t", task_description="d",
        task_dependencies=[], error_history=[], hypothesis=None,
    )
    result = brain.replan(req, known_task_ids=["T01"])
    task = Task(id="T01", title="t", description="d", module="m.py")
    applied = brain.apply_to_task(task, result)
    assert applied is True
    assert getattr(task, "orchestrator_state", None) == "RE_SPEC"
    # Hypothesis recorded on the task.
    assert "flask" in task.description


# ---------------------------------------------------------------------------
# Engine-level wiring: telemetry + trigger->replan path
# ---------------------------------------------------------------------------

def test_replan_telemetry_recorded(tmp_db):
    """session.record_replan persists a re-plan attempt to engine.db."""
    from engine.orchestrator import session as sess
    from engine.orchestrator.session import record_replan, get_replan_history
    sess.create_session("run-1", ["T01"])
    row_id = record_replan(
        "run-1", "T01", ("validation", "syntax"),
        "repeated syntax", "raw LLM output", True, True, None, 100,
    )
    assert row_id > 0
    history = get_replan_history("run-1")
    assert len(history) == 1
    assert history[0]["task_id"] == "T01"
    assert history[0]["validated"] == 1
    assert history[0]["applied"] == 1


def _run_with_replan_transport(transport, max_retries_generate=3,
                               max_replans_per_task=2, replan_port=8080):
    """Run the engine with a transport whose generate always fails validation
    but whose re-plan port returns a valid patch. Returns (result, events)."""
    import engine.engine as eng_mod
    import engine.committer as cm
    from engine.engine import Engine, EngineConfig, Task

    events = []
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d",
                                        module="app.py")]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    # Use a non-3090 model so generation goes to port 8082 (not the
    # re-plan port 8080).
    cfg = EngineConfig(max_retries_generate=max_retries_generate,
                      max_replans_per_task=max_replans_per_task,
                      replan_model_port=replan_port,
                      model_config="3070-qwen35-9b")
    try:
        eng = Engine(transport=transport, config=cfg,
                     on_event=lambda et, d: events.append((et, d)))
        result = eng.run("/tmp/prd.md", "/tmp/project")
        return result, events
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


class _FailingGenReplanTransport:
    """Generate always fails validation (bad import); re-plan port returns a
    valid patch that fixes the import constraint."""

    def __init__(self):
        self.generate_calls = 0
        self.replan_calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        if port == 8080:
            # Re-plan brain response.
            self.replan_calls += 1
            return {"content": json.dumps({
                "action": "re-spec", "task_id": "T01",
                "spec_patch": {
                    "hypothesis": "flask is not available in this environment",
                    "constraint_updates": [
                        "Use stdlib http.server — do NOT use flask"],
                    "dependency_allowlist_additions": [],
                    "acceptance_amendments": [],
                },
                "reason": "validation exhausted on import 'flask'.",
            }), "finish_reason": "stop", "reasoning_content": "",
                "thinking_tokens": 0, "total_tokens": 100,
                "prompt_tokens": 50, "completion_tokens": 50,
                "predicted_per_second": 5.0}
        # Generation: always emit a bad import so validation fails.
        self.generate_calls += 1
        return {"content": "```python\nimport nonexistent_module_12345\n```",
                "finish_reason": "stop", "reasoning_content": "",
                "thinking_tokens": 0, "total_tokens": 50,
                "prompt_tokens": 20, "completion_tokens": 30,
                "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def test_engine_replan_records_telemetry(tmp_db):
    """When the trigger fires and the brain runs, telemetry is recorded in
    engine.db even if the re-plan retry also fails (the import is still bad)."""
    transport = _FailingGenReplanTransport()
    result, events = _run_with_replan_transport(transport, max_retries_generate=2)
    # The re-plan brain should have been called at least once.
    assert transport.replan_calls >= 1, "re-plan brain was never invoked"
    # Telemetry recorded.
    from engine.orchestrator.session import get_replan_history
    history = get_replan_history(result.run_id)
    assert len(history) >= 1
    assert history[0]["validated"] == 1  # patch passed T3 validation