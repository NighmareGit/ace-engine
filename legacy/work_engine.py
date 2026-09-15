#!/usr/bin/env python3
"""
work_engine.py — Orchestrate the full remote work pipeline.

Connects all existing tools into one brain:
  - sandbox_manager.py  — Docker sandbox lifecycle
  - remote_control.py   — model swap, benchmark, system health
  - telemetry_dashboard.py — generates dashboard from telemetry DB
  - Gitea repos on Triton (http://localhost:3000)

Pipeline:
  1. Receive task (project, branch, prompt, config)
  2. Swap model on Triton (if needed)
  3. Create Docker sandbox
  4. Clone project into sandbox (from Gitea or local SCP)
  5. Run inference task inside sandbox
  6. Collect results (code, logs, metrics)
  7. Push results back to Gitea (feature branch)
  8. Record telemetry (session, events, timing)
  9. Generate report

Usage:
    python3 work_engine.py run \\
      --project coder-harness \\
      --branch main \\
      --config 3090-qwen36-35b \\
      --task "Fix the SSH connection pattern in ssh_utils.py" \\
      --name fix-ssh-01

    python3 work_engine.py bench \\
      --project coder-harness \\
      --config 3090-qwen36-35b \\
      --tasks throughput,quality

    python3 work_engine.py sessions
    python3 work_engine.py collect --name fix-ssh-01
    python3 work_engine.py cleanup
    python3 work_engine.py status
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from schema_unified import DB_PATH as TELEMETRY_DB, ensure_schema

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(format=LOG_FMT, level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("work_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"
COMPOSE_DIR = "~/dockers/beellama-benchmark"
BEE_LLAMA_PORT_3090 = 8080
BEE_LLAMA_PORT_3070 = 8082
GITEA_URL = "http://localhost:3000"
GITEA_TOKEN = "<GITEA_TOKEN>"
SANDBOX_PREFIX = "harness-"
CONTAINER_IMAGE = "ubuntu:24.04"
DEFAULT_PROJECT_PATH = "/home/<user>/projects"

# Config ID -> Docker Compose profile (shared with remote_control.py)
CONFIG_TO_PROFILE = {
    "3090-qwen36-35b":   "config-i",
    "3090-muse-glimmer":  "config-h",
    "3090-laguna-xs":     "config-g-laguna",
    "3090-qwen35-9b":     "config-h-qwen35",
    "3070-qwen35-9b":     None,
    "3070-qwen35-4b":     None,
}

# Inference port per config
CONFIG_TO_PORT = {
    "3090-qwen36-35b":   BEE_LLAMA_PORT_3090,
    "3090-muse-glimmer":  BEE_LLAMA_PORT_3090,
    "3090-laguna-xs":     BEE_LLAMA_PORT_3090,
    "3090-qwen35-9b":     BEE_LLAMA_PORT_3090,
    "3070-qwen35-9b":     BEE_LLAMA_PORT_3070,
    "3070-qwen35-4b":     BEE_LLAMA_PORT_3070,
}


# ===========================================================================
# SSH transport (key-based, matching remote_control.py pattern)
# ===========================================================================

def ssh_run(cmd: str, host: str = TRITON_HOST, user: str = TRITON_USER,
            timeout: int = 30) -> tuple[str, int]:
    """Execute *cmd* on *host* via SSH. Returns (stdout, returncode).

    Uses key-based SSH (no sshpass).
    """
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        cmd,
    ]
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return f"SSH command timed out after {timeout}s", 1
    except Exception as exc:
        return f"SSH error: {exc}", 1


def scp_get(remote_path: str, local_path: str, host: str = TRITON_HOST,
            user: str = TRITON_USER, recursive: bool = True) -> tuple[str, int]:
    """Copy from Triton to local machine via SCP."""
    scp_cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
    ]
    if recursive:
        scp_cmd.append("-r")
    scp_cmd.extend([
        f"{user}@{host}:{remote_path}",
        local_path,
    ])
    try:
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "SCP transfer timed out", 1
    except Exception as exc:
        return f"SCP error: {exc}", 1


# ===========================================================================
# Telemetry DB
# ===========================================================================

class TelemetryDB:
    """Local SQLite telemetry store for work sessions and events."""

    def __init__(self, db_path: str = TELEMETRY_DB):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_schema(self) -> None:
        ensure_schema(self.db_path)

    def insert_session(self, sandbox_name: str, project: str,
                       branch: str | None = None,
                       config_id: str | None = None,
                       task_prompt: str | None = None,
                       session_uuid: str | None = None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO work_sessions
                   (sandbox_name, project, branch, config_id, task_prompt,
                    status, session_uuid)
                   VALUES (?, ?, ?, ?, ?, 'created', ?)""",
                (sandbox_name, project, branch, config_id, task_prompt,
                 session_uuid or str(uuid.uuid4())),
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

    def record_event(self, session_id: int, event_type: str,
                     data: Any | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO work_events (session_id, event_type, event_data) "
                "VALUES (?, ?, ?)",
                (session_id, event_type,
                 json.dumps(data) if data is not None else None),
            )
            conn.commit()
        finally:
            conn.close()

    def get_sessions(self, status: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM work_sessions WHERE status = ? ORDER BY id DESC",
                    (status,),
                ).fetchall()
            else:
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
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                       SUM(CASE WHEN status='created' THEN 1 ELSE 0 END) AS created,
                       COALESCE(SUM(inference_tokens), 0) AS total_tokens,
                       COALESCE(SUM(inference_seconds), 0.0) AS total_seconds
                   FROM work_sessions"""
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


# ===========================================================================
# WorkEngine — the brain
# ===========================================================================

class WorkEngine:
    """Orchestrate the full remote work pipeline.

    Connects: SSH → model swap → Docker sandbox → inference → Gitea push.
    """

    def __init__(self, host: str = TRITON_HOST, user: str = TRITON_USER,
                 dry_run: bool = False):
        self.host = host
        self.user = user
        self.dry_run = dry_run
        self.compose_dir = COMPOSE_DIR
        self.gitea_url = GITEA_URL
        self.gitea_token = GITEA_TOKEN
        self.db = TelemetryDB()

    # ------------------------------------------------------------------
    # SSH helpers
    # ------------------------------------------------------------------

    def _ssh(self, cmd: str, timeout: int = 30) -> tuple[str, int]:
        """Run command on Triton via SSH."""
        if self.dry_run:
            log.info("[dry-run] ssh: %s", cmd[:200])
            return "", 0
        return ssh_run(cmd, host=self.host, user=self.user, timeout=timeout)

    def _ssh_json(self, cmd: str, timeout: int = 30) -> dict:
        """Run SSH command, parse first JSON object from stdout."""
        raw, rc = self._ssh(cmd, timeout=timeout)
        if rc != 0:
            return {"error": raw, "exit_code": rc}
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            return {"raw": raw}

    def _compose(self, sub_cmd: str, timeout: int = 30) -> tuple[str, int]:
        """Run docker compose command on Triton."""
        return self._ssh(f"cd {self.compose_dir} && {sub_cmd}", timeout=timeout)

    # ------------------------------------------------------------------
    # Step 1: Model swap
    # ------------------------------------------------------------------

    def _swap_model(self, config_id: str, session_id: int) -> dict:
        """Swap BeeLlama to the requested model config.

        Returns {status, load_time_ms, profile}.
        """
        profile = CONFIG_TO_PROFILE.get(config_id)
        result = {"config_id": config_id, "profile": profile}

        if profile is None and config_id not in CONFIG_TO_PROFILE:
            result["status"] = "error"
            result["message"] = f"Unknown config: {config_id}"
            return result

        self.db.update_session(session_id, status="swapping")
        self.db.record_event(session_id, "model_swap_start",
                             {"config_id": config_id, "profile": profile})

        t0 = time.monotonic()

        if profile:
            # Try state machine first
            sm_cmd = (
                f"cd {self.compose_dir} && "
                f"python3 deploy-state-machine.py swap {profile} 2>/dev/null"
            )
            raw, rc = self._ssh(sm_cmd, timeout=300)

            if rc == 0 and "READY" in raw.upper():
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                result.update(status="ok", load_time_ms=elapsed_ms,
                              method="state_machine")
                self.db.record_event(session_id, "model_swap_done", result)
                return result

            # Fallback: direct docker compose
            self._compose(f"docker compose --profile {profile} down 2>/dev/null",
                          timeout=60)
            self._compose(f"docker compose --profile {profile} up -d",
                          timeout=60)

            # Poll health (max 180s)
            for _ in range(60):
                if self._check_health(BEE_LLAMA_PORT_3090):
                    break
                time.sleep(3)
            else:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                result.update(status="timeout", load_time_ms=elapsed_ms,
                              message="BeeLlama did not become healthy within 180s")
                self.db.record_event(session_id, "model_swap_timeout", result)
                return result
        else:
            # 3070 configs don't need a swap
            result["status"] = "no_swap_needed"
            result["message"] = f"{config_id} uses the shared 3070 service"
            self.db.record_event(session_id, "model_swap_skip", result)
            return result

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result.update(status="ok", load_time_ms=elapsed_ms,
                      method="docker_compose")
        self.db.record_event(session_id, "model_swap_done", result)
        return result

    def _check_health(self, port: int, timeout: int = 10) -> bool:
        """Check BeeLlama /health endpoint."""
        raw, rc = self._ssh(f"curl -sf http://localhost:{port}/health",
                            timeout=timeout)
        if rc != 0:
            return False
        try:
            return json.loads(raw).get("status") == "ok"
        except (json.JSONDecodeError, AttributeError):
            return False

    # ------------------------------------------------------------------
    # Step 2: Create Docker sandbox
    # ------------------------------------------------------------------

    def _create_sandbox(self, project: str, branch: str, name: str,
                        session_id: int) -> dict:
        """Create a Docker sandbox on Triton with project mounted.

        Returns {name, container_id, status}.
        """
        cname = f"{SANDBOX_PREFIX}{name}"
        project_path = f"{DEFAULT_PROJECT_PATH}/{project}"

        self.db.update_session(session_id, status="sandbox_ready")
        self.db.record_event(session_id, "sandbox_create_start",
                             {"name": cname, "project": project})

        # Ensure host directory exists and pull image
        self._ssh(f"mkdir -p {project_path}", timeout=10)
        self._ssh(f"docker pull {CONTAINER_IMAGE}", timeout=120)

        # Create container — volume-mount project read/write for results
        create_cmd = (
            f"docker run -d "
            f"--name {cname} "
            f"-v {project_path}:/workspace "
            f"{CONTAINER_IMAGE} "
            f"sleep infinity"
        )
        out, rc = self._ssh(create_cmd, timeout=30)

        if rc != 0:
            self.db.update_session(session_id, status="failed",
                                   error_message=out)
            self.db.record_event(session_id, "sandbox_create_fail",
                                 {"error": out})
            return {"name": name, "status": "failed", "error": out}

        container_id = out
        self.db.record_event(session_id, "sandbox_created",
                             {"container_id": container_id})

        # Install deps inside container
        install_cmd = (
            f"docker exec {cname} bash -c "
            f"'apt-get update -qq && "
            f"apt-get install -y -qq python3 python3-pip git curl jq "
            f">/dev/null 2>&1'"
        )
        self._ssh(install_cmd, timeout=120)

        log.info("Sandbox '%s' created (container %s, session %d)",
                 name, container_id[:12], session_id)
        return {
            "name": name,
            "container_id": container_id,
            "status": "created",
        }

    # ------------------------------------------------------------------
    # Step 3: Clone / prepare project in sandbox
    # ------------------------------------------------------------------

    def _prepare_project(self, project: str, branch: str, name: str,
                         session_id: int) -> dict:
        """Ensure project code is available in the sandbox.

        The project is already volume-mounted from Triton's ~/projects/.
        We just verify it's there and on the right branch.
        """
        cname = f"{SANDBOX_PREFIX}{name}"
        project_path = f"{DEFAULT_PROJECT_PATH}/{project}"

        # Verify project exists on Triton
        raw, rc = self._ssh(f"test -d {project_path} && echo OK", timeout=10)
        if rc != 0 or "OK" not in raw:
            return {"status": "error",
                    "error": f"Project {project} not found at {project_path}"}

        # Check branch
        git_out, git_rc = self._ssh(
            f"cd {project_path} && git branch --show-current 2>/dev/null",
            timeout=10,
        )
        current_branch = git_out.strip() if git_rc == 0 else "unknown"

        # Verify files are accessible inside container
        ls_out, ls_rc = self._ssh(
            f"docker exec {cname} ls /workspace/ | head -20",
            timeout=10,
        )

        self.db.record_event(session_id, "project_ready", {
            "project": project,
            "branch": current_branch,
            "files_visible": ls_rc == 0,
        })

        return {
            "status": "ok",
            "project": project,
            "branch": current_branch,
            "files_sample": ls_out.splitlines()[:10] if ls_out else [],
        }

    # ------------------------------------------------------------------
    # Step 4: Run inference
    # ------------------------------------------------------------------

    def _run_inference(self, prompt: str, config: str, name: str,
                       session_id: int,
                       max_tokens: int = 2048) -> dict:
        """Run inference via BeeLlama API on Triton.

        Runs curl directly on Triton (BeeLlama uses host networking).
        Returns {content, timings, usage, prompt_preview}.
        """
        port = CONFIG_TO_PORT.get(config, BEE_LLAMA_PORT_3090)
        self.db.update_session(session_id, status="inferencing")
        self.db.record_event(session_id, "inference_start", {
            "config": config, "port": port, "max_tokens": max_tokens,
            "prompt_preview": prompt[:200],
        })

        t0 = time.monotonic()

        # Escape the prompt for JSON inside shell.
        # We write a temp file to avoid shell-escaping hell.
        escaped = prompt.replace("\\", "\\\\").replace("'", "\\'")
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")

        payload = (
            f'{{"model":"q",'
            f'"messages":[{{"role":"user","content":"{escaped}"}}],'
            f'"max_tokens":{max_tokens},'
            f'"temperature":0.3,"top_p":0.95,"top_k":40}}'
        )

        # Write payload to a temp file on Triton to avoid quoting issues
        tmp_payload = f"/tmp/work-engine-payload-{os.getpid()}.json"
        # Use python to write the file safely on Triton
        write_cmd = (
            f"python3 -c \"import sys; "
            f"open('{tmp_payload}','w').write(sys.stdin.read())\" "
            f"<<'PAYLOAD_EOF'\n{payload}\nPAYLOAD_EOF"
        )
        self._ssh(write_cmd, timeout=10)

        # Run inference
        curl_cmd = (
            f"curl -s -X POST http://localhost:{port}/v1/chat/completions "
            f"-H 'Content-Type: application/json' "
            f"-d @{tmp_payload}"
        )
        raw, rc = self._ssh(curl_cmd, timeout=120)

        # Cleanup temp file
        self._ssh(f"rm -f {tmp_payload}", timeout=5)

        elapsed_s = time.monotonic() - t0

        if rc != 0:
            error_result = {
                "status": "error",
                "error": raw,
                "elapsed_seconds": round(elapsed_s, 2),
            }
            self.db.record_event(session_id, "inference_fail", error_result)
            return error_result

        # Parse response
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            error_result = {
                "status": "error",
                "error": f"Failed to parse response JSON: {raw[:500]}",
                "elapsed_seconds": round(elapsed_s, 2),
            }
            self.db.record_event(session_id, "inference_parse_fail",
                                 error_result)
            return error_result

        # Extract content
        content = ""
        reasoning = ""
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            reasoning = message.get("reasoning_content", "")

        # Extract timings
        timings = response.get("timings", {})
        usage = response.get("usage", {})

        tps = timings.get("predicted_per_second", 0.0)
        pps = timings.get("prompt_per_second", 0.0)
        predicted_ms = timings.get("predicted_ms", 0.0)
        prompt_ms = timings.get("prompt_ms", 0.0)
        predicted_n = timings.get("predicted_n", 0)
        total_tokens = usage.get("total_tokens", 0)
        thinking_tokens = timings.get("thinking_tokens", 0)
        if thinking_tokens == 0 and reasoning:
            thinking_tokens = len(reasoning) // 4

        # Record inference metrics
        self.db.update_session(
            session_id,
            inference_tokens=predicted_n,
            inference_seconds=round(predicted_ms / 1000.0, 3)
                if predicted_ms else round(elapsed_s, 3),
            inference_tokens_per_sec=tps,
        )

        result = {
            "status": "ok",
            "content": content,
            "reasoning_content": reasoning[:2000] if reasoning else "",
            "timings": {
                "predicted_per_second": tps,
                "prompt_per_second": pps,
                "predicted_ms": predicted_ms,
                "prompt_ms": prompt_ms,
                "predicted_n": predicted_n,
                "thinking_tokens": thinking_tokens,
            },
            "usage": {"total_tokens": total_tokens},
            "elapsed_seconds": round(elapsed_s, 2),
            "prompt_preview": prompt[:200],
        }

        self.db.record_event(session_id, "inference_done", {
            "tokens": predicted_n,
            "tps": tps,
            "elapsed_s": round(elapsed_s, 2),
        })
        return result

    # ------------------------------------------------------------------
    # Step 5: Collect results
    # ------------------------------------------------------------------

    def _collect_results(self, name: str, results: dict,
                         session_id: int) -> dict:
        """Write results to the project directory on Triton.

        Returns {status, path}.
        """
        cname = f"{SANDBOX_PREFIX}{name}"
        project_path = results.get("_project_path",
                                   f"{DEFAULT_PROJECT_PATH}/{name}")

        # Write results file to the project dir on Triton
        results_json = json.dumps(results, indent=2, default=str)
        # Escape for shell heredoc
        escaped_json = results_json.replace("'", "'\\''")

        results_file = f"{project_path}/results-{name}.json"
        write_cmd = (
            f"mkdir -p {project_path} && "
            f"cat > {results_file} << 'RESULTSEOF'\n"
            f"{results_json}\n"
            f"RESULTSEOF"
        )
        out, rc = self._ssh(write_cmd, timeout=15)

        self.db.record_event(session_id, "results_collected", {
            "file": results_file,
            "size_bytes": len(results_json),
        })

        if rc != 0:
            return {"status": "error", "error": out}

        return {"status": "ok", "file": results_file,
                "size_bytes": len(results_json)}

    # ------------------------------------------------------------------
    # Step 6: Push results to Gitea
    # ------------------------------------------------------------------

    def _push_results(self, project: str, branch: str, name: str,
                      session_id: int) -> dict:
        """Push results to a Gitea feature branch.

        Returns {status, branch, commit_sha}.
        """
        project_path = f"{DEFAULT_PROJECT_PATH}/{project}"
        results_branch = f"results/{name}"

        self.db.update_session(session_id, status="pushing")
        self.db.record_event(session_id, "push_start", {
            "branch": results_branch,
        })

        # Create feature branch
        self._ssh(
            f"cd {project_path} && "
            f"git checkout -b {results_branch} 2>/dev/null || "
            f"git checkout {results_branch}",
            timeout=15,
        )

        # Stage results
        self._ssh(
            f"cd {project_path} && "
            f"git add results-{name}.json",
            timeout=10,
        )

        # Commit
        commit_msg = f"results: {name} [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}]"
        commit_out, commit_rc = self._ssh(
            f"cd {project_path} && "
            f"git commit -m '{commit_msg}' 2>&1",
            timeout=15,
        )

        # Count files changed
        diff_out, _ = self._ssh(
            f"cd {project_path} && git diff --stat HEAD~1 2>/dev/null | tail -1",
            timeout=10,
        )
        files_changed = 0
        if diff_out:
            m = re.search(r"(\d+) files? changed", diff_out)
            if m:
                files_changed = int(m.group(1))

        # Get commit SHA
        sha_out, _ = self._ssh(
            f"cd {project_path} && git rev-parse --short HEAD",
            timeout=10,
        )

        # Push to Gitea remote
        push_out, push_rc = self._ssh(
            f"cd {project_path} && "
            f"git push origin {results_branch} 2>&1",
            timeout=60,
        )

        # Switch back to original branch
        self._ssh(
            f"cd {project_path} && git checkout {branch}",
            timeout=10,
        )

        self.db.update_session(
            session_id,
            git_commits=1,
            git_files_changed=files_changed,
        )

        result = {
            "status": "ok" if push_rc == 0 else "push_failed",
            "branch": results_branch,
            "commit_sha": sha_out.strip() if sha_out else None,
            "files_changed": files_changed,
            "push_output": push_out[:500] if push_out else "",
        }

        if push_rc != 0:
            result["error"] = push_out

        self.db.record_event(session_id, "push_done", result)
        return result

    # ------------------------------------------------------------------
    # Step 7: Cleanup
    # ------------------------------------------------------------------

    def _destroy_sandbox(self, name: str, session_id: int) -> dict:
        """Destroy the Docker sandbox container."""
        cname = f"{SANDBOX_PREFIX}{name}"
        out, rc = self._ssh(f"docker rm -f {cname}", timeout=15)

        self.db.update_session(session_id, status="destroyed",
                               completed_at=datetime.now(timezone.utc).isoformat())
        self.db.record_event(session_id, "sandbox_destroyed", {"name": name})

        return {"name": name, "status": "destroyed", "exit_code": rc}

    # ------------------------------------------------------------------
    # Public API: run
    # ------------------------------------------------------------------

    def run(self, project: str, branch: str = "main",
            config: str = "3090-qwen36-35b",
            task: str = "", name: str | None = None,
            max_tokens: int = 2048) -> dict:
        """Execute the full pipeline: swap → sandbox → infer → collect → push.

        Args:
            project:  Project name (under ~/projects/ on Triton)
            branch:   Git branch to work from
            config:   Model config ID (e.g. '3090-qwen36-35b')
            task:     The prompt / task description for inference
            name:     Session name (auto-generated if omitted)
            max_tokens: Max tokens for inference

        Returns:
            dict with full pipeline results
        """
        if not task:
            return {"status": "error", "message": "Task prompt is required"}

        if name is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"work-{ts}"

        project_path = f"{DEFAULT_PROJECT_PATH}/{project}"
        pipeline_start = time.monotonic()
        results: dict[str, Any] = {
            "name": name,
            "project": project,
            "branch": branch,
            "config": config,
            "task_preview": task[:300],
            "steps": {},
        }

        # Record session
        session_id = self.db.insert_session(
            sandbox_name=name,
            project=project,
            branch=branch,
            config_id=config,
            task_prompt=task,
        )
        results["session_id"] = session_id

        try:
            # Step 1: Swap model
            log.info("Step 1/7: Swapping model to %s", config)
            swap_result = self._swap_model(config, session_id)
            results["steps"]["swap"] = swap_result

            if swap_result.get("status") not in ("ok", "no_swap_needed"):
                results["status"] = "failed"
                results["error"] = f"Model swap failed: {swap_result}"
                self.db.update_session(session_id, status="failed",
                                       error_message=results["error"])
                self.db.record_event(session_id, "pipeline_abort",
                                     {"reason": "model_swap_failed"})
                return results

            # Step 2: Create sandbox
            log.info("Step 2/7: Creating sandbox '%s'", name)
            sandbox_result = self._create_sandbox(project, branch, name,
                                                  session_id)
            results["steps"]["sandbox"] = sandbox_result

            if sandbox_result.get("status") != "created":
                results["status"] = "failed"
                results["error"] = f"Sandbox creation failed: {sandbox_result}"
                self.db.update_session(session_id, status="failed",
                                       error_message=results["error"])
                self.db.record_event(session_id, "pipeline_abort",
                                     {"reason": "sandbox_create_failed"})
                return results

            # Step 3: Prepare project
            log.info("Step 3/7: Preparing project '%s'", project)
            prepare_result = self._prepare_project(project, branch, name,
                                                   session_id)
            results["steps"]["prepare"] = prepare_result

            if prepare_result.get("status") != "ok":
                results["status"] = "failed"
                results["error"] = (f"Project preparation failed: "
                                    f"{prepare_result}")
                self.db.update_session(session_id, status="failed",
                                       error_message=results["error"])
                self.db.record_event(session_id, "pipeline_abort",
                                     {"reason": "project_prepare_failed"})
                return results

            # Step 4: Run inference
            log.info("Step 4/7: Running inference (config=%s, max_tokens=%d)",
                     config, max_tokens)
            self.db.update_session(session_id, status="running")
            infer_result = self._run_inference(task, config, name, session_id,
                                               max_tokens=max_tokens)
            results["steps"]["inference"] = infer_result

            if infer_result.get("status") != "ok":
                results["status"] = "failed"
                results["error"] = f"Inference failed: {infer_result}"
                self.db.update_session(session_id, status="failed",
                                       error_message=results["error"])
                self.db.record_event(session_id, "pipeline_inference_failed",
                                     infer_result)
                # Still try to collect partial results

            # Step 5: Collect results
            log.info("Step 5/7: Collecting results")
            results["_project_path"] = project_path
            collect_result = self._collect_results(name, results, session_id)
            results["steps"]["collect"] = collect_result

            # Step 6: Push to Gitea (only if inference succeeded)
            if infer_result.get("status") == "ok":
                log.info("Step 6/7: Pushing results to Gitea")
                push_result = self._push_results(project, branch, name,
                                                 session_id)
                results["steps"]["push"] = push_result
            else:
                log.info("Step 6/7: Skipping push (inference failed)")
                results["steps"]["push"] = {"status": "skipped",
                                             "reason": "inference_failed"}

        except Exception as exc:
            log.error("Pipeline error: %s", exc)
            results["status"] = "failed"
            results["error"] = str(exc)
            self.db.update_session(session_id, status="failed",
                                   error_message=str(exc))
            self.db.record_event(session_id, "pipeline_exception",
                                 {"error": str(exc)})

        finally:
            # Step 7: Always cleanup
            log.info("Step 7/7: Cleaning up sandbox '%s'", name)
            try:
                cleanup_result = self._destroy_sandbox(name, session_id)
                results["steps"]["cleanup"] = cleanup_result
            except Exception as exc:
                log.warning("Cleanup failed: %s", exc)
                results["steps"]["cleanup"] = {"status": "error",
                                                "error": str(exc)}

        # Final status
        total_s = time.monotonic() - pipeline_start
        results["elapsed_seconds"] = round(total_s, 2)

        if "status" not in results:
            results["status"] = "completed"

        self.db.update_session(
            session_id,
            status="completed" if results["status"] == "completed" else "failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.db.record_event(session_id, "pipeline_done", {
            "status": results["status"],
            "elapsed_s": round(total_s, 2),
        })

        return results

    # ------------------------------------------------------------------
    # Public API: bench
    # ------------------------------------------------------------------

    def bench(self, project: str, config: str = "3090-qwen36-35b",
              tasks: str = "throughput",
              name: str | None = None) -> dict:
        """Run benchmarks inside a sandbox.

        Args:
            project:  Project name
            config:   Model config ID
            tasks:    Comma-separated task types: throughput, quality, all
            name:     Session name (auto-generated if omitted)

        Returns:
            dict with benchmark results
        """
        if name is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"bench-{ts}"

        task_list = [t.strip() for t in tasks.split(",")]
        results: dict[str, Any] = {
            "name": name,
            "project": project,
            "config": config,
            "tasks": task_list,
            "benchmarks": {},
        }

        profile = CONFIG_TO_PROFILE.get(config)
        session_id = self.db.insert_session(
            sandbox_name=name,
            project=project,
            config_id=config,
            task_prompt=f"bench:{tasks}",
        )
        results["session_id"] = session_id

        t0 = time.monotonic()

        for task_type in task_list:
            log.info("Running benchmark: %s (config=%s)", task_type, config)

            if task_type == "throughput":
                script = f"{self.compose_dir}/scripts/bench-throughput.sh"
                cmd = f"bash {script} {profile or 'shared'} --reps 3 2>&1"
                raw, rc = self._ssh(cmd, timeout=600)
                results["benchmarks"][task_type] = {
                    "status": "ok" if rc == 0 else "failed",
                    "stdout": raw[-3000:] if raw else "",
                    "exit_code": rc,
                    "throughput": _parse_throughput(raw),
                }

            elif task_type == "quality":
                script = f"{self.compose_dir}/scripts/bench-quality.sh"
                cmd = f"bash {script} {profile or 'shared'} 2>&1"
                raw, rc = self._ssh(cmd, timeout=600)
                results["benchmarks"][task_type] = {
                    "status": "ok" if rc == 0 else "failed",
                    "stdout": raw[-3000:] if raw else "",
                    "exit_code": rc,
                }

            elif task_type == "all":
                # Run both throughput and quality
                for sub in ("throughput", "quality"):
                    sub_result = self.bench(project, config, sub,
                                            name=f"{name}-{sub}")
                    results["benchmarks"][sub] = sub_result

            else:
                results["benchmarks"][task_type] = {
                    "status": "error",
                    "error": f"Unknown benchmark type: {task_type}",
                }

        total_s = time.monotonic() - t0
        results["elapsed_seconds"] = round(total_s, 2)
        results["status"] = "completed"

        self.db.update_session(session_id, status="completed",
                               completed_at=datetime.now(timezone.utc).isoformat())
        self.db.record_event(session_id, "bench_done", {
            "tasks": task_list,
            "elapsed_s": round(total_s, 2),
        })

        return results

    # ------------------------------------------------------------------
    # Public API: sessions
    # ------------------------------------------------------------------

    def sessions(self, status: str | None = None) -> list[dict]:
        """List work sessions.

        Args:
            status:  Optional filter (created, running, completed, failed, destroyed)

        Returns:
            list of session dicts
        """
        return self.db.get_sessions(status=status)

    # ------------------------------------------------------------------
    # Public API: collect
    # ------------------------------------------------------------------

    def collect(self, name: str, local_path: str | None = None) -> dict:
        """Collect results from a completed session.

        Copies the results JSON from Triton to local machine.

        Args:
            name:       Session name
            local_path: Local destination (default: ./collected-{name}/)

        Returns:
            dict with collection status
        """
        if local_path is None:
            local_path = f"./collected-{name}"

        project_path = f"{DEFAULT_PROJECT_PATH}/{name}"
        remote_results = f"{project_path}/results-{name}.json"

        # Check if file exists on Triton
        raw, rc = self._ssh(f"test -f {remote_results} && echo EXISTS",
                            timeout=10)
        if rc != 0 or "EXISTS" not in raw:
            return {"status": "error",
                    "error": f"Results file not found: {remote_results}"}

        # SCP from Triton
        os.makedirs(local_path, exist_ok=True)
        local_file = os.path.join(local_path, f"results-{name}.json")
        _, scp_rc = scp_get(remote_results, local_file,
                            host=self.host, user=self.user)

        if scp_rc != 0:
            return {"status": "error", "error": "SCP transfer failed"}

        # Also collect session events from local telemetry DB
        sessions = self.db.get_sessions()
        session_id = None
        for s in sessions:
            if s["sandbox_name"] == name:
                session_id = s["id"]
                break

        events = self.db.get_events(session_id) if session_id else []

        return {
            "status": "ok",
            "local_path": local_file,
            "session_id": session_id,
            "events": events,
        }

    # ------------------------------------------------------------------
    # Public API: cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> dict:
        """Destroy all harness sandboxes on Triton.

        Returns:
            dict with destruction summary
        """
        # List all harness containers
        cmd = (
            f"docker ps -a "
            f"--filter name={SANDBOX_PREFIX} "
            f"--format '{{{{.Names}}}}'"
        )
        raw, rc = self._ssh(cmd, timeout=15)

        containers = []
        if rc == 0 and raw:
            containers = [c.strip() for c in raw.splitlines() if c.strip()]

        results = []
        for cname in containers:
            bare = cname.replace(SANDBOX_PREFIX, "", 1)
            out, drc = self._ssh(f"docker rm -f {cname}", timeout=15)
            results.append({
                "name": bare,
                "full_name": cname,
                "status": "destroyed" if drc == 0 else "error",
                "exit_code": drc,
            })

            # Update telemetry
            sessions = self.db.get_sessions()
            for s in sessions:
                if s["sandbox_name"] == bare or s["sandbox_name"] == cname:
                    self.db.update_session(
                        s["id"],
                        status="destroyed",
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self.db.record_event(s["id"], "sandbox_destroyed",
                                         {"name": bare})
                    break

        return {
            "destroyed": len(results),
            "details": results,
        }

    # ------------------------------------------------------------------
    # Public API: status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """System status: model, GPU, sandboxes, telemetry.

        Returns:
            dict with comprehensive status
        """
        result: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "host": self.host,
            "model": {},
            "gpus": [],
            "sandboxes": [],
            "telemetry": {},
            "gitea": {},
        }

        # Model status (BeeLlama health + models)
        for label, port in [("3090", BEE_LLAMA_PORT_3090),
                            ("3070", BEE_LLAMA_PORT_3070)]:
            healthy = self._check_health(port)
            models = []
            if healthy:
                raw, rc = self._ssh(f"curl -sf http://localhost:{port}/v1/models",
                                    timeout=10)
                if rc == 0:
                    try:
                        data = json.loads(raw)
                        models = [m.get("id", "?")
                                  for m in data.get("data", [])]
                    except (json.JSONDecodeError, AttributeError):
                        pass
            result["model"][label] = {
                "port": port,
                "healthy": healthy,
                "models": models,
            }

        # GPU status
        fields = ("index,name,memory.used,memory.total,"
                  "temperature.gpu,utilization.gpu")
        raw, rc = self._ssh(
            f"nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits",
            timeout=10,
        )
        if rc == 0:
            for line in raw.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    result["gpus"].append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "memory_used_mb": int(parts[2]),
                        "memory_total_mb": int(parts[3]),
                        "temperature_c": int(parts[4]),
                        "utilization_pct": int(parts[5]),
                    })

        # Active sandboxes
        cmd = (
            f"docker ps -a "
            f"--filter name={SANDBOX_PREFIX} "
            f"--format '{{{{.Names}}}}\t{{{{.Status}}}}\t{{{{Image}}}}'"
        )
        raw, rc = self._ssh(cmd, timeout=10)
        if rc == 0 and raw:
            for line in raw.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    result["sandboxes"].append({
                        "name": parts[0].replace(SANDBOX_PREFIX, ""),
                        "full_name": parts[0],
                        "status": parts[1],
                        "image": parts[2],
                    })

        # Telemetry summary
        result["telemetry"] = self.db.summary()

        # Gitea health
        raw, rc = self._ssh(
            f"curl -sf http://localhost:3000/api/v1/version "
            f"-H 'Authorization: token {self.gitea_token}'",
            timeout=10,
        )
        if rc == 0:
            try:
                ver = json.loads(raw)
                result["gitea"] = {
                    "status": "ok",
                    "version": ver.get("version", "unknown"),
                }
            except json.JSONDecodeError:
                result["gitea"] = {"status": "ok", "raw": raw}
        else:
            result["gitea"] = {"status": "unreachable"}

        # Overall health
        all_ok = (
            result["model"].get("3090", {}).get("healthy", False)
            and len(result["gpus"]) >= 1
        )
        result["overall"] = "healthy" if all_ok else "degraded"

        return result


# ===========================================================================
# Output helpers
# ===========================================================================

def _parse_throughput(text: str) -> dict | None:
    """Extract tok/s numbers from benchmark output."""
    values = []
    for line in text.splitlines():
        low = line.lower()
        if "tok/s" in low or "tokens/sec" in low or "t/s" in low:
            nums = re.findall(r'(\d+\.?\d*)\s*(?:tok|t)/s', low)
            values.extend(float(n) for n in nums)
    if values:
        return {
            "values": values,
            "avg": round(sum(values) / len(values), 1),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
        }
    return None


def output(data: Any, pretty: bool = False) -> None:
    """Print data as JSON."""
    if pretty:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, default=str))


# ===========================================================================
# CLI
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="work_engine",
        description="Orchestrate the full remote work pipeline on Triton.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s run --project coder-harness --config 3090-qwen36-35b \\
                --task "Fix the SSH pattern to use key-based auth" --name fix-ssh-01

              %(prog)s bench --project coder-harness --config 3090-qwen36-35b \\
                --tasks throughput,quality

              %(prog)s sessions
              %(prog)s collect --name fix-ssh-01
              %(prog)s cleanup
              %(prog)s status --pretty
        """),
    )
    p.add_argument("--host", default=TRITON_HOST,
                   help="Triton host (default: %(default)s)")
    p.add_argument("--user", default=TRITON_USER,
                   help="SSH user (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without executing.")
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print JSON output.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging.")

    sub = p.add_subparsers(dest="command", help="Available commands")

    # ---- run ----
    run_p = sub.add_parser("run", help="Execute a coding task (full pipeline).")
    run_p.add_argument("--project", required=True,
                       help="Project name (under ~/projects/ on Triton).")
    run_p.add_argument("--branch", default="main",
                       help="Git branch (default: main).")
    run_p.add_argument("--config", default="3090-qwen36-35b",
                       help="Model config ID (default: 3090-qwen36-35b).")
    run_p.add_argument("--task", required=True,
                       help="Task prompt for the model.")
    run_p.add_argument("--name", default=None,
                       help="Session name (auto-generated if omitted).")
    run_p.add_argument("--max-tokens", type=int, default=2048,
                       help="Max tokens for inference (default: 2048).")

    # ---- bench ----
    bench_p = sub.add_parser("bench", help="Run benchmarks on a project.")
    bench_p.add_argument("--project", required=True,
                         help="Project name.")
    bench_p.add_argument("--config", default="3090-qwen36-35b",
                         help="Model config ID (default: 3090-qwen36-35b).")
    bench_p.add_argument("--tasks", default="throughput",
                         help="Comma-separated benchmark types: "
                              "throughput,quality,all (default: throughput).")
    bench_p.add_argument("--name", default=None,
                         help="Session name (auto-generated if omitted).")

    # ---- sessions ----
    sess_p = sub.add_parser("sessions", help="List work sessions.")
    sess_p.add_argument("--status", default=None,
                        help="Filter by status (created, running, completed, "
                             "failed, destroyed).")

    # ---- collect ----
    coll_p = sub.add_parser("collect",
                            help="Collect results from a completed session.")
    coll_p.add_argument("--name", required=True,
                        help="Session name.")
    coll_p.add_argument("--to", dest="local_path", default=None,
                        help="Local destination path.")

    # ---- cleanup ----
    sub.add_parser("cleanup", help="Destroy all harness sandboxes.")

    # ---- status ----
    sub.add_parser("status", help="System status: model, GPU, sandboxes, telemetry.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return 0

    engine = WorkEngine(host=args.host, user=args.user, dry_run=args.dry_run)
    pretty = args.pretty

    if args.command == "run":
        result = engine.run(
            project=args.project,
            branch=args.branch,
            config=args.config,
            task=args.task,
            name=args.name,
            max_tokens=args.max_tokens,
        )
    elif args.command == "bench":
        result = engine.bench(
            project=args.project,
            config=args.config,
            tasks=args.tasks,
            name=args.name,
        )
    elif args.command == "sessions":
        result = engine.sessions(status=getattr(args, "status", None))
    elif args.command == "collect":
        result = engine.collect(
            name=args.name,
            local_path=getattr(args, "local_path", None),
        )
    elif args.command == "cleanup":
        result = engine.cleanup()
    elif args.command == "status":
        result = engine.status()
    else:
        parser.print_help()
        return 1

    output(result, pretty=pretty)
    return 0


if __name__ == "__main__":
    sys.exit(main())
