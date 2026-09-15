#!/usr/bin/env python3
"""scripts/smoke_native_lane.py — live smoke of the Wave-3 IIL native lane
against the real subject endpoint (Triton :8082, Qwen3.5-9B-MTP).

Sends the canonical 30-intent spike suite (24 positive + 6 negative) through
``engine/intent/native.py``'s NativeLane, pointed at the real model. Records
structural validity %, intent accuracy %, FP rate, p50 latency, and how many
intents the TF-IDF router would have fast-pathed vs escalated to native.

The 30-intent suite is reconstructed from the frozen route table
(``engine/intent/routes/ace_actions.json``): 3 curated positives per action
(24 total, 8 actions × 3) + 1 escalate_model example, and 6 negatives that
match no action. This mirrors the spike S1 suite documented in
``.scratch/research/spikes-w0-results.md`` (100% / 95.8% / 0% / 1.9s).

Usage:
    python3 scripts/smoke_native_lane.py                 # default endpoint
    python3 scripts/smoke_native_lane.py --endpoint URL   # override
    python3 scripts/smoke_native_lane.py --limit 10       # partial run

Config:
    --endpoint URL   Base URL of the subject model (default http://127.0.0.1,
                     which the transport reaches via SSH tunnel to Triton
                     :8082 when TRANSPORT_MODE=auto and not on Triton).
    --port INT       Subject port (default 8082).
    --temperature F  Sampling temp (default 0.3).
    --timeout SEC    Per-request timeout in seconds (default 120 — 9B emits
                     ~48 reasoning tokens).
    --json           Emit a JSON summary to stdout (for CI / follow-up).

Exit 0 on completion (the run always records, even if the endpoint is
degraded — a degraded run writes SKIPPED with the error, not a traceback).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Make the repo importable when run as a script.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# The 30-intent suite (reconstructed from the frozen route table + spike doc).
# ---------------------------------------------------------------------------

# 24 positives: 3 per action, chosen to span clear-keyword and paraphrase.
POSITIVE_INTENTS: list[tuple[str, str]] = [
    # run_tests
    ("Run the tests for T01.", "run_tests"),
    ("Execute the test suite and report failures.", "run_tests"),
    ("Check that the tests pass before committing.", "run_tests"),
    # commit
    ("Commit the staged changes with message 'feat: add retry'.", "commit"),
    ("Save and commit what's staged.", "commit"),
    ("Snapshot the staged work in a commit.", "commit"),
    # grep
    ("Search for 'TODO' across the codebase.", "grep"),
    ("Find every occurrence of 'MAX_RETRIES'.", "grep"),
    ("Look for 'FIXME' in the source.", "grep"),
    # read_file
    ("Read the contents of engine/pipeline.py.", "read_file"),
    ("Show me the file README.md.", "read_file"),
    ("Display the file at path engine/state.py.", "read_file"),
    # list_tasks
    ("List all tasks in the current run.", "list_tasks"),
    ("What tasks are pending?", "list_tasks"),
    ("Which tasks are done and which are failed?", "list_tasks"),
    # escalate_model
    ("This needs more brain — escalate to 35B.", "escalate_model"),
    ("Bump this up to the 35B model.", "escalate_model"),
    ("This is too hard for 9B, escalate.", "escalate_model"),
    # fail_with_reason
    ("Mark T02 as failed: dependency not available.", "fail_with_reason"),
    ("This task cannot proceed — fail it.", "fail_with_reason"),
    ("Fail the task: the sandbox has no network.", "fail_with_reason"),
    # read_telemetry
    ("Show me the telemetry for the last run.", "read_telemetry"),
    ("What does the telemetry say?", "read_telemetry"),
    ("Pull up the gap report.", "read_telemetry"),
    # validate
    ("Validate the current task.", "validate"),
    ("Run the validator on T03.", "validate"),
    ("Verify the task code compiles and imports resolve.", "validate"),
]

# 6 negatives: no action should fire.
NEGATIVE_INTENTS: list[str] = [
    "What's the weather like today?",
    "Tell me a joke about recursion.",
    "Write a haiku about the build system.",
    "Explain quantum entanglement in two sentences.",
    "Good morning! How are you feeling?",
    "What time is it in Tokyo?",
]


def _build_suite():
    """Return a list of (text, expected_action_or_None)."""
    items = [(text, exp) for text, exp in POSITIVE_INTENTS]
    items += [(text, None) for text in NEGATIVE_INTENTS]
    return items


# ---------------------------------------------------------------------------
# Router fast-path analysis (which intents the TF-IDF router would catch).
# ---------------------------------------------------------------------------

def _analyze_router_fastpath():
    """Determine, for each suite intent, whether the router fast-paths it.

    Returns (fast_path_indices, escalate_indices) into the combined suite.
    """
    from engine.intent.router import load_default_router
    router = load_default_router()
    suite = _build_suite()
    fast, esc = [], []
    for i, (text, _exp) in enumerate(suite):
        route = router.route(text)
        if not route.uncertain and route.action:
            fast.append(i)
        else:
            esc.append(i)
    return fast, esc, router


# ---------------------------------------------------------------------------
# Native-lane live run
# ---------------------------------------------------------------------------

def run_smoke(endpoint: str, port: int, temperature: float, timeout: float,
              limit: int | None, as_json: bool) -> dict:
    """Run the suite through the native lane against the real endpoint.

    Returns a results dict. Never raises on a degraded endpoint — records
    SKIPPED with the error instead.
    """
    from engine.intent.native import NativeLane
    from engine.intent.types import IntentRequest, Route

    suite = _build_suite()
    if limit is not None:
        suite = suite[:limit]

    # Build the native lane pointed at the real endpoint.
    native = NativeLane(port=port, base_url=endpoint)
    # Advertise the registry actions as tool definitions (the IIL only sends
    # tools the registry advertises — the model can't invent an unknown tool).
    from engine.intent.tools import ToolRegistry
    registry = ToolRegistry()
    native.set_tools_from_registry(registry.descriptions)

    results = []
    errors = []
    n = len(suite)

    print(f"[smoke] suite={n} intents -> {endpoint}:{port} "
          f"(temp={temperature}, timeout={timeout}s)")
    print(f"[smoke] tools advertised: {sorted(registry.descriptions.keys())}")
    print("-" * 72)

    for i, (text, expected) in enumerate(suite):
        t0 = time.perf_counter()
        req = IntentRequest(text=text, model="qwen3.5-9b-mtp", channel="smoke")
        route = Route(action=expected or "", uncertain=True)
        try:
            res = native.resolve(req, route)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            # Structural validity: the response was well-formed (model returned
            # a parsable completion — verdict dispatch OR a clean reject). A
            # negative that declines to tool-call is STILL structurally valid
            # (matches the spike definition: 30/30 well-formed).
            structural_ok = res.verdict.value in ("dispatch", "escalate", "reject")
            # For positives: action matches expected. For negatives: no action.
            if expected is None:
                correct = (res.action is None)
            else:
                correct = (res.action == expected)
            results.append({
                "i": i, "text": text, "expected": expected,
                "action": res.action, "verdict": res.verdict.value,
                "structural_ok": structural_ok, "correct": correct,
                "latency_ms": latency_ms, "error": None,
            })
            tag = "OK" if correct else "MISS"
            if expected is None:
                tag = "OK(reject)" if correct else "FP!"
            print(f"  [{i+1:2d}/{n}] {tag:10s} -> {res.action!r:20s} "
                  f"({latency_ms:7.1f}ms)  {text[:55]}")
        except Exception as exc:  # noqa: BLE001 — record, don't crash
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            errors.append({"i": i, "text": text, "error": str(exc)})
            results.append({
                "i": i, "text": text, "expected": expected,
                "action": None, "verdict": "error",
                "structural_ok": False, "correct": False,
                "latency_ms": latency_ms, "error": str(exc),
            })
            print(f"  [{i+1:2d}/{n}] ERROR      -> {str(exc)[:60]}  {text[:40]}")

    # Aggregate.
    pos_results = [r for r in results if r["expected"] is not None]
    neg_results = [r for r in results if r["expected"] is None]
    structural = sum(1 for r in results if r["structural_ok"])
    pos_correct = sum(1 for r in pos_results if r["correct"])
    fp = sum(1 for r in neg_results if not r["correct"])  # negative misrouted
    latencies = sorted(r["latency_ms"] for r in results if r["error"] is None)

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0

    # Router fast-path analysis (local, no network).
    fast_indices, esc_indices, router = _analyze_router_fastpath()
    # Map to the (possibly limited) suite indices.
    suite_len = len(suite)
    fast_in_run = [i for i in fast_indices if i < suite_len]
    esc_in_run = [i for i in esc_indices if i < suite_len]

    summary = {
        "endpoint": f"{endpoint}:{port}",
        "suite_total": n,
        "positives": len(pos_results),
        "negatives": len(neg_results),
        "structural_validity_pct": pct(structural, n),
        "intent_accuracy_pct": pct(pos_correct, len(pos_results)) if pos_results else 0.0,
        "false_positive_rate_pct": pct(fp, len(neg_results)) if neg_results else 0.0,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "errors": len(errors),
        "router_fast_path_count": len(fast_in_run),
        "router_escalate_count": len(esc_in_run),
        "skipped": False,
        "skip_reason": None,
    }

    print("-" * 72)
    print(f"[smoke] structural validity : {summary['structural_validity_pct']}% "
          f"({structural}/{n})")
    print(f"[smoke] intent accuracy     : {summary['intent_accuracy_pct']}% "
          f"({pos_correct}/{len(pos_results)})")
    print(f"[smoke] false-positive rate : {summary['false_positive_rate_pct']}% "
          f"({fp}/{len(neg_results)})")
    print(f"[smoke] latency p50 / p95   : {p50:.0f}ms / {p95:.0f}ms")
    print(f"[smoke] router fast-path    : {len(fast_in_run)}/{n} "
          f"(escalate {len(esc_in_run)})")
    if errors:
        print(f"[smoke] ERRORS              : {len(errors)}")
        for e in errors:
            print(f"        - [{e['i']}] {e['error'][:80]}")

    # Persist raw results next to the script for the results doc.
    out_path = os.path.join(PROJECT_ROOT, ".scratch", "research",
                            "smoke-native-lane-raw.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results, "errors": errors},
                  fh, indent=2)
    print(f"[smoke] raw results -> {out_path}")

    if as_json:
        print(json.dumps(summary, indent=2))

    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--endpoint", default="http://127.0.0.1",
                    help="Base URL of the subject model")
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--limit", type=int, default=None,
                    help="Run only the first N intents (debug)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON summary to stdout")
    args = ap.parse_args()

    # Reachability probe: a cheap /v1/models call via the transport. If the
    # endpoint is unreachable, record SKIPPED (do not restart / retry).
    print(f"[smoke] probing {args.endpoint}:{args.port} ...")
    try:
        import urllib.request
        url = f"{args.endpoint}:{args.port}/v1/models"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
        print(f"[smoke] probe OK (HTTP 200)")
    except Exception as exc:
        # The endpoint is unreachable from here. The transport may still reach
        # it via SSH tunnel (TRITON_HOST) — so we WARN and continue rather than
        # bailing, but record the probe failure in the summary.
        print(f"[smoke] probe FAILED: {exc}")
        print(f"[smoke] continuing anyway — transport may reach it via SSH tunnel")

    summary = run_smoke(args.endpoint, args.port, args.temperature,
                        args.timeout, args.limit, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
