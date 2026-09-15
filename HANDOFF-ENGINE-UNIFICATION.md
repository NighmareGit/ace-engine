# Handoff: Engine Unification — Coder Harness

> **Status: HISTORICAL (2026-09-04).** The engine unification described here has since been
> implemented and iterated past this handoff (campaign 37/37, ace-followups 9/9, ace-judge-wiring
> 8/8). For current state and operation, read `ACE-RUNBOOK.md` first, then `AGENTS.md`.

> **Date:** 2026-09-04
> **Session:** Planning & Design Phase — Complete
> **Next Phase:** Implementation (Phase 1-4)
> **Author:** MiMo (metacognitive friction + fireplace + red-team + codebase-design applied)

---

## What This Session Accomplished

### 1. Engine Audit (Completed)
Performed a comprehensive audit of the coder-harness codebase. Found:
- **48,455 lines** Python across **52 files**
- **6 overlapping CLI entry points** for "run a coding task"
- **4 overlapping pipeline modules** (each implementing 60-80% of the same flow)
- **274 tests** that validate infrastructure, not output quality
- **0 real code generation tests** — the engine has never produced working code
- **2 critical weaknesses**: no code validation, no E2E test

Key finding: The benchmark platform and the autonomous coding engine were built in the same directory, sharing modules and CLIs, without ever being properly separated.

### 2. Architecture Design (Completed)
Designed a unified engine architecture using 4 skills:
- **Fireplace** — 8 perspectives on what "unified" means
- **Red-team** — 6 attack vectors on the proposed design
- **Grilling** — 5 design decisions that determine everything
- **Codebase-design** — Deep module design with seams, interfaces, and leverage

### 3. Planning Documents (Completed — 7 files, 1,717 lines)

| File | Lines | Purpose |
|------|-------|---------|
| `EPIC-ENGINE-UNIFICATION.md` | 616 | **Master plan** — state machine, architecture, dependency graph, phase plan, red team, decisions |
| `prd-engine-unification-01-core.md` | 168 | **PRD-01** — `engine/` package: 10 modules, 30 user stories, 8 decisions |
| `prd-engine-unification-02-pipeline.md` | 199 | **PRD-02** — Pipeline state machine + unified CLI: 30 user stories, 6 decisions |
| `prd-engine-unification-03-consolidation.md` | 189 | **PRD-03** — Module consolidation: 25→legacy, 9→bench, 15 retained |
| `prd-engine-unification-04-e2e.md` | 268 | **PRD-04** — E2E validation: test pyramid, MockTransport, 4-stage validator |
| `docs/ADR-001-engine-unification.md` | 121 | **ADR** — Architecture decision record |
| `MIGRATION.md` | 156 | **Migration guide** — old→new mapping for everything |

**No code was written.** The planning/design phase is complete.

---

## The Architecture (Summary)

### Current State (The Problem)
```
6 CLI entry points:    harness.py, enginectl.py, demo_e2e.py, work_engine.py,
                       work_engine_simple.py, orchestrate.py

4 pipeline modules:    task_queue.py, code_generator.py, demo_e2e.py, work_engine.py

2 telemetry systems:   telemetry_*.py (6 files) + streaming_*.py (6 files)

52 Python files, 48,455 lines, 0 real code generation tests
```

### Target State (The Solution)
```
1 CLI:                 cli.py

1 engine:              engine/ (10 modules)
  ├── engine.py        Engine class: run(), step(), status(), cancel()
  ├── pipeline.py      12-state state machine
  ├── context.py       Project context builder
  ├── generator.py     Code generation (prompt_compiler + BeeLlama)
  ├── validator.py     4-stage validation (AST → syntax → imports → execution)
  ├── tester.py        Pytest runner via transport
  ├── committer.py     Git + Gitea (branch, commit, push)
  ├── state.py         Run state persistence (SQLite)
  ├── events.py        Event bridge → streaming
  └── __init__.py      Config, constants

1 benchmark platform:  bench/ (9 modules, moved from top level)

15 retained modules:   transport.py, ssh_utils.py, prompt_compiler.py,
                       engine_service.py, engine_client.py, streaming_*.py (4),
                       event_schema.py, dsh_adapter.py, gpu_telemetry.py,
                       generate_readme.py, validate_e2e.py

25 legacy modules:     All superseded modules in legacy/ (importable, not used)
```

### The 12-State Pipeline State Machine
```
                            ┌──────────┐
                            │   IDLE   │
                            └────┬─────┘
                                 │ engine.run(prd, project, config)
                                 ▼
                            ┌──────────┐
                            │ PARSING  │  prd_parser.parse()
                            └────┬─────┘
                                 │
                                 ▼
                            ┌──────────┐
                  ┌────────│  QUEUED  │  task_queue.next()
                  │         └────┬─────┘
                  │              ▼
                  │         ┌──────────┐
                  │         │ CONTEXT  │  context.build()
                  │         └────┬─────┘
                  │              ▼
                  │         ┌──────────┐
                  │         │GENERATE  │  generator.generate()
                  │         └────┬─────┘
                  │              ▼
                  │         ┌──────────┐
                  │         │ VALIDATE │  validator.validate()
                  │         └────┬─────┘
                  │         PASS │ FAIL → retry < 3? → GENERATE
                  │              ▼
                  │         ┌──────────┐
                  │         │  TEST    │  tester.test()
                  │         └────┬─────┘
                  │         PASS │ FAIL → fix < 2? → GENERATE
                  │              ▼
                  │         ┌──────────┐
                  │         │  COMMIT  │  committer.commit()
                  │         └────┬─────┘
                  │              ▼
                  │         ┌──────────┐
                  └────────→│   NEXT   │  → QUEUED or DONE
                            └──────────┘
```

### Key Interfaces

```python
# The one entry point
class Engine:
    def run(prd_path, project_path, config, dry_run) -> RunResult
    def step(task_id) -> StepResult
    def status(run_id) -> RunStatus
    def cancel() -> bool

# Transport (unchanged)
from transport import get_transport
t = get_transport()  # auto-detects HTTP/Local/SSH

# Streaming (unchanged, opt-in)
from streaming_client import emit_event_fire_and_forget

# Prompts (unchanged)
from prompt_compiler import PromptCompiler, ManifestGenerator
```

---

## Critical Context for Next Session

### The #1 Priority
**Run `engine.run("test-prd.md", "/home/<user>/projects/ace-demo")` and see if it actually works.** This single test validates or invalidates 48,455 lines of infrastructure. Phase 1 of the plan builds exactly this.

### What's Proven vs What's Not

| Component | Status | Evidence |
|-----------|--------|----------|
| SSH transport | ✅ Proven | 201 tok/s live fire, 9/9 E2E |
| HTTP transport | ✅ Built, needs prod validation | `engine_service.py` (1,265 lines) |
| Streaming system | ✅ Real, tested | 2,909 lines of tests, all passing |
| Prompt compiler | ✅ Real, tested | `prompt_compiler.py` (1,019 lines) |
| CCBS | ✅ Running on Triton | Deployed at `/home/<user>/ccbs/` |
| Engine core | ❌ Never tested with real code | `demo_e2e.py` dry-run only |
| Code generation | ❌ Never validated | No AST validation on output |
| E2E pipeline | ❌ Never executed | No PRD → commit path verified |

### Runtime Context

| Property | Value |
|----------|-------|
| **Triton** | <LAN_IP>, user <user>, key-based SSH |
| **BeeLlama 3090** | Port 8080, Config-I (Qwen3.6-35B), 128K ctx, 197 tok/s |
| **BeeLlama 3070** | Port 8082, Qwen3.5-9B, 20K ctx |
| **Gitea** | Port 3000, token `<GITEA_TOKEN>` |
| **Test PRD** | `test-prd.md` — Health Check API Endpoint |
| **Test project** | `/home/<user>/projects/ace-demo` on Triton |
| **Python** | 3.12 on Triton, 3.10+ on nightmare |
| **Docker** | 29.6.1 on Triton |

### File Locations

| What | Where |
|------|-------|
| **Epic** | `/home/<user>/projects/ace-engine/EPIC-ENGINE-UNIFICATION.md` |
| **PRDs** | `/home/<user>/projects/ace-engine/prd-engine-unification-0{1,2,3,4}-*.md` |
| **ADR** | `/home/<user>/projects/ace-engine/docs/ADR-001-engine-unification.md` |
| **Migration** | `/home/<user>/projects/ace-engine/MIGRATION.md` |
| **Codebase** | `/home/<user>/projects/ace-engine/` |
| **Git repo** | `http://<LAN_IP>:3000/<user>/coder-harness` |

### Pending Jobs (from Earlier Sessions)
- **Run `demo_e2e.py` on a real PRD** — still the single most important next step
- **Add pytest to Triton** — `pip3 install pytest` for test execution
- **Consolidate pipeline modules** — now planned as Phase 3-4 of the epic
- **Add AST validation** — now planned as `engine/validator.py` in Phase 1

---

## What the Next Session Should Do

### Phase 1: Engine Kernel (4-6 hours)
Build the `engine/` package and `cli.py`. Prove it works with a real E2E test.

**Files to create:**
1. `engine/__init__.py` — Config dataclass, constants
2. `engine/engine.py` — Engine class with `run()`, `step()`, `status()`, `cancel()`
3. `engine/pipeline.py` — 12-state state machine
4. `engine/context.py` — Project context builder
5. `engine/generator.py` — Code generation (prompt_compiler + BeeLlama)
6. `engine/validator.py` — 4-stage validation (AST → syntax → imports → execution)
7. `engine/tester.py` — Pytest runner via transport
8. `engine/committer.py` — Git + Gitea
9. `engine/state.py` — Run state persistence (SQLite)
10. `engine/events.py` — Event bridge to streaming
11. `cli.py` — Single CLI entry point
12. `tests/e2e/test_prd_to_commit.py` — THE critical test

**Success criteria:**
```bash
python3 cli.py ace run --prd test-prd.md --project /home/<user>/projects/ace-demo --dry-run
# → Produces complete task plan

python3 cli.py ace run --prd test-prd.md --project /home/<user>/projects/ace-demo
# → Generates real code, validates, tests, commits to Gitea
```

### Phase 2: Integration (2-3 hours)
Wire streaming events, verify transport end-to-end, test with real BeeLlama.

### Phase 3: Hardening (2-3 hours)
Checkpoint/resume, timeouts, sandbox option, auto-revert.

### Phase 4: Consolidation (1-2 hours)
Move old modules to `legacy/`, update docs, clean up.

---

## Design Decisions Already Made

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | One engine package, not scattered modules | Eliminates 6 entry points, 4 pipelines |
| D2 | State machine with 12 states | Each state is testable, resumable, observable |
| D3 | Validator is multi-stage (AST → syntax → import → exec) | Catches progressively more issues |
| D4 | Streaming is opt-in, not hard dependency | Engine works standalone |
| D5 | Legacy/ not delete | Safety net during transition |
| D6 | CLI is monolithic, not plugin-based | 52 files is enough |
| D7 | Sandbox is opt-in (`--sandbox`) | Direct execution is faster, simpler |
| D8 | Benchmark platform moves to bench/ | Separates concerns |
| D9 | Transport detected once at startup | Eliminates per-call overhead |
| D10 | Retry with escalation (3 gen, 2 fix, 2 commit) | Intelligent retry, not blind |

---

## Skills Available for Next Session

The following skills are relevant and should be used:

| Skill | When to Use |
|-------|-------------|
| `meta-cognitive-ralph-loop` | For the implementation phase — autonomous coding with adversarial testing |
| `metacognitive-friction` | For any design decisions still open |
| `red-team` | To attack the implementation after Phase 1 |
| `grilling` | To stress-test interfaces before building |
| `codebase-design` | To verify module depth and seam placement |
| `tdd` | For the engine modules — test-driven development |
| `check-work` | For verification after each phase |
| `handoff` | For session transitions |

---

## What NOT to Do

1. **Don't start coding before reviewing the epic.** The architecture is designed — follow it.
2. **Don't skip the E2E test.** It's the single most important artifact.
3. **Don't delete legacy modules.** Move them. Deletion is Phase 4+.
4. **Don't make streaming a hard dependency.** It's opt-in.
5. **Don't add more CLI entry points.** `cli.py` is the one.
6. **Don't merge the SQLite databases.** Separate DBs with cross-references.
7. **Don't build Docker sandbox in Phase 1.** Direct execution first.

---

*Handoff complete. The next session should start by reading `EPIC-ENGINE-UNIFICATION.md` and then begin Phase 1 implementation.*

---

## Suggested Skills for Next Session

- `meta-cognitive-ralph-loop` — autonomous coding with adversarial testing
- `tdd` — test-driven development for engine modules
- `check-work` — verification after each phase
- `metacognitive-friction` — for any remaining design decisions
- `red-team` — attack the implementation after Phase 1
