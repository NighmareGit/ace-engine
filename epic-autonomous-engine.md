# Epic: Autonomous Coding Engine

## Vision

Build a coding engine that accepts a PRD/epic/story/ticket and executes it end-to-end: **parse → decompose → generate → test → fix → commit → PR**.

The Autonomous Coding Engine (ACE) is a self-directed software development pipeline. Given a human-authored Product Requirements Document, ACE breaks the work into atomic coding tasks, generates implementation code for each task, validates correctness through automated testing, iteratively fixes failures, and delivers the result as a committed branch with a pull request — all without human intervention during execution.

---

## Success Criteria

1. **Parse** any markdown PRD into atomic coding tasks with clear inputs, outputs, and dependencies.
2. **Generate** valid Python or TypeScript code for each task, respecting project conventions detected from existing code.
3. **Run tests** and achieve >80% pass rate on the first generation pass.
4. **Auto-fix** failing tests up to 3 retries, escalating to human review if still failing.
5. **Commit** all changes to Gitea with descriptive, conventional-commit-format messages.
6. **Create PR** with a summary of changes, affected files, test results, and any unresolved issues.
7. **Total time** < 30 minutes for a 10-task PRD on typical hardware.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Autonomous Coding Engine                     │
│                                                                     │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐              │
│  │   PRD     │───▶│  Context     │───▶│   Code       │              │
│  │  Parser   │    │  Manager     │    │  Generator   │              │
│  └──────────┘    └──────────────┘    └──────┬───────┘              │
│       │                                       │                      │
│       ▼                                       ▼                      │
│  ┌──────────┐                        ┌──────────────┐              │
│  │  Task    │                        │  File Ops    │              │
│  │  Queue   │                        │  (Triton)    │              │
│  └────┬─────┘                        └──────┬───────┘              │
│       │                                       │                      │
│       ▼                                       ▼                      │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐              │
│  │ Quality  │◀───│   Test       │◀───│  Test        │              │
│  │  Gates   │    │  Runner     │    │  Executor    │              │
│  └────┬─────┘    └──────────────┘    └──────────────┘              │
│       │                                                             │
│       ▼                                                             │
│  ┌──────────┐                                                       │
│  │   Git    │───▶ Commit → Push → PR                                 │
│  │ Workflow │                                                       │
│  └──────────┘                                                       │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │              Error Recovery / Retry Loop                  │      │
│  │  (retries code gen → test up to 3x, then escalates)      │      │
│  └──────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Components

| # | Module | File | Purpose |
|---|--------|------|---------|
| 1 | **PRD Parser** | `prd_parser.py` | Parse markdown PRDs into structured task lists with metadata |
| 2 | **File Operations** | `file_ops.py` | Safe filesystem operations on the Triton workspace (read, write, list, diff) |
| 3 | **Test Runner** | `test_runner.py` | Execute pytest/unittest or Jest/Vitest and collect results |
| 4 | **Code Generator** | `code_generator.py` | LLM-powered code generation with context injection |
| 5 | **Context Manager** | `context_manager.py` | Maintain working memory: file contents, ASTs, dependencies, decisions |
| 6 | **Quality Gates** | `quality_gates.py` | Lint, type-check, complexity analysis, coverage thresholds |
| 7 | **Git Workflow** | `git_workflow.py` | Branch, commit, push, and create PR via Gitea API |
| 8 | **Task Queue** | `task_queue.py` | DAG-based task orchestration with dependency resolution |

---

## Milestones

### MVP — Tracer Bullets (4 loops)

**Goal:** Parse a PRD → generate 3 files → commit to Gitea.

- PRD Parser handles simple markdown with `- [ ] task` format
- Code Generator produces valid Python files
- File Operations writes to Triton workspace
- Git Workflow creates a commit with message
- No tests, no PR, no quality gates yet

**Exit criteria:** Run `python main.py prd.md` and see 3 new files committed.

---

### V1 — Full Pipeline with Error Recovery (8 loops)

**Goal:** Complete parse → generate → test → fix → commit → PR cycle.

- Task Queue resolves dependencies and orders execution
- Test Runner executes and parses results
- Error Recovery retries failing tests up to 3 times
- Quality Gates enforces minimum standards
- Git Workflow creates PR with summary

**Exit criteria:** A 10-task PRD produces a passing PR in <30 minutes.

---

### V2 — Multi-File Awareness + Code Review (12 loops)

**Goal:** Engine understands cross-file dependencies and performs self-review.

- Context Manager tracks imports, types, and interfaces across files
- Code Generator uses cross-file context for coherent generation
- Code Review Integration performs automated review before PR
- Multi-file refactoring support (renaming, interface changes)

**Exit criteria:** Engine handles a PRD that requires coordinated changes across 5+ files.

---

## Key Design Decisions

1. **Language:** Python 3.11+ for the engine itself; generates Python or TypeScript.
2. **LLM Backend:** Abstracted via a provider interface; default to local model via API.
3. **File Safety:** All writes go through `file_ops.py` which validates paths, creates backups, and logs changes.
4. **Idempotency:** Running the engine twice on the same PRD produces identical results (deterministic task IDs).
5. **Observability:** Every decision, generation, and test result is logged to a structured JSONL trace file.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM generates invalid syntax | Tasks fail | Syntax validation before commit; retry with error context |
| Infinite fix loops | Engine hangs | Hard cap of 3 retries per task; circuit breaker on repeated failures |
| PRD ambiguity | Wrong output | Parser flags ambiguous tasks; human review gate for critical ambiguity |
| Large PRDs overwhelm context | Quality degrades | Chunked processing; context window management in Context Manager |
| Gitea API downtime | Cannot commit | Local commit with push retry; queue pushes for later |

---

## Timeline

| Week | Milestone | Deliverable |
|------|-----------|-------------|
| 1-2 | MVP | Parse → generate → commit pipeline |
| 3-4 | V1 | Full pipeline with tests and error recovery |
| 5-6 | V2 | Multi-file awareness and code review |
| 7-8 | Polish | Documentation, performance tuning, edge cases |
