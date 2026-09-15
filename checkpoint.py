"""Checkpoint manager for crash recovery during benchmark runs."""

import json
import sqlite3
from datetime import datetime


class CheckpointManager:
    """Saves benchmark progress every N runs to SQLite for crash recovery."""

    def __init__(self, db_path, interval=5):
        """
        Args:
            db_path: path to SQLite database (or ':memory:' for testing)
            interval: save checkpoint every N runs (default 5)
        """
        self.db_path = db_path
        self.interval = interval
        self.runs_since_checkpoint = 0
        # Keep a persistent connection (important for :memory: databases)
        self._conn = sqlite3.connect(db_path)
        self._ensure_tables()

    def _ensure_tables(self):
        """Create checkpoints table if it doesn't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                saved_at TEXT NOT NULL,
                completed_run_ids TEXT NOT NULL,
                config_progress TEXT NOT NULL,
                metadata TEXT
            )
        """)
        self._conn.commit()

    def mark_run_complete(self, run_id):
        """Mark a run as complete. Auto-checkpoints at interval.

        Args:
            run_id: the benchmark_runs.id to mark complete
        """
        self._conn.execute(
            "UPDATE benchmark_runs SET status='complete', completed_at=? WHERE id=?",
            (datetime.now().isoformat(), run_id)
        )
        self._conn.commit()

        self.runs_since_checkpoint += 1
        if self.runs_since_checkpoint >= self.interval:
            self.save_checkpoint()

    def save_checkpoint(self):
        """Save current progress to checkpoint table.

        Captures:
        - All run IDs with status='complete'
        - Per-config progress (last task index seen)
        - Session metadata (timestamp, run count)
        """
        # Gather completed run IDs (handle missing table gracefully)
        try:
            cursor = self._conn.execute(
                "SELECT id, model_config_id FROM benchmark_runs WHERE status='complete'"
            )
            completed_rows = cursor.fetchall()
        except sqlite3.OperationalError:
            completed_rows = []
        completed_run_ids = [row[0] for row in completed_rows]

        # Build config progress: {config_id: completed_task_count}
        config_progress = {}
        for run_id, config_id in completed_rows:
            if config_id not in config_progress:
                config_progress[config_id] = 0
            config_progress[config_id] += 1

        metadata = {
            "total_completed": len(completed_run_ids),
            "saved_at": datetime.now().isoformat(),
        }

        self._conn.execute(
            "INSERT INTO checkpoints (saved_at, completed_run_ids, config_progress, metadata) "
            "VALUES (?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                json.dumps(completed_run_ids),
                json.dumps(config_progress),
                json.dumps(metadata),
            )
        )
        self._conn.commit()
        self.runs_since_checkpoint = 0

    def load_checkpoint(self):
        """Load the most recent checkpoint. Returns dict or None."""
        cursor = self._conn.execute(
            "SELECT completed_run_ids, config_progress, metadata, saved_at "
            "FROM checkpoints ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None

        completed_run_ids = json.loads(row[0])
        config_progress = json.loads(row[1])
        metadata = json.loads(row[2]) if row[2] else {}
        saved_at = row[3]

        return {
            "completed_run_ids": completed_run_ids,
            "config_progress": config_progress,
            "metadata": metadata,
            "saved_at": saved_at,
        }

    def get_incomplete_runs(self):
        """Get list of run IDs with status='running' (interrupted)."""
        try:
            cursor = self._conn.execute(
                "SELECT id FROM benchmark_runs WHERE status='running'"
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            return []

    def resume_from_checkpoint(self):
        """Resume benchmark from last checkpoint.

        Returns:
            dict with keys: completed_run_ids, config_progress, should_resume
        """
        checkpoint = self.load_checkpoint()
        if checkpoint is None:
            return {
                "completed_run_ids": [],
                "config_progress": {},
                "should_resume": False,
            }

        # Mark any 'running' status runs as 'incomplete'
        incomplete = self.get_incomplete_runs()
        if incomplete:
            for run_id in incomplete:
                self._conn.execute(
                    "UPDATE benchmark_runs SET status='incomplete' WHERE id=?",
                    (run_id,)
                )
            self._conn.commit()

        return {
            "completed_run_ids": checkpoint.get("completed_run_ids", []),
            "config_progress": checkpoint.get("config_progress", {}),
            "should_resume": True,
        }

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
