"""IIL dispatch pipeline — the mechanical path that feeds ORCH primitives.

The pipeline is the single entry point the orchestrator calls. It runs the
dispatch law:

    TF-IDF router first (the mechanical path, ≤10ms p50).
    HIGH confidence hit  → return immediately (the ~94% common case).
    UNCERTAIN / low conf → escalate to native lane A (the ~6% edge).

Telemetry: every dispatch emits a row to the additive ``intent_events``
table (engine.db, WAL). That table is the contract with the Tool-Gap
Detector (ORCH-7):
  * G7 (router uncertain)  reads rows WHERE uncertain=1
  * G10 (hallucinated tool) reads rows WHERE action NOT IN registry
The table is additive and never mutates existing telemetry (ADR-0002).

Perf law applies to the MECHANICAL path only (router). Native-lane latency
(~1.9s) is GPU reasoning overhead — accepted, documented in spikes.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from engine.intent.laneb import LaneBExecutor, register_host_tool
from engine.intent.native import NativeLane
from engine.intent.router import IntentRouter, load_default_router
from engine.intent.tools import ToolRegistry
from engine.intent.types import (
    IntentRequest, IntentResult, Lane, LaneBConfig, LaneBResult, Route, Verdict,
)

log = logging.getLogger("engine.intent.pipeline")

# Table name is the TGD contract — toolgap_detector's G7/G10 extractors
# read from EXACTLY this table. Renamed? Update BOTH sites.
INTENT_EVENTS_TABLE = "intent_events"

# Lane-B+ telemetry table (additive). TGD can read lane-B events from here.
# Schema: one row per lane-B execution (or per sub-event, see _write_laneb_row).
LANEB_EVENTS_TABLE = "laneb_events"


class IntentPipeline:
    """The IIL dispatch pipeline: router → (optional) native escalation.

    Attributes:
        router: the TF-IDF pre-router (B+ lane).
        native: the native lane-A adapter (escalation path).
        emit_telemetry: when True (default), write intent_events rows.
    """

    def __init__(
        self,
        router: IntentRouter | None = None,
        native: NativeLane | None = None,
        emit_telemetry: bool = True,
        # Lane B+ (capped RLM bypass). MUST be opted into per config —
        # capability is default-off (never default-on).
        lane_b_enabled: bool = False,
        lane_b_config: LaneBConfig | None = None,
        lane_b_executor: LaneBExecutor | None = None,
        registry: ToolRegistry | None = None,
        # Budget-counter integration (T8). Called as increment(tokens) -> dict.
        budget_increment: Callable[[int], dict] | None = None,
    ) -> None:
        self.router = router or load_default_router()
        self.native = native or NativeLane()
        self.emit_telemetry = emit_telemetry
        self.lane_b_enabled = lane_b_enabled
        self._budget_increment = budget_increment
        # The lane-B executor is lazily built (it needs the registry tools +
        # the budget increment closure). Build now if enabled, else None.
        self._lane_b_executor = None
        if lane_b_enabled:
            self._lane_b_executor = lane_b_executor or LaneBExecutor(
                config=lane_b_config or LaneBConfig(),
                registry_tools=(registry.descriptions if registry else {}),
                budget_increment=budget_increment,
            )
            # Wire host-side tool dispatch for every registered tool so lane-B
            # code's ctx.tool(name) calls resolve on the host.
            if registry:
                for name, entry in registry._tools.items():
                    if entry.callable is not None:
                        register_host_tool(name, entry.callable)

    # -- public API ----------------------------------------------------------

    def dispatch(self, request: IntentRequest) -> IntentResult:
        """Run the full dispatch path for one intent.

        Mechanical path (≤10ms p50): TF-IDF router. On a high-confidence hit
        return immediately. On uncertain/low-confidence, escalate to native.
        """
        t0 = time.perf_counter()

        # 1. Mechanical router (the perf-law path).
        route = self.router.route(request.text)

        if not route.uncertain and route.action:
            # High-confidence hit — return immediately (common case).
            result = IntentResult(
                request=request,
                verdict=Verdict.DISPATCH,
                route=route,
                lane=Lane.ROUTER,
                action=route.action,
                reason="router high-confidence hit",
            )
        elif not route.action:
            # No route matched at all — reject (nothing to escalate).
            result = IntentResult(
                request=request,
                verdict=Verdict.REJECT,
                route=route,
                lane=Lane.NONE,
                reason="no route matched the intent",
            )
        else:
            # Uncertain band — escalate to native lane A.
            result = self.native.resolve(request, route)
            if result.verdict == Verdict.DISPATCH:
                result.verdict = Verdict.ESCALATE  # mark as escalation

            # Lane B+ (capped RLM bypass): if lane A did not dispatch AND
            # lane B is enabled AND the intent is flagged complex/ambiguous,
            # try code-as-intent. Lane B sits BEHIND A's escalation.
            if (
                result.verdict != Verdict.DISPATCH
                and self.lane_b_enabled
                and self._lane_b_executor is not None
                and _is_complex_intent(request)
            ):
                laneb_result = self._lane_b_executor.execute(
                    intent_text=request.text,
                    intent_params=request.context.get("intent_params", {}),
                    run_id=request.run_id,
                )
                # Emit lane-B telemetry (additive table + hook).
                _write_laneb_row(laneb_result)
                if laneb_result.ok:
                    result = _laneb_to_intent_result(laneb_result, request, route)

        result.latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        result.telemetry = result.to_telemetry_row()

        # 2. Emit telemetry (feeds TGD G7/G10). Best-effort: a telemetry
        #    failure must never crash the dispatch path (layer survives).
        if self.emit_telemetry:
            try:
                _write_event(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("dispatch telemetry suppressed: %s", exc)

        return result

    def dispatch_text(self, text: str, **kwargs: Any) -> IntentResult:
        """Convenience: dispatch a raw string as an IntentRequest."""
        return self.dispatch(IntentRequest(text=text, **kwargs))

    @property
    def actions(self) -> list[str]:
        """Known actions (delegates to the router's route table)."""
        return self.router.actions


# ---------------------------------------------------------------------------
# Telemetry — the additive intent_events table (TGD G7/G10 contract).
# ---------------------------------------------------------------------------

def _ensure_events_table() -> None:
    """Create the additive intent_events table if absent (safe to repeat).

    Additive: never mutates existing tables. The schema is the contract with
    toolgap_detector G7 (uncertain=1 rows) and G10 (action-not-in-registry).
    """
    from engine.state import _get_conn
    conn = _get_conn()
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {INTENT_EVENTS_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        model TEXT,
        channel TEXT,
        task_id TEXT,
        run_id TEXT,
        verdict TEXT,
        lane TEXT,
        action TEXT,
        confidence REAL,
        uncertain INTEGER DEFAULT 0,
        latency_ms REAL,
        route_action TEXT,
        route_table TEXT
    )""")
    conn.commit()
    conn.close()


def _write_event(result: IntentResult) -> None:
    """Append one telemetry row. Best-effort: a telemetry failure must never
    crash the dispatch path (the layer survives, per design invariant)."""
    try:
        _ensure_events_table()
        from engine.state import _get_conn
        row = result.to_telemetry_row()
        conn = _get_conn()
        conn.execute(f"""INSERT INTO {INTENT_EVENTS_TABLE}
            (model, channel, task_id, run_id, verdict, lane, action,
             confidence, uncertain, latency_ms, route_action, route_table)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            row["model"], row["channel"], row["task_id"], row["run_id"],
            row["verdict"], row["lane"], row["action"],
            row["confidence"], int(row["uncertain"]),
            row["latency_ms"], row["route_action"], row["route_table"],
        ))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001 — telemetry must not break dispatch
        log.warning("intent_events write failed: %s", exc)


def get_intent_events(run_id: str | None = None, limit: int = 100) -> list[dict]:
    """Read intent_events rows (most recent first). Used by tests + tooling."""
    _ensure_events_table()
    from engine.state import _get_conn
    conn = _get_conn()
    if run_id:
        rows = conn.execute(
            f"SELECT * FROM {INTENT_EVENTS_TABLE} WHERE run_id = ? "
            f"ORDER BY id DESC LIMIT ?", (run_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {INTENT_EVENTS_TABLE} ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Lane B+ helpers
# ---------------------------------------------------------------------------

def _is_complex_intent(request: IntentRequest) -> bool:
    """Heuristic: is this intent complex/ambiguous enough to warrant lane B+?

    The trigger is the "complex" or "ambiguous" flag the caller set in
    context, OR an explicit lane=BPLUS hint. This is the policy gate — lane B
    only runs for intents flagged as needing the capped bypass.
    """
    ctx = request.context or {}
    if ctx.get("complex") or ctx.get("ambiguous"):
        return True
    if ctx.get("lane") == "bplus":
        return True
    return False


def _laneb_to_intent_result(laneb: LaneBResult, request: IntentRequest,
                            route: Route) -> IntentResult:
    """Convert a successful LaneBResult into an IntentResult (lane=BPLUS)."""
    return IntentResult(
        request=request,
        verdict=Verdict.DISPATCH,
        route=route,
        lane=Lane.BPLUS,
        action="laneb_code_intent",
        arguments={"value": laneb.value},
        raw={"code": laneb.code, "llm_query_count": laneb.llm_query_count,
             "depth_used": laneb.depth_used},
        latency_ms=laneb.latency_ms,
        reason="lane B+ (code-as-intent) resolved the intent",
        telemetry={
            "lane": Lane.BPLUS.value,
            "llm_query_count": laneb.llm_query_count,
            "depth_used": laneb.depth_used,
            "events": laneb.events,
        },
    )


def _ensure_laneb_table() -> None:
    """Create the additive laneb_events table if absent (safe to repeat).

    Additive: never mutates existing tables. Each row is one lane-B execution
    summary. TGD can read lane-B signals from here (code accepted/rejected,
    llm_query count, depth used, budget aborts, result ok/fail).
    """
    from engine.state import _get_conn
    conn = _get_conn()
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {LANEB_EVENTS_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        run_id TEXT,
        ok INTEGER DEFAULT 0,
        partial INTEGER DEFAULT 0,
        llm_query_count INTEGER DEFAULT 0,
        depth_used INTEGER DEFAULT 0,
        error TEXT,
        events TEXT,
        latency_ms REAL
    )""")
    conn.commit()
    conn.close()


def _write_laneb_row(result: LaneBResult) -> None:
    """Append one lane-B execution summary row. Best-effort."""
    import json as _json
    try:
        _ensure_laneb_table()
        from engine.state import _get_conn
        conn = _get_conn()
        conn.execute(f"""INSERT INTO {LANEB_EVENTS_TABLE}
            (run_id, ok, partial, llm_query_count, depth_used, error, events,
             latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
            result.run_id,
            int(bool(result.ok)),
            int(bool(result.partial)),
            result.llm_query_count,
            result.depth_used,
            result.error,
            _json.dumps(result.events),
            result.latency_ms,
        ))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001 — telemetry must not break dispatch
        log.warning("laneb_events write failed: %s", exc)


def get_laneb_events(run_id: str | None = None, limit: int = 100) -> list[dict]:
    """Read laneb_events rows (most recent first). Used by tests + TGD."""
    _ensure_laneb_table()
    from engine.state import _get_conn
    conn = _get_conn()
    if run_id:
        rows = conn.execute(
            f"SELECT * FROM {LANEB_EVENTS_TABLE} WHERE run_id = ? "
            f"ORDER BY id DESC LIMIT ?", (run_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {LANEB_EVENTS_TABLE} ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
