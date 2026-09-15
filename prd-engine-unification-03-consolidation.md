# PRD: Module Consolidation and Legacy Migration

## Problem Statement

The coder-harness codebase contains 25 modules that are superseded by the new `engine/` package but remain in the top-level directory, creating import confusion, namespace collisions, and maintenance debt. These modules include:

- 4 pipeline implementations: `work_engine.py` (1,440 lines), `work_engine_simple.py` (731 lines), `demo_e2e.py` (709 lines), `task_queue.py` (1,585 lines)
- 2 CLIs: `harness.py` (1,323 lines), `enginectl.py` (1,046 lines)
- 6 supporting modules being absorbed by `engine/`: `context_manager.py` (505 lines), `code_generator.py` (1,620 lines), `quality_gates.py` (607 lines), `test_runner.py` (887 lines), `git_workflow.py` (414 lines), `file_ops.py` (693 lines)
- 3 remote control modules: `remote_control.py` (1,088 lines), `sandbox_manager.py` (588 lines), `gitea_utils.py` (663 lines)
- 6 telemetry modules being superseded by streaming: `telemetry_collector.py` (448 lines), `telemetry_dashboard.py` (1,446 lines), `telemetry_schema.py` (~200 lines), `telemetry_models.py` (572 lines), `telemetry_config.py` (550 lines), `schema_unified.py` (138 lines)
- 3 prompt modules being consolidated: `prompt_templates.py` (726 lines), `prompt_engine.py` (950 lines) (keeping `prompt_compiler.py`)
- 1 demo: `prd_parser.py` (396 lines) — absorbed by `engine/context.py`
- 1 checkpoint: `checkpoint.py` (162 lines) — superseded by `engine/state.py`

Total: ~15,000 lines of code that will be moved to `legacy/`.

The benchmark platform modules (`runner.py`, `judge.py`, `pilot.py`, `report.py`, `gpu_fit.py`, `orchestrate.py`, `preflight.py`, `watchdog.py`) will be moved to `bench/`.

The problem is not that these modules are bad — many are well-written. The problem is that they exist alongside the new engine, creating 6 entry points for the same task, 4 pipeline implementations, and 2 overlapping telemetry systems.

## Solution

Move all superseded modules to `legacy/` (importable but not used by the engine or CLI). Move benchmark platform to `bench/`. Update all imports in the new `engine/` package and `cli.py` to reference only the retained modules. Run the full test suite to verify nothing breaks.

## User Stories

1. As a maintainer, I want all old modules moved to `legacy/` so that the top-level directory only contains the active engine, transport, streaming, and benchmark modules
2. As a maintainer, I want the `legacy/` directory to have an `__init__.py` that imports key classes so that existing code can do `from legacy.work_engine import WorkEngine` during transition
3. As a maintainer, I want the benchmark platform moved to `bench/` so that it's clearly separated from the engine
4. As a maintainer, I want `bench/` to have an `__init__.py` that re-exports `BenchmarkRunner`, `JudgeScorer`, etc. so that `from bench.runner import BenchmarkRunner` works
5. As a maintainer, I want all imports in `engine/` to reference only retained modules (transport, prompt_compiler, streaming_client) so that there are no circular dependencies
6. As a maintainer, I want all imports in `cli.py` to be lazy so that `--help` is fast
7. As a maintainer, I want the full test suite (`python3 -m pytest tests/`) to pass after migration so that nothing is broken
8. As a maintainer, I want `AGENTS.md` rewritten to reflect the new architecture so that agents have accurate documentation
9. As a maintainer, I want `README.md` regenerated to reflect the new architecture so that users have accurate documentation
10. As a maintainer, I want old test files (`test_integration.py`, `test_prompts.py`) moved to `tests/legacy/` so that they don't pollute the new test suite
11. As a maintainer, I want a `MIGRATION.md` document that maps old module names to new locations so that anyone referencing the old docs can find the new location
12. As a maintainer, I want the `legacy/` modules to be excluded from linting and type checking so that they don't generate noise
13. As a maintainer, I want the `bench/` modules to be independently runnable (`python3 -m bench.orchestrate --phase 1`) so that the benchmark platform works without the engine
14. As a maintainer, I want the top-level directory to contain fewer than 15 Python files so that the project is navigable
15. As a maintainer, I want each retained module to have a clear, single responsibility so that the architecture is understandable

## Implementation Decisions

### Decision 1: Legacy Directory

`legacy/` contains all superseded modules. It has:
- `__init__.py` that imports key classes for backward compatibility
- All `.py` files moved verbatim (no modifications)
- A `README.md` explaining that these are legacy modules, superseded by `engine/`

Modules moved to `legacy/`:
```
work_engine.py, work_engine_simple.py, demo_e2e.py, task_queue.py,
harness.py, enginectl.py, context_manager.py, code_generator.py,
quality_gates.py, test_runner.py, git_workflow.py, file_ops.py,
remote_control.py, sandbox_manager.py, gitea_utils.py,
telemetry_collector.py, telemetry_dashboard.py, telemetry_schema.py,
telemetry_models.py, telemetry_config.py, schema_unified.py,
prompt_templates.py, prompt_engine.py, prd_parser.py, checkpoint.py
```

### Decision 2: Bench Directory

`bench/` contains the benchmark platform. It has:
- `__init__.py` that re-exports key classes
- All benchmark modules moved verbatim
- Updated imports to reference `transport.py` (not `ssh_utils.py`)

Modules moved to `bench/`:
```
runner.py, judge.py, pilot.py, report.py, gpu_fit.py,
orchestrate.py, preflight.py, watchdog.py, checkpoint.py
```

### Decision 3: Retained Top-Level Modules

Only these Python files remain at the top level:
```
cli.py                  # NEW — unified CLI
transport.py            # KEEP — three-transport abstraction
ssh_utils.py            # KEEP — SSH transport (used by transport.py)
prompt_compiler.py      # KEEP — manifest-driven prompts
engine_service.py       # KEEP — HTTP API (port 3082)
engine_client.py        # KEEP — HTTP client
streaming_server.py     # KEEP — SSE server entry
streaming_core.py       # KEEP — ring buffer + SSE
streaming_routes.py     # KEEP — FastAPI routes
streaming_client.py     # KEEP — event emission
event_schema.py         # KEEP — SQLite WAL schema
dsh_adapter.py          # KEEP — DSH bridge
gpu_telemetry.py        # KEEP — GPU poller
generate_readme.py      # KEEP — utility
validate_e2e.py         # KEEP — E2E validation
```

That's 15 files — down from 52.

### Decision 4: Import Updates

The `engine/` package imports:
- `transport.get_transport` — for all remote operations
- `prompt_compiler.PromptCompiler` — for prompt assembly
- `streaming_client.emit_event_fire_and_forget` — for events
- `event_schema.ensure_schema` — for SQLite setup

The `cli.py` imports:
- `engine.Engine` — for ace commands
- `transport.get_transport` — for transport detection
- Subprocess calls to `bench/` modules — for bench commands

### Decision 5: Test Reorganization

```
tests/
├── __init__.py
├── unit/
│   ├── __init__.py
│   ├── test_engine_engine.py        # NEW
│   ├── test_engine_pipeline.py      # NEW
│   ├── test_engine_context.py       # NEW
│   ├── test_engine_generator.py     # NEW
│   ├── test_engine_validator.py     # NEW
│   ├── test_engine_tester.py        # NEW
│   ├── test_engine_committer.py     # NEW
│   ├── test_engine_state.py         # NEW
│   ├── test_engine_events.py        # NEW
│   ├── test_cli.py                  # NEW
│   ├── test_prompt_compiler.py      # MOVED from test_prompts.py
│   ├── test_streaming.py            # KEPT
│   ├── test_dsh_adapter.py          # KEPT
│   └── test_gpu_telemetry.py        # KEPT
├── integration/
│   ├── __init__.py
│   ├── test_engine_pipeline.py      # NEW
│   ├── test_transport.py            # NEW
│   └── test_streaming_integration.py # KEPT
├── e2e/
│   ├── __init__.py
│   └── test_prd_to_commit.py        # NEW — THE CRITICAL TEST
└── legacy/
    ├── test_integration.py          # MOVED
    ├── test_prompts.py              # MOVED
    └── test_streaming_split.py      # MOVED
```

### Decision 6: Documentation Updates

- `AGENTS.md` — rewritten for new architecture (module reference, CLI reference, data flow)
- `README.md` — regenerated by `generate_readme.py`
- `MIGRATION.md` — new file mapping old → new locations
- `CONTRIBUTING.md` — updated for new structure
- `docs/API-REFERENCE.md` — updated for new CLI

## Testing Decisions

- **Regression**: Run `python3 -m pytest tests/` after every move. Any failure blocks the next move.
- **Import check**: `python3 -c "from engine import Engine"` must succeed after migration.
- **CLI check**: `python3 cli.py --help` must print usage without errors.
- **Bench check**: `python3 -m bench.orchestrate --dry-run` must work.
- **Legacy check**: `python3 -c "from legacy.work_engine import WorkEngine"` must work.

## Out of Scope

1. Deleting legacy modules (moved, not deleted)
2. Modifying legacy module code (moved verbatim)
3. Rewriting legacy tests (moved, not rewritten)
4. Removing legacy test dependencies
5. Updating legacy module documentation

## Further Notes

### Why Move, Not Delete

Moving to `legacy/` instead of deleting serves three purposes:
1. **Safety net** — if the new engine has a bug, old modules are still importable
2. **Reference** — developers can look at old implementations for context
3. **Gradual migration** — existing scripts that import old modules can be updated incrementally

Legacy modules will be deleted after 2 weeks of stable operation of the new engine.

### Glossary

| Term | Definition |
|------|------------|
| **Legacy** | Superseded modules in `legacy/` directory |
| **Bench** | Benchmark platform in `bench/` directory |
| **Retained** | Active modules at top level |
