#!/usr/bin/env python3
"""Auto-generate a comprehensive README.md for the coder-harness project.

Inspects the actual codebase: Python source files, projects.yaml, schema.sql,
models/manifest.json, and file inventory.  Output is a single README.md.

Python stdlib only — no third-party dependencies.

Usage:
    python3 generate_readme.py                # writes README.md in cwd
    python3 generate_readme.py -o path/to/out # custom output path
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import textwrap
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _line_count(path: str) -> int:
    """Return the number of lines in a file, or 0 on error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _read_text(path: str) -> str:
    """Read a file to string, empty on error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Python source analyser
# ---------------------------------------------------------------------------

class _PythonModule:
    """Parsed metadata for a single .py file."""

    def __init__(self, path: str, base_dir: str) -> None:
        self.path = path
        self.rel = os.path.relpath(path, base_dir)
        self.basename = os.path.basename(path)
        self.line_count = _line_count(path)
        self.module_docstring: str = ""
        self.classes: list[dict[str, Any]] = []
        self.functions: list[dict[str, Any]] = []
        self._parse()

    # ---- parsing ----------------------------------------------------------

    def _parse(self) -> None:
        source = _read_text(self.path)
        if not source:
            return
        try:
            tree = ast.parse(source, filename=self.path)
        except SyntaxError:
            return

        self.module_docstring = ast.get_docstring(tree) or ""

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                self._parse_class(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._parse_func(node)

    def _parse_class(self, node: ast.ClassDef) -> None:
        doc = ast.get_docstring(node) or ""
        bases = []
        for b in node.bases:
            if isinstance(b, ast.Name):
                bases.append(b.id)
            elif isinstance(b, ast.Attribute):
                bases.append(ast.unparse(b))
        methods: list[dict[str, Any]] = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = _safe_unparse(item.args)
                ret = ""
                if item.returns:
                    ret = _safe_unparse(item.returns)
                methods.append({
                    "name": item.name,
                    "signature": sig,
                    "returns": ret,
                    "doc": (ast.get_docstring(item) or "")[:200],
                })
        self.classes.append({
            "name": node.name,
            "bases": bases,
            "doc": doc,
            "methods": methods,
        })

    def _parse_func(self, node) -> None:
        sig = _safe_unparse(node.args)
        ret = ""
        if node.returns:
            ret = _safe_unparse(node.returns)
        self.functions.append({
            "name": node.name,
            "signature": sig,
            "returns": ret,
            "doc": (ast.get_docstring(node) or "")[:200],
        })

    # ---- summary ----------------------------------------------------------

    def purpose(self) -> str:
        """Best-effort one-line purpose from the module docstring."""
        if not self.module_docstring:
            return "(no docstring)"
        first = self.module_docstring.strip().split("\n")[0]
        first = re.sub(r"\s+", " ", first)
        if len(first) > 120:
            first = first[:117] + "..."
        return first


def _safe_unparse(node) -> str:
    """AST-unparse a node to a string, falling back to repr."""
    try:
        return ast.unparse(node)
    except Exception:
        return repr(node)


# ---------------------------------------------------------------------------
# argparse subcommand introspection
# ---------------------------------------------------------------------------

def _parse_argparse_subcommands(source: str) -> list[dict[str, str]]:
    """Best-effort extraction of argparse subcommands from source.

    Looks for patterns like:
        sub = parser.add_subparsers(...)
        sub.add_parser("name", help="...")
    """
    results: list[dict[str, str]] = []
    # Generic pattern: add_parser("name", ...)
    for m in re.finditer(
        r'''add_parser\(\s*['"]([^'"]+)['"][^)]*help\s*=\s*['"]([^'"]*)['"]''',
        source,
    ):
        results.append({"name": m.group(1), "help": m.group(2)})
    # Also catch: add_parser("name") without help (next line may have help=)
    if not results:
        for m in re.finditer(r'''add_parser\(\s*['"]([^'"]+)['"]''', source):
            results.append({"name": m.group(1), "help": ""})
    return results


# ---------------------------------------------------------------------------
# Section generators
# ---------------------------------------------------------------------------

class ReadmeGenerator:
    """Build a README.md by inspecting the coder-harness codebase."""

    def __init__(self, base_dir: str = ".") -> None:
        self.base_dir = os.path.abspath(base_dir)
        self._py_files: list[_PythonModule] = []
        self._load_python_files()
        self._manifest: dict = {}
        self._load_manifest()
        self._projects_yaml: str = _read_text(os.path.join(self.base_dir, "projects.yaml"))
        self._schema_sql: str = _read_text(os.path.join(self.base_dir, "schema.sql"))

    # ---- loaders ----------------------------------------------------------

    def _load_python_files(self) -> None:
        pattern = os.path.join(self.base_dir, "**", "*.py")
        paths = glob.glob(pattern, recursive=True)
        # Exclude __pycache__
        paths = [p for p in paths if "__pycache__" not in p]
        paths.sort()
        self._py_files = [_PythonModule(p, self.base_dir) for p in paths]

    def _load_manifest(self) -> None:
        raw = _read_text(os.path.join(self.base_dir, "models", "manifest.json"))
        if raw:
            try:
                self._manifest = json.loads(raw)
            except json.JSONDecodeError:
                self._manifest = {}

    # ---- entry point ------------------------------------------------------

    def generate(self, output_path: str = "README.md") -> None:
        """Generate the full README.md and write it to *output_path*."""
        sections = [
            self._header(),
            self._quick_start(),
            self._architecture(),
            self._commands(),
            self._models(),
            self._projects(),
            self._schema(),
            self._file_inventory(),
            self._benchmark_results(),
            self._deployment(),
        ]
        readme = "\n\n".join(s for s in sections if s) + "\n"
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(readme)
        print(f"Generated {output_path} ({len(readme):,} bytes)")

    # ---- individual sections ----------------------------------------------

    def _header(self) -> str:
        return textwrap.dedent("""\
            # Coder Harness — Remote Coding Engine for Triton

            > Benchmark, evaluate, and run coding tasks on a dual-GPU Triton machine (RTX 3090 + RTX 3070) via BeeLlama.cpp inference.

            [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
            [![SQLite](https://img.shields.io/badge/database-sqlite-green.svg)](https://www.sqlite.org/)
            [![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](#license)

            ## What is this?

            Coder Harness is a **self-scoring, crash-resilient benchmark platform** for evaluating local
            LLM inference across model, GPU, and context-size combinations.  It sends standardized coding
            and orchestration tasks to [BeeLlama.cpp](https://github.com/windknown/beellama) endpoints
            over SSH, scores responses with an automated judge model, tracks everything in SQLite, and
            generates comparison reports with scatter plots.

            **Key capabilities:**

            - **6 model configurations** across 2 GPUs (RTX 3090 24 GB + RTX 3070 8 GB)
            - **23 benchmark tasks** spanning orchestrator, coder, and multi-turn roles
            - **Automated scoring** on 5 quality dimensions (completeness, correctness, quality, intelligence, role-fit)
            - **GPU fit matrix** — probe which configs fit at which context sizes
            - **Crash recovery** — checkpoint manager saves progress and resumes from the last good state
            - **GPU watchdog** — background thread polls temperatures and aborts on hardware failure
            - **Telemetry dashboard** — live monitoring of inference sessions
        """)

    def _quick_start(self) -> str:
        return textwrap.dedent("""\
            ## Quick Start

            ```bash
            cd /home/<user>/projects/ace-engine

            # 1. Preflight — verify SSH, GPUs, beellama, disk, load, stress
            python3 preflight.py

            # 2. Run the full benchmark pipeline (GPU fit → full run → scoring → report)
            python3 orchestrate.py --phase 1

            # 3. Run a single model config
            python3 orchestrate.py --config 3090-qwen36-35b

            # 4. Generate comparison report
            python3 report.py --format md

            # 5. Auto-generate this README
            python3 generate_readme.py
            ```

            > Operational runbook: [ACE-RUNBOOK.md](ACE-RUNBOOK.md) — start here to run a PRD through the ACE engine, swap model configs, or debug.
        """)

    def _architecture(self) -> str:
        return textwrap.dedent("""\
            ## Architecture

            ```
            nightmare (DSH Host)
              └─ orchestrate.py ──→ SSH ──→ Triton (<LAN_IP>)
                                               ├─ BeeLlama 3090 (port 8080)
                                               ├─ BeeLlama 3070 (port 8082)
                                               ├─ Gitea (port 3000)
                                               └─ Docker sandboxes
            ```

            ### Module Dependency Graph

            ```
            orchestrate.py       CLI entry — sequences the full pipeline
            ├── preflight.py     7-point health check
            │   └── ssh_utils.py SSH + beellama + nvidia-smi
            ├── gpu_fit.py       GPU fit matrix probe
            │   └── ssh_utils.py
            ├── runner.py        Inference execution + SQLite logging
            │   └── ssh_utils.py
            ├── judge.py         Automated scoring via judge model
            │   └── ssh_utils.py
            ├── watchdog.py      Background GPU temperature monitor
            │   └── ssh_utils.py
            ├── checkpoint.py    Crash recovery — save/resume progress
            └── report.py        Comparison tables + scatter plots
                                    └── (matplotlib, sqlite3)

            pilot.py             Methodology validation — standalone CLI
            ├── runner.py
            ├── judge.py
            └── ssh_utils.py

            remote_control.py    Remote session orchestration
            sandbox_manager.py   Docker sandbox lifecycle
            work_engine.py       Task routing and execution
            gitea_utils.py       Gitea API integration
            telemetry_dashboard.py  Live inference monitoring
            ```

            ### Data Flow

            ```
            corpus.json ──→ runner.py ──→ SQLite (benchmark_runs)
            manifest.json ──→ runner.py ──→ SQLite (model_configs)
                                          ↓
                                    judge.py ──→ SQLite (judge_scores)
                                          ↓
                                    report.py ──→ reports/benchmark-report.md
                                               ──→ reports/scatter-*.png
            ```

            ### Pipeline Sequence

            ```
            preflight → GPU fit → pilot (optional) → full run → scoring → report
                 │                    │                   │          │
                 │                    │                   │          └─ report.py
                 │                    │                   └─ judge.py (per run)
                 │                    └─ pilot.py (45 runs, go/no-go)
                 └─ preflight.py (7 checks)
            ```
        """)

    def _commands(self) -> str:
        """Auto-generate the Available Commands section from argparse introspection."""
        lines = ["## Available Commands", ""]

        # Collect subcommands from orchestrate.py (main entry point)
        orchestrate_src = _read_text(os.path.join(self.base_dir, "orchestrate.py"))
        pilotsrc = _read_text(os.path.join(self.base_dir, "pilot.py"))
        runsrc = _read_text(os.path.join(self.base_dir, "runner.py"))
        judgesrc = _read_text(os.path.join(self.base_dir, "judge.py"))
        gpufitsrc = _read_text(os.path.join(self.base_dir, "gpu_fit.py"))
        reportsrc = _read_text(os.path.join(self.base_dir, "report.py"))
        preflightsrc = _read_text(os.path.join(self.base_dir, "preflight.py"))
        remote_src = _read_text(os.path.join(self.base_dir, "remote_control.py"))
        dashboard_src = _read_text(os.path.join(self.base_dir, "telemetry_dashboard.py"))

        # Build a table of CLI entry points
        entries: list[dict[str, str]] = []

        def _add(module_name: str, cmd: str, purpose: str) -> None:
            entries.append({"module": module_name, "command": cmd, "purpose": purpose})

        _add("preflight.py", "python3 preflight.py", "Run 7-point preflight health check")
        _add("orchestrate.py", "python3 orchestrate.py --phase <0|1>", "Full benchmark pipeline (GPU fit or full run)")
        _add("orchestrate.py", "python3 orchestrate.py --config <id>", "Run a single model config")
        _add("orchestrate.py", "python3 orchestrate.py --resume", "Resume from last checkpoint")
        _add("orchestrate.py", "python3 orchestrate.py --dry-run", "Dry-run (no inference, no SSH)")
        _add("runner.py", "python3 runner.py --config <id> [--task <id>]", "Run benchmark tasks")
        _add("judge.py", "python3 judge.py --run-id <id>", "Score a single completed run")
        _add("judge.py", "python3 judge.py --batch", "Score all unscored runs")
        _add("judge.py", "python3 judge.py --summary", "Print scoring summary")
        _add("gpu_fit.py", "python3 gpu_fit.py [--config <id>]", "Probe GPU fit matrix")
        _add("gpu_fit.py", "python3 gpu_fit.py --show", "Display fit results")
        _add("pilot.py", "python3 pilot.py", "Run methodology pilot (45 runs)")
        _add("pilot.py", "python3 pilot.py --dry-run", "Preview pilot plan")
        _add("report.py", "python3 report.py --format md", "Generate Markdown report")
        _add("report.py", "python3 report.py --format json", "Generate JSON report")
        _add("remote_control.py", "python3 remote_control.py", "Remote session orchestration")
        _add("sandbox_manager.py", "python3 sandbox_manager.py", "Docker sandbox lifecycle")
        _add("work_engine.py", "python3 work_engine.py", "Task routing and execution")
        _add("gitea_utils.py", "python3 gitea_utils.py", "Gitea API integration")
        _add("telemetry_dashboard.py", "python3 telemetry_dashboard.py", "Live telemetry dashboard")

        # Parse argparse subcommands from orchestrate.py for richer detail
        subcmds = _parse_argparse_subcommands(orchestrate_src)
        if subcmds:
            lines.append("### Orchestrator Subcommands")
            lines.append("")
            lines.append("| Subcommand | Description |")
            lines.append("|------------|-------------|")
            for sc in subcmds:
                lines.append(f"| `{sc['name']}` | {sc['help'] or '—'} |")
            lines.append("")

        # General CLI reference table
        lines.append("### CLI Reference")
        lines.append("")
        lines.append("| Module | Command | Purpose |")
        lines.append("|--------|---------|---------|")
        for e in entries:
            lines.append(f"| `{e['module']}` | `{e['command']}` | {e['purpose']} |")
        lines.append("")

        # Global flags
        lines.append("### Common Flags")
        lines.append("")
        lines.append("| Flag | Description |")
        lines.append("|------|-------------|")
        lines.append("| `--dry-run` | Preview actions without executing inference or SSH |")
        lines.append("| `--resume` | Resume from last saved checkpoint |")
        lines.append("| `--config <id>` | Target a specific model configuration |")
        lines.append("| `--task <id>` | Target a specific benchmark task |")
        lines.append("| `--db <path>` | Custom SQLite database path |")
        lines.append("| `--sizes <csv>` | Comma-separated context sizes for GPU fit probe |")
        lines.append("| `--format md|json` | Output format for reports |")
        lines.append("")
        return "\n".join(lines)

    def _models(self) -> str:
        """Generate Model Configurations table from manifest.json."""
        lines = ["## Model Configurations", ""]
        configs = self._manifest.get("configs", [])
        if not configs:
            lines.append("*No model configurations found in `models/manifest.json`.*")
            return "\n".join(lines)

        lines.append("Auto-generated from [`models/manifest.json`](models/manifest.json).")
        lines.append("")
        lines.append("| Config ID | GPU | Model | VRAM | Context | Speculative | KVarN | Thinking |")
        lines.append("|-----------|-----|-------|------|---------|-------------|-------|----------|")
        for c in configs:
            vram = f"{c.get('expected_vram_mb', 0) / 1000:.1f} GB"
            ctx = f"{c.get('context_size', 0):,}"
            thinking = "✅" if c.get("thinking_enabled") else "—"
            lines.append(
                f"| `{c['id']}` | {c.get('gpu', '—')} | {c.get('model_name', '—')} "
                f"| {vram} | {ctx} | {c.get('speculative_type', '—')} "
                f"| {c.get('kvarn_level') or '—'} | {thinking} |"
            )
        lines.append("")

        # Additional notes table
        has_notes = any(c.get("notes") for c in configs)
        if has_notes:
            lines.append("### Configuration Notes")
            lines.append("")
            lines.append("| Config ID | Notes |")
            lines.append("|-----------|-------|")
            for c in configs:
                if c.get("notes"):
                    lines.append(f"| `{c['id']}` | {c['notes']} |")
            lines.append("")

        return "\n".join(lines)

    def _projects(self) -> str:
        """Generate Project Registry table from projects.yaml."""
        lines = ["## Project Registry", ""]
        if not self._projects_yaml.strip():
            lines.append("*No `projects.yaml` found.*")
            return "\n".join(lines)

        lines.append("Auto-generated from [`projects.yaml`](projects.yaml).")
        lines.append("")
        lines.append("| Project | Description | Language | Gitea Repo | Benchmarks |")
        lines.append("|---------|-------------|----------|------------|------------|")

        # Parse YAML manually (stdlib only, no pyyaml)
        current: dict[str, Any] = {}
        in_projects = False
        defaults: dict[str, Any] = {}
        in_defaults = False
        current_list_key: str | None = None

        def _clean_val(v: str) -> str:
            """Strip surrounding quotes and inline comments."""
            v = v.strip()
            # Remove trailing inline comment
            comment_idx = v.find("  #")
            if comment_idx > 0:
                v = v[:comment_idx].rstrip()
            # Strip quotes
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            return v

        for line in self._projects_yaml.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue

            # Detect top-level keys (no leading whitespace)
            if line and not line[0].isspace():
                current_list_key = None
                if stripped.startswith("projects:"):
                    in_projects = True
                    in_defaults = False
                    continue
                elif stripped.startswith("defaults:"):
                    in_defaults = True
                    in_projects = False
                    continue
                elif stripped.startswith("config_profiles:"):
                    in_projects = False
                    in_defaults = False
                    continue
                else:
                    in_projects = False
                    in_defaults = False

            if in_projects:
                # List item: "  - value"
                if stripped.startswith("- ") and current_list_key:
                    val = _clean_val(stripped[2:])
                    current.setdefault(current_list_key, []).append(val)
                    continue

                if stripped.startswith("- name:"):
                    if current:
                        self._emit_project_row(lines, current)
                    current = {"name": _clean_val(stripped.split(":", 1)[1])}
                    current_list_key = None
                elif ":" in stripped:
                    key = stripped.split(":", 1)[0].strip()
                    val = stripped.split(":", 1)[1].strip()
                    if key in ("entry_points", "benchmark_configs"):
                        current_list_key = key
                        current.setdefault(key, [])
                        # Handle inline list: [a, b, c]
                        if val.startswith("[") and val.endswith("]"):
                            items = [_clean_val(v) for v in val[1:-1].split(",") if v.strip()]
                            current[key].extend(items)
                    else:
                        current_list_key = None
                        current[key] = _clean_val(val)

            if in_defaults and ":" in stripped:
                key = stripped.split(":", 1)[0].strip()
                val = _clean_val(stripped.split(":", 1)[1])
                defaults[key] = val

        if current:
            self._emit_project_row(lines, current)
        lines.append("")

        # Defaults section
        if defaults:
            lines.append("### Default Settings")
            lines.append("")
            lines.append("| Setting | Value |")
            lines.append("|---------|-------|")
            for k, v in defaults.items():
                lines.append(f"| `{k}` | `{v}` |")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _emit_project_row(lines: list[str], p: dict) -> None:
        def _strip_quotes(v: str) -> str:
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                return v[1:-1]
            # Handle inline comment
            idx = v.find("#")
            if idx > 0:
                v = v[:idx].rstrip()
            return v

        name = _strip_quotes(p.get("name", "—"))
        desc = _strip_quotes(p.get("description", "—"))
        lang = _strip_quotes(p.get("language", p.get("type", "—")))
        gitea = _strip_quotes(p.get("gitea_repo", "—"))
        if gitea == "null":
            gitea = "—"
        configs = p.get("benchmark_configs", [])
        bench_str = ", ".join(f"`{_strip_quotes(c)}`" for c in configs) if configs else "—"
        lines.append(f"| `{name}` | {desc} | {lang} | {gitea} | {bench_str} |")

    def _schema(self) -> str:
        """Generate Database Schema section from schema.sql."""
        lines = ["## Database Schema", ""]
        if not self._schema_sql.strip():
            lines.append("*No `schema.sql` found.*")
            return "\n".join(lines)

        lines.append("Auto-generated from [`schema.sql`](schema.sql).  ")
        lines.append("SQLite database: `benchmark-results.db`")
        lines.append("")

        # Extract CREATE TABLE blocks
        tables = re.findall(
            r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);",
            self._schema_sql,
            re.DOTALL,
        )

        # Extract the comment line that immediately precedes each CREATE TABLE
        table_comments: dict[str, str] = {}
        lines_sql = self._schema_sql.splitlines()
        for i, line in enumerate(lines_sql):
            m = re.match(r"\s*CREATE TABLE IF NOT EXISTS (\w+)", line)
            if m:
                tname = m.group(1)
                # Walk backwards to find the most recent descriptive comment
                for j in range(i - 1, max(i - 5, -1), -1):
                    cl = lines_sql[j].strip()
                    if cl.startswith("--"):
                        text = cl.lstrip("-").strip()
                        # Skip separator lines like "==="
                        if text and not all(c in "=─━" for c in text):
                            table_comments[tname] = text
                            break

        lines.append(f"**{len(tables)} tables** in the schema:")
        lines.append("")

        # Summary table
        lines.append("| Table | Purpose |")
        lines.append("|-------|---------|")
        for tname, _ in tables:
            purpose = table_comments.get(tname, "—")
            lines.append(f"| `{tname}` | {purpose} |")
        lines.append("")

        # Full DDL
        lines.append("### Full DDL")
        lines.append("")
        lines.append("```sql")
        lines.append(self._schema_sql.strip())
        lines.append("```")
        lines.append("")

        # Indexes
        indexes = re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)\s+ON\s+\w+\(([^)]+)\)", self._schema_sql)
        if indexes:
            lines.append("### Indexes")
            lines.append("")
            lines.append("| Index | Columns |")
            lines.append("|-------|---------|")
            for iname, cols in indexes:
                lines.append(f"| `{iname}` | `{cols}` |")
            lines.append("")

        return "\n".join(lines)

    def _file_inventory(self) -> str:
        """Auto-discover all .py files with line counts and purposes."""
        lines = ["## File Inventory", ""]
        lines.append("Auto-discovered from the codebase.  ")
        lines.append(f"**{len(self._py_files)} Python files**")
        lines.append("")

        # Group by directory
        dir_groups: dict[str, list[_PythonModule]] = {}
        for pm in self._py_files:
            d = os.path.dirname(pm.rel) or "."
            dir_groups.setdefault(d, []).append(pm)

        total_lines = sum(pm.line_count for pm in self._py_files)

        lines.append(f"**Total: {total_lines:,} lines of Python**")
        lines.append("")

        for d in sorted(dir_groups):
            files = dir_groups[d]
            if d != ".":
                lines.append(f"### `{d}/`")
                lines.append("")
            lines.append("| File | Lines | Purpose |")
            lines.append("|------|-------|---------|")
            for pm in sorted(files, key=lambda x: x.basename):
                lines.append(f"| `{pm.basename}` | {pm.line_count} | {pm.purpose()} |")
            lines.append("")

        return "\n".join(lines)

    def _benchmark_results(self) -> str:
        """Reference to the latest benchmark report and database."""
        lines = ["## Benchmark Results", ""]

        # Check if reports exist
        report_md = os.path.join(self.base_dir, "reports", "benchmark-report.md")
        report_json = os.path.join(self.base_dir, "reports", "benchmark-report.json")
        db_path = os.path.join(self.base_dir, "benchmark-results.db")

        if os.path.exists(report_md):
            lines.append("**Latest report:** [`reports/benchmark-report.md`](reports/benchmark-report.md)")
        else:
            lines.append("*No benchmark report generated yet.  Run `python3 report.py --format md` to generate one.*")

        if os.path.exists(report_json):
            lines.append(f"**Machine-readable:** [`reports/benchmark-report.json`](reports/benchmark-report.json)")

        # Scatter plots
        scatter_dir = os.path.join(self.base_dir, "reports")
        scatters = sorted(glob.glob(os.path.join(scatter_dir, "scatter-*.png")))
        if scatters:
            lines.append("")
            lines.append("### Scatter Plots")
            lines.append("")
            for s in scatters:
                name = os.path.basename(s)
                lines.append(f"- [`{name}`](reports/{name})")

        # Database
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            lines.append("")
            lines.append(f"**Database:** `benchmark-results.db` ({db_size:,} bytes)")
            lines.append("")

            # Try to get some stats
            try:
                import sqlite3
                conn = sqlite3.connect(db_path)
                try:
                    run_count = conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0]
                    score_count = conn.execute("SELECT COUNT(*) FROM judge_scores").fetchone()[0]
                    model_count = conn.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0]
                    task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                    lines.append(f"| Metric | Value |")
                    lines.append(f"|--------|-------|")
                    lines.append(f"| Model configs | {model_count} |")
                    lines.append(f"| Tasks | {task_count} |")
                    lines.append(f"| Benchmark runs | {run_count} |")
                    lines.append(f"| Judge scores | {score_count} |")
                except Exception:
                    pass
                finally:
                    conn.close()
            except Exception:
                pass

        lines.append("")
        lines.append("### Example Queries")
        lines.append("")
        lines.append("```sql")
        lines.append("-- Average throughput per config")
        lines.append("SELECT model_config_id, AVG(predicted_per_second) AS avg_tps")
        lines.append("FROM benchmark_runs WHERE status='complete'")
        lines.append("GROUP BY model_config_id ORDER BY avg_tps DESC;")
        lines.append("")
        lines.append("-- Best config for coder tasks")
        lines.append("SELECT br.model_config_id, AVG(js.overall) AS avg_score")
        lines.append("FROM benchmark_runs br")
        lines.append("JOIN judge_scores js ON br.id = js.run_id")
        lines.append("WHERE br.status='complete'")
        lines.append("  AND br.task_id IN (SELECT id FROM tasks WHERE role='coder')")
        lines.append("GROUP BY br.model_config_id ORDER BY avg_score DESC;")
        lines.append("```")
        lines.append("")

        return "\n".join(lines)

    def _deployment(self) -> str:
        """Link to deployment docs."""
        lines = ["## Deployment", ""]
        deploy_readme = os.path.join(self.base_dir, "tickets", "deploy", "README.md")
        if os.path.exists(deploy_readme):
            lines.append("For Triton deployment instructions, see [`tickets/deploy/README.md`](tickets/deploy/README.md).")
        else:
            lines.append("*Deployment docs not found.*")

        lines.append("")
        lines.append("### Triton Machine")
        lines.append("")
        lines.append("| Property | Value |")
        lines.append("|----------|-------|")
        lines.append("| Host | `<LAN_IP>` |")
        lines.append("| User | `<user>` |")
        lines.append("| GPU 0 | RTX 3090 24 GB |")
        lines.append("| GPU 1 | RTX 3070 8 GB |")
        lines.append("| Docker | 29.6.1 |")
        lines.append("| BeeLlama 3090 | port 8080 |")
        lines.append("| BeeLlama 3070 | port 8082 |")
        lines.append("")

        # Docker Compose profiles
        compose_path = os.path.join(self.base_dir, "tickets", "deploy", "docker-compose.yml")
        if os.path.exists(compose_path):
            compose_src = _read_text(compose_path)
            profiles = re.findall(r"^\s+([a-z][\w-]+):\s*$", compose_src, re.MULTILINE)
            # Filter to likely profile names (config-*)
            profiles = [p for p in profiles if p.startswith("config-")]
            if profiles:
                lines.append("### Docker Compose Profiles")
                lines.append("")
                lines.append("| Profile | Command |")
                lines.append("|---------|---------|")
                for p in sorted(set(profiles)):
                    lines.append(f"| `{p}` | `docker compose --profile {p} up -d` |")
                lines.append("")

        # Benchmark scripts
        scripts_dir = os.path.join(self.base_dir, "tickets", "deploy", "scripts")
        if os.path.isdir(scripts_dir):
            bench_scripts = sorted(glob.glob(os.path.join(scripts_dir, "bench-*.sh")))
            if bench_scripts:
                lines.append("### Benchmark Scripts (on Triton)")
                lines.append("")
                lines.append("| Script | Purpose |")
                lines.append("|--------|---------|")
                for s in bench_scripts:
                    name = os.path.basename(s)
                    lines.append(f"| [`{name}`](tickets/deploy/scripts/{name}) | (see script) |")
                lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(f"*Auto-generated by [`generate_readme.py`](generate_readme.py) from the coder-harness codebase.*")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-generate README.md for the coder-harness project.",
    )
    parser.add_argument(
        "-o", "--output",
        default="README.md",
        help="Output file path (default: README.md)",
    )
    parser.add_argument(
        "-d", "--dir",
        default=".",
        help="Base directory to inspect (default: current directory)",
    )
    args = parser.parse_args()

    gen = ReadmeGenerator(base_dir=args.dir)
    gen.generate(output_path=args.output)


if __name__ == "__main__":
    main()
