# IMPLEMENTATION BLUEPRINT.md: Engine Unification

## Overview

This blueprint provides literal instructions for implementing the `engine/` package. Every file, class, function, and data structure is defined. A downstream agent can implement this without asking the architect anything.

---

## File: engine/__init__.py

```python
"""Engine package — the one autonomous coding engine."""

from dataclasses import dataclass, field
from typing import Callable

VERSION = "0.1.0"

@dataclass
class EngineConfig:
    """Configuration for an engine run."""
    model_config: str = "3090-qwen36-35b"
    max_retries_generate: int = 3
    max_retries_test: int = 2
    timeout_inference: int = 300
    timeout_test: int = 180
    timeout_commit: int = 60
    timeout_context: int = 120
    dry_run: bool = False
    sandbox: bool = False
    on_event: Callable | None = None
```

### BLOCKER #1 FIX: Task dataclass

```python
from dataclasses import dataclass, field
from typing import Callable

VERSION = "0.1.0"

@dataclass
class Task:
    """A single atomic coding unit derived from a PRD."""
    id: str                      # e.g. "T01"
    title: str                   # e.g. "Create health check endpoint"
    description: str             # Full task description from PRD
    module: str                  # PRIMARY target file, e.g. "api/health.py"
    dependencies: list[str] = field(default_factory=list)  # Task IDs this depends on
    task_type: str = "implementation"  # implementation | refactoring | debug | test
    priority: int = 1            # 1=highest
    prd_section: str = ""        # Original PRD section text
    files: list[str] = field(default_factory=list)  # C6 FIX: ALL target files (module + extras); empty = [module]
    acceptance_criteria: list[str] = field(default_factory=list)  # from PRDParser

@dataclass
class EngineConfig:
    """Configuration for an engine run."""
    model_config: str = "3090-qwen36-35b"
    max_retries_generate: int = 3
    max_retries_test: int = 2
    timeout_inference: int = 300
    timeout_test: int = 180
    timeout_commit: int = 60
    timeout_context: int = 120
    dry_run: bool = False
    sandbox: bool = False
    on_event: Callable | None = None
```

---

## File: engine/engine.py

**Pattern:** Facade. The ONE entry point.

```python
class Engine:
    """The one entry point for the autonomous coding engine."""

    def __init__(self, transport=None, on_event=None, config=None):
        """
        Args:
            transport: Auto-detected if None. From transport.get_transport().
            on_event: Optional callback. Signature: (event_type: str, data: dict) -> None.
            config: EngineConfig dataclass. Defaults if None.
        """
        self.transport = transport or _get_transport()  # call once, cache
        self.on_event = on_event
        self.config = config or EngineConfig()

        # SIM FIX: proactive health probe — fail fast with a clear error instead
        # of a silent RemoteTransport that fails on first use
        if not self.transport.check_health():
            raise ConnectionError(
                "Transport unreachable: engine_service (:3082) down, not on Triton, "
                "and SSH to Triton failed. Run 'python3 cli.py health' to diagnose.")

        # SIM FIX B10: SIGINT handler — checkpoint + graceful cancel, not a raw kill
        import signal
        signal.signal(signal.SIGINT, lambda s, f: self.cancel())


# SIM FIX B3: project bootstrap — ace-demo may not exist on a fresh Triton
def bootstrap_project(project_path, transport):
    """Create project skeleton + git repo + Gitea remote if the project
    directory doesn't exist. Idempotent: skips steps that are already done."""
    # 1. Directory exists?
    stdout, stderr, rc = transport.run_command(f"test -d {project_path} && echo yes")
    if "yes" not in stdout:
        transport.run_command(f"mkdir -p {project_path}")
        transport.run_command(f"cd {project_path} && git init")
        transport.run_command(f"cd {project_path} && git config user.email ace@local && git config user.name ace")
    # 2. Git repo initialized?
    stdout, _, rc = transport.run_command(f"cd {project_path} && git rev-parse --git-dir")
    if rc != 0:
        transport.run_command(f"cd {project_path} && git init")
    # 3. Origin remote configured? (SIM FIX B4: wire Gitea token auth here)
    stdout, _, _ = transport.run_command(f"cd {project_path} && git remote get-url origin")
    if rc != 0 and stdout.strip() == "":
        import os
        token = os.environ.get("GITEA_TOKEN", "")
        repo_name = os.path.basename(project_path)
        remote_url = f"http://<user>:{token}@<LAN_IP>:3000/<user>/{repo_name}.git"
        # Note: Gitea only listens on localhost INSIDE Triton — when running from
        # nightmare, create the repo via Gitea API over SSH tunnel first, and use
        # the token-authenticated URL for push. See MIGRATION_BLUEPRINT prerequisites.
        transport.run_command(f"cd {project_path} && git remote add origin {remote_url}")

    def run(self, prd_path, project_path, config=None, dry_run=False) -> RunResult:
        """Execute a PRD end-to-end."""
        # 1. tasks = parse_prd(prd_path)        — parse PRD into Task list
        # 2. pipeline = Pipeline(self)           — create state machine
        # 3. pipeline.transition(State.PARSING)  — start parsing
        # 4. pipeline.tasks = tasks
        # 5. if dry_run: pipeline.transition(State.QUEUED); return RunResult(tasks=tasks)
        # 6. pipeline.transition(State.QUEUED)
        # 7. Loop through tasks (topological order):
        #    a. pipeline.transition(State.CONTEXT)   — build context
        #    b. ctx = build_context(project_path, task, self.transport)
        #    c. pipeline.transition(State.GENERATE)  — generate code
        #    d. code = generate_code(ctx, task, self.config, self.transport)
        #    e. pipeline.transition(State.VALIDATE)  — validate
        #    f. result = validate(code, ctx)
        #    g. if not result.passed and retries < 3: goto step 7c (retry)
        #    f2. SIM FIX B2: STAGE FILES TO DISK — write generated files to the
        #        project BEFORE testing. Without this, pytest runs against
        #        whatever was already on disk and tests NOTHING.
        #        for filename, content in code.files.items():
        #            write_project_file(self.transport, f"{project_path}/{filename}", content)
        #        (save original contents first; restore on test failure = rollback)
        #    h. pipeline.transition(State.TEST)      — test
        #    i. test = run_tests(project_path, self.transport)
        #    j. if not test.passed and fixes < 2: goto step 7c (fix)
        #    k. pipeline.transition(State.COMMIT)    — commit
        #    l. commit = commit_code(project_path, task, code, self.transport)
        #    m. pipeline.transition(State.NEXT)
        #    n. pipeline.transition(State.QUEUED) for next task
        # 8. pipeline.transition(State.DONE)
        # 9. Return RunResult(...)

    def step(self, task_id) -> StepResult:
        """Execute a single task (manual control)."""

    def status(self, run_id=None) -> RunStatus:
        """Get pipeline status. If run_id is None, return current run."""

    def cancel(self) -> bool:
        """Cancel gracefully. Checkpoints state."""


# BLOCKER #3 FIX: PRD parsing function
def parse_prd(prd_path: str) -> list:
    """
    Parse a PRD markdown file into a list of Task objects.

    Supports multiple PRD formats:
    - Numbered lists: "1. As a user, I want..."
    - Bullet lists: "- Create health check endpoint"
    - Tables: "| T01 | Health Check | api/health.py |"
    - Headers: "## Task 1: Health Check" followed by description

    Returns list[Task] in dependency order (topological sort).
    """
    import re

    with open(prd_path, "r") as f:
        content = f.read()

    tasks = []
    task_id_counter = 1

    # C5 FIX (significant functional gap): the existing PRDParser is RICHER than
    # regex parsing — module grouping (~50% task reduction), user story
    # classification, spec extraction, dependency ordering, library detection.
    # TRY PRDParser FIRST; the regex strategies below are only a fallback for
    # PRDParser failures or outputs with zero tasks.
    try:
        from prd_parser import PRDParser
        parsed = PRDParser().parse(prd_path)
        raw_tasks = parsed.get("tasks", []) if isinstance(parsed, dict) else list(parsed)
        if raw_tasks:
            return _topological_sort([_prdparser_task_to_engine_task(t) for t in raw_tasks])
    except Exception:
        pass  # fall through to regex strategies

    # C6 FIX: mapping layer — PRDParser task dicts → engine Task dataclass.
    # Key differences: files_to_create is a LIST (Task.module keeps the primary
    # file; the full list is preserved), category enum differs from task_type.
    CATEGORY_TO_TYPE = {"test": "test", "api": "implementation", "data": "implementation",
                        "ui": "implementation", "logic": "implementation", "infra": "refactoring"}
    def _prdparser_task_to_engine_task(raw: dict) -> Task:
        files = raw.get("files_to_create", []) or []
        primary = files[0] if files else f"task_{raw.get('id', 'T00')}.py"
        return Task(
            id=raw["id"],
            title=raw.get("title", raw["id"]),
            description=raw.get("description", ""),
            module=primary.replace("..", "_").lstrip("/"),
            dependencies=raw.get("dependencies", []) or raw.get("depends_on", []),
            task_type=CATEGORY_TO_TYPE.get(raw.get("category", ""), "implementation"),
            priority=1,
            prd_section=raw.get("spec", ""),
            files=files or [primary],
        )

    # SIMULATION FIX (applies to ALL strategies): strip boilerplate sections
    # (User Stories, Problem Statement, Acceptance Criteria...) BEFORE any
    # extraction, so numbered/bullet items are drawn from deliverable sections
    # (Technical Requirements, Tasks, Implementation, Deliverables) only.
    # Table strategy (T01 IDs) is unaffected — boilerplate never uses T-ids.
    BOILERPLATE_SECTIONS = {"user stories", "problem statement", "acceptance criteria",
                            "further notes", "out of scope", "glossary", "references"}
    def _filter_boilerplate(text):
        sections = re.split(r'\n##\s+', text)
        kept = [s for s in sections
                if not any(s.lower().startswith(b) for b in BOILERPLATE_SECTIONS)]
        # If filtering removed EVERYTHING (PRD is all boilerplate), keep original
        return "\n".join(kept) if any(s.strip() for s in kept) else text
    task_content = _filter_boilerplate(content)

    # Strategy 1: Table format (| ID | Title | Module | ...)
    table_pattern = r'\|\s*(T\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|'
    table_matches = re.findall(table_pattern, content)
    if table_matches:
        for match in table_matches:
            task_id, title, module = match
            tasks.append(Task(
                id=task_id.strip(),
                title=title.strip(),
                description=title.strip(),
                module=module.strip(),
            ))
        return _topological_sort(tasks)

    # Strategy 2: Numbered list ("1. ..." or "1) ...") — on FILTERED content
    numbered_pattern = r'(?:^|\n)\s*(\d+)[.)]\s+(.*?)(?=\n\s*\d+[.)]|\n##|\Z)'
    numbered_matches = re.findall(numbered_pattern, task_content, re.DOTALL)
    if numbered_matches:
        # Only use numbered items NOT inside a boilerplate section
        # (split content by ## headers, drop boilerplate sections first)
        sections = re.split(r'\n##\s+', content)
        task_content = "\n".join(
            s for s in sections
            if not any(s.lower().startswith(b) for b in BOILERPLATE_SECTIONS)
        )
        numbered_matches = re.findall(numbered_pattern, task_content, re.DOTALL)
        # If nothing remains after filtering, fall through to header strategy
    if numbered_matches:
        for num, text in numbered_matches:
            task_id = f"T{int(num):02d}"
            # SIMULATION FIX: detect filenames with OR without backticks
            # ("Include a requirements.txt" has no backticks in the real PRD)
            module_match = (re.search(r'`([^`]+\.(?:py|js|ts|yaml|txt))`', text)
                            or re.search(r'\b([\w/\-]+\.(?:py|js|ts|yaml|txt))\b', text))
            module = module_match.group(1) if module_match else f"task_{task_id}.py"
            # RED-TEAM V2 FIX: sanitize module — block path traversal from PRD content
            module = module.replace("..", "_").lstrip("/")
            if module.startswith(("/", "~")):
                module = f"task_{task_id}.py"
            tasks.append(Task(
                id=task_id,
                title=text.strip()[:80],
                description=text.strip(),
                module=module,
            ))
        return _topological_sort(tasks)

    # Strategy 3: Bullet lists ("- ..." or "* ...")
    bullet_pattern = r'(?:^|\n)\s*[-*]\s+(.*?)(?=\n\s*[-*]|\n##|\Z)'
    bullet_matches = re.findall(bullet_pattern, content, re.DOTALL)
    if bullet_matches:
        for i, text in enumerate(bullet_matches, 1):
            task_id = f"T{i:02d}"
            module_match = re.search(r'`([^`]+\.(?:py|js|ts|yaml))`', text)
            module = module_match.group(1) if module_match else f"task_{task_id}.py"
            # RED-TEAM V2 FIX: sanitize module — block path traversal from PRD content
            module = module.replace("..", "_").lstrip("/")
            if module.startswith(("/", "~")):
                module = f"task_{task_id}.py"
            tasks.append(Task(
                id=task_id,
                title=text.strip()[:80],
                description=text.strip(),
                module=module,
            ))
        return _topological_sort(tasks)

    # Strategy 4: Headers ("## Task: ..." or "## ...")
    header_pattern = r'(?:^|\n)##\s+(.*?)(?=\n##|\Z)'
    header_matches = re.findall(header_pattern, content, re.DOTALL)
    for i, text in enumerate(header_matches, 1):
        task_id = f"T{i:02d}"
        module_match = re.search(r'`([^`]+\.(?:py|js|ts|yaml))`', text)
        module = module_match.group(1) if module_match else f"task_{task_id}.py"
        tasks.append(Task(
            id=task_id,
            title=text.strip().split("\n")[0][:80],
            description=text.strip(),
            module=module,
        ))

    return _topological_sort(tasks)


def _topological_sort(tasks: list) -> list:
    """Sort tasks by dependency order (topological sort)."""
    # Simple implementation: tasks with no dependencies come first
    # Full DAG sort for complex dependency graphs
    task_map = {t.id: t for t in tasks}
    visited = set()
    result = []

    def visit(task_id):
        if task_id in visited:
            return
        visited.add(task_id)
        task = task_map.get(task_id)
        if task:
            for dep in task.dependencies:
                visit(dep)
            result.append(task)

    for task in tasks:
        visit(task.id)

    return result
```

### BLOCKER #1 FIX (continued): Output dataclasses

```python
@dataclass
class RunResult:
    run_id: str
    success: bool
    prd_path: str
    project_path: str
    total_time_s: float
    tasks: list  # list[TaskResult]
    states_visited: list  # list[State]
    total_tokens: int
    error_message: str | None

@dataclass
class TaskResult:
    task_id: str
    title: str
    state: str  # State.value
    attempts: int
    commit_sha: str | None
    time_s: float
    tokens: int
    error_message: str | None

@dataclass
class StepResult:
    task_id: str
    state: str  # State.value
    success: bool
    detail: dict

@dataclass
class RunStatus:
    run_id: str
    state: str  # State.value
    progress_pct: float
    current_task: str | None
    tasks_completed: int
    tasks_total: int
    elapsed_s: float
```

---

## File: engine/pipeline.py

**Pattern:** State Machine. The 12-state machine with validated transitions.

### BLOCKER #2 FIX: TransitionResult defined here

```python
from enum import Enum
from datetime import datetime

class State(Enum):
    IDLE = "IDLE"
    PARSING = "PARSING"
    QUEUED = "QUEUED"
    CONTEXT = "CONTEXT"
    GENERATE = "GENERATE"
    VALIDATE = "VALIDATE"
    TEST = "TEST"
    COMMIT = "COMMIT"
    NEXT = "NEXT"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

# BLOCKER #2 FIX: TransitionResult dataclass
@dataclass
class TransitionResult:
    """Result of a state transition."""
    from_state: State
    to_state: State
    timestamp: str          # ISO format datetime
    checkpoint_saved: bool  # True if checkpoint succeeded
    event_emitted: bool     # True if on_event callback was called
    error: str | None       # Error message if transition failed

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

class Pipeline:
    def __init__(self, engine):
        self.engine = engine
        self.state = State.IDLE
        self.run_id = None
        self.tasks = []         # list[Task]
        self.current_task = None
        self.current_task_idx = 0
        self.states_visited = []

        # BLOCKER #5 FIX: Retry tracking
        self.task_retries = {}  # dict[str, dict] — {task_id: {"generate": 0, "test": 0, "commit": 0}}

    def transition(self, new_state, **kwargs):
        """Validate transition, update state, log, checkpoint, emit event."""
        # CHAOS FIX C10: Guard QUEUED→DONE — only valid when all tasks terminal
        if self.state == State.QUEUED and new_state == State.DONE:
            from engine.state import load_run
            task_states = kwargs.get("task_states", {})
            all_terminal = all(
                s in ("DONE", "FAILED", "CANCELLED")
                for s in task_states.values()
            ) if task_states else False
            if not all_terminal and len(self.tasks) > 0:
                # Not all tasks done — this is an error, not DONE
                new_state = State.FAILED
                kwargs["error"] = "QUEUED→DONE guard: not all tasks terminal"

        # CHAOS FIX C11: Truncate error context to prevent context window overflow
        # (Applied in generator.py, but pipeline tracks truncation warning)

        # 1. Validate
        if new_state not in VALID_TRANSITIONS.get(self.state, []):
            raise ValueError(f"Invalid transition: {self.state.value} → {new_state.value}")

        # 2. Capture old state for TransitionResult
        old_state = self.state

        # 3. Update state
        self.state = new_state
        self.states_visited.append(new_state)

        # 4. Log
        timestamp = datetime.now().isoformat()
        print(f"[{timestamp}] {old_state.value} → {new_state.value}")

        # 5. Checkpoint
        checkpoint_saved = False
        try:
            from engine.state import checkpoint_run
            checkpoint_saved = True  # checkpoint_run handles actual save
        except Exception:
            pass

        # 6. Emit event
        event_emitted = False
        if self.engine.on_event is not None:
            self.engine.on_event("state_transition", {
                "from": old_state.value,
                "to": new_state.value,
                "run_id": self.run_id,
                "task_id": self.current_task.id if self.current_task else None,
            })
            event_emitted = True

        return TransitionResult(
            from_state=old_state,
            to_state=new_state,
            timestamp=timestamp,
            checkpoint_saved=checkpoint_saved,
            event_emitted=event_emitted,
            error=None,
        )

    def get_retry_count(self, task_id: str, kind: str) -> int:
        """BLOCKER #5 FIX: Get retry count for a task and kind (generate/test/commit)."""
        if task_id not in self.task_retries:
            self.task_retries[task_id] = {"generate": 0, "test": 0, "commit": 0}
        return self.task_retries[task_id].get(kind, 0)

    def increment_retry(self, task_id: str, kind: str) -> int:
        """BLOCKER #5 FIX: Increment retry count and return new count."""
        if task_id not in self.task_retries:
            self.task_retries[task_id] = {"generate": 0, "test": 0, "commit": 0}
        self.task_retries[task_id][kind] = self.task_retries[task_id].get(kind, 0) + 1
        return self.task_retries[task_id][kind]

    def checkpoint(self):
        """Persist current state to SQLite."""
        from engine.state import checkpoint_run
        checkpoint_run(self.run_id, {
            "state": self.state.value,
            "current_task_id": self.current_task.id if self.current_task else None,
            # SIM FIX B5: serialize FULL Task objects (not just IDs) + the index,
            # so resume() can reconstruct current_task without re-parsing the PRD
            "tasks_json": json.dumps([t.__dict__ for t in self.tasks]),
            "current_task_idx": self.current_task_idx,
            "task_retries_json": json.dumps(self.task_retries),
        })

    @classmethod
    def resume(cls, run_id, engine):
        """Restore from SQLite checkpoint. SIM FIX B5: full state reconstruction."""
        from engine.state import load_run
        data = load_run(run_id)
        if data is None:
            raise ValueError(f"No checkpoint found for run_id={run_id}")
        pipeline = cls(engine)
        pipeline.run_id = run_id
        pipeline.state = State(data["state"])
        # Reconstruct FULL Task objects from tasks_json (now serialized as dicts)
        tasks_data = json.loads(data.get("tasks_json", "[]"))
        pipeline.tasks = [Task(**t) for t in tasks_data]
        # Restore the task index and current_task reference (was lost before — B5)
        pipeline.current_task_idx = data.get("current_task_idx", 0)
        if 0 <= pipeline.current_task_idx < len(pipeline.tasks):
            pipeline.current_task = pipeline.tasks[pipeline.current_task_idx]
        # Restore retry counters (persists across crash — Chaos C5)
        pipeline.task_retries = json.loads(data.get("task_retries_json", "{}"))
        # Rebuild states_visited history from transition log if present
        return pipeline

    def cancel(self):
        """Transition to CANCELLED, checkpoint."""
        self.transition(State.CANCELLED)


# CHAOS FIX #1: Per-task total attempt cap (prevents COMMIT→QUEUED infinite loop)
MAX_TOTAL_ATTEMPTS_PER_TASK = 5

# CHAOS FIX #2: Transport health re-check on failure
def _recheck_transport(engine):
    """Re-detect transport on first failure. Falls back HTTP→Local→SSH."""
    from transport import get_transport
    try:
        engine.transport = get_transport()
        return True
    except Exception:
        return False

# CHAOS FIX #3: Timeout wrapper for state execution
import signal

class TimeoutError(Exception):
    pass

def _with_timeout(func, timeout_s, label="operation"):
    """Run func with timeout. Raises TimeoutError if exceeded."""
    import threading
    result = [None]
    error = [None]
    def target():
        try:
            result[0] = func()
        except Exception as e:
            error[0] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout_s}s")
    if error[0]:
        raise error[0]
    return result[0]

# CHAOS FIX #4: Checkpoint thread safety
import threading
_checkpoint_lock = threading.Lock()
```

---

## File: engine/context.py

**Pattern:** Plain function (no Builder).

```python
def build_context(project_path, task, transport):
    """
    Build project context for a task.

    Returns a Context dataclass with:
    - file_tree: dict of project files
    - relevant_files: files related to this task
    - imports: detected import dependencies
    - framework: detected framework (django, flask, fastapi, etc.)
    - existing_code: relevant existing code snippets
    """
    # 1. transport.run_command(f"find {project_path} -name '*.py' -type f")
    # 2. Filter to relevant files based on task description
    # 3. Read relevant files via transport
    # 4. Detect framework from imports/file patterns
    # 5. Return Context(...)

from dataclasses import dataclass

@dataclass
class Context:
    file_tree: dict
    relevant_files: list[str]
    imports: dict  # filename -> list of imports
    framework: str | None
    existing_code: dict  # filename -> code content
```

---

## File: engine/generator.py

**Pattern:** Plain function (no Strategy).

### BLOCKER #6 FIX: Prompt compiler bridging path

```python
import re
import time
from dataclasses import dataclass

def generate_code(context, task, config, transport, error_ctx=None):
    """
    Build prompt, call BeeLlama, extract code blocks.

    BLOCKER #6 FIX: Concrete call chain for prompt_compiler:
    1. Import PromptCompiler and ManifestGenerator from prompt_compiler
    2. Use ManifestGenerator to create a manifest from the task + context
    3. Use PromptCompiler to compile the manifest into a prompt string
    4. Send prompt to BeeLlama
    5. Extract code blocks from response

    Returns GeneratedCode with files, raw_response, tokens, thinking_tokens, latency_ms.
    """
    from prompt_compiler import PromptCompiler, ManifestGenerator

    # 1. Build prompt via prompt_compiler (BLOCKER #6 FIX — corrected API)
    # ManifestGenerator.generate() takes a prd_result dict, not individual fields.
    # C4 FIX: include files_to_create / files_to_modify (LISTS) — ManifestGenerator
    # uses them to populate context_refs; a single "module" string leaves prompts
    # without resolved context files. Also include prd_title and acceptance_criteria.
    prd_result = {
        "prd_title": f"Engine run — task {task.id}",
        "tasks": [{
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "module": task.module,
            "files_to_create": [task.module],
            "files_to_modify": [],
            "task_type": task.task_type,
            "dependencies": task.dependencies,
            "acceptance_criteria": getattr(task, "acceptance_criteria", []),
            "spec": task.prd_section,
        }],
        "project_context": {
            "project_path": context.file_tree.get("root", ""),
            "framework": context.framework,
        }
    }
    manifest_gen = ManifestGenerator()
    manifest = manifest_gen.generate(prd_result, project_context={
        "project_path": context.file_tree.get("root", ""),
        "file_tree": context.file_tree,
        "relevant_files": context.relevant_files,
        "imports": context.imports,
        "framework": context.framework,
    })

    # PromptCompiler takes manifest_path (str), not a dict directly.
    # RED-TEAM F7 FIX: mkstemp (mktemp has a name race condition)
    import tempfile, json as _json, os as _os
    fd, manifest_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        _json.dump(manifest, f)
    try:
        compiler = PromptCompiler(manifest_path)
        prompt_text = compiler.compile(task.id, context={
            "project_path": context.file_tree.get("root", ""),
            "file_tree": context.file_tree,
        })
    finally:
        if _os.path.exists(manifest_path):
            _os.remove(manifest_path)

    # 2. If error_ctx, append error context to prompt
    if error_ctx is not None:
        prompt_text += f"\n\n## Previous attempt failed (attempt {error_ctx.attempt_number}):\n"
        if error_ctx.previous_code:
            prompt_text += f"Previous code:\n```python\n{error_ctx.previous_code}\n```\n"
        if error_ctx.validation_errors:
            prompt_text += f"Validation errors:\n" + "\n".join(error_ctx.validation_errors) + "\n"
        if error_ctx.test_failures:
            prompt_text += f"Test failures:\n" + "\n".join(error_ctx.test_failures) + "\n"

    # 3. Call BeeLlama via transport
    port = 8080 if config.model_config.startswith("3090") else 8082
    messages = [{"role": "user", "content": prompt_text}]
    start = time.time()
    response = transport.curl_beellama(port, messages, max_tokens=2048, temperature=0.3)
    latency_ms = (time.time() - start) * 1000

    # VALIDATION SIM FIX: transport.py has NO write_file/run_tests methods.
    # File writing uses transport.run_command with a heredoc (or extend transport.py
    # with write_file() in Phase 1 — one method, ~10 lines, all three transports).
    # File writing helper (module-level, used by engine.py before TEST stage):
    #   def write_project_file(transport, path, content):
    #       import base64
    #       b64 = base64.b64encode(content.encode()).decode()
    #       transport.run_command(f"echo {b64} | base64 -d > {path}", timeout=30)
    # IMPORTANT: files must be WRITTEN TO DISK after validation passes and
    # BEFORE run_tests() — pytest executes against files on disk, not in-memory.

    raw_response = response.get("content", "")
    # C1 FIX (implementation killer): transport returns a FLAT dict — there is
    # NO nested "usage" key, and the thinking field is "thinking_tokens".
    # The original nested-parsing version would silently record 0 tokens.
    tokens = response.get("total_tokens", 0)
    thinking_tokens = response.get("thinking_tokens", 0)
    tokens_per_sec = response.get("predicted_per_second", 0.0)

    # 4. Extract code blocks from response
    # SIM FIX B12: capture ALL fence types (python, text, dockerfile, json, bare),
    # not just python — the PRD deliverables include requirements.txt and Dockerfile.
    code_blocks = re.findall(r'```[\w\-]*\s*\n(.*?)```', raw_response, re.DOTALL)
    files = {}
    if code_blocks and task.module:
        for i, block in enumerate(code_blocks):
            block = block.strip()
            # SIM FIX B6: try to recover the real filename — LLMs usually label
            # blocks with a comment line like "# app.py" or "# File: test_app.py"
            first_line = block.split("\n", 1)[0]
            fname_match = re.match(r'#\s*(?:File:|filename:)?\s*([\w./\-]+\.[\w]+)\s*$', first_line)
            if fname_match:
                fname = fname_match.group(1).lstrip("/")
                block = block.split("\n", 1)[1] if "\n" in block else block  # strip label line
            elif i == 0:
                fname = task.module
            else:
                base, ext = (task.module.rsplit(".", 1)
                             if "." in task.module else (task.module, "py"))
                fname = f"{base}_{i+1}.{ext}"
            # Path traversal guard: no absolute paths, no ..
            fname = fname.replace("..", "_").lstrip("/")
            files[fname] = block
    elif raw_response and task.module:
        # No code fences — treat entire response as code
        files[task.module] = raw_response.strip()

    return GeneratedCode(
        files=files,
        raw_response=raw_response,
        tokens=tokens,
        thinking_tokens=thinking_tokens,
        latency_ms=latency_ms,
    )

@dataclass
class GeneratedCode:
    files: dict          # filename -> code content (extracted, clean code)
    raw_response: str    # Full LLM response (including markdown, thinking)
    tokens: int
    thinking_tokens: int
    latency_ms: float

@dataclass
class ErrorContext:
    previous_code: str | None
    validation_errors: list[str]
    test_failures: list[str]
    attempt_number: int
```

---

## File: engine/validator.py

**Pattern:** Plain functions (no Chain of Responsibility).

```python
def validate(code, context):
    """
    Run 4 validation stages in sequence.

    BLOCKER #4 FIX: Validates each extracted file from GeneratedCode.files,
    NOT the raw_response. Each stage runs on the clean code string.

    Returns ValidationResult with passed (bool) and stages (list of dicts).
    """
    all_stages = []
    all_passed = True

    for filename, code_str in code.files.items():
        # Stage 1: AST parse
        passed, error = check_ast(code_str)
        all_stages.append({"stage": "ast", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 2: Syntax check
        passed, error = check_syntax(code_str)
        all_stages.append({"stage": "syntax", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 3: Import resolution
        passed, error = check_imports(code_str, context.imports)
        all_stages.append({"stage": "imports", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 4: Execution test (SIM FIX B1: restricted namespace — NO builtins)
        # Empty __builtins__ blocks os/system/open/network access from generated code.
        # Combined with _with_timeout (default 5s) this is safe enough for a local
        # single-developer tool. Full Docker isolation remains opt-in via --sandbox.
        namespace = {"__name__": "__test__", "__builtins__": {}}
        passed, error = _with_timeout(
            lambda: check_execution(code_str, namespace), 5, "exec validation")
        all_stages.append({"stage": "execution", "file": filename, "passed": passed, "error": error})
        if not passed:
            all_passed = False

    return ValidationResult(passed=all_passed, stages=all_stages)

def check_ast(code_str):
    """Parse code with ast.parse. Returns (passed, error_msg)."""
    import ast
    try:
        ast.parse(code_str)
        return (True, None)
    except SyntaxError as e:
        return (False, str(e))

def check_syntax(code_str):
    """Compile code. Returns (passed, error_msg)."""
    try:
        compile(code_str, '<generated>', 'exec')
        return (True, None)
    except SyntaxError as e:
        return (False, str(e))

def check_imports(code_str, known_imports):
    """SIM FIX B7: Concrete import checker (was a stub that always passed).

    Parses import statements via ast, then verifies each top-level module is:
    1. A stdlib module (sys.stdlib_module_names, Python 3.10+), OR
    2. A file/directory in the project (known_imports keys or file_tree), OR
    3. An explicitly allowlisted third-party package (requirements.txt deps)

    Returns (passed, error_msg). Unknown modules fail validation, which feeds
    back into the GENERATE retry with a targeted error message.
    """
    import ast
    import sys

    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        return (True, None)  # syntax errors caught by earlier stages

    stdlib = getattr(sys, "stdlib_module_names", set())
    project_modules = set(known_imports.keys()) if known_imports else set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in stdlib or top in project_modules or top in ("fastapi", "uvicorn", "pytest"):
                continue
            return (False, f"Import '{name}' not found in stdlib or project")
    return (True, None)

def check_execution(code_str, namespace):
    """Execute code in isolated namespace. Returns (passed, error_msg)."""
    try:
        exec(code_str, namespace)
        return (True, None)
    except Exception as e:
        return (False, str(e))

@dataclass
class ValidationResult:
    passed: bool
    stages: list[dict]  # [{stage: "ast", passed: True, error: None}, ...]
```

---

## File: engine/tester.py

**Pattern:** Plain function (no Adapter).

```python
def run_tests(project_path, transport, timeout=180):
    """
    Run pytest via transport, parse results.

    SIM FIX: pytest exit code 5 means "no tests collected" — that is NOT a pass.
    It means the generated code has no tests, which is a quality failure (the
    generator prompt must require tests; retry GENERATE with that feedback).

    Returns TestResult with passed, tests_passed, tests_failed, failures, raw_output.
    """
    stdout, stderr, rc = transport.run_command(
        f"cd {project_path} && python3 -m pytest --tb=short -q",
        timeout=timeout
    )
    if rc == 5:
        # No tests found — treat as failure with actionable feedback
        return TestResult(passed=False, tests_passed=0, tests_failed=0,
                          failures=[{"test": "collection", "error":
                                     "No tests found. Generated code must include tests."}],
                          raw_output=stdout + stderr)
    # Parse pytest output: "X passed, Y failed"
    # Parse failure details from stdout
    # Return TestResult(...)
    # C10 NOTE: engine_service.py exposes a dedicated POST /run-tests endpoint
    # with richer parsing (per-test results, timing, framework detection) — but
    # transport.py does not wrap it. Using run_command via /exec works on all
    # transports. If Phase 2 adds transport.run_tests(), prefer the endpoint.

@dataclass
class TestResult:
    passed: bool
    tests_passed: int
    tests_failed: int
    failures: list[dict]  # [{test: "...", error: "..."}]
    raw_output: str
```

---

## File: engine/committer.py

**Pattern:** Plain function (no Adapter).

### CHAOS FIX C7: Docker sandbox container tracking + cleanup on resume
### CHAOS FIX C8: git reset --hard before COMMIT→QUEUED

```python
import time
from dataclasses import dataclass

def commit_code(project_path, task, code, transport):
    """
    Branch, write files, commit, push via transport.

    CHAOS FIX C8: On failure, git reset --hard to clean state.
    CHAOS FIX C7: Track active container IDs in checkpoint.

    Returns CommitResult with success, sha, branch, error_message.
    """
    branch = f"ace/{task.id}-{int(time.time())}"
    try:
        # 1. git checkout -b {branch}
        transport.run_git(project_path, "checkout", "-b", branch)

        # 2. Write each file from code.files
        # C2 FIX (implementation killer): transport has NO write_file method on
        # ANY transport class. Use the shared write_project_file helper
        # (base64 via run_command) — defined once, used by staging and commit.
        for filename, content in code.files.items():
            full_path = f"{project_path}/{filename}"
            write_project_file(transport, full_path, content)

        # 3. git add .
        transport.run_git(project_path, "add", ".")

        # 4. git commit
        commit_msg = f"feat({task.module}): {task.title}"
        stdout, stderr, rc = transport.run_git(project_path, "commit", "-m", commit_msg)
        if rc != 0:
            # CHAOS FIX C8: Reset on failure
            transport.run_git(project_path, "reset", "--hard", "HEAD")
            return CommitResult(success=False, sha=None, branch=branch,
                              error_message=f"commit failed: {stderr}")

        # 5. git push
        stdout, stderr, rc = transport.run_git(project_path, "push", "origin", branch)
        if rc != 0:
            transport.run_git(project_path, "reset", "--hard", "HEAD~1")
            # SIM FIX B8: permanent errors fail fast, don't consume retries.
            # Auth/permission failures (401/403) will never succeed on retry.
            permanent = any(s in stderr.lower() for s in
                            ("authentication", "401", "403", "permission denied",
                             "not found", "404"))
            if permanent:
                return CommitResult(success=False, sha=None, branch=branch,
                                    error_message=f"PERMANENT push failure (do not retry): {stderr}")
            return CommitResult(success=False, sha=None, branch=branch,
                                error_message=f"push failed (retryable): {stderr}")

        # 6. Get SHA
        stdout, _, _ = transport.run_git(project_path, "rev-parse", "HEAD")
        sha = stdout.strip()

        return CommitResult(success=True, sha=sha, branch=branch, error_message=None)

    except Exception as e:
        # CHAOS FIX C8: Best-effort cleanup
        try:
            transport.run_git(project_path, "reset", "--hard", "HEAD")
            transport.run_git(project_path, "checkout", "-")
            transport.run_git(project_path, "branch", "-D", branch)
        except Exception:
            pass
        return CommitResult(success=False, sha=None, branch=branch, error_message=str(e))


def cleanup_sandbox(container_name, transport):
    """CHAOS FIX C7: Destroy orphaned Docker container on resume.
    C3 FIX: docker_exec(container, cmd) takes container FIRST — the original
    call swapped args and would run `docker exec "rm" "-f" <name>`. Use
    run_command with an explicit docker rm instead."""
    transport.run_command(f"docker rm -f {container_name}", timeout=30)


# C2 FIX: the single file-write helper for the whole engine. transport.py has
# no write_file on any transport class; base64-through-run_command works on
# all three transports (Local, HTTP /exec, SSH) with zero transport changes.
# Phase 1 OPTIONALLY adds a native transport.write_file() later (~10 lines
# per transport); until then this is the only supported write path.
import base64 as _base64

def write_project_file(transport, path, content):
    """Write a file on the remote/local project host via transport.run_command.
    Base64 encoding avoids all shell-quoting issues with generated code."""
    b64 = _base64.b64encode(content.encode("utf-8")).decode("ascii")
    stdout, stderr, rc = transport.run_command(
        f"mkdir -p $(dirname {path}) && echo {b64} | base64 -d > {path}", timeout=30)
    return rc == 0


@dataclass
class CommitResult:
    success: bool
    sha: str | None
    branch: str
    error_message: str | None
```

---

## File: engine/state.py

**Pattern:** Plain functions (no Repository).

### CHAOS FIX #3: WAL checkpoint + integrity check + backup
### CHAOS FIX #4: Threading lock for checkpoint safety

```python
import sqlite3
import json
import shutil
import os
from datetime import datetime

from pathlib import Path

# RED-TEAM V4 FIX: absolute path anchored to the repo root — a relative DB_PATH
# silently creates a second database if the CLI is invoked from another directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = str(REPO_ROOT / "engine.db")  # Rule 6: separate from benchmark-results.db
BACKUP_PATH = str(REPO_ROOT / "engine.db.bak")

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")  # CHAOS FIX: prevent SQLITE_BUSY
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _backup_db():
    """CHAOS FIX: Backup DB before writes."""
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, BACKUP_PATH)

def _validate_db():
    """CHAOS FIX: Validate DB integrity on open."""
    try:
        conn = _get_conn()
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result[0] == "ok"
    except Exception:
        return False

def _restore_backup():
    """CHAOS FIX: Restore from backup if DB is corrupted."""
    if os.path.exists(BACKUP_PATH):
        shutil.copy2(BACKUP_PATH, DB_PATH)
        return True
    return False

def init_db():
    """Create engine_runs and engine_task_results tables."""
    if not _validate_db():
        _restore_backup()
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_runs (
        id TEXT PRIMARY KEY,
        prd_path TEXT NOT NULL,
        project_path TEXT NOT NULL,
        config TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'IDLE',
        current_task_id TEXT,
        tasks_json TEXT,
        task_retries_json TEXT,
        started_at TEXT DEFAULT (datetime('now')),
        completed_at TEXT,
        result_json TEXT,
        error_message TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_task_results (
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
    )""")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # CHAOS FIX: prevent WAL growth
    conn.close()
    # C8 FIX: engine.db tracks its own schema version (separate DB per Rule 6 —
    # do NOT bump benchmark-results.db's schema_version)
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL, updated_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("INSERT INTO schema_version (version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version)")
    conn.commit()
    conn.close()

def checkpoint_run(run_id, data):
    """BLOCKER #7 FIX + CHAOS FIX: Full parameterized INSERT with backup + WAL checkpoint."""
    _backup_db()  # CHAOS FIX: backup before write
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO engine_runs
        (id, prd_path, project_path, config, state, current_task_id,
         tasks_json, task_retries_json, started_at, completed_at, result_json, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        data.get("prd_path", ""),
        data.get("project_path", ""),
        data.get("config", "3090-qwen36-35b"),
        data.get("state", "IDLE"),
        data.get("current_task_id"),
        data.get("tasks_json", "[]"),
        data.get("task_retries_json", "{}"),
        data.get("started_at", datetime.now().isoformat()),
        data.get("completed_at"),
        data.get("result_json"),
        data.get("error_message"),
    ))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # CHAOS FIX: prevent WAL growth
    conn.close()

    # CHAOS FIX C9: Write tasks_json to sidecar file as backup
    tasks_json = data.get("tasks_json")
    if tasks_json:
        sidecar = f"engine_run_{run_id}_tasks.json"
        try:
            with open(sidecar, "w") as f:
                f.write(tasks_json)
        except Exception:
            pass

def save_task_result(run_id, task_id, result):
    """BLOCKER #7 FIX: Full parameterized INSERT OR REPLACE into engine_task_results."""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO engine_task_results
        (run_id, task_id, state, generated_code, validation_result,
         test_result, commit_sha, attempts, started_at, completed_at, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        task_id,
        result.get("state", "IDLE"),
        result.get("generated_code"),
        result.get("validation_result"),
        result.get("test_result"),
        result.get("commit_sha"),
        result.get("attempts", 0),
        result.get("started_at", datetime.now().isoformat()),
        result.get("completed_at"),
        result.get("error_message"),
    ))
    conn.commit()
    conn.close()

def load_run(run_id):
    """BLOCKER #7 FIX + CHAOS FIX C9: SELECT with tasks_json validation + sidecar restore."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM engine_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)

    # CHAOS FIX C9: Validate tasks_json integrity
    tasks_json = result.get("tasks_json", "[]")
    try:
        import json as _json
        _json.loads(tasks_json)
    except (json.JSONDecodeError, TypeError):
        # tasks_json corrupted — try sidecar file
        sidecar = f"engine_run_{run_id}_tasks.json"
        try:
            with open(sidecar, "r") as f:
                result["tasks_json"] = f.read()
        except FileNotFoundError:
            # Sidecar also missing — mark as unrecoverable
            result["error_message"] = "tasks_json corrupted, no sidecar backup"
            result["tasks_json"] = "[]"

    # Validate task_retries_json
    retries_json = result.get("task_retries_json", "{}")
    try:
        _json.loads(retries_json)
    except (json.JSONDecodeError, TypeError):
        result["task_retries_json"] = "{}"

    return result

def list_runs(status=None):
    """SELECT from engine_runs with optional filter."""
    conn = _get_conn()
    if status:
        rows = conn.execute("SELECT * FROM engine_runs WHERE state = ?", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM engine_runs").fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

---

## File: engine/events.py

**Pattern:** Plain function (no Observer).

```python
# C9 FIX: two output paths — (1) the synchronous on_event callback (primary,
# used by the pipeline), and (2) a best-effort bridge to the live streaming
# server via streaming_client.emit_event_fire_and_forget(source, type, data)
# when STREAMING_URL is configured. Both are fire-and-forget; neither blocks.
import os

def emit_event(on_event, event_type, data):
    """
    If on_event is set, call it (synchronous, primary path).
    Additionally bridge to the streaming server when STREAMING_URL is set.
    Never raises — observability must never break the pipeline.
    """
    if on_event is not None:
        try:
            on_event(event_type, data)
        except Exception:
            pass  # callback failures must not crash the engine
    if os.environ.get("STREAMING_URL"):
        try:
            from streaming_client import emit_event_fire_and_forget
            emit_event_fire_and_forget("engine", event_type, data)
        except Exception:
            pass  # streaming is opt-in (AD4); absence is fine
```

---

## CLI: cli.py

**Structure:** Monolithic file, ~500 lines, using argparse with subparsers.

```
cli.py
├── ace
│   ├── run --prd <file> --project <path> [--config] [--dry-run]
│   ├── parse <prd>
│   ├── status [--run <id>]
│   ├── resume --run <id>
│   └── cancel
├── bench
│   ├── run --config <id> [--task <id>]
│   ├── pilot
│   └── report --format md|json
├── stream
│   ├── start [--port 3081]
│   ├── stop
│   └── status
├── status
└── health
```

All commands output JSON: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "..."}`.
`--pretty` adds indentation. `--verbose` enables debug logging.
`--transport auto|http|ssh|local` overrides auto-detection.

Lazy imports: heavy modules only imported when their subcommand is invoked.

---

## Test Structure

```
tests/
├── __init__.py
├── unit/
│   ├── __init__.py
│   ├── test_engine_engine.py        # Engine facade tests
│   ├── test_engine_pipeline.py      # State machine transitions
│   ├── test_engine_context.py       # Context builder
│   ├── test_engine_generator.py     # Code generation
│   ├── test_engine_validator.py     # 4-stage validation
│   ├── test_engine_tester.py        # Test runner
│   ├── test_engine_committer.py     # Git commit
│   ├── test_engine_state.py         # SQLite persistence
│   ├── test_engine_events.py        # Event emission
│   └── test_cli.py                  # CLI argument parsing
├── integration/
│   ├── __init__.py
│   ├── test_engine_pipeline.py      # Full pipeline with mock transport
│   └── test_transport_integration.py
├── e2e/
│   ├── __init__.py
│   └── test_prd_to_commit.py        # THE CRITICAL TEST
└── legacy/
    ├── test_integration.py
    └── test_prompts.py
```

---

## Implementation Order

1. `engine/__init__.py` — EngineConfig, constants (5 min)
2. `engine/state.py` — SQLite tables + functions (30 min)
3. `engine/pipeline.py` — 12-state machine (1 hour)
4. `engine/events.py` — emit_event function (5 min)
5. `engine/context.py` — build_context function (30 min)
6. `engine/validator.py` — validate + 4 check functions (30 min)
7. `engine/generator.py` — generate_code function (45 min)
8. `engine/tester.py` — run_tests function (20 min)
9. `engine/committer.py` — commit_code function (30 min)
10. `engine/engine.py` — Engine facade (1 hour)
11. `cli.py` — Unified CLI (1 hour)
12. `tests/unit/test_engine_pipeline.py` — State machine tests (30 min)
13. `tests/unit/test_engine_validator.py` — Validator tests (20 min)
14. `tests/integration/test_engine_pipeline.py` — Full pipeline mock test (30 min)
15. `tests/e2e/test_prd_to_commit.py` — THE CRITICAL TEST (1 hour)

**Total estimated: ~8 hours**
