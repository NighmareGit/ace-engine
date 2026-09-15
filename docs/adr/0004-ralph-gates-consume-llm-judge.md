# ADR 0004 — Ralph gates consume LLM Judge output; ADR-0002 scope is the in-task retry loop

**Status:** accepted
**Decided:** S5 grill of the ace-meta-cognitive-ralph-loop epic (round 2, QB)
**Narrows:** ADR-0002 (judge-steered retry and escalation) — does NOT amend it
**Linked design:** `docs/epics/EPIC-ace-meta-cognitive-ralph-loop.md`,
`.scratch/ralph-loop/research/redesign-s2.md` §4

## Context

ADR-0002 settled that the LLM Judge (`:8080`, 35B) is telemetry, never a gate:
"Keep the deterministic gate as the sole driver of the generate→validate retry
loop. Do **not** call the LLM Judge inside the loop." The Ralph epic's gate
table makes `llm_judge.py:score_task()` the Review and RedTeam gates whose
pass/fail steers the round driver's post-commit revert decisions — an apparent
contradiction surfaced during the S5 grill against `CONTEXT.md`.

## Decision

The telemetry/gate distinction is **pipeline-context-dependent**:

- **Normal pipeline (ADR-0002 holds, unchanged):** the LLM Judge is telemetry.
  Its scores/reasoning feed escalation events; the deterministic gate alone
  drives the in-task generate→validate→test retry loop.
- **Ralph workflow (this ADR):** the same `llm_judge.py` scorer — unchanged,
  still emitting 5-dimension scores — feeds a **Ralph gate** whose pass/fail is
  computed by the round driver (threshold logic over the score), and whose
  verdict drives **post-commit revert** (`git reset --hard <pre-round-sha>`),
  not in-loop retry.

This is a narrowing, not an amendment: ADR-0002's rejection reasons (stochastic
latency inside the retry loop, judge steering generation) do not apply to the
Ralph context because (a) Ralph gates run once per round, post-commit — there
is no retry budget to burn; (b) the failure action is a revert, not another
generation attempt; (c) R1's model-separation requirement is satisfied (35B
judge on :8080 vs 9B generator on :8082, `judge_port != subject_port` guard).

Terminology: the gate is the **round driver's consumption** of the score, not
the scorer itself. The LLM Judge never claims to be a gate; `gates.py` wraps
the score in a `GateResult` owned by the round driver. See *Ralph gate* in
`CONTEXT.md`.

## Consequences

- `llm_judge.py` requires no changes; the new consumer is
  `engine/workflows/ralph/gates.py`.
- `CONTEXT.md`'s *Judge Gate vs LLM Judge* entry gains the Ralph-workflow
  clause; a distinct *Ralph gate* entry is added.
- A future change that moves Ralph gate verdicts into the in-task retry loop
  would require revisiting BOTH this ADR and ADR-0002 — recorded here so the
  boundary is explicit.
