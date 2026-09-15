# CONTEXT.md — Epic-5 ACE Hardening Glossary

Ubiquitous language for the Epic-5 hardening design. Terms crystallized from the
failure analysis (`research/epic5-first-run-failures-2026-09-09.md`) and the
design (`docs/epic5-hardening-design.md`). Glossary only — no implementation
details. Cross-references the design doc's decision register.

---

## A

### Archive (Results Archive)
The dedicated Gitea repo `<user>/ace-results` that stores per-run engine state. For
each `run_id` it holds a snapshot of `engine.db` and a JSON `report.json`. The
archive is the source of truth for autopsies; the live engine DB is
`ENGINE_DB_PATH=/home/<user>/ace-results/engine.db`. See *Autopsy*.

### Autopsy
A post-hoc forensic read of a completed run, pulled from the *Archive* via
`fetch_run(run_id)` / `ace archive pull <run_id>`. Consumes `report.json` (and
optionally the `.db` snapshot) to reconstruct why a run or task failed. Never
requires re-running the engine.

---

## C

### CCBS
Config Capability Benchmark Suite. The two-layer system that consumes engine
output (scores + `scored_state` + deterministic verdict) to rank model configs.
CCBS is a consumer of engine telemetry, not part of the engine itself.

### Cross-stage cap
`MAX_TOTAL_ATTEMPTS_PER_TASK` — a per-task ceiling on the sum of generate + test
+ commit attempts. When total attempts reach it, the task fails and a
*Task escalation* event fires. Prevents unbounded COMMIT→QUEUED looping.

---

## D

### Deterministic gate
The validator's 4-stage check (empty → AST → syntax → imports → execution) that
decides whether generated code passes. It is the *gate* that drives retries: fast,
free, and reproducible. Distinct from the *LLM Judge*, which is quality telemetry.
See *Judge Gate vs LLM Judge*.

---

## E

### Engine.db
The sole SQLite database for engine state (Rule 6: never `benchmark-results.db`).
Lives at `ENGINE_DB_PATH` (standard path `/home/<user>/ace-results/engine.db`).
Holds `engine_runs`, `engine_task_results`, `judge_verdicts`, `engine_scores`,
`schema_version`.

### ErrorContext
The structured feedback (previous code, validation errors, test failures, attempt
number) appended to the prompt on each retry. Flows the *Deterministic gate*'s
findings back into generation. Does **not** include LLM Judge reasoning.

### Escalation event
A fire-and-forget `task_escalation` event emitted over the `emit_event` bridge when
a task exhausts retries (validation, test, or commit). Carries run_id, task_id,
stage, attempts, last errors, full *Validation stage* trace, LLM Judge scores,
and judge reasoning. Consumable by an orchestrator via `Engine(on_event=…)`.
Escalation never blocks or crashes the pipeline.

### Exhaustion
See *Reasoning exhaustion* and *Validation exhaustion*.

---

## I

### Ideation
The divergent first phase of a Ralph round: generate multiple candidate plans,
score them on feasibility, alignment, and novelty, and select one. Budget-capped
and mechanical — novelty is computed against prior selected plans, not
LLM-trusted. See *Ideation collapse*.

### Ideation collapse
A signal that fires when a Ralph run selects mechanically similar plans across
consecutive rounds, detected by TF-IDF cosine similarity against prior selected
plan text. Triggers a forced-diversity instruction in the next round's ideation
and emits a telemetry event. Does not fail the run.

---

## J

### Judge Gate vs LLM Judge
Two distinct roles:
- **Judge gate** (deterministic): the *Deterministic gate* — pass/fail, drives
  retries, writes `judge_verdicts`.
- **LLM Judge**: the model on `:8080` (Qwen3.6-35B) that emits 5-dimension quality
  scores (`engine_scores`). In the normal pipeline, telemetry never a gate — its
  reasoning feeds *Escalation events*, not the retry loop (ADR-0002). In the Ralph
  workflow, the same scorer feeds a *Ralph gate* whose pass/fail the round driver
  consumes for post-commit revert decisions (ADR-0004). The judge never scores
  itself (`judge_port != subject_port`).

### Judge reasoning
The LLM Judge's free-text rationale for a score. Attached to the *Escalation
event* payload as evidence. Not used to steer the generate→validate loop.

---

## L

### Live run
An end-to-end engine execution on Triton against real BeeLlama endpoints
(subject `:8082`, judge `:8080`), as opposed to a dry-run / mock-transport test.
The failure analysis is based on 8 live runs + 1 debug run.

---

## O

### One-retry discipline
Scoring/judge calls get at most ONE retry, and only on transient failures
(timeout / 5xx / malformed JSON). A bad score is final data — never a reroll
trigger. Distinct from *generation* retries, which are the pipeline's own concern
and bounded by `max_retries_generate` / `max_retries_test`.

---

## P

### Pattern
A learned insight (strategy, lesson, anti-pattern, or heuristic) extracted from
a Ralph round. Canonical store is the `ralph_patterns` table in engine.db. A
pattern is `proposed` on creation and becomes `approved` only through the
mechanical approval gate; prompt injection reads approved rows only. See
*Pattern projection*.

### Pattern projection
The one-way export of approved patterns to markdown skill-docs in the skill
registry. The projection is regenerated from SQL on approval and never read back
as source of truth. Seeds the G9 skill registry for human and agent consumption.

---

## R

### Ralph gate
A post-commit verification step in the Ralph workflow that consumes the LLM
Judge's scores to decide whether a committed round proceeds or is reverted. Owned
by the round driver, not the pipeline — distinct from the in-task *Judge gate*
that drives the generate→validate retry loop. See *Judge Gate vs LLM Judge*,
ADR-0004.

### Ralph round
One iteration of the Ralph loop: divergent ideation, a single ACE run through
the existing pipeline, post-commit gate evaluation, and a RoundReport. The unit
of fresh-agent execution — the next round sees only the RoundReport, the pinned
workspace, and the objective. See *RoundReport*.

### Ralph run
A complete Ralph execution: one or more *Ralph rounds* grouped under a single
`ralph_run_id`. Resumable — a crashed run continues from the last completed
round. Distinct from an ACE engine run, though each Ralph round dispatches one.

### RoundReport
The typed record that crosses the round boundary. The only structured data the
next Ralph round receives, alongside the pinned workspace and the original
objective. Encodes the selected plan, gate verdict, ideation summary, pattern
proposals, and budget consumed. Bounded to 64KB serialized.
The signature where a thinking-style model emits `reasoning_content` first and,
at too-low `max_tokens`, returns empty `content` with `finish_reason=length`.
Detected as: empty content + `finish_reason=length` + (non-empty `reasoning_content`
or `thinking_tokens > 0`). Triggers a single budget-escalation retry; if it recurs,
surfaces a distinct `reasoning exhausted` error rather than a generic validation
failure.

### Reasoning budget
The `max_tokens` allocated per generation, selected by role
(`max_tokens_by_role`: orchestrator/multi-turn/coder). The *Reasoning-budget
strategy* uses a static floor with a single adaptive escalation on *Reasoning
exhaustion*.

### Results Archive
See *Archive*.

### Retryable vs PERMANENT (commit)
The committer's classification of a push failure. PERMANENT (auth/401/403/404/
permission/not-found) fails the task immediately; retryable re-enters the
commit/generate loop. Previously computed but ignored by the engine.

---

## S

### Scored_state
The state code was in when the LLM Judge scored it: `validation_failed` |
`test_failed` | `committed`. Flagged on every `engine_scores` row so CCBS can
filter/weight. Scoring is never suppressed; the flag makes the score
interpretable.

### Stage Telemetry
The per-stage validation trace (`[{stage, file, passed, error}]`) persisted to
`engine_task_results.validation_result` on every failed attempt and carried into
`TaskResult.validation_stages` / `report.json`. Replaces the old count-only
"validation failed after N attempts" message.

### Subject
The model under test (9B-MTP on `:8082`). Distinct from the *LLM Judge* on `:8080`.

---

## T

### Task escalation
See *Escalation event*.

### TaskResult
The per-task output record. Gains `validation_stages` (the last failed attempt's
*Stage Telemetry*) and carries `scores` (including `scored_state`) into
`report.json`.

---

## V

### Validation exhaustion
When the generate→validate loop burns through `max_retries_generate` attempts
without passing the *Deterministic gate*. The terminal failure now records the
failing stage name + file + error and emits a *Task escalation* event.

### Validation stage
One step of the *Deterministic gate*: `empty`, `ast`, `syntax`, `imports`,
`execution`. Each produces `{stage, file, passed, error}`. Stages 0–3
short-circuit; stage 4 continues to the next file. See *Stage Telemetry*.

---

## Retrieval + Memory

Three additive features integrated into the engine run loop (DESIGN-G4). Each
activates on a disjoint dispatch branch or behind a feature flag — code atoms
are structurally unaffected.

### Retrieval (G2)
`enable_retrieval` flag (default `False`).  Fires only for research atoms
(CONTEXT stage): `RepoMapBuilder` builds a tree-sitter map of the workspace,
and `assemble_retrieved()` injects the rendered chunks into the prompt wrapped
in `--- UNTRUSTED RETRIEVAL CONTENT ---` markers with per-atom-type token
budgets.  Flag off → research atoms behave identically to baseline.

### Memory recall (G3)
`enable_memory_recall` flag (default `False`).  Fires only in ralph ideation:
`recall_for_objective()` retrieves approved claims (poisoning-gated via
`ace memory approve/reject`) and injects them into novelty signals.  One-way
contract: gate writes (`approve_for_recall`), recall reads
(`status='approved'` only).  Flag off → ideation unaffected.

### Edit-ops (G1)
New `task_type="edit"` dispatched when a PRD has `category: edit`.  Uses
Aider-style SEARCH/REPLACE fences, exact-match `apply_blocks()` seam (multi-match
fails loud), post-apply `ast.parse`, commit-gate read-back.  No feature flag —
the branch only activates for edit-category PRDs (zero blast radius).

→ Full design: [docs/features/retrieval-memory/](docs/features/retrieval-memory/)
