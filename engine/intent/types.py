"""IIL types layer — pure dataclasses, no logic (three-layer separation).

Every contract the Intent Interception Layer exports lives here: the request
that enters the layer, the route the pre-router assigns, and the result that
leaves it. No imports from the engine, no I/O — this module is the stable
seam that runtime.py and protocol.py plug into (grok addendum, tickets #7/#11).

The three-layer split:
  types.py     — THESE contracts (import-safe from anywhere)
  runtime.py   — router + native lane + pipeline (the mechanical engine)
  protocol.py  — wire formats, OpenAI/jinja bridge, ast.literal_eval parser
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Lane(str, Enum):
    """Which answering lane produced the result (FROZEN lane table)."""
    ROUTER = "router"        # B+ TF-IDF mechanical pre-router (fast path)
    NATIVE = "native"        # A native OpenAI tool-call lane (escalation)
    FALLBACK = "fallback"    # C fallback (Hammer-class) — NOT this wave
    BPLUS = "bplus"          # B+ capped RLM bypass (code-as-intent, llm_query)
    NONE = "none"            # no lane answered (rejected / unroutable)


class Verdict(str, Enum):
    """Dispatch verdict for an intent."""
    DISPATCH = "dispatch"    # routed + validated → dispatch the tool
    ESCALATE = "escalate"    # uncertain → escalate to native lane
    REJECT = "reject"        # no matching action / not in registry


@dataclass
class IntentRequest:
    """A model-emitted intent entering the IIL.

    Attributes:
        text: the raw utterance / content emitted by the model.
        model: model id that emitted the intent (telemetry).
        channel: emission channel (e.g. "tool_call", "content", "plan").
        task_id: optional originating task (telemetry / TGD evidence).
        run_id: optional originating run (telemetry / TGD evidence).
        context: optional extra context dict (caller-controlled, not parsed).
    """
    text: str
    model: str = "unknown"
    channel: str = "content"
    task_id: str | None = None
    run_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Route:
    """The pre-router's routing decision for one intent.

    Attributes:
        action: the ACE action name the router matched (e.g. "run_tests").
        confidence: cosine / similarity score in [0, 1].
        runner_up_action: second-best action (for gap computation).
        runner_up_confidence: second-best score.
        uncertain: True when below threshold or gap (the ~6% edge).
        latency_ms: router latency in ms (perf-law telemetry).
        route_table: name of the route table used (versioning / drift alarm).
    """
    action: str
    confidence: float = 0.0
    runner_up_action: str | None = None
    runner_up_confidence: float = 0.0
    uncertain: bool = True
    latency_ms: float = 0.0
    route_table: str = "default"


@dataclass
class IntentResult:
    """The result the IIL returns to its caller.

    Attributes:
        request: the originating request (echoed for correlation).
        verdict: dispatch / escalate / reject.
        route: the router's decision (even on reject — feeds G7 telemetry).
        lane: which lane ultimately answered.
        action: resolved action name (tool to dispatch).
        arguments: parsed tool arguments (dict) if available.
        raw: the raw lane output (native tool-call payload, etc.).
        latency_ms: end-to-end IIL latency in ms.
        reason: human-readable explanation (esp. on reject / escalate).
        telemetry: dict of telemetry fields emitted (route, confidence, lane).
    """
    request: IntentRequest
    verdict: Verdict
    route: Route
    lane: Lane = Lane.NONE
    action: str | None = None
    arguments: dict[str, Any] | None = None
    raw: Any = None
    latency_ms: float = 0.0
    reason: str = ""
    telemetry: dict[str, Any] = field(default_factory=dict)

    def to_telemetry_row(self) -> dict[str, Any]:
        """Flatten to a telemetry-row dict for the intent_events table."""
        return {
            "model": self.request.model,
            "channel": self.request.channel,
            "task_id": self.request.task_id,
            "run_id": self.request.run_id,
            "verdict": self.verdict.value,
            "lane": self.lane.value,
            "action": self.action or "",
            "confidence": self.route.confidence,
            "uncertain": self.route.uncertain,
            "latency_ms": self.latency_ms,
            "route_action": self.route.action,
            "route_table": self.route.route_table,
        }


# ---------------------------------------------------------------------------
# Lane B+ (capped RLM bypass) types
# ---------------------------------------------------------------------------

class LaneBEventType(str, Enum):
    """Telemetry event types for the lane-B+ executor (additive contract)."""
    CODE_ACCEPTED = "code_accepted"
    CODE_REJECTED = "code_rejected"
    LLM_QUERY_QUEUED = "llm_query_queued"
    LLM_QUERY_ANSWERED = "llm_query_answered"
    DEPTH_REFUSED = "depth_refused"
    BUDGET_ABORT = "budget_abort"
    WALL_CLOCK_ABORT = "wall_clock_abort"
    RESULT_OK = "result_ok"
    RESULT_FAIL = "result_fail"
    DOCKER_REFUSED = "docker_refused"


@dataclass
class LaneBConfig:
    """Tunables for the lane-B+ executor (spec §B+, grill G3).

    All budgets are BINDING. Lane B is a capped bypass, not the
    orchestrator — exceeding any budget aborts with a partial result +
    telemetry, never silently continues.
    """
    # --- model endpoints ---
    # Subject model that writes the intent code (code-gen).
    code_gen_port: int = 8082
    code_gen_base_url: str = "http://127.0.0.1"
    code_gen_model: str = "qwen3.5-9b-mtp"
    # Judge model that answers llm_query() queued questions.
    judge_port: int = 8080
    judge_base_url: str = "http://127.0.0.1"
    judge_model: str = "qwen3.6-35b"

    # --- binding budgets ---
    max_llm_queries: int = 4        # call-count budget (binding)
    max_wall_clock_ms: int = 30000  # wall-clock budget in ms (0 = unbounded)
    max_depth: int = 2              # llm_query recursion depth cap (binding)

    # --- sandbox ---
    # When True, lane B REJECTS (never falls back to subprocess) if the docker
    # backend is unavailable. Hard rule: untrusted code runs ONLY in docker.
    docker_required: bool = True
    sandbox_timeout_sec: int = 30

    # --- retries ---
    # Code-gen parse failures: reject + retry once, then fail.
    max_code_gen_retries: int = 1


@dataclass
class LaneBResult:
    """Result of one lane-B+ execution (code-as-intent).

    Returned to the pipeline so it can be surfaced as an IntentResult with
    lane=BPLUS. ``ok`` is False on any budget abort / rejection / failure;
    ``partial`` is True when a budget was hit mid-run but a value was
    produced before the abort.
    """
    ok: bool
    value: Any = None
    code: str | None = None           # the generated python code (audit)
    llm_query_count: int = 0          # how many llm_query() calls ran
    depth_used: int = 0               # max recursion depth reached
    partial: bool = False             # True if budget-aborted mid-run
    error: str | None = None          # human-readable failure reason
    events: list[str] = field(default_factory=list)  # LaneBEventType values
    latency_ms: float = 0.0
    run_id: str | None = None
