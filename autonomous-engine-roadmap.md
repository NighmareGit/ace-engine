# Autonomous Coding Engine — Roadmap

## Goal

Transform the coder-harness from a benchmark platform into a **full autonomous coding engine** that can:
1. Accept a PRD, epic, story, or set of tickets
2. Decompose into atomic coding tasks
3. Generate code for each task
4. Test the code
5. Fix failures
6. Commit and push to Gitea
7. Create PRs with descriptions

## Current State (23 files, 13,500 lines)

| Component | Status | Lines |
|-----------|--------|-------|
| SSH transport | ✅ Working | 261 |
| BeeLlama inference | ✅ 195 tok/s | — |
| Gitea API | ✅ Working | 663 |
| SQLite telemetry | ✅ Schema unified | 138 |
| Benchmark platform | ✅ Complete | 4,228 |
| Master CLI (harness.py) | ✅ 14 commands | 780 |
| Integration tests | ✅ 62 passing | 1,107 |
| E2E validation | ✅ 9/9 passed | 403 |

## Missing Components

| Component | Complexity | Est. Lines |
|-----------|-----------|------------|
| PRD parser + task decomposer | Medium | ~400 |
| File operations (read/write on Triton) | Low | ~200 |
| Test execution engine | Medium | ~300 |
| Code generation loop | High | ~500 |
| Error recovery | High | ~300 |
| Context memory | High | ~400 |
| Lint + quality gates | Medium | ~250 |
| Git workflow (branch/commit/PR) | Medium | ~300 |
| Code review integration | Medium | ~200 |
| Task queue + orchestration | High | ~500 |
| Multi-file awareness | High | ~300 |
| End-to-end demo | Integration | ~200 |

## Loop Plan: 12 Loops in 4 Phases

### Phase A: Foundation (Loops 1-3)

**Loop 1: PRD Parser + Task Decomposer**
- Input: Markdown PRD file
- Output: JSON task list with dependencies
- Files: `prd_parser.py`

**Loop 2: File Operations on Triton**
- Read/write/list/grep files via SSH
- Files: `file_ops.py`

**Loop 3: Test Execution Engine**
- Run pytest on Triton, parse results
- Files: `test_runner.py`

### Phase B: Core Engine (Loops 4-6)

**Loop 4: Code Generation Loop**
- Task → inference → extract code → write → test → retry
- Files: `code_generator.py`

**Loop 5: Error Recovery**
- Parse test failures, generate fix prompts, retry
- Enhancement to `code_generator.py`

**Loop 6: Context Memory**
- Track files, modifications, test results, conversation history
- Files: `context_manager.py`

### Phase C: Quality Gates (Loops 7-9)

**Loop 7: Lint + Quality Gates**
- flake8/pylint on generated code
- Files: `quality_gates.py`

**Loop 8: Git Workflow**
- Branch → commit → push → PR on Gitea
- Files: `git_workflow.py`

**Loop 9: Code Review Integration**
- Judge model reviews code before commit
- Enhancement to existing `judge.py`

### Phase D: Orchestration (Loops 10-12)

**Loop 10: Task Queue + Orchestration**
- PRD → decompose → execute → track
- Files: `task_queue.py`

**Loop 11: Multi-File Awareness**
- Understand imports, class hierarchies, project structure
- Enhancement to `context_manager.py`

**Loop 12: End-to-End Demo**
- Run full pipeline on a real PRD
- Integration test + documentation

## Token Budget (at 195 tok/s)

| Phase | Tokens | Time |
|-------|--------|------|
| Phase A | ~15K | ~80s |
| Phase B | ~20K | ~100s |
| Phase C | ~12K | ~60s |
| Phase D | ~25K | ~130s |
| **Total** | **~72K** | **~6 min** |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Inference quality too low for code gen | Medium | High | Use thinking model (Qwen3.6-35B), increase max_tokens |
| Context window overflow | Low | Medium | Sliding window, file summarization |
| Test execution fails on Triton | Low | Low | Pre-install pytest in sandbox |
| Gitea push fails | Low | Low | Fallback to SCP |
| Model swap during generation | Low | High | Lock model during task execution |

## Success Criteria

1. Parse a 50-line PRD into 5+ atomic tasks
2. Generate valid Python code for each task
3. Run tests and achieve >80% pass rate
4. Commit all changes to Gitea with descriptive messages
5. Create a PR with summary
6. Total time < 30 minutes for a simple PRD
