# RLM Ticket → ace-engine Mapping — SHARED CONTRACT v1

**Date:** 2026-09-09 · **Artifact of:** `dsh-recllm/docs/HANDOFF-ace-engine-schema-pass.md` W1
**Status:** AGREED (both repos). Schema: `schemas/rlm-ticket-v1.schema.json` (mirrored in both repos).
**Change control:** bumps to either file require a paired change in both repos (RLM Epic-7 contract tests + ace adapter tests gate on these files).

## Mapping table

| ace-engine `Task` field | RLM rev-2 source | Decision |
|---|---|---|
| `id` | `id` | **Accepted verbatim** (`EPIC-XXX-TXXX`, witnesses `W###`). Verified: engine treats IDs as opaque strings at runtime; the `T\d+` regex in `engine/prd.py` is markdown-parsing fallback only, bypassed by the adapter. |
| `title` | `title` | Direct. |
| `description` | `description` | Direct. |
| `module` | `files[0].path` | **First file entry** (primary target). NOT competence — `module` is the primary file path in the consumer (`Task.module` docstring), a different axis than the competence vocab. |
| `dependencies` | `dependencies` | Direct (ticket IDs, opaque strings). |
| `task_type` | `competence` | **Derived, closed mapping:** `implement`, `compile` → `implementation`; `test`, `attack` → `test`; `investigate` → `debug`; `explore`, `research`, `plan`, `review` → `implementation` (original competence preserved in `metadata.competence`). Rationale: consumer's 4-value type drives role/token selection only; the 9-value competence is the richer axis and must not be truncated at the schema level. |
| `priority` | `complexity` | **Derived:** `critical`→1, `high`→1, `medium`→2, `low`→3. |
| `prd_section` | synthesized | **Adapter synthesis:** `description` + `eats` rendered as "Evidence inputs: <path>" lines. RLM has no depth-1 PRD-section concept; the consumer uses this field as context text only. |
| `files` | `files[].path` | All paths (superset incl. `module`). |
| `acceptance_criteria` | `contract.success_criteria` | **Direct projection** — consumer reads the list of strings from the single contracted field (engine's deliberate fold, PRE-EPIC §6.1). No dual-write. |

## Dropped / engine-side-only fields

| RLM field | Consumer treatment |
|---|---|
| `skill`, `competence`, `recommended_model`, `confidence`, `mission_intent`, `metadata.*` | Preserved verbatim in `Task.metadata["rlm"]` (added dict field, default empty) — round-trippable, not executed on. |
| `eats` | v1: metadata + `prd_section` synthesis. v2: executor pre-loads these paths into context for the weak model. |
| `tests` (commands) | Executed by the consumer's TEST stage via existing test-runner plumbing. |

## Decisions on the seven W1 questions

1. **Field mapping** — table above. `module`↔`competence` was the wrong axis guess; primary file is the correct source. `task_type`/`priority` are DERIVED by the adapter (engine need not emit them).
2. **`deliverable` semantics** — consumer HONORS evidence-based close: a ticket is closed only when `deliverable` exists and is non-empty (matches the engine's existing deliverable-close workflow; T7's AC checklist records it). Fallback if absent at close: ticket FAILED with `deliverable-missing` error class (feeds TGD G1 signatures).
3. **`eats`** — v1 metadata (harmless), synthesized into context text; v2 pre-load.
4. **Escalation reporting** — **JSONL event file**: consumer appends one JSON object per escalation to `engine_run_<run_id>_escalations.jsonl` in the run directory, fields aligned to the RLM `observe` execution-record schema (§12.3): `{run_id, ticket_id, tier_from, tier_to, attempt, outcome, reason, ts}`. Events are already produced by T4/T8/oracle telemetry — the adapter writes the projection. (Chosen over API call: no coupling; over single event: consumers may need replay.)
5. **Witness tickets** — tolerated and dispatched: consumer treats `W###` as `task_type=test` tasks whose dependencies are the closure; the combined-tests run reuses the existing TEST stage.
6. **ID format** — full `EPIC-XXX-TXXX` accepted (see mapping table). No truncation, no mapping rule needed.
7. **Frozen artifact** — this file + `schemas/rlm-ticket-v1.schema.json`, mirrored in both repos. Adapter location: **ace-engine side** (`engine/adapters/rlm_tickets.py`), keeping the RLM core consumer-agnostic (D1). The RLM engine's Epic-7 contract tests validate emitted tickets against the schema; the consumer's adapter tests validate the projection against this mapping.

## Decision-log entries (D24–D26, both repos)

- **D24 adapter placement:** consumer-side adapter; RLM core never learns consumer formats (D1 upheld). Rejected: emitter-side adapter (violates D1), dual-write fields (two sources of truth).
- **D25 competence/task_type:** derive, never truncate — `metadata.competence` preserves the 9-value vocab; `task_type` remains a consumer-execution detail. Rejected: extending the closed vocab with consumer values (breaks validation-gate determinism).
- **D26 escalation channel:** JSONL event file per run, observe-schema aligned. Rejected: API call (couples engine availability to planner), DB (RLM v1 is DB-free by invariant).
