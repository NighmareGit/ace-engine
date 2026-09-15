# PRD: Engine Observability — Session Logs, Tracing, Autopsy (v1)

Source epic: `docs/features/observability/EPIC.md` (authoritative for
motivation, requirements traceability, module map). ADRs: 0002 (judge-steered
retry — session logs are telemetry, not pipeline logic), 0003 (workflow over
states — no State enum changes). Glossary: `CONTEXT.md` (run, task, attempt,
stage, atom, session_log, autopsy).

## Problem Statement

The ACE engine's telemetry is fragmented across 7 `engine.db` tables,
per-run JSONL sidecars, fire-and-forget events, and `run.log`. Reconstructing
why a task failed — what the LLM saw, what it returned, how long it took,
what the validator said — requires reading 6+ files across 2 repos and
manually correlating by `(run_id, task_id, attempt)`. The run-1 x-ray
(`.scratch/ralph-loop/run1/XRAY-AND-GRILL.md`) proved this autopsy is
possible but *slow* (hours) and *incomplete* (no session logs exist — atoms
store only hashes).

Specific gaps:

1. **No session logs.** `engine/trace.py:22-36` stores `input_hash` /
   `output_hash` (SHA-256) — deliberately content-free for deterministic
   replay. The actual prompts and responses are *gone* after the run.
2. **No payload capture.** `engine/generator.py:82-95` reads
   `reasoning_content`, `finish_reason`, `thinking_tokens` from the transport
   response but discards them after code extraction.
3. **No live event query.** `engine/events.py:6-21` is fire-and-forget;
   events cannot be queried after emission.
4. **No per-attempt telemetry.** Token/latency profiles per model per role
   are not recorded. N4 is computed post-hoc from run reports.
5. **No autopsy CLI.** `engine/forensics.py` bisects atom-hash determinism
   only; it cannot show session logs, validation traces, or re-plan history.

## Solution

Additive-only telemetry capture: a `session_logs` table in `engine.db` records
every LLM call (prompt hash + truncated prompt + full-prompt sidecar, response,
token counts, latency, finish reason, exhaustion flag). A `SessionLogRecorder`
mirrors `AtomRecorder`'s context-manager API and plugs into the generation
chokepoint (`engine/generator.py:83`) and re-plan brain. An `ace autopsy` CLI
reconstructs the full failure timeline from one command. An `engine_events`
table makes the fire-and-forget event bus queryable. The G4 context-bloat
signal upgrades from a code-blob proxy to real `prompt_tokens`.

## User Stories

1. As an operator, I want every LLM call recorded in `session_logs` with
   prompt hash, response, token counts, and latency, so that I can reconstruct
   what happened during a run without reading source code.
2. As an operator, I want `ace autopsy <run_id> [task_id]` to print a
   structured 7-section report (run header, task summary, session log
   timeline, validation trace, gate verdicts, re-plan history, divergence
   check), so that I can answer "why did task X fail" in under 5 minutes.
3. As an operator, I want full prompts available in a per-run JSONL sidecar
   for deterministic replay, so that I can re-run a failure with the exact
   same prompt.
4. As an operator, I want the event bus queryable via `engine_events`, so
   that I can tail a running run's events by `(run_id, task_id, event_type)`.
5. As an operator, I want per-model token/latency profiles in
   `generation_telemetry`, so that I can compare model behavior across runs.
6. As the TGD detector, I want G4 (context bloat) to read real
   `prompt_tokens` from `session_logs`, so that the signal has zero false
   positives from large generated files.
7. As a developer, I want `session_logs` linked to `atoms` via `atom_id`,
   so that I can join trace data with payload data in a single query.
8. As the owner, I want no PII or secrets in `engine.db` — prompts are
   truncated + hashed, full payloads live in the run-dir sidecar.
9. As the owner, I want session log writes to add < 50 ms per LLM call,
   so that telemetry never becomes the bottleneck.
10. As a test runner, I want a `test_session_log.py` that verifies every LLM
    call in a mock run produces a `session_logs` row, so that the capture
    path is regression-proof.
11. As a test runner, I want a `test_autopsy.py` that verifies `ace autopsy`
    returns all 7 sections for a known run, so that the CLI is regression-
    proof.
12. As the engine, I want `session_logs` to be additive — no changes to
    `atoms`, `atom_replay.py`, or the state machine — so that deterministic
    replay semantics are preserved.

## Implementation Decisions

### D1 — Session logs in engine.db (not per-run JSONL, not extending atoms)

Atoms (`engine/trace.py:22-36`) store only `input_hash`/`output_hash` —
deliberately content-free. Storing full payloads in atoms would break replay
semantics (`atom_replay.py:61-69` recomputes `output_hash` from
`{from_state, to_state, meta}`). A new `session_logs` table is the correct
home: queryable by `(run_id, task_id, attempt, stage)` with a single SELECT.
The run dir lives under the *workspace*, not the engine checkout — autopsies
run from the engine checkout against `engine.db` + a run-id.

### D2 — Truncation + sidecar pattern

Store `prompt_hash` (SHA-256 of full prompt) + `prompt_truncated` (first 4000
chars) in-row. Store the **full** response (`content`, `reasoning_content`,
`finish_reason`) in-row because responses are short (<=4096 toks ~ 16 KB). For
full-prompt replay, write the complete prompt to a per-run JSONL sidecar
(`engine_run_<id>_session_payloads.jsonl`) — same sidecar pattern as
`engine/state.py:210-217`. This keeps `engine.db` small (< 500 KB per 5M-token
run) while preserving replayability.

### D3 — SessionLogRecorder API

Mirror `AtomRecorder`'s context-manager API (`engine/trace.py:60-165`):

```python
with SessionLogRecorder(run_id, db_path="engine.db") as rec:
    rec.record(task_id, attempt, stage, atom_id, model, port,
               prompt_text, response, meta)
```

The recorder handles: prompt hashing, truncation, sidecar write, in-row
insert. Context manager ensures `close()` on exit.

### D4 — Plug into generate_code (single chokepoint)

`engine/generator.py:33-159` (`generate_code`) is the single chokepoint where
the prompt is built, BeeLlama is called, and the response is parsed. Record
the session log at line ~83 (after `response = transport.curl_beellama(...)`),
before code extraction. For re-plan calls, record in
`engine/orchestrator/replan_brain.py`.

### D5 — Event bus parallel write

`engine/events.py:6-21` (`emit_event`) stays fire-and-forget for callbacks.
Add a parallel write to `engine_events` table (additive). The write is
best-effort (wrapped in try/except) — observability must never break the
pipeline (same swallow-contract as the existing streaming bridge).

### D6 — G4 upgrade

`toolgap_detector.py:_extract_g4` (lines 261-311) currently uses large
generated code blobs as a proxy for context pressure. Once `session_logs`
captures `prompt_tokens`, rewrite `_extract_g4` to read real token counts.
The code-blob proxy is removed.

### D7 — Autopsy CLI

`engine/autopsy.py` — read-only over `engine.db` + run dir. Extends the
read-only philosophy of `engine/forensics.py`. CLI:

```
ace autopsy <run_id>           # full run summary + per-task timelines
ace autopsy <run_id> <task_id> # deep-dive: session logs + validation + replans
ace autopsy <run_id> <task_id> --full  # include full prompt/response from sidecar
```

Output sections (each with headers):
1. Run header (from `engine_runs`)
2. Task summary (from `engine_task_results`)
3. Session log timeline (from `session_logs`)
4. Validation stage trace (from `engine_task_results.validation_result`)
5. Gate verdicts (from `judge_verdicts` + `engine_scores`)
6. Re-plan history (from `orchestrator_replans` + escalation JSONL)
7. Divergence check (calls `forensics.bisect`)

### D8 — Privacy enforcement

- Prompts: SHA-256 hash + first 4000 chars in DB; full prompt in run-dir
  sidecar only.
- Responses: capped at 16 KB in DB (model output limit).
- No secret patterns (API keys, tokens) logged — the truncation + hashing
  makes accidental capture a hash, not plaintext.
- Sidecar payloads live in the run dir, archived with it.

## Schema

```sql
CREATE TABLE IF NOT EXISTS session_logs (
    session_id     TEXT PRIMARY KEY,            -- {run_id}-{task_id}-{attempt}-{stage}
    run_id         TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,            -- 1-based generation attempt
    stage          TEXT NOT NULL,               -- generate | replan | judge | test
    atom_id        TEXT REFERENCES atoms(atom_id),
    model          TEXT NOT NULL,               -- model config string
    port           INTEGER NOT NULL,            -- 8080 (35B) or 8082 (9B)
    prompt_hash    TEXT NOT NULL,               -- SHA-256 of full prompt
    prompt_truncated TEXT NOT NULL,             -- first 4000 chars, inline
    prompt_payload_path TEXT,                   -- NULL or path to sidecar JSONL with full prompt
    system_message TEXT,                        -- system prompt (if any)
    user_message   TEXT NOT NULL,               -- the compiled prompt (truncated inline)
    response_content TEXT NOT NULL,             -- model content (capped 16 KB)
    response_reasoning TEXT,                    -- reasoning_content / thinking
    finish_reason  TEXT NOT NULL,               -- length | stop | tool_calls
    prompt_tokens  INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens   INTEGER NOT NULL,
    thinking_tokens INTEGER DEFAULT 0,
    latency_ms     INTEGER NOT NULL,
    exhausted      INTEGER NOT NULL DEFAULT 0,  -- reasoning exhaustion flag
    error          TEXT,                        -- transport/parse error if any
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_logs_run_task ON session_logs(run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_session_logs_atom ON session_logs(atom_id);

CREATE TABLE IF NOT EXISTS generation_telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    stage           TEXT NOT NULL,
    model           TEXT NOT NULL,
    port            INTEGER NOT NULL,
    role            TEXT NOT NULL,              -- coder | orchestrator | judge
    prompt_tokens   INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    thinking_tokens INTEGER DEFAULT 0,
    total_tokens    INTEGER NOT NULL,
    tokens_per_sec  REAL DEFAULT 0.0,
    latency_ms      INTEGER NOT NULL,
    finish_reason   TEXT NOT NULL,
    exhausted       INTEGER NOT NULL DEFAULT 0,
    max_tokens_used INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_gen_tel_model ON generation_telemetry(model, role);

CREATE TABLE IF NOT EXISTS engine_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    task_id         TEXT,
    event_type      TEXT NOT NULL,
    data_json       TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_engine_events_run ON engine_events(run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_engine_events_type ON engine_events(event_type);
```

## Testing Decisions

- **Unit tests (`tests/test_session_log.py`):**
  - `SessionLogRecorder` writes a row → row retrievable by `get_logs(run_id)`.
  - Prompt hashing is deterministic (same prompt → same hash).
  - Truncation: prompt > 4000 chars → `prompt_truncated` = first 4000 chars.
  - Sidecar: full prompt written to `engine_run_<id>_session_payloads.jsonl`.
  - Context manager: `close()` on `__exit__`.
  - Link to atoms: `atom_id` foreign key resolves.

- **Integration tests (`tests/test_autopsy.py`):**
  - `ace autopsy <run_id>` returns all 7 sections.
  - `ace autopsy <run_id> <task_id>` returns deep-dive with session logs.
  - `ace autopsy <run_id> <task_id> --full` includes sidecar payloads.
  - Read-only: no DB writes, no task mutation.

- **Generator plug-in test (`tests/test_generator_telemetry.py`):**
  - Mock transport → `generate_code` produces a `session_logs` row.
  - `reasoning_content`, `finish_reason`, `thinking_tokens` captured.

- **Event bus test (`tests/test_engine_events.py`):**
  - `emit_event` → row appears in `engine_events` within 100 ms.
  - Callback path unchanged (fire-and-forget still works).

- **G4 upgrade test (`tests/test_toolgap_g4.py`):**
  - A run with `prompt_tokens >= 0.9 * model_cap` triggers G4 with real data.
  - No false positives from large generated code blobs.

- **Performance test (`tests/test_session_log_perf.py`):**
  - 1000 inserts in < 5 s (WAL mode, autocommit).
  - 100 concurrent inserts (distinct run_id) — zero SQLITE_BUSY.

- **Privacy test (`tests/test_session_log_privacy.py`):**
  - A prompt containing a fake secret pattern → `prompt_truncated` contains
    only the hash prefix, not the secret. Full prompt in sidecar only.

## Out of Scope

- **Real-time dashboard / GUI.** This PRD is capture + query. A future
  "observability dashboard" epic consumes these tables.
- **Log shipping / external SIEM.** Local `engine.db` + sidecar only.
- **Attempt-to-attempt diffing.** Session logs capture each attempt; diffing
  is a future analysis feature.
- **Changes to `atom_replay.py` semantics.** It stays hash-diff only.
- **Changes to the pipeline state machine.** Purely additive telemetry.
- **Model-response quality scoring.** The LLM Judge already scores outputs;
  this PRD captures telemetry, not judgment.
- **Cross-run correlation / fleet analytics.** Single-run focus. Fleet
  analytics is a future epic.
