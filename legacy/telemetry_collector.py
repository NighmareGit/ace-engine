#!/usr/bin/env python3
"""
telemetry_collector.py — Active telemetry collection for coder-harness.

Monitors the telemetry SQLite database for new events, collects GPU stats
from Triton via SSH, records benchmark results, tracks sandbox lifecycle
events, and generates periodic summary reports.

Usage:
    python3 telemetry_collector.py start --interval 60
    python3 telemetry_collector.py collect
    python3 telemetry_collector.py events --limit 20
    python3 telemetry_collector.py summary
    python3 telemetry_collector.py export --output telemetry-export.json
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DB = os.path.expanduser("~/coder-harness-telemetry.db")
SSH_HOST = "<user>@<LAN_IP>"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]

# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gpu_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gpu_index INTEGER NOT NULL,
    name TEXT,
    memory_used_mb REAL,
    memory_total_mb REAL,
    temperature_c REAL,
    utilization_pct REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT NOT NULL,
    benchmark_type TEXT NOT NULL,
    tokens_per_sec REAL,
    tokens_generated INTEGER,
    elapsed_seconds REAL,
    vram_used_mb REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# TelemetryCollector
# ---------------------------------------------------------------------------

class TelemetryCollector:
    """Collect, record, and report telemetry for coder-harness."""

    def __init__(self, db_path: str = DEFAULT_DB, dry_run: bool = False):
        self.db_path = db_path
        self.dry_run = dry_run
        self._ensure_schema()

    # -- schema -------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create tables if they don't already exist."""
        conn = sqlite3.connect(self.db_path)
        # Try to create the extra tables; ignore if they already exist
        for stmt in SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # table already exists
        conn.commit()
        conn.close()

    # -- SSH helper ---------------------------------------------------------

    @staticmethod
    def ssh(cmd: str) -> tuple[str, int]:
        """Run *cmd* on the remote Triton host via SSH.

        Returns (stdout, returncode).
        """
        full_cmd = ["ssh"] + SSH_OPTS + [SSH_HOST, cmd]
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout, result.returncode
        except FileNotFoundError:
            return "ssh: command not found", -1
        except subprocess.TimeoutExpired:
            return "ssh: connection timed out", -1
        except Exception as exc:
            return f"ssh: {exc}", -1

    # -- GPU stats ----------------------------------------------------------

    def collect_gpu_stats(self) -> list[dict]:
        """Collect GPU stats from Triton via SSH."""
        cmd = (
            "nvidia-smi --query-gpu=index,name,memory.used,memory.total,"
            "temperature.gpu,utilization.gpu --format=csv,noheader"
        )
        out, rc = self.ssh(cmd)
        if rc != 0:
            return []

        gpus: list[dict] = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            try:
                gpus.append({
                    "gpu_index": int(parts[0]),
                    "name": parts[1],
                    "memory_used_mb": float(parts[2].replace(" MiB", "")),
                    "memory_total_mb": float(parts[3].replace(" MiB", "")),
                    "temperature_c": float(parts[4]),
                    "utilization_pct": float(parts[5].replace(" %", "")),
                })
            except (ValueError, IndexError):
                continue
        return gpus

    def record_gpu_snapshots(self, gpus: list[dict]) -> None:
        """Persist GPU snapshots to the database."""
        if self.dry_run:
            print(json.dumps({"action": "gpu_snapshots", "count": len(gpus), "data": gpus}, indent=2))
            return
        conn = sqlite3.connect(self.db_path)
        for gpu in gpus:
            conn.execute(
                "INSERT INTO gpu_snapshots "
                "(gpu_index, name, memory_used_mb, memory_total_mb, temperature_c, utilization_pct) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    gpu["gpu_index"],
                    gpu["name"],
                    gpu["memory_used_mb"],
                    gpu["memory_total_mb"],
                    gpu["temperature_c"],
                    gpu["utilization_pct"],
                ),
            )
        conn.commit()
        conn.close()

    # -- event recording ----------------------------------------------------

    def record_event(self, event_type: str, data: dict | None = None, session_id: str | None = None) -> None:
        """Record a telemetry event."""
        payload = json.dumps(data) if data else None
        if self.dry_run:
            print(json.dumps({
                "action": "record_event",
                "session_id": session_id,
                "event_type": event_type,
                "event_data": data,
            }, indent=2))
            return
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO work_events (session_id, event_type, event_data) VALUES (?, ?, ?)",
            (session_id, event_type, payload),
        )
        conn.commit()
        conn.close()

    # -- benchmark results --------------------------------------------------

    def record_benchmark(self, config_id: str, benchmark_type: str,
                         tokens_per_sec: float, tokens_generated: int,
                         elapsed_seconds: float, vram_used_mb: float | None = None) -> None:
        """Record a benchmark result."""
        if self.dry_run:
            print(json.dumps({
                "action": "benchmark",
                "config_id": config_id,
                "benchmark_type": benchmark_type,
                "tokens_per_sec": tokens_per_sec,
                "tokens_generated": tokens_generated,
                "elapsed_seconds": elapsed_seconds,
                "vram_used_mb": vram_used_mb,
            }, indent=2))
            return
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO benchmark_results "
            "(config_id, benchmark_type, tokens_per_sec, tokens_generated, elapsed_seconds, vram_used_mb) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (config_id, benchmark_type, tokens_per_sec, tokens_generated, elapsed_seconds, vram_used_mb),
        )
        conn.commit()
        conn.close()

    # -- one-shot collection ------------------------------------------------

    def collect(self) -> dict:
        """Run a single collection cycle: GPU stats + lightweight DB scan."""
        result: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gpu_snapshots": [],
            "events_collected": 0,
            "errors": [],
        }

        # GPU
        try:
            gpus = self.collect_gpu_stats()
            self.record_gpu_snapshots(gpus)
            result["gpu_snapshots"] = gpus
        except Exception as exc:
            result["errors"].append(f"gpu_collect: {exc}")

        # Count new events since last check (simple heuristic: last 60 s)
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM work_events "
                    "WHERE timestamp >= datetime('now', '-60 seconds')"
                ).fetchone()
                result["events_collected"] = row[0] if row else 0
            except sqlite3.OperationalError:
                result["events_collected"] = 0
            conn.close()
        except Exception as exc:
            result["errors"].append(f"event_scan: {exc}")

        return result

    # -- recent events ------------------------------------------------------

    def recent_events(self, limit: int = 20) -> list[dict]:
        """Return the most recent telemetry events."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM work_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    # -- summary ------------------------------------------------------------

    def generate_summary(self) -> dict:
        """Generate a summary of all telemetry data."""
        conn = sqlite3.connect(self.db_path)

        def _count(table: str, where: str = "") -> int:
            sql = f"SELECT COUNT(*) FROM {table}"
            if where:
                sql += f" WHERE {where}"
            try:
                return conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                return 0

        summary: dict = {
            "total_sessions": _count("work_sessions"),
            "active_sandboxes": _count("work_sessions", "status='running'"),
            "total_events": _count("work_events"),
            "gpu_snapshots": _count("gpu_snapshots"),
            "benchmark_runs": _count("benchmark_results"),
            "recent_events": [],
            "config_usage": [],
            "gpu_latest": [],
            "benchmark_latest": [],
        }

        # Recent events
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM work_events ORDER BY timestamp DESC LIMIT 10"
            ).fetchall()
            summary["recent_events"] = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            pass

        # Config usage
        try:
            rows = conn.execute(
                "SELECT config_id, COUNT(*) as runs FROM work_sessions "
                "GROUP BY config_id ORDER BY runs DESC"
            ).fetchall()
            summary["config_usage"] = [{"config": r[0], "runs": r[1]} for r in rows]
        except sqlite3.OperationalError:
            pass

        # Latest GPU snapshots (one per gpu_index)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM gpu_snapshots WHERE id IN "
                "(SELECT MAX(id) FROM gpu_snapshots GROUP BY gpu_index)"
            ).fetchall()
            summary["gpu_latest"] = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            pass

        # Latest benchmarks
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM benchmark_results ORDER BY timestamp DESC LIMIT 10"
            ).fetchall()
            summary["benchmark_latest"] = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            pass

        conn.close()
        return summary

    # -- export -------------------------------------------------------------

    def export_json(self, output_path: str) -> None:
        """Export all telemetry data to a JSON file."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        export: dict = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "db_path": self.db_path,
            "tables": {},
        }

        for table in ("work_sessions", "work_events", "gpu_snapshots", "benchmark_results"):
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                export["tables"][table] = [dict(row) for row in rows]
            except sqlite3.OperationalError:
                export["tables"][table] = []

        conn.close()

        with open(output_path, "w") as fh:
            json.dump(export, fh, indent=2)
        print(json.dumps({"status": "exported", "path": output_path, "rows": {
            k: len(v) for k, v in export["tables"].items()
        }}))

    # -- continuous collection loop -----------------------------------------

    def run_loop(self, interval: int = 60) -> None:
        """Collect telemetry every *interval* seconds until interrupted."""
        print(json.dumps({
            "status": "started",
            "interval_seconds": interval,
            "db_path": self.db_path,
            "dry_run": self.dry_run,
        }))
        try:
            while True:
                cycle = self.collect()
                print(json.dumps({"cycle": cycle}))
                time.sleep(interval)
        except KeyboardInterrupt:
            print(json.dumps({"status": "stopped"}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telemetry collector for coder-harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to telemetry SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Print actions instead of writing to DB")

    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="Start continuous collection loop")
    p_start.add_argument("--interval", type=int, default=60, help="Seconds between collections (default: 60)")

    # collect
    sub.add_parser("collect", help="Run a single collection cycle")

    # events
    p_events = sub.add_parser("events", help="Show recent telemetry events")
    p_events.add_argument("--limit", type=int, default=20, help="Number of events to show (default: 20)")

    # summary
    sub.add_parser("summary", help="Show a telemetry summary")

    # export
    p_export = sub.add_parser("export", help="Export telemetry data to JSON")
    p_export.add_argument("--output", default="telemetry-export.json", help="Output file path")

    args = parser.parse_args()
    collector = TelemetryCollector(db_path=args.db, dry_run=args.dry_run)

    if args.command == "start":
        collector.run_loop(interval=args.interval)

    elif args.command == "collect":
        result = collector.collect()
        print(json.dumps(result, indent=2))

    elif args.command == "events":
        events = collector.recent_events(limit=args.limit)
        print(json.dumps(events, indent=2))

    elif args.command == "summary":
        summary = collector.generate_summary()
        print(json.dumps(summary, indent=2))

    elif args.command == "export":
        collector.export_json(output_path=args.output)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
