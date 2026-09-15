# ARCHITECTURE.md: Engine Unification

## 0. Mission Anchor

> Master reference for the Coder Harness Benchmark Platform: a self-scoring, crash-resilient system for evaluating local LLM models on a dual-GPU Triton machine, with an autonomous coding engine (ACE) that executes PRDs end-to-end.
> — *AGENTS.md, line 3 (project mission)*

> **The Autonomous Coding Engine (ACE)** parses PRDs into tasks, generates code via BeeLlama inference, runs quality gates, tests, and commits to Gitea — all end-to-end with zero human intervention.
> — *AGENTS.md, line 9 (ACE definition)*

The full rationale for these clauses lives in **AGENTS.md §16 "Autonomous Coding Engine (ACE)"**.

### Goal-Trace Table

| # | Mission Clause | Design Section | Acceptance Gate |
|---|----------------|----------------|-----------------|
| 1 | **PRD → task decomposition** | §5 Execution Pipeline phase 1 PARSE; §6 Module Map — context.py, generator.py | AC1.4 — Dry-run produces task plan (PRE_PRD.md) |
| 2 | **BeeLlama code generation** | §5 Execution Pipeline phase 2 GENERATE; §6 Module Map — generator.py | AC1.6 — Generator calls BeeLlama via transport (PRE_PRD.md) |
| 3 | **Quality gates (validation)** | §5 Execution Pipeline phase 3 VALIDATE; §8 NFR8 — Validator coverage 4 stages | AC1.7 — Validator catches all 4 error types (PRE_PRD.md) |
| 4 | **Self-repair / test loop** | §5 Execution Pipeline phase 4 TEST + Retry; §8 NFR5 — Retry intelligence | AC1.8 — Test runner executes pytest (PRE_PRD.md) |
| 5 | **Gitea commit** | §5 Execution Pipeline phase 5 COMMIT; §6 Module Map — committer.py | AC1.9 — Committer creates branch and commits (PRE_PRD.md) |
| 6 | **Zero human intervention** | §1 Overview — "one unified `engine/` package" consolidation; §5 full pipeline sequence; §3 principle 5 — one CLI, one engine, one entry point | AC1.10 — Engine.run() executes full pipeline (PRE_PRD.md) |
| 7 | **Crash-resilient checkpoint / resume** | §7 Integration Seams — SQLite tables (state.py); §8 NFR3 — Checkpoint; §11 C3–C5, C9 | AC2.1 — Checkpoint/resume works; AC2.3 — SIGINT graceful shutdown |
| 8 | **Self-scoring via judge** | *Gap* — judge.py is bench-side only; no ACE pipeline step covers this | *Gap* — No AC for engine-level self-scoring (see EPICS.md §Epic 5 stub) |
| 9 | **Benchmark drivability for CCBS** | *Gap* — No design artifact integrates CCBS with the engine | *Gap* — No AC for CCBS benchmark drivability (see EPICS.md §Epic 5 stub) |
| 10 | **Separate SQLite DBs (Rule 6)** | §9 Infrastructure — "two SEPARATE databases"; §11 C12 | AC4.3 — Benchmark platform independent (PRE_PRD.md) |

> **Known coverage gaps (rows 8–9):** Self-scoring and CCBS integration are scoped as Phase 2 work. See EPICS.md "Epic 5 (Phase 2 stub)" for the planned acceptance criteria.

## 1. Overview

The Engine Unification consolidates 6 overlapping CLI entry points and 4 pipeline modules into one unified `engine/` Python package with a 12-state state machine, one CLI entry point (`cli.py`), and a verified end-to-end path from PRD to Gitea commit. The benchmark platform moves to `bench/`. Superseded modules move to `legacy/`.

## 2. C4 Container Diagram

Reference the Mermaid diagram at design_drafts/c4-container.mmd. The key containers are:
- **Developer Machine (nightmare):** cli.py (unified CLI)
- **Engine Package:** engine/ (10 modules: __init__, engine, pipeline, context, generator, validator, tester, committer, state, events)
- **Retained Infrastructure:** transport.py, prompt_compiler.py, streaming_client.py
- **Triton (<LAN_IP>):** BeeLlama 3090 (:8080), BeeLlama 3070 (:8082), Gitea (:3000), Projects (/home/<user>/projects/)
- **Observability:** streaming_server.py (:3081 SSE), Dashboard (browser)

## 3. Architecture Principles

1. **Plain functions over classes** — Only engine.py (Facade) and pipeline.py (State Machine) use classes with methods. All other modules use plain functions + dataclasses.
2. **Transport is a dependency** — Engine imports transport.get_transport() at init time. Never imports ssh_utils or engine_client directly.
3. **Streaming is opt-in** — Optional on_event callback. Engine works without streaming.
4. **Legacy, not delete** — Superseded modules move to legacy/ with deprecation warnings. Importable for 2 weeks.
5. **One CLI, one engine, one entry point** — cli.py is the only documented CLI. engine/ is the only pipeline.

## 4. State Machine (12 States)

Reference the Mermaid state diagram at design_drafts/state-machine-engine.mmd.

States: IDLE → PARSING → QUEUED → CONTEXT → GENERATE → VALIDATE → TEST → COMMIT → NEXT → DONE (+ FAILED, CANCELLED)

Transition rules:
- IDLE → [PARSING, CANCELLED]
- PARSING → [QUEUED, FAILED, CANCELLED]
- QUEUED → [CONTEXT, DONE, CANCELLED]
- CONTEXT → [GENERATE, CANCELLED]
- GENERATE → [VALIDATE, FAILED, CANCELLED]
- VALIDATE → [TEST, GENERATE, FAILED, CANCELLED] (GENERATE = retry, max 3)
- TEST → [COMMIT, GENERATE, FAILED, CANCELLED] (GENERATE = fix, max 2)
- COMMIT → [NEXT, QUEUED, CANCELLED] (QUEUED = commit failed, skip)
- NEXT → [QUEUED, DONE, CANCELLED]
- DONE → [] (terminal)
- FAILED → [] (terminal)
- CANCELLED → [] (terminal)

Every transition: validates legality, updates state, logs with timestamp, checkpoints to SQLite, calls on_event callback.

## 5. Execution Pipeline

Reference the Mermaid sequence diagram at design_drafts/sequence-prd-to-commit.mmd.

The pipeline executes 5 phases per task:
1. PARSE: PRD → task list (done once at start)
2. GENERATE: context → prompt → BeeLlama → code extraction
3. VALIDATE: AST → syntax → imports → execution (4 stages in sequence)
4. TEST: pytest via transport → parse results
5. COMMIT: branch → write files → commit → push

Retry: GENERATE max 3 (each adds error context), TEST max 2 fixes, COMMIT max 2.

## 6. Module Map

| Module | Pattern | Lines (est.) | Responsibility |
|--------|---------|-------------|----------------|
| engine/__init__.py | Dataclass | ~30 | EngineConfig, constants, version |
| engine/engine.py | Facade | ~250 | Public API: run(), step(), status(), cancel() |
| engine/pipeline.py | State Machine | ~300 | 12-state machine, transitions, checkpoint, resume |
| engine/context.py | Plain function | ~120 | build_context() — file tree, imports, framework |
| engine/generator.py | Plain function | ~180 | generate_code() — prompt → BeeLlama → code |
| engine/validator.py | Plain functions | ~120 | validate() + 4 check_*() functions |
| engine/tester.py | Plain function | ~80 | run_tests() — pytest via transport |
| engine/committer.py | Plain function | ~100 | commit_code() — branch, write, commit, push |
| engine/state.py | Plain functions | ~80 | SQLite: init_db, checkpoint_run, load_run, list_runs |
| engine/events.py | Plain function | ~20 | emit_event() — optional callback |
| engine/trace.py *(Phase 2)* | Plain functions | ~100 | AtomRecorder — records every transition as a deterministic atom to engine.db (Rule 6); consumed by replayer and forensics |
| **Total** | | **~1,380** | |

### Phase 2 Module: `engine/trace.py` (AtomRecorder)

> **Determinism contract:** Given identical `(PRD, task, atom index, recorded input)` and `TRANSPORT_MODE=mock`, an atom replayer must produce byte-identical `output_hash`. LLM nondeterminism is frozen at record time: the recorded BeeLlama response is replayed from the transcript, never re-generated. See `BACKTEST-DESIGN.md` §2 for the full contract and `TICKET-STATE-MACHINE.md` §5 for the atom schema shared with campaign-side replay.

## 7. Integration Seams

| Seam | Provider | Consumer(s) | Contract |
|------|----------|-------------|----------|
| transport.get_transport() | transport.py | context, generator, tester, committer | Auto-detect HTTP/Local/SSH, cache at init |
| prompt_compiler.PromptCompiler | prompt_compiler.py | generator | Manifest + template → prompt string |
| on_event callback | events.py | engine, pipeline | Callable(event_type, data) or None |
| SQLite tables | state.py | pipeline, engine | engine_runs + engine_task_results |

## 8. NFR Table

| # | NFR | SLO | Verification Gate |
|---|-----|-----|-------------------|
| NFR1 | E2E completion | <5 min trivial PRD | Integration test: total_time_s < 300 |
| NFR2 | CLI size | <600 lines | wc -l cli.py |
| NFR3 | Checkpoint | Fast (guideline) | No measured SLO |
| NFR4 | State machine | All 12 reachable | Unit test enumerates transitions |
| NFR5 | Retry intelligence | Error context escalation | Unit test verifies ErrorContext |
| NFR6 | Streaming opt-in | Works without streaming | Unit test: on_event=None |
| NFR7 | Transport detection | Single detection | Unit test: transport called once |
| NFR8 | Validator coverage | 4 stages | Unit test per stage |
| NFR9 | Legacy compat | Importable 2 weeks | Import test + warnings.warn |
| NFR10 | Test pyramid | 15-20 unit + 2-3 int + 1 E2E | pytest count |
| NFR11 | Dry-run | Task plan preview | CLI test --dry-run |
| NFR12 | Signal handling | SIGINT graceful | Integration test SIGINT |

## 9. Infrastructure

- **Runtime:** Python 3.12 on Triton (<LAN_IP>)
- **Inference:** BeeLlama 3090 (:8080) + 3070 (:8082)
- **Git:** Gitea (:3000) with token auth
- **Transport:** HTTP (engine_service :3082), Local (subprocess), SSH (nightmare → Triton)
- **Database:** SQLite WAL mode — two SEPARATE databases per handoff Rule 6: `benchmark-results.db` (benchmarks, untouched) + `engine.db` (engine_runs, engine_task_results)
- **Streaming:** Optional SSE server (:3081)

## 10. Design Decisions Log

| # | Decision | Rationale | Rejected Alternatives | Iteration |
|---|----------|-----------|----------------------|-----------|
| D1 | Hybrid Minimal State Machine with Facade | Preserves 12-state observability, simplifies execution | Event-Sourced, Plugin, Saga, Minimal 6-state | 0 |
| D2 | 2 patterns (State Machine + Facade) | Pragmatic review: 8 patterns was over-engineering | Strategy, Chain of Responsibility, Builder, Repository, Observer, Adapter | 0 |
| D3 | Plain functions for context/generator/validator/tester/committer/state/events | Single-developer tool, no variation to justify classes | Named GoF patterns for each module | 0 |
| D4 | Optional on_event callback | One consumer (streaming), no need for Observer registry | Observer pattern with subscribe/unsubscribe | 0 |
| D5 | Simple sqlite3 functions | 2 tables, single writer, no complex queries | Repository pattern with CRUD abstraction | 0 |

## 11. Chaos/Threat Mitigations

| # | Vulnerability | Severity | Mitigation | Design Decision |
|---|---------------|----------|------------|-----------------|
| C1 | COMMIT→QUEUED infinite loop | Critical | Per-task total attempt cap (MAX_TOTAL_ATTEMPTS_PER_TASK = 5) | Added to Pipeline |
| C2 | Transport cached with no fallback | Critical | Transport health re-check on first failure, fallback HTTP→Local→SSH | Added to Engine |
| C3 | SQLite WAL corruption on power loss | High | WAL checkpoint after each write, backup DB before writes, integrity check on open | Added to state.py |
| C4 | Signal handler races with checkpoint | High | threading.Lock around checkpoint writes, signal handler sets flag only | Added to Pipeline |
| C5 | Retry counters not persisted | High | Retry counts stored in Pipeline.task_retries, persisted via checkpoint_run task_retries_json | Added to Pipeline + state.py |
| C6 | No per-step timeouts | High | _with_timeout wrapper, enforced per state (300s inference, 180s test, 60s commit) | Added to Pipeline |
| C7 | Docker sandbox leak on crash | Medium | Track container IDs in checkpoint, cleanup on resume | Added to committer.py |
| C8 | Partial git state on COMMIT→QUEUED | Medium | git reset --hard before QUEUED transition | Added to committer.py |
| C9 | tasks_json truncation on crash | Medium | Backup tasks_json to file, validate JSON on resume | Added to state.py |
| C10 | QUEUED→DONE premature transition | Medium | Guard: only valid when all tasks completed/failed. Enforced in pipeline.transition() | Added to Pipeline |
| C11 | Error context exceeds context window | Medium | Truncate error context to 50% of model context window | Added to generator.py |
| C12 | SQLite single-writer contention | Medium | PRAGMA busy_timeout=5000; engine state in separate engine.db (Rule 6) — no telemetry contention | Added to state.py |
| C13 | SSH zombie processes | Low | subprocess.Popen with cleanup, atexit handler | Added to transport.py |
| C14 | HTTPTransport retry masks failures | Low | Distinguish retryable vs permanent errors | Added to engine_client.py |
| C15 | CANCELLED is terminal but work partial | Low | Add CANCELLED→IDLE or restart command | Added to CLI |

### Validation-Round Mitigations (V = red-team/simulation, see VALIDATION_REPORT.md)

| # | Vulnerability | Severity | Mitigation |
|---|---------------|----------|------------|
| V1 | exec() of LLM code with full builtins — arbitrary code execution | Critical | Restricted namespace (`__builtins__` = {}), 5s timeout; Docker sandbox opt-in |
| V2 | Path traversal via task.module from PRD content | Critical | Sanitize modules at parse time (strip `..`, leading `/`); guard in file extraction |
| V3 | HTTPTransport sends no auth token → 401 on every call (real codebase bug) | Critical | Phase 1 prerequisite: transport.py must send `Authorization: Token {ENGINE_TOKEN}` |
| B2 | Generated code never written to disk before pytest — TEST tested nothing | Critical | New pipeline step: stage files after VALIDATE, before TEST; rollback on failure |
| B3 | Project directory not bootstrapped | High | `bootstrap_project()` — mkdir + git init, idempotent |
| B4 | Git push has no remote/auth setup | High | Bootstrap wires Gitea token remote |
| B5 | Resume loses task index and current_task | High | Checkpoint serializes full Task objects + index |
| V4 | CWD-relative DB path → silent second database | High | Absolute path anchored to repo root |

**Scope triage (conformance audit):** C3 (DB backup/restore), C4 (thread-safe checkpointing), C9 (sidecar backups) move from Phase 1 to Phase 3 (Hardening) to keep Phase 1 near the handoff's 4–6h estimate.
