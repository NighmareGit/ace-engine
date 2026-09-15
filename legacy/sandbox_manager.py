#!/usr/bin/env python3
"""
sandbox_manager.py — Manage Docker-based coding sandboxes on Triton (<LAN_IP>).

Each sandbox is an ephemeral Docker container on Triton, accessed via SSH from
nightmare. Project code is volume-mounted read-only; results are collected back
to nightmare via docker cp.

Usage:
    python3 sandbox_manager.py create  --project coder-harness --branch main --name test-01
    python3 sandbox_manager.py list
    python3 sandbox_manager.py exec    --name test-01 --command "ls -la"
    python3 sandbox_manager.py collect --name test-01 --from /workspace/results --to ./results/
    python3 sandbox_manager.py destroy --name test-01
    python3 sandbox_manager.py destroy-all
    python3 sandbox_manager.py report

Flags:
    --dry-run   Print commands without executing them.
    --host      Triton host address (default: <LAN_IP>).
    --user      SSH username on Triton (default: <user>).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from schema_unified import DB_PATH, ensure_schema

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(format=LOG_FMT, level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("sandbox_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SANDBOX_PREFIX = "harness-"
DEFAULT_HOST = "<LAN_IP>"
DEFAULT_USER = "<user>"
DEFAULT_PROJECT_PATH = "/home/<user>/projects"
DEFAULT_MODELS_PATH = "/home/<user>/models"
CONTAINER_IMAGE = "ubuntu:24.04"


# ===========================================================================
# SSH helper
# ===========================================================================

def ssh_run(
    cmd: str,
    *,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    timeout: int = 30,
    dry_run: bool = False,
) -> tuple[str, int]:
    """Execute *cmd* on *host* via SSH.  Returns (stdout, returncode)."""
    full_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        f"{user}@{host}",
        cmd,
    ]
    if dry_run:
        log.info("[dry-run] %s", " ".join(full_cmd))
        return "", 0
    log.debug("ssh: %s", cmd)
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("ssh returned %d: %s", result.returncode, result.stderr.strip())
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        log.error("SSH command timed out after %ds: %s", timeout, cmd)
        return "", -1
    except FileNotFoundError:
        log.error("'ssh' binary not found.  Is OpenSSH installed?")
        return "", -1


# ===========================================================================
# Telemetry DB
# ===========================================================================

class TelemetryDB:
    """Thin wrapper around the local SQLite telemetry database."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_schema()

    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_schema(self) -> None:
        ensure_schema(self.db_path)

    # -----------------------------------------------------------------------

    def insert_session(
        self,
        sandbox_name: str,
        project: str,
        branch: str | None = None,
        config_id: str | None = None,
    ) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO work_sessions (sandbox_name, project, branch, config_id, status)
                   VALUES (?, ?, ?, ?, 'created')""",
                (sandbox_name, project, branch, config_id),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        finally:
            conn.close()

    def update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [session_id]
        conn = self._connect()
        try:
            conn.execute(f"UPDATE work_sessions SET {sets} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()

    def record_event(
        self,
        session_id: int,
        event_type: str,
        data: Any | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO work_events (session_id, event_type, event_data) VALUES (?, ?, ?)",
                (session_id, event_type, json.dumps(data) if data is not None else None),
            )
            conn.commit()
        finally:
            conn.close()

    def get_sessions(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM work_sessions ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_events(self, session_id: int) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM work_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def summary(self) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN status='created' THEN 1 ELSE 0 END) AS created,
                       SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='destroyed' THEN 1 ELSE 0 END) AS destroyed,
                       COALESCE(SUM(inference_tokens), 0) AS total_tokens,
                       COALESCE(SUM(inference_seconds), 0.0) AS total_seconds,
                       COALESCE(SUM(git_commits), 0) AS total_commits
                   FROM work_sessions"""
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


# ===========================================================================
# SandboxManager
# ===========================================================================

class SandboxManager:
    """Manage Docker-based coding sandboxes on Triton via SSH."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        user: str = DEFAULT_USER,
        dry_run: bool = False,
    ):
        self.host = host
        self.user = user
        self.dry_run = dry_run
        self.db = TelemetryDB()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ssh(self, cmd: str, timeout: int = 30) -> tuple[str, int]:
        return ssh_run(cmd, host=self.host, user=self.user, timeout=timeout, dry_run=self.dry_run)

    @staticmethod
    def _container_name(name: str) -> str:
        """Return the full Docker container name with harness prefix."""
        if name.startswith(SANDBOX_PREFIX):
            return name
        return f"{SANDBOX_PREFIX}{name}"

    @staticmethod
    def _bare_name(container_name: str) -> str:
        """Strip the harness prefix to get the logical sandbox name."""
        if container_name.startswith(SANDBOX_PREFIX):
            return container_name[len(SANDBOX_PREFIX):]
        return container_name

    def _json_ssh(self, cmd: str, timeout: int = 30) -> dict:
        """Run an SSH command and parse its stdout as JSON."""
        out, rc = self._ssh(cmd, timeout=timeout)
        if rc != 0:
            return {"error": out or "command failed", "exit_code": rc}
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"raw": out}

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def create(
        self,
        project: str,
        branch: str = "main",
        name: str | None = None,
        config_id: str | None = None,
    ) -> dict:
        """Create a Docker sandbox on Triton.  Returns status dict."""
        if name is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"{ts}"
        cname = self._container_name(name)
        project_path = f"{DEFAULT_PROJECT_PATH}/{project}"
        models_path = DEFAULT_MODELS_PATH

        # Record session in telemetry
        session_id = self.db.insert_session(
            sandbox_name=name,
            project=project,
            branch=branch,
            config_id=config_id,
        )

        # Ensure host directory exists and pull image
        self._ssh(f"mkdir -p {project_path} {models_path}", timeout=10)
        self._ssh(f"docker pull {CONTAINER_IMAGE}", timeout=120)

        # Create the container — volume-mount project read-only
        create_cmd = (
            f"docker run -d "
            f"--name {cname} "
            f"-v {project_path}:/workspace:ro "
            f"-v {models_path}:/models:ro "
            f"{CONTAINER_IMAGE} "
            f"sleep infinity"
        )
        out, rc = self._ssh(create_cmd, timeout=30)

        if rc != 0:
            self.db.update_session(session_id, status="failed", error_message=out)
            self.db.record_event(session_id, "error", {"message": out, "phase": "create"})
            return {"name": name, "status": "failed", "error": out, "session_id": session_id}

        container_id = out
        self.db.update_session(session_id, status="created")
        self.db.record_event(session_id, "sandbox_created", {
            "container_id": container_id,
            "project": project,
            "branch": branch,
        })

        # Install deps inside container in background — non-blocking
        install_cmd = (
            f"docker exec {cname} bash -c "
            f"'apt-get update -qq && apt-get install -y -qq python3 python3-pip git curl jq >/dev/null 2>&1'"
        )
        self._ssh(install_cmd, timeout=120)
        # Create /workspace symlink to project files if not already there
        self._ssh(f"docker exec {cname} bash -c 'test -d /workspace || ln -s /workspace /workspace'", timeout=5)

        log.info("Sandbox '%s' created (container %s, session %d)", name, container_id[:12], session_id)
        return {
            "name": name,
            "container_id": container_id,
            "status": "created",
            "session_id": session_id,
        }

    # ------------------------------------------------------------------

    def list_sandboxes(self) -> list[dict]:
        """List active harness sandboxes on Triton."""
        cmd = (
            f"docker ps -a "
            f"--filter name={SANDBOX_PREFIX} "
            f"--format '{{{{.ID}}}}\t{{{{.Names}}}}\t{{{{.Status}}}}\t{{{{.Image}}}}\t{{{{.CreatedAt}}}}'"
        )
        out, rc = self._ssh(cmd)
        if rc != 0 or not out:
            return []

        sandboxes = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            cid, name, status, image, created = parts[:5]
            sandboxes.append({
                "container_id": cid,
                "name": self._bare_name(name),
                "full_name": name,
                "status": status,
                "image": image,
                "created": created,
            })
        return sandboxes

    # ------------------------------------------------------------------

    def exec(self, name: str, command: str, timeout: int = 60) -> dict:
        """Execute a command in a sandbox. Returns {stdout, stderr, exit_code}."""
        cname = self._container_name(name)
        # Escape single quotes in command for the outer bash -c wrapper
        escaped = command.replace("'", "'\\''")
        remote_cmd = f"docker exec {cname} bash -c '{escaped}'"

        result = subprocess.run(
            [
                "ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                f"{self.user}@{self.host}",
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }

    # ------------------------------------------------------------------

    def collect(
        self,
        name: str,
        remote_path: str,
        local_path: str,
    ) -> dict:
        """Copy files from sandbox to the local machine via Triton -> nightmare."""
        cname = self._container_name(name)
        local_dir = os.path.dirname(local_path) or "."
        os.makedirs(local_dir, exist_ok=True)

        # First, copy from container to a staging path on Triton
        staging = f"/tmp/{cname}-collect-{os.getpid()}"
        cp_cmd = f"docker cp {cname}:{remote_path} {staging}"
        _, rc = self._ssh(cp_cmd, timeout=60)
        if rc != 0:
            return {"status": "error", "error": "docker cp to staging failed", "exit_code": rc}

        # Then scp from Triton to nightmare
        scp_cmd = [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-r",
            f"{self.user}@{self.host}:{staging}",
            local_path,
        ]
        if self.dry_run:
            log.info("[dry-run] %s", " ".join(scp_cmd))
            return {"status": "dry-run", "local_path": local_path}

        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"status": "error", "error": result.stderr, "exit_code": result.returncode}

        # Cleanup staging on Triton
        self._ssh(f"rm -rf {staging}", timeout=10)

        log.info("Collected %s -> %s", remote_path, local_path)
        return {"status": "ok", "local_path": local_path, "exit_code": 0}

    # ------------------------------------------------------------------

    def destroy(self, name: str) -> dict:
        """Destroy a sandbox."""
        cname = self._container_name(name)
        cmd = f"docker rm -f {cname}"
        _, rc = self._ssh(cmd, timeout=15)

        # Update telemetry
        sessions = self.db.get_sessions()
        for s in sessions:
            if s["sandbox_name"] == name or s["sandbox_name"] == cname:
                self.db.update_session(
                    s["id"],
                    status="destroyed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                self.db.record_event(s["id"], "sandbox_destroyed", {"name": name})
                break

        log.info("Destroyed sandbox '%s' (rc=%d)", name, rc)
        return {"name": name, "status": "destroyed", "exit_code": rc}

    # ------------------------------------------------------------------

    def destroy_all(self) -> dict:
        """Destroy all harness sandboxes."""
        sandboxes = self.list_sandboxes()
        results = []
        for sb in sandboxes:
            r = self.destroy(sb["name"])
            results.append(r)
        return {"destroyed": len(results), "details": results}

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def record_event(self, session_id: int, event_type: str, data: Any | None = None) -> None:
        """Record a telemetry event."""
        self.db.record_event(session_id, event_type, data)

    def generate_report(self) -> dict:
        """Generate a summary report of all sessions."""
        summary = self.db.summary()
        sessions = self.db.get_sessions()
        return {
            "summary": summary,
            "sessions": sessions,
        }


# ===========================================================================
# CLI
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sandbox_manager",
        description="Manage Docker-based coding sandboxes on Triton (<LAN_IP>).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s create --project coder-harness --branch main --name test-01
              %(prog)s list
              %(prog)s exec --name test-01 --command "python3 --version"
              %(prog)s collect --name test-01 --from /workspace/results --to ./results/
              %(prog)s destroy --name test-01
              %(prog)s destroy-all
              %(prog)s report
        """),
    )
    p.add_argument("--host", default=DEFAULT_HOST, help="Triton host (default: %(default)s)")
    p.add_argument("--user", default=DEFAULT_USER, help="SSH user on Triton (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true", help="Print commands without executing.")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")

    sub = p.add_subparsers(dest="command", help="Available commands")

    # create
    sp = sub.add_parser("create", help="Create a new sandbox.")
    sp.add_argument("--project", required=True, help="Project name (directory under /home/<user>/projects).")
    sp.add_argument("--branch", default="main", help="Git branch (default: main).")
    sp.add_argument("--name", default=None, help="Sandbox name (auto-generated if omitted).")
    sp.add_argument("--config-id", default=None, help="Optional config identifier for telemetry.")

    # list
    sub.add_parser("list", help="List active harness sandboxes.")

    # exec
    sp = sub.add_parser("exec", help="Execute a command in a sandbox.")
    sp.add_argument("--name", required=True, help="Sandbox name.")
    sp.add_argument("--command", "-c", required=True, help="Command to execute.")
    sp.add_argument("--timeout", type=int, default=60, help="Command timeout in seconds (default: 60).")

    # collect
    sp = sub.add_parser("collect", help="Copy files from a sandbox to local machine.")
    sp.add_argument("--name", required=True, help="Sandbox name.")
    sp.add_argument("--from", dest="remote_path", required=True, help="Remote path inside the sandbox.")
    sp.add_argument("--to", dest="local_path", required=True, help="Local destination path.")

    # destroy
    sp = sub.add_parser("destroy", help="Destroy a sandbox.")
    sp.add_argument("--name", required=True, help="Sandbox name.")

    # destroy-all
    sub.add_parser("destroy-all", help="Destroy all harness sandboxes.")

    # report
    sub.add_parser("report", help="Generate a telemetry summary report.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return 0

    mgr = SandboxManager(host=args.host, user=args.user, dry_run=args.dry_run)

    if args.command == "create":
        result = mgr.create(
            project=args.project,
            branch=args.branch,
            name=args.name,
            config_id=args.config_id,
        )
    elif args.command == "list":
        result = mgr.list_sandboxes()
    elif args.command == "exec":
        result = mgr.exec(args.name, args.command, timeout=args.timeout)
    elif args.command == "collect":
        result = mgr.collect(args.name, args.remote_path, args.local_path)
    elif args.command == "destroy":
        result = mgr.destroy(args.name)
    elif args.command == "destroy-all":
        result = mgr.destroy_all()
    elif args.command == "report":
        result = mgr.generate_report()
    else:
        parser.print_help()
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
