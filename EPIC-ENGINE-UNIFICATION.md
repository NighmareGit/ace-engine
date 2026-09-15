# Epic: Engine Unification — From Scaffolding to Autonomous Coding Engine

> **Date:** 2026-09-04
> **Status:** Planning
> **Author:** MiMo (metacognitive friction + fireplace + red-team + codebase-design applied)
> **Supersedes:** `autonomous-engine-roadmap.md`, `autonomous-engine-roadmap-v2.md`

---

## Problem Statement

The coder-harness codebase has 48,455 lines of Python across 52 files with **6 overlapping entry points** for "run a coding task" (`harness.py`, `enginectl.py`, `demo_e2e.py`, `task_queue.py`, `work_engine.py`, `work_engine_simple.py`). The subsystems are real but disconnected. The core loop (PRD → generate → validate → fix → commit) has never executed end-to-end on real code. The engine is a collection of well-built rooms with no hallways connecting them.

**Root cause:** The benchmark platform and the autonomous coding engine were built in the same directory, sharing modules, CLIs, and SQLite databases, without ever being properly separated. New features were added as new files rather than integrating into existing modules.

**Impact:** The engine cannot generate, validate, test, or commit real code. 274 tests validate infrastructure, not output quality. The system has never produced a single line of working code from a PRD.

---

## Solution

Consolidate the 6 overlapping pipeline modules into **one unified engine** (`engine/` package) with a single state machine, one CLI entry point, and a verified end-to-end path from PRD to Gitea commit. Move the benchmark platform into its own `bench/` package. Move defunct modules to `legacy/`. Prove the engine works with a real E2E test against a trivial PRD.

---

## Current State: The Dependency Graph (What Exists)

```
CLIs (6 entry points — THE PROBLEM):
  harness.py (1,323 lines) ──→ delegates to everything
  enginectl.py (1,046 lines) ──→ delegates to everything (zero-dep variant)
  demo_e2e.py (709 lines) ──→ self-contained pipeline
  work_engine.py (1,440 lines) ──→ self-contained pipeline
  work_engine_simple.py (731 lines) ──→ self-contained pipeline (simplified)
  orchestrate.py (274 lines) ──→ benchmark pipeline (separate concern)

Pipeline modules (4 implementations of the same thing):
  task_queue.py (1,585 lines) ──→ DAG + dependency ordering
  code_generator.py (1,620 lines) ──→ generate + retry + quality gates
  demo_e2e.py (709 lines) ──→ parse + generate + test + commit
  work_engine.py (1,440 lines) ──→ swap + sandbox + infer + push + cleanup

Supporting modules (reused by pipelines):
  prd_parser.py (396 lines) ──→ PRD → task list
  context_manager.py (505 lines) ──→ project file tracking
  quality_gates.py (607 lines) ──→ AST + imports + style
  test_runner.py (887 lines) ──→ pytest execution
  git_workflow.py (414 lines) ──→ branch + commit + push
  file_ops.py (693 lines) ──→ file read/write via SSH
  prompt_templates.py (726 lines) ──→ 7 task-type templates
  prompt_engine.py (950 lines) ──→ manifest-driven assembly
  prompt_compiler.py (1,019 lines) ──→ manifest + template resolution

Transport (proven, keep):
  transport.py (670 lines) ──→ HTTP/Local/SSH abstraction
  ssh_utils.py (261 lines) ──→ SSH + BeeLlama + nvidia-smi

Streaming (real, tested, keep):
  streaming_server.py (98 lines) ──→ SSE server entry
  streaming_core.py (586 lines) ──→ ring buffer + SSE manager
  streaming_routes.py (720 lines) ──→ FastAPI routes
  streaming_client.py (450 lines) ──→ event emission
  event_schema.py (329 lines) ──→ SQLite WAL schema
  dsh_adapter.py (635 lines) ──→ DSH → streaming bridge
  gpu_telemetry.py (470 lines) ──→ GPU poller

Engine service (HTTP API, keep):
  engine_service.py (1,265 lines) ──→ FastAPI on port 3082
  engine_client.py (374 lines) ──→ HTTP client

Telemetry (redundant with streaming, consolidate):
  telemetry_collector.py (448 lines) ──→ GPU/event collection
  telemetry_dashboard.py (1,446 lines) ──→ HTML dashboard
  telemetry_schema.py (~200 lines) ──→ 6 new tables
  telemetry_models.py (572 lines) ──→ typed dataclasses
  telemetry_config.py (550 lines) ──→ centralized config
  schema_unified.py (138 lines) ──→ canonical schema

Remote control (absorb into transport + CLI):
  remote_control.py (1,088 lines) ──→ unified controller
  sandbox_manager.py (588 lines) ──→ Docker sandbox lifecycle
  gitea_utils.py (663 lines) ──→ Gitea API client
```

---

## Target State: The Unified Architecture

```
coder-harness/
│
├── engine/                          # NEW — the one engine
│   ├── __init__.py                  # Config, constants, version
│   ├── engine.py                    # Engine class: run(), step(), status(), cancel()
│   ├── pipeline.py                  # Pipeline state machine (12 states)
│   ├── context.py                   # Project context builder
│   ├── generator.py                 # Code generation (prompt_compiler + BeeLlama)
│   ├── validator.py                 # Multi-stage validation (AST → syntax → import → exec)
│   ├── tester.py                    # Test runner (pytest via transport)
│   ├── committer.py                 # Git + Gitea (branch, commit, push, PR)
│   ├── state.py                     # Run state persistence (SQLite)
│   └── events.py                    # Event bridge → streaming_client
│
├── bench/                           # RENAME — benchmark platform (untouched)
│   ├── __init__.py
│   ├── runner.py
│   ├── judge.py
│   ├── pilot.py
│   ├── report.py
│   ├── gpu_fit.py
│   ├── orchestrate.py
│   ├── preflight.py
│   ├── watchdog.py
│   └── checkpoint.py
│
├── transport.py                     # KEEP — three-transport abstraction
├── ssh_utils.py                     # KEEP — SSH transport
├── prompt_compiler.py               # KEEP — manifest-driven prompts
├── engine_service.py                # KEEP — HTTP API (port 3082)
├── engine_client.py                 # KEEP — HTTP client
├── streaming_server.py              # KEEP — SSE server
├── streaming_core.py                # KEEP — ring buffer + SSE
├── streaming_routes.py              # KEEP — FastAPI routes
├── streaming_client.py              # KEEP — event emission
├── event_schema.py                  # KEEP — SQLite WAL schema
├── dsh_adapter.py                   # KEEP — DSH bridge
├── gpu_telemetry.py                 # KEEP — GPU poller
│
├── cli.py                           # NEW — single CLI entry point
│
├── legacy/                          # MOVED — old modules, importable but not used
│   ├── work_engine.py
│   ├── work_engine_simple.py
│   ├── demo_e2e.py
│   ├── harness.py
│   ├── enginectl.py
│   ├── remote_control.py
│   ├── sandbox_manager.py
│   ├── gitea_utils.py
│   ├── file_ops.py
│   ├── context_manager.py
│   ├── code_generator.py
│   ├── quality_gates.py
│   ├── test_runner.py
│   ├── git_workflow.py
│   ├── task_queue.py
│   ├── checkpoint.py
│   ├── prd_parser.py
│   ├── prompt_templates.py
│   ├── prompt_engine.py
│   ├── telemetry_collector.py
│   ├── telemetry_dashboard.py
│   ├── telemetry_schema.py
│   ├── telemetry_config.py
│   ├── telemetry_models.py
│   └── schema_unified.py
│
├── models/manifest.json
├── corpus.json
├── projects.yaml
│
└── tests/
    ├── unit/
    │   ├── test_prompt_compiler.py
    │   ├── test_streaming.py
    │   ├── test_dsh_adapter.py
    │   └── test_gpu_telemetry.py
    ├── integration/
    │   ├── test_engine_pipeline.py   # NEW
    │   └── test_streaming_integration.py
    └── e2e/
        └── test_prd_to_commit.py    # THE CRITICAL TEST
```

---

## Pipeline State Machine (The Heart of the Engine)

This is the **single most important artifact** in the entire redesign. Every other decision flows from this.

```
                            ┌──────────┐
                            │   IDLE   │
                            └────┬─────┘
                                 │ engine.run(prd, project, config)
                                 ▼
                            ┌──────────┐
                            │ PARSING  │  prd_parser.parse(prd_path)
                            └────┬─────┘  → task list + dependency DAG
                                 │ tasks ready
                                 ▼
                            ┌──────────┐
                  ┌────────│  QUEUED  │  task_queue.next() — topological order
                  │         └────┬─────┘
                  │              │ next task available
                  │              ▼
                  │         ┌──────────┐
                  │         │ CONTEXT  │  context.build(project_path, task)
                  │         └────┬─────┘  → file tree + imports + framework
                  │              │ context ready
                  │              ▼
                  │         ┌──────────┐
                  │         │GENERATE  │  generator.generate(context, task)
                  │         └────┬─────┘  → prompt_compiler → BeeLlama → extract code
                  │              │ code produced
                  │              ▼
                  │         ┌──────────┐
                  │         │ VALIDATE │  validator.validate(code, context)
                  │         └────┬─────┘  → AST parse → syntax → imports
                  │              │
                  │         ┌────┴────┐
                  │         │         │
                  │      PASS      FAIL ──→ retry_count < 3? ──→ GENERATE
                  │         │         │         │
                  │         │         │    retry_count >= 3
                  │         │         │         │
                  │         │         │         ▼
                  │         │         │    ┌────────┐
                  │         │         │    │ FAILED │  → mark task failed
                  │         │         │    └───┬────┘
                  │         │         │        │
                  │         │         │        └──→ QUEUED (next task)
                  │         │         │
                  │         ▼         │
                  │    ┌──────────┐   │
                  │    │  TEST    │   │  tester.test(project_path)
                  │    └────┬─────┘   │  → pytest execution + parse results
                  │         │         │
                  │    ┌────┴────┐    │
                  │    │         │    │
                  │ PASS       FAIL  │
                  │    │         │    │
                  │    │    fix_count < 2? ──→ GENERATE (with error context)
                  │    │         │         │
                  │    │    fix_count >= 2  │
                  │    │         │         │
                  │    │         ▼         │
                  │    │    ┌────────┐    │
                  │    │    │ FAILED │    │
                  │    │    └───┬────┘    │
                  │    │        │         │
                  │    │        └──→ QUEUED (next task)
                  │    │
                  │    ▼
                  │ ┌──────────┐
                  │ │COMMIT    │  committer.commit(project_path, task, code)
                  │ └────┬─────┘  → branch + commit + push
                  │      │
                  │      ▼
                  │ ┌──────────┐
                  └→│  NEXT    │  → loop back to QUEUED
                    │  TASK    │
                    └────┬─────┘
                         │ all tasks done
                         ▼
                    ┌──────────┐
                    │  DONE    │  → summary report + final commit
                    └──────────┘
```

### State Definitions

| State | Input | Output | Timeout | Retry |
|-------|-------|--------|---------|-------|
| `IDLE` | — | — | — | — |
| `PARSING` | PRD markdown | Task list + DAG | 30s | No |
| `QUEUED` | Task list | Next task (topological) | — | — |
| `CONTEXT` | Project path + task | File tree, imports, framework | 120s | No |
| `GENERATE` | Context + task + error ctx | Generated code | 300s | Yes (max 3) |
| `VALIDATE` | Generated code | Pass/fail + errors | 30s | No |
| `TEST` | Project path | Test results | 180s | No |
| `COMMIT` | Code + task + results | Git SHA | 60s | Yes (max 2) |
| `NEXT` | — | — | — | — |
| `DONE` | All tasks | Summary report | — | — |
| `FAILED` | Error context | Failure record | — | — |
| `CANCELLED` | — | Checkpoint state | — | — |

### Transition Rules

| From | To | Condition |
|------|----|-----------|
| IDLE → PARSING | `engine.run()` called |
| PARSING → QUEUED | PRD parsed successfully |
| PARSING → FAILED | PRD unparseable |
| QUEUED → CONTEXT | Next task available |
| QUEUED → DONE | All tasks completed |
| CONTEXT → GENERATE | Context built |
| GENERATE → VALIDATE | Code produced |
| VALIDATE → TEST | Validation passed |
| VALIDATE → GENERATE | Validation failed, retry < 3 |
| VALIDATE → FAILED | Validation failed, retry >= 3 |
| TEST → COMMIT | Tests passed |
| TEST → GENERATE | Tests failed, fix < 2 |
| TEST → FAILED | Tests failed, fix >= 2 |
| COMMIT → NEXT | Commit successful |
| COMMIT → QUEUED | Commit failed (continue to next task) |
| NEXT → QUEUED | More tasks remaining |
| NEXT → DONE | No more tasks |
| Any → CANCELLED | User cancels (SIGINT/SIGTERM) |
| Any → FAILED | Unrecoverable error |

### Retry Strategy

| Step | Max Retries | Escalation |
|------|-------------|------------|
| GENERATE | 3 | Each retry adds error context + previous failed code |
| TEST | 2 (fixes) | Each fix adds test failure output + stack trace |
| COMMIT | 2 | Fallback to direct push if branch fails |

---

## Module Interface Contracts

### `engine/engine.py` — The One Entry Point

```python
class Engine:
    def __init__(self, transport=None, streaming_url=None, config=None):
        """
        Args:
            transport: Auto-detected if None. From transport.get_transport().
            streaming_url: Optional. Events emitted if set.
            config: EngineConfig dataclass. Defaults if None.
        """

    def run(self, prd_path: str, project_path: str,
            config: str = "3090-qwen36-35b",
            dry_run: bool = False) -> RunResult:
        """Execute a PRD end-to-end. Returns structured result."""

    def step(self, task_id: str) -> StepResult:
        """Execute a single task (manual control)."""

    def status(self, run_id: str = None) -> RunStatus:
        """Get pipeline status."""

    def cancel(self) -> bool:
        """Cancel gracefully. Checkpoints state."""
```

### `engine/pipeline.py` — State Machine

```python
class Pipeline:
    def __init__(self, engine: 'Engine'):
        self.state = State.IDLE
        self.tasks: list[Task] = []
        self.current_task: Task | None = None
        self.run_id: str

    def transition(self, new_state: State, **kwargs) -> TransitionResult:
        """Advance state machine. Validates transitions. Logs events."""

    def checkpoint(self) -> None:
        """Persist current state to SQLite."""

    @classmethod
    def resume(cls, run_id: str, engine: 'Engine') -> 'Pipeline':
        """Restore from checkpoint."""
```

### `engine/context.py` — Project Context Builder

```python
class ContextBuilder:
    def build(self, project_path: str, task: Task) -> Context:
        """
        Returns Context with:
            - file_tree: dict of project files
            - relevant_files: files related to this task
            - imports: detected import dependencies
            - framework: detected framework (django, flask, fastapi, etc.)
            - existing_code: relevant existing code snippets
        """
```

### `engine/generator.py` — Code Generation

```python
class CodeGenerator:
    def generate(self, context: Context, task: Task,
                 error_ctx: ErrorContext = None) -> GeneratedCode:
        """
        1. Builds prompt via prompt_compiler
        2. Calls BeeLlama via transport
        3. Extracts code blocks from response
        4. Returns GeneratedCode with files, metadata, timings
        """
```

### `engine/validator.py` — Multi-Stage Validation

```python
class Validator:
    def validate(self, code: GeneratedCode, context: Context) -> ValidationResult:
        """
        Stages:
            1. AST parse (Python: ast.parse, JS: try parse)
            2. Import resolution (stdlib? project? unknown?)
            3. Syntax check (compile/eval in isolated namespace)
            4. Style check (basic naming, no obvious smells)
        Returns ValidationResult with pass/fail per stage + errors.
        """
```

### `engine/tester.py` — Test Runner

```python
class TestRunner:
    def test(self, project_path: str, timeout: int = 180) -> TestResult:
        """
        1. Runs pytest via transport
        2. Parses output (passed, failed, errors)
        3. Returns TestResult with counts, failures, stack traces
        """
```

### `engine/committer.py` — Git + Gitea

```python
class Committer:
    def commit(self, project_path: str, task: Task,
               code: GeneratedCode, test_result: TestResult) -> CommitResult:
        """
        1. Create feature branch (ace/{task_id}-{timestamp})
        2. Write generated files
        3. Git add + commit (conventional commit message)
        4. Push to Gitea
        5. Return CommitResult with SHA, branch, push status
        """
```

### `engine/state.py` — Run Persistence

```python
class RunState:
    def save(self, pipeline: Pipeline) -> None:
        """Persist pipeline state to SQLite."""

    def load(self, run_id: str) -> dict:
        """Load pipeline state from SQLite."""

    def list_runs(self, status: str = None) -> list[dict]:
        """List all runs, optionally filtered by status."""
```

### `cli.py` — Single Entry Point

```bash
# Engine commands
python3 cli.py ace run --prd <file> --project <path> [--config <id>] [--dry-run]
python3 cli.py ace status [--run <id>]
python3 cli.py ace parse <prd>
python3 cli.py ace resume --run <id>
python3 cli.py ace cancel [--run <id>]

# Benchmark commands (delegate to bench/)
python3 cli.py bench run --config <id> [--task <id>]
python3 cli.py bench pilot
python3 cli.py bench report --format md

# Stream commands
python3 cli.py stream start [--port 3081]
python3 cli.py stream stop
python3 cli.py stream status

# System commands
python3 cli.py status
python3 cli.py health

# Global flags
--transport [auto|http|ssh|local]
--pretty
--verbose
```

---

## Phase Plan

### Phase 1: Engine Kernel
**Goal:** Build `engine/` package + `cli.py` + E2E test. Prove it works.

**Deliverables:**
- `engine/__init__.py` — Config dataclass, constants
- `engine/engine.py` — Engine class with `run()`, `step()`, `status()`, `cancel()`
- `engine/pipeline.py` — 12-state state machine
- `engine/context.py` — Project context builder (absorbs `context_manager.py`)
- `engine/generator.py` — Code generation (absorbs `code_generator.py` core)
- `engine/validator.py` — Multi-stage validation (absorbs `quality_gates.py`)
- `engine/tester.py` — Test runner (absorbs `test_runner.py` core)
- `engine/committer.py` — Git + Gitea (absorbs `git_workflow.py` + `gitea_utils.py`)
- `engine/state.py` — Run state persistence
- `engine/events.py` — Event bridge to streaming
- `cli.py` — Single CLI entry with `ace` subcommands
- `tests/e2e/test_prd_to_commit.py` — THE critical test

**Success criteria:**
```bash
python3 cli.py ace run --prd test-prd.md --project /home/<user>/projects/ace-demo --dry-run
# → Produces complete task plan with timing estimates

python3 cli.py ace run --prd test-prd.md --project /home/<user>/projects/ace-demo
# → Generates real code, validates, tests, commits to Gitea
```

**Estimated effort:** 4-6 hours

### Phase 2: Integration
**Goal:** Wire streaming events, verify transport end-to-end, test with real BeeLlama.

**Deliverables:**
- `engine/events.py` connected to `streaming_client`
- `cli.py` `stream start/stop` commands
- `--transport` flag on all commands
- Real inference test (not dry-run)

**Success criteria:**
- Pipeline emits events visible in streaming dashboard
- Transport auto-detection works across all three modes
- Real BeeLlama inference produces valid code

**Estimated effort:** 2-3 hours

### Phase 3: Hardening
**Goal:** Checkpoint/resume, timeouts, sandbox option, auto-revert.

**Deliverables:**
- Checkpoint/resume for interrupted runs
- Per-step timeouts (configurable)
- `--sandbox` flag for Docker-isolated execution
- Auto-revert on test failure (git stash before write)
- Credential centralization (single config file)

**Success criteria:**
- `python3 cli.py ace resume --run <id>` recovers from crash
- Timeouts prevent hung pipelines
- `--sandbox` runs tests in Docker container

**Estimated effort:** 2-3 hours

### Phase 4: Consolidation
**Goal:** Move old modules to `legacy/`, update docs, clean up.

**Deliverables:**
- All old modules moved to `legacy/`
- `AGENTS.md` rewritten for new architecture
- `README.md` regenerated
- `tests/unit/` and `tests/integration/` reorganized
- Old test files migrated or removed

**Success criteria:**
- `python3 -m pytest tests/` — all tests pass
- No imports from legacy modules in engine/ or bench/
- Documentation reflects actual architecture

**Estimated effort:** 1-2 hours

---

## Red Team: Attack Vectors

| # | Vector | Severity | Mitigation |
|---|--------|----------|------------|
| 1 | Moving 25 files to legacy/ breaks imports for anyone using the old CLIs | High | Keep legacy/ importable. Add `from legacy.work_engine import WorkEngine` compatibility shims. Remove after 1 week. |
| 2 | The pipeline state machine has no per-step timeout — BeeLlama hangs = pipeline stalls | High | Every `transition()` call checks elapsed time against state timeout. Enforced in `pipeline.py`. |
| 3 | Validator has false negatives — lets broken code through | Medium | Add execution-based validation (run code in isolated namespace). Not just AST. |
| 4 | Generator produces code that passes validation but doesn't actually work | Medium | The TEST stage catches this. But first-pass success rate may be low. Acceptable — retry handles it. |
| 5 | Streaming is opt-in but someone forgets to start it | Low | Engine works without streaming. Events are fire-and-forget. No blocking. |
| 6 | Credential sprawl — Gitea token in multiple files | Medium | Centralize in `engine/config.yaml` or env vars. Single source of truth. |

---

## Decision Log

| # | Decision | Rationale | Trade-off |
|---|----------|-----------|-----------|
| D1 | One engine package, not scattered modules | Eliminates 6 entry points, 4 pipelines | Must move 25 files to legacy/ |
| D2 | State machine with 12 states | Each state is testable, resumable, observable | More complex than linear pipeline |
| D3 | Validator is multi-stage (AST → syntax → import → exec) | Catches progressively more issues | Each stage adds latency (~1s total) |
| D4 | Streaming is opt-in, not hard dependency | Engine works standalone | No real-time visibility unless started |
| D5 | Legacy/ not delete | Safety net during transition | Directory clutter for 1 week |
| D6 | CLI is monolithic, not plugin-based | 52 files is enough | Single file grows to ~500 lines |
| D7 | Sandbox is opt-in (`--sandbox`) | Direct execution is faster, simpler | Less isolation for generated code |
| D8 | Benchmark platform moves to bench/ | Separates concerns | Must update imports in orchestrate.py |

---

## Glossary

| Term | Definition |
|------|------------|
| **ACE** | Autonomous Coding Engine — the unified engine |
| **Pipeline** | The state machine that drives PRD → commit |
| **Task** | A single atomic coding unit derived from a PRD |
| **Context** | Project state snapshot used for code generation |
| **Validator** | Multi-stage code validation (AST, syntax, imports, execution) |
| **Transport** | Abstraction for local/HTTP/SSH execution |
| **BeeLlama** | The LLM inference server on Triton |
| **Triton** | The dual-GPU machine (<LAN_IP>) |
| **Gitea** | Self-hosted Git service on Triton |
| **Streaming** | SSE event broadcasting system for real-time visibility |
| **CCBS** | Config Capability Benchmark Suite |
| **DAG** | Directed Acyclic Graph — task dependency structure |

---

## References

- `prd-autonomous-engine.md` — Original ACE PRD (superseded by this epic)
- `autonomous-engine-roadmap-v2.md` — Previous roadmap (superseded)
- `ENGINE-AUDIT.md` — Honest audit that identified the problems this epic solves
- `ace-watch-state-machine.md` — Previous implementation state machine (superseded)
- `docs/workflow-state-machine.md` — Previous workflow state machine (superseded)
- `docs/docker-architecture.md` — Docker architecture (unchanged)
- `prd-engine-control-layer.md` — Engine control layer PRD (already implemented)
