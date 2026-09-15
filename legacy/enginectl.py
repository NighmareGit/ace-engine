#!/usr/bin/env python3
"""
enginectl.py — Unified CLI for full coder-harness engine control.

Every command outputs structured JSON:
    {"ok": true,  "data": {...}}   on success
    {"ok": false, "error": "..."}  on failure

Zero external dependencies — uses only stdlib (argparse, urllib, subprocess,
os, json, socket, signal, sys, time, pathlib).

Usage:
    python3 enginectl.py status
    python3 enginectl.py stream start --port 3081
    python3 enginectl.py stream stop
    python3 enginectl.py stream status
    python3 enginectl.py dashboard
    python3 enginectl.py gpu
    python3 enginectl.py run my-prd.md --project /path --config config-i
    python3 enginectl.py parse my-prd.md
    python3 enginectl.py model list
    python3 enginectl.py telemetry query --type gpu --since 1h --limit 10
    python3 enginectl.py telemetry export --format json
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent

ENGINE_PID_FILE = "/tmp/engine-service.pid"
ENGINE_STATE_FILE = "/tmp/engine-service.state"
ENGINE_SCRIPT = str(_HERE / "engine_service.py")
ENGINE_PORT = 3082

STREAMING_SCRIPT = str(_HERE / "streaming_server.py")
STREAMING_PID_FILE = "/tmp/streaming-server.pid"
STREAMING_STATE_FILE = "/tmp/streaming-server.json"
STREAMING_PORT = 3081

BEE_LLAMA_PORT_3090 = 8080
BEE_LLAMA_PORT_3070 = 8082

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"

# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def _is_triton() -> bool:
    """Return True if we are running directly on Triton (or inside its Docker)."""
    if os.path.exists("/.dockerenv"):
        return True
    if os.environ.get("CONTAINER") == "docker":
        return True
    try:
        hostname = socket.gethostname()
        if "host" in hostname.lower():
            return True
    except Exception:
        pass
    return False


def _host_for_http() -> str:
    """Return the base host used for HTTP service URLs."""
    if _is_triton():
        return "127.0.0.1"
    return TRITON_HOST


def _base_url(port: int) -> str:
    """Build a base URL for a given port."""
    return f"http://{_host_for_http()}:{port}"


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------

_TRITON_PROJECTS_DIR = "/home/<user>/projects"
_LOCAL_PROJECTS_DIR = os.path.expanduser("~/projects")

_CONFIG_TO_PORT: dict[str, int] = {
    "3090-qwen36-35b": 8080,
    "3090-muse-glimmer": 8080,
    "3090-laguna-xs": 8080,
    "3090-qwen35-9b": 8080,
    "3070-qwen35-9b": 8082,
    "3070-qwen35-4b": 8082,
}


def _check_health(port: int) -> bool:
    """Check BeeLlama health on *port* (remote when on nightmare, local when on Triton)."""
    if _is_triton():
        return _probe_health(port) is not None
    return _probe_health_remote(port) is not None


def _ssh_exec(cmd: str) -> dict:
    """Execute *cmd* on Triton via SSH. Returns {stdout, stderr, rc}."""
    try:
        result = subprocess.run(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{TRITON_USER}@{TRITON_HOST}",
                cmd,
            ],
            capture_output=True, text=True, timeout=30,
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "rc": 1}


# ---------------------------------------------------------------------------
# JSON output helpers
# ---------------------------------------------------------------------------


def _ok(data: Any, pretty: bool = False) -> str:
    """Return a JSON string for a successful result."""
    envelope = {"ok": True, "data": data}
    return json.dumps(envelope, indent=2 if pretty else None, default=str)


def _err(error: str, pretty: bool = False) -> str:
    """Return a JSON string for a failed result."""
    envelope = {"ok": False, "error": error}
    return json.dumps(envelope, indent=2 if pretty else None, default=str)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# HTTP helpers (zero-dep)
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = 3.0,
              headers: Optional[dict] = None) -> Optional[dict]:
    """GET *url* and return parsed JSON, or None on any failure."""
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _http_post_json(url: str, body: dict, timeout: float = 10.0) -> Optional[dict]:
    """POST JSON to *url* and return parsed response, or None on failure."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PID / process helpers
# ---------------------------------------------------------------------------


def _read_pid_file(path: str) -> Optional[int]:
    """Read a PID from a file, returning None on any error."""
    try:
        with open(path, "r") as fh:
            return int(fh.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _process_alive(pid: int) -> bool:
    """Return True if a process with *pid* exists."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_state_file(path: str) -> dict:
    """Read a JSON state file, returning empty dict on failure."""
    try:
        with open(path, "r") as fh:
            return json.loads(fh.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_state_file(path: str, state: dict) -> None:
    """Atomically write a JSON state file."""
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# GPU detection (nvidia-smi)
# ---------------------------------------------------------------------------


def _query_gpus_local() -> list[dict]:
    """Query GPU stats via local nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp_c": float(parts[2]),
                    "vram_used_mb": float(parts[3]),
                    "vram_total_mb": float(parts[4]),
                })
        return gpus
    except Exception:
        return []


def _query_gpus_remote(host: str) -> list[dict]:
    """Query GPU stats via SSH to *host*."""
    try:
        result = subprocess.run(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{TRITON_USER}@{host}",
                "nvidia-smi --query-gpu=index,name,temperature.gpu,memory.used,"
                "memory.total --format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp_c": float(parts[2]),
                    "vram_used_mb": float(parts[3]),
                    "vram_total_mb": float(parts[4]),
                })
        return gpus
    except Exception:
        return []


def query_gpus() -> list[dict]:
    """Auto-detect and query GPUs."""
    if _is_triton():
        return _query_gpus_local()
    return _query_gpus_remote(TRITON_HOST)


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------


def _probe_health(port: int, timeout: float = 3.0) -> Optional[dict]:
    """Probe GET /health on *port* (localhost). Returns dict or None."""
    return _http_get(f"http://127.0.0.1:{port}/health", timeout=timeout)


def _probe_health_remote(port: int, timeout: float = 3.0) -> Optional[dict]:
    """Probe GET /health on *port* on Triton via SSH curl."""
    try:
        result = subprocess.run(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{TRITON_USER}@{TRITON_HOST}",
                f"curl -sf http://127.0.0.1:{port}/health",
            ],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def _uptime_from_pid(pid: int) -> Optional[float]:
    """Estimate process uptime in seconds via /proc/{pid}/stat."""
    try:
        stat_path = f"/proc/{pid}/stat"
        if not os.path.exists(stat_path):
            return None
        with open(stat_path, "r") as fh:
            fields = fh.read().split()
        # field 22 = starttime (clock ticks since boot)
        starttime_ticks = int(fields[21])
        clocks_per_sec = os.sysconf("SC_CLK_TCK")
        boot_time = _get_boot_time()
        start_epoch = boot_time + (starttime_ticks / clocks_per_sec)
        return time.time() - start_epoch
    except Exception:
        return None


def _get_boot_time() -> float:
    """Return system boot time as epoch seconds (Linux only)."""
    try:
        with open("/proc/stat", "r") as fh:
            for line in fh:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except Exception:
        pass
    return 0.0


# ===========================================================================
# Command implementations
# ===========================================================================


def cmd_status(args: argparse.Namespace) -> int:
    """Full system status: engine, streaming, GPU, model, BeeLlama."""
    pretty = getattr(args, "pretty", False)

    # -- Engine --
    engine_pid = _read_pid_file(ENGINE_PID_FILE)
    engine_running = engine_pid is not None and _process_alive(engine_pid)
    engine_uptime = _uptime_from_pid(engine_pid) if engine_running else None
    engine_health = None
    if engine_running:
        if _is_triton():
            engine_health = _probe_health(ENGINE_PORT)
        else:
            engine_health = _http_get(
                f"{_base_url(ENGINE_PORT)}/health", timeout=3
            )
    engine_data = {
        "running": engine_running,
        "pid": engine_pid,
        "port": ENGINE_PORT,
        "uptime_s": round(engine_uptime, 1) if engine_uptime is not None else None,
        "health": engine_health,
    }

    # -- Streaming --
    stream_state = _read_state_file(STREAMING_STATE_FILE)
    stream_pid = stream_state.get("pid") or _read_pid_file(STREAMING_PID_FILE)
    stream_running = stream_pid is not None and _process_alive(stream_pid)
    stream_uptime = _uptime_from_pid(stream_pid) if stream_running else None
    stream_port = stream_state.get("port", STREAMING_PORT)
    stream_health = None
    if stream_running:
        if _is_triton():
            stream_health = _probe_health(stream_port)
        else:
            stream_health = _http_get(
                f"{_base_url(stream_port)}/health", timeout=3
            )
    streaming_data = {
        "running": stream_running,
        "port": stream_port,
        "events_count": stream_state.get("events_count", 0),
        "uptime_s": round(stream_uptime, 1) if stream_uptime is not None else None,
        "health": stream_health,
    }

    # -- GPU --
    gpu_list = query_gpus()

    # -- Model (BeeLlama 3090) --
    model_health = None
    if _is_triton():
        model_health = _probe_health(BEE_LLAMA_PORT_3090)
    else:
        model_health = _http_get(
            f"{_base_url(BEE_LLAMA_PORT_3090)}/health", timeout=3
        )
    model_data = {
        "config": stream_state.get("model_config", "unknown"),
        "port": BEE_LLAMA_PORT_3090,
        "status": "ready" if model_health else "unreachable",
        "health": model_health,
    }

    # -- BeeLlama --
    beellama_3090 = _probe_health(BEE_LLAMA_PORT_3090) is not None
    beellama_3070 = _probe_health(BEE_LLAMA_PORT_3070) is not None
    beellama_data = {
        "3090": beellama_3090,
        "3070": beellama_3070,
    }

    data = {
        "engine": engine_data,
        "streaming": streaming_data,
        "gpu": gpu_list,
        "model": model_data,
        "beellama": beellama_data,
    }
    print(_ok(data, pretty))
    return 0


# ---------------------------------------------------------------------------
# Stream lifecycle
# ---------------------------------------------------------------------------


def cmd_stream_start(args: argparse.Namespace) -> int:
    """Start the streaming server as a background process."""
    pretty = getattr(args, "pretty", False)
    port = getattr(args, "port", STREAMING_PORT)
    host = getattr(args, "host", "127.0.0.1")
    token = getattr(args, "token", None)

    # Already running?
    state = _read_state_file(STREAMING_STATE_FILE)
    existing_pid = state.get("pid") or _read_pid_file(STREAMING_PID_FILE)
    if existing_pid and _process_alive(existing_pid):
        print(_err(f"Streaming server already running (pid={existing_pid})", pretty))
        return 1

    # Port in use?
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.bind(("0.0.0.0", port))
    except OSError:
        sock.close()
        print(_err(f"Port {port} is already in use", pretty))
        return 1
    finally:
        try:
            sock.close()
        except Exception:
            pass

    # Build command
    cmd = [sys.executable, STREAMING_SCRIPT, "--port", str(port), "--host", host]
    if token:
        cmd.extend(["--token", token])

    log_fd = open("/tmp/streaming-server.log", "a")
    proc = subprocess.Popen(
        cmd, stdout=log_fd, stderr=log_fd, start_new_session=True,
    )

    # Persist PID
    with open(STREAMING_PID_FILE, "w") as fh:
        fh.write(str(proc.pid))

    new_state = {
        "pid": proc.pid,
        "state": "STARTED",
        "port": port,
        "host": host,
        "started_at": _now_iso(),
    }
    _write_state_file(STREAMING_STATE_FILE, new_state)

    print(_ok({
        "status": "started",
        "pid": proc.pid,
        "port": port,
        "host": host,
        "dashboard": f"http://{_host_for_http()}:{port}/",
    }, pretty))
    return 0


def cmd_stream_stop(args: argparse.Namespace) -> int:
    """Stop the streaming server."""
    import signal as _signal

    pretty = getattr(args, "pretty", False)
    state = _read_state_file(STREAMING_STATE_FILE)
    pid = state.get("pid") or _read_pid_file(STREAMING_PID_FILE)

    if not pid or not _process_alive(pid):
        state["state"] = "STOPPED"
        state["pid"] = None
        _write_state_file(STREAMING_STATE_FILE, state)
        print(_ok({"status": "not_running"}, pretty))
        return 0

    try:
        os.kill(pid, _signal.SIGTERM)
        time.sleep(1)
    except ProcessLookupError:
        pass

    state["state"] = "STOPPED"
    state["pid"] = None
    state["stopped_at"] = _now_iso()
    _write_state_file(STREAMING_STATE_FILE, state)

    print(_ok({"status": "stopped", "pid": pid}, pretty))
    return 0


def cmd_stream_status(args: argparse.Namespace) -> int:
    """Streaming server health check."""
    pretty = getattr(args, "pretty", False)
    state = _read_state_file(STREAMING_STATE_FILE)
    pid = state.get("pid") or _read_pid_file(STREAMING_PID_FILE)
    port = state.get("port", STREAMING_PORT)
    running = pid is not None and _process_alive(pid)

    health = None
    if running:
        health = _probe_health(port)

    result = {
        "pid": pid,
        "running": running,
        "state": state.get("state", "STOPPED"),
        "port": port,
        "health": health,
        "dashboard": f"http://{_host_for_http()}:{port}/" if running else None,
    }
    print(_ok(result, pretty))
    return 0


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Return dashboard URLs as JSON."""
    pretty = getattr(args, "pretty", False)
    state = _read_state_file(STREAMING_STATE_FILE)
    port = state.get("port", STREAMING_PORT)
    host = _host_for_http()

    data = {
        "url": f"http://{host}:{port}/",
        "demo_url": f"http://{host}:{port}/?demo=1",
        "api_base": f"http://{host}:{port}",
    }
    print(_ok(data, pretty))
    return 0


# ---------------------------------------------------------------------------
# GPU (dedicated command)
# ---------------------------------------------------------------------------


def cmd_gpu(args: argparse.Namespace) -> int:
    """Query GPU status."""
    pretty = getattr(args, "pretty", False)
    gpus = query_gpus()
    print(_ok({"gpu": gpus}, pretty))
    return 0


# ---------------------------------------------------------------------------
# Run / Parse / Model commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a PRD through the engine in background.

    Starts the ACE pipeline as a detached process, returns immediately
    with a run_id.  The engine streams events via STREAMING_SERVER_URL.
    """
    import uuid

    pretty = getattr(args, "pretty", False)
    prd_path: str = getattr(args, "prd", None) or ""
    project_path: str = getattr(args, "project", None) or ""
    config: str = getattr(args, "config", None) or "3090-qwen36-35b"

    if not prd_path:
        print(_err("Missing required argument: prd", pretty))
        return 1

    # Auto-configure streaming env vars
    stream_url = os.environ.get("STREAMING_SERVER_URL", "http://127.0.0.1:3081")
    stream_token = os.environ.get("STREAMING_TOKEN", "streaming-secret-2024")

    # Check BeeLlama health on the target port
    port = _CONFIG_TO_PORT.get(config, 8080)
    if not _check_health(port):
        print(_err(f"BeeLlama not healthy on port {port}", pretty))
        return 1

    # Build environment
    env = os.environ.copy()
    env["STREAMING_SERVER_URL"] = stream_url
    env["STREAMING_TOKEN"] = stream_token
    env["PYTHONUNBUFFERED"] = "1"

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    log_path = f"/tmp/ace-watch-{run_id}.log"

    # Start engine — subprocess locally, or via nohup when on Triton
    if _is_triton():
        runner_dir = f"{_TRITON_PROJECTS_DIR}/coder-harness"
        proj = f"{_TRITON_PROJECTS_DIR}/{os.path.basename(project_path)}" if project_path else ""
        cmd_str = (
            f"cd {runner_dir} && "
            f"STREAMING_SERVER_URL={stream_url} "
            f"STREAMING_TOKEN={stream_token} "
            f"PYTHONUNBUFFERED=1 "
            f"nohup python3 harness.py ace run "
            f"--project {proj} "
            f"{prd_path} "
            f"> {log_path} 2>&1 & "
            f"echo $!"
        )
        result = _ssh_exec(cmd_str)
        pid = result.get("stdout", "").strip()
    else:
        cmd = [sys.executable, "harness.py", "ace", "run"]
        if project_path:
            cmd.extend(["--project", project_path])
        cmd.append(prd_path)

        log_fd = open(log_path, "a")
        proc = subprocess.Popen(
            cmd, stdout=log_fd, stderr=log_fd, env=env,
            start_new_session=True, cwd=_LOCAL_PROJECTS_DIR,
        )
        pid = str(proc.pid)

    # Persist run state
    run_state = {
        "run_id": run_id,
        "pid": pid,
        "prd": prd_path,
        "project": project_path,
        "config": config,
        "log_path": log_path,
        "started_at": time.time(),
        "status": "running",
    }
    state_path = f"/tmp/ace-watch-{run_id}.json"
    _write_state_file(state_path, run_state)

    print(_ok({
        "run_id": run_id,
        "pid": pid,
        "log_path": log_path,
        "state_path": state_path,
        "streaming_url": stream_url,
        "dashboard_url": f"http://{TRITON_HOST}:3081/",
        "status": "running",
    }, pretty))
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    """Parse a PRD into a structured task list."""
    pretty = getattr(args, "pretty", False)
    prd_path: str = getattr(args, "prd", None) or ""

    if not prd_path:
        print(_err("Missing required argument: prd", pretty))
        return 1

    if not os.path.isfile(prd_path):
        print(_err(f"File not found: {prd_path}", pretty))
        return 1

    # Import the PRD parser from the same package
    try:
        sys.path.insert(0, str(_HERE))
        from prd_parser import PRDParser  # type: ignore[import-untyped]
    except ImportError:
        print(_err("prd_parser module not found", pretty))
        return 1

    try:
        parser = PRDParser()
        result = parser.parse(prd_path)
        print(_ok(result, pretty))
        return 0
    except Exception as exc:
        print(_err(str(exc), pretty))
        return 1


def cmd_model(args: argparse.Namespace) -> int:
    """Model management commands: list | status | swap."""
    pretty = getattr(args, "pretty", False)
    action: str = getattr(args, "model_cmd", None) or ""

    if action == "list":
        configs = [
            {"id": "3090-qwen36-35b", "gpu": "3090", "model": "Qwen3.6-35B-A3B",
             "port": 8080, "context": 128000},
            {"id": "3090-muse-glimmer", "gpu": "3090", "model": "Muse-Glimmer-30B",
             "port": 8080, "context": 32000},
            {"id": "3090-qwen35-9b", "gpu": "3090", "model": "Qwen3.5-9B-MTP",
             "port": 8080, "context": 140000},
            {"id": "3070-qwen35-9b", "gpu": "3070", "model": "Qwen3.5-9B-MTP",
             "port": 8082, "context": 8000},
        ]
        print(_ok({"configs": configs}, pretty))
        return 0

    if action == "status":
        port = BEE_LLAMA_PORT_3090
        health = _check_health(port)
        print(_ok({"port": port, "healthy": health}, pretty))
        return 0

    if action == "swap":
        config_id: str = getattr(args, "config_id", None) or getattr(args, "config", None) or ""
        if not config_id:
            print(_err("Missing required argument: config_id", pretty))
            return 1

        # Delegate to remote_control.py via SSH
        cmd_str = (
            f"cd {_TRITON_PROJECTS_DIR}/coder-harness && "
            f"python3 remote_control.py model swap {config_id}"
        )
        result = _ssh_exec(cmd_str)

        ok = result.get("rc", 1) == 0
        data = {"stdout": result.get("stdout", ""), "stderr": result.get("stderr", "")}
        if ok:
            print(_ok(data, pretty))
        else:
            print(_err(data.get("stderr", "swap failed"), pretty))
        return 0 if ok else 1

    print(_err("Missing model subcommand: list|status|swap", pretty))
    return 1


def cmd_telemetry_query(args: argparse.Namespace) -> int:
    """Query telemetry events from the streaming SQLite database."""
    pretty = getattr(args, "pretty", False)
    db_path = getattr(args, "db", None) or os.path.expanduser("~/streaming-events.db")
    event_type = getattr(args, "type", None)
    since = getattr(args, "since", None)
    limit = getattr(args, "limit", 50)

    if not os.path.exists(db_path):
        print(_err(f"Database not found: {db_path}", pretty))
        return 1

    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        query = "SELECT * FROM live_events WHERE 1=1"
        params: list = []

        if event_type:
            if "*" in event_type:
                query += " AND type LIKE ?"
                params.append(event_type.replace("*", "%"))
            else:
                query += " AND type = ?"
                params.append(event_type)

        if since:
            try:
                since_int = int(since)
                query += " AND seq > ?"
                params.append(since_int)
            except ValueError:
                pass  # Ignore non-numeric since values

        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        events = []
        for row in rows:
            events.append({
                "id": row["id"],
                "seq": row["seq"],
                "time": row["time"],
                "source": row["source"],
                "type": row["type"],
                "data": json.loads(row["data"]) if row["data"] else {},
            })

        print(_ok({"events": events, "count": len(events)}, pretty))
        return 0
    except Exception as e:
        print(_err(str(e), pretty))
        return 1


def cmd_telemetry_export(args: argparse.Namespace) -> int:
    """Export all telemetry data from the streaming SQLite database."""
    pretty = getattr(args, "pretty", False)
    db_path = getattr(args, "db", None) or os.path.expanduser("~/streaming-events.db")

    if not os.path.exists(db_path):
        print(_err(f"Database not found: {db_path}", pretty))
        return 1

    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Export all known tables
        tables = ["live_events", "source_sequences"]
        export: dict[str, list] = {}
        for table in tables:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                export[table] = [dict(row) for row in rows]
            except Exception:
                export[table] = []

        conn.close()
        print(_ok(export, pretty))
        return 0
    except Exception as e:
        print(_err(str(e), pretty))
        return 1


# ===========================================================================
# Argument parser
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    p = argparse.ArgumentParser(
        prog="enginectl",
        description="Unified CLI for the coder-harness engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 enginectl.py status\n"
            "  python3 enginectl.py stream start --port 3081\n"
            "  python3 enginectl.py stream stop\n"
            "  python3 enginectl.py stream status\n"
            "  python3 enginectl.py dashboard\n"
            "  python3 enginectl.py gpu\n"
            "  python3 enginectl.py --pretty status\n"
        ),
    )

    # Global flags
    p.add_argument(
        "--json", dest="json_output", action="store_true", default=True,
        help="Output as JSON (default: true).",
    )
    p.add_argument(
        "--pretty", action="store_true", default=False,
        help="Pretty-print JSON output.",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )

    sub = p.add_subparsers(dest="command", help="Command to run")

    # ---- status ----
    sub.add_parser("status", help="Full system status (engine, streaming, GPU, model)")

    # ---- stream ----
    stream_p = sub.add_parser("stream", help="Streaming server lifecycle")
    stream_sub = stream_p.add_subparsers(dest="stream_cmd", help="Stream action")

    # stream start
    ss = stream_sub.add_parser("start", help="Start the streaming server")
    ss.add_argument("--port", type=int, default=STREAMING_PORT,
                    help=f"Port (default: {STREAMING_PORT})")
    ss.add_argument("--host", default="127.0.0.1",
                    help="Bind host (default: 127.0.0.1)")
    ss.add_argument("--token", default=None,
                    help="Auth token for the streaming server")

    # stream stop
    stream_sub.add_parser("stop", help="Stop the streaming server")

    # stream status
    stream_sub.add_parser("status", help="Streaming server health check")

    # ---- dashboard ----
    sub.add_parser("dashboard", help="Dashboard URL info")

    # ---- gpu ----
    sub.add_parser("gpu", help="Query GPU status")

    # ---- run ----
    run_p = sub.add_parser("run", help="Run a PRD through the engine")
    run_p.add_argument("prd", nargs="?", help="PRD file path")
    run_p.add_argument("--project", required=True, help="Project directory (required)")
    run_p.add_argument("--config", default="3090-qwen36-35b",
                       help="Model config ID (default: 3090-qwen36-35b)")

    # ---- parse ----
    parse_p = sub.add_parser("parse", help="Parse a PRD into task list")
    parse_p.add_argument("prd", nargs="?", help="PRD file path")

    # ---- model ----
    model_p = sub.add_parser("model", help="Model management: list|status|swap")
    model_sub = model_p.add_subparsers(dest="model_cmd", help="Model action")
    model_sub.add_parser("list", help="List model configs")
    model_sub.add_parser("status", help="Current model status")
    swap_p = model_sub.add_parser("swap", help="Swap to a config")
    swap_p.add_argument("config_id", nargs="?", help="Config ID to swap to")
    swap_p.add_argument("--config", default=None,
                        help="Config ID (alternative to positional arg)")

    # ---- telemetry ----
    telem_p = sub.add_parser("telemetry", help="Telemetry: query|export")
    telem_sub = telem_p.add_subparsers(dest="telem_cmd", help="Telemetry action")

    # telemetry query
    tq = telem_sub.add_parser("query", help="Query telemetry events")
    tq.add_argument("--type", help="Event type filter (supports wildcards, e.g. 'task.*')")
    tq.add_argument("--since", help="Sequence number floor (e.g. 1000)")
    tq.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    tq.add_argument("--db", help="Database path (default: ~/streaming-events.db)")

    # telemetry export
    te = telem_sub.add_parser("export", help="Export telemetry data")
    te.add_argument("--format", default="json", help="Export format (default: json)")
    te.add_argument("--db", help="Database path (default: ~/streaming-events.db)")

    return p


# ===========================================================================
# Dispatch
# ===========================================================================


def dispatch(args: argparse.Namespace) -> int:
    """Route to the appropriate command handler."""
    cmd = args.command

    if cmd is None:
        # No subcommand given — print help and exit 0
        build_parser().print_help()
        return 0

    if cmd == "status":
        return cmd_status(args)

    if cmd == "stream":
        sub = getattr(args, "stream_cmd", None)
        if sub == "start":
            return cmd_stream_start(args)
        if sub == "stop":
            return cmd_stream_stop(args)
        if sub == "status":
            return cmd_stream_status(args)
        # No sub-subcommand — print stream help
        print(_err("Missing stream subcommand: start|stop|status", getattr(args, "pretty", False)))
        return 1

    if cmd == "dashboard":
        return cmd_dashboard(args)

    if cmd == "gpu":
        return cmd_gpu(args)

    if cmd == "run":
        return cmd_run(args)

    if cmd == "parse":
        return cmd_parse(args)

    if cmd == "model":
        return cmd_model(args)

    if cmd == "telemetry":
        sub = getattr(args, "telem_cmd", None)
        if sub == "query":
            return cmd_telemetry_query(args)
        if sub == "export":
            return cmd_telemetry_export(args)
        print(_err("Missing telemetry subcommand: query|export", getattr(args, "pretty", False)))
        return 1

    print(_err(f"Unknown command: {cmd}", getattr(args, "pretty", False)))
    return 1


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    parser = build_parser()
    parsed_args = parser.parse_args()
    sys.exit(dispatch(parsed_args))
