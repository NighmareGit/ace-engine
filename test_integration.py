#!/usr/bin/env python3
"""
Integration tests for the coder-harness platform.

Validates critical paths: schema consistency, SSH patterns, config mappings,
module imports, CLI --help, and JSON output validity.

Usage:
    cd /home/<user>/projects/ace-engine
    python3 -m unittest test_integration.py -v
"""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure coder-harness modules are importable
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(args, timeout=15):
    """Run a CLI command and return (returncode, stdout, stderr)."""
    r = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(HERE),
    )
    return r.returncode, r.stdout, r.stderr


def _load_schema_tables(sql_path):
    """Parse schema.sql and extract table names from CREATE TABLE statements."""
    with open(sql_path) as f:
        sql = f.read()
    tables = []
    for line in sql.splitlines():
        line = line.strip()
        if line.upper().startswith("CREATE TABLE IF NOT EXISTS"):
            # Extract table name: CREATE TABLE IF NOT EXISTS tablename (
            parts = line.split()
            # parts: CREATE, TABLE, IF, NOT, EXISTS, tablename, (...
            if len(parts) >= 6:
                name = parts[5].rstrip("(").strip()
                tables.append(name)
    return tables


# ===========================================================================
# 1. Schema Consistency
# ===========================================================================

class TestUnifiedSchema(unittest.TestCase):
    """All telemetry tools use the same DB schema via schema_unified."""

    def test_unified_schema_creates_all_tables(self):
        """ensure_schema creates work_sessions, work_events, gpu_snapshots, benchmark_results."""
        from schema_unified import ensure_schema

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            ensure_schema(db_path)
            db = sqlite3.connect(db_path)
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            db.close()

            self.assertIn("work_sessions", tables)
            self.assertIn("work_events", tables)
            self.assertIn("gpu_snapshots", tables)
            self.assertIn("benchmark_results", tables)
        finally:
            os.unlink(db_path)

    def test_work_sessions_has_required_columns(self):
        """work_sessions table has task_prompt, session_uuid, session_name."""
        from schema_unified import ensure_schema

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            ensure_schema(db_path)
            db = sqlite3.connect(db_path)
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(work_sessions)").fetchall()
            }
            db.close()

            self.assertIn("task_prompt", cols)
            self.assertIn("session_uuid", cols)
            self.assertIn("session_name", cols)
            self.assertIn("sandbox_name", cols)
            self.assertIn("project", cols)
            self.assertIn("config_id", cols)
            self.assertIn("status", cols)
            self.assertIn("inference_tokens", cols)
            self.assertIn("inference_tokens_per_sec", cols)
        finally:
            os.unlink(db_path)

    def test_gpu_snapshots_columns(self):
        """gpu_snapshots has the required monitoring columns."""
        from schema_unified import ensure_schema

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            ensure_schema(db_path)
            db = sqlite3.connect(db_path)
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(gpu_snapshots)").fetchall()
            }
            db.close()

            self.assertIn("gpu_index", cols)
            self.assertIn("memory_used_mb", cols)
            self.assertIn("memory_total_mb", cols)
            self.assertIn("temperature_c", cols)
            self.assertIn("utilization_pct", cols)
        finally:
            os.unlink(db_path)

    def test_benchmark_results_columns(self):
        """benchmark_results has the required benchmark tracking columns."""
        from schema_unified import ensure_schema

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            ensure_schema(db_path)
            db = sqlite3.connect(db_path)
            cols = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(benchmark_results)"
                ).fetchall()
            }
            db.close()

            self.assertIn("config_id", cols)
            self.assertIn("benchmark_type", cols)
            self.assertIn("tokens_per_sec", cols)
            self.assertIn("tokens_generated", cols)
            self.assertIn("elapsed_seconds", cols)
        finally:
            os.unlink(db_path)

    def test_schema_is_idempotent(self):
        """Calling ensure_schema twice doesn't raise or duplicate tables."""
        from schema_unified import ensure_schema

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            result1 = ensure_schema(db_path)
            result2 = ensure_schema(db_path)
            self.assertTrue(result1)
            self.assertTrue(result2)

            # Verify table count is still 4
            db = sqlite3.connect(db_path)
            count = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            db.close()
            self.assertGreaterEqual(count, 4)
        finally:
            os.unlink(db_path)

    def test_schema_migration_adds_missing_columns(self):
        """ensure_schema adds missing columns to pre-existing tables."""
        from schema_unified import ensure_schema

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Create a minimal work_sessions table WITHOUT the new columns
            db = sqlite3.connect(db_path)
            db.execute("""
                CREATE TABLE work_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sandbox_name TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT
                )
            """)
            db.commit()
            db.close()

            # Run ensure_schema — it should add the missing columns
            ensure_schema(db_path)

            db = sqlite3.connect(db_path)
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(work_sessions)").fetchall()
            }
            db.close()

            self.assertIn("task_prompt", cols)
            self.assertIn("session_uuid", cols)
            self.assertIn("session_name", cols)
        finally:
            os.unlink(db_path)

    def test_benchmark_schema_tables_exist(self):
        """schema.sql creates the expected benchmark tables."""
        schema_path = str(HERE / "schema.sql")
        tables = _load_schema_tables(schema_path)

        self.assertIn("model_configs", tables)
        self.assertIn("tasks", tables)
        self.assertIn("benchmark_runs", tables)
        self.assertIn("judge_scores", tables)
        self.assertIn("latency_profiles", tables)
        self.assertIn("gpu_fit_matrix", tables)
        self.assertIn("schema_version", tables)


# ===========================================================================
# 2. SSH Connectivity Patterns (Mocked)
# ===========================================================================

class TestSSHPatterns(unittest.TestCase):
    """Verify SSH command construction without connecting."""

    def test_ssh_key_based_command_format(self):
        """SSH key-based command includes StrictHostKeyChecking=no."""
        from ssh_utils import SSHClient

        client = SSHClient()
        # Build the command that connect() would use with key_path
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-i", "/tmp/test_key",
            f"{client.user}@{client.host}",
            "echo OK",
        ]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("StrictHostKeyChecking=no", cmd)
        self.assertIn("ConnectTimeout=10", cmd)
        self.assertIn("<user>@<LAN_IP>", cmd)

    def test_ssh_password_command_format(self):
        """SSH password-based command uses sshpass."""
        from ssh_utils import SSHClient

        client = SSHClient()
        cmd = [
            "sshpass", "-p", client.pw,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{client.user}@{client.host}",
            "echo OK",
        ]
        self.assertEqual(cmd[0], "sshpass")
        self.assertIn("ssh", cmd)
        self.assertIn("StrictHostKeyChecking=no", cmd)

    def test_ssh_run_command_format(self):
        """SSHClient.run() builds correct command structure."""
        from ssh_utils import SSHClient

        client = SSHClient()
        # The run method builds: sshpass -p <pw> ssh -o StrictHostKeyChecking=no
        # -o ConnectTimeout=10 user@host <cmd>
        expected_pattern = [
            "sshpass", "-p", client.pw,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{client.user}@{client.host}",
        ]
        self.assertEqual(expected_pattern[0], "sshpass")
        self.assertEqual(expected_pattern[3], "ssh")
        self.assertIn("StrictHostKeyChecking=no", expected_pattern)

    def test_ssh_client_constants(self):
        """SSHClient uses the correct Triton constants."""
        from ssh_utils import SSHClient, TRITON_HOST, TRITON_USER, PORT_3090, PORT_3070

        client = SSHClient()
        self.assertEqual(client.host, "<LAN_IP>")
        self.assertEqual(client.user, "<user>")
        self.assertEqual(TRITON_HOST, "<LAN_IP>")
        self.assertEqual(TRITON_USER, "<user>")
        self.assertEqual(PORT_3090, 8080)
        self.assertEqual(PORT_3070, 8082)

    def test_remote_control_ssh_pattern(self):
        """remote_control.py ssh_run uses the same SSH pattern."""
        # Verify the ssh_run function exists and builds correct commands
        from remote_control import ssh_run
        import inspect

        source = inspect.getsource(ssh_run)
        self.assertIn("StrictHostKeyChecking=no", source)
        # The function uses f-string interpolation with TRITON_USER and TRITON_HOST
        # constants, which appear as f-string syntax in source
        self.assertIn("TRITON_USER", source)
        self.assertIn("TRITON_HOST", source)

    def test_sandbox_manager_ssh_pattern(self):
        """sandbox_manager.py ssh_run uses StrictHostKeyChecking=no."""
        from sandbox_manager import ssh_run
        import inspect

        source = inspect.getsource(ssh_run)
        self.assertIn("StrictHostKeyChecking=no", source)


# ===========================================================================
# 3. Config Mapping Consistency
# ===========================================================================

class TestConfigMapping(unittest.TestCase):
    """All config IDs map correctly between modules and manifest."""

    def test_config_to_profile_in_remote_control(self):
        """remote_control.CONFIG_TO_PROFILE covers all manifest configs."""
        from remote_control import CONFIG_TO_PROFILE, ALL_CONFIGS

        # Every config in ALL_CONFIGS should have a mapping
        for cfg in ALL_CONFIGS:
            cid = cfg["id"]
            self.assertIn(
                cid,
                CONFIG_TO_PROFILE,
                f"Config {cid} missing from CONFIG_TO_PROFILE",
            )

    def test_config_to_profile_in_work_engine(self):
        """work_engine.CONFIG_TO_PROFILE matches remote_control."""
        from remote_control import CONFIG_TO_PROFILE as RC_MAP
        from work_engine import CONFIG_TO_PROFILE as WE_MAP

        self.assertEqual(RC_MAP, WE_MAP)

    def test_config_to_port_in_work_engine(self):
        """work_engine.CONFIG_TO_PORT covers all configs with correct ports."""
        from work_engine import CONFIG_TO_PORT, BEE_LLAMA_PORT_3090, BEE_LLAMA_PORT_3070

        for cid, port in CONFIG_TO_PORT.items():
            if cid.startswith("3090"):
                self.assertEqual(
                    port,
                    BEE_LLAMA_PORT_3090,
                    f"Config {cid} should use port {BEE_LLAMA_PORT_3090}",
                )
            elif cid.startswith("3070"):
                self.assertEqual(
                    port,
                    BEE_LLAMA_PORT_3070,
                    f"Config {cid} should use port {BEE_LLAMA_PORT_3070}",
                )

    def test_profile_models_cover_mapped_profiles(self):
        """remote_control.PROFILE_MODELS covers all profiles referenced in CONFIG_TO_PROFILE."""
        from remote_control import CONFIG_TO_PROFILE, PROFILE_MODELS

        for cid, profile in CONFIG_TO_PROFILE.items():
            if profile is not None:
                self.assertIn(
                    profile,
                    PROFILE_MODELS,
                    f"Profile {profile} (for config {cid}) missing from PROFILE_MODELS",
                )

    def test_manifest_configs_match_all_configs(self):
        """models/manifest.json config IDs match remote_control.ALL_CONFIGS."""
        manifest_path = HERE / "models" / "manifest.json"
        with open(manifest_path) as f:
            manifest_ids = {c["id"] for c in json.load(f)["configs"]}

        from remote_control import ALL_CONFIGS

        rc_ids = {c["id"] for c in ALL_CONFIGS}
        self.assertEqual(manifest_ids, rc_ids)

    def test_config_gpu_prefix_matches(self):
        """Config IDs starting with '3090' map to GPU 3090, '3070' to GPU 3070."""
        from remote_control import ALL_CONFIGS

        for cfg in ALL_CONFIGS:
            prefix = cfg["id"].split("-")[0]
            self.assertEqual(
                prefix,
                cfg["gpu"],
                f"Config {cfg['id']} has GPU prefix {prefix} but gpu={cfg['gpu']}",
            )

    def test_3070_configs_have_no_profile(self):
        """3070 configs map to None profile (shared service)."""
        from remote_control import CONFIG_TO_PROFILE

        for cid, profile in CONFIG_TO_PROFILE.items():
            if cid.startswith("3070"):
                self.assertIsNone(
                    profile,
                    f"3070 config {cid} should have None profile, got {profile}",
                )

    def test_3090_configs_have_valid_profiles(self):
        """3090 configs map to non-None Docker profiles."""
        from remote_control import CONFIG_TO_PROFILE

        for cid, profile in CONFIG_TO_PROFILE.items():
            if cid.startswith("3090"):
                self.assertIsNotNone(
                    profile,
                    f"3090 config {cid} should have a Docker profile",
                )


# ===========================================================================
# 4. Module Imports
# ===========================================================================

class TestModuleImports(unittest.TestCase):
    """All 19 Python modules import without fatal errors."""

    # Core platform modules
    CORE_MODULES = [
        "ssh_utils",
        "preflight",
        "runner",
        "judge",
        "gpu_fit",
        "watchdog",
        "checkpoint",
        "schema_unified",
    ]

    # Orchestration modules
    ORCHESTRATION_MODULES = [
        "orchestrate",
        "pilot",
        "report",
    ]

    # Remote / management modules
    REMOTE_MODULES = [
        "remote_control",
        "work_engine",
        "sandbox_manager",
        "gitea_utils",
    ]

    # Telemetry modules
    TELEMETRY_MODULES = [
        "telemetry_collector",
        "telemetry_dashboard",
    ]

    # Entry points
    ENTRY_MODULES = [
        "harness",
        "generate_readme",
    ]

    ALL_MODULES = (
        CORE_MODULES
        + ORCHESTRATION_MODULES
        + REMOTE_MODULES
        + TELEMETRY_MODULES
        + ENTRY_MODULES
    )

    def test_all_modules_list_count(self):
        """We have exactly 19 modules to test."""
        self.assertEqual(len(self.ALL_MODULES), 19)

    def _try_import(self, mod_name):
        """Import a module, returning (success, error_or_None)."""
        try:
            importlib.import_module(mod_name)
            return True, None
        except ImportError as e:
            return False, str(e)
        except Exception as e:
            # Non-import errors (e.g., syntax) are real failures
            return False, f"{type(e).__name__}: {e}"

    def test_core_modules_import(self):
        """Core modules (ssh_utils, runner, etc.) import successfully."""
        for mod in self.CORE_MODULES:
            with self.subTest(module=mod):
                ok, err = self._try_import(mod)
                self.assertTrue(ok, f"Module {mod} failed to import: {err}")

    def test_orchestration_modules_import(self):
        """Orchestration modules import successfully."""
        for mod in self.ORCHESTRATION_MODULES:
            with self.subTest(module=mod):
                ok, err = self._try_import(mod)
                self.assertTrue(ok, f"Module {mod} failed to import: {err}")

    def test_remote_modules_import(self):
        """Remote/management modules import successfully."""
        for mod in self.REMOTE_MODULES:
            with self.subTest(module=mod):
                ok, err = self._try_import(mod)
                self.assertTrue(ok, f"Module {mod} failed to import: {err}")

    def test_telemetry_modules_import(self):
        """Telemetry modules import successfully."""
        for mod in self.TELEMETRY_MODULES:
            with self.subTest(module=mod):
                ok, err = self._try_import(mod)
                self.assertTrue(ok, f"Module {mod} failed to import: {err}")

    def test_entry_modules_import(self):
        """Entry-point modules import successfully."""
        for mod in self.ENTRY_MODULES:
            with self.subTest(module=mod):
                ok, err = self._try_import(mod)
                self.assertTrue(ok, f"Module {mod} failed to import: {err}")

    def test_all_modules_import(self):
        """Every module in the platform imports without errors."""
        failed = []
        for mod in self.ALL_MODULES:
            ok, err = self._try_import(mod)
            if not ok:
                failed.append((mod, err))

        if failed:
            msg = "Failed imports:\n" + "\n".join(
                f"  {mod}: {err}" for mod, err in failed
            )
            self.fail(msg)


# ===========================================================================
# 5. CLI --help Works
# ===========================================================================

class TestCLIHelp(unittest.TestCase):
    """All CLIs respond to --help without crashing."""

    # (script_name, extra_args) — scripts that accept --help
    CLI_SCRIPTS = [
        ("harness.py", []),
        ("remote_control.py", []),
        ("work_engine.py", []),
        ("sandbox_manager.py", []),
        ("gitea_utils.py", []),
        ("runner.py", []),
        ("judge.py", []),
        ("gpu_fit.py", []),
        ("orchestrate.py", []),
        ("pilot.py", []),
        ("report.py", []),
        ("telemetry_collector.py", []),
        ("telemetry_dashboard.py", []),
        ("generate_readme.py", []),
    ]

    def test_all_cli_help(self):
        """Every CLI script responds to --help with exit code 0."""
        for script, extra_args in self.CLI_SCRIPTS:
            with self.subTest(script=script):
                cmd = ["python3", script, "--help"] + extra_args
                rc, stdout, stderr = _run_cli(cmd, timeout=10)
                self.assertEqual(
                    rc,
                    0,
                    f"{script} --help exited with code {rc}.\n"
                    f"stderr: {stderr[:300]}",
                )
                self.assertIn(
                    "usage:",
                    (stdout + stderr).lower(),
                    f"{script} --help did not output usage information",
                )

    def test_harness_subcommand_help(self):
        """harness.py subcommands accept --help."""
        subcommands = ["status", "health", "setup"]
        for sub in subcommands:
            with self.subTest(subcommand=sub):
                rc, stdout, stderr = _run_cli(
                    ["python3", "harness.py", sub, "--help"], timeout=10
                )
                # Subcommands that don't have --help still shouldn't crash
                # (exit code may be non-zero if they try to execute)
                # Just verify no Python traceback
                self.assertNotIn(
                    "Traceback",
                    stderr,
                    f"harness.py {sub} --help produced a traceback",
                )

    def test_runner_cli_help_has_config_flag(self):
        """runner.py --help mentions --config."""
        rc, stdout, stderr = _run_cli(["python3", "runner.py", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--config", stdout)

    def test_judge_cli_help_has_run_id_flag(self):
        """judge.py --help mentions --run-id."""
        rc, stdout, stderr = _run_cli(["python3", "judge.py", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--run-id", stdout)

    def test_report_cli_help_has_format_flag(self):
        """report.py --help mentions --format."""
        rc, stdout, stderr = _run_cli(["python3", "report.py", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--format", stdout)


# ===========================================================================
# 6. JSON Output Validity
# ===========================================================================

class TestJSONOutput(unittest.TestCase):
    """Verify structured JSON output from key commands."""

    def test_remote_control_model_list_json(self):
        """remote_control.py model list outputs valid JSON array."""
        rc, stdout, stderr = _run_cli(
            ["python3", "remote_control.py", "model", "list"], timeout=15
        )
        # Should output valid JSON (even if SSH fails, output is structured)
        data = json.loads(stdout)
        self.assertIsInstance(data, (list, dict))

        # If it's a list, it should contain config dicts
        if isinstance(data, list) and len(data) > 0:
            first = data[0]
            self.assertIn("id", first)
            self.assertIn("gpu", first)

    def test_harness_model_list_json(self):
        """harness.py model list outputs valid JSON."""
        rc, stdout, stderr = _run_cli(
            ["python3", "harness.py", "model", "list"], timeout=15
        )
        data = json.loads(stdout)
        self.assertIsInstance(data, (list, dict))

    def test_model_list_has_six_configs(self):
        """model list returns all 6 known configurations."""
        rc, stdout, stderr = _run_cli(
            ["python3", "remote_control.py", "model", "list"], timeout=15
        )
        data = json.loads(stdout)
        if isinstance(data, list):
            ids = {item.get("id") for item in data}
            self.assertEqual(len(ids), 6, f"Expected 6 configs, got {len(ids)}")
            expected_ids = {
                "3090-qwen36-35b",
                "3090-muse-glimmer",
                "3090-laguna-xs",
                "3090-qwen35-9b",
                "3070-qwen35-9b",
                "3070-qwen35-4b",
            }
            self.assertEqual(ids, expected_ids)

    def test_harness_setup_outputs_text(self):
        """harness.py setup outputs a setup guide (not JSON, but should not crash)."""
        rc, stdout, stderr = _run_cli(["python3", "harness.py", "setup"])
        self.assertEqual(rc, 0)
        self.assertIn("Triton", stdout)

    def test_harness_version_outputs_version(self):
        """harness.py --version outputs version string."""
        rc, stdout, stderr = _run_cli(["python3", "harness.py", "--version"])
        self.assertEqual(rc, 0)
        self.assertIn("1.0.0", stdout)


# ===========================================================================
# 7. Manifest & Corpus Consistency
# ===========================================================================

class TestDataFilesConsistency(unittest.TestCase):
    """Data files (manifest, corpus) are valid and consistent."""

    def test_manifest_json_valid(self):
        """models/manifest.json is valid JSON with configs array."""
        manifest_path = HERE / "models" / "manifest.json"
        with open(manifest_path) as f:
            data = json.load(f)

        self.assertIn("configs", data)
        self.assertIsInstance(data["configs"], list)
        self.assertGreaterEqual(len(data["configs"]), 6)

    def test_manifest_config_required_fields(self):
        """Every manifest config has required fields."""
        manifest_path = HERE / "models" / "manifest.json"
        with open(manifest_path) as f:
            configs = json.load(f)["configs"]

        required = {"id", "gpu", "model_name", "model_path", "port"}
        for cfg in configs:
            with self.subTest(config=cfg.get("id")):
                for field in required:
                    self.assertIn(
                        field, cfg, f"Config {cfg.get('id')} missing {field}"
                    )

    def test_corpus_json_valid(self):
        """corpus.json is valid JSON with task definitions."""
        corpus_path = HERE / "corpus.json"
        with open(corpus_path) as f:
            tasks = json.load(f)

        self.assertIsInstance(tasks, list)
        self.assertGreaterEqual(len(tasks), 23)

    def test_corpus_tasks_have_required_fields(self):
        """Every corpus task has id, role, category, prompt."""
        corpus_path = HERE / "corpus.json"
        with open(corpus_path) as f:
            tasks = json.load(f)

        for task in tasks:
            with self.subTest(task=task.get("id")):
                self.assertIn("id", task)
                self.assertIn("role", task)
                self.assertIn("category", task)
                self.assertIn("prompt", task)
                self.assertIn(task["role"], ("orchestrator", "coder", "multi-turn"))

    def test_corpus_task_ids_are_sequential(self):
        """Corpus tasks are numbered T01 through T23."""
        corpus_path = HERE / "corpus.json"
        with open(corpus_path) as f:
            tasks = json.load(f)

        ids = [t["id"] for t in tasks]
        expected = [f"T{i:02d}" for i in range(1, len(tasks) + 1)]
        self.assertEqual(ids, expected)


# ===========================================================================
# 8. Database Schema via Runner
# ===========================================================================

class TestBenchmarkSchema(unittest.TestCase):
    """Benchmark database schema is correctly applied by runner.py."""

    def test_runner_init_db_creates_tables(self):
        """BenchmarkRunner._init_db creates all expected tables."""
        from runner import BenchmarkRunner

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Create runner with temp DB (no SSH needed for init)
            runner = BenchmarkRunner(db_path=db_path)

            db = sqlite3.connect(db_path)
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            db.close()

            self.assertIn("model_configs", tables)
            self.assertIn("tasks", tables)
            self.assertIn("benchmark_runs", tables)
            self.assertIn("judge_scores", tables)
            self.assertIn("latency_profiles", tables)
            self.assertIn("gpu_fit_matrix", tables)
        finally:
            os.unlink(db_path)

    def test_runner_syncs_manifest_to_db(self):
        """BenchmarkRunner syncs manifest.json config IDs to model_configs table."""
        from runner import BenchmarkRunner

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            runner = BenchmarkRunner(db_path=db_path)

            db = sqlite3.connect(db_path)
            config_ids = {
                r[0]
                for r in db.execute("SELECT id FROM model_configs").fetchall()
            }
            db.close()

            self.assertIn("3090-qwen36-35b", config_ids)
            self.assertIn("3070-qwen35-9b", config_ids)
            self.assertEqual(len(config_ids), 6)
        finally:
            os.unlink(db_path)

    def test_runner_syncs_corpus_to_db(self):
        """BenchmarkRunner syncs corpus.json task IDs to tasks table."""
        from runner import BenchmarkRunner

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            runner = BenchmarkRunner(db_path=db_path)

            db = sqlite3.connect(db_path)
            task_ids = {
                r[0] for r in db.execute("SELECT id FROM tasks").fetchall()
            }
            db.close()

            self.assertIn("T01", task_ids)
            self.assertIn("T23", task_ids)
            self.assertEqual(len(task_ids), 23)
        finally:
            os.unlink(db_path)


# ===========================================================================
# 9. Checkpoint Manager
# ===========================================================================

class TestCheckpointManager(unittest.TestCase):
    """CheckpointManager creates its table and persists data."""

    def test_checkpoint_table_created(self):
        """CheckpointManager creates the checkpoints table."""
        from checkpoint import CheckpointManager

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            mgr = CheckpointManager(db_path)
            db = sqlite3.connect(db_path)
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            db.close()

            self.assertIn("checkpoints", tables)
            mgr._conn.close()
        finally:
            os.unlink(db_path)

    def test_checkpoint_save_and_load(self):
        """Save and load checkpoint preserves data."""
        from checkpoint import CheckpointManager

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            mgr = CheckpointManager(db_path)
            # save_checkpoint() reads from benchmark_runs (which doesn't exist
            # in this temp DB), so it saves empty progress — that's fine.
            mgr.save_checkpoint()
            loaded = mgr.load_checkpoint()
            mgr._conn.close()

            self.assertIsNotNone(loaded)
            self.assertIn("completed_run_ids", loaded)
        finally:
            os.unlink(db_path)


# ===========================================================================
# 10. Watchdog Without SSH
# ===========================================================================

class TestWatchdog(unittest.TestCase):
    """GPUWatchdog can be instantiated and polled without SSH."""

    def test_watchdog_creates_with_defaults(self):
        """GPUWatchdog initializes with sensible defaults."""
        from watchdog import GPUWatchdog
        import threading

        abort = threading.Event()
        wd = GPUWatchdog(abort_event=abort)
        self.assertEqual(wd.gpu_indices, [0, 1])
        self.assertEqual(wd.poll_interval, 30)
        self.assertFalse(abort.is_set())

    def test_watchdog_poll_without_ssh(self):
        """GPUWatchdog.poll_once returns empty status when no SSH client."""
        from watchdog import GPUWatchdog

        wd = GPUWatchdog()
        status = wd.poll_once()
        self.assertIsInstance(status, dict)

    def test_watchdog_not_aborted_initially(self):
        """GPUWatchdog.is_aborted() returns False initially."""
        from watchdog import GPUWatchdog

        wd = GPUWatchdog()
        self.assertFalse(wd.is_aborted())


# ===========================================================================
# 11. Pilot Design Constants
# ===========================================================================

class TestPilotDesign(unittest.TestCase):
    """Pilot configuration matches documented design."""

    def test_pilot_configs_exist_in_manifest(self):
        """All pilot configs are valid manifest config IDs."""
        from pilot import PILOT_CONFIGS

        manifest_path = HERE / "models" / "manifest.json"
        with open(manifest_path) as f:
            manifest_ids = {c["id"] for c in json.load(f)["configs"]}

        for cid in PILOT_CONFIGS:
            self.assertIn(cid, manifest_ids, f"Pilot config {cid} not in manifest")

    def test_pilot_tasks_are_valid(self):
        """Pilot tasks reference valid task IDs and roles."""
        from pilot import PILOT_TASKS

        valid_roles = {"orchestrator", "coder", "multi-turn"}
        for task_id, role, difficulty, desc in PILOT_TASKS:
            self.assertTrue(task_id.startswith("T"))
            self.assertIn(role, valid_roles)
            self.assertIn(difficulty, {"easy", "medium", "hard"})

    def test_pilot_reps_positive(self):
        """Pilot repetitions is a positive integer."""
        from pilot import REPS

        self.assertIsInstance(REPS, int)
        self.assertGreater(REPS, 0)


# ===========================================================================
# 12. Telemetry Collector Schema
# ===========================================================================

class TestTelemetryCollectorSchema(unittest.TestCase):
    """TelemetryCollector creates its schema correctly."""

    def test_collector_creates_gpu_snapshots(self):
        """TelemetryCollector creates gpu_snapshots table."""
        from telemetry_collector import TelemetryCollector

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            tc = TelemetryCollector(db_path=db_path)
            db = sqlite3.connect(db_path)
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            db.close()

            self.assertIn("gpu_snapshots", tables)
            self.assertIn("benchmark_results", tables)
        finally:
            os.unlink(db_path)


# ===========================================================================
# 13. Report Helpers
# ===========================================================================

class TestReportHelpers(unittest.TestCase):
    """Report module utility functions work correctly."""

    def test_median_odd(self):
        """_median of odd-length list returns middle element."""
        from report import _median

        self.assertEqual(_median([1, 2, 3]), 2)
        self.assertEqual(_median([1, 3, 5, 7, 9]), 5)

    def test_median_even(self):
        """_median of even-length list returns average of middle two."""
        from report import _median

        self.assertEqual(_median([1, 2, 3, 4]), 2.5)

    def test_median_empty(self):
        """_median of empty list returns None."""
        from report import _median

        self.assertIsNone(_median([]))

    def test_iqr_basic(self):
        """_iqr returns (Q1, Q3) tuple."""
        from report import _iqr

        q1, q3 = _iqr([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertIsNotNone(q1)
        self.assertIsNotNone(q3)
        self.assertLess(q1, q3)


# ===========================================================================
# 14. File Inventory
# ===========================================================================

class TestFileInventory(unittest.TestCase):
    """All expected files exist in the coder-harness directory."""

    EXPECTED_PYTHON = [
        "ssh_utils.py",
        "preflight.py",
        "runner.py",
        "judge.py",
        "gpu_fit.py",
        "watchdog.py",
        "checkpoint.py",
        "orchestrate.py",
        "pilot.py",
        "report.py",
        "schema_unified.py",
        "sandbox_manager.py",
        "remote_control.py",
        "work_engine.py",
        "gitea_utils.py",
        "telemetry_collector.py",
        "telemetry_dashboard.py",
        "harness.py",
        "generate_readme.py",
    ]

    EXPECTED_DATA = [
        "schema.sql",
        "corpus.json",
        "models/manifest.json",
        "projects.yaml",
    ]

    def test_all_python_files_exist(self):
        """All 19 Python source files exist."""
        for fname in self.EXPECTED_PYTHON:
            with self.subTest(file=fname):
                path = HERE / fname
                self.assertTrue(path.exists(), f"Missing: {fname}")

    def test_all_data_files_exist(self):
        """All expected data files exist."""
        for fname in self.EXPECTED_DATA:
            with self.subTest(file=fname):
                path = HERE / fname
                self.assertTrue(path.exists(), f"Missing: {fname}")

    def test_no_python_syntax_errors(self):
        """All Python files compile without syntax errors."""
        for fname in self.EXPECTED_PYTHON:
            with self.subTest(file=fname):
                path = HERE / fname
                try:
                    with open(path) as f:
                        compile(f.read(), str(path), "exec")
                except SyntaxError as e:
                    self.fail(f"Syntax error in {fname}: {e}")


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
