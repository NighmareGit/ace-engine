"""S4 tests: lane B+ executor — capped RLM bypass (code-as-intent, llm_query).

Lane B+ is the code-as-intent lane: the subject model writes a SHORT python
function `run(ctx)` that composes registry tool calls; the executor runs it
via the docker sandbox with the queued-intent primitive llm_query().

Test scope (TDD, target >= 28 new):
  - Depth-2 enforcement + depth-3 refusal
  - Call-budget abort (max_llm_queries)
  - Wall-clock abort
  - Code rejection (no run func) + retry-once + fence tolerance
  - Docker-only enforcement (subprocess refusal)
  - Queued-intent resume mechanics (mock LLM)
  - Budget counter integration (T8)
  - Pipeline gating (default-off, complex-flag trigger)
  - Protocol: prompt builder + parser (fence tolerance, strict rejection)

ALL LLM calls are mocked. Sandbox docker tests auto-skip on the dev box; logic
tests use the subprocess backend EXCEPT the refusal path (tested regardless).
"""

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent.laneb import LaneBExecutor, LaneBError, register_host_tool
from engine.intent.laneb_protocol import (
    build_code_gen_prompt, build_judge_prompt, parse_code_gen_response,
)
from engine.intent.sandbox import SandboxConfig, SandboxRunner
from engine.intent.types import (
    IntentRequest, LaneBConfig, LaneBEventType, LaneBResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_config():
    """A lane-B config tuned for tests (small budgets, docker NOT required)."""
    return LaneBConfig(
        max_llm_queries=4,
        max_wall_clock_ms=30000,
        max_depth=2,
        docker_required=False,       # tests use subprocess backend
        sandbox_timeout_sec=10,
        max_code_gen_retries=1,
    )


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Isolated engine DB for telemetry tests."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    from engine.state import init_db
    init_db()
    yield db_path


@pytest.fixture
def docker_required_config():
    """Config with docker_required=True (for the refusal test)."""
    return LaneBConfig(
        max_llm_queries=4,
        max_wall_clock_ms=30000,
        max_depth=2,
        docker_required=True,
        sandbox_timeout_sec=10,
    )


def _make_code_gen_response(text: str) -> dict:
    """Wrap text in an OpenAI-style completion response."""
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": 42},
    }


def _make_judge_response(text: str) -> dict:
    return _make_code_gen_response(text)


def _simple_run_code():
    """A minimal valid `def run():` that returns a constant."""
    return "def run():\n    return {'done': True}\n"


def _querying_run_code(question: str = "what?"):
    """A run() that calls llm_query once and returns its answer."""
    return (
        "def run():\n"
        f"    a = llm_query({question!r})\n"
        "    return {'answer': a}\n"
    )


def _nested_querying_run_code():
    """A run() that calls llm_query, and the answer's follow-up also calls
    llm_query (depth-2 nesting)."""
    return (
        "def run():\n"
        "    a1 = llm_query('first question')\n"
        "    a2 = llm_query('follow-up question')\n"
        "    return {'a1': a1, 'a2': a2}\n"
    )


def _triple_querying_run_code():
    """A run() that would recurse 3 levels (depth-3 — must be refused)."""
    return (
        "def run():\n"
        "    a1 = llm_query('q1')\n"
        "    a2 = llm_query('q2')\n"
        "    a3 = llm_query('q3')\n"
        "    return {'a3': a3}\n"
    )


def _tool_calling_run_code():
    """A run() that calls a registry tool then returns."""
    return (
        "def run():\n"
        "    r = tool('my_tool', x=1)\n"
        "    return {'tool_result': r}\n"
    )


# ---------------------------------------------------------------------------
# Protocol: prompt builder
# ---------------------------------------------------------------------------

class TestCodeGenPrompt:
    """build_code_gen_prompt injects tools, constraints, and budget."""

    def test_prompt_is_two_messages(self):
        msgs = build_code_gen_prompt("do X", {}, {"run_tests": "Run tests"}, LaneBConfig())
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_prompt_mentions_sandbox_constraints(self):
        msgs = build_code_gen_prompt("do X", {}, {}, LaneBConfig())
        system = msgs[0]["content"]
        assert "NO network" in system
        assert "NO file IO" in system

    def test_prompt_lists_tools(self):
        msgs = build_code_gen_prompt("do X", {}, {"run_tests": "Run tests"}, LaneBConfig())
        user = msgs[1]["content"]
        assert "run_tests" in user
        assert "Run tests" in user

    def test_prompt_mentions_budget(self):
        cfg = LaneBConfig(max_llm_queries=3, max_depth=2)
        msgs = build_code_gen_prompt("do X", {}, {}, cfg)
        system = msgs[0]["content"]
        assert "3" in system  # max_llm_queries

    def test_prompt_includes_intent_params(self):
        msgs = build_code_gen_prompt("do X", {"task_id": "T01"}, {}, LaneBConfig())
        user = msgs[1]["content"]
        assert "T01" in user

    def test_prompt_advertises_llm_query(self):
        msgs = build_code_gen_prompt("do X", {}, {}, LaneBConfig())
        system = msgs[0]["content"]
        assert "llm_query" in system


class TestJudgePrompt:
    """build_judge_prompt includes intent + tool results."""

    def test_judge_prompt_has_intent_and_question(self):
        msgs = build_judge_prompt("why?", "do X", [])
        user = msgs[1]["content"]
        assert "do X" in user
        assert "why?" in user

    def test_judge_prompt_includes_tool_results(self):
        msgs = build_judge_prompt("why?", "do X",
                                  [{"tool": "run_tests", "result": {"ok": True}}])
        user = msgs[1]["content"]
        assert "run_tests" in user


class TestParseCodeGenResponse:
    """parse_code_gen_response: strict, fence-tolerant."""

    def test_parses_plain_function(self):
        code = parse_code_gen_response("def run():\n    return 1\n")
        assert "def run():" in code

    def test_parses_markdown_fenced_function(self):
        text = "```python\ndef run():\n    return 1\n```"
        code = parse_code_gen_response(text)
        assert "def run():" in code

    def test_parses_fenced_no_lang(self):
        text = "```\ndef run():\n    return 1\n```"
        code = parse_code_gen_response(text)
        assert "def run():" in code

    def test_parses_with_surrounding_prose(self):
        text = "Here is the code:\n\ndef run():\n    return 1\n\nHope that helps."
        code = parse_code_gen_response(text)
        assert "def run():" in code

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            parse_code_gen_response("")

    def test_rejects_no_function(self):
        with pytest.raises(ValueError, match="no .def run.."):
            parse_code_gen_response("x = 1\nprint(x)\n")

    def test_rejects_wrong_signature(self):
        with pytest.raises(ValueError, match="no .def run.."):
            parse_code_gen_response("def run(x):\n    return x\n")

    def test_rejects_ctx_signature(self):
        """The sandbox calls run() with zero args — run(ctx) is rejected."""
        with pytest.raises(ValueError, match="no .def run.."):
            parse_code_gen_response("def run(ctx):\n    return ctx\n")

    def test_rejects_multiple_run_functions(self):
        text = "def run():\n    return 1\ndef run():\n    return 2\n"
        with pytest.raises(ValueError, match="exactly one"):
            parse_code_gen_response(text)

    def test_preserves_imports(self):
        text = "import json\ndef run():\n    return json.dumps({'a': 1})\n"
        code = parse_code_gen_response(text)
        assert "import json" in code
        assert "def run():" in code


# ---------------------------------------------------------------------------
# Executor: docker-only enforcement
# ---------------------------------------------------------------------------

class TestDockerEnforcement:
    """docker_required=True → refuse when docker is unavailable."""

    def test_refuses_when_docker_unavailable(self, docker_required_config):
        """When docker_required and docker is absent, lane B refuses with a
        clear error + DOCKER_REFUSED telemetry — never falls back to
        subprocess for untrusted code."""
        runner = SandboxRunner(SandboxConfig(allow_fallback=False))
        # Force docker unavailable: on a dev box without the image, available
        # is already False. If docker IS available this test is meaningless,
        # so we patch the runner's available to False.
        runner._detected = True
        runner._backend = None  # type: ignore[assignment]
        # Monkeypatch available to False.
        type(runner)._detected = True  # no-op; instead patch via object attr
        # Simplest: patch the property by overriding _detect.
        runner._detect = lambda: None  # type: ignore[method-assign]

        executor = LaneBExecutor(
            config=docker_required_config,
            sandbox_runner=runner,
        )
        result = executor.execute("complex intent")
        assert result.ok is False
        assert "docker" in result.error.lower()
        assert LaneBEventType.DOCKER_REFUSED.value in result.events


# ---------------------------------------------------------------------------
# Executor: code rejection + retry-once
# ---------------------------------------------------------------------------

class TestCodeRejection:
    """Code-gen that emits no run(ctx) is rejected + retried once, then fails."""

    def test_rejects_invalid_code_then_fails(self, base_config):
        """Both code-gen attempts return non-function text → fail with
        CODE_REJECTED telemetry."""
        call_count = 0

        def fake_gen(port, messages):
            nonlocal call_count
            call_count += 1
            return _make_code_gen_response("x = 1\nprint(x)\n")  # no run(ctx)

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
        )
        result = executor.execute("do something")
        assert result.ok is False
        assert "code-gen failed" in result.error
        # 1 initial + 1 retry = 2 calls.
        assert call_count == 2
        assert LaneBEventType.CODE_REJECTED.value in result.events

    def test_retries_once_then_succeeds(self, base_config):
        """First attempt invalid, second valid → succeeds."""
        attempts = []

        def fake_gen(port, messages):
            attempts.append(1)
            if len(attempts) == 1:
                return _make_code_gen_response("not a function\n")
            return _make_code_gen_response(_simple_run_code())

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
        )
        result = executor.execute("do something")
        assert result.ok is True
        assert len(attempts) == 2
        assert LaneBEventType.CODE_ACCEPTED.value in result.events


# ---------------------------------------------------------------------------
# Executor: queued-intent resume mechanics (mock LLM)
# ---------------------------------------------------------------------------

class TestQueuedIntent:
    """llm_query() queues a question, executor answers via judge, resumes."""

    def test_single_llm_query_resumes(self, base_config):
        """Code calls llm_query once → executor answers → code returns."""
        def fake_gen(port, messages):
            return _make_code_gen_response(_querying_run_code("what?"))

        def fake_judge(port, messages):
            return _make_judge_response("42")

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
            judge_caller=fake_judge,
        )
        result = executor.execute("complex intent")
        assert result.ok is True
        assert result.llm_query_count == 1
        assert result.value == {"answer": "42"}
        assert LaneBEventType.LLM_QUERY_QUEUED.value in result.events
        assert LaneBEventType.LLM_QUERY_ANSWERED.value in result.events

    def test_tool_call_resumes(self, base_config):
        """Code calls ctx.tool() → executor dispatches on host → resumes."""
        register_host_tool("my_tool", lambda **kw: {"host": "result"})

        def fake_gen(port, messages):
            return _make_code_gen_response(_tool_calling_run_code())

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
        )
        result = executor.execute("do something")
        assert result.ok is True
        assert result.value == {"tool_result": {"host": "result"}}


# ---------------------------------------------------------------------------
# Executor: depth enforcement
# ---------------------------------------------------------------------------

class TestDepthEnforcement:
    """Depth <= max_depth is enforced; depth-3 is refused."""

    def test_depth_2_allowed(self, base_config):
        """Two sequential llm_query calls (depth 2) succeed."""
        def fake_gen(port, messages):
            return _make_code_gen_response(_nested_querying_run_code())

        def fake_judge(port, messages):
            return _make_judge_response("answered")

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
            judge_caller=fake_judge,
        )
        result = executor.execute("complex intent")
        assert result.ok is True
        assert result.llm_query_count == 2
        assert result.depth_used == 2

    def test_depth_3_refused(self, base_config):
        """Three sequential llm_query calls (depth 3) hit the depth cap."""
        def fake_gen(port, messages):
            return _make_code_gen_response(_triple_querying_run_code())

        def fake_judge(port, messages):
            return _make_judge_response("answered")

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
            judge_caller=fake_judge,
        )
        result = executor.execute("complex intent")
        # Depth-3 is refused: ok=False, DEPTH_REFUSED event.
        assert result.ok is False
        assert LaneBEventType.DEPTH_REFUSED.value in result.events
        assert "depth" in result.error.lower()


# ---------------------------------------------------------------------------
# Executor: budgets
# ---------------------------------------------------------------------------

class TestBudgets:
    """Call-count budget and wall-clock budget are binding."""

    def test_call_budget_abort(self, base_config):
        """Exceeding max_llm_queries aborts with BUDGET_ABORT."""
        base_config = LaneBConfig(
            max_llm_queries=1,  # only 1 query allowed
            max_wall_clock_ms=30000,
            max_depth=2,
            docker_required=False,
            sandbox_timeout_sec=10,
        )

        # Code calls llm_query twice; second call exceeds budget.
        double_query = (
            "def run():\n"
            "    a1 = llm_query('q1')\n"
            "    a2 = llm_query('q2')\n"
            "    return {'a2': a2}\n"
        )

        def fake_gen(port, messages):
            return _make_code_gen_response(double_query)

        def fake_judge(port, messages):
            return _make_judge_response("answered")

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
            judge_caller=fake_judge,
        )
        result = executor.execute("complex intent")
        assert result.ok is False
        assert result.llm_query_count == 1
        assert LaneBEventType.BUDGET_ABORT.value in result.events

    def test_wall_clock_abort(self):
        """A tiny wall-clock budget aborts before the code can complete."""
        cfg = LaneBConfig(
            max_llm_queries=4,
            max_wall_clock_ms=1,  # 1ms — expires immediately
            max_depth=2,
            docker_required=False,
            sandbox_timeout_sec=10,
        )

        def fake_gen(port, messages):
            # Code that takes a moment (sleep) to ensure wall-clock expiry.
            return _make_code_gen_response(
                "import time\ndef run():\n"
                "    time.sleep(0.5)\n"
                "    return {'done': True}\n"
            )

        executor = LaneBExecutor(
            config=cfg,
            code_gen_caller=fake_gen,
        )
        result = executor.execute("complex intent")
        assert result.ok is False
        assert LaneBEventType.WALL_CLOCK_ABORT.value in result.events


# ---------------------------------------------------------------------------
# Executor: budget counter integration (T8)
# ---------------------------------------------------------------------------

class TestBudgetIntegration:
    """Lane-B LLM calls count against the engine budget counters."""

    def test_budget_increment_called(self, base_config):
        """Each LLM call (code-gen + judge) bumps the budget counter."""
        calls = []

        def fake_budget(tokens):
            calls.append(tokens)
            return {"llm_calls": len(calls), "total_tokens": sum(calls)}

        def fake_gen(port, messages):
            return _make_code_gen_response(_querying_run_code())

        def fake_judge(port, messages):
            return _make_judge_response("42")

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
            judge_caller=fake_judge,
            budget_increment=fake_budget,
        )
        result = executor.execute("complex intent", run_id="r1")
        assert result.ok is True
        # code-gen (1) + judge (1) = 2 budget bumps.
        assert len(calls) == 2

    def test_budget_increment_failure_is_swallowed(self, base_config):
        """A budget-counter failure must not break the lane."""
        def bad_budget(tokens):
            raise RuntimeError("db gone")

        def fake_gen(port, messages):
            return _make_code_gen_response(_simple_run_code())

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
            budget_increment=bad_budget,
        )
        # Should NOT raise — budget accounting is best-effort.
        result = executor.execute("do something")
        assert result.ok is True


# ---------------------------------------------------------------------------
# Pipeline gating
# ---------------------------------------------------------------------------

class TestPipelineGating:
    """Lane B is config-gated (default-off) and only triggers on complex flag."""

    def test_lane_b_default_off(self, tmp_db):
        """Default pipeline has lane_b_enabled=False — lane B never runs."""
        from engine.intent.pipeline import IntentPipeline
        pipe = IntentPipeline()
        assert pipe.lane_b_enabled is False
        assert pipe._lane_b_executor is None

    def test_lane_b_triggers_on_complex_flag(self, tmp_db):
        """With lane_b_enabled=True and a complex intent, lane B runs."""
        from engine.intent.pipeline import IntentPipeline, get_laneb_events

        def fake_gen(port, messages):
            return _make_code_gen_response(_simple_run_code())

        executor = LaneBExecutor(
            config=LaneBConfig(docker_required=False, sandbox_timeout_sec=10),
            code_gen_caller=fake_gen,
        )
        pipe = IntentPipeline(
            lane_b_enabled=True,
            lane_b_executor=executor,
        )
        req = IntentRequest(
            text="A highly complex and ambiguous intent.",
            context={"complex": True, "intent_params": {}},
            run_id="laneb-run-1",
        )
        res = pipe.dispatch(req)
        assert res.lane.value == "bplus"
        # Telemetry row written.
        events = get_laneb_events(run_id="laneb-run-1")
        assert len(events) >= 1
        assert events[0]["ok"] == 1

    def test_lane_b_not_triggered_without_flag(self, tmp_db):
        """lane_b_enabled=True but intent NOT flagged complex → lane B skipped."""
        from engine.intent.pipeline import IntentPipeline, get_laneb_events

        def fake_gen(port, messages):
            return _make_code_gen_response(_simple_run_code())

        executor = LaneBExecutor(
            config=LaneBConfig(docker_required=False, sandbox_timeout_sec=10),
            code_gen_caller=fake_gen,
        )
        pipe = IntentPipeline(
            lane_b_enabled=True,
            lane_b_executor=executor,
        )
        # A clear-keyword intent that the router resolves (no complex flag).
        req = IntentRequest(
            text="Run the tests for T01.",
            context={},  # no complex/ambiguous flag
            run_id="laneb-run-2",
        )
        res = pipe.dispatch(req)
        # Router resolves it (run_tests), lane B never runs.
        assert res.action == "run_tests"
        assert res.lane.value == "router"
        events = get_laneb_events(run_id="laneb-run-2")
        assert events == []


# ---------------------------------------------------------------------------
# LaneBResult shape
# ---------------------------------------------------------------------------

class TestLaneBResult:
    """LaneBResult carries the full audit trail."""

    def test_result_has_events_list(self, base_config):
        def fake_gen(port, messages):
            return _make_code_gen_response(_simple_run_code())

        executor = LaneBExecutor(
            config=base_config,
            code_gen_caller=fake_gen,
        )
        result = executor.execute("do something", run_id="r1")
        assert isinstance(result.events, list)
        assert result.run_id == "r1"
        assert result.latency_ms >= 0.0
        assert LaneBEventType.CODE_ACCEPTED.value in result.events
        assert LaneBEventType.RESULT_OK.value in result.events
