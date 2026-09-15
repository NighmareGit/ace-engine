# legacy/ Sunset Policy

**Effective date:** 2026-09-04 (engine-unification close)
**Deletion date:** 2026-09-18 (14 days after close)

All modules in `legacy/` are superseded by the unified `engine/` package. They will
be deleted on **2026-09-18**. After that date any remaining references to `legacy.*`
imports will break.

## Module Migration Table

| # | legacy/ module | engine/ replacement | Notes |
|---|----------------|---------------------|-------|
| 1 | `code_generator.py` | `engine/generator.py` | Code generation via BeeLlama |
| 2 | `context_manager.py` | `engine/context.py` | Project context building |
| 3 | `demo_e2e.py` | `tests/e2e/` | End-to-end demo / test harness |
| 4 | `enginectl.py` | `engine/cli.py` | CLI entry point for engine commands |
| 5 | `file_ops.py` | `engine/paths.py` / `engine/sandbox.py` | File I/O and sandbox path management |
| 6 | `git_workflow.py` | `engine/committer.py` | Branch / commit / push workflow |
| 7 | `gitea_utils.py` | `engine/committer.py` | Gitea API client via SSH tunnel |
| 8 | `harness.py` | `engine/pipeline.py` | Master pipeline orchestration |
| 9 | `prd_parser.py` | `engine/prd.py` | PRD parsing and task decomposition |
| 10 | `prompt_engine.py` | `engine/prompts.py` | Prompt compilation engine |
| 11 | `prompt_templates.py` | `engine/prompts.py` | Task-type prompt templates |
| 12 | `quality_gates.py` | `engine/validator.py` | AST / syntax / import validation |
| 13 | `remote_control.py` | `transport.py` | Unified remote controller (transport layer) |
| 14 | `sandbox_manager.py` | `engine/sandbox.py` | Docker sandbox lifecycle |
| 15 | `schema_unified.py` | `engine/events.py` | Canonical SQLite schema |
| 16 | `task_queue.py` | `engine/state.py` | DAG task orchestration and state |
| 17 | `telemetry_collector.py` | `engine/events.py` | Active GPU / event collection |
| 18 | `telemetry_config.py` | `engine/events.py` | Centralized telemetry configuration |
| 19 | `telemetry_dashboard.py` | `engine/events.py` | HTML telemetry dashboard |
| 20 | `telemetry_models.py` | `engine/events.py` | Typed telemetry dataclasses |
| 21 | `telemetry_schema.py` | `engine/events.py` | Extended telemetry schema |
| 22 | `test_runner.py` | `engine/tester.py` | Pytest / unittest execution |
| 23 | `work_engine.py` | `engine/pipeline.py` | Full pipeline with Docker sandboxes |
| 24 | `work_engine_simple.py` | `engine/pipeline.py` | Simplified pipeline (SSH + curl) |
| 25 | `__init__.py` | — | No direct replacement; removed with package |

## Archive policy

On **2026-09-18**, the `legacy/` directory is **moved** (not deleted) to an
archive sibling directory named `legacy-deleted-<YYYYMMDD>/`. This preserves
the original files for reference while removing them from the import path.

- The move is atomic (single `shutil.move` call) — no files are deleted.
- A manifest of every moved file and its size is printed to stdout.
- The archive directory sits next to where `legacy/` used to be
  (same parent directory).
- To inspect archived code, browse `legacy-deleted-20260918/` directly.

**Never uses `rm -rf`.** The `--execute` flag is the only way to trigger the
archive, and it refuses to run before the sunset date (exit 2).

## What happens on 2026-09-18

1. Running `python3 tools/legacy_sunset.py --execute` moves `legacy/` to `legacy-deleted-20260918/`.
2. Any remaining `from legacy import ...` or `import legacy.*` will raise `ModuleNotFoundError`.
3. The `tools/legacy_sunset.py` script can be run without `--execute` to preview what would be archived.

## Running the sunset check

```bash
python3 tools/legacy_sunset.py          # preview: shows days remaining or archive plan
python3 tools/legacy_sunset.py --execute # archive: moves legacy/ to legacy-deleted-<YYYYMMDD>/
```
