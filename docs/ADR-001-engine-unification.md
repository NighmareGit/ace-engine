# ADR-001: Engine Unification Architecture

> **Date:** 2026-09-04
> **Status:** Accepted
> **Deciders:** MiMo (metacognitive friction applied)

---

## Context

The coder-harness codebase has grown to 48,455 lines across 52 Python files. The original architecture was a benchmark platform for evaluating local LLM inference quality. Over time, an autonomous coding engine (ACE) was built alongside the benchmark platform, sharing modules, CLIs, and SQLite databases.

The result is 6 overlapping CLI entry points, 4 pipeline implementations, 2 telemetry systems, and no single path from PRD to working code. The engine has never generated, validated, tested, or committed real code.

---

## Decision

**Consolidate the 6 overlapping pipeline modules into one unified `engine/` package with a 12-state state machine, one CLI entry point, and a verified E2E path. Move the benchmark platform to `bench/`. Move superseded modules to `legacy/`.**

---

## Architecture

### What Stays (15 top-level files)

| File | Role | Why |
|------|------|-----|
| `cli.py` | Unified CLI | Replaces 6 CLIs with 1 entry point |
| `transport.py` | Three-transport abstraction | Proven, clean, battle-tested |
| `ssh_utils.py` | SSH transport | Used by transport.py |
| `prompt_compiler.py` | Manifest-driven prompts | Tested, well-designed |
| `engine_service.py` | HTTP API (port 3082) | Already deployed on Triton |
| `engine_client.py` | HTTP client | Used by transport.py |
| `streaming_server.py` | SSE server | Real, tested |
| `streaming_core.py` | Ring buffer + SSE | Real, tested |
| `streaming_routes.py` | FastAPI routes | Real, tested |
| `streaming_client.py` | Event emission | Real, tested |
| `event_schema.py` | SQLite WAL schema | Real, tested |
| `dsh_adapter.py` | DSH bridge | Real, tested |
| `gpu_telemetry.py` | GPU poller | Real, tested |
| `generate_readme.py` | Utility | Keep |
| `validate_e2e.py` | E2E validation | Keep |

### What Moves to `engine/` (10 new modules)

| Module | Replaces | Lines (est.) |
|--------|----------|-------------|
| `engine/__init__.py` | — | ~50 |
| `engine/engine.py` | `demo_e2e.py` + `work_engine.py` core | ~400 |
| `engine/pipeline.py` | `task_queue.py` + `workflow-state-machine.py` | ~350 |
| `engine/context.py` | `context_manager.py` | ~200 |
| `engine/generator.py` | `code_generator.py` core | ~300 |
| `engine/validator.py` | `quality_gates.py` | ~250 |
| `engine/tester.py` | `test_runner.py` core | ~200 |
| `engine/committer.py` | `git_workflow.py` + `gitea_utils.py` | ~250 |
| `engine/state.py` | `checkpoint.py` | ~150 |
| `engine/events.py` | — | ~80 |
| **Total** | | **~2,230** |

### What Moves to `bench/` (9 modules)

`runner.py`, `judge.py`, `pilot.py`, `report.py`, `gpu_fit.py`, `orchestrate.py`, `preflight.py`, `watchdog.py`, `checkpoint.py`

### What Moves to `legacy/` (25 modules)

All superseded modules. Importable but not used by the engine or CLI.

---

## State Machine

The pipeline uses 12 states with validated transitions:

```
IDLE → PARSING → QUEUED → CONTEXT → GENERATE → VALIDATE → TEST → COMMIT → NEXT → DONE
                                   ↑              ↑         ↑
                                   └── retry ─────┘    fix ─┘
                                                      ↓
                                                   FAILED
```

Every transition:
1. Validates the transition is legal
2. Updates `self.state`
3. Logs the transition with timestamp
4. Persists to SQLite (checkpoint)
5. Emits an event to the streaming server

---

## Consequences

### Positive
- One CLI, one pipeline, one entry point — no confusion
- 12-state machine enables checkpoint/resume
- E2E test proves the engine works
- 15 top-level files (down from 52) — navigable
- Legacy modules available for reference during transition

### Negative
- 25 files moved to legacy/ — temporary clutter
- Some duplication between engine/ and legacy/ during transition
- Old CLI users need to learn new commands

### Risks
- Legacy modules might have subtle state that new engine doesn't replicate → mitigated by keeping legacy/ importable
- Pipeline state machine might have edge cases → mitigated by comprehensive unit tests
- E2E test depends on BeeLlama being available → mitigated by skip decorator

---

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Keep all modules, add a facade | Doesn't solve the redundancy — still 4 pipelines under the hood |
| Delete old modules immediately | Too risky — old modules have edge cases we might need |
| Plugin-based CLI | Overengineered for 52 files — monolithic is simpler |
| Merge all SQLite databases | Unnecessary complexity — separate DBs with cross-references is fine |
| Make streaming a hard dependency | Reduces standalone capability — opt-in is better |
