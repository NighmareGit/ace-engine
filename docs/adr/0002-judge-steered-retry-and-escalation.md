# ADR 0002 — Deterministic gate stays the retry driver; LLM Judge feeds escalation, not the loop

**Status:** proposed
**Decided:** 2026-09-09
**Linked design:** `docs/epic5-hardening-design.md` §5 (Judge-steered retry + escalation)

## Context

The ACE engine has two "judges" with very different latency, cost, and
determinism profiles:

- The **deterministic gate** (`engine/validator.py`): a 4-stage check (empty →
  AST → syntax → imports → execution). Fast, free, reproducible. Drives the
  generate→validate retry loop today.
- The **LLM Judge** (`engine/llm_judge.py`): the model on `:8080` (Qwen3.6-35B)
  that emits 5-dimension quality scores. Slow (network + inference), stochastic,
  and bound by the one-retry scoring discipline. Today it runs **post-hoc only** —
  its verdict never influences retries or escalation.

The failure analysis found that retries exhaust with no orchestrator visibility
and no judge-informed recovery. The tempting fix is to call the LLM Judge inside
the retry loop so its reasoning steers regeneration. The runbook's one-retry
scoring discipline and the cost/latency budget make that loaded.

## Decision

**Keep the deterministic gate as the sole driver of the generate→validate retry
loop.** Do **not** call the LLM Judge inside the loop.

Instead, capture the LLM Judge's reasoning and attach it to a fire-and-forget
**`task_escalation` event** emitted when retries exhaust. The event payload
carries: run_id, task_id, stage, attempts, last errors, the full validation-stage
trace, the LLM Judge scores, and the judge reasoning. An orchestrator subscribes
via `Engine(on_event=…)` and decides what to do (pause / run / human review /
model swap). Escalation never blocks or crashes the pipeline.

As a prerequisite, fix the currently-dead `emit_event` bridge: its signature is
`emit_event(on_event, event_type, data)` (3 args, `events.py:6`) but all three
call sites in `engine.py` call it with 2 args, so escalation cannot fire today.

Also wire the two dead safety nets: `MAX_TOTAL_ATTEMPTS_PER_TASK` becomes a real
cross-stage attempt cap (escalate + fail at the cap), and `_recheck_transport` is
called once on first transport failure.

## Consequences

**Positive**
- The retry loop stays fast, free, and deterministic — no LLM call multiplied
  across retries-per-task. Generation speed is decoupled from judge availability.
- The one-retry scoring discipline is respected: the judge is still called at
  most twice (initial + one transient retry), and its reasoning is preserved as
  evidence rather than consumed inline.
- The orchestrator gets a rich, structured signal (scores + reasoning + full
  stage trace) at exactly the right moment (terminal failure), which is what it
  needs to act — without the engine presuming what that action should be.
- Fixing the `emit_event` calling convention unblocks every future use of the
  event bridge, not just escalation.

**Negative / trade-offs**
- Recovery from a bad generation is slower: the loop can't ask the judge "what's
  wrong" mid-flight; it only learns at escalation time. This is intentional —
  the cost/latency budget is spent on telemetry, not on a speculative per-retry
  LLM call.
- The escalation event is fire-and-forget; if the orchestrator is absent, the
  signal is observable only in logs/export. This is acceptable because the engine
  must not depend on an orchestrator to finish a run.
- Two distinct "judge" concepts (gate vs telemetry) now coexist; the glossary
  (`CONTEXT.md`, *Judge Gate vs LLM Judge*) exists precisely to keep them apart.

## Alternatives considered

1. **LLM Judge steers the retry loop** (call judge per retry, feed reasoning
   into `ErrorContext`). Rejected: multiplies latency/cost by retries-per-task,
   couples generation to judge availability, risks burning the one-retry scoring
   budget on generation steering, and blurs the gate/telemetry separation. The
   deterministic gate already gives precise, actionable stage errors.
2. **LLM Judge as the gate** (replace the validator with the judge). Rejected:
   the judge is stochastic and slow; a gate must be deterministic and fast. A
   stochastic gate would make the pass/fail decision irreproducible — the
   opposite of what a gate needs to be.
3. **Post-hoc scoring only, no escalation** (status quo + just fix the bugs).
   Rejected: leaves the "no orchestrator visibility on exhaustion" gap open; the
   failure analysis showed that gap is operationally painful. Escalation is the
   minimal bridge that closes it without coupling.

## Consequences of not doing this

If the LLM Judge were pulled into the loop, each task would cost an extra
judge call per retry (×3 generate retries default), generation throughput would
drop to judge speed, and the one-retry scoring discipline would be violated or
re-explained. If escalation were omitted, terminal failures would remain
invisible to any orchestrator — recreating the diagnosability gap this design
exists to close.
