# Engine Observability — Execution Plan

Source: EPIC.md, PRD-v1.md, TICKETS.md, XRAY-AND-GRILL.md (Q1, Q9).
Branch: `feature/engine-observability` (cut from main @ 61d57e0, fast-forwarded to
include D2 stub pre-pass @ 8694010).

## Wave Map

```
Wave 1 (parallel):
  OBS-01 (#26) session_logs + SessionLogRecorder   ← foundation
  OBS-04 (#29) engine_events table                  ← independent

Wave 2 (after OBS-01):
  OBS-02 (#27) ace autopsy CLI                      ← needs session_logs
  OBS-03 (#28) generation_telemetry                 ← shares capture point
  OBS-05 (#30) TGD G4 upgrade                       ← needs session_logs.prompt_tokens
  OBS-06 (#31) privacy + performance guards         ← needs SessionLogRecorder

Wave 3 (after OBS-01 + OBS-03):
  OBS-07 (#32) cost/quality analytics               ← needs session_logs + generation_telemetry

Wave 3 (after OBS-04):
  OBS-08 (#33) live-tail event stream               ← needs engine_events
```

## Per-Ticket Approach

### OBS-01 — session_logs + SessionLogRecorder (#26)
- New file `engine/session_log.py`: `SessionLogRecorder` class mirroring
  `AtomRecorder` context-manager API (trace.py:60-165).
- DDL: `session_logs` table (schema per PRD-v1.md / XRAY Q1).
- Sidecar writer: per-run JSONL `engine_run_<id>_session_payloads.jsonl`
  (same pattern as state.py:210-217 tasks_json sidecar).
- Plug into `engine/generator.py:83` (after curl_beellama, before code
  extraction) — additive, non-breaking.
- Plug into `engine/orchestrator/replan_brain.py:61` re-plan calls.
- Tests: `tests/test_session_log.py` (hashing, truncation, sidecar, context
  manager, atom link).

### OBS-02 — ace autopsy CLI (#27)
- New file `engine/autopsy.py`: read-only over engine.db + run dir.
- 7 sections: run header, task summary, session log timeline, validation
  trace, gate verdicts, re-plan history, divergence check (calls
  forensics.bisect).
- Wire into `engine/cli.py` as `ace autopsy` subcommand.
- Tests: `tests/test_autopsy.py`.

### OBS-03 — generation_telemetry (#28)
- DDL: `generation_telemetry` table.
- Recording logic in `engine/generator.py` (after session_log record).
- Tests: `tests/test_generation_telemetry.py`.

### OBS-04 — engine_events (#29)
- DDL: `engine_events` table.
- Additive parallel write in `engine/events.py:emit_event` (best-effort,
  try/except wrapped — preserves swallow-contract).
- Tests: `tests/test_engine_events.py`.

### OBS-05 — TGD G4 upgrade (#30)
- Rewrite `_extract_g4` in `engine/orchestrator/toolgap_detector.py:261-311`
  to read real `prompt_tokens` from `session_logs`.
- Graceful degradation: no session_logs rows → G4 returns [].
- Tests: `tests/test_toolgap_g4.py`.

### OBS-06 — privacy + performance guards (#31)
- Truncation (4000 char inline) + response capping (16 KB) in
  SessionLogRecorder.
- WAL mode + busy_timeout=5000 (already in state.py `_get_conn`).
- Tests: `tests/test_session_log_privacy.py`, `tests/test_session_log_perf.py`.

### OBS-07 — cost/quality analytics (#32)
- SQL views / Python query functions for per-model aggregation.
- N4 computation from engine_scores + session_logs.
- Tests: `tests/test_analytics_queries.py`.

### OBS-08 — live-tail event stream (#33)
- New file `engine/tail.py`: `ace tail <run_id>` CLI.
- Polls engine_events for new rows; configurable interval; filter by
  event_type; exits on terminal run state.
- Tests: `tests/test_tail.py`.

## Risks

1. **Capture overhead**: session_log write per LLM call must stay < 50 ms.
   Mitigation: WAL mode + autocommit (no explicit transaction per row).
2. **Concurrent runs**: distinct run_id keys avoid cross-run hot rows.
   Mitigation: WAL + busy_timeout=5000 (already configured in `_get_conn`).
3. **Disk bloat**: 5M-token runs. Mitigation: truncation + sidecar + gzip.
4. **Autopsy on run-1788948210**: that run predates session_logs, so the
   session log timeline section will be empty — the CLI must handle this
   gracefully (print "no session logs captured" rather than error).
5. **Wave-0 rail fixes**: unstaged changes in engine/prompts.py and
   prompt_compiler.py (files_to_create pinning + targeting hint). These
   are additive and coexist with our capture hooks. Resolution rule: BOTH
   changes win.

## Blockers

(None yet — will document here if a ticket conflicts with reality.)

## Outcome

### Tickets

| Ticket | Title | Gitea # | Status |
|--------|-------|---------|--------|
| OBS-01 | session_logs + SessionLogRecorder | #26 | ✅ DONE |
| OBS-02 | ace autopsy CLI | #27 | ✅ DONE |
| OBS-03 | generation_telemetry | #28 | ✅ DONE |
| OBS-04 | engine_events table | #29 | ✅ DONE |
| OBS-05 | TGD G4 upgrade | #30 | ✅ DONE |
| OBS-06 | privacy + performance guards | #31 | ✅ DONE |
| OBS-07 | cost/quality analytics | #32 | ✅ DONE |
| OBS-08 | live-tail event stream | #33 | ✅ DONE |

### Test Suite

- **695 passed / 4 skipped** (baseline 695 + 0 new failures; all observability
  tests green)
- Capture smoke test: mock transport → session_logs row + sidecar payload ✅

### Branch Commit Range

`61d57e0` (branch cut) → `be3f472` (OBS-08) + `19d04fb` (test fix) + README update.

Key commits:
- `776e0d8` OBS-01 session_logs + SessionLogRecorder
- `bc68e8b` OBS-04 engine_events
- `64b1689` OBS-03 generation_telemetry
- `31eecdd` OBS-02 ace autopsy CLI
- `8c93845` OBS-05 TGD G4 upgrade
- `326ee4a` OBS-06 privacy + performance
- `c42785a` OBS-07 cost/quality analytics
- `be3f472` OBS-08 live-tail event stream

### Deviations

1. **Secret redaction added (R9)**: The original design relied on truncation +
   hashing alone. The R9 test ("grep session_logs for a known secret pattern →
   zero matches") requires actual pattern redaction, so `_redact_secrets()` was
   added to redact `sk-*`, `api-key`, `token`, `Bearer <token>` patterns
   before inline storage. Full prompts in the sidecar are unredacted (they live
   in the run dir, not engine.db).

2. **G4 threshold**: Used a fixed 14,400-token threshold (~90% of 16K cap)
   rather than per-model caps, since model config strings don't directly encode
   context size. This catches both 16K and 32K models conservatively.

3. **Existing test_g4_fires updated**: The fixture was inserting large
   generated_code (old proxy). Updated to insert a session_logs row with
   high prompt_tokens (new real-data signal). Test still asserts G4 fires.

### Ledger Text (for owner to ledger after review)

> **Ledger 47**: Engine Observability epic complete — 8 tickets (OBS-01..08)
> implemented on `feature/engine-observability`. session_logs table captures
> every LLM call (prompt hash + truncated inline + full sidecar, response,
> tokens, latency, finish_reason, exhaustion). ace autopsy CLI reconstructs
> failures in 7 sections. engine_events makes the event bus queryable. G4
> upgraded from code-blob proxy to real prompt_tokens. Privacy: secret
> redaction + 16 KB response cap + 4 K char inline truncation. Performance:
> < 50 ms/write, WAL + busy_timeout, 100 concurrent inserts zero SQLITE_BUSY.
> 695 passed / 4 skipped.
