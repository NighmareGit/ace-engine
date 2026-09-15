# Autonomous Coding Engine — Roadmap v2

> **v2 Changes:** Consolidated 12 loops → 8 loops. Added MVP-first approach.
> Filled critical gaps: prompt engineering, response parsing, sandbox decision,
> dependency resolution, cost tracking. Measurable success criteria per loop.

## Goal

Build a minimal autonomous coding engine that can:
1. Accept a PRD (Markdown)
2. Decompose into atomic tasks with dependencies
3. Generate code for each task using local LLM inference
4. Test the code automatically
5. Fix failures in a retry loop
6. Commit and push to Gitea with a PR

## Current State (23+ files, 13,500+ lines)

### Already Built — Reusable

| Component | File | Lines | Status | Reuse in Engine |
|-----------|------|-------|--------|-----------------|
| SSH transport | `ssh_utils.py` | 261 | ✅ Solid | Direct — all remote ops go through this |
| BeeLlama inference | `ssh_utils.py` | — | ✅ 195 tok/s | Direct — `curl_beellama()` |
| Gitea API | `gitea_utils.py` | 663 | ✅ Complete | Direct — create files, branches, push |
| Docker sandbox | `sandbox_manager.py` | 588 | ✅ Lifecycle | Direct — create/exec/collect/destroy |
| Work engine | `work_engine.py` | 1,440 | ✅ 7-step pipeline | Extend — add code extraction + retry |
| Benchmark judge | `judge.py` | 417 | ✅ Code exec test | Extend — reuse `_execute_code_test()` |
| Telemetry | `schema_unified.py`, `telemetry_collector.py` | 448+ | ✅ SQLite schema | Extend — add cost tracking tables |
| Preflight | `preflight.py` | 174 | ✅ Health check | Direct — gate before engine start |
| Checkpoint | `checkpoint.py` | 162 | ✅ Crash recovery | Direct — save/resume task progress |

### Not Built — Must Create

| Component | Complexity | Est. Lines | Priority |
|-----------|-----------|------------|----------|
| **Prompt templates** (system + user + output format) | Medium | ~300 | **P0** |
| **Response parser** (extract code from LLM output) | Low | ~150 | **P0** |
| **PRD parser + task decomposer** | Medium | ~400 | **P0** |
| **Code generation loop** (generate → test → fix → retry) | High | ~500 | **P0** |
| **Task dependency resolver** | Medium | ~200 | **P1** |
| **Cost tracker** (tokens, budget, per-task) | Low | ~150 | **P1** |
| **Model router** (which model per task type) | Medium | ~200 | **P2** |
| **Rollback mechanism** (git stash/revert on failure) | Medium | ~200 | **P2** |

---

## Architecture Decision: Sandbox vs Direct SSH

### Decision: **Hybrid — Sandbox for code gen, Direct SSH for infrastructure**

| Operation | Method | Rationale |
|-----------|--------|-----------|
| Inference (BeeLlama calls) | Direct SSH → `curl` on Triton | No sandbox needed; BeeLlama is host-networked |
| Code generation (write files) | Docker sandbox | Isolation — broken code can't corrupt project |
| Test execution (pytest) | Docker sandbox | Safety — arbitrary code from LLM needs containment |
| Git operations (commit/push) | Direct SSH on Triton | Gitea is localhost; needs git credentials |
| Model swap (docker compose) | Direct SSH on Triton | Infrastructure operation, not code work |
| File read (context gathering) | Direct SSH | Read-only, safe |

**Why not all-sandbox?** Creating a Docker container takes ~15s. For a 20-task PRD, that's 5 minutes of overhead. Only sandbox the dangerous parts (code write + test execution).

**Why not all-direct-SSH?** An LLM might generate `rm -rf /` or overwrite critical files. Sandbox provides containment for code generation and test execution.

---

## Prompt Engineering Strategy

### The Problem

The existing prompts (`coding-easy.txt`, `coding-hard.txt`) are raw task descriptions with no system prompt, no output format specification, and no chain-of-thought guidance. Code generation quality depends entirely on prompt design.

### Solution: Three-Layer Prompt Architecture

```
┌─────────────────────────────────────────────┐
│  Layer 1: System Prompt (per task type)      │
│  - Role definition                           │
│  - Output format (```python blocks)          │
│  - Constraints (no imports beyond stdlib)    │
├─────────────────────────────────────────────┤
│  Layer 2: Context Injection (per task)       │
│  - Existing file contents (relevant parts)   │
│  - Import graph / dependencies               │
│  - Previous task outputs (if chained)        │
├─────────────────────────────────────────────┤
│  Layer 3: Task Prompt (from PRD)             │
│  - What to build                             │
│  - Acceptance criteria                       │
│  - File target path                          │
└─────────────────────────────────────────────┘
```

### Prompt Templates (to create in `prompts/`)

| Template | File | Purpose |
|----------|------|---------|
| `system-coder.txt` | `prompts/` | System prompt for code generation tasks |
| `system-reviewer.txt` | `prompts/` | System prompt for code review / quality check |
| `system-decomposer.txt` | `prompts/` | System prompt for PRD → task decomposition |
| `system-fixer.txt` | `prompts/` | System prompt for error-fixing retries |
| `output-format.txt` | `prompts/` | Mandatory output format spec (JSON + code blocks) |

### System Prompt Example (`system-coder.txt`)

```
You are a senior Python developer. Generate code that:
1. Is production-ready with type hints and docstrings
2. Handles edge cases (empty input, None, wrong types)
3. Includes error handling with specific exceptions
4. Follows PEP 8 style
5. Uses only Python standard library unless explicitly allowed

OUTPUT FORMAT — you MUST respond in exactly this structure:

<reasoning>
Brief analysis of the task and approach (2-3 sentences).
</reasoning>

<code file="path/to/file.py">
```python
# Your complete, runnable code here
```
</code>

<tests file="tests/test_file.py">
```python
# Test cases for the code above
```
</tests>

<explanation>
One paragraph explaining key design decisions.
</explanation>
```

---

## Response Parsing Strategy

### The Problem

LLM responses are free-form text. We need to reliably extract:
1. **Code blocks** with their target file paths
2. **Reasoning** for logging/debugging
3. **Test code** for execution
4. **Structured metadata** (explanation, confidence)

### Solution: XML-Tag-Based Parser

The system prompt enforces XML tags (`<code file="...">`, `<tests file="...">`). The parser extracts them:

```python
# response_parser.py — Concept

import re
from dataclasses import dataclass

@dataclass
class ParsedResponse:
    reasoning: str
    code_blocks: list[dict]   # [{path, content, language}]
    test_blocks: list[dict]   # [{path, content}]
    explanation: str
    raw: str

def parse_llm_response(response: str) -> ParsedResponse:
    """Extract structured content from LLM response."""

    # Primary: XML-tag extraction
    reasoning = _extract_tag(response, "reasoning")
    code_blocks = _extract_code_tags(response)      # <code file="...">
    test_blocks = _extract_test_tags(response)      # <tests file="...">
    explanation = _extract_tag(response, "explanation")

    # Fallback: markdown code block extraction (if no XML tags)
    if not code_blocks:
        code_blocks = _extract_markdown_code_blocks(response)

    # Fallback: entire response as single code block
    if not code_blocks and not test_blocks:
        code_blocks = [{"path": "main.py", "content": response, "language": "python"}]

    return ParsedResponse(
        reasoning=reasoning or "",
        code_blocks=code_blocks,
        test_blocks=test_blocks,
        explanation=explanation or "",
        raw=response,
    )
```

### Fallback Chain

```
1. XML tags → extract file paths + code      (best case)
2. Markdown ``` blocks → extract code         (common case)
3. Entire response as code                    (worst case)
4. Parse failure → log error, skip task       (give up gracefully)
```

---

## Revised Loop Plan: 8 Loops in 3 Phases

### Why 8, Not 12?

The original 12-loop plan had redundancy:
- ~~Loop 4 (code gen) + Loop 5 (error recovery)~~ → **One loop** (retry is intrinsic to generation)
- ~~Loop 7 (lint) + Loop 9 (code review)~~ → **One loop** (quality gate = lint + review + test)
- ~~Loop 10 (task queue) + Loop 11 (multi-file)~~ → **One loop** (orchestration handles both)

### MVP-First Approach

**Phase 1 (Loops 1-4) IS the MVP.** If Phase 1 works, we have a functional autonomous engine. Phase 2 and 3 add robustness and scale.

```
Phase 1: MVP ─── Loops 1-4 ─── "Parse PRD → Generate 3 files → Test 1 → Commit"
Phase 2: Harden ── Loops 5-6 ── "Fix errors, run all tests, quality gates"
Phase 3: Scale ─── Loops 7-8 ── "Dependency resolution, cost tracking, model routing"
```

---

### Phase 1: MVP (Loops 1-4)

> **Goal:** Parse a simple PRD → generate 3 files → run 1 test → commit to Gitea.
> This proves the concept end-to-end.

#### Loop 1: Prompt Templates + Response Parser

**Files:** `prompts/system-coder.txt`, `prompts/system-fixer.txt`, `response_parser.py`

**Deliverables:**
- [ ] 4 system prompt templates (coder, reviewer, decomposer, fixer)
- [ ] `response_parser.py` with XML-tag extraction + markdown fallback
- [ ] Unit tests for parser (5+ test cases: clean XML, markdown only, malformed, empty, multi-block)

**Success criteria:**
- Parser extracts code from 90%+ of sample LLM responses (test against 20 stored responses)
- Zero crashes on malformed input (graceful degradation)

**Implementation notes:**
- Reuse `judge.py`'s `_execute_code_test()` regex as a baseline, but extend with XML-tag extraction
- Store prompt templates in `prompts/` directory alongside existing `coding-easy.txt` etc.
- Parser should be stateless — pure function, easy to test

#### Loop 2: PRD Parser + Task Decomposer

**Files:** `prd_parser.py`

**Deliverables:**
- [ ] `prd_parser.py` — reads Markdown PRD, outputs JSON task list
- [ ] Each task has: `id`, `title`, `description`, `file_target`, `dependencies`, `priority`
- [ ] Dependency graph extraction (task B depends on task A)

**Input:** Markdown PRD (H1 = project, H2 = feature, H3 = task, bullet = requirement)
**Output:**
```json
{
  "project": "my-api",
  "tasks": [
    {
      "id": "T01",
      "title": "Create database models",
      "description": "Define SQLAlchemy models for users and posts...",
      "file_target": "models.py",
      "dependencies": [],
      "priority": 1
    },
    {
      "id": "T02",
      "title": "Create API routes",
      "description": "FastAPI routes for CRUD operations...",
      "file_target": "routes.py",
      "dependencies": ["T01"],
      "priority": 2
    }
  ]
}
```

**Success criteria:**
- Parses a 50-line PRD into 3-8 atomic tasks
- Correctly extracts dependency relationships (not just sequential)
- Handles malformed PRDs gracefully (missing sections → warnings, not crashes)

#### Loop 3: Code Generation Loop

**Files:** `code_generator.py`

**Deliverables:**
- [ ] `code_generator.py` — takes a task + context, calls BeeLlama, extracts code, writes files
- [ ] Integration with `response_parser.py` for code extraction
- [ ] Integration with `sandbox_manager.py` for file writes in sandbox
- [ ] Integration with `judge.py`'s `_execute_code_test()` for immediate testing
- [ ] Simple retry loop: generate → test → fix (max 3 attempts)

**Pipeline per task:**
```
1. Read relevant existing files (context)
2. Build prompt: system template + context + task description
3. Call BeeLlama via SSH (curl)
4. Parse response → extract code blocks
5. Write code to sandbox
6. Run tests in sandbox
7. If tests pass → commit file
8. If tests fail → build fix prompt (error + original code) → retry (max 3x)
9. If all retries fail → log failure, move to next task
```

**Success criteria:**
- Generates syntactically valid Python code for 80%+ of simple tasks
- Test pass rate > 60% on first attempt, > 80% after 3 retries
- Writes correct file paths (matches `file_target` from PRD)

#### Loop 4: Git Workflow + Gitea Push

**Files:** `git_workflow.py`

**Deliverables:**
- [ ] `git_workflow.py` — branch → commit → push → create PR on Gitea
- [ ] Reuse `gitea_utils.py` for all Gitea API calls
- [ ] Descriptive commit messages (auto-generated from task title)
- [ ] PR description with task summary + files changed

**Pipeline:**
```
1. Create feature branch: auto/prd-{name}-{timestamp}
2. For each completed task:
   a. Stage the generated file(s)
   b. Commit with message: "feat(T01): {task_title}"
3. Push branch to Gitea
4. Create PR: title + body (task list, files changed, test results)
5. Switch back to main branch
```

**Success criteria:**
- Branch is created and pushed to Gitea
- Commit messages are descriptive (not "update")
- PR is created with a readable description
- No force-pushes or rebase on main

---

### Phase 2: Harden (Loops 5-6)

> **Goal:** Make the engine reliable. Fix errors, run all tests, add quality gates.

#### Loop 5: Error Recovery + Quality Gates

**Files:** `code_generator.py` (extend), `quality_gates.py` (new)

**Deliverables:**
- [ ] Enhanced retry logic: parse error messages → generate targeted fix prompts
- [ ] `quality_gates.py` — lint (flake8), type check (mypy), code review (judge model)
- [ ] Rollback on failure: `git stash` or revert if generated code breaks existing code
- [ ] Quality gate pipeline: lint → type check → test → review → commit

**Error recovery strategy:**
```
Test failure → extract traceback → build fix prompt:
  "The following code failed with this error:
   [error output]
   Fix the code. Only output the corrected file."

Retry up to 3x. After 3 failures:
  → Log the failure
  → Skip the task
  → Continue with next task
  → Report failures in PR description
```

**Quality gate pipeline (before commit):**
```
1. flake8 — style/lint errors (auto-fix obvious ones)
2. mypy — type errors (warn only, don't block)
3. pytest — all tests pass
4. Judge model — code review (score > 6/10 to proceed)
5. If all pass → commit. If any fail → retry or skip.
```

**Rollback mechanism:**
```python
# Before writing generated code:
git stash  # Save current state

# After test failure:
git stash pop  # Restore previous state

# If all retries fail:
git checkout -- .  # Hard revert to last good commit
```

**Success criteria:**
- Error recovery fixes 50%+ of test failures on first retry
- Quality gate catches 80%+ of lint/style issues before commit
- Rollback works correctly (no corrupted state after failure)

#### Loop 6: Multi-File Context + Orchestration

**Files:** `context_manager.py` (new), `task_queue.py` (new)

**Deliverables:**
- [ ] `context_manager.py` — tracks files, modifications, imports, class hierarchies
- [ ] `task_queue.py` — executes tasks in dependency order, parallel where possible
- [ ] Import-aware context: when generating `routes.py`, include `models.py` in prompt
- [ ] Parallel execution: independent tasks run in parallel (different sandboxes)

**Context management:**
```
For each task:
  1. Read file_target (if exists) — current state
  2. Read dependencies' files — already generated code
  3. Read project structure — file tree, imports
  4. Build context window (max 4K tokens)
  5. Inject into prompt
```

**Task queue with dependencies:**
```
Dependency graph:
  T01 (models.py) → T02 (routes.py)
  T01 (models.py) → T03 (tests.py)
  T02 + T03 → T04 (integration)

Execution order:
  Round 1: T01 (no deps)
  Round 2: T02, T03 (parallel — both depend only on T01)
  Round 3: T04 (depends on T02 + T03)
```

**Success criteria:**
- Context injection improves code quality (measurable: +10% test pass rate)
- Parallel execution works for independent tasks (2+ tasks concurrently)
- Dependency order is correct (no task runs before its deps complete)

---

### Phase 3: Scale (Loops 7-8)

> **Goal:** Cost awareness, model selection, production readiness.

#### Loop 7: Cost Tracking + Budget Management

**Files:** `cost_tracker.py` (new), `schema_unified.py` (extend)

**Deliverables:**
- [ ] `cost_tracker.py` — tracks tokens, time, and estimated cost per task/session
- [ ] SQLite table: `engine_costs` (task_id, tokens_in, tokens_out, elapsed_s, estimated_cost)
- [ ] Budget enforcement: stop engine if total cost exceeds budget
- [ ] Dashboard integration: cost per task, total spend, tokens per model

**Cost tracking schema:**
```sql
CREATE TABLE engine_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    task_id TEXT,
    model_config TEXT,
    tokens_prompt INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    tokens_total INTEGER DEFAULT 0,
    elapsed_seconds REAL DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    status TEXT CHECK(status IN ('success', 'failed', 'skipped')),
    created_at TEXT DEFAULT (datetime('now'))
);
```

**Budget enforcement:**
```python
# Before each inference call:
if cost_tracker.total_cost() > budget_limit:
    raise BudgetExceeded(f"Spent ${cost_tracker.total_cost():.2f} / ${budget_limit:.2f}")
```

**Success criteria:**
- Accurate token counting (matches BeeLlama `usage.total_tokens`)
- Cost estimates are within 20% of actual (if using priced API; local inference = $0)
- Budget stop works correctly (halts engine, generates partial PR)

#### Loop 8: Model Router + Production Hardening

**Files:** `model_router.py` (new), `engine.py` (new — main entry point)

**Deliverables:**
- [ ] `model_router.py` — selects best model per task type based on benchmarks
- [ ] `engine.py` — unified CLI entry: `python3 engine.py run --prd prd.md --config 3090-qwen36-35b`
- [ ] Full end-to-end demo on a real PRD
- [ ] Documentation + README update

**Model routing logic:**
```python
# Based on benchmark results from pilot.py / orchestrate.py
TASK_MODEL_MAP = {
    "decomposition": "3090-qwen36-35b",    # Best at planning (orchestrator role)
    "code_generation": "3090-qwen36-35b",  # Best at coding (coder role)
    "code_review": "3070-qwen35-9b",       # Good enough, faster
    "error_fixing": "3090-qwen36-35b",     # Needs strong reasoning
}
```

**Production CLI:**
```bash
# Full pipeline
python3 engine.py run --prd feature-prd.md --config 3090-qwen36-35b --budget 10.00

# Decompose only
python3 engine.py decompose --prd feature-prd.md --output tasks.json

# Generate from tasks
python3 engine.py generate --tasks tasks.json --config 3090-qwen36-35b

# Status / resume
python3 engine.py status --session work-20250101-120000
python3 engine.py resume --session work-20250101-120000
```

**Success criteria:**
- Full pipeline runs on a real 50-line PRD without manual intervention
- End-to-end time < 30 minutes for a 5-task PRD
- Generated PR is reviewable on Gitea with all files + tests

---

## Cost Tracking

### Token Budget (at 195 tok/s on 3090)

| Phase | Inference Calls | Tokens/Call | Total Tokens | Time |
|-------|----------------|-------------|--------------|------|
| Decomposition (1 call) | 1 | ~2K in, ~1K out | ~3K | ~15s |
| Code Generation (N tasks × 1.5 avg) | ~8 | ~4K in, ~2K out | ~48K | ~4min |
| Error Recovery (avg 0.5 retry/task) | ~4 | ~6K in, ~2K out | ~32K | ~3min |
| Quality Gates (judge per task) | ~5 | ~3K in, ~0.5K out | ~17.5K | ~1.5min |
| **Total (5-task PRD)** | **~18** | — | **~100K** | **~9min** |

### Cost at API Rates (for comparison)

| Provider | Input $/1M | Output $/1M | Estimated Cost |
|----------|-----------|-------------|----------------|
| Local (BeeLlama) | $0 | $0 | **$0.00** |
| OpenAI GPT-4 | $30 | $60 | ~$3.90 |
| Anthropic Claude | $15 | $75 | ~$3.23 |
| DeepSeek API | $0.14 | $0.28 | ~$0.05 |

**Key insight:** Local inference is free. Cost tracking is still useful for measuring efficiency and comparing against cloud alternatives.

---

## Dependency Resolution

### Problem

Tasks in a PRD have dependencies. Task B (API routes) can't be generated until Task A (database models) exists.

### Solution: Topological Sort + Parallel Rounds

```python
def resolve_dependencies(tasks: list[dict]) -> list[list[dict]]:
    """Return execution rounds (tasks in each round can run in parallel)."""

    # Build adjacency list
    graph = {t["id"]: t["dependencies"] for t in tasks}

    # Kahn's algorithm for topological sort
    rounds = []
    remaining = set(graph.keys())

    while remaining:
        # Find tasks with all deps satisfied
        ready = [tid for tid in remaining
                 if all(dep not in remaining for dep in graph[tid])]

        if not ready:
            raise CircularDependency(f"Circular deps in: {remaining}")

        rounds.append(ready)
        remaining -= set(ready)

    return rounds
```

### Example

```
PRD: "Build a REST API"
Tasks:
  T01: models.py       (deps: [])
  T02: database.py     (deps: [T01])
  T03: routes.py       (deps: [T01])
  T04: auth.py         (deps: [T01])
  T05: tests.py        (deps: [T02, T03, T04])
  T06: main.py         (deps: [T03, T04])

Execution rounds:
  Round 1: [T01]                          → sequential (1 sandbox)
  Round 2: [T02, T03, T04]                → parallel (3 sandboxes)
  Round 3: [T05, T06]                     → parallel (2 sandboxes)

Total: 3 rounds, 6 tasks, max 3 concurrent
```

---

## Concurrency Model

### Decision: **Task-level parallelism, not inference-level**

- Each task gets its own Docker sandbox
- Multiple sandboxes can run in parallel (limited by GPU — 2 concurrent inferences max on dual-GPU Triton)
- Inference calls are serialized through a queue (BeeLlama can only handle one request at a time per port)
- File writes happen in sandboxes (no conflicts)

### Concurrency Limits

| Resource | Limit | Rationale |
|----------|-------|-----------|
| Concurrent sandboxes | 4 | Docker memory on Triton (32GB host) |
| Concurrent inferences | 1 per port | BeeLlama is single-threaded per model |
| Concurrent test runs | 1 per sandbox | Avoid OOM in containers |

---

## MVP Validation Checklist

Before moving to Phase 2, the MVP (Loops 1-4) must pass:

- [ ] **PRD parsing:** 3 different PRDs parsed into valid task lists (JSON)
- [ ] **Prompt quality:** Code generated from prompts is syntactically valid Python (>90%)
- [ ] **Response parsing:** Parser extracts code from 90%+ of sample responses
- [ ] **Code generation:** At least 1 file generated and written to sandbox
- [ ] **Test execution:** At least 1 test run inside sandbox (pass or fail — just prove it works)
- [ ] **Git workflow:** Branch created, committed, pushed to Gitea
- [ ] **PR creation:** PR visible on Gitea web UI with description
- [ ] **End-to-end:** Full pipeline runs without manual intervention on a simple PRD

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM generates invalid code | High | Medium | Retry loop (3x), fallback to simpler prompts |
| Response parser fails on edge cases | Medium | Medium | Fallback chain (XML → markdown → raw) |
| Docker sandbox OOM | Low | Low | Limit concurrent sandboxes, monitor memory |
| BeeLlama crash during generation | Low | High | Checkpoint + resume, health check before each call |
| Circular dependency in PRD | Low | Low | Detect in topological sort, error with clear message |
| Budget exceeded | Medium | Low | Hard stop at budget limit, partial PR commit |
| Model swap during generation | Low | High | Lock model during task execution |

---

## File Inventory (New Files to Create)

### Phase 1 (MVP)

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `prompts/system-coder.txt` | ~40 | System prompt for code generation |
| `prompts/system-reviewer.txt` | ~30 | System prompt for code review |
| `prompts/system-decomposer.txt` | ~40 | System prompt for PRD decomposition |
| `prompts/system-fixer.txt` | ~30 | System prompt for error fixing |
| `prompts/output-format.txt` | ~30 | Output format specification |
| `response_parser.py` | ~150 | Extract code from LLM responses |
| `prd_parser.py` | ~400 | Parse Markdown PRD → JSON tasks |
| `code_generator.py` | ~500 | Generate → test → retry loop |
| `git_workflow.py` | ~300 | Branch → commit → push → PR |

### Phase 2 (Harden)

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `quality_gates.py` | ~250 | Lint, type check, review gates |
| `context_manager.py` | ~300 | Track files, imports, modifications |
| `task_queue.py` | ~300 | Dependency resolution + parallel execution |

### Phase 3 (Scale)

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `cost_tracker.py` | ~150 | Token/cost tracking + budget |
| `model_router.py` | ~200 | Select model per task type |
| `engine.py` | ~400 | Unified CLI entry point |

**Total new code:** ~3,970 lines across 16 files

---

## Success Criteria (Measurable)

| Metric | MVP (Phase 1) | Hardened (Phase 2) | Production (Phase 3) |
|--------|---------------|--------------------|--------------------|
| PRD parse accuracy | 80%+ tasks extracted | 90%+ with correct deps | 95%+ with priorities |
| Code syntax validity | 80%+ | 90%+ | 95%+ |
| Test pass rate (first attempt) | 40%+ | 60%+ | 75%+ |
| Test pass rate (after retries) | 60%+ | 80%+ | 90%+ |
| End-to-end time (5-task PRD) | < 30 min | < 20 min | < 15 min |
| Manual intervention needed | 2-3 times | 0-1 times | 0 times |
| PR quality | Readable | Reviewable | Mergeable |
| Cost per 5-task PRD | $0 (local) | $0 (local) | $0 (local) |

---

## Implementation Order

```
Week 1: Loop 1 (prompts + parser) + Loop 2 (PRD parser)
  → Can test: parse a PRD, generate code, extract from response

Week 2: Loop 3 (code generation loop) + Loop 4 (git workflow)
  → MVP complete: end-to-end on simple PRD

Week 3: Loop 5 (error recovery + quality gates)
  → Hardened: retry logic, lint, review

Week 4: Loop 6 (context + orchestration) + Loop 7 (cost tracking)
  → Production: multi-file, parallel, cost-aware

Week 5: Loop 8 (model router + engine CLI) + demo
  → Ship: full pipeline, documented, demonstrated
```
