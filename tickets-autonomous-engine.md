# Tickets: Autonomous Coding Engine (ACE)

12 tickets organized as tracer bullets, from foundational infrastructure to full pipeline.

---

## ACE-01: PRD Parser + Task Decomposer

**Title:** Build a markdown PRD parser that produces a structured task list

**What to build:**
A Python module (`prd_parser.py`) that reads a markdown PRD and produces a `PRD` dataclass containing a list of `ParsedTask` objects. The parser must handle headings (H1-H4) as hierarchy, checkbox lists (`- [ ]`) as tasks, nested bullets as sub-details, and extract acceptance criteria. Task IDs must be deterministic (content-hashed). The parser should be fast (<100ms for 20 tasks) and gracefully handle malformed input.

**Acceptance Criteria:**
- [ ] Parses a 20-task markdown PRD into structured `ParsedTask` objects in <100ms
- [ ] Task IDs are deterministic: same input always produces same IDs
- [ ] Handles malformed sections without crashing (logs warnings, skips unparseable parts)
- [ ] Extracts acceptance criteria from nested checkbox lists
- [ ] Preserves raw markdown in the output for reference

**Blocked by:** None

---

## ACE-02: File Operations on Triton

**Title:** Safe, audited filesystem operations for the Triton workspace

**What to build:**
A Python module (`file_ops.py`) that provides `read`, `write`, `list_files`, `diff`, `backup`, `restore`, `exists`, and `delete` operations. All writes must be atomic (write to temp file, then rename). Backups must be created before every overwrite. Path traversal (`../`) must be blocked with a `SecurityError`. All operations must be logged. The module should handle files up to 100MB without loading entire contents into memory.

**Acceptance Criteria:**
- [ ] All writes are atomic (no partial writes on crash)
- [ ] Backups created automatically before every overwrite
- [ ] Path traversal attempts (`../`) raise `SecurityError` and are logged
- [ ] `diff()` produces valid unified diff output comparing current vs backup
- [ ] Handles 100MB files via streaming (no OOM)

**Blocked by:** None

---

## ACE-03: Test Execution Engine

**Title:** Run test suites and return structured results

**What to build:**
A Python module (`test_runner.py`) that detects the test framework (pytest, unittest, Jest, Vitest) from project structure, executes tests, and returns a `TestResult` dataclass with pass/fail/skip/error counts, per-test details, raw output, and coverage data. Must enforce per-test timeouts (default 30s) and kill hung processes. Must parse pytest JSON output and Jest JSON output.

**Acceptance Criteria:**
- [ ] Auto-detects framework from project files (`conftest.py`, `jest.config.*`)
- [ ] Parses pytest JSON output into structured `TestResult`
- [ ] Enforces per-test timeout and kills processes exceeding it
- [ ] Returns both structured results and raw stdout/stderr
- [ ] Handles framework crashes gracefully (reports as `error` status)

**Blocked by:** None

---

## ACE-04: Code Generation Loop

**Title:** LLM-powered code generation with context injection and retry

**What to build:**
A Python module (`code_generator.py`) that takes a `ParsedTask` and `ContextSnapshot`, calls an LLM to generate implementation code, and returns `GeneratedCode` with file contents, explanations, assumptions, and warnings. Must include an LLM provider abstraction interface. Must validate generated syntax before returning (strip markdown fences, check for valid Python/TypeScript). Must accept `RetryContext` with previous errors for improved retry generation. Must handle API timeouts (retry 2x), rate limiting (back off), and context window overflow (truncate).

**Acceptance Criteria:**
- [ ] Generates syntactically valid code >90% of the time on first attempt
- [ ] LLM provider abstraction supports OpenAI-compatible, Anthropic, and local endpoints
- [ ] Retry context improves success rate (measurable via test suite)
- [ ] Handles API timeouts with exponential backoff
- [ ] Strips markdown fences from LLM output before validation

**Blocked by:** ACE-01 (needs task format), ACE-02 (needs file operations)

---

## ACE-05: Error Recovery

**Title:** Automatic retry and escalation for failing tasks

**What to build:**
Integration logic that connects the Code Generator, Test Runner, and Quality Gates into a retry loop. When a task fails (test failures or quality gate violations), the system must: (1) collect error details, (2) feed them back to the Code Generator as retry context, (3) re-run tests, (4) repeat up to 3 times, (5) mark as failed and log escalation if still failing. Must implement a circuit breaker that detects repeated identical failures and stops early.

**Acceptance Criteria:**
- [ ] Retry loop attempts up to 3 fixes per task
- [ ] Each retry includes progressively more context (error messages, stack traces, related files)
- [ ] Circuit breaker stops retrying after 3 identical consecutive failures
- [ ] Failed tasks are marked clearly and do not block unrelated tasks
- [ ] Full retry history is logged to the trace file

**Blocked by:** ACE-04 (needs code generator)

---

## ACE-06: Context Memory

**Title:** Project state tracking for informed code generation

**What to build:**
A Python module (`context_manager.py`) that maintains a live snapshot of the project: file tree, imports, exports, type definitions, dependency graph, and recent changes. Must provide `snapshot()` for full capture and `update()` for incremental updates. Must implement `get_relevant_files()` that returns files most related to a given task (using import graph and naming similarity). Must produce a `summarize()` text output that fits within an LLM token budget.

**Acceptance Criteria:**
- [ ] Full snapshot of 500-file project completes in <5 seconds
- [ ] Incremental update after single file change in <50ms
- [ ] `get_relevant_files()` returns contextually appropriate results (validated manually)
- [ ] Import graph correctly detects circular dependencies
- [ ] `summarize()` output respects token budget (default 4000 tokens)

**Blocked by:** ACE-02 (needs file operations)

---

## ACE-07: Lint + Quality Gates

**Title:** Automated code quality enforcement before commit

**What to build:**
A Python module (`quality_gates.py)` that runs linting (ruff), type checking (mypy/tsc), formatting checks, complexity analysis, and coverage thresholds. Must run all checks in parallel and return a `QualityResult` with per-check results, structured issue lists (file, line, column, severity, message), and a pass/fail summary. Must identify auto-fixable issues. Must gracefully skip checks when tools are not installed.

**Acceptance Criteria:**
- [ ] Runs lint, type check, format, and complexity checks in parallel
- [ ] Returns structured issue list with file/line/column/severity
- [ ] Distinguishes errors (blocking) from warnings (non-blocking)
- [ ] Identifies which issues are auto-fixable
- [ ] Total runtime <30 seconds for a 20-file project

**Blocked by:** ACE-04 (needs generated code to check)

---

## ACE-08: Git Workflow

**Title:** Branch, commit, push, and create PR via Gitea

**What to build:**
A Python module (`git_workflow.py`) that manages the full git lifecycle: create feature branch (timestamp-named), stage and commit changes (conventional commit format), push to remote, and create a PR via Gitea API. Must handle merge conflicts (report, don't auto-resolve), nothing-to-commit cases, and push rejections. PR body must include summary, changed files list, test results, and quality gate results.

**Acceptance Criteria:**
- [ ] Creates branch from main with format `ace/feat-YYYYMMDD-HHMMSS`
- [ ] Commits with conventional format (`feat:`, `fix:`, `chore:`)
- [ ] PR body includes summary, file list, test results, and quality gate results
- [ ] Handles merge conflicts gracefully (returns conflict details)
- [ ] All operations are idempotent where possible (re-push doesn't fail)

**Blocked by:** ACE-02 (needs file operations for staging)

---

## ACE-09: Code Review Integration

**Title:** Automated self-review before PR creation

**What to build:**
A review module that takes the generated code, test results, and quality gate results, and produces a structured review. Must check: (1) does generated code match task acceptance criteria, (2) are there any TODO/FIXME/HACK comments, (3) are error paths handled, (4) are imports clean (no unused imports). Must produce a review summary that gets included in the PR body. Must flag high-risk changes for human review.

**Acceptance Criteria:**
- [ ] Review covers acceptance criteria alignment, error handling, and code cleanliness
- [ ] Flags TODO/FIXME/HACK comments with file and line references
- [ ] Detects unused imports and flags them
- [ ] Produces a risk score (low/medium/high) based on change size and complexity
- [ ] Review summary is included in PR body automatically

**Blocked by:** ACE-04 (needs generated code), ACE-07 (needs quality results)

---

## ACE-10: Task Queue + Orchestration

**Title:** DAG-based task orchestration with dependency resolution

**What to build:**
A Python module (`task_queue.py`) that takes the parsed task list, builds a dependency DAG, detects circular dependencies, and executes tasks in topological order with configurable parallelism. Must track task status (pending/running/completed/failed/skipped), manage retries, and report progress. Must calculate the critical path and provide an ASCII visualization of the DAG. Must be thread-safe for parallel execution.

**Acceptance Criteria:**
- [ ] Resolves DAG with up to 100 nodes in <10ms
- [ ] Detects circular dependencies at initialization and raises clear error
- [ ] Executes independent tasks in parallel (configurable concurrency)
- [ ] Provides real-time progress state (total, completed, failed, running, pending)
- [ ] ASCII DAG visualization renders correctly in terminal

**Blocked by:** ACE-01 (needs parsed tasks), ACE-04 (needs code generator), ACE-06 (needs context)

---

## ACE-11: Multi-File Awareness

**Title:** Cross-file dependency tracking and coherent generation

**What to build:**
Extend the Context Manager and Code Generator to handle cross-file dependencies. When generating code for a task that modifies multiple files, the system must: (1) load all relevant file contents, (2) understand import relationships, (3) generate consistent interfaces across files, (4) ensure type compatibility between modules. Must handle refactoring scenarios (renaming a function updates all callers).

**Acceptance Criteria:**
- [ ] Generates consistent interfaces when task spans 3+ files
- [ ] Import graph is used to identify all affected files
- [ ] Refactoring scenarios (rename, interface change) update all dependents
- [ ] Type compatibility is maintained across generated modules
- [ ] Multi-file generation completes within context window limits

**Blocked by:** ACE-06 (needs context manager)

---

## ACE-12: End-to-End Demo

**Title:** Complete pipeline demonstration from PRD to PR

**What to build:**
A `main.py` entry point that orchestrates the full pipeline: parse PRD → build context → queue tasks → generate code → run tests → retry failures → quality gates → commit → create PR. Must include a sample PRD (`sample-prd.md`) that exercises all features. Must produce a summary report with timing, task results, and PR link. Must be runnable with a single command: `python main.py sample-prd.md`.

**Acceptance Criteria:**
- [ ] Single command `python main.py sample-prd.md` runs the full pipeline
- [ ] Sample PRD includes 8-12 tasks with dependencies
- [ ] Produces a summary report with per-task results and total timing
- [ ] Creates a real PR on Gitea with correct content
- [ ] Total runtime <30 minutes for the sample PRD

**Blocked by:** ACE-10 (needs orchestration)

---

## Dependency Graph

```
ACE-01 ──┬──▶ ACE-04 ──┬──▶ ACE-05
         │              ├──▶ ACE-07 ──▶ ACE-09
ACE-02 ──┼──▶ ACE-06 ──┼──▶ ACE-09
         │              └──▶ ACE-11
         └──▶ ACE-08        │
                            ▼
         ACE-01 ──┬──▶ ACE-10 ──▶ ACE-12
         ACE-04 ──┤
         ACE-06 ──┘
```

## Execution Order (Recommended)

| Phase | Tickets | Parallel? |
|-------|---------|-----------|
| 1 (Foundation) | ACE-01, ACE-02, ACE-03 | Yes — all independent |
| 2 (Core) | ACE-04, ACE-06, ACE-08 | Partially — ACE-08 only needs ACE-02 |
| 3 (Quality) | ACE-05, ACE-07 | Yes — both depend on ACE-04 |
| 4 (Integration) | ACE-09, ACE-10, ACE-11 | Partially — ACE-11 independent of ACE-09/10 |
| 5 (Demo) | ACE-12 | No — needs everything |
