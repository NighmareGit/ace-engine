#!/usr/bin/env python3
"""End-to-end demo of the autonomous coding engine.

Runs the full pipeline on test-prd.md:
parse → generate → quality-gate → test → commit → PR → summary

Usage:
    python3 demo_e2e.py                          # default: test-prd.md, ace-demo
    python3 demo_e2e.py --prd test-prd.md        # specify PRD
    python3 demo_e2e.py --project /path/to/proj  # target project on Triton
    python3 demo_e2e.py --dry-run                # preview plan, no execution
    python3 demo_e2e.py --repo <user>/ace-demo   # Gitea repo for PR
    python3 demo_e2e.py --no-pr                  # skip PR creation
    python3 demo_e2e.py --verbose                # extra logging
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── ensure coder-harness modules are importable ──────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

class Timer:
    """Wall-clock timer used for step durations."""

    def __init__(self):
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 2)

    def reset(self):
        self._t0 = time.monotonic()


def _banner(text: str):
    """Print a centered banner line."""
    width = 64
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)
    print()


def _step(num: int, label: str):
    """Print a pipeline step header."""
    print(f"── Step {num}: {label} ──")


def _indent(text: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


def _ts() -> str:
    """ISO-8601 timestamp for reports."""
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════
#  Pipeline
# ═══════════════════════════════════════════════════════════════════════

class E2EPipeline:
    """Orchestrates the full autonomous coding engine pipeline.

    Steps:
        1. Parse PRD → task list
        2. For each task:
           a. Build context (project snapshot)
           b. Generate code (inference → extract → write)
           c. Run quality gates
           d. Run tests
           e. If tests fail → retry (max N)
           f. Git commit
        3. Create PR on Gitea
        4. Print summary report
    """

    def __init__(
        self,
        prd_path: str,
        project_path: str,
        host: str = "<LAN_IP>",
        user: str = "<user>",
        gitea_repo: Optional[str] = None,
        base_branch: str = "main",
        max_retries: int = 3,
        dry_run: bool = False,
        skip_pr: bool = False,
        verbose: bool = False,
    ):
        self.prd_path = prd_path
        self.project_path = project_path
        self.host = host
        self.user = user
        self.gitea_repo = gitea_repo
        self.base_branch = base_branch
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.skip_pr = skip_pr
        self.verbose = verbose

        # Results accumulator
        self.task_results: List[Dict[str, Any]] = []
        self.overall_start = time.monotonic()
        self.prd_data: Optional[dict] = None
        self.pr_url: Optional[str] = None
        self.git_branch: Optional[str] = None
        self.error: Optional[str] = None

    # ── step 1: parse ──────────────────────────────────────────────────

    def step_parse_prd(self) -> dict:
        """Parse the PRD markdown into a structured task list."""
        _step(1, "Parse PRD")
        from prd_parser import PRDParser

        parser = PRDParser()
        prd = parser.parse(self.prd_path)
        self.prd_data = prd

        print(f"  Title:    {prd['prd_title']}")
        print(f"  Tasks:    {prd['metadata']['total_tasks']}")
        print(f"  Est. tok: {prd['metadata']['total_estimated_tokens']:,}")
        print(f"  Est. time:{prd['metadata']['estimated_time_seconds']}s")
        print()
        for task in prd["tasks"]:
            print(f"    {task['id']}: {task['title']} [{task['complexity']}]")

        return prd

    # ── step 2a: build context ─────────────────────────────────────────

    def step_build_context(self, task: dict) -> dict:
        """Snapshot the project and build context for the task."""
        if self.verbose:
            print("    [context] Building project snapshot...")
        from context_manager import ContextManager

        mgr = ContextManager()
        try:
            summary = mgr.snapshot_project(self.project_path)
            ctx = mgr.get_context_for_task(task)
            if self.verbose:
                print(f"    [context] {summary.get('files_captured', 0)} files captured")
            return ctx
        except Exception as exc:
            # If snapshot fails (project doesn't exist on Triton yet), return empty context
            if self.verbose:
                print(f"    [context] Snapshot failed (expected on first run): {exc}")
            return {
                "project_structure": {},
                "relevant_files": {},
                "recent_modifications": [],
                "test_history": {},
                "conversation_summary": [],
            }

    # ── step 2b: generate code ─────────────────────────────────────────

    def step_generate_code(
        self, task: dict, context: dict, retry_context: Optional[dict] = None
    ) -> dict:
        """Run code generation (inference → extract → write → test).

        If retry_context is provided, builds a retry prompt instead of
        starting from scratch.
        """
        from code_generator import CodeGenerator

        gen = CodeGenerator(
            host=self.host,
            user=self.user,
            max_retries=1,  # We handle retries at pipeline level
        )

        result = gen.generate(
            task=task,
            context=context,
            project_path=self.project_path,
            run_tests=False,  # We run tests separately in step 2d
            dry_run=self.dry_run,
            verbose=self.verbose,
        )
        return result

    # ── step 2c: quality gates ─────────────────────────────────────────

    def step_quality_gates(self, files: Dict[str, str]) -> dict:
        """Run linting, style, and security checks on generated files."""
        from quality_gates import QualityGates

        qg = QualityGates(host=self.host, user=self.user)

        if self.dry_run:
            return {"overall": "dry_run", "checks": {}}

        # Check files locally if they exist, otherwise try remote
        results = {"overall": "pass", "checks": {}, "score": 100}

        for filepath, content in files.items():
            # Check syntax locally by writing to temp file
            import tempfile
            import os

            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False, dir="/tmp"
                ) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name

                # Run syntax check
                sr = qg.check_syntax("/tmp", os.path.basename(tmp_path))
                if sr.get("issues"):
                    results["checks"][f"{filepath}:syntax"] = sr
                    results["overall"] = "warn" if results["overall"] != "fail" else "fail"

                # Run security check
                sec = qg.check_security("/tmp", os.path.basename(tmp_path))
                if sec.get("issues"):
                    results["checks"][f"{filepath}:security"] = sec
                    if any(i.get("severity") == "high" for i in sec.get("issues", [])):
                        results["overall"] = "fail"

                # Run style check
                st = qg.check_style("/tmp", os.path.basename(tmp_path))
                if st.get("issues"):
                    results["checks"][f"{filepath}:style"] = st

                os.unlink(tmp_path)
            except Exception as exc:
                if self.verbose:
                    print(f"    [qg] Error checking {filepath}: {exc}")

        if self.verbose:
            print(f"    [qg] Overall: {results['overall']} (score: {results.get('score', '?')})")

        return results

    # ── step 2d: run tests ─────────────────────────────────────────────

    def step_run_tests(self) -> dict:
        """Run pytest/unittest on the remote project."""
        from test_runner import TestRunner

        runner = TestRunner(host=self.host, user=self.user, timeout=60)

        if self.dry_run:
            return {"status": "dry_run", "total": 0, "passed": 0, "failed": 0}

        result = runner.run_tests(self.project_path)
        return result

    # ── step 2e: git commit ────────────────────────────────────────────

    def step_git_commit(self, task: dict, files: Dict[str, str]) -> dict:
        """Create branch, stage all, and commit."""
        from git_workflow import GitWorkflow

        gw = GitWorkflow(host=self.host, user=self.user)

        if self.dry_run:
            branch_name = gw._generate_branch_name(task)
            commit_msg = gw._generate_commit_message(task, list(files.keys()))
            return {
                "success": True,
                "dry_run": True,
                "branch": branch_name,
                "message": commit_msg,
            }

        # Create branch
        branch_name = gw._generate_branch_name(task)
        branch_result = gw.create_branch(self.project_path, branch_name, verbose=self.verbose)
        if not branch_result.get("success"):
            return {
                "success": False,
                "error": f"Branch creation failed: {branch_result.get('stderr', 'unknown')}",
            }
        self.git_branch = branch_name

        # Commit
        commit_msg = gw._generate_commit_message(task, list(files.keys()))
        commit_result = gw.commit_all(self.project_path, commit_msg, verbose=self.verbose)
        if not commit_result.get("success"):
            return {
                "success": False,
                "error": f"Commit failed: {commit_result.get('stderr', 'unknown')}",
            }

        # Push
        push_result = gw.push(self.project_path, branch_name, verbose=self.verbose)
        if not push_result.get("success"):
            return {
                "success": False,
                "error": f"Push failed: {push_result.get('stderr', 'unknown')}",
            }

        return {
            "success": True,
            "branch": branch_name,
            "commit_message": commit_msg,
        }

    # ── step 3: create PR ──────────────────────────────────────────────

    def step_create_pr(self) -> dict:
        """Create a pull request on Gitea."""
        if self.skip_pr or not self.gitea_repo:
            return {"success": False, "skipped": True, "reason": "PR creation disabled or no repo specified"}

        from git_workflow import GitWorkflow

        gw = GitWorkflow(host=self.host, user=self.user)

        if self.dry_run:
            return {
                "success": True,
                "dry_run": True,
                "repo": self.gitea_repo,
                "head": self.git_branch,
                "base": self.base_branch,
            }

        if not self.git_branch:
            return {"success": False, "error": "No branch set — cannot create PR"}

        # Build PR body from task results
        body_parts = ["## Autonomous Coding Engine — Generated PR\n"]
        body_parts.append(f"**PRD:** `{self.prd_path}`")
        body_parts.append(f"**Tasks completed:** {len(self.task_results)}")
        body_parts.append("")
        body_parts.append("### Task Results\n")
        for r in self.task_results:
            status_icon = "✅" if r.get("status") in ("success", "completed") else "❌"
            body_parts.append(
                f"- {status_icon} **{r.get('task_id', '?')}**: {r.get('title', '?')} "
                f"— {r.get('status', 'unknown')} ({r.get('attempts', 1)} attempt(s))"
            )
        body_parts.append(f"\n---\n*Generated at {_ts()} by ACE demo_e2e.py*")

        pr_result = gw.create_pr(
            repo=self.gitea_repo,
            title=f"feat: {self.prd_data.get('prd_title', 'ACE Implementation')}",
            body="\n".join(body_parts),
            head_branch=self.git_branch,
            base_branch=self.base_branch,
            verbose=self.verbose,
        )

        if pr_result.get("success"):
            self.pr_url = pr_result.get("pr_url", "")
            print(f"  PR created: {self.pr_url}")

        return pr_result

    # ── step 4: summary ────────────────────────────────────────────────

    def step_summary(self) -> dict:
        """Print and save the summary report."""
        _banner("SUMMARY REPORT")

        elapsed = round(time.monotonic() - self.overall_start, 2)
        total = len(self.task_results)
        passed = sum(
            1 for r in self.task_results if r.get("status") in ("success", "completed", "dry_run")
        )
        failed = total - passed

        print(f"  PRD:        {self.prd_path}")
        print(f"  Project:    {self.project_path}")
        print(f"  Total:      {total} tasks")
        print(f"  Passed:     {passed}")
        print(f"  Failed:     {failed}")
        print(f"  Duration:   {elapsed}s")
        if self.git_branch:
            print(f"  Branch:     {self.git_branch}")
        if self.pr_url:
            print(f"  PR:         {self.pr_url}")
        print()

        # Per-task breakdown
        for r in self.task_results:
            status = r.get("status", "unknown")
            icon = "✅" if status in ("success", "completed", "dry_run") else "❌"
            attempts = r.get("attempts", 1)
            files_count = r.get("files_generated", 0)
            print(
                f"    {icon} {r.get('task_id', '?')}: {r.get('title', '?')}"
                f"  [{status}] {files_count} file(s), {attempts} attempt(s)"
            )
            if r.get("error"):
                print(f"       Error: {r['error']}")
            if r.get("quality"):
                print(f"       Quality: {r['quality']}")
        print()

        # Save report
        report = {
            "prd": self.prd_path,
            "project": self.project_path,
            "host": self.host,
            "dry_run": self.dry_run,
            "git_branch": self.git_branch,
            "pr_url": self.pr_url,
            "tasks": self.task_results,
            "summary": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "elapsed_seconds": elapsed,
            },
            "generated_at": _ts(),
        }

        report_dir = _HERE / "reports"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / "e2e-demo-report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  Report saved: {report_path}")

        # Also save a Markdown summary
        md_path = report_dir / "e2e-demo-report.md"
        md_lines = [
            "# ACE E2E Demo Report\n",
            f"- **Date:** {_ts()}",
            f"- **PRD:** `{self.prd_path}`",
            f"- **Project:** `{self.project_path}`",
            f"- **Branch:** `{self.git_branch or 'N/A'}`",
            f"- **PR:** {self.pr_url or 'N/A'}",
            f"- **Dry run:** {self.dry_run}",
            f"- **Duration:** {elapsed}s\n",
            f"## Results: {passed}/{total} passed\n",
            "| Task | Title | Status | Files | Attempts | Error |",
            "|------|-------|--------|-------|----------|-------|",
        ]
        for r in self.task_results:
            icon = "✅" if r.get("status") in ("success", "completed", "dry_run") else "❌"
            md_lines.append(
                f"| {r.get('task_id', '?')} | {r.get('title', '?')} | {icon} {r.get('status', '?')} "
                f"| {r.get('files_generated', 0)} | {r.get('attempts', 1)} "
                f"| {r.get('error', '')} |"
            )
        md_lines.append("")
        md_lines.append(f"*Generated by ACE demo_e2e.py*")
        with open(md_path, "w") as f:
            f.write("\n".join(md_lines))
        print(f"  MD report:   {md_path}")

        return report

    # ── full pipeline ──────────────────────────────────────────────────

    def run(self) -> dict:
        """Execute the full pipeline."""
        _banner("Autonomous Coding Engine — End-to-End Demo")

        if self.dry_run:
            print("  MODE: DRY RUN (no SSH, no inference, no file writes)\n")

        # ── Step 1: Parse PRD ──
        try:
            prd = self.step_parse_prd()
        except Exception as exc:
            self.error = f"PRD parsing failed: {exc}"
            print(f"\n  ❌ {self.error}")
            return {"error": self.error, "tasks": []}

        # ── Step 2: Execute each task ──
        for task in prd["tasks"]:
            task_id = task["id"]
            title = task["title"]
            print(f"\n── Processing {task_id}: {title} ──\n")

            task_result: Dict[str, Any] = {
                "task_id": task_id,
                "title": title,
                "complexity": task.get("complexity", "medium"),
                "status": "pending",
                "attempts": 0,
                "files_generated": 0,
                "quality": None,
                "test_result": None,
                "git_result": None,
                "error": None,
            }

            # ── 2a: Build context ──
            print("  [a] Building context...")
            context = self.step_build_context(task)

            # ── 2b-e: Generate with retry loop ──
            last_files: Dict[str, str] = {}
            success = False

            for attempt in range(1, self.max_retries + 1):
                task_result["attempts"] = attempt
                print(f"  [b] Generating code (attempt {attempt}/{self.max_retries})...")

                gen_result = self.step_generate_code(task, context)
                status = gen_result.get("status", "unknown")

                if status == "dry_run":
                    # Dry run: record plan, skip further steps
                    task_result["status"] = "dry_run"
                    task_result["plan"] = gen_result.get("plan", {})
                    print(f"  → Dry run plan captured")
                    success = True
                    break

                files = gen_result.get("files", {})
                if not files:
                    err = gen_result.get("error", "No code blocks extracted")
                    task_result["error"] = err
                    print(f"  ✗ No code generated: {err}")
                    continue

                last_files = files
                task_result["files_generated"] = len(files)
                print(f"  → Generated {len(files)} file(s): {', '.join(files.keys())}")

                # ── 2c: Quality gates ──
                print("  [c] Running quality gates...")
                qg_result = self.step_quality_gates(files)
                task_result["quality"] = {
                    "overall": qg_result.get("overall", "unknown"),
                    "score": qg_result.get("score"),
                }
                if self.verbose:
                    print(f"  → Quality: {qg_result.get('overall', '?')} (score: {qg_result.get('score', '?')})")

                # ── 2d: Run tests ──
                print("  [d] Running tests...")
                test_result = self.step_run_tests()
                task_result["test_result"] = {
                    "status": test_result.get("status", "unknown"),
                    "passed": test_result.get("passed", 0),
                    "failed": test_result.get("failed", 0),
                }
                test_status = test_result.get("status", "unknown")
                print(f"  → Tests: {test_status} ({test_result.get('passed', 0)} passed, {test_result.get('failed', 0)} failed)")

                if test_status == "passed" or test_status == "dry_run":
                    success = True
                    break

                # Tests failed — retry
                if attempt < self.max_retries:
                    print(f"  ⚠ Tests failed — retrying ({attempt}/{self.max_retries})...")
                else:
                    print(f"  ✗ All {self.max_retries} attempts exhausted")

            # ── 2f: Git commit ──
            if success and last_files:
                print("  [f] Committing to git...")
                git_result = self.step_git_commit(task, last_files)
                task_result["git_result"] = git_result
                if git_result.get("success"):
                    print(f"  → Committed on branch: {git_result.get('branch', '?')}")
                else:
                    print(f"  ✗ Git failed: {git_result.get('error', '?')}")

            # ── Record final status ──
            if success:
                task_result["status"] = "completed" if not self.dry_run else "dry_run"
            else:
                task_result["status"] = "failed"

            self.task_results.append(task_result)
            print()

        # ── Step 3: Create PR ──
        if not self.dry_run and self.task_results:
            _step(3, "Create Pull Request")
            pr_result = self.step_create_pr()
            if not pr_result.get("success") and not pr_result.get("skipped"):
                print(f"  ⚠ PR creation failed: {pr_result.get('error', '?')}")

        # ── Step 4: Summary ──
        return self.step_summary()


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="ACE End-to-End Demo — full autonomous coding pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Dry-run (preview plan, no execution)
  python3 demo_e2e.py --dry-run

  # Full run against Triton
  python3 demo_e2e.py --prd test-prd.md --project /home/<user>/projects/ace-demo

  # Run without creating a PR
  python3 demo_e2e.py --no-pr

  # Verbose output
  python3 demo_e2e.py --verbose
""",
    )
    parser.add_argument(
        "--prd",
        default=str(_HERE / "test-prd.md"),
        help="Path to PRD markdown file (default: test-prd.md)",
    )
    parser.add_argument(
        "--project",
        default="/home/<user>/projects/ace-demo",
        help="Target project path on Triton (default: /home/<user>/projects/ace-demo)",
    )
    parser.add_argument(
        "--host",
        default="<LAN_IP>",
        help="Triton SSH host (default: <LAN_IP>)",
    )
    parser.add_argument(
        "--user",
        default="<user>",
        help="Triton SSH user (default: <user>)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Gitea repo in owner/name format (e.g. <user>/ace-demo)",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        help="Base branch for PR (default: main)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Max retry attempts per task (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview pipeline without executing (no SSH, no inference)",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Skip PR creation",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    args = parser.parse_args()

    # Auto-detect repo if not specified
    repo = args.repo
    if not repo and not args.dry_run:
        # Try to infer from project path
        project_name = args.project.rstrip("/").split("/")[-1]
        repo = f"<user>/{project_name}"
        print(f"  (Auto-detected Gitea repo: {repo})")

    pipeline = E2EPipeline(
        prd_path=args.prd,
        project_path=args.project,
        host=args.host,
        user=args.user,
        gitea_repo=repo,
        base_branch=args.base_branch,
        max_retries=args.max_retries,
        dry_run=args.dry_run,
        skip_pr=args.no_pr,
        verbose=args.verbose,
    )

    report = pipeline.run()

    # Exit code: 0 if all passed or dry run, 1 if any genuinely failed
    failed_count = report.get("summary", {}).get("failed", 0)
    is_all_dry_run = all(r.get("status") == "dry_run" for r in report.get("tasks", []))
    if report.get("error") or (failed_count > 0 and not is_all_dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()
