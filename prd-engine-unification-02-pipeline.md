# PRD: Pipeline State Machine and Unified CLI

## Problem Statement

The coder-harness codebase has 6 overlapping CLI entry points that confuse users and fragment the command surface:

1. `harness.py` (1,323 lines) — "master CLI" that delegates to 14+ subcommands
2. `enginectl.py` (1,046 lines) — "zero-dep CLI" with JSON output for operators
3. `demo_e2e.py` (709 lines) — standalone E2E demo
4. `work_engine.py` (1,440 lines) — full pipeline with Docker sandboxes
5. `work_engine_simple.py` (731 lines) — simplified pipeline
6. `orchestrate.py` (274 lines) — benchmark pipeline

Each CLI has different flags, different output formats, different error handling, and different subsets of the same commands. A user who types `python3 harness.py run` gets a different result than `python3 work_engine.py run` — both claim to "run a coding task" but execute different pipelines.

Additionally, there is no pipeline state machine. The existing `workflow-state-machine.py` (478 lines) documents a state machine but doesn't implement one. The actual execution is linear: parse → generate → test → commit with no resumability, no checkpoint, and no observable state.

The core problem: there is no single command that works, and there is no state machine that tracks progress.

## Solution

Build a unified CLI (`cli.py`) with 3 command groups (ace, bench, stream, status) that delegates to the engine package and benchmark platform. Implement a 12-state pipeline state machine in `engine/pipeline.py` that tracks every transition, checkpoints after each state, and supports resume from any point.

## User Stories

1. As a developer, I want `python3 cli.py ace run --prd my-prd.md --project /path` so that one command executes the entire pipeline
2. As a developer, I want `python3 cli.py ace parse my-prd.md` so that I can preview the task decomposition without executing
3. As a developer, I want `python3 cli.py ace status` so that I can see all running/completed engine runs
4. As a developer, I want `python3 cli.py ace status --run <id>` so that I can see detailed status of a specific run
5. As a developer, I want `python3 cli.py ace resume --run <id>` so that I can continue an interrupted run from its last checkpoint
6. As a developer, I want `python3 cli.py ace cancel` so that I can gracefully stop the current run (SIGINT also triggers this)
7. As a developer, I want all CLI commands to output structured JSON (`{"ok": true, "data": {...}}`) so that agents can parse the output
8. As a developer, I want a `--pretty` flag that formats JSON output with indentation so that human reading is easy
9. As a developer, I want a `--verbose` flag that enables debug logging so that I can troubleshoot issues
10. As a developer, I want `python3 cli.py bench run --config 3090-qwen36-35b` so that I can run benchmarks (delegating to existing `orchestrate.py`)
11. As a developer, I want `python3 cli.py bench pilot` so that I can run the methodology pilot
12. As a developer, I want `python3 cli.py bench report --format md` so that I can generate comparison reports
13. As a developer, I want `python3 cli.py stream start --port 3081` so that I can start the streaming server
14. As a developer, I want `python3 cli.py stream stop` so that I can stop the streaming server
15. As a developer, I want `python3 cli.py stream status` so that I can check if the streaming server is running
16. As a developer, I want `python3 cli.py status` so that I can see system health (GPU, BeeLlama, transport)
17. As a developer, I want `python3 cli.py health` so that I can run the 7-point preflight check
18. As a developer, I want every command to detect the transport once at startup so that there's no per-call overhead
19. As a developer, I want `--transport auto|http|ssh|local` on every command so that I can override transport detection
20. As a developer, I want the CLI to print a helpful error message when a command fails so that I know what went wrong
21. As a developer, I want `python3 cli.py --version` so that I can check the CLI version
22. As a developer, I want the CLI to be a single file (<600 lines) so that it's easy to understand and maintain
23. As a developer, I want the CLI to use lazy imports so that `cli.py --help` is fast (no heavy imports)
24. As a developer, I want the pipeline state machine to log every transition with timestamp so that I can audit the run
25. As a developer, I want the pipeline to checkpoint to SQLite after each state transition so that crashed runs are resumable
26. As a developer, I want the pipeline to enforce per-step timeouts (300s inference, 180s tests, 60s git) so that hung operations don't block forever
27. As a developer, I want the pipeline to emit events to the streaming server on every transition so that the dashboard shows real-time progress
28. As a developer, I want the pipeline to handle SIGINT/SIGTERM by checkpointing and transitioning to CANCELLED so that no work is lost
29. As a developer, I want the pipeline to track token usage (prompt tokens, completion tokens, thinking tokens) per task so that I can monitor cost
30. As a developer, I want the pipeline to produce a `RunResult` summary at completion with total time, tokens, tasks succeeded/failed so that I can assess the run

## Implementation Decisions

### Decision 1: CLI Architecture

`cli.py` is a monolithic file (~500 lines) using `argparse` with subparsers. It uses lazy imports: the heavy modules (`engine`, `bench/`, streaming) are only imported when their subcommand is invoked. This keeps `--help` fast.

Structure:
```
cli.py
├── ace run --prd <file> --project <path> [--config] [--dry-run]
├── ace parse <prd>
├── ace status [--run <id>]
├── ace resume --run <id>
├── ace cancel
├── bench run --config <id> [--task <id>]
├── bench pilot
├── bench report --format md|json
├── stream start [--port 3081]
├── stream stop
├── stream status
├── status
└── health
```

### Decision 2: Output Format

All commands output JSON to stdout:
```json
{"ok": true, "data": {...}}
```
or on error:
```json
{"ok": false, "error": "message", "detail": "..."}
```

The `--pretty` flag adds indentation. Logs go to stderr.

### Decision 3: Transport Detection

The CLI calls `transport.get_transport()` once during initialization and passes the transport instance to all downstream modules. The `--transport` flag overrides the auto-detection.

### Decision 4: State Machine Implementation

The `Pipeline` class in `engine/pipeline.py` implements the 12-state machine with:

- `transition(new_state, **kwargs)` — validates the transition is legal, updates state, logs, checkpoints, emits event
- `checkpoint()` — serializes pipeline state to SQLite
- `resume(run_id)` — class method that restores from SQLite
- `cancel()` — transitions to CANCELLED, checkpoints

Transition validation uses a whitelist:
```python
VALID_TRANSITIONS = {
    State.IDLE: [State.PARSING, State.CANCELLED],
    State.PARSING: [State.QUEUED, State.FAILED, State.CANCELLED],
    State.QUEUED: [State.CONTEXT, State.DONE, State.CANCELLED],
    State.CONTEXT: [State.GENERATE, State.CANCELLED],
    State.GENERATE: [State.VALIDATE, State.FAILED, State.CANCELLED],
    State.VALIDATE: [State.TEST, State.GENERATE, State.FAILED, State.CANCELLED],
    State.TEST: [State.COMMIT, State.GENERATE, State.FAILED, State.CANCELLED],
    State.COMMIT: [State.NEXT, State.QUEUED, State.CANCELLED],
    State.NEXT: [State.QUEUED, State.DONE, State.CANCELLED],
    State.DONE: [],
    State.FAILED: [],
    State.CANCELLED: [],
}
```

### Decision 5: Checkpoint Schema

Two new tables in `benchmark-results.db`:

```sql
CREATE TABLE IF NOT EXISTS engine_runs (
    id TEXT PRIMARY KEY,
    prd_path TEXT NOT NULL,
    project_path TEXT NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id TEXT,
    tasks_json TEXT,
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

### Decision 6: Signal Handling

The CLI registers SIGINT and SIGTERM handlers that:
1. Set a `cancelled` flag on the pipeline
2. Allow the current step to finish (don't interrupt mid-inference)
3. Checkpoint the pipeline state
4. Print a JSON summary of what was completed
5. Exit with code 0 (graceful) or 1 (error)

## Testing Decisions

- **CLI tests**: `tests/unit/test_cli.py` — test argument parsing, output format, error messages. Mock all heavy imports.
- **Pipeline tests**: `tests/unit/test_pipeline.py` — test state machine transitions, checkpoint/resume, timeout enforcement. Mock transport.
- **Integration**: `tests/integration/test_engine_pipeline.py` — test full pipeline with mock transport. Verify state transitions, retry logic, event emission.
- **Seam**: The seam is `transport.get_transport()`. Tests inject `MockTransport`.
- **Prior art**: `test_streaming.py` (1,027 lines) demonstrates the mocking pattern.

## Out of Scope

1. Interactive mode (all commands are batch)
2. Pipeline parallelism (tasks execute sequentially in V1)
3. Custom state machines (the 12 states are fixed)
4. Pipeline visualization (dashboard is existing)
5. Remote CLI (CLI runs on nightmare or Triton, not via HTTP)

## Further Notes

### Migration Path

The existing CLIs (`harness.py`, `enginectl.py`, etc.) will be moved to `legacy/` after Phase 4. During transition, they remain importable for backward compatibility.

### Glossary

| Term | Definition |
|------|------------|
| **CLI** | Command-line interface (`cli.py`) |
| **Pipeline** | The 12-state state machine in `engine/pipeline.py` |
| **Run** | A single execution of `Engine.run()` |
| **Checkpoint** | Serialized pipeline state in SQLite |
| **Transport** | The `transport.py` abstraction |
