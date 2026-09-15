# PRD: Unified Engine Core — The `engine/` Package

## Problem Statement

The coder-harness codebase has 4 overlapping pipeline modules (`task_queue.py`, `code_generator.py`, `demo_e2e.py`, `work_engine.py`) that each implement 60-80% of the same PRD→generate→validate→test→commit flow, with different interfaces, different error handling, and no shared state. There is no single entry point that can execute a PRD end-to-end. The engine has never generated, validated, tested, or committed real code.

Additionally, 6 CLI entry points (`harness.py`, `enginectl.py`, `demo_e2e.py`, `task_queue.py`, `work_engine.py`, `work_engine_simple.py`) confuse users about which command to run, and each delegates to different subsets of the same modules.

The core problem is architectural: there is no unified engine. There are 4 partial implementations that were never integrated.

## Solution

Create an `engine/` Python package with 10 modules that implement the complete autonomous coding pipeline behind a single `Engine` class. The `Engine.run(prd_path, project_path, config)` method executes a PRD end-to-end: parse → context → generate → validate → test → commit. The engine uses a 12-state state machine for resumability, emits events to the streaming server for observability, and delegates all remote operations to the proven `transport.py` layer.

A single `cli.py` replaces all 6 existing CLIs with one entry point: `python3 cli.py ace run --prd <file> --project <path>`.

## User Stories

1. As a developer, I want to run `python3 cli.py ace run --prd my-prd.md --project /path/to/project` so that the engine executes the entire PRD without further intervention
2. As a developer, I want the engine to parse my PRD into atomic, dependency-ordered tasks so that work happens in the right sequence
3. As a developer, I want the engine to build project context (file tree, imports, framework detection) for each task so that generated code matches my project's conventions
4. As a developer, I want the engine to generate code via BeeLlama inference with retry logic so that transient LLM errors don't block progress
5. As a developer, I want the engine to validate generated code through multiple stages (AST, syntax, imports) so that broken code is caught before testing
6. As a developer, I want the engine to run pytest against generated code so that I know the code actually works
7. As a developer, I want the engine to automatically fix failing tests with error context so that the retry loop is intelligent, not blind
8. As a developer, I want the engine to commit passing code to Gitea with conventional commit messages so that the git history is readable
9. As a developer, I want the engine to produce a structured `RunResult` with timing, token usage, and per-task outcomes so that I can assess quality and cost
10. As a developer, I want the engine to emit events to the streaming server so that I can monitor progress in real time
11. As a developer, I want the engine to work without the streaming server (opt-in) so that it's self-contained
12. As a developer, I want the engine to detect and cache the transport at startup so that there's no per-call overhead
13. As a developer, I want the engine to support `--dry-run` so that I can preview the task plan without executing
14. As a developer, I want the engine to support `--transport auto|http|ssh|local` so that I can force a specific transport mode
15. As a developer, I want the engine to checkpoint state after each task so that interrupted runs can be resumed
16. As a developer, I want `python3 cli.py ace status --run <id>` so that I can check the status of a running or completed engine run
17. As a developer, I want `python3 cli.py ace resume --run <id>` so that I can continue an interrupted run
18. As a developer, I want `python3 cli.py ace cancel` so that I can gracefully stop the engine (SIGINT saves state)
19. As a developer, I want the engine to log every state transition with timestamps so that I can audit what happened
20. As a developer, I want the engine to respect per-step timeouts (300s for inference, 180s for tests, 60s for git) so that hung operations don't block forever
21. As a developer, I want the engine to validate that generated code's imports exist in the project or are stdlib so that the code doesn't reference nonexistent modules
22. As a developer, I want the engine to detect the project's framework (Django, Flask, FastAPI, etc.) so that generated code follows framework conventions
23. As a developer, I want the engine to generate a summary report at the end so that I can quickly assess what was done
24. As a developer, I want the engine to support multiple model configs (`--config 3090-qwen36-35b`) so that I can use different models for different tasks
25. As a developer, I want the engine to track token usage and cost per task so that I can monitor resource consumption
26. As a developer, I want the engine to work with Python projects on Triton via the transport layer so that it doesn't require local installation
27. As a developer, I want the engine to create feature branches (not modify main) so that the main branch stays clean
28. As a developer, I want the engine to handle PRDs of varying formats (bullet lists, numbered tasks, tables) so that I don't need a rigid template
29. As a developer, I want the engine to flag ambiguous requirements in the PRD parsing stage so that I can clarify before code is generated
30. As a developer, I want the engine to retry code generation with progressively more context (error messages, stack traces, related files) so that fixes are intelligent

## Implementation Decisions

### Decision 1: Engine Package Structure

The `engine/` package contains 10 modules:

- `__init__.py` — `EngineConfig` dataclass, version, constants
- `engine.py` — `Engine` class: the single entry point (`run()`, `step()`, `status()`, `cancel()`)
- `pipeline.py` — `Pipeline` class: 12-state state machine with transition validation
- `context.py` — `ContextBuilder` class: project file tree, imports, framework detection
- `generator.py` — `CodeGenerator` class: prompt compilation → BeeLlama inference → code extraction
- `validator.py` — `Validator` class: 4-stage validation (AST → syntax → imports → execution)
- `tester.py` — `TestRunner` class: pytest execution via transport
- `committer.py` — `Committer` class: branch + write + commit + push via transport
- `state.py` — `RunState` class: SQLite persistence for pipeline state
- `events.py` — `EventBridge` class: fire-and-forget event emission to streaming

### Decision 2: Transport is a Dependency, Not Owned

The engine imports `transport.get_transport()` at `Engine.__init__()` time and caches the result. All remote operations (BeeLlama inference, file writes, git commands, test execution) go through the transport interface. The engine never imports `ssh_utils.py` or `engine_client.py` directly.

### Decision 3: Prompt Compiler is a Dependency, Not Owned

The engine imports `prompt_compiler.PromptCompiler` and `prompt_compiler.ManifestGenerator` for prompt assembly. The engine does not contain prompt templates — those live in `prompt_compiler.py` and `prompt_templates.py`.

### Decision 4: Streaming is Opt-In

The `EventBridge` in `engine/events.py` wraps `streaming_client.emit_event_fire_and_forget()`. If `streaming_url` is not provided to `Engine.__init__()`, events are silently dropped. The engine never blocks on event delivery.

### Decision 5: State Machine with 12 States

The pipeline uses 12 discrete states (IDLE, PARSING, QUEUED, CONTEXT, GENERATE, VALIDATE, TEST, COMMIT, NEXT, DONE, FAILED, CANCELLED) with validated transitions. Every `transition()` call:
1. Validates the transition is allowed
2. Updates `self.state`
3. Logs the transition with timestamp
4. Persists to SQLite (checkpoint)
5. Emits an event to the streaming server

### Decision 6: Retry with Escalation

- **GENERATE**: Max 3 retries. Each retry includes: previous failed code, validation errors, and error context from the LLM.
- **TEST**: Max 2 fix attempts. Each fix includes: test failure output, stack traces, and the code that failed.
- **COMMIT**: Max 2 retries. Fallback from branch+push to direct push.

### Decision 7: SQLite Schema for Run State

A new `engine_runs` table in `benchmark-results.db`:

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

### Decision 8: Dry-Run Mode

`--dry-run` executes PARSING → QUEUED and stops. Returns the task plan without generating code. Useful for previewing what the engine would do.

## Testing Decisions

- **Unit tests**: Each `engine/` module gets a test file in `tests/unit/test_engine_*.py`. Mock transport, mock BeeLlama, mock git.
- **Integration tests**: `tests/integration/test_engine_pipeline.py` tests the full pipeline with mock transport (no real BeeLlama).
- **E2E test**: `tests/e2e/test_prd_to_commit.py` tests with real BeeLlama against `test-prd.md`. This is THE critical test.
- **Seam**: The test seam is `transport.get_transport()`. Tests inject a `MockTransport` that returns canned responses.
- **Prior art**: `test_streaming.py` (1,027 lines) demonstrates the mocking pattern for this codebase.

## Out of Scope

1. Multi-language support (Python only in V1)
2. Docker sandbox execution (Phase 3 feature, not Phase 1)
3. Model swap during a run (single model per run)
4. PR creation via Gitea API (commit only, PR is Phase 2)
5. Telemetry dashboard integration (events only, dashboard is existing)
6. Frontend UI generation
7. Database migration generation
8. Performance optimization of generated code

## Further Notes

### Migration Path

The engine package will initially import logic from existing modules (e.g., `context.py` wraps `context_manager.py`, `generator.py` wraps `code_generator.py`). After Phase 1 is proven, the logic will be inlined and the old modules moved to `legacy/`.

### Glossary

| Term | Definition |
|------|------------|
| **Engine** | The unified `engine/` package — the one pipeline |
| **Pipeline** | The 12-state state machine driving PRD → commit |
| **Transport** | The `transport.py` abstraction (HTTP/Local/SSH) |
| **BeeLlama** | LLM inference server on Triton (ports 8080/8082) |
| **Context** | Project state snapshot for code generation |
| **Validator** | Multi-stage code validation pipeline |
| **RunResult** | Structured output from `Engine.run()` |
