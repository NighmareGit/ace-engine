# MIGRATION_BLUEPRINT.md: Engine Unification

## Overview

This blueprint maps old module locations to new locations and provides step-by-step migration instructions. The migration happens in Phase 4 of the epic, after the engine is proven.

---

## Migration Steps

### Step 1: Create Directory Structure

```bash
cd /home/<user>/projects/ace-engine
mkdir -p engine
mkdir -p bench
mkdir -p legacy
mkdir -p tests/unit
mkdir -p tests/integration
mkdir -p tests/e2e
mkdir -p tests/legacy
```

### Step 2: Move Benchmark Platform to bench/

| Source | Destination | Notes |
|--------|-------------|-------|
| `runner.py` | `bench/runner.py` | Move verbatim |
| `judge.py` | `bench/judge.py` | Move verbatim |
| `pilot.py` | `bench/pilot.py` | Move verbatim |
| `report.py` | `bench/report.py` | Move verbatim |
| `gpu_fit.py` | `bench/gpu_fit.py` | Move verbatim |
| `orchestrate.py` | `bench/orchestrate.py` | Move verbatim |
| `preflight.py` | `bench/preflight.py` | Move verbatim |
| `watchdog.py` | `bench/watchdog.py` | Move verbatim |
| `checkpoint.py` | `bench/checkpoint.py` | Move verbatim |

Create `bench/__init__.py`:
```python
"""Benchmark platform for evaluating LLM inference quality."""
from .runner import BenchmarkRunner
from .judge import JudgeScorer
```

### Step 3: Move Superseded Modules to legacy/

| Source | Destination | Notes |
|--------|-------------|-------|
| `work_engine.py` | `legacy/work_engine.py` | Replaced by engine/engine.py |
| `work_engine_simple.py` | `legacy/work_engine_simple.py` | Replaced by engine/engine.py |
| `demo_e2e.py` | `legacy/demo_e2e.py` | Replaced by engine/engine.py |
| `task_queue.py` | `legacy/task_queue.py` | Replaced by engine/pipeline.py |
| `code_generator.py` | `legacy/code_generator.py` | Replaced by engine/generator.py |
| `context_manager.py` | `legacy/context_manager.py` | Replaced by engine/context.py |
| `quality_gates.py` | `legacy/quality_gates.py` | Replaced by engine/validator.py |
| `test_runner.py` | `legacy/test_runner.py` | Replaced by engine/tester.py |
| `git_workflow.py` | `legacy/git_workflow.py` | Replaced by engine/committer.py |
| `file_ops.py` | `legacy/file_ops.py` | Absorbed by engine/context.py |
| `prd_parser.py` | `legacy/prd_parser.py` | Absorbed by engine/engine.py |
| `prompt_templates.py` | `legacy/prompt_templates.py` | Absorbed by prompt_compiler.py |
| `prompt_engine.py` | `legacy/prompt_engine.py` | Merged into prompt_compiler.py |
| `harness.py` | `legacy/harness.py` | Replaced by cli.py |
| `enginectl.py` | `legacy/enginectl.py` | Replaced by cli.py |
| `remote_control.py` | `legacy/remote_control.py` | Absorbed by cli.py + transport.py |
| `sandbox_manager.py` | `legacy/sandbox_manager.py` | Absorbed by engine/committer.py |
| `gitea_utils.py` | `legacy/gitea_utils.py` | Absorbed by engine/committer.py |
| `telemetry_collector.py` | `legacy/telemetry_collector.py` | Replaced by gpu_telemetry.py |
| `telemetry_dashboard.py` | `legacy/telemetry_dashboard.py` | Replaced by streaming_routes.py |
| `telemetry_schema.py` | `legacy/telemetry_schema.py` | Replaced by event_schema.py |
| `telemetry_models.py` | `legacy/telemetry_models.py` | Absorbed by engine/generator.py |
| `telemetry_config.py` | `legacy/telemetry_config.py` | Absorbed by engine/__init__.py |
| `schema_unified.py` | `legacy/schema_unified.py` | Replaced by event_schema.py |

Create `legacy/__init__.py` with deprecation warnings:
```python
"""Legacy modules — superseded by engine/. Will be deleted after 2-week sunset."""
import warnings

def _warn(module_name):
    warnings.warn(
        f"Importing from legacy.{module_name} is deprecated. "
        f"Use the new engine/ package instead. "
        f"legacy/ will be deleted after 2026-09-18.",
        DeprecationWarning,
        stacklevel=2
    )
```

### Step 4: Move Old Tests to tests/legacy/

| Source | Destination |
|--------|-------------|
| `test_integration.py` | `tests/legacy/test_integration.py` |
| `test_prompts.py` | `tests/legacy/test_prompts.py` |

### Step 5: Update Imports in Retained Modules

**RED-TEAM M1 FIX — retained modules that import moved modules will break.**
Before moving anything, audit retained modules for imports of to-be-moved modules
and re-point or absorb them:

| Retained module | Imports from (moving to legacy/) | Fix |
|-----------------|----------------------------------|-----|
| `engine_service.py` | `test_runner.py`, `code_generator.py`, `git_workflow.py`, `quality_gates.py` (check actual imports) | Either keep needed logic inline, or import from `legacy.` explicitly during the 2-week window |
| `streaming_routes.py` / `dsh_adapter.py` | audit for moved-module imports | Same treatment |
| `generate_readme.py`, `validate_e2e.py` | audit | Same treatment |

**Rule: run `python3 -c "import <retained_module>"` for every retained module
after each move batch; any ImportError blocks the next batch.**

**RED-TEAM M3 FIX — scripts referencing old paths:** update
`tickets/deploy/scripts/bench-*.sh` to call `python3 -m bench.<module>` (or run
them with the repo root on PYTHONPATH pointing at the new locations).

**RED-TEAM M2 FIX — sunset date:** derive the deprecation-warning date from a
constant set at migration time (`LEGACY_SUNSET = date + timedelta(days=14)`),
not a hardcoded literal.

**transport.py** — no changes needed (already self-contained)

**prompt_compiler.py** — absorb templates from prompt_templates.py:
- Copy `PromptTemplates` class inline
- Copy `ManifestGenerator` class inline
- Update `__all__` to export both

**engine_service.py** — no changes needed (already self-contained)

**engine_client.py** — no changes needed (already self-contained)

**streaming_*.py** — no changes needed (already self-contained)

**event_schema.py** — add engine tables:
```sql
CREATE TABLE IF NOT EXISTS engine_runs (...);
CREATE TABLE IF NOT EXISTS engine_task_results (...);
```

### Step 6: Update AGENTS.md

Rewrite to reflect new architecture:
- Module reference: engine/ (10 modules) + bench/ (9 modules) + retained (15 files)
- CLI reference: cli.py with ace/bench/stream/status groups
- Data flow: PRD → engine → transport → BeeLlama/Gitea

### Step 7: Regenerate README.md

Run `python3 generate_readme.py` or manually update to reflect new structure.

### Step 8: Verify

```bash
# Import checks
python3 -c "from engine import Engine, EngineConfig"
python3 -c "from legacy.work_engine import WorkEngine"  # with deprecation warning
python3 -c "from bench.runner import BenchmarkRunner"

# CLI check
python3 cli.py --help

# Bench check
python3 -m bench.orchestrate --dry-run

# Test suite
python3 -m pytest tests/unit/ -v
python3 -m pytest tests/integration/ -v
```

---

## Rollback Plan

If migration breaks something:

1. Copy modules back from `legacy/` to top-level
2. Copy benchmark modules back from `bench/` to top-level
3. Restore original `AGENTS.md` and `README.md`
4. Run `python3 -m pytest tests/` to verify restoration

The `legacy/` directory serves as the rollback mechanism — modules are moved, not deleted.

---

## Timeline

| Day | Action | Verification |
|-----|--------|-------------|
| Day 1 | Steps 1-3: Create dirs, move bench + legacy | Import checks pass |
| Day 2 | Steps 4-5: Move tests, update imports | Test suite passes |
| Day 3 | Steps 6-7: Update docs | Docs accurate |
| Day 4 | Step 8: Full verification | All checks pass |
| Day 5-14 | Legacy sunset window | Track legacy imports |
| Day 15 | Delete legacy/ | Final cleanup |

---

## Import Changes Summary

### Before (old pattern)
```python
from code_generator import CodeGenerator
from context_manager import ContextManager
from quality_gates import QualityGates
from test_runner import TestRunner
from git_workflow import GitWorkflow
from task_queue import TaskQueue
```

### After (new pattern)
```python
from engine.engine import Engine
from engine.pipeline import Pipeline, State
from engine.context import build_context
from engine.generator import generate_code
from engine.validator import validate
from engine.tester import run_tests
from engine.committer import commit_code
```

### Transport (unchanged)
```python
from transport import get_transport  # Same as before
```

### Streaming (unchanged)
```python
from streaming_client import StreamingClient  # Same as before
```

---

## Database Changes

### New Tables (in a NEW database: `engine.db`)

> CONFORMANCE FIX: Handoff Rule 6 — "Don't merge the SQLite databases. Separate DBs with cross-references." Engine run state lives in its own `engine.db`, NOT in `benchmark-results.db`.

```sql
CREATE TABLE IF NOT EXISTS engine_runs (
    id TEXT PRIMARY KEY,
    prd_path TEXT NOT NULL,
    project_path TEXT NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id TEXT,
    tasks_json TEXT,
    task_retries_json TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    result_json TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS engine_task_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES engine_runs(id),
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    generated_code TEXT,
    validation_result TEXT,
    test_result TEXT,
    commit_sha TEXT,
    attempts INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT
);
```

### Existing Tables (unchanged)

All benchmark tables (`model_configs`, `tasks`, `benchmark_runs`, `judge_scores`, etc.) remain in `benchmark-results.db` and are untouched.

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'engine'"
Run from the `coder-harness/` directory. The `engine/` package is a local package.

### "ModuleNotFoundError: No module named 'transport'"
Run from the `coder-harness/` directory. `transport.py` is a local module.

### "ImportError: cannot import name 'CodeGenerator' from 'code_generator'"
You're importing from the old location. Use `from engine.generator import generate_code`.

### "sqlite3.OperationalError: no such table: engine_runs"
Run `python3 -c "from engine.state import init_db; init_db()"` to create the tables.

### Old tests fail after migration
Old tests in `tests/legacy/` may reference old module locations. These are expected to fail until updated. Run `python3 -m pytest tests/ -m "not legacy"` to skip them.
