# Technical Specifications: Autonomous Coding Engine

## Overview

This document specifies the interface contracts, error handling, dependencies, and acceptance criteria for every module in the Autonomous Coding Engine (ACE).

---

## 1. PRD Parser (`prd_parser.py`)

### Purpose

Parse a markdown PRD into a structured list of atomic coding tasks, each with metadata including dependencies, acceptance criteria, and priority.

### Input Format

A markdown file (UTF-8) with the following supported structures:

```markdown
# Title
## Epic/Feature Name
### User Story / Task
- [ ] Task description
  - Sub-detail
- Acceptance criteria:
  - [ ] Criterion 1
  - [ ] Criterion 2
```

The parser must handle:
- H1/H2/H3/H4 headings as hierarchy levels
- Checkbox lists (`- [ ]` / `- [x]`) as tasks
- Nested bullets as sub-details
- Blank-line-separated sections
- Code blocks (fenced with ```) — preserved as-is, not parsed

### Output Format

```python
@dataclass
class ParsedTask:
    id: str                    # Deterministic: hash of heading path + description
    title: str                 # Human-readable task name
    description: str           # Full description text
    level: int                 # Heading depth (1-6)
    parent_id: str | None      # ID of parent task, None for top-level
    acceptance_criteria: list[str]
    dependencies: list[str]    # IDs of tasks this depends on (parsed from text or explicit)
    priority: str              # "critical" | "high" | "medium" | "low"
    metadata: dict             # Any extra fields from the PRD

@dataclass
class PRD:
    title: str
    tasks: list[ParsedTask]
    metadata: dict             # PRD-level metadata (author, date, version)
    raw_markdown: str          # Original content preserved
```

### Error Handling

| Error | Behavior |
|-------|----------|
| File not found | Raise `FileNotFoundError` with path in message |
| Empty file | Return `PRD` with empty `tasks` list and warning log |
| Unparseable structure | Skip unparseable sections; log warnings; return what was parseable |
| Duplicate task IDs | Append numeric suffix to disambiguate |
| Encoding error | Raise `UnicodeDecodeError` with file path |

### Dependencies

- `markdown-it-py` or `mistune` for AST-based parsing
- `hashlib` for deterministic task ID generation
- Python stdlib only otherwise

### Acceptance Criteria

- [ ] Parses a 20-task PRD in <100ms
- [ ] Deterministic: same input always produces same task IDs
- [ ] Handles malformed markdown gracefully (no crashes)
- [ ] Preserves original markdown in `raw_markdown` field
- [ ] Correctly extracts acceptance criteria from nested lists

---

## 2. File Operations (`file_ops.py`)

### Purpose

Provide safe, audited filesystem operations for reading, writing, and managing files in the Triton workspace.

### Input/Output

All methods accept and return `pathlib.Path` or `str` paths. All paths are resolved relative to a configured workspace root.

```python
class FileOps:
    def __init__(self, workspace_root: Path, backup_dir: Path | None = None):
        ...

    def read(self, path: str | Path) -> str:
        """Read file contents. Raises FileNotFoundError if missing."""

    def write(self, path: str | Path, content: str, create_dirs: bool = True) -> WriteResult:
        """Write content atomically. Creates backup if file exists."""

    def list_files(self, pattern: str = "**/*", exclude: list[str] | None = None) -> list[Path]:
        """List files matching glob pattern, excluding specified patterns."""

    def diff(self, path: str | Path) -> str:
        """Return unified diff of current vs last backup."""

    def backup(self, path: str | Path) -> Path:
        """Create a timestamped backup. Returns backup path."""

    def restore(self, backup_path: str | Path) -> Path:
        """Restore from backup. Returns restored path."""

    def exists(self, path: str | Path) -> bool:
        """Check file existence."""

    def delete(self, path: str | Path) -> bool:
        """Delete file (moves to backup dir). Returns True if deleted."""

@dataclass
class WriteResult:
    path: Path
    backup_path: Path | None   # None if file didn't exist before
    bytes_written: int
    duration_ms: float
```

### Error Handling

| Error | Behavior |
|-------|----------|
| Path traversal attempt (../) | Raise `SecurityError` |
| Write outside workspace | Raise `SecurityError` |
| Permission denied | Raise `PermissionError` with clear message |
| Disk full | Raise `OSError`; do not leave partial writes |
| Backup failure | Log warning; proceed with write (non-fatal) |

### Dependencies

- Python stdlib: `pathlib`, `shutil`, `tempfile`, `hashlib`
- No external dependencies

### Acceptance Criteria

- [ ] All writes are atomic (write to temp, then rename)
- [ ] Backups created before every overwrite
- [ ] Path traversal attacks blocked and logged
- [ ] `diff()` produces valid unified diff output
- [ ] Handles files up to 100MB without memory issues (streaming)

---

## 3. Test Runner (`test_runner.py`)

### Purpose

Execute test suites (pytest, unittest, or Jest) and return structured results.

### Input Format

```python
@dataclass
class TestConfig:
    framework: str              # "pytest" | "unittest" | "jest" | "vitest"
    test_path: str              # Path to test directory or file
    timeout_seconds: int        # Per-test timeout (default: 30)
    coverage: bool              # Whether to collect coverage data
    extra_args: list[str]       # Additional CLI arguments
```

### Output Format

```python
@dataclass
class TestResult:
    framework: str
    total: int
    passed: int
    failed: int
    skipped: int
    errors: int
    duration_seconds: float
    coverage_percent: float | None
    test_cases: list[TestCase]
    output: str                 # Raw stdout/stderr
    exit_code: int

@dataclass
class TestCase:
    name: str
    status: str                 # "passed" | "failed" | "skipped" | "error"
    duration_seconds: float
    message: str | None         # Error/failure message
    traceback: str | None       # Full traceback for failures
    file_path: str | None
    line_number: int | None
```

### Error Handling

| Error | Behavior |
|-------|----------|
| Framework not installed | Return result with `errors=1` and clear message |
| Test timeout | Kill process; mark all incomplete tests as timed out |
| Non-zero exit code | Parse output for structured results; include raw output |
| Segfault/crash | Capture core dump info; report as `error` status |
| No tests found | Return result with `total=0`; log warning |

### Dependencies

- `subprocess` for running test commands
- `pytest` (optional, for programmatic invocation)
- Framework detection via file presence (`conftest.py`, `jest.config.*`)

### Acceptance Criteria

- [ ] Detects framework automatically from project structure
- [ ] Parses pytest JSON output (`--tb=short --json-report`)
- [ ] Captures both stdout and stderr
- [ ] Enforces per-test timeout and kills hung tests
- [ ] Returns structured results even when framework crashes

---

## 4. Code Generator (`code_generator.py`)

### Purpose

Generate implementation code for individual tasks using an LLM, with context from the project and other tasks.

### Input Format

```python
@dataclass
class GenerationRequest:
    task: ParsedTask            # The task to implement
    context: ContextSnapshot    # Current project state
    language: str               # "python" | "typescript"
    style_guide: str | None     # Optional style preferences
    existing_files: dict[str, str]  # File path → content for reference
    retry_context: RetryContext | None  # If retrying, includes previous errors
```

### Output Format

```python
@dataclass
class GeneratedCode:
    task_id: str
    files: list[GeneratedFile]
    explanation: str            # Human-readable explanation of approach
    assumptions: list[str]      # Assumptions made during generation
    warnings: list[str]         # Potential issues to review
    tokens_used: int
    generation_time_ms: float

@dataclass
class GeneratedFile:
    path: str                   # Relative path within workspace
    content: str                # Full file content
    language: str
    action: str                 # "create" | "modify" | "delete"
    diff: str | None            # For modifications, the unified diff
```

### Error Handling

| Error | Behavior |
|-------|----------|
| LLM API timeout | Retry 2x with exponential backoff; raise on 3rd failure |
| Invalid syntax in output | Strip markdown fences; retry with syntax error context |
| Context window exceeded | Truncate context; log what was dropped |
| Empty response | Raise `GenerationError` with task ID |
| Rate limiting | Back off per provider headers; queue and retry |

### Dependencies

- LLM API client (abstracted via `LLMProvider` interface)
- `tree-sitter` for syntax validation (optional)
- `tiktoken` for token counting (optional)

### Acceptance Criteria

- [ ] Generates syntactically valid code >90% of the time
- [ ] Respects existing code style (detected from project files)
- [ ] Includes type hints in Python, type annotations in TypeScript
- [ ] Generates tests alongside implementation code
- [ ] Retry context improves success rate on subsequent attempts

---

## 5. Context Manager (`context_manager.py`)

### Purpose

Maintain a live snapshot of the project state that the Code Generator and other components can query.

### Data Maintained

```python
@dataclass
class ContextSnapshot:
    timestamp: float
    file_tree: dict[str, FileInfo]     # Path → metadata
    imports: dict[str, list[str]]      # File → imported symbols
    exports: dict[str, list[str]]      # File → exported symbols
    types: dict[str, TypeInfo]         # Type definitions
    dependencies: dict[str, list[str]] # File → files it depends on
    decisions: list[Decision]          # Architecture/design decisions made
    task_status: dict[str, str]        # Task ID → status
    recent_changes: list[Change]       # Last N file modifications

@dataclass
class FileInfo:
    path: str
    language: str
    size_bytes: int
    last_modified: float
    checksum: str
    line_count: int

@dataclass
class Decision:
    timestamp: float
    task_id: str
    description: str
    rationale: str
    alternatives: list[str]
```

### Core Methods

```python
class ContextManager:
    def snapshot(self) -> ContextSnapshot:
        """Capture current project state."""

    def update(self, changes: list[Change]) -> None:
        """Incrementally update context after file operations."""

    def get_relevant_files(self, task: ParsedTask, max_files: int = 10) -> list[str]:
        """Return files most relevant to a given task."""

    def get_import_graph(self) -> dict[str, list[str]]:
        """Return the full import dependency graph."""

    def record_decision(self, decision: Decision) -> None:
        """Record an architectural/design decision."""

    def summarize(self, max_tokens: int = 4000) -> str:
        """Generate a text summary suitable for LLM context injection."""
```

### Error Handling

| Error | Behavior |
|-------|----------|
| File read failure during snapshot | Skip file; log warning; continue |
| Parse error on import extraction | Skip file; mark as "unparseable" |
| Context too large for LLM | Truncate by relevance; log what was dropped |
| Stale snapshot | Auto-refresh when file checksums change |

### Dependencies

- `tree-sitter` with Python/TypeScript grammars for AST parsing
- `pathlib` for file operations
- `hashlib` for checksums

### Acceptance Criteria

- [ ] Snapshot of 500-file project completes in <5 seconds
- [ ] Incremental update after single file change in <50ms
- [ ] `get_relevant_files` returns contextually appropriate results
- [ ] Import graph correctly detects circular dependencies
- [ ] Text summary fits within specified token budget

---

## 6. Quality Gates (`quality_gates.py`)

### Purpose

Enforce code quality standards before committing changes.

### Checks Performed

```python
@dataclass
class QualityConfig:
    lint: bool = True
    type_check: bool = True
    format_check: bool = True
    max_complexity: int = 15          # Cyclomatic complexity threshold
    min_coverage: float = 70.0        # Minimum test coverage %
    max_line_length: int = 120
    require_docstrings: bool = False
    require_type_hints: bool = True

@dataclass
class QualityResult:
    passed: bool
    checks: list[CheckResult]
    summary: str
    duration_seconds: float

@dataclass
class CheckResult:
    name: str                         # "lint" | "type_check" | "format" | "complexity" | "coverage"
    passed: bool
    issues: list[Issue]
    duration_seconds: float

@dataclass
class Issue:
    file_path: str
    line: int
    column: int
    severity: str                     # "error" | "warning" | "info"
    message: str
    rule: str | None
    fixable: bool
```

### Error Handling

| Error | Behavior |
|-------|----------|
| Linter not installed | Skip lint check; log warning |
| Type checker timeout | Mark as failed with timeout message |
| Unfixable issues | Report all issues; do not auto-fix |
| Partial failures | Report results for each check independently |

### Dependencies

- `ruff` (Python linting + formatting)
- `mypy` (Python type checking)
- `eslint` / `tsc --noEmit` (TypeScript checking)
- All optional; gracefully skipped if not installed

### Acceptance Criteria

- [ ] Runs all enabled checks in parallel
- [ ] Returns structured issue list with file/line/column
- [ ] Distinguishes errors from warnings
- [ ] Identifies auto-fixable issues
- [ ] Total runtime <30 seconds for a 20-file project

---

## 7. Git Workflow (`git_workflow.py`)

### Purpose

Manage git operations: branching, committing, pushing, and creating pull requests via Gitea.

### Operations

```python
class GitWorkflow:
    def __init__(self, repo_path: Path, gitea_url: str | None = None, token: str | None = None):
        ...

    def create_branch(self, name: str) -> str:
        """Create and checkout a new branch. Returns branch name."""

    def commit(self, message: str, files: list[str] | None = None) -> CommitResult:
        """Stage and commit. If files is None, commit all changes."""

    def push(self, branch: str | None = None) -> PushResult:
        """Push current branch to remote."""

    def create_pr(self, title: str, body: str, base: str = "main") -> PRResult:
        """Create PR via Gitea API. Returns PR URL."""

    def get_status(self) -> RepoStatus:
        """Return current repo status: branch, dirty files, ahead/behind."""

    def stash(self) -> bool:
        """Stash current changes."""

    def stash_pop(self) -> bool:
        """Pop most recent stash."""

    def log(self, count: int = 10) -> list[CommitInfo]:
        """Return recent commits."""

@dataclass
class CommitResult:
    sha: str
    message: str
    files_changed: int
    insertions: int
    deletions: int

@dataclass
class PRResult:
    url: str
    number: int
    title: str
    body: str

@dataclass
class RepoStatus:
    branch: str
    is_dirty: bool
    ahead: int
    behind: int
    staged_files: list[str]
    modified_files: list[str]
    untracked_files: list[str]
```

### Error Handling

| Error | Behavior |
|-------|----------|
| Not a git repository | Raise `GitError` with init instructions |
| Merge conflict | Return conflict details; do not auto-resolve |
| Gitea API error | Retry 2x; raise with API response body |
| Nothing to commit | Return `CommitResult` with `files_changed=0` |
| Push rejected | Fetch and re-attempt once; raise on second failure |

### Dependencies

- `gitpython` for local git operations
- `httpx` or `requests` for Gitea API calls
- Gitea personal access token (from env var or config)

### Acceptance Criteria

- [ ] Creates branch from main with timestamp-based naming
- [ ] Commits with conventional commit format (feat:, fix:, etc.)
- [ ] Push handles non-fast-forward gracefully
- [ ] PR body includes summary, changed files list, and test results
- [ ] All operations are idempotent where possible

---

## 8. Task Queue (`task_queue.py`)

### Purpose

Orchestrate task execution order based on dependencies, manage parallel execution where possible, and track overall progress.

### Data Model

```python
@dataclass
class TaskNode:
    task: ParsedTask
    status: str                    # "pending" | "ready" | "running" | "completed" | "failed" | "skipped"
    dependencies: set[str]         # IDs of tasks this depends on
    dependents: set[str]           # IDs of tasks that depend on this
    retries: int                   # Number of retry attempts
    result: TaskResult | None
    started_at: float | None
    completed_at: float | None
    error: str | None

@dataclass
class TaskResult:
    task_id: str
    files_created: list[str]
    files_modified: list[str]
    tests_passed: bool
    quality_passed: bool
    duration_seconds: float

@dataclass
class QueueState:
    total: int
    completed: int
    failed: int
    running: int
    pending: int
    skipped: int
    started_at: float
    elapsed_seconds: float
    estimated_remaining: float
```

### Core Methods

```python
class TaskQueue:
    def __init__(self, tasks: list[ParsedTask], max_parallel: int = 1, max_retries: int = 3):
        ...

    def get_ready(self) -> list[TaskNode]:
        """Return tasks whose dependencies are all satisfied."""

    def mark_running(self, task_id: str) -> None:
        """Mark task as currently executing."""

    def mark_completed(self, task_id: str, result: TaskResult) -> None:
        """Mark task done; unlock dependents."""

    def mark_failed(self, task_id: str, error: str) -> None:
        """Mark task failed; decide whether to retry or skip dependents."""

    def get_state(self) -> QueueState:
        """Return current queue state for progress reporting."""

    def get_critical_path(self) -> list[str]:
        """Return task IDs on the critical path (longest dependency chain)."""

    def visualize(self) -> str:
        """Return ASCII DAG visualization of task dependencies."""
```

### Error Handling

| Error | Behavior |
|-------|----------|
| Circular dependency detected | Raise `DependencyError` with cycle path |
| All retries exhausted | Mark task as `failed`; skip unblocked dependents |
| Deadlock (no ready tasks, tasks still running) | Log warning; wait with timeout |
| Task timeout | Kill task; mark as failed; retry if retries remain |

### Dependencies

- `networkx` for DAG operations (optional; can use manual implementation)
- `topological_sort` for execution ordering
- Python stdlib `threading` or `asyncio` for parallel execution

### Acceptance Criteria

- [ ] Correctly resolves DAG with up to 100 nodes in <10ms
- [ ] Detects circular dependencies at initialization
- [ ] Executes independent tasks in parallel (configurable concurrency)
- [ ] Progress state is always consistent (no race conditions)
- [ ] Critical path calculation is correct for complex DAGs
- [ ] ASCII visualization renders correctly in terminal

---

## Cross-Cutting Concerns

### Logging

All modules log to a structured JSONL file:

```json
{
  "timestamp": "2025-01-15T10:30:00Z",
  "level": "info",
  "module": "code_generator",
  "event": "generation_complete",
  "task_id": "ace-01",
  "duration_ms": 1234,
  "tokens_used": 2048
}
```

### Configuration

All modules accept configuration via a shared `ACEConfig` dataclass:

```python
@dataclass
class ACEConfig:
    workspace_root: Path
    backup_dir: Path
    log_file: Path
    llm_provider: str
    llm_model: str
    gitea_url: str | None
    gitea_token: str | None
    quality: QualityConfig
    max_retries: int = 3
    max_parallel_tasks: int = 1
    timeout_per_task: int = 300  # seconds
```

### Telemetry

Every pipeline run produces a trace file:

```
workspace/
  .ace/
    traces/
      2025-01-15T103000.jsonl    # Full execution trace
      2025-01-15T103000.summary.json  # Machine-readable summary
```
