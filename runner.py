#!/usr/bin/env python3
"""Core benchmark runner — sends tasks to beellama and logs results to SQLite."""

import json
import os
import sys
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'benchmark-results.db')
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema.sql')
CORPUS_PATH = os.path.join(os.path.dirname(__file__), 'corpus.json')
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), 'models', 'manifest.json')

# Sampling defaults (from CONFIG-INDEX.md)
DEFAULT_SAMPLING = {
    "temperature": 0.3,
    "top_p": 0.95,
    "top_k": 40,
    "min_p": 0.05,
    "repeat_penalty": 1.1,
    "max_tokens": 2048,
}


class BenchmarkRunner:
    """Runs benchmark tasks against beellama endpoints and logs results."""

    def __init__(self, db_path=None, ssh_client=None, corpus_path=None):
        self.db_path = db_path or DB_PATH
        self.ssh = ssh_client
        self.corpus = self._load_corpus(corpus_path or CORPUS_PATH)
        self.manifest = self._load_manifest()
        self._init_db()
        self._sync_manifest_to_db()
        self._sync_corpus_to_db()

    # ------------------------------------------------------------------
    # Loading helpers
    # ------------------------------------------------------------------

    def _load_corpus(self, path):
        with open(path) as f:
            tasks = json.load(f)
        return {t["id"]: t for t in tasks}

    def _load_manifest(self):
        with open(MANIFEST_PATH) as f:
            configs = json.load(f)["configs"]
        return {c["id"]: c for c in configs}

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _init_db(self):
        """Initialize database schema (idempotent via IF NOT EXISTS)."""
        conn = sqlite3.connect(self.db_path)
        # Disable FK checks for schema init so model_configs/tasks don't
        # need to exist before benchmark_runs is created.
        conn.execute("PRAGMA foreign_keys = OFF")
        with open(SCHEMA_PATH) as f:
            schema_sql = f.read()
        # Guard the schema_version INSERT so it doesn't duplicate on re-init:
        # add a UNIQUE constraint and use INSERT OR IGNORE
        schema_sql = schema_sql.replace(
            "CREATE TABLE IF NOT EXISTS schema_version (\n"
            "  version    INTEGER NOT NULL DEFAULT 1,\n"
            "  updated_at TEXT    NOT NULL DEFAULT (datetime('now'))\n"
            ");",
            "CREATE TABLE IF NOT EXISTS schema_version (\n"
            "  version    INTEGER NOT NULL DEFAULT 1,\n"
            "  updated_at TEXT    NOT NULL DEFAULT (datetime('now')),\n"
            "  UNIQUE(version)\n"
            ");",
        )
        schema_sql = schema_sql.replace(
            "INSERT INTO schema_version (version) VALUES (1);",
            "INSERT OR IGNORE INTO schema_version (version) VALUES (1);",
        )
        conn.executescript(schema_sql)
        conn.close()

    def _get_connection(self):
        """Open a connection with FK enforcement enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _sync_manifest_to_db(self):
        """Upsert model configs from manifest.json into model_configs table."""
        conn = self._get_connection()
        for cfg_id, cfg in self.manifest.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO model_configs
                    (id, gpu, model_name, model_path, port, context_size,
                     thinking_enabled, kvarn_level, speculative_type, draft_model, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cfg_id,
                    cfg.get("gpu", ""),
                    cfg.get("model_name", ""),
                    cfg.get("model_path", ""),
                    cfg.get("port", 8080),
                    cfg.get("context_size", 4096),
                    1 if cfg.get("thinking_enabled") else 0,
                    cfg.get("kvarn_level"),
                    cfg.get("speculative_type", "none"),
                    cfg.get("draft_model"),
                    cfg.get("notes", ""),
                ),
            )
        conn.commit()
        conn.close()

    def _sync_corpus_to_db(self):
        """Upsert task definitions from corpus.json into tasks table."""
        conn = self._get_connection()
        for task_id, task in self.corpus.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                    (id, role, category, difficulty, prompt,
                     expected_behavior, scoring_criteria,
                     automated_check, test_cases, turns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task.get("role", "orchestrator"),
                    task.get("category", "unknown"),
                    task.get("difficulty"),
                    task.get("prompt", ""),
                    task.get("expected_behavior", ""),
                    task.get("scoring_criteria", ""),
                    task.get("automated_check"),
                    json.dumps(task.get("test_cases", [])),
                    task.get("turns", 1),
                ),
            )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_multi_turn_prompt(self, task, turn_index):
        """Build prompt for a multi-turn task with conversation history."""
        turns = task.get("turns_descriptions", [])
        if not turns:
            return task["prompt"]

        history = []
        for i in range(min(turn_index, len(turns))):
            history.append(f"Turn {i + 1}: {turns[i]}")

        if turn_index < len(turns):
            current = turns[turn_index]
        else:
            current = task["prompt"]

        if history:
            return f"Previous conversation:\n{chr(10).join(history)}\n\nNow: {current}"
        return current

    # ------------------------------------------------------------------
    # Single run
    # ------------------------------------------------------------------

    def run_single(self, model_config_id, task_id, turn_index=0, repetition=1,
                   dry_run=False, sampling_params=None):
        """Run a single task against a model config.

        Args:
            model_config_id: config ID from manifest (e.g. '3090-qwen35-9b')
            task_id: task ID from corpus (e.g. 'T01')
            turn_index: which turn for multi-turn tasks (0-based)
            repetition: which repetition (1-based)
            dry_run: if True, return plan dict without executing
            sampling_params: override default sampling params

        Returns:
            run_id (int), plan dict if dry_run, or None on error
        """
        # 1. Look up config
        config = self.manifest.get(model_config_id)
        if config is None:
            raise ValueError(f"Config {model_config_id} not found")

        # 2. Look up task
        task = self.corpus.get(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

        # 3. Build prompt
        if task.get("role") == "multi-turn" and task.get("turns", 1) > 1:
            prompt = self._build_multi_turn_prompt(task, turn_index)
        else:
            prompt = task["prompt"]

        messages = [{"role": "user", "content": prompt}]

        # 4. Sampling params
        params = {**DEFAULT_SAMPLING, **(sampling_params or {})}

        if dry_run:
            preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
            return {
                "config": model_config_id,
                "task": task_id,
                "turn": turn_index,
                "repetition": repetition,
                "port": config["port"],
                "prompt_preview": preview,
                "params": params,
                "status": "would_execute",
            }

        # 5. Insert 'running' status
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO benchmark_runs
                    (model_config_id, task_id, turn_index, repetition, status,
                     sampling_params, session_id)
                VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    model_config_id,
                    task_id,
                    turn_index,
                    repetition,
                    json.dumps(params),
                    f"bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                ),
            )
            run_id = cursor.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            print(
                f"  SKIP {task_id} rep={repetition} turn={turn_index} "
                f"(already run for {model_config_id})"
            )
            conn.close()
            return None

        # 6. Execute inference
        try:
            result = self.ssh.curl_beellama(
                port=config["port"],
                messages=messages,
                max_tokens=params.get("max_tokens", 2048),
                temperature=params.get("temperature", 0.3),
                top_p=params.get("top_p", 0.95),
                top_k=params.get("top_k", 40),
            )

            # 7. Parse response
            content = result.get("content", "")
            reasoning = result.get("reasoning_content", "")
            thinking_tokens = result.get("thinking_tokens", 0)

            response_text = content
            if reasoning:
                response_text += f"\n\n[REASONING]: {reasoning}"

            # 8. Update DB with results
            conn.execute(
                """
                UPDATE benchmark_runs SET
                    status = 'complete',
                    completed_at = datetime('now'),
                    response_text = ?,
                    predicted_per_second = ?,
                    prompt_per_second = ?,
                    predicted_ms = ?,
                    prompt_ms = ?,
                    predicted_n = ?,
                    thinking_tokens = ?,
                    total_tokens = ?
                WHERE id = ?
                """,
                (
                    response_text,
                    result.get("predicted_per_second"),
                    result.get("prompt_per_second"),
                    result.get("predicted_ms"),
                    result.get("prompt_ms"),
                    result.get("predicted_n"),
                    thinking_tokens,
                    result.get("total_tokens"),
                    run_id,
                ),
            )
            conn.commit()

        except Exception as e:
            conn.execute(
                """
                UPDATE benchmark_runs SET
                    status = 'failed',
                    completed_at = datetime('now'),
                    error_message = ?
                WHERE id = ?
                """,
                (str(e), run_id),
            )
            conn.commit()
            run_id = None
        finally:
            conn.close()

        return run_id

    # ------------------------------------------------------------------
    # Batch / matrix runs
    # ------------------------------------------------------------------

    def run_batch(self, model_config_id, task_ids=None, dry_run=False):
        """Run all tasks (or subset) for a model config.

        Args:
            model_config_id: config ID
            task_ids: list of task IDs (None = all tasks)
            dry_run: if True, return plans without executing

        Returns:
            list of run_ids (or dry-run dicts)
        """
        tasks_to_run = task_ids or sorted(self.corpus.keys())
        results = []

        for task_id in tasks_to_run:
            task = self.corpus.get(task_id)
            if task is None:
                print(f"  WARNING: Task {task_id} not found, skipping")
                continue

            reps = 3  # Default: 3 repetitions per task

            for rep in range(1, reps + 1):
                turns = task.get("turns", 1)
                for turn in range(turns):
                    result = self.run_single(
                        model_config_id, task_id,
                        turn_index=turn, repetition=rep,
                        dry_run=dry_run,
                    )
                    results.append(result)

                    if not dry_run:
                        status = "✅" if result else "❌"
                        print(f"  {status} {task_id} rep={rep} turn={turn}")

        return results

    def run_config_matrix(self, config_ids=None, task_ids=None, dry_run=False):
        """Run all configs × all tasks.

        Returns:
            dict: {config_id: [run_ids]}
        """
        configs = config_ids or sorted(self.manifest.keys())
        all_results = {}

        for config_id in configs:
            print(f"\n{'=' * 60}")
            print(f"Running config: {config_id}")
            print(f"{'=' * 60}")
            results = self.run_batch(config_id, task_ids, dry_run=dry_run)
            all_results[config_id] = results

        return all_results

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_completed_runs(self, model_config_id=None, task_id=None):
        """Get list of completed run IDs."""
        conn = self._get_connection()
        query = "SELECT id FROM benchmark_runs WHERE status='complete'"
        params = []
        if model_config_id:
            query += " AND model_config_id=?"
            params.append(model_config_id)
        if task_id:
            query += " AND task_id=?"
            params.append(task_id)

        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [r["id"] for r in rows]

    def should_skip(self, model_config_id, task_id, turn_index=0, repetition=1):
        """Check if this run already exists (for --resume)."""
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT id FROM benchmark_runs
            WHERE model_config_id=? AND task_id=? AND turn_index=? AND repetition=?
            AND status IN ('complete', 'running')
            """,
            (model_config_id, task_id, turn_index, repetition),
        ).fetchone()
        conn.close()
        return row is not None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_summary(self):
        """Print summary of all runs in the database."""
        conn = self._get_connection()

        total = conn.execute(
            "SELECT COUNT(*) as c FROM benchmark_runs"
        ).fetchone()["c"]
        complete = conn.execute(
            "SELECT COUNT(*) as c FROM benchmark_runs WHERE status='complete'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM benchmark_runs WHERE status='failed'"
        ).fetchone()["c"]
        running = conn.execute(
            "SELECT COUNT(*) as c FROM benchmark_runs WHERE status='running'"
        ).fetchone()["c"]

        print(f"\nBenchmark Summary:")
        print(f"  Total runs: {total}")
        print(f"  Complete:   {complete}")
        print(f"  Failed:     {failed}")
        print(f"  Running:    {running}")

        rows = conn.execute(
            """
            SELECT model_config_id, status, COUNT(*) as c
            FROM benchmark_runs GROUP BY model_config_id, status
            """
        ).fetchall()

        if rows:
            print(f"\nPer-config breakdown:")
            configs = {}
            for r in rows:
                cfg = r["model_config_id"]
                if cfg not in configs:
                    configs[cfg] = {}
                configs[cfg][r["status"]] = r["c"]

            for cfg, statuses in configs.items():
                c = statuses.get("complete", 0)
                f = statuses.get("failed", 0)
                print(f"  {cfg}: {c} complete, {f} failed")

        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Benchmark Runner")
    parser.add_argument("--config", type=str, help="Config ID to test")
    parser.add_argument("--task", type=str, help="Task ID to test")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print plan without executing"
    )
    parser.add_argument(
        "--resume", action="store_true", help="Skip completed runs"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print run summary"
    )
    args = parser.parse_args()

    from ssh_utils import SSHClient

    ssh = SSHClient()
    ssh.connect()

    runner = BenchmarkRunner(ssh_client=ssh)

    if args.summary:
        runner.print_summary()
    elif args.config:
        if args.task:
            result = runner.run_single(args.config, args.task, dry_run=args.dry_run)
            print(f"Result: {result}")
        else:
            results = runner.run_batch(args.config, dry_run=args.dry_run)
            print(f"\nCompleted {len(results)} runs")
    else:
        results = runner.run_config_matrix(dry_run=args.dry_run)
        total = sum(len(v) for v in results.values())
        print(f"\nTotal: {total} runs planned/executed")
