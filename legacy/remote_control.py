#!/usr/bin/env python3
"""remote_control.py — Unified CLI for controlling the Triton remote coding engine.

Combines SSH, Docker, BeeLlama API, and Gitea operations into one interface.
Runs on nightmare, targets <user>@<LAN_IP> (Triton) via SSH.

Usage:
    python3 remote_control.py model status [--pretty]
    python3 remote_control.py model swap <config> [--dry-run] [--pretty]
    python3 remote_control.py model list [--pretty]
    python3 remote_control.py bench throughput --config <id> [--reps N] [--pretty]
    python3 remote_control.py bench quality --config <id> [--tasks <csv>] [--pretty]
    python3 remote_control.py bench all --configs <csv> [--pretty]
    python3 remote_control.py sandbox create --project <name> [--name <sbox>] [--pretty]
    python3 remote_control.py sandbox exec --name <sbox> --command <cmd> [--pretty]
    python3 remote_control.py sandbox collect --name <sbox> --from <path> [--pretty]
    python3 remote_control.py sandbox destroy --name <sbox> [--dry-run] [--pretty]
    python3 remote_control.py project status [--pretty]
    python3 remote_control.py project sync --local <path> --remote <path> [--dry-run] [--pretty]
    python3 remote_control.py telemetry dashboard [--pretty]
    python3 remote_control.py telemetry export [--pretty]
    python3 remote_control.py system health [--pretty]
    python3 remote_control.py system gpu [--pretty]
    python3 remote_control.py system logs [--pretty]
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"
COMPOSE_DIR = "~/dockers/beellama-benchmark"
BEE_LLAMA_PORT_3090 = 8080
BEE_LLAMA_PORT_3070 = 8082

# Manifest config ID -> Docker Compose profile name
CONFIG_TO_PROFILE = {
    "3090-qwen36-35b":   "config-i",
    "3090-muse-glimmer":  "config-h",
    "3090-laguna-xs":     "config-g-laguna",
    "3090-qwen35-9b":     "config-h-qwen35",
    "3070-qwen35-9b":     None,          # shared 3070 service, no profile toggle
    "3070-qwen35-4b":     None,          # no dedicated profile
}

# Docker Compose profile -> human-readable model name
PROFILE_MODELS = {
    "config-i":        "Qwen3.6-35B-A3B IQ4_XS",
    "config-h":        "Muse-Glimmer-30B Q3_K_XL",
    "config-g-laguna":  "Laguna-XS-2.1 APEX-i-quality",
    "config-g-glm":     "GLM-4.7-Flash 23B-A3B",
    "config-h-coder":   "Qwen3-Coder-30B + cpu-moe",
    "config-h-qwen35":  "Qwen3.5-9B-MTP Q4_K_M",
}

# Candidate known configs for listing
ALL_CONFIGS = [
    {"id": "3090-qwen36-35b",  "gpu": "3090", "model": "Qwen3.6-35B-A3B IQ4_XS",    "vram_mb": 18800, "ctx": 128000, "profile": "config-i"},
    {"id": "3090-muse-glimmer", "gpu": "3090", "model": "Muse-Glimmer-30B Q3_K_XL",  "vram_mb": 15900, "ctx": 32000,  "profile": "config-h"},
    {"id": "3090-laguna-xs",    "gpu": "3090", "model": "Laguna-XS-2.1 APEX-i-quality","vram_mb": 19100, "ctx": 8000,  "profile": "config-g-laguna"},
    {"id": "3090-qwen35-9b",    "gpu": "3090", "model": "Qwen3.5-9B-MTP Q4_K_M",     "vram_mb": 8000,  "ctx": 140000, "profile": "config-h-qwen35"},
    {"id": "3070-qwen35-9b",    "gpu": "3070", "model": "Qwen3.5-9B-MTP Q4_K_M",     "vram_mb": 6900,  "ctx": 8000,   "profile": None},
    {"id": "3070-qwen35-4b",    "gpu": "3070", "model": "Qwen3.5-4B Q5_K_M",          "vram_mb": 4000,  "ctx": 32000,  "profile": None},
]


# ---------------------------------------------------------------------------
# SSH transport
# ---------------------------------------------------------------------------

def ssh_run(cmd, timeout=30):
    """Execute *cmd* on Triton via SSH. Returns (stdout: str, exit_code: int).

    Uses key-based SSH (no sshpass, no paramiko).
    """
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{TRITON_USER}@{TRITON_HOST}",
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


def scp_put(local_path, remote_path, recursive=False):
    """Copy *local_path* to Triton via SCP. Returns (message, exit_code)."""
    scp_cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
    ]
    if recursive:
        scp_cmd.append("-r")
    scp_cmd.extend([
        local_path,
        f"{TRITON_USER}@{TRITON_HOST}:{remote_path}",
    ])
    try:
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return f"Uploaded {local_path} → {remote_path}", 0
        return f"SCP failed: {result.stderr.strip()}", result.returncode
    except subprocess.TimeoutExpired:
        return "SCP transfer timed out", 1
    except Exception as exc:
        return f"SCP error: {exc}", 1


def ssh_json(cmd, timeout=30):
    """Run *cmd* on Triton and parse the first JSON object from stdout.

    Returns (parsed_dict_or_list, exit_code).  On parse failure the raw
    string is returned as the value.
    """
    raw, rc = ssh_run(cmd, timeout=timeout)
    if rc != 0:
        return {"error": raw, "exit_code": rc}, rc
    try:
        return json.loads(raw), 0
    except (json.JSONDecodeError, ValueError):
        return raw, 0


# ---------------------------------------------------------------------------
# RemoteControl — the unified interface
# ---------------------------------------------------------------------------

class RemoteControl:
    """Unified controller for the Triton remote coding engine."""

    def __init__(self, host=TRITON_HOST, user=TRITON_USER):
        self.host = host
        self.user = user
        self.compose_dir = COMPOSE_DIR

    # ---- helpers ----

    def _ssh(self, cmd, timeout=30):
        return ssh_run(cmd, timeout=timeout)

    def _compose(self, sub_cmd, timeout=30):
        full = f"cd {self.compose_dir} && {sub_cmd}"
        return self._ssh(full, timeout=timeout)

    def _curl_health(self, port, timeout=10):
        """Return True if BeeLlama health endpoint is ok."""
        raw, rc = self._ssh(f"curl -sf http://localhost:{port}/health", timeout=timeout)
        if rc != 0:
            return False
        try:
            return json.loads(raw).get("status") == "ok"
        except (json.JSONDecodeError, AttributeError):
            return False

    def _nvidia_smi(self, timeout=10):
        """Query GPU stats via nvidia-smi. Returns list of dicts."""
        fields = "index,name,memory.used,memory.total,temperature.gpu,utilization.gpu,driver_version"
        raw, rc = self._ssh(
            f"nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits",
            timeout=timeout,
        )
        if rc != 0:
            return []
        gpus = []
        for line in raw.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_used_mb": int(parts[2]),
                    "memory_total_mb": int(parts[3]),
                    "temperature_c": int(parts[4]),
                    "utilization_pct": int(parts[5]),
                    "driver_version": parts[6] if len(parts) > 6 else "unknown",
                })
        return gpus

    # ===================================================================
    # MODEL operations
    # ===================================================================

    def model_status(self):
        """Get current loaded model, GPU stats, and BeeLlama health."""
        result = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "host": self.host,
            "endpoints": {},
            "gpus": [],
        }

        # BeeLlama health + model list on each port
        for label, port in [("3090", BEE_LLAMA_PORT_3090), ("3070", BEE_LLAMA_PORT_3070)]:
            healthy = self._curl_health(port)
            models = []
            if healthy:
                raw, rc = self._ssh(f"curl -sf http://localhost:{port}/v1/models", timeout=10)
                if rc == 0:
                    try:
                        data = json.loads(raw)
                        models = [m.get("id", "?") for m in data.get("data", [])]
                    except (json.JSONDecodeError, AttributeError):
                        pass
            result["endpoints"][label] = {
                "port": port,
                "healthy": healthy,
                "models": models,
            }

        result["gpus"] = self._nvidia_smi()

        # Detect active Docker profile
        raw, rc = self._ssh(
            f"cd {self.compose_dir} && docker compose ls --format json 2>/dev/null || true",
            timeout=15,
        )
        active_profiles = []
        if rc == 0 and raw:
            try:
                services = json.loads(raw)
                if isinstance(services, list):
                    for svc in services:
                        name = svc.get("Name", "")
                        if "llama" in name.lower() or "beellama" in name.lower():
                            active_profiles.append(name)
            except (json.JSONDecodeError, TypeError):
                pass
        result["active_containers"] = active_profiles

        # Fallback: also try `docker ps`
        raw2, rc2 = self._ssh(
            "docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null | grep -i llama || true",
            timeout=10,
        )
        if rc2 == 0 and raw2:
            result["running_containers"] = []
            for line in raw2.strip().splitlines():
                parts = line.split("\t")
                result["running_containers"].append({
                    "name": parts[0] if len(parts) > 0 else "?",
                    "status": parts[1] if len(parts) > 1 else "?",
                    "ports": parts[2] if len(parts) > 2 else "?",
                })

        return result

    def model_swap(self, config_id, dry_run=False):
        """Swap to a different model config via the deploy state machine.

        Returns {status, load_time_ms, config_id, profile}.
        """
        profile = CONFIG_TO_PROFILE.get(config_id)
        if profile is None and config_id not in CONFIG_TO_PROFILE:
            return {"status": "error", "message": f"Unknown config: {config_id}"}

        result = {"config_id": config_id, "profile": profile, "dry_run": dry_run}

        if dry_run:
            # Describe what would happen
            if profile:
                result["plan"] = [
                    f"cd {self.compose_dir}",
                    f"docker compose --profile {profile} down",
                    f"docker compose --profile {profile} up -d",
                    "Wait for /health on port 8080",
                ]
            else:
                result["plan"] = [
                    f"Config {config_id} uses the shared 3070 service",
                    "No profile swap needed — model is always loaded",
                ]
            result["status"] = "dry_run"
            return result

        # Use the deploy state machine if available
        t0 = time.monotonic()
        if profile:
            # Try state machine first, fall back to direct compose
            sm_cmd = (
                f"cd {self.compose_dir} && "
                f"python3 deploy-state-machine.py swap {profile} 2>/dev/null"
            )
            raw, rc = self._ssh(sm_cmd, timeout=300)
            if rc == 0 and "READY" in raw.upper():
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                result.update({"status": "ok", "load_time_ms": elapsed_ms, "method": "state_machine"})
                return result

            # Fallback: direct docker compose
            self._compose(f"docker compose --profile {profile} down 2>/dev/null", timeout=60)
            self._compose(f"docker compose --profile {profile} up -d", timeout=60)

            # Poll health (max 180s)
            for _ in range(60):
                if self._curl_health(BEE_LLAMA_PORT_3090):
                    break
                time.sleep(3)
            else:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                result.update({"status": "timeout", "load_time_ms": elapsed_ms,
                               "message": "BeeLlama did not become healthy within 180s"})
                return result
        else:
            result["status"] = "no_swap_needed"
            result["message"] = f"{config_id} uses the shared 3070 service"
            return result

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result.update({"status": "ok", "load_time_ms": elapsed_ms, "method": "docker_compose"})
        return result

    def model_list(self):
        """Return list of all known model configs."""
        # Prefer the live manifest on Triton if present
        manifest_path = f"{self.compose_dir}/../models/manifest.json"
        raw, rc = self._ssh(f"cat {manifest_path} 2>/dev/null", timeout=10)
        if rc == 0:
            try:
                data = json.loads(raw)
                if "configs" in data:
                    return data["configs"]
            except (json.JSONDecodeError, ValueError):
                pass
        # Fall back to local copy
        local_manifest = Path(__file__).parent / "models" / "manifest.json"
        if local_manifest.exists():
            with open(local_manifest) as f:
                data = json.load(f)
            return data.get("configs", ALL_CONFIGS)
        return ALL_CONFIGS

    # ===================================================================
    # BENCHMARK operations
    # ===================================================================

    def bench_throughput(self, config_id, reps=3, dry_run=False):
        """Run throughput benchmark on Triton.

        Returns dict with per-rep timing results.
        """
        profile = CONFIG_TO_PROFILE.get(config_id)
        result = {"config_id": config_id, "profile": profile, "reps": reps, "dry_run": dry_run}

        if dry_run:
            result["plan"] = [
                f"ssh → cd {self.compose_dir}/../ && bash scripts/bench-throughput.sh {profile or 'shared'} --reps {reps}",
            ]
            result["status"] = "dry_run"
            return result

        # Run the throughput bench script on Triton
        script = f"{self.compose_dir}/scripts/bench-throughput.sh"
        cmd = f"bash {script} {profile or 'shared'} --reps {reps} 2>&1"
        raw, rc = self._ssh(cmd, timeout=max(300, reps * 120))
        result["stdout"] = raw
        result["exit_code"] = rc
        result["status"] = "ok" if rc == 0 else "failed"

        # Try to parse throughput numbers from output
        result["throughput"] = _parse_throughput(raw)
        return result

    def bench_quality(self, config_id, tasks="all", dry_run=False):
        """Run quality benchmark on Triton.

        Returns dict with task-level quality results.
        """
        profile = CONFIG_TO_PROFILE.get(config_id)
        result = {"config_id": config_id, "profile": profile, "tasks": tasks, "dry_run": dry_run}

        if dry_run:
            result["plan"] = [
                f"ssh → cd {self.compose_dir}/../ && bash scripts/bench-quality.sh {profile or 'shared'} --tasks {tasks}",
            ]
            result["status"] = "dry_run"
            return result

        script = f"{self.compose_dir}/scripts/bench-quality.sh"
        task_flag = f"--tasks {tasks}" if tasks != "all" else ""
        cmd = f"bash {script} {profile or 'shared'} {task_flag} 2>&1"
        raw, rc = self._ssh(cmd, timeout=600)
        result["stdout"] = raw
        result["exit_code"] = rc
        result["status"] = "ok" if rc == 0 else "failed"
        return result

    def bench_all(self, config_ids, dry_run=False):
        """Run benchmarks across multiple configs."""
        results = []
        for cid in config_ids:
            r = self.bench_throughput(cid, dry_run=dry_run)
            results.append(r)
            if not dry_run and r.get("status") != "ok":
                # Abort on first failure unless dry-run
                results.append({"error": f"Benchmark failed on {cid}, stopping", "status": "aborted"})
                break
        return results

    # ===================================================================
    # SANDBOX operations
    # ===================================================================

    def sandbox_create(self, project, name=None, dry_run=False):
        """Create a Docker sandbox container on Triton.

        Returns {container_id, name, status}.
        """
        name = name or f"sandbox-{project}-{int(time.time())}"
        result = {"project": project, "name": name, "dry_run": dry_run}

        if dry_run:
            result["plan"] = [
                f"docker run -d --name {name} "
                f"-v ~/projects/{project}:/workspace "
                f"--gpus '\"device=0\"' "
                f"nvidia/cuda:12.4.0-base-ubuntu22.04 "
                f"tail -f /dev/null",
            ]
            result["status"] = "dry_run"
            return result

        # Create container with GPU access and project mounted
        cmd = (
            f"docker run -d --name {name} "
            f"-v ~/projects/{project}:/workspace "
            f"--gpus '\"device=0\"' "
            f"nvidia/cuda:12.4.0-base-ubuntu22.04 "
            f"tail -f /dev/null"
        )
        raw, rc = self._ssh(cmd, timeout=120)
        if rc == 0 and len(raw) > 0:
            result.update({"container_id": raw[:12], "status": "created"})
        else:
            result.update({"status": "error", "message": raw})
        return result

    def sandbox_exec(self, name, command, timeout=60):
        """Execute a command inside a sandbox container.

        Returns {stdout, exit_code}.
        """
        cmd = f"docker exec {name} {command}"
        raw, rc = self._ssh(cmd, timeout=timeout)
        return {"container": name, "command": command, "stdout": raw, "exit_code": rc}

    def sandbox_collect(self, name, remote_path, local_path=None):
        """Copy files from a sandbox container to nightmare.

        Uses docker cp to extract, then SCP back.
        """
        result = {"container": name, "remote_path": remote_path}
        if local_path is None:
            local_path = f"./collected-{name}-{int(time.time())}"

        # docker cp from container to Triton host
        tmp = f"/tmp/collect-{name}"
        self._ssh(f"docker cp {name}:{remote_path} {tmp}", timeout=60)

        # SCP from Triton to nightmare
        msg, rc = scp_put(
            f"{self.user}@{self.host}:{tmp}",
            local_path,
            recursive=True,
        )
        result["local_path"] = local_path
        result["status"] = "ok" if rc == 0 else "error"
        result["message"] = msg
        return result

    def sandbox_destroy(self, name, dry_run=False):
        """Destroy a Docker sandbox container."""
        result = {"name": name, "dry_run": dry_run}

        if dry_run:
            result["plan"] = [f"docker rm -f {name}"]
            result["status"] = "dry_run"
            return result

        self._ssh(f"docker rm -f {name}", timeout=30)
        result["status"] = "destroyed"
        return result

    # ===================================================================
    # PROJECT operations
    # ===================================================================

    def project_status(self):
        """List projects present on Triton."""
        raw, rc = self._ssh("ls -1d ~/projects/*/ 2>/dev/null || true", timeout=10)
        projects = []
        if rc == 0 and raw:
            for line in raw.strip().splitlines():
                path = line.rstrip("/")
                name = os.path.basename(path)
                # Get some info about each project
                size_raw, _ = self._ssh(f"du -sh {path} 2>/dev/null | cut -f1", timeout=10)
                git_branch = self._ssh(f"cd {path} && git branch --show-current 2>/dev/null || echo 'n/a'", timeout=10)[0]
                projects.append({
                    "name": name,
                    "path": path,
                    "size": size_raw.strip() if size_raw else "?",
                    "branch": git_branch.strip() if git_branch else "n/a",
                })
        return projects

    def project_sync(self, local_path, remote_path, dry_run=False):
        """Sync a local project to Triton via SCP/rsync.

        Uses rsync over SSH for incremental sync.
        """
        result = {"local": local_path, "remote": remote_path, "dry_run": dry_run}

        if dry_run:
            result["plan"] = [
                f"rsync -avz --delete -e 'ssh -o StrictHostKeyChecking=no' "
                f"{local_path}/ {TRITON_USER}@{TRITON_HOST}:{remote_path}/",
            ]
            result["status"] = "dry_run"
            return result

        # Use rsync for efficient incremental transfer
        rsync_cmd = [
            "rsync", "-avz", "--delete",
            "-e", "ssh -o StrictHostKeyChecking=no",
            f"{local_path}/",
            f"{TRITON_USER}@{TRITON_HOST}:{remote_path}/",
        ]
        try:
            proc = subprocess.run(
                rsync_cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            result["stdout"] = proc.stdout
            result["exit_code"] = proc.returncode
            result["status"] = "ok" if proc.returncode == 0 else "error"
            if proc.returncode != 0:
                result["stderr"] = proc.stderr
        except subprocess.TimeoutExpired:
            result["status"] = "error"
            result["message"] = "rsync timed out after 300s"
        except Exception as exc:
            result["status"] = "error"
            result["message"] = str(exc)
        return result

    # ===================================================================
    # TELEMETRY operations
    # ===================================================================

    def telemetry_dashboard(self):
        """Generate an HTML dashboard of system and benchmark status.

        Returns HTML string.
        """
        status = self.model_status()
        gpus = status.get("gpus", [])
        endpoints = status.get("endpoints", {})

        gpu_rows = ""
        for g in gpus:
            pct = int(g["memory_used_mb"] / max(g["memory_total_mb"], 1) * 100)
            gpu_rows += textwrap.dedent(f"""\
            <tr>
              <td>{g['index']}</td>
              <td>{g['name']}</td>
              <td>{g['memory_used_mb']}/{g['memory_total_mb']} MB ({pct}%)</td>
              <td>{g['temperature_c']}°C</td>
              <td>{g['utilization_pct']}%</td>
            </tr>\n""")

        ep_rows = ""
        for label, ep in endpoints.items():
            status_icon = "🟢" if ep["healthy"] else "🔴"
            models_str = ", ".join(ep["models"]) if ep["models"] else "none"
            ep_rows += textwrap.dedent(f"""\
            <tr>
              <td>{label} (:{ep['port']})</td>
              <td>{status_icon}</td>
              <td>{models_str}</td>
            </tr>\n""")

        html = textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html>
        <head>
          <title>Triton Dashboard — Remote Control</title>
          <style>
            body {{ font-family: system-ui, sans-serif; margin: 2em; background: #0d1117; color: #c9d1d9; }}
            h1 {{ color: #58a6ff; }}
            table {{ border-collapse: collapse; margin: 1em 0; }}
            th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; }}
            th {{ background: #161b22; color: #58a6ff; }}
            tr:nth-child(even) {{ background: #161b22; }}
            .ok {{ color: #3fb950; }}
            .fail {{ color: #f85149; }}
          </style>
        </head>
        <body>
          <h1>Triton Remote Control Dashboard</h1>
          <p>Generated: {status['timestamp']}</p>
          <p>Host: {self.user}@{self.host}</p>

          <h2>GPUs</h2>
          <table>
            <tr><th>#</th><th>Model</th><th>VRAM</th><th>Temp</th><th>Utilization</th></tr>
            {gpu_rows}
          </table>

          <h2>BeeLlama Endpoints</h2>
          <table>
            <tr><th>Endpoint</th><th>Status</th><th>Models</th></tr>
            {ep_rows}
          </table>

          <h2>Running Containers</h2>
          <pre>{json.dumps(status.get('running_containers', []), indent=2)}</pre>
        </body>
        </html>
        """)
        return html

    def telemetry_export(self):
        """Export SQLite benchmark database to JSON.

        Runs a query on Triton and parses the results.
        """
        db_path = f"{self.compose_dir}/../benchmark-results.db"
        # Query all completed runs with scores
        query = """
        SELECT
            br.id, br.model_config_id, br.task_id, br.turn_index,
            br.repetition, br.status, br.predicted_per_second,
            br.prompt_per_second, br.predicted_ms, br.prompt_ms,
            br.predicted_n, br.thinking_tokens, br.total_tokens,
            br.started_at, br.completed_at,
            js.completeness, js.correctness, js.quality,
            js.intelligence, js.role_fit, js.overall, js.scored_at
        FROM benchmark_runs br
        LEFT JOIN judge_scores js ON br.id = js.run_id
        WHERE br.status = 'complete'
        ORDER BY br.model_config_id, br.task_id
        """
        cmd = f"sqlite3 -json {db_path} \"{query}\" 2>/dev/null"
        raw, rc = self._ssh(cmd, timeout=30)
        if rc == 0 and raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass
        return {"note": "No data found or database not present on Triton", "raw": raw}


    # ===================================================================
    # SYSTEM operations
    # ===================================================================

    def system_health(self):
        """Full health check: SSH, GPU, BeeLlama, disk, Docker, load."""
        result = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "host": self.host,
            "checks": {},
        }

        # SSH
        t0 = time.monotonic()
        raw, rc = self._ssh("echo OK", timeout=10)
        ssh_ms = int((time.monotonic() - t0) * 1000)
        result["checks"]["ssh"] = {
            "status": "pass" if rc == 0 and "OK" in raw else "fail",
            "latency_ms": ssh_ms,
        }

        # GPU
        gpus = self._nvidia_smi()
        gpu_ok = len(gpus) >= 2
        gpu_detail = []
        for g in gpus:
            over_temp = g["temperature_c"] > 83
            gpu_detail.append({
                **g,
                "over_temp": over_temp,
                "status": "fail" if over_temp else "pass",
            })
        result["checks"]["gpu"] = {
            "status": "pass" if gpu_ok else ("warn" if gpus else "fail"),
            "count": len(gpus),
            "gpus": gpu_detail,
        }

        # BeeLlama endpoints
        for label, port in [("3090", BEE_LLAMA_PORT_3090), ("3070", BEE_LLAMA_PORT_3070)]:
            healthy = self._curl_health(port)
            result["checks"][f"beellama_{label}"] = {
                "status": "pass" if healthy else "fail",
                "port": port,
            }

        # Disk space
        raw, rc = self._ssh("df -BG /home/<user>/ | tail -1 | awk '{print $4}'", timeout=10)
        if rc == 0:
            gb = int(raw.strip().replace("G", ""))
            result["checks"]["disk"] = {
                "status": "pass" if gb >= 10 else "fail",
                "available_gb": gb,
            }
        else:
            result["checks"]["disk"] = {"status": "fail", "detail": raw}

        # System load
        raw, rc = self._ssh("cat /proc/loadavg | awk '{print $1}'", timeout=10)
        if rc == 0:
            load = float(raw.strip())
            result["checks"]["load"] = {
                "status": "pass" if load < 4.0 else "warn",
                "load_avg_1m": load,
            }

        # Docker
        raw, rc = self._ssh("docker ps --format '{{.Names}}' 2>/dev/null | head -20", timeout=10)
        if rc == 0:
            containers = [n for n in raw.strip().splitlines() if n]
            result["checks"]["docker"] = {
                "status": "pass",
                "running_containers": len(containers),
                "containers": containers,
            }
        else:
            result["checks"]["docker"] = {"status": "fail", "detail": raw}

        # Overall
        statuses = [c["status"] for c in result["checks"].values()]
        if all(s == "pass" for s in statuses):
            result["overall"] = "healthy"
        elif any(s == "fail" for s in statuses):
            result["overall"] = "degraded"
        else:
            result["overall"] = "warning"

        return result

    def system_gpu(self):
        """Detailed GPU status."""
        gpus = self._nvidia_smi()
        # Also get process info
        raw, rc = self._ssh(
            "nvidia-smi --query-compute-apps=pid,process_name,used_memory "
            "--format=csv,noheader,nounits 2>/dev/null || true",
            timeout=10,
        )
        processes = []
        if rc == 0 and raw:
            for line in raw.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    processes.append({
                        "pid": int(parts[0]),
                        "name": parts[1],
                        "memory_mb": int(parts[2]),
                    })

        return {
            "gpus": gpus,
            "processes": processes,
            "gpu_count": len(gpus),
        }

    def system_logs(self, lines=50):
        """Get recent container logs from Triton."""
        logs = {}
        for label, port in [("3090", BEE_LLAMA_PORT_3090), ("3070", BEE_LLAMA_PORT_3070)]:
            # Find the container for this port
            raw, rc = self._ssh(
                f"docker ps --filter 'publish={port}' --format '{{{{.Names}}}}' 2>/dev/null",
                timeout=10,
            )
            if rc == 0 and raw.strip():
                container = raw.strip().splitlines()[0]
                log_raw, _ = self._ssh(
                    f"docker logs --tail {lines} {container} 2>&1",
                    timeout=15,
                )
                logs[label] = {
                    "container": container,
                    "port": port,
                    "log": log_raw,
                }
            else:
                logs[label] = {"container": "not found", "port": port, "log": ""}

        # Also get system journal errors
        journal_raw, _ = self._ssh(
            f"journalctl -p err --no-pager -n {lines} 2>/dev/null | tail -20",
            timeout=10,
        )
        logs["system_errors"] = journal_raw

        return logs


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _parse_throughput(text):
    """Attempt to extract tok/s numbers from benchmark output."""
    results = []
    for line in text.splitlines():
        low = line.lower()
        if "tok/s" in low or "tokens/sec" in low or "t/s" in low:
            # Try to find a number
            import re
            nums = re.findall(r'(\d+\.?\d*)\s*(?:tok|t)/s', low)
            if nums:
                results.append(float(nums[0]))
    if results:
        return {
            "values": results,
            "avg": sum(results) / len(results),
            "min": min(results),
            "max": max(results),
        }
    return None


def output(data, pretty=False):
    """Print data as JSON (or pretty-printed)."""
    if pretty:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, default=str))


# ---------------------------------------------------------------------------
# CLI — argparse
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="remote_control",
        description="Unified CLI for controlling the Triton remote coding engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s model status --pretty
              %(prog)s model swap config-i --dry-run
              %(prog)s bench throughput --config 3090-qwen36-35b --reps 3
              %(prog)s sandbox create --project coder-harness --name test-01
              %(prog)s system health --pretty
        """),
    )
    parser.add_argument("--host", default=TRITON_HOST, help="Triton host (default: %(default)s)")
    parser.add_argument("--user", default=TRITON_USER, help="Triton user (default: %(default)s)")

    sub = parser.add_subparsers(dest="group", help="Command group")

    # ---- model ----
    model_p = sub.add_parser("model", help="Model operations")
    model_sub = model_p.add_subparsers(dest="action")

    model_sub.add_parser("status", help="Show loaded model + GPU stats")
    model_sub.add_parser("list", help="List all available configs")

    swap_p = model_sub.add_parser("swap", help="Swap to a different model config")
    swap_p.add_argument("config", help="Config ID or Docker profile name (e.g. config-i)")
    swap_p.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # ---- bench ----
    bench_p = sub.add_parser("bench", help="Benchmark operations")
    bench_sub = bench_p.add_subparsers(dest="action")

    tp_p = bench_sub.add_parser("throughput", help="Run throughput benchmark")
    tp_p.add_argument("--config", required=True, help="Config ID (e.g. 3090-qwen36-35b)")
    tp_p.add_argument("--reps", type=int, default=3, help="Repetitions (default: 3)")
    tp_p.add_argument("--dry-run", action="store_true")

    qual_p = bench_sub.add_parser("quality", help="Run quality benchmark")
    qual_p.add_argument("--config", required=True, help="Config ID")
    qual_p.add_argument("--tasks", default="all", help="Task IDs, CSV or 'all' (default: all)")
    qual_p.add_argument("--dry-run", action="store_true")

    all_p = bench_sub.add_parser("all", help="Run benchmarks across configs")
    all_p.add_argument("--configs", required=True, help="Comma-separated config IDs")
    all_p.add_argument("--dry-run", action="store_true")

    # ---- sandbox ----
    sandbox_p = sub.add_parser("sandbox", help="Sandbox operations")
    sandbox_sub = sandbox_p.add_subparsers(dest="action")

    create_p = sandbox_sub.add_parser("create", help="Create Docker sandbox")
    create_p.add_argument("--project", required=True, help="Project name")
    create_p.add_argument("--name", help="Container name (auto-generated if omitted)")
    create_p.add_argument("--dry-run", action="store_true")

    exec_p = sandbox_sub.add_parser("exec", help="Execute command in sandbox")
    exec_p.add_argument("--name", required=True, help="Container name")
    exec_p.add_argument("--command", required=True, help="Command to execute")
    exec_p.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")

    collect_p = sandbox_sub.add_parser("collect", help="Collect files from sandbox")
    collect_p.add_argument("--name", required=True, help="Container name")
    collect_p.add_argument("--from", dest="remote_path", required=True, help="Remote path to collect")
    collect_p.add_argument("--to", dest="local_path", help="Local destination path")

    destroy_p = sandbox_sub.add_parser("destroy", help="Destroy sandbox")
    destroy_p.add_argument("--name", required=True, help="Container name")
    destroy_p.add_argument("--dry-run", action="store_true")

    # ---- project ----
    project_p = sub.add_parser("project", help="Project operations")
    project_sub = project_p.add_subparsers(dest="action")

    project_sub.add_parser("status", help="Show all projects on Triton")

    sync_p = project_sub.add_parser("sync", help="Sync local project to Triton")
    sync_p.add_argument("--local", required=True, help="Local project path")
    sync_p.add_argument("--remote", required=True, help="Remote destination path")
    sync_p.add_argument("--dry-run", action="store_true")

    # ---- telemetry ----
    tel_p = sub.add_parser("telemetry", help="Telemetry operations")
    tel_sub = tel_p.add_subparsers(dest="action")

    tel_sub.add_parser("dashboard", help="Show HTML dashboard")
    tel_sub.add_parser("export", help="Export SQLite to JSON")

    # ---- system ----
    sys_p = sub.add_parser("system", help="System operations")
    sys_sub = sys_p.add_subparsers(dest="action")

    sys_sub.add_parser("health", help="Full health check")
    sys_sub.add_parser("gpu", help="GPU status")

    logs_p = sys_sub.add_parser("logs", help="Recent container logs")
    logs_p.add_argument("--lines", type=int, default=50, help="Number of log lines")

    return parser


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def main():
    # Extract --pretty from wherever it appears (before argparse sees it)
    global_pretty = "--pretty" in sys.argv
    sys.argv = [a for a in sys.argv if a != "--pretty"]

    parser = build_parser()
    args = parser.parse_args()

    pretty = global_pretty

    # Set globals from CLI
    global TRITON_HOST, TRITON_USER
    TRITON_HOST = args.host
    TRITON_USER = args.user

    rc = RemoteControl(host=args.host, user=args.user)

    group = args.group
    action = getattr(args, "action", None)

    if group is None:
        parser.print_help()
        sys.exit(0)

    # ---- model ----
    if group == "model":
        if action == "status":
            output(rc.model_status(), pretty=pretty)
        elif action == "list":
            output(rc.model_list(), pretty=pretty)
        elif action == "swap":
            # Allow Docker profile name as shorthand
            config = args.config
            # Map profile name back to config ID if needed
            if config not in CONFIG_TO_PROFILE:
                for cid, prof in CONFIG_TO_PROFILE.items():
                    if prof == config:
                        config = cid
                        break
            output(rc.model_swap(config, dry_run=args.dry_run), pretty=pretty)
        else:
            parser.parse_args(["model", "--help"])

    # ---- bench ----
    elif group == "bench":
        if action == "throughput":
            output(rc.bench_throughput(args.config, reps=args.reps, dry_run=args.dry_run), pretty=pretty)
        elif action == "quality":
            output(rc.bench_quality(args.config, tasks=args.tasks, dry_run=args.dry_run), pretty=pretty)
        elif action == "all":
            configs = [c.strip() for c in args.configs.split(",")]
            output(rc.bench_all(configs, dry_run=args.dry_run), pretty=pretty)
        else:
            parser.parse_args(["bench", "--help"])

    # ---- sandbox ----
    elif group == "sandbox":
        if action == "create":
            output(rc.sandbox_create(args.project, name=args.name, dry_run=args.dry_run), pretty=pretty)
        elif action == "exec":
            output(rc.sandbox_exec(args.name, args.command, timeout=getattr(args, "timeout", 60)), pretty=pretty)
        elif action == "collect":
            output(rc.sandbox_collect(
                args.name,
                args.remote_path,
                local_path=getattr(args, "local_path", None),
            ), pretty=pretty)
        elif action == "destroy":
            output(rc.sandbox_destroy(args.name, dry_run=args.dry_run), pretty=pretty)
        else:
            parser.parse_args(["sandbox", "--help"])

    # ---- project ----
    elif group == "project":
        if action == "status":
            output(rc.project_status(), pretty=pretty)
        elif action == "sync":
            output(rc.project_sync(args.local, args.remote, dry_run=args.dry_run), pretty=pretty)
        else:
            parser.parse_args(["project", "--help"])

    # ---- telemetry ----
    elif group == "telemetry":
        if action == "dashboard":
            html = rc.telemetry_dashboard()
            if pretty:
                print(html)
            else:
                # Save to file and print path
                out_path = Path(__file__).parent / "reports" / "dashboard.html"
                out_path.parent.mkdir(exist_ok=True)
                out_path.write_text(html)
                output({"dashboard_path": str(out_path), "size_bytes": len(html)}, pretty=pretty)
        elif action == "export":
            output(rc.telemetry_export(), pretty=pretty)
        else:
            parser.parse_args(["telemetry", "--help"])

    # ---- system ----
    elif group == "system":
        if action == "health":
            output(rc.system_health(), pretty=pretty)
        elif action == "gpu":
            output(rc.system_gpu(), pretty=pretty)
        elif action == "logs":
            output(rc.system_logs(lines=getattr(args, "lines", 50)), pretty=pretty)
        else:
            parser.parse_args(["system", "--help"])

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
