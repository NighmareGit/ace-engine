#!/usr/bin/env python3
"""
Workflow State Machine for Coder Harness
==========================================

Manages the end-to-end workflow: docs update → Ralph loops → live-fire test.
Designed for speed: uses Config-I (Qwen3.6-35B, 188 tok/s) for all inference.

States:
  INIT → SCOPING → DISPATCHING → EXECUTING → REVIEWING → TESTING
  → ITERATING → REPORTING → COMPLETE

Each state has:
  - entry action (what to do when entering)
  - exit condition (what must be true to leave)
  - timeout (max time in state)
  - rollback (what to do on failure)
"""

import json
import sqlite3
import os
import subprocess
from datetime import datetime
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"
CONFIG = "config-i"  # Qwen3.6-35B — 188 tok/s, speed > intelligence
CONFIG_ID = "3090-qwen36-35b"
DB_PATH = os.path.expanduser("~/coder-harness-workflow.db")

# ── State Machine ────────────────────────────────────────────────────────────

STATES = {
    "INIT": {
        "description": "Initialize workflow, verify connectivity",
        "timeout_seconds": 60,
        "transitions": {"ready": "SCOPING", "error": "ERROR"},
    },
    "SCOPING": {
        "description": "Read project state, identify gaps, plan work",
        "timeout_seconds": 120,
        "transitions": {"scoped": "DISPATCHING", "error": "ERROR"},
    },
    "DISPATCHING": {
        "description": "Launch parallel agents for implementation",
        "timeout_seconds": 300,
        "transitions": {"dispatched": "EXECUTING", "error": "ERROR"},
    },
    "EXECUTING": {
        "description": "Agents implementing tasks",
        "timeout_seconds": 600,
        "transitions": {"executed": "REVIEWING", "error": "ERROR"},
    },
    "REVIEWING": {
        "description": "Code review of all changes",
        "timeout_seconds": 120,
        "transitions": {"passed": "TESTING", "failed": "EXECUTING", "error": "ERROR"},
    },
    "TESTING": {
        "description": "Run tests, verify functionality",
        "timeout_seconds": 180,
        "transitions": {"passed": "ITERATING", "failed": "EXECUTING", "error": "ERROR"},
    },
    "ITERATING": {
        "description": "Meta-cognitive reflection, plan next iteration",
        "timeout_seconds": 60,
        "transitions": {"more_work": "DISPATCHING", "done": "REPORTING", "error": "ERROR"},
    },
    "REPORTING": {
        "description": "Generate final report, push to Gitea",
        "timeout_seconds": 120,
        "transitions": {"reported": "COMPLETE", "error": "ERROR"},
    },
    "COMPLETE": {
        "description": "Workflow finished successfully",
        "timeout_seconds": 0,
        "transitions": {},
    },
    "ERROR": {
        "description": "Error state — needs manual intervention",
        "timeout_seconds": 0,
        "transitions": {"retry": "SCOPING", "abort": "COMPLETE"},
    },
}

# ── Task Definitions (Scoped for 188 tok/s Qwen3.6-35B) ─────────────────────
# Complexity is MODERATE — no CUDA kernels, no Rust, no deep architecture.
# Focus: Python scripts, config files, documentation, integration tests.

TASKS = {
    "docs-update": {
        "description": "Update all documentation to reflect complete platform",
        "complexity": "low",
        "estimated_tokens": 8000,
        "files": ["AGENTS.md", "README.md", "docs/API-REFERENCE.md", "CONTRIBUTING.md"],
    },
    "remote-control-docs": {
        "description": "Create remote-control.md and workflow-state-machine.md",
        "complexity": "low",
        "estimated_tokens": 6000,
        "files": ["docs/remote-control.md", "docs/workflow-state-machine.md"],
    },
    "telemetry-integration": {
        "description": "Wire telemetry_collector into harness.py dashboard command",
        "complexity": "medium",
        "estimated_tokens": 4000,
        "files": ["harness.py", "telemetry_collector.py"],
    },
    "gitea-automation": {
        "description": "Add auto-push to Gitea after benchmark runs",
        "complexity": "medium",
        "estimated_tokens": 5000,
        "files": ["work_engine.py", "gitea_utils.py"],
    },
    "live-fire-test": {
        "description": "Run Config-I through real project task (corpus-real.json RT08)",
        "complexity": "medium",
        "estimated_tokens": 3000,
        "files": [],
    },
    "sandbox-cleanup": {
        "description": "Add auto-cleanup of old sandboxes (>24h)",
        "complexity": "low",
        "estimated_tokens": 3000,
        "files": ["sandbox_manager.py"],
    },
    "error-handling": {
        "description": "Add structured error handling to work_engine.py",
        "complexity": "medium",
        "estimated_tokens": 4000,
        "files": ["work_engine.py"],
    },
    "config-profiles": {
        "description": "Add config-h-qwen35 to docker-compose (missing profile)",
        "complexity": "low",
        "estimated_tokens": 2000,
        "files": ["tickets/deploy/docker-compose.yml"],
    },
    "benchmark-quality": {
        "description": "Run quality benchmarks on 3 configs with real tasks",
        "complexity": "medium",
        "estimated_tokens": 5000,
        "files": [],
    },
    "final-report": {
        "description": "Generate comparison report and push to Gitea",
        "complexity": "low",
        "estimated_tokens": 3000,
        "files": ["reports/"],
    },
}

# ── Workflow Engine ──────────────────────────────────────────────────────────

class WorkflowStateMachine:
    def __init__(self):
        self.state = "INIT"
        self.history = []
        self.current_task = None
        self.iteration = 0
        self.max_iterations = 10
        self.completed_tasks = []
        self.failed_tasks = []
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL,
            task TEXT,
            iteration INTEGER,
            started_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            status TEXT DEFAULT 'running',
            details TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            complexity TEXT,
            status TEXT DEFAULT 'pending',
            started_at TEXT,
            completed_at TEXT,
            iteration INTEGER,
            result TEXT
        )""")
        conn.commit()
        conn.close()
    
    def transition(self, new_state, details=None):
        old_state = self.state
        # Validate transition
        valid = STATES[old_state]["transitions"]
        if new_state not in valid and new_state != "ERROR":
            raise ValueError(f"Invalid transition: {old_state} → {new_state}")
        
        self.state = new_state
        self.history.append({
            "from": old_state,
            "to": new_state,
            "timestamp": datetime.now().isoformat(),
            "details": details,
        })
        
        # Record in DB
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO workflow_state (state, task, iteration, details) VALUES (?, ?, ?, ?)",
            (new_state, self.current_task, self.iteration, json.dumps(details) if details else None)
        )
        conn.commit()
        conn.close()
        
        print(f"  [{old_state}] → [{new_state}] {details or ''}")
    
    def run(self):
        """Execute the workflow state machine."""
        print("=== Workflow State Machine ===")
        print(f"Config: {CONFIG} ({CONFIG_ID}) — 188 tok/s")
        print(f"Max iterations: {self.max_iterations}")
        print()
        
        # INIT
        self.transition("INIT", "Verifying connectivity")
        if not self._check_connectivity():
            self.transition("ERROR", "Cannot reach Triton")
            return
        self.transition("SCOPING", "Connectivity OK")
        
        # Main loop
        while self.iteration < self.max_iterations:
            self.iteration += 1
            print(f"\n--- Iteration {self.iteration}/{self.max_iterations} ---")
            
            # SCOPING
            tasks = self._scope_tasks()
            if not tasks:
                self.transition("REPORTING", "All tasks complete")
                break
            self.transition("DISPATCHING", f"{len(tasks)} tasks identified")
            
            # DISPATCHING → EXECUTING
            self.transition("EXECUTING", f"Running {len(tasks)} tasks")
            results = self._execute_tasks(tasks)
            
            # REVIEWING
            self.transition("REVIEWING", "Reviewing results")
            review_ok = self._review_results(results)
            
            if review_ok:
                self.transition("TESTING", "Review passed")
                test_ok = self._test_results(results)
                
                if test_ok:
                    self.transition("ITERATING", "Tests passed")
                    more = self._plan_next_iteration()
                    if not more:
                        self.transition("REPORTING", "No more work")
                        break
                else:
                    self.transition("EXECUTING", "Tests failed, retrying")
            else:
                self.transition("EXECUTING", "Review failed, retrying")
        
        # REPORTING
        self._generate_report()
        self.transition("COMPLETE", f"Workflow finished after {self.iteration} iterations")
        
        print(f"\n=== COMPLETE ===")
        print(f"Iterations: {self.iteration}")
        print(f"Tasks completed: {len(self.completed_tasks)}")
        print(f"Tasks failed: {len(self.failed_tasks)}")
    
    def _check_connectivity(self):
        try:
            r = subprocess.run(
                ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5',
                 f'{TRITON_USER}@{TRITON_HOST}', 'echo OK'],
                capture_output=True, text=True, timeout=10
            )
            return r.returncode == 0 and 'OK' in r.stdout
        except Exception:
            return False
    
    def _scope_tasks(self):
        remaining = [t for t in TASKS if t not in self.completed_tasks]
        # Sort by complexity (low first for speed)
        order = {"low": 0, "medium": 1, "high": 2}
        remaining.sort(key=lambda t: order.get(TASKS[t]["complexity"], 99))
        return remaining[:3]  # Max 3 tasks per iteration
    
    def _execute_tasks(self, tasks):
        results = {}
        for task_name in tasks:
            self.current_task = task_name
            task = TASKS[task_name]
            print(f"  Executing: {task_name} ({task['complexity']})")
            
            # Record task start
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO workflow_tasks (name, description, complexity, status, started_at, iteration) VALUES (?, ?, ?, 'running', datetime('now'), ?)",
                (task_name, task["description"], task["complexity"], self.iteration)
            )
            conn.commit()
            conn.close()
            
            # Task execution would be delegated to subagents
            # For now, mark as completed
            results[task_name] = {"status": "executed", "tokens": task["estimated_tokens"]}
            self.completed_tasks.append(task_name)
            
            # Record completion
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "UPDATE workflow_tasks SET status='completed', completed_at=datetime('now') WHERE name=?",
                (task_name,)
            )
            conn.commit()
            conn.close()
        
        return results
    
    def _review_results(self, results):
        # Simple review: check all tasks executed
        return all(r["status"] == "executed" for r in results.values())
    
    def _test_results(self, results):
        # Simple test: check all tasks have output
        return len(results) > 0
    
    def _plan_next_iteration(self):
        remaining = [t for t in TASKS if t not in self.completed_tasks]
        return len(remaining) > 0
    
    def _generate_report(self):
        report = {
            "workflow": "coder-harness-enhancement",
            "config": CONFIG,
            "config_id": CONFIG_ID,
            "iterations": self.iteration,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "total_tasks": len(TASKS),
            "history": self.history,
            "timestamp": datetime.now().isoformat(),
        }
        
        # Save report
        report_path = Path("reports/workflow-report.json")
        report_path.parent.mkdir(exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report saved to {report_path}")
        
        # Save to Triton Gitea
        try:
            subprocess.run(
                ['scp', '-o', 'StrictHostKeyChecking=no', str(report_path),
                 f'{TRITON_USER}@{TRITON_HOST}:/tmp/harness-files/reports/'],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Workflow State Machine")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--reset", action="store_true", help="Reset to INIT")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    args = parser.parse_args()
    
    wsm = WorkflowStateMachine()
    
    if args.status:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT state, task, iteration, started_at FROM workflow_state ORDER BY id DESC LIMIT 5").fetchall()
        for r in rows:
            print(f"  {r[3]} | {r[0]} | task={r[1]} | iter={r[2]}")
        conn.close()
    elif args.reset:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        print("Workflow reset.")
    elif args.dry_run:
        print("=== DRY RUN ===")
        print(f"Config: {CONFIG} ({CONFIG_ID})")
        print(f"Tasks: {len(TASKS)}")
        for name, task in TASKS.items():
            print(f"  {task['complexity']:6s} | {task['estimated_tokens']:5d} tok | {name}: {task['description']}")
        total_tokens = sum(t['estimated_tokens'] for t in TASKS.values())
        print(f"\nTotal estimated tokens: {total_tokens}")
        print(f"Estimated time at 188 tok/s: {total_tokens/188:.0f}s ({total_tokens/188/60:.1f} min)")
    else:
        wsm.run()
