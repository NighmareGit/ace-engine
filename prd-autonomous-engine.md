# PRD: Autonomous Coding Engine (ACE)

## Problem Statement

Software development is labor-intensive and sequential. A developer must read a PRD, break it into tasks, write code, run tests, fix bugs, commit changes, and create a PR — all manually. For a typical 10-task feature, this process takes hours to days and requires sustained human attention.

**The core problem:** There is no autonomous system that can take a structured requirements document and produce a complete, tested, reviewed pull request without human intervention during execution.

**Impact:** Teams spend ~40% of engineering time on routine implementation tasks that follow predictable patterns. This delays delivery, increases cost, and diverts engineers from higher-value creative and architectural work.

---

## Solution

The **Autonomous Coding Engine (ACE)** is a pipeline that automates the full software delivery lifecycle from PRD to pull request:

1. **Parse** the PRD into atomic, dependency-ordered tasks
2. **Generate** implementation code for each task using an LLM
3. **Test** the generated code automatically
4. **Fix** failing tests through iterative retry
5. **Review** code quality via automated gates
6. **Commit** all changes with descriptive messages
7. **Create PR** with a summary of everything done

The engine operates within a sandboxed workspace (Triton), making it safe to run autonomously. All decisions are logged for auditability, and the system escalates to human review when confidence is low.

---

## User Stories

### Epic 1: PRD Ingestion

**US-01:** As a developer, I want to feed a markdown PRD into the engine so that it understands what needs to be built.

**US-02:** As a developer, I want the engine to parse user stories and acceptance criteria so that each task has clear success conditions.

**US-03:** As a developer, I want the engine to detect dependencies between tasks so that it builds things in the right order.

**US-04:** As a developer, I want the engine to flag ambiguous or incomplete requirements so that I can clarify before code is generated.

**US-05:** As a developer, I want the engine to handle PRDs of varying formats (bullet lists, numbered tasks, table-based) so that I don't need to follow a rigid template.

### Epic 2: Code Generation

**US-06:** As a developer, I want the engine to generate valid Python code that follows my project's existing style and conventions.

**US-07:** As a developer, I want the engine to generate TypeScript/JavaScript code so that it works for frontend projects too.

**US-08:** As a developer, I want the engine to generate unit tests alongside implementation code so that coverage is built in from the start.

**US-09:** As a developer, I want the engine to respect existing code patterns (imports, naming, structure) so that generated code feels native to the project.

**US-10:** As a developer, I want the engine to generate type-annotated code so that static analysis catches issues early.

### Epic 3: Testing & Validation

**US-11:** As a developer, I want the engine to run the test suite after each code generation so that I know the code works.

**US-12:** As a developer, I want the engine to automatically retry failing tests with fixed code so that transient issues don't block progress.

**US-13:** As a developer, I want the engine to stop retrying after 3 failures so that it doesn't waste time on unsolvable problems.

**US-14:** As a developer, I want the engine to report test results with clear failure messages so that I can diagnose issues quickly.

**US-15:** As a developer, I want the engine to measure code coverage so that I know how thoroughly the code is tested.

### Epic 4: Code Quality

**US-16:** As a developer, I want the engine to run linting (ruff/eslint) so that code follows style rules.

**US-17:** As a developer, I want the engine to run type checking (mypy/tsc) so that type errors are caught.

**US-18:** As a developer, I want the engine to check cyclomatic complexity so that generated code doesn't become too complex.

**US-19:** As a developer, I want the engine to fix auto-fixable lint issues so that I don't waste time on formatting.

### Epic 5: Git Workflow

**US-20:** As a developer, I want the engine to create a feature branch so that main stays clean.

**US-21:** As a developer, I want the engine to commit with conventional commit messages (feat:, fix:) so that the git history is readable.

**US-22:** As a developer, I want the engine to push to the remote so that my work is backed up.

**US-23:** As a developer, I want the engine to create a pull request with a summary so that I can review what was done.

**US-24:** As a developer, I want the PR description to include a list of changed files, test results, and any unresolved issues.

### Epic 6: Observability & Control

**US-25:** As a developer, I want to see real-time progress as the engine works so that I know it's not stuck.

**US-26:** As a developer, I want a structured log of every decision the engine made so that I can audit its work.

**US-27:** As a developer, I want to configure retry limits, timeouts, and quality thresholds so that the engine respects my preferences.

**US-28:** As a developer, I want the engine to produce a summary report at the end so that I can quickly assess the outcome.

**US-29:** As a developer, I want the engine to work incrementally (not restart from scratch on failure) so that partial progress is preserved.

**US-30:** As a developer, I want to be able to resume a failed run from where it left off so that I don't lose work.

---

## Implementation Decisions

### Decision 1: Python as Implementation Language

**Choice:** Build ACE in Python 3.11+.

**Rationale:** Python has the best ecosystem for LLM integration, AST parsing, and test execution. The engine targets Python and TypeScript projects, and Python is the natural choice for tooling in both ecosystems.

**Trade-off:** ACE cannot self-modify in TypeScript, but this is acceptable since the engine is infrastructure, not product code.

### Decision 2: LLM Abstraction Layer

**Choice:** Abstract LLM calls behind a `LLMProvider` interface with pluggable backends.

**Rationale:** LLM providers change rapidly. The engine should work with OpenAI, Anthropic, local models, or any API-compatible endpoint without code changes.

**Trade-off:** Slight overhead from abstraction; mitigated by keeping the interface minimal.

### Decision 3: Deterministic Task IDs

**Choice:** Generate task IDs from content hash (heading path + description).

**Rationale:** This makes the engine idempotent — running twice on the same PRD produces the same task IDs and thus the same commit structure. It also enables incremental re-runs.

**Trade-off:** IDs are opaque (not human-readable); mitigated by including task title in all output.

### Decision 4: Workspace Sandboxing

**Choice:** All file operations happen within a configured workspace root; path traversal is blocked.

**Rationale:** Safety is paramount for an autonomous system. Blocking writes outside the workspace prevents accidental damage to unrelated projects.

**Trade-off:** Requires explicit workspace configuration; but this is a one-time setup cost.

### Decision 5: Retry with Escalation

**Choice:** Retry failing tasks up to 3 times with progressively more context (error messages, stack traces, related file contents). After 3 failures, mark as failed and continue with remaining tasks.

**Rationale:** Many LLM failures are transient (syntax errors, import mistakes) that can be fixed with error feedback. But infinite loops waste resources and produce diminishing returns.

**Trade-off:** Some tasks that could be fixed with more retries will fail; mitigated by including detailed failure context in the PR.

### Decision 6: JSONL Tracing

**Choice:** Log all events to a structured JSONL trace file.

**Rationale:** JSONL is parseable, append-friendly, and works with standard tools (jq, pandas). Structured logs enable post-hoc analysis without custom parsing.

**Trade-off:** Larger log files than plain text; mitigated by compression and rotation.

---

## Testing Decisions

### Unit Tests

- Each module has a dedicated test file (`test_prd_parser.py`, etc.)
- Mock external dependencies (LLM API, Gitea API, git)
- Focus on edge cases: empty inputs, malformed data, permission errors

### Integration Tests

- Full pipeline test: PRD → commit (with mock LLM)
- File operations test: write → backup → diff → restore
- Task queue test: dependency resolution with complex DAGs
- Git workflow test: branch → commit → push (with local git server)

### End-to-End Test

- Real PRD → real LLM → real commit (requires API key)
- Run on CI with a known-good PRD
- Verify PR is created with correct content
- Marked as `@slow` — not run on every commit

### Quality Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Unit test coverage | >85% | `pytest --cov` |
| Integration test pass rate | 100% | CI pipeline |
| E2E test pass rate | >90% | Weekly run |
| LLM generation validity | >90% | Syntax check on output |
| First-pass test rate | >80% | Generation → test pipeline |

---

## Out of Scope

1. **Frontend UI generation** — ACE focuses on backend/CLI code; no React/Vue/Angular component generation in V1.
2. **Database migrations** — ACE does not generate or manage schema migrations.
3. **Deployment automation** — No CI/CD pipeline creation; ACE creates code, not infrastructure.
4. **Multi-repo changes** — ACE operates within a single repository.
5. **Non-markdown PRDs** — No support for YAML, JSON, or Jira-format inputs in V1.
6. **Real-time collaboration** — ACE is a batch processor; no streaming or interactive mode.
7. **Custom LLM fine-tuning** — ACE uses existing models; no training pipeline.
8. **Security auditing** — No dependency vulnerability scanning or SAST in V1 (future enhancement).
9. **Documentation generation** — ACE generates code and tests, not user docs or API docs.
10. **Performance optimization** — ACE generates correct code, not necessarily optimal code.

---

## Further Notes

### Glossary

| Term | Definition |
|------|------------|
| **PRD** | Product Requirements Document — a structured description of what to build |
| **ACE** | Autonomous Coding Engine — the system described in this document |
| **Triton** | The workspace environment where ACE executes |
| **Task** | A single atomic coding unit derived from the PRD |
| **Quality Gate** | A checkpoint that must pass before code is committed |
| **Tracer Bullet** | A minimal end-to-end implementation that validates the architecture |
| **Context Snapshot** | A point-in-time view of the project state used for code generation |
| **DAG** | Directed Acyclic Graph — the dependency structure of tasks |

### Future Considerations

- **V3:** Add code review integration (AI reviewer before human review)
- **V4:** Support for refactoring existing codebases (not just new features)
- **V5:** Multi-language support beyond Python/TypeScript (Go, Rust, Java)
- **V6:** Plugin system for custom quality gates and generation strategies

### References

- Google's "Self-Driving CI" concept
- GitHub Copilot Workspace architecture
- Devin AI autonomous agent approach
- SWE-bench evaluation methodology
