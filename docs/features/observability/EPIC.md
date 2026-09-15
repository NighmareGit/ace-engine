# EPIC: Engine Observability — Session Logs, Tracing, Autopsy

Source: run-1 x-ray (`.scratch/ralph-loop/run1/XRAY-AND-GRILL.md` Q1, Q9) +
run-1 autopsy (`RUN-REPORT.md`), capability inventory (Phase A), multi-frame
ideation (Phase B). Supersedes no prior epic — this is a new vertical.

## Motivation

The ACE engine has *fragmented* telemetry. After run-1788948210 (S8 dogfood,
N4=10%) answering "why did T02 fail" required reading **6 files across 2
repos** — `engine.db` (7 tables), `run.log`, escalation JSONL, run report,
workspace, and the engine source. The x-ray proved the autopsy was possible
but *slow* (hours) because no single query reconstructs the LLM's experience.

The blind spots are structural:

1. **No session logs.** `engine/trace.py` stores only `input_hash`/`output_hash`
   (SHA-256 — deliberately content-free for replay). The actual prompts sent
   to the model and the responses received are *gone* after the run. Atoms
   answer "what state transition happened"; they cannot answer "what did the
   model see and say" (`XRAY Q1`).
2. **No prompt/response payload capture.** `engine/generator.py:82-95` reads
   `reasoning_content`, `finish_reason`, `prompt_tokens`, `completion_tokens`,
   `thinking_tokens` from the transport response — but *discards* them after
   code extraction. Nothing is persisted.
3. **No live-tail.** `engine/events.py` is fire-and-forget; events are not
   queryable after emission. Observing a running run means tailing `run.log`.
4. **No cost/quality aggregation.** Budget counters exist
   (`engine/orchestrator/session.py:145-178`) but per-task/per-attempt
   token+latency breakdowns are not recorded. N4 is computed post-hoc by
   reading run reports, not from a queryable table.
5. **No autopsy CLI.** `engine/forensics.py` bisects atom-hash determinism;
   it cannot show session logs, validation traces, or re-plan history
   together. The x-ray's Q9 design (`ace autopsy`) is unimplemented.

## Hard Requirements (testable)

- **R1.** A `session_logs` table in `engine.db` records every LLM call:
   prompt_hash, prompt_truncated (first 4000 chars), prompt_payload_path
   (sidecar for full prompt), response_content, response_reasoning,
   finish_reason, prompt_tokens, completion_tokens, total_tokens,
   thinking_tokens, latency_ms, exhausted flag, model, port, stage
   (generate|replan|judge|test), attempt. Link to `atoms.atom_id`.
   Test: insert a session log row; query by (run_id, task_id, attempt, stage)
   → exact row returned.
- **R2.** `engine/session_log.py` — an `SessionLogRecorder` mirroring
   `AtomRecorder`'s context-manager API. Plugged into
   `engine/generator.py:83` (after `curl_beellama`, before code extraction)
   and `engine/orchestrator/replan_brain.py` re-plan calls. Additive: does
   not modify atoms table. Test: mock transport → recorder writes a row →
   row retrievable.
- **R3.** Per-run JSONL sidecar (`engine_run_<id>_session_payloads.jsonl`)
   stores full prompts for replay. Truncation policy: in-row truncated
   (4000 chars) + sidecar full payload. Same sidecar pattern as
   `engine/state.py:210-217` (tasks_json sidecar). Test: 5M-token run
   produces sidecar < 2 MB compressed.
- **R4.** `ace autopsy <run_id> [task_id]` CLI — read-only over `engine.db`
   + run dir. Sections: run header, task summary, session log timeline,
   validation stage trace, gate verdicts, re-plan history, divergence check
   (calls `forensics.bisect`). Test: `ace autopsy run-1788948210 T02`
   returns structured output with all 7 sections in < 5 s.
- **R5.** A `generation_telemetry` table records per-attempt token/latency
   profiles: model, port, role, prompt_tokens, completion_tokens,
   thinking_tokens, tokens_per_sec, latency_ms, finish_reason, exhausted.
   Feeds cost/quality analytics. Test: aggregate query returns per-model
   median latency and token distribution.
- **R6.** Event bus persistence — `engine/events.py` events are written to
   an `engine_events` table (additive) with (run_id, task_id, event_type,
   data_json, created_at). The fire-and-forget callback path is unchanged;
   this is a parallel write. Test: emit an event → row appears in
   `engine_events` within 100 ms.
- **R7.** TGD signal G4 (context bloat) upgraded from proxy (large code
   blobs) to real `prompt_tokens` once `session_logs` captures them.
   `toolgap_detector.py:_extract_g4` reads `session_logs.prompt_tokens`
   directly. Test: a run with prompt_tokens >= 90% of model cap triggers
   G4 with real data, not the code-blob proxy.
- **R8.** `session_logs` linked to `atoms` via `atom_id` foreign key.
   Replay (`atom_replay.py`) is unchanged — it stays a hash-diff tool.
   Session logs are orthogonal (payload capture). Test: joining
   `atoms NATURAL JOIN session_logs` returns the prompt/response for each
   transition.
- **R9.** Privacy: no full secrets/PII in `engine.db`. Truncation +
   hashing for prompts; responses capped at 16 KB (model output limit).
   Sidecar payloads live in the run dir (archived with it, not in DB).
   Test: grep session_logs for a known secret pattern → zero matches.
- **R10.** Performance: session log write adds < 50 ms per LLM call
   (async commit or WAL autocommit). Concurrent runs use distinct
   `run_id` keys — no cross-run lock contention. Test: 100 concurrent
   inserts (distinct run_id) complete without SQLITE_BUSY.

## Module Map

Prefer extending existing modules over new ones:

| Module | Role | Change |
|--------|------|--------|
| `engine/session_log.py` | **NEW** — SessionLogRecorder + DDL + sidecar writer | New file |
| `engine/trace.py` | Atoms table (unchanged) | No change |
| `engine/atom_replay.py` | Hash-diff replay (unchanged) | No change |
| `engine/forensics.py` | Bisect (unchanged); `autopsy.py` extends read-only philosophy | No change |
| `engine/autopsy.py` | **NEW** — `ace autopsy` CLI | New file |
| `engine/generator.py` | Generation chokepoint — plug in SessionLogRecorder at line ~83 | Additive |
| `engine/events.py` | Event bus — add parallel DB write (additive) | Additive |
| `engine/orchestrator/session.py` | Budget counters (already exists) | No change |
| `engine/orchestrator/toolgap_detector.py` | G4 upgrade to real prompt_tokens | Edit _extract_g4 |
| `engine/state.py` | DB plumbing (WAL, busy_timeout) — reuse `_get_conn()` | No change |
| `engine/llm_judge.py` | Judge scoring (already persists to engine_scores) | No change |
| `engine/orchestrator/report_builder.py` | Run report assembly (already exists) | No change |
| `engine/adapters/escalation_jsonl.py` | Escalation projection (already exists) | No change |

## Deliverables

1. `engine/session_log.py` — SessionLogRecorder + DDL + sidecar writer
2. `engine/autopsy.py` — `ace autopsy` CLI
3. `engine/events.py` — additive `engine_events` table write
4. `engine/generator.py` — plug SessionLogRecorder into generate_code
5. `engine/orchestrator/replan_brain.py` — plug SessionLogRecorder into re-plan
6. `engine/orchestrator/toolgap_detector.py` — G4 upgrade
7. `tests/test_session_log.py` — unit + integration tests
8. `tests/test_autopsy.py` — CLI tests
9. `docs/features/observability/PRD-v1.md` — this spec
10. `docs/features/observability/TICKETS.md` — ticket slice map

## Metrics

- **Autopsy latency:** `ace autopsy <run_id>` returns full output in < 5 s
  (target: < 1 s for < 1000 session log rows).
- **Disk overhead:** < 500 KB per 5M-token run in engine.db; < 2 MB
  compressed sidecar.
- **Write latency:** < 50 ms added per LLM call.
- **Coverage:** 100% of LLM calls (generate, replan, judge, test) recorded
  in session_logs.
- **G4 precision:** context-bloat signal upgrades from code-blob proxy to
  real prompt_tokens (eliminates false positives from large generated files).

## Non-Goals

- **No real-time dashboard.** This epic is about capture + query, not a GUI.
  A future "observability dashboard" epic can consume these tables.
- **No log shipping / external SIEM.** Local engine.db + sidecar only.
- **No model-response diffing between attempts.** Session logs capture each
  attempt; diffing is a future analysis feature.
- **No changes to atom_replay.py semantics.** It stays hash-diff only.
- **No PII/secrets in engine.db.** Truncation + sidecar pattern enforces this.
- **No changes to the pipeline state machine.** Purely additive telemetry.

## Traceability

| Phase A Gap | Requirement | Ticket |
|-------------|-------------|--------|
| No session logs (atoms are hash-only) | R1, R2, R3 | OBS-01 |
| No autopsy CLI | R4 | OBS-02 |
| No per-attempt token/latency profile | R5 | OBS-03 |
| Fire-and-forget events not queryable | R6 | OBS-04 |
| G4 context-bloat is code-blob proxy | R7 | OBS-05 |
| No atom↔session linkage | R8 | OBS-06 (folded into OBS-01) |
| Privacy/secrets in logs | R9 | OBS-07 |
| Performance under concurrent runs | R10 | OBS-08 |
| No live-tail for running runs | R6, R4 | OBS-09 |
| No cost/quality aggregation | R5 | OBS-10 |

## Phase B Ideation Summary

### F1 — Autopsy frame
**Current state:** 6+ files across 2 repos; hours to answer "why did T02 fail".
**Gaps:** No session logs, no single-query reconstruction, no CLI.
**Feature:** `session_logs` table + `ace autopsy` CLI → 5-minute answer.

### F2 — Live-operations frame
**Current state:** `run.log` tailing; events fire-and-forget (no query).
**Gaps:** No structured event stream; no per-task live status query.
**Feature:** `engine_events` table (additive parallel write) → query events
by (run_id, task_id, event_type).

### F3 — Model-behavior frame
**Current state:** `transport.py` captures `reasoning_content`, `finish_reason`,
`thinking_tokens` but discards after code extraction. `generator.py` reads
them at lines 88-95 but only keeps aggregated fields in `GeneratedCode`.
**Gaps:** No per-model latency/token profile; no reasoning_content archive.
**Feature:** `generation_telemetry` table → per-model median latency, token
distribution, exhaustion rate.

### F4 — Replay/regression frame
**Current state:** `atom_replay.py` checks hash determinism of state
transitions. No payload replay (prompts are hashed, not stored).
**Gaps:** Cannot replay a failure with the exact same prompt.
**Feature:** `prompt_hash` → sidecar payload linkage (R3). Full prompt
available for replay without breaking atom hash semantics.

### F5 — Cost/quality analytics frame
**Current state:** Budget counters exist (`session.py:145-178`) but no
per-attempt breakdown. N4 computed post-hoc from run reports.
**Gaps:** No queryable cost/quality dashboard; no per-model comparison.
**Feature:** `generation_telemetry` + `session_logs` → aggregate queries
for token/latency/pass-rate by model/role/stage.

### F6 — Blind-spot red-team
**Attacks:**
- *Disk bloat from 5M-token runs:* mitigated by truncation (4000 char
  inline) + sidecar + gzip. Responses capped at 16 KB.
- *PII/secrets in logs:* mitigated by hashing + truncation. Full prompts
  in run-dir sidecar (archived, not in DB).
- *WAL write-amplification:* mitigated by WAL mode + autocommit (no
  explicit transaction per row). Checkpoint after run completes.
- *Concurrent run lock contention:* mitigated by distinct run_id keys
  (no cross-run hot rows). WAL + busy_timeout=5000 handles contention.
- *Performance overhead:* < 50 ms per LLM call (async commit).
