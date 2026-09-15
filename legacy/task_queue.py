#!/usr/bin/env python3
"""Task Queue + Orchestration — the brain of the Autonomous Coding Engine.

Chains: parse → decompose → execute → track.

Accepts a PRD (or pre-parsed task list), resolves dependencies via topological
sort, executes tasks through the full pipeline (generate → test → quality gate
→ commit), and produces structured execution reports.

All heavy modules (code_generator, quality_gates, git_workflow, context_manager,
test_runner) are imported lazily so the queue works standalone for planning and
dry-run even when those modules are not yet implemented.

Usage:
    python3 task_queue.py load test-prd.md              # Load PRD tasks
    python3 task_queue.py load tasks.json --tasks        # Load pre-parsed JSON
    python3 task_queue.py status                         # Show queue status
    python3 task_queue.py next                           # Show next ready task
    python3 task_queue.py execute --task T01             # Execute single task
    python3 task_queue.py execute-all                    # Execute all tasks
    python3 task_queue.py execute-all --dry-run          # Plan without executing
    python3 task_queue.py report                         # Generate execution report
    python3 task_queue.py report --format json           # JSON report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"
DEFAULT_CONFIG = "3090-qwen36-35b"
MAX_RETRIES = 3

# Task states
STATE_PENDING = "pending"
STATE_READY = "ready"
STATE_RUNNING = "running"
STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
STATE_BLOCKED = "blocked"
STATE_SKIPPED = "skipped"

# ---------------------------------------------------------------------------
# Lazy imports — these modules may not exist yet
# ---------------------------------------------------------------------------


def _lazy_import(module_name: str, class_name: str = None):
    """Import a module lazily, returning the module or a class from it.

    Returns None if the module is not available.  Prints a warning when
    verbose mode is active (controlled by the global ``VERBOSE`` flag).
    """
    try:
        mod = __import__(module_name)
        if class_name:
            return getattr(mod, class_name)
        return mod
    except ImportError as e:
        if VERBOSE:
            print(f"  [lazy-import] {module_name} not available: {e}", file=sys.stderr)
        return None


VERBOSE = False


# ---------------------------------------------------------------------------
# Streaming event emission (fire-and-forget)
# ---------------------------------------------------------------------------

from streaming_client import emit_event_fire_and_forget as _emit


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------


def _task_id_sort_key(task: dict) -> str:
    """Stable sort key for tasks by their ID (e.g. T01, T02, …)."""
    tid = task.get("id", "")
    # Strip leading 'T' and convert to int for proper numeric sort
    try:
        return int(tid.lstrip("Tt"))
    except (ValueError, AttributeError):
        return tid


def _validate_task(task: dict, index: int = 0) -> List[str]:
    """Return a list of validation errors for a task dict."""
    errors = []
    if not task.get("id"):
        errors.append(f"Task #{index}: missing 'id'")
    if not task.get("title"):
        errors.append(f"Task #{index}: missing 'title'")
    deps = task.get("dependencies", [])
    if not isinstance(deps, list):
        errors.append(f"Task #{index}: 'dependencies' must be a list")
    return errors


# ============================================================================
# TaskQueue
# ============================================================================


class TaskQueue:
    """Orchestration brain for the Autonomous Coding Engine.

    Maintains a DAG of tasks with dependency tracking, executes them through
    the full pipeline (generate → test → quality gate → commit), and produces
    structured reports.

    Parameters
    ----------
    host : str
        Triton SSH host (default: ``<LAN_IP>``).
    user : str
        Triton SSH user (default: ``<user>``).
    config : str
        Model config ID for inference (default: ``3090-qwen36-35b``).
    project_path : str
        Working directory on Triton for code generation.
    dry_run : bool
        If True, plan execution without actually running anything.
    verbose : bool
        If True, print detailed progress information.
    """

    def __init__(
        self,
        host: str = TRITON_HOST,
        user: str = TRITON_USER,
        config: str = DEFAULT_CONFIG,
        project_path: str = "/home/<user>/projects",
        dry_run: bool = False,
        verbose: bool = False,
    ):
        self.host = host
        self.user = user
        self.config = config
        self.project_path = project_path
        self.dry_run = dry_run
        self.verbose = verbose

        # Task lists — the core state
        self.tasks: List[dict] = []          # All loaded tasks (pending + running)
        self.completed: List[dict] = []      # Successfully finished tasks
        self.failed: List[dict] = []         # Tasks that errored out
        self.skipped: List[dict] = []        # Tasks skipped (e.g. dependency failed)
        self.current_task: Optional[dict] = None

        # Execution metadata
        self.execution_log: List[dict] = []  # Timeline of events
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.prd_title: Optional[str] = None
        self.prd_metadata: Optional[dict] = None

        # Retry counters per task ID
        self._retry_counts: Dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_prd(self, prd_path: str) -> dict:
        """Parse a markdown PRD and load its tasks into the queue.

        Uses :class:`PRDParser` from ``prd_parser.py`` when available.
        Falls back to a lightweight built-in parser for basic markdown.

        Returns
        -------
        dict
            ``{"prd_title": ..., "tasks": [...], "metadata": {...}}``
        """
        prd_path = os.path.abspath(prd_path)
        if not os.path.isfile(prd_path):
            raise FileNotFoundError(f"PRD file not found: {prd_path}")

        self._log_event("prd_load", {"path": prd_path})

        # Try the dedicated parser first
        PRDParser = _lazy_import("prd_parser", "PRDParser")
        if PRDParser is not None:
            parser = PRDParser()
            result = parser.parse(prd_path)
        else:
            result = self._builtin_parse_prd(prd_path)

        self.prd_title = result.get("prd_title", "Untitled")
        self.prd_metadata = result.get("metadata", {})
        tasks = result.get("tasks", [])

        if self.verbose:
            print(f"  Parsed PRD: {self.prd_title}")
            print(f"  Tasks found: {len(tasks)}")

        self.load_tasks(tasks)
        return result

    def load_tasks(self, tasks: list):
        """Load pre-parsed task dicts into the queue.

        Each task must have at minimum ``id`` and ``title``.  Optional:
        ``dependencies`` (list of task IDs), ``files_to_create``,
        ``files_to_modify``, ``acceptance_criteria``, ``complexity``,
        ``estimated_tokens``, ``category``, ``description``.

        Parameters
        ----------
        tasks : list[dict]
            Task dictionaries.
        """
        # Validate
        all_errors = []
        for i, t in enumerate(tasks):
            all_errors.extend(_validate_task(t, i))
        if all_errors:
            raise ValueError(
                "Task validation failed:\n" + "\n".join(f"  • {e}" for e in all_errors)
            )

        # Check for duplicate IDs
        seen_ids = set()
        for t in tasks:
            tid = t["id"]
            if tid in seen_ids:
                raise ValueError(f"Duplicate task ID: {tid}")
            seen_ids.add(tid)

        # Normalize tasks with defaults
        for t in tasks:
            t.setdefault("dependencies", [])
            t.setdefault("files_to_create", [])
            t.setdefault("files_to_modify", [])
            t.setdefault("acceptance_criteria", [])
            t.setdefault("complexity", "medium")
            t.setdefault("estimated_tokens", 3000)
            t.setdefault("category", "logic")
            t.setdefault("description", t.get("title", ""))
            t.setdefault("status", STATE_PENDING)

        self.tasks = sorted(tasks, key=_task_id_sort_key)

        # Validate dependency references
        valid_ids = {t["id"] for t in self.tasks}
        for t in self.tasks:
            for dep in t.get("dependencies", []):
                if dep not in valid_ids:
                    raise ValueError(
                        f"Task {t['id']} depends on unknown task '{dep}'"
                    )

        # Check for cycles
        if self._has_cycle():
            raise ValueError("Dependency graph contains a cycle")

        self._log_event("tasks_loaded", {"count": len(self.tasks)})

        if self.verbose:
            print(f"  Loaded {len(self.tasks)} tasks")
            for t in self.tasks:
                deps = t.get("dependencies", [])
                dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
                print(f"    {t['id']}: {t['title']}{dep_str}")

    def load_tasks_from_json(self, json_path: str) -> dict:
        """Load tasks from a JSON file (output of prd_parser.py).

        The JSON should have a top-level ``"tasks"`` key with a list of task
        dicts, or be a bare list of task dicts.

        Returns
        -------
        dict
            The parsed JSON structure.
        """
        json_path = os.path.abspath(json_path)
        if not os.path.isfile(json_path):
            raise FileNotFoundError(f"JSON file not found: {json_path}")

        with open(json_path) as f:
            data = json.load(f)

        if isinstance(data, dict):
            tasks = data.get("tasks", [])
            self.prd_title = data.get("prd_title")
            self.prd_metadata = data.get("metadata")
        elif isinstance(data, list):
            tasks = data
        else:
            raise ValueError(f"Expected dict or list in {json_path}, got {type(data)}")

        self.load_tasks(tasks)
        return data

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    def next_task(self) -> Optional[dict]:
        """Get the next ready task (no unmet dependencies).

        Uses topological ordering: a task is ready when all its declared
        dependencies appear in ``self.completed``.

        Returns
        -------
        dict or None
            The next ready task, or ``None`` if no tasks are available.
        """
        completed_ids = {t["id"] for t in self.completed}
        failed_ids = {t["id"] for t in self.failed}
        skipped_ids = {t["id"] for t in self.skipped}
        blocked_ids = completed_ids | failed_ids | skipped_ids

        for task in self.tasks:
            if task["id"] in blocked_ids:
                continue

            deps = task.get("dependencies", [])
            if all(d in completed_ids for d in deps):
                return task

        return None

    def get_ready_tasks(self) -> List[dict]:
        """Return all currently ready tasks (dependency-free or deps met)."""
        ready = []
        completed_ids = {t["id"] for t in self.completed}
        failed_ids = {t["id"] for t in self.failed}
        skipped_ids = {t["id"] for t in self.skipped}
        blocked_ids = completed_ids | failed_ids | skipped_ids

        for task in self.tasks:
            if task["id"] in blocked_ids:
                continue
            deps = task.get("dependencies", [])
            if all(d in completed_ids for d in deps):
                ready.append(task)

        return ready

    def get_blocked_tasks(self) -> List[dict]:
        """Return tasks that are blocked by unmet dependencies."""
        completed_ids = {t["id"] for t in self.completed}
        done_ids = completed_ids | {t["id"] for t in self.failed} | {t["id"] for t in self.skipped}

        blocked = []
        for task in self.tasks:
            if task["id"] in done_ids:
                continue
            deps = task.get("dependencies", [])
            if not all(d in completed_ids for d in deps):
                blocked.append(task)
        return blocked

    def topological_order(self) -> List[dict]:
        """Return all tasks in a valid topological execution order.

        Raises ValueError if the dependency graph contains a cycle.

        Returns
        -------
        list[dict]
            Tasks ordered so dependencies always come first.
        """
        if self._has_cycle():
            raise ValueError("Dependency graph contains a cycle — cannot compute topological order")

        # Kahn's algorithm
        in_degree = {t["id"]: 0 for t in self.tasks}
        dependents = defaultdict(list)  # dep -> [tasks that depend on it]
        task_map = {t["id"]: t for t in self.tasks}

        for t in self.tasks:
            for dep in t.get("dependencies", []):
                in_degree[t["id"]] += 1
                dependents[dep].append(t["id"])

        queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
        order = []

        while queue:
            tid = queue.popleft()
            order.append(task_map[tid])
            for dependent_id in dependents[tid]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        return order

    def _has_cycle(self) -> bool:
        """Detect cycles in the dependency graph using DFS."""
        task_map = {t["id"]: t for t in self.tasks}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {t["id"]: WHITE for t in self.tasks}

        def dfs(node_id):
            color[node_id] = GRAY
            for dep in task_map[node_id].get("dependencies", []):
                if dep in color:
                    if color[dep] == GRAY:
                        return True  # cycle
                    if color[dep] == WHITE and dfs(dep):
                        return True
            color[node_id] = BLACK
            return False

        return any(dfs(tid) for tid, c in color.items() if c == WHITE)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_task(self, task: dict) -> dict:
        """Execute a single task through the full pipeline.

        Pipeline steps:
          1. Build context (context_manager)
          2. Generate code (code_generator) with retry
          3. Quality check (quality_gates)
          4. Run tests (test_runner)
          5. Git commit (git_workflow)

        Each step is attempted via lazy import.  Missing modules are skipped
        with a warning, allowing the queue to function for planning/dry-run
        even when only some modules exist.

        Parameters
        ----------
        task : dict
            The task to execute.

        Returns
        -------
        dict
            Execution result with keys: ``task_id``, ``status``, ``duration_s``,
            ``steps``, ``error`` (if failed).
        """
        task_id = task["id"]
        title = task.get("title", "untitled")
        print(f"\n{'─' * 60}")
        print(f"Executing: {task_id} — {title}")
        print(f"{'─' * 60}")

        if self.dry_run:
            return self._dry_run_task(task)

        self.current_task = task
        task["status"] = STATE_RUNNING
        self._log_event("task_start", {"task_id": task_id, "title": title})
        try:
            _emit("engine", "task.start", {
                "task_id": task_id,
                "title": title,
                "description": task.get("description", ""),
            })
        except Exception:
            pass

        result = {
            "task_id": task_id,
            "title": title,
            "status": STATE_COMPLETE,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "duration_s": 0.0,
            "steps": {},
            "error": None,
            "retries": 0,
        }

        start = time.monotonic()

        try:
            # Step 1: Build context
            context = self._step_build_context(task)
            result["steps"]["context"] = context

            # Step 2: Generate code (with retry)
            gen_result = self._step_generate_code(task, context)
            result["steps"]["generate"] = gen_result

            # Step 3: Quality gates
            quality_result = self._step_quality_check(task, gen_result)
            result["steps"]["quality"] = quality_result

            # Step 4: Run tests
            test_result = self._step_run_tests(task, gen_result)
            result["steps"]["tests"] = test_result

            # Step 5: Git commit
            commit_result = self._step_git_commit(task, gen_result)
            result["steps"]["commit"] = commit_result

            # Check overall status
            gen_status = gen_result.get("status", "ok")
            quality_status = quality_result.get("status", "pass")

            if gen_status == "error":
                # Generation failed — nothing was produced, so the task
                # cannot be considered complete regardless of what later
                # gates report.
                result["status"] = STATE_FAILED
                result["error"] = (
                    f"Code generation failed: "
                    f"{gen_result.get('error', 'unknown error')}"
                )
            elif quality_status == "fail":
                result["status"] = STATE_FAILED
                result["error"] = f"Quality gate failed: {quality_result.get('reason', 'unknown')}"
            elif quality_status == "skip":
                # Quality gates had nothing to check but generation
                # reported success with files — treat as a warning, not
                # a hard failure, so we don't block downstream work on
                # a cosmetic issue.
                if self.verbose:
                    print(f"  ⚠ {task_id}: quality gates skipped — {quality_result.get('reason', '')}")
            elif test_result.get("status") == "fail":
                result["status"] = STATE_FAILED
                result["error"] = f"Tests failed: {test_result.get('summary', 'unknown')}"
            else:
                task["status"] = STATE_COMPLETE
                self.completed.append(task)
                print(f"  ✅ {task_id} completed successfully")
                try:
                    _emit("engine", "task.complete", {
                        "task_id": task_id,
                        "duration_s": round(time.monotonic() - start, 2),
                    })
                except Exception:
                    pass

        except Exception as e:
            result["status"] = STATE_FAILED
            result["error"] = str(e)
            task["status"] = STATE_FAILED
            self.failed.append(task)
            print(f"  ❌ {task_id} failed: {e}")
            try:
                _emit("engine", "task.fail", {
                    "task_id": task_id,
                    "error": str(e),
                })
            except Exception:
                pass
            if self.verbose:
                traceback.print_exc()

        finally:
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            result["duration_s"] = round(time.monotonic() - start, 2)
            self.current_task = None
            self._log_event("task_complete", {
                "task_id": task_id,
                "status": result["status"],
                "duration_s": result["duration_s"],
                "error": result["error"],
            })

        return result

    def execute_all(self) -> dict:
        """Execute all tasks in dependency order.

        Processes tasks topologically, executing ready tasks as they become
        available.  Stops early if a critical dependency fails.

        Returns
        -------
        dict
            Summary with keys: ``total``, ``completed``, ``failed``, ``skipped``,
            ``duration_s``, ``results``.
        """
        self.start_time = time.monotonic()

        try:
            _emit("engine", "pipeline.start", {
                "prd_title": self.prd_title or "Untitled",
                "total_tasks": len(self.tasks),
            })
        except Exception:
            pass

        if self.dry_run:
            print("\n" + "=" * 60)
            print("DRY RUN — Execution Plan")
            print("=" * 60)
        else:
            print("\n" + "=" * 60)
            print("EXECUTING ALL TASKS")
            print("=" * 60)

        total = len(self.tasks)
        results = []
        iteration = 0
        max_iterations = total * 2  # Safety: prevent infinite loops

        while iteration < max_iterations:
            iteration += 1
            ready = self.get_ready_tasks()

            if not ready:
                # Check if we're done or stuck
                remaining = self.get_blocked_tasks()
                if not remaining:
                    break  # All done
                else:
                    # All remaining tasks are blocked by failed deps
                    for t in remaining:
                        t["status"] = STATE_SKIPPED
                        self.skipped.append(t)
                        self._log_event("task_skipped", {
                            "task_id": t["id"],
                            "reason": "dependency failed",
                        })
                        if self.verbose:
                            print(f"  ⏭ {t['id']} skipped (dependency failed)")
                    break

            # Execute each ready task (sequentially for safety)
            for task in ready:
                result = self.execute_task(task)
                results.append(result)

                # In dry-run mode, simulate completion so the loop advances
                if result["status"] == "dry_run":
                    task["status"] = STATE_COMPLETE
                    self.completed.append(task)

                # If this task failed and others depend on it, stop
                elif result["status"] == STATE_FAILED:
                    dep_failures = self._propagate_failure(task["id"])
                    if dep_failures and self.verbose:
                        print(f"  ⚠ Blocked {len(dep_failures)} downstream tasks")

        self.end_time = time.monotonic()
        duration = round(self.end_time - self.start_time, 2)

        summary = {
            "total": total,
            "completed": len(self.completed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "pending": len([t for t in self.tasks if t["status"] == STATE_PENDING]),
            "duration_s": duration,
            "results": results,
        }

        self._log_event("execute_all_complete", {
            "total": total,
            "completed": summary["completed"],
            "failed": summary["failed"],
            "skipped": summary["skipped"],
            "duration_s": duration,
        })

        try:
            _emit("engine", "pipeline.complete", {
                "total": total,
                "completed": summary["completed"],
                "failed": summary["failed"],
                "duration_s": duration,
            })
        except Exception:
            pass

        # Print summary
        print(f"\n{'=' * 60}")
        print("EXECUTION SUMMARY")
        print(f"{'=' * 60}")
        print(f"  Total:     {summary['total']}")
        print(f"  Completed: {summary['completed']} ✅")
        print(f"  Failed:    {summary['failed']} ❌")
        print(f"  Skipped:   {summary['skipped']} ⏭")
        print(f"  Pending:   {summary['pending']} ⏳")
        print(f"  Duration:  {summary['duration_s']}s")
        print(f"{'=' * 60}")

        return summary

    # ------------------------------------------------------------------
    # Pipeline steps (delegated to other modules)
    # ------------------------------------------------------------------

    def _step_build_context(self, task: dict) -> dict:
        """Step 1: Build context for the task.

        Uses ``context_manager`` if available; otherwise returns a minimal
        context dict with the task definition itself.
        """
        if self.verbose:
            print("  [1/5] Building context...")

        ContextManager = _lazy_import("context_manager", "ContextManager")
        if ContextManager is not None:
            try:
                cm = ContextManager(host=self.host, user=self.user)
                context = cm.get_context_for_task(task)
                if self.verbose:
                    print(f"    Context built: {len(context.get('files', []))} files")
                return {"status": "ok", "source": "context_manager", "data": context}
            except Exception as e:
                return {"status": "error", "source": "context_manager", "error": str(e)}

        # Fallback: minimal context from task definition
        context = {
            "task_id": task["id"],
            "title": task.get("title", ""),
            "description": task.get("description", ""),
            "files_to_create": task.get("files_to_create", []),
            "files_to_modify": task.get("files_to_modify", []),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "category": task.get("category", "logic"),
        }
        return {"status": "ok", "source": "builtin", "data": context}

    def _step_generate_code(self, task: dict, context: dict) -> dict:
        """Step 2: Generate code for the task (with retry).

        Uses ``code_generator`` if available.  Retries up to ``MAX_RETRIES``
        times on failure.
        """
        if self.verbose:
            print("  [2/5] Generating code...")

        CodeGenerator = _lazy_import("code_generator", "CodeGenerator")
        if CodeGenerator is not None:
            last_error = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    gen = CodeGenerator(host=self.host, user=self.user, config=self.config)
                    result = gen.generate(task, context)
                    if self.verbose:
                        files = result.get("files", [])
                        print(f"    Generated {len(files)} file(s) on attempt {attempt}")
                    return {
                        "status": "ok",
                        "source": "code_generator",
                        "attempt": attempt,
                        "data": result,
                    }
                except Exception as e:
                    last_error = e
                    if self.verbose:
                        print(f"    Attempt {attempt} failed: {e}")

            return {
                "status": "error",
                "source": "code_generator",
                "error": f"All {MAX_RETRIES} attempts failed: {last_error}",
            }

        # Fallback: placeholder generation
        return {
            "status": "ok",
            "source": "placeholder",
            "data": {
                "files": [],
                "message": "code_generator not available — placeholder output",
                "task_id": task["id"],
            },
        }

    def _step_quality_check(self, task: dict, gen_result: dict) -> dict:
        """Step 3: Run quality gates on generated code.

        Uses ``quality_gates`` if available.

        Precondition: if the generate step failed or produced no files,
        quality gates are **skipped** with status ``"skip"`` and a clear
        reason string so that downstream steps (and the final result
        aggregator) can distinguish "nothing to check" from "passed".
        """
        if self.verbose:
            print("  [3/5] Running quality gates...")

        # ------------------------------------------------------------------
        # Guard: skip if the generate step failed or has no files
        # ------------------------------------------------------------------
        gen_status = gen_result.get("status", "error")
        gen_files = gen_result.get("data", {}).get("files", [])
        if gen_status == "error" or not gen_files:
            reason = (
                f"Generation step status={gen_status}, "
                f"files={len(gen_files)} — nothing to quality-check."
            )
            if self.verbose:
                print(f"    Quality gate: SKIPPED ({reason})")
            return {
                "status": "skip",
                "source": "quality_gates",
                "reason": reason,
            }

        QualityGates = _lazy_import("quality_gates", "QualityGates")
        if QualityGates is not None:
            try:
                gates = QualityGates()
                project_path = gen_result.get("data", {}).get("project_path", self.project_path)
                result = gates.check_all(project_path, gen_files, task_id=task["id"])
                overall = result.get("overall", "pass")
                if self.verbose:
                    print(f"    Quality gate: {overall}")
                return {"status": overall, "source": "quality_gates", "data": result}
            except Exception as e:
                return {"status": "warn", "source": "quality_gates", "error": str(e)}

        # Fallback: skip if no quality gates available
        return {"status": "skip", "source": "skipped", "reason": "quality_gates not available"}

    def _step_run_tests(self, task: dict, gen_result: dict) -> dict:
        """Step 4: Run tests for the generated code.

        Uses ``test_runner`` if available.
        """
        if self.verbose:
            print("  [4/5] Running tests...")

        TestRunner = _lazy_import("test_runner", "TestRunner")
        if TestRunner is not None:
            try:
                runner = TestRunner(host=self.host, user=self.user)
                project_path = gen_result.get("data", {}).get("project_path", self.project_path)
                result = runner.run(project_path)
                passed = result.get("passed", 0)
                failed = result.get("failed", 0)
                total = passed + failed
                if self.verbose:
                    print(f"    Tests: {passed}/{total} passed")
                status = "pass" if failed == 0 else "fail"
                return {"status": status, "source": "test_runner", "data": result}
            except Exception as e:
                return {"status": "warn", "source": "test_runner", "error": str(e)}

        # Fallback: no tests available
        return {"status": "pass", "source": "skipped", "reason": "test_runner not available"}

    def _step_git_commit(self, task: dict, gen_result: dict) -> dict:
        """Step 5: Commit generated code to git.

        Uses ``git_workflow`` if available.
        """
        if self.verbose:
            print("  [5/5] Committing to git...")

        GitWorkflow = _lazy_import("git_workflow", "GitWorkflow")
        if GitWorkflow is not None:
            try:
                gw = GitWorkflow(host=self.host, user=self.user)
                message = f"feat({task['id']}): {task.get('title', 'task implementation')}"
                result = gw.commit_all(self.project_path, message)
                if self.verbose:
                    print(f"    Committed: {result.get('commit_sha', 'unknown')[:8]}")
                return {"status": "ok", "source": "git_workflow", "data": result}
            except Exception as e:
                return {"status": "error", "source": "git_workflow", "error": str(e)}

        # Fallback: no git workflow available
        return {"status": "ok", "source": "skipped", "reason": "git_workflow not available"}

    # ------------------------------------------------------------------
    # Dry-run helpers
    # ------------------------------------------------------------------

    def _dry_run_task(self, task: dict) -> dict:
        """Produce a dry-run plan for a single task without executing it."""
        task_id = task["id"]
        title = task.get("title", "untitled")
        deps = task.get("dependencies", [])

        print(f"  📋 {task_id}: {title}")
        if deps:
            print(f"     Dependencies: {', '.join(deps)}")
        print(f"     Category: {task.get('category', 'logic')}")
        print(f"     Complexity: {task.get('complexity', 'medium')}")
        print(f"     Est. tokens: {task.get('estimated_tokens', 3000)}")
        if task.get("files_to_create"):
            print(f"     Files to create: {', '.join(task['files_to_create'])}")
        if task.get("files_to_modify"):
            print(f"     Files to modify: {', '.join(task['files_to_modify'])}")

        # Show which modules are available
        modules = {
            "context_manager": _lazy_import("context_manager"),
            "code_generator": _lazy_import("code_generator"),
            "quality_gates": _lazy_import("quality_gates"),
            "test_runner": _lazy_import("test_runner"),
            "git_workflow": _lazy_import("git_workflow"),
        }
        available = [name for name, mod in modules.items() if mod is not None]
        missing = [name for name, mod in modules.items() if mod is None]
        if available:
            print(f"     Modules available: {', '.join(available)}")
        if missing:
            print(f"     Modules missing: {', '.join(missing)}")

        return {
            "task_id": task_id,
            "title": title,
            "status": "dry_run",
            "duration_s": 0.0,
            "steps": {},
            "error": None,
            "retries": 0,
        }

    # ------------------------------------------------------------------
    # Failure propagation
    # ------------------------------------------------------------------

    def _propagate_failure(self, failed_task_id: str) -> List[str]:
        """Mark all tasks transitively dependent on ``failed_task_id`` as skipped.

        Returns the list of task IDs that were skipped.
        """
        skipped = []
        failed_ids = {t["id"] for t in self.failed}

        # BFS from the failed task through dependents
        dependents = defaultdict(list)
        task_map = {t["id"]: t for t in self.tasks}
        for t in self.tasks:
            for dep in t.get("dependencies", []):
                dependents[dep].append(t["id"])

        queue = deque(dependents.get(failed_task_id, []))
        visited = set()

        while queue:
            tid = queue.popleft()
            if tid in visited or tid in failed_ids:
                continue
            visited.add(tid)

            task = task_map.get(tid)
            if task is None:
                continue

            # Check if ALL dependencies are failed/skipped
            deps = task.get("dependencies", [])
            completed_ids = {t["id"] for t in self.completed}
            if all(d in failed_ids or d in failed_ids for d in deps):
                task["status"] = STATE_SKIPPED
                self.skipped.append(task)
                skipped.append(tid)
                queue.extend(dependents.get(tid, []))

        return skipped

    # ------------------------------------------------------------------
    # Status and reporting
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Get current queue status.

        Returns
        -------
        dict
            ``{total, completed, failed, skipped, pending, ready, blocked,
            current_task, elapsed_s}``
        """
        completed_ids = {t["id"] for t in self.completed}
        failed_ids = {t["id"] for t in self.failed}
        skipped_ids = {t["id"] for t in self.skipped}
        done_ids = completed_ids | failed_ids | skipped_ids

        pending = [t for t in self.tasks if t["id"] not in done_ids]
        ready = self.get_ready_tasks()
        blocked = self.get_blocked_tasks()

        elapsed = None
        if self.start_time:
            end = self.end_time or time.monotonic()
            elapsed = round(end - self.start_time, 2)

        return {
            "total": len(self.tasks),
            "completed": len(self.completed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "pending": len(pending),
            "ready": len(ready),
            "blocked": len(blocked),
            "current_task": self.current_task["id"] if self.current_task else None,
            "elapsed_s": elapsed,
        }

    def print_status(self):
        """Print a human-readable status summary."""
        status = self.get_status()
        print(f"\n{'=' * 60}")
        print("TASK QUEUE STATUS")
        print(f"{'=' * 60}")
        if self.prd_title:
            print(f"  PRD: {self.prd_title}")
        print(f"  Total:   {status['total']}")
        print(f"  Complete: {status['completed']} ✅")
        print(f"  Failed:  {status['failed']} ❌")
        print(f"  Skipped: {status['skipped']} ⏭")
        print(f"  Pending: {status['pending']} ⏳")
        print(f"  Ready:   {status['ready']} ▶")
        print(f"  Blocked: {status['blocked']} 🔒")
        if status["current_task"]:
            print(f"  Current: {status['current_task']}")
        if status["elapsed_s"] is not None:
            print(f"  Elapsed: {status['elapsed_s']}s")
        print(f"{'=' * 60}")

        # Show task details
        if self.tasks:
            print()
            for task in self.tasks:
                tid = task["id"]
                title = task.get("title", "untitled")[:50]
                if tid in {t["id"] for t in self.completed}:
                    icon = "✅"
                elif tid in {t["id"] for t in self.failed}:
                    icon = "❌"
                elif tid in {t["id"] for t in self.skipped}:
                    icon = "⏭"
                elif task.get("status") == STATE_RUNNING:
                    icon = "▶"
                elif task in self.get_ready_tasks():
                    icon = "📋"
                else:
                    icon = "⏳"
                deps = task.get("dependencies", [])
                dep_str = f" ← {','.join(deps)}" if deps else ""
                print(f"  {icon} {tid}: {title}{dep_str}")

    def get_report(self, fmt: str = "markdown") -> dict:
        """Generate a structured execution report.

        Parameters
        ----------
        fmt : str
            ``"markdown"`` for a markdown string, ``"json"`` for raw dict.

        Returns
        -------
        dict
            ``{"markdown": str, "data": dict}`` when fmt is ``"markdown"``,
            or the raw data dict when fmt is ``"json"``.
        """
        status = self.get_status()

        # Build per-task results
        task_results = []
        for task in self.tasks:
            entry = {
                "id": task["id"],
                "title": task.get("title", ""),
                "status": task.get("status", STATE_PENDING),
                "category": task.get("category", ""),
                "complexity": task.get("complexity", ""),
                "dependencies": task.get("dependencies", []),
            }
            # Find matching execution result from log
            for event in self.execution_log:
                if (event.get("event") == "task_complete"
                        and event.get("data", {}).get("task_id") == task["id"]):
                    data = event["data"]
                    entry["duration_s"] = data.get("duration_s")
                    entry["error"] = data.get("error")
                    break
            task_results.append(entry)

        report_data = {
            "prd_title": self.prd_title or "Untitled",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": status,
            "tasks": task_results,
            "execution_log": self.execution_log,
        }

        if self.prd_metadata:
            report_data["prd_metadata"] = self.prd_metadata

        if fmt == "json":
            return report_data

        # Markdown format
        lines = [
            f"# Task Queue Report: {self.prd_title or 'Untitled'}",
            "",
            f"**Generated:** {report_data['generated_at']}",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total tasks | {status['total']} |",
            f"| Completed | {status['completed']} |",
            f"| Failed | {status['failed']} |",
            f"| Skipped | {status['skipped']} |",
            f"| Pending | {status['pending']} |",
            f"| Duration | {status.get('elapsed_s') or 'N/A'}s |",
            "",
        ]

        # Task details
        lines.append("## Tasks")
        lines.append("")
        lines.append("| ID | Title | Status | Category | Duration |")
        lines.append("|----|-------|--------|----------|----------|")

        status_icons = {
            STATE_COMPLETE: "✅",
            STATE_FAILED: "❌",
            STATE_SKIPPED: "⏭",
            STATE_RUNNING: "▶",
            STATE_PENDING: "⏳",
        }

        for t in task_results:
            icon = status_icons.get(t["status"], "❓")
            dur = t.get("duration_s", "—")
            if dur and dur != "—":
                dur = f"{dur}s"
            lines.append(
                f"| {t['id']} | {t['title'][:40]} | {icon} {t['status']} "
                f"| {t['category']} | {dur} |"
            )

        lines.append("")

        # Errors
        errors = [t for t in task_results if t.get("error")]
        if errors:
            lines.append("## Errors")
            lines.append("")
            for t in errors:
                lines.append(f"- **{t['id']}**: {t['error']}")
            lines.append("")

        # Dependency graph
        lines.append("## Dependency Graph")
        lines.append("")
        lines.append("```")
        order = self.topological_order()
        for t in order:
            deps = t.get("dependencies", [])
            dep_str = f" → {', '.join(deps)}" if deps else ""
            lines.append(f"  {t['id']}: {t['title'][:40]}{dep_str}")
        lines.append("```")
        lines.append("")

        report_data["markdown"] = "\n".join(lines)
        return report_data

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def _log_event(self, event_type: str, data: dict = None):
        """Record an event in the execution log."""
        entry = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        }
        self.execution_log.append(entry)
        if self.verbose:
            print(f"  [event] {event_type}: {json.dumps(data or {}, default=str)[:120]}")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_state(self, path: str = None):
        """Save queue state to a JSON file for resumption.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to ``task_queue_state.json`` in SCRIPT_DIR.
        """
        if path is None:
            path = os.path.join(SCRIPT_DIR, "task_queue_state.json")

        state = {
            "prd_title": self.prd_title,
            "prd_metadata": self.prd_metadata,
            "tasks": self.tasks,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "execution_log": self.execution_log,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(path, "w") as f:
            json.dump(state, f, indent=2, default=str)

        if self.verbose:
            print(f"  State saved to {path}")

    def load_state(self, path: str = None) -> bool:
        """Load queue state from a JSON file.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to ``task_queue_state.json`` in SCRIPT_DIR.

        Returns
        -------
        bool
            True if state was loaded successfully, False otherwise.
        """
        if path is None:
            path = os.path.join(SCRIPT_DIR, "task_queue_state.json")

        if not os.path.isfile(path):
            return False

        try:
            with open(path) as f:
                state = json.load(f)

            self.prd_title = state.get("prd_title")
            self.prd_metadata = state.get("prd_metadata")
            self.tasks = state.get("tasks", [])
            self.completed = state.get("completed", [])
            self.failed = state.get("failed", [])
            self.skipped = state.get("skipped", [])
            self.execution_log = state.get("execution_log", [])

            if self.verbose:
                print(f"  State loaded from {path}")
                print(f"    Tasks: {len(self.tasks)}, Completed: {len(self.completed)}, "
                      f"Failed: {len(self.failed)}")

            return True
        except Exception as e:
            if self.verbose:
                print(f"  Failed to load state: {e}")
            return False

    # ------------------------------------------------------------------
    # Built-in PRD parser fallback
    # ------------------------------------------------------------------

    def _builtin_parse_prd(self, prd_path: str) -> dict:
        """Lightweight PRD parser for basic markdown.

        Extracts user stories (As a…I want…so that…) and technical
        requirements (bullet points under ``## Technical Requirements``).
        Used when ``prd_parser.py`` is not available.
        """
        import re

        content = Path(prd_path).read_text()

        # Title
        title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else "Untitled PRD"

        # User stories
        story_pattern = (
            r'As\s+(?:a|an)\s+(.+?),\s+I\s+want\s+(.+?),?\s+so\s+that\s+(.+?)(?:\.|$)'
        )
        stories = re.findall(story_pattern, content, re.MULTILINE | re.IGNORECASE)

        # Requirements
        req_match = re.search(
            r'#+\s+Technical Requirements\s*\n(.*?)(?=^#|\Z)',
            content,
            re.MULTILINE | re.DOTALL,
        )
        requirements = []
        if req_match:
            requirements = re.findall(r'^\s*[-*]\s+(.+)$', req_match.group(1), re.MULTILINE)

        # Build tasks
        tasks = []
        task_id = 1

        for actor, want, benefit in stories:
            tasks.append({
                "id": f"T{task_id:02d}",
                "title": want.strip(),
                "description": f"As {actor}, implement: {want}. {benefit}",
                "files_to_create": [],
                "files_to_modify": [],
                "dependencies": [],
                "acceptance_criteria": [f"Feature implements: {want}"],
                "complexity": "medium",
                "estimated_tokens": 3000,
                "category": self._categorize(want),
            })
            task_id += 1

        for req in requirements:
            tasks.append({
                "id": f"T{task_id:02d}",
                "title": req[:60],
                "description": req,
                "files_to_create": [],
                "files_to_modify": [],
                "dependencies": [],
                "acceptance_criteria": [f"Requirement met: {req[:80]}"],
                "complexity": "medium",
                "estimated_tokens": 2000,
                "category": self._categorize(req),
            })
            task_id += 1

        return {
            "prd_title": title,
            "tasks": tasks,
            "metadata": {
                "total_tasks": len(tasks),
                "total_estimated_tokens": sum(t["estimated_tokens"] for t in tasks),
                "estimated_time_seconds": sum(t["estimated_tokens"] for t in tasks) // 200,
            },
        }

    @staticmethod
    def _categorize(description: str) -> str:
        """Categorize a task description into a rough domain."""
        desc = description.lower()
        if any(w in desc for w in ("model", "database", "schema", "table")):
            return "data"
        if any(w in desc for w in ("api", "endpoint", "route", "request")):
            return "api"
        if any(w in desc for w in ("ui", "frontend", "button", "page", "display")):
            return "ui"
        if any(w in desc for w in ("test", "verify", "validate")):
            return "test"
        if any(w in desc for w in ("config", "setup", "install", "deploy")):
            return "infra"
        return "logic"


# ============================================================================
# CLI
# ============================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        description="Task Queue + Orchestration for the Autonomous Coding Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 task_queue.py load test-prd.md              # Load PRD tasks
  python3 task_queue.py load tasks.json --tasks        # Load pre-parsed JSON
  python3 task_queue.py status                         # Show queue status
  python3 task_queue.py next                           # Show next ready task
  python3 task_queue.py execute --task T01             # Execute single task
  python3 task_queue.py execute-all                    # Execute all tasks
  python3 task_queue.py execute-all --dry-run          # Plan without executing
  python3 task_queue.py report                         # Generate execution report
  python3 task_queue.py report --format json           # JSON report
        """,
    )

    parser.add_argument(
        "--host", default=TRITON_HOST,
        help=f"Triton SSH host (default: {TRITON_HOST})",
    )
    parser.add_argument(
        "--user", default=TRITON_USER,
        help=f"Triton SSH user (default: {TRITON_USER})",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help=f"Model config ID (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Plan execution without actually running",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print detailed progress information",
    )
    parser.add_argument(
        "--state", type=str, default=None,
        help="Path to state file for save/load",
    )

    sub = parser.add_subparsers(dest="command", help="Command to execute")

    # load
    load_p = sub.add_parser("load", help="Load tasks from a PRD or JSON file")
    load_p.add_argument("file", help="Path to PRD markdown or JSON task file")
    load_p.add_argument(
        "--tasks", action="store_true",
        help="Treat the file as pre-parsed JSON (not a PRD)",
    )

    # status
    sub.add_parser("status", help="Show queue status")

    # next
    sub.add_parser("next", help="Show next ready task")

    # execute
    exec_p = sub.add_parser("execute", help="Execute a single task")
    exec_p.add_argument("--task", required=True, help="Task ID to execute (e.g. T01)")

    # execute-all
    sub.add_parser("execute-all", help="Execute all tasks in dependency order")

    # report
    report_p = sub.add_parser("report", help="Generate execution report")
    report_p.add_argument(
        "--format", choices=["markdown", "json"], default="markdown",
        help="Report format (default: markdown)",
    )
    report_p.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file path (default: stdout / reports/)",
    )

    return parser


def main():
    """CLI entry point."""
    global VERBOSE

    parser = build_parser()
    args = parser.parse_args()
    VERBOSE = args.verbose

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Build queue — always try to load previous state so tasks are available
    queue = TaskQueue(
        host=args.host,
        user=args.user,
        config=args.config,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    queue.load_state(args.state)

    if not queue.tasks and args.command not in ("load",):
        print("No tasks loaded.  Run 'load' first.", file=sys.stderr)
        sys.exit(1)

    # Dispatch
    try:
        if args.command == "load":
            # Clear any previously loaded state for a fresh load
            queue.tasks = []
            queue.completed = []
            queue.failed = []
            queue.skipped = []

            if args.tasks:
                result = queue.load_tasks_from_json(args.file)
            else:
                result = queue.load_prd(args.file)

            queue.save_state(args.state)
            print(f"\nLoaded {len(queue.tasks)} tasks from {args.file}")

        elif args.command == "status":
            queue.print_status()

        elif args.command == "next":
            task = queue.next_task()
            if task is None:
                print("No ready tasks.")
                status = queue.get_status()
                if status["failed"] > 0:
                    print(f"  ({status['failed']} tasks failed, may be blocking progress)")
                if status["completed"] == status["total"]:
                    print("  All tasks completed! 🎉")
            else:
                deps = task.get("dependencies", [])
                print(f"Next task: {task['id']} — {task.get('title', 'untitled')}")
                print(f"  Category: {task.get('category', 'logic')}")
                print(f"  Complexity: {task.get('complexity', 'medium')}")
                print(f"  Est. tokens: {task.get('estimated_tokens', 3000)}")
                if deps:
                    print(f"  Dependencies: {', '.join(deps)} (all met ✅)")
                if task.get("files_to_create"):
                    print(f"  Files to create: {', '.join(task['files_to_create'])}")
                if task.get("files_to_modify"):
                    print(f"  Files to modify: {', '.join(task['files_to_modify'])}")

        elif args.command == "execute":
            task_id = args.task
            task = None
            for t in queue.tasks:
                if t["id"] == task_id:
                    task = t
                    break

            if task is None:
                print(f"Task {task_id} not found.", file=sys.stderr)
                available = [t["id"] for t in queue.tasks]
                if available:
                    print(f"  Available: {', '.join(available)}", file=sys.stderr)
                sys.exit(1)

            # Check dependencies
            completed_ids = {t["id"] for t in queue.completed}
            deps = task.get("dependencies", [])
            unmet = [d for d in deps if d not in completed_ids]
            if unmet:
                print(f"⚠ Task {task_id} has unmet dependencies: {', '.join(unmet)}")
                print("  Execute those first, or use --dry-run to preview.")
                if not args.dry_run:
                    sys.exit(1)

            result = queue.execute_task(task)
            queue.save_state(args.state)

            if result["status"] == STATE_FAILED:
                sys.exit(1)

        elif args.command == "execute-all":
            summary = queue.execute_all()
            queue.save_state(args.state)

            if summary["failed"] > 0:
                sys.exit(1)

        elif args.command == "report":
            report = queue.get_report(fmt=args.format)

            if args.format == "json":
                output = json.dumps(report, indent=2, default=str)
            else:
                output = report.get("markdown", json.dumps(report, indent=2))

            if args.output:
                Path(args.output).write_text(output)
                print(f"Report written to {args.output}")
            else:
                # Default report location for markdown
                if args.format == "markdown" and not args.output:
                    report_dir = os.path.join(SCRIPT_DIR, "reports")
                    os.makedirs(report_dir, exist_ok=True)
                    default_path = os.path.join(report_dir, "task-queue-report.md")
                    Path(default_path).write_text(output)
                    print(f"Report written to {default_path}")
                else:
                    print(output)

    except KeyboardInterrupt:
        print("\nInterrupted. Saving state...")
        queue.save_state(args.state)
        sys.exit(130)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        queue.save_state(args.state)
        sys.exit(1)


if __name__ == "__main__":
    main()
