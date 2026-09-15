#!/usr/bin/env python3
"""
harness.py — Single entry point for the Coder Harness platform.

Routes every subcommand to the existing module that actually owns the logic.
No duplication, no new engines — just a thin router.

Usage:
    python3 harness.py --help
    python3 harness.py status
    python3 harness.py bench throughput -c 3090-qwen36-35b
    python3 harness.py run -p coder-harness -t "Fix bug X" -c 3090-qwen36-35b
    python3 harness.py model swap config-i
    python3 harness.py dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Ensure sibling modules are importable (same directory as this script)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ---------------------------------------------------------------------------
# Lazy imports — only load what the chosen subcommand actually needs.
# This keeps --help instant and avoids SSH/import overhead for unrelated
# commands.
# ---------------------------------------------------------------------------

def _import_remote_control():
    from remote_control import RemoteControl, output as rc_output
    return RemoteControl, rc_output

def _import_work_engine():
    from work_engine import WorkEngine, output as we_output
    return WorkEngine, we_output

def _import_sandbox_manager():
    from sandbox_manager import SandboxManager
    return SandboxManager

def _import_gitea_utils():
    from gitea_utils import GiteaClient
    return GiteaClient

def _import_telemetry():
    from telemetry_dashboard import TelemetryDashboard
    return TelemetryDashboard

def _import_preflight():
    from preflight import preflight_check
    return preflight_check

def _import_orchestrate():
    from orchestrate import BenchmarkOrchestrator
    return BenchmarkOrchestrator

def _import_pilot():
    from pilot import MethodologyPilot
    return MethodologyPilot

def _import_report():
    from report import BenchmarkReporter
    return BenchmarkReporter


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _out(data, pretty=False):
    """Print *data* as JSON.  Pretty-print when *pretty* is True."""
    if pretty:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, default=str))


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ===================================================================
# Command handlers
# ===================================================================

# ---- status --------------------------------------------------------

def cmd_status(args):
    """Full system status: model + GPU + sandboxes + telemetry summary."""
    RC, _ = _import_remote_control()
    rc = RC()
    status = {
        "harness_version": __version__,
        "timestamp": _now_iso(),
        "model_and_gpu": rc.model_status(),
    }
    _out(status, pretty=args.pretty)


# ---- health --------------------------------------------------------

def cmd_health(args):
    """Quick preflight health check (7-point)."""
    try:
        preflight_check = _import_preflight()
        results = preflight_check()
        _out(results, pretty=args.pretty)

        # Exit non-zero if anything failed
        failed = any(r.get("status") == "fail" for r in results.values())
        return 1 if failed else 0
    except Exception as e:
        _out({"error": str(e)}, pretty=getattr(args, "pretty", False))
        return 1


# ---- setup ---------------------------------------------------------

def cmd_setup(args):
    """Print a first-time setup guide."""
    guide = r"""
=== Coder Harness Setup ===

1. Verify SSH connectivity:
   ssh -o StrictHostKeyChecking=no <user>@<LAN_IP> echo OK

2. Check Triton services:
   python3 harness.py health

3. List available models:
   python3 harness.py model list

4. Run a test benchmark:
   python3 harness.py bench throughput -c 3090-qwen35-9b --reps 1

5. Generate dashboard:
   python3 harness.py dashboard

Triton: <LAN_IP> (user: <user>)
Gitea: http://<LAN_IP>:3000
BeeLlama: http://<LAN_IP>:8080 (3090) / :8082 (3070)
"""
    print(guide.strip())
    return 0


# ---- bench throughput ----------------------------------------------

def cmd_bench_throughput(args):
    RC, _ = _import_remote_control()
    rc = RC()
    result = rc.bench_throughput(args.config, reps=args.reps, dry_run=args.dry_run)
    _out(result, pretty=args.pretty)


# ---- bench quality -------------------------------------------------

def cmd_bench_quality(args):
    RC, _ = _import_remote_control()
    rc = RC()
    result = rc.bench_quality(args.config, tasks=args.tasks, dry_run=args.dry_run)
    _out(result, pretty=args.pretty)


# ---- bench all (remote_control) ------------------------------------

def cmd_bench_all(args):
    RC, _ = _import_remote_control()
    rc = RC()
    configs = [c.strip() for c in args.configs.split(",")]
    result = rc.bench_all(configs, dry_run=args.dry_run)
    _out(result, pretty=args.pretty)


# ---- bench orchestrate (full pipeline) -----------------------------

def cmd_bench_orchestrate(args):
    """Run the full orchestrator pipeline (Phase 0 or 1)."""
    BenchmarkOrchestrator = _import_orchestrate()
    sizes = None
    if args.sizes:
        sizes = [int(s) for s in args.sizes.split(",")]

    orch = BenchmarkOrchestrator(resume=args.resume)
    try:
        if args.config:
            orch.single_config_run(args.config, dry_run=args.dry_run)
        else:
            orch.run(phase=args.phase, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 1
    except Exception as exc:
        print(f"\nError: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        orch.cleanup()
    return 0


# ---- bench pilot ---------------------------------------------------

def cmd_bench_pilot(args):
    """Run the methodology pilot (5 tasks × 3 configs × 3 reps)."""
    MethodologyPilot = _import_pilot()
    pilot = MethodologyPilot()

    if args.dry_run:
        pilot.print_plan()
    elif args.report_only:
        pilot.generate_report()
    else:
        pilot.run()
    return 0


# ---- run (work engine) --------------------------------------------

def cmd_run(args):
    """Execute a coding task via the full WorkEngine pipeline."""
    WorkEngine, _ = _import_work_engine()
    engine = WorkEngine(dry_run=args.dry_run)
    result = engine.run(
        project=args.project,
        branch=args.branch,
        config=args.config,
        task=args.task,
        name=args.name,
        max_tokens=args.max_tokens,
    )
    _out(result, pretty=args.pretty)


# ---- sessions (work engine) ---------------------------------------

def cmd_sessions(args):
    """List work sessions."""
    WorkEngine, _ = _import_work_engine()
    engine = WorkEngine()
    result = engine.sessions(status=args.status)
    _out(result, pretty=args.pretty)


# ---- collect (work engine) ----------------------------------------

def cmd_collect(args):
    """Collect results from a completed work session."""
    WorkEngine, _ = _import_work_engine()
    engine = WorkEngine()
    result = engine.collect(name=args.name, local_path=args.to)
    _out(result, pretty=args.pretty)


# ---- model status --------------------------------------------------

def cmd_model_status(args):
    RC, _ = _import_remote_control()
    rc = RC()
    _out(rc.model_status(), pretty=args.pretty)


# ---- model swap ----------------------------------------------------

def cmd_model_swap(args):
    RC, _ = _import_remote_control()
    rc = RC()
    config = args.config
    # Accept Docker profile names as shorthand (e.g. "config-i")
    from remote_control import CONFIG_TO_PROFILE
    if config not in CONFIG_TO_PROFILE:
        for cid, prof in CONFIG_TO_PROFILE.items():
            if prof == config:
                config = cid
                break
    _out(rc.model_swap(config, dry_run=args.dry_run), pretty=args.pretty)


# ---- model list ----------------------------------------------------

def cmd_model_list(args):
    RC, _ = _import_remote_control()
    rc = RC()
    _out(rc.model_list(), pretty=args.pretty)


# ---- sandbox create ------------------------------------------------

def cmd_sandbox_create(args):
    RC, _ = _import_remote_control()
    rc = RC()
    _out(rc.sandbox_create(args.project, name=args.name, dry_run=args.dry_run),
         pretty=args.pretty)


# ---- sandbox list --------------------------------------------------

def cmd_sandbox_list(args):
    SandboxManager = _import_sandbox_manager()
    sm = SandboxManager()
    _out(sm.list_sandboxes(), pretty=args.pretty)


# ---- sandbox destroy -----------------------------------------------

def cmd_sandbox_destroy(args):
    RC, _ = _import_remote_control()
    rc = RC()
    _out(rc.sandbox_destroy(args.name, dry_run=args.dry_run), pretty=args.pretty)


# ---- sandbox cleanup (destroy all) --------------------------------

def cmd_sandbox_cleanup(args):
    RC, _ = _import_remote_control()
    rc = RC()
    # remote_control doesn't have a destroy-all; use SandboxManager
    SandboxManager = _import_sandbox_manager()
    sm = SandboxManager(dry_run=args.dry_run)
    _out(sm.destroy_all(), pretty=args.pretty)


# ---- gitea repos --------------------------------------------------

def cmd_gitea_repos(args):
    GiteaClient = _import_gitea_utils()
    gc = GiteaClient()
    _out(gc.list_repos(), pretty=args.pretty)


# ---- gitea branches ------------------------------------------------

def cmd_gitea_branches(args):
    GiteaClient = _import_gitea_utils()
    gc = GiteaClient()
    _out(gc.list_branches(args.repo), pretty=args.pretty)


# ---- gitea files ---------------------------------------------------

def cmd_gitea_files(args):
    GiteaClient = _import_gitea_utils()
    gc = GiteaClient()
    path = args.path or ""
    _out(gc.list_files(args.repo, path), pretty=args.pretty)


# ---- dashboard -----------------------------------------------------

def cmd_dashboard(args):
    TelemetryDashboard = _import_telemetry()
    td = TelemetryDashboard()
    out_path = args.output or str(_HERE / "reports" / "telemetry-dashboard.html")
    result_path = td.generate(out_path)
    result = {
        "dashboard_path": result_path,
        "timestamp": _now_iso(),
        "size_bytes": os.path.getsize(result_path) if os.path.exists(result_path) else 0,
    }
    _out(result, pretty=args.pretty)


# ---- telemetry export ----------------------------------------------

def cmd_telemetry_export(args):
    """Export telemetry data to JSON."""
    RC, _ = _import_remote_control()
    rc = RC()
    _out(rc.telemetry_export(), pretty=args.pretty)


# ---- report --------------------------------------------------------

def cmd_report(args):
    BenchmarkReporter = _import_report()
    reporter = BenchmarkReporter()
    fmt = args.format
    reporter.generate_full_report(fmt=fmt)
    result = {
        "format": fmt,
        "timestamp": _now_iso(),
        "output_dir": str(_HERE / "reports"),
    }
    _out(result, pretty=args.pretty)


# ---- engine start ----------------------------------------------------

ENGINE_PID_FILE = "/tmp/engine-service.pid"
ENGINE_STATE_FILE = "/tmp/engine-service.state"
ENGINE_SCRIPT = str(_HERE / "engine_service.py")

# Valid engine states
ENGINE_STATES = ("STOPPED", "STARTING", "READY", "PROCESSING", "DEGRADED", "FAILED")


def _engine_read_state() -> dict:
    """Read engine state from the state file."""
    state = {"state": "STOPPED", "consecutive_errors": 0, "pid": None}
    try:
        with open(ENGINE_STATE_FILE, "r") as f:
            data = json.loads(f.read())
            state.update(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # Also check PID file
    try:
        with open(ENGINE_PID_FILE, "r") as f:
            state["pid"] = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass
    return state


def _engine_write_state(state: dict):
    """Write engine state to the state file."""
    with open(ENGINE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _engine_is_running(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_engine_start(args):
    """Start the engine service as a background process."""
    import signal
    import subprocess as sp

    state = _engine_read_state()

    # Check if already running
    pid = state.get("pid")
    if pid and _engine_is_running(pid):
        _out({"error": "Engine service already running", "pid": pid},
             pretty=args.pretty)
        return 1

    # Check if port is in use
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.bind(("0.0.0.0", args.port))
        sock.close()
    except OSError:
        _out({"error": f"Port {args.port} is already in use"},
             pretty=args.pretty)
        return 1

    # Set up environment
    env = os.environ.copy()
    if args.token:
        env["ENGINE_TOKEN"] = args.token

    # Write state: STARTING
    state["state"] = "STARTING"
    state["consecutive_errors"] = 0
    _engine_write_state(state)

    # Launch engine_service.py as background process
    cmd = [sys.executable, ENGINE_SCRIPT, "--port", str(args.port)]
    log_fd = open("/tmp/engine-service.log", "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fd,
        stderr=log_fd,
        env=env,
        start_new_session=True,
    )

    # Write PID
    with open(ENGINE_PID_FILE, "w") as f:
        f.write(str(proc.pid))

    state["pid"] = proc.pid
    state["state"] = "READY"
    state["started_at"] = _now_iso()
    _engine_write_state(state)

    _out({
        "status": "started",
        "pid": proc.pid,
        "port": args.port,
        "log": "/tmp/engine-service.log",
    }, pretty=args.pretty)
    return 0


# ---- engine stop -----------------------------------------------------

def cmd_engine_stop(args):
    """Stop the engine service via SIGTERM."""
    import signal

    state = _engine_read_state()
    pid = state.get("pid")

    if not pid:
        _out({"error": "No engine service PID found", "status": "STOPPED"},
             pretty=args.pretty)
        return 0

    if not _engine_is_running(pid):
        _out({"status": "already_stopped", "pid": pid},
             pretty=args.pretty)
        state["state"] = "STOPPED"
        state["pid"] = None
        _engine_write_state(state)
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    state["state"] = "STOPPED"
    state["pid"] = None
    _engine_write_state(state)

    _out({"status": "stopped", "pid": pid}, pretty=args.pretty)
    return 0


# ---- engine status ---------------------------------------------------

def cmd_engine_status(args):
    """Check engine service health via GET /health."""
    state = _engine_read_state()
    pid = state.get("pid")

    # Check if process is alive
    running = pid and _engine_is_running(pid)

    # Probe health endpoint
    health = None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{3082}/health",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass

    result = {
        "pid": pid,
        "running": running,
        "state": state.get("state", "STOPPED"),
        "consecutive_errors": state.get("consecutive_errors", 0),
        "health": health,
    }

    _out(result, pretty=args.pretty)
    return 0


# ===================================================================
# Streaming service lifecycle
# ===================================================================

STREAMING_SCRIPT = os.path.join(os.path.dirname(__file__), "streaming_server.py")
STREAMING_PID_FILE = "/tmp/streaming-server.pid"
STREAMING_STATE_FILE = "/tmp/streaming-server.json"


def _stream_read_state() -> dict:
    try:
        with open(STREAMING_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "STOPPED"}


def _stream_write_state(state: dict):
    with open(STREAMING_STATE_FILE, "w") as f:
        json.dump(state, f)


def _stream_is_running(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    import signal
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_stream_start(args):
    """Start the streaming server as a background process."""
    import subprocess as sp

    state = _stream_read_state()
    pid = state.get("pid")

    if pid and _stream_is_running(pid):
        _out({"error": "Streaming server already running", "pid": pid},
             pretty=args.pretty)
        return 1

    # Check port
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.bind(("0.0.0.0", args.port))
        sock.close()
    except OSError:
        _out({"error": f"Port {args.port} is already in use"},
             pretty=args.pretty)
        return 1

    # Build command
    cmd = [sys.executable, STREAMING_SCRIPT, "--port", str(args.port),
           "--host", args.host]
    if args.token:
        cmd.extend(["--token", args.token])
    if args.db:
        cmd.extend(["--db", args.db])

    log_fd = open("/tmp/streaming-server.log", "a")
    proc = subprocess.Popen(
        cmd, stdout=log_fd, stderr=log_fd, start_new_session=True,
    )

    with open(STREAMING_PID_FILE, "w") as f:
        f.write(str(proc.pid))

    state = {"pid": proc.pid, "state": "STARTED", "port": args.port,
             "started_at": _now_iso()}
    _stream_write_state(state)

    _out({"status": "started", "pid": proc.pid, "port": args.port,
          "dashboard": f"http://127.0.0.1:{args.port}/"},
         pretty=args.pretty)
    return 0


def cmd_stream_stop(args):
    """Stop the streaming server."""
    state = _stream_read_state()
    pid = state.get("pid")

    if not pid or not _stream_is_running(pid):
        _out({"status": "not_running"}, pretty=args.pretty)
        return 0

    import signal
    os.kill(pid, signal.SIGTERM)
    import time
    time.sleep(1)

    state["state"] = "STOPPED"
    state["stopped_at"] = _now_iso()
    _stream_write_state(state)

    _out({"status": "stopped", "pid": pid}, pretty=args.pretty)
    return 0


def cmd_stream_status(args):
    """Check streaming server health."""
    state = _stream_read_state()
    pid = state.get("pid")
    port = state.get("port", 3081)

    running = pid and _stream_is_running(pid)

    health = None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass

    result = {
        "pid": pid,
        "running": running,
        "state": state.get("state", "STOPPED"),
        "port": port,
        "health": health,
        "dashboard": f"http://127.0.0.1:{port}/" if running else None,
    }
    _out(result, pretty=args.pretty)
    return 0


def cmd_stream_dashboard(args):
    """Print the dashboard URL."""
    state = _stream_read_state()
    port = state.get("port", 3081)
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard: {url}")
    print(f"Demo mode: {url}?demo=1")
    return 0


# ===================================================================
# Argument parser
# ===================================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog="harness.py",
        description="Coder Harness — Remote coding engine for Triton dual-GPU",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 harness.py status                      # System health
  python3 harness.py health                      # Quick 7-point check
  python3 harness.py setup                       # First-time guide
  python3 harness.py bench throughput -c 3090-qwen36-35b
  python3 harness.py bench quality -c 3090-qwen36-35b
  python3 harness.py bench pilot                 # Methodology pilot
  python3 harness.py bench orchestrate --phase 1 # Full pipeline
  python3 harness.py run -p coder-harness -t "Fix bug X" -c 3090-qwen36-35b
  python3 harness.py model swap config-i
  python3 harness.py sandbox create -p coder-harness -n test-01
  python3 harness.py gitea repos
  python3 harness.py dashboard
  python3 harness.py report --format json
        """,
    )

    # Global flags
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print JSON output.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging.")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command", help="Command to run")

    # -----------------------------------------------------------------
    # System
    # -----------------------------------------------------------------
    sub.add_parser("status", help="Full system status (model, GPU, sandboxes)")
    sub.add_parser("health", help="Quick 7-point preflight health check")
    sub.add_parser("setup",  help="Print first-time setup guide")

    # -----------------------------------------------------------------
    # Bench
    # -----------------------------------------------------------------
    bench_p = sub.add_parser("bench", help="Run benchmarks")
    bench_sub = bench_p.add_subparsers(dest="bench_cmd", help="Benchmark type")

    # bench throughput
    bt = bench_sub.add_parser("throughput", help="Throughput benchmark (tok/s)")
    bt.add_argument("-c", "--config", required=True,
                    help="Model config ID (e.g. 3090-qwen36-35b)")
    bt.add_argument("--reps", type=int, default=3,
                    help="Repetitions per task (default: 3)")
    bt.add_argument("--dry-run", action="store_true",
                    help="Print plan without executing")

    # bench quality
    bq = bench_sub.add_parser("quality", help="Quality benchmark (scoring)")
    bq.add_argument("-c", "--config", required=True,
                    help="Model config ID")
    bq.add_argument("--tasks", default="all",
                    help="Comma-separated task IDs (default: all)")
    bq.add_argument("--dry-run", action="store_true",
                    help="Print plan without executing")

    # bench all
    ba = bench_sub.add_parser("all", help="Run benchmarks across multiple configs")
    ba.add_argument("--configs", required=True,
                    help="Comma-separated config IDs")
    ba.add_argument("--dry-run", action="store_true",
                    help="Print plan without executing")

    # bench orchestrate (full pipeline)
    bo = bench_sub.add_parser("orchestrate",
                              help="Full orchestrator pipeline (Phase 0 or 1)")
    bo.add_argument("--phase", type=int, choices=[0, 1], default=1,
                    help="Phase to run (0=GPU fit, 1=full eval)")
    bo.add_argument("--config",
                    help="Single config ID (skips other configs)")
    bo.add_argument("--resume", action="store_true",
                    help="Resume from last checkpoint")
    bo.add_argument("--sizes",
                    help="Context sizes for GPU fit (comma-separated)")
    bo.add_argument("--dry-run", action="store_true",
                    help="Print plan without executing")

    # bench pilot
    bp = bench_sub.add_parser("pilot",
                              help="Methodology pilot (5 tasks × 3 configs × 3 reps)")
    bp.add_argument("--dry-run", action="store_true",
                    help="Print plan without executing")
    bp.add_argument("--report", dest="report_only", action="store_true",
                    help="Regenerate report from existing data")

    # -----------------------------------------------------------------
    # Run (work engine)
    # -----------------------------------------------------------------
    run_p = sub.add_parser("run", help="Execute a coding task (full pipeline)")
    run_p.add_argument("-p", "--project", required=True,
                       help="Project name (under ~/projects/ on Triton)")
    run_p.add_argument("-t", "--task", required=True,
                       help="Task prompt for the model")
    run_p.add_argument("-c", "--config", default="3090-qwen36-35b",
                       help="Model config ID (default: 3090-qwen36-35b)")
    run_p.add_argument("--branch", default="main",
                       help="Git branch (default: main)")
    run_p.add_argument("--name", default=None,
                       help="Session name (auto-generated if omitted)")
    run_p.add_argument("--max-tokens", type=int, default=2048,
                       help="Max tokens for inference (default: 2048)")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Print plan without executing")

    # -----------------------------------------------------------------
    # Sessions
    # -----------------------------------------------------------------
    sess_p = sub.add_parser("sessions", help="List work sessions")
    sess_p.add_argument("--status", default=None,
                        help="Filter by status (created, running, completed, "
                             "failed, destroyed)")

    # -----------------------------------------------------------------
    # Collect
    # -----------------------------------------------------------------
    coll_p = sub.add_parser("collect", help="Collect results from a work session")
    coll_p.add_argument("--name", required=True,
                        help="Session name")
    coll_p.add_argument("--to", dest="to", default=None,
                        help="Local destination path")

    # -----------------------------------------------------------------
    # Model
    # -----------------------------------------------------------------
    model_p = sub.add_parser("model", help="Model management")
    model_sub = model_p.add_subparsers(dest="model_cmd", help="Model operation")

    model_sub.add_parser("status", help="Current model + GPU status")

    ms = model_sub.add_parser("swap", help="Swap to a different model config")
    ms.add_argument("config",
                    help="Config ID or Docker profile name (e.g. config-i)")
    ms.add_argument("--dry-run", action="store_true",
                    help="Print plan without executing")

    model_sub.add_parser("list", help="List all available model configs")

    # -----------------------------------------------------------------
    # Sandbox
    # -----------------------------------------------------------------
    sbx_p = sub.add_parser("sandbox", help="Sandbox management")
    sbx_sub = sbx_p.add_subparsers(dest="sandbox_cmd", help="Sandbox operation")

    sbx_create = sbx_sub.add_parser("create", help="Create a Docker sandbox")
    sbx_create.add_argument("-p", "--project", required=True,
                            help="Project name")
    sbx_create.add_argument("-n", "--name", default=None,
                            help="Sandbox name (auto-generated if omitted)")
    sbx_create.add_argument("--dry-run", action="store_true",
                            help="Print plan without executing")

    sbx_sub.add_parser("list", help="List active harness sandboxes")

    sbx_destroy = sbx_sub.add_parser("destroy", help="Destroy a sandbox")
    sbx_destroy.add_argument("-n", "--name", required=True,
                             help="Sandbox name")
    sbx_destroy.add_argument("--dry-run", action="store_true",
                             help="Print plan without executing")

    sbx_sub.add_parser("cleanup", help="Destroy all harness sandboxes")

    # -----------------------------------------------------------------
    # Gitea
    # -----------------------------------------------------------------
    gitea_p = sub.add_parser("gitea", help="Gitea repository operations")
    gitea_sub = gitea_p.add_subparsers(dest="gitea_cmd", help="Gitea operation")

    gitea_sub.add_parser("repos", help="List all repositories")

    gb = gitea_sub.add_parser("branches", help="List branches in a repo")
    gb.add_argument("repo", help="Repository name")

    gf = gitea_sub.add_parser("files", help="List files in a repo path")
    gf.add_argument("repo", help="Repository name")
    gf.add_argument("--path", default="",
                    help="Directory path within the repo (default: root)")

    # -----------------------------------------------------------------
    # Dashboard / Telemetry
    # -----------------------------------------------------------------
    sub.add_parser("dashboard", help="Generate HTML telemetry dashboard")

    tel_p = sub.add_parser("telemetry", help="Telemetry operations")
    tel_sub = tel_p.add_subparsers(dest="telemetry_cmd",
                                   help="Telemetry operation")
    tel_sub.add_parser("export", help="Export telemetry to JSON")

    # -----------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------
    rpt = sub.add_parser("report", help="Generate benchmark report")
    rpt.add_argument("--format", choices=["md", "json"], default="md",
                     help="Output format (default: md)")

    # -----------------------------------------------------------------
    # ACE — Autonomous Coding Engine
    # -----------------------------------------------------------------
    ace_p = sub.add_parser("ace", help="Autonomous Coding Engine commands")
    ace_sub = ace_p.add_subparsers(dest="ace_cmd", help="ACE operation")

    ace_parse = ace_sub.add_parser("parse", help="Parse PRD into tasks")
    ace_parse.add_argument("prd_file", help="Path to markdown PRD file")
    ace_parse.add_argument("-o", "--output", help="Output JSON file")

    ace_gen = ace_sub.add_parser("generate", help="Generate code for a task")
    ace_gen.add_argument("--task", help="Task JSON string")
    ace_gen.add_argument("--task-file", help="Task JSON file")
    ace_gen.add_argument("--project", required=True, help="Project path on Triton")
    ace_gen.add_argument("--max-retries", type=int, default=3)

    ace_test = ace_sub.add_parser("test", help="Run tests on project")
    ace_test.add_argument("project", help="Project path on Triton")
    ace_test.add_argument("--pattern", default="test_*.py")

    ace_lint = ace_sub.add_parser("lint", help="Run quality checks")
    ace_lint.add_argument("project", help="Project path on Triton")

    ace_commit = ace_sub.add_parser("commit", help="Commit changes to Gitea")
    ace_commit.add_argument("project", help="Project path on Triton")
    ace_commit.add_argument("-m", "--message", required=True, help="Commit message")

    ace_pr = ace_sub.add_parser("pr", help="Create PR on Gitea")
    ace_pr.add_argument("--repo", required=True, help="Gitea repo (owner/name)")
    ace_pr.add_argument("--title", required=True)
    ace_pr.add_argument("--body", required=True)
    ace_pr.add_argument("--head", required=True, help="Head branch")

    ace_run = ace_sub.add_parser("run", help="Execute full pipeline on PRD")
    ace_run.add_argument("prd_file", help="Path to markdown PRD")
    ace_run.add_argument("--project", required=True, help="Project path on Triton")
    ace_run.add_argument("--manifest", help="Path to PROMPTS_MANIFEST.json (pre-compiled by orchestrator)")
    ace_run.add_argument("--compile", action="store_true",
                         help="Generate PROMPTS_MANIFEST.json from PRD before running")

    ace_compile = ace_sub.add_parser("compile",
                                      help="Generate PROMPTS_MANIFEST.json from PRD (for orchestrator)")
    ace_compile.add_argument("prd_file", help="Path to markdown PRD")
    ace_compile.add_argument("-o", "--output", default="PROMPTS_MANIFEST.json",
                             help="Output manifest path")
    ace_compile.add_argument("--project", help="Project path for context")

    ace_sub.add_parser("status", help="Show autonomous engine status")

    # -----------------------------------------------------------------
    # Validate / Test
    # -----------------------------------------------------------------
    sub.add_parser("validate", help="Run end-to-end validation")
    sub.add_parser("test", help="Run test suite")

    # -----------------------------------------------------------------
    # Engine service lifecycle
    # -----------------------------------------------------------------
    engine_p = sub.add_parser("engine", help="Engine service lifecycle management")
    engine_sub = engine_p.add_subparsers(dest="engine_cmd", help="Engine operation")

    engine_start = engine_sub.add_parser("start", help="Start the engine service")
    engine_start.add_argument("--port", type=int, default=3082,
                              help="Port for the engine service (default: 3082)")
    engine_start.add_argument("--token", default=None,
                              help="Engine service token (sent as 'Authorization: Token <secret>' — NOT Bearer; default: ENGINE_TOKEN env var)")

    engine_sub.add_parser("stop", help="Stop the engine service")
    engine_sub.add_parser("status", help="Check engine service status")

    # Streaming service lifecycle
    stream_p = sub.add_parser("stream", help="Live streaming service lifecycle")
    stream_sub = stream_p.add_subparsers(dest="stream_cmd", help="Streaming operation")

    stream_start = stream_sub.add_parser("start", help="Start the streaming server")
    stream_start.add_argument("--port", type=int, default=3081,
                              help="Port for the streaming server (default: 3081)")
    stream_start.add_argument("--host", default="127.0.0.1",
                              help="Host to bind (default: 127.0.0.0.1)")
    stream_start.add_argument("--token", default=None,
                              help="bearer <token> for event ingestion")
    stream_start.add_argument("--db", default=None,
                              help="SQLite database path")

    stream_sub.add_parser("stop", help="Stop the streaming server")
    stream_sub.add_parser("status", help="Check streaming server status")
    stream_sub.add_parser("dashboard", help="Open the live dashboard URL")

    return p


# ===================================================================
# Main dispatcher
# ===================================================================

def main():
    parser = build_parser()
    args = parser.parse_args()

    # Activate verbose logging if requested
    if getattr(args, "verbose", False):
        import logging
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        # Suppress noisy INFO logs from sibling modules — show only warnings+
        import logging
        for name in ("remote_control", "work_engine", "sandbox_manager",
                      "gitea_utils", "telemetry_dashboard"):
            logging.getLogger(name).setLevel(logging.WARNING)

    # Ensure telemetry schema is up to date before any command
    try:
        from schema_unified import ensure_schema
        ensure_schema()
    except ImportError:
        pass  # schema_unified not yet created
    except Exception:
        pass  # DB not accessible, continue anyway

    if args.command is None:
        parser.print_help()
        return 0

    # -----------------------------------------------------------------
    # Route to the appropriate handler
    # -----------------------------------------------------------------

    # System commands
    if args.command == "status":
        return cmd_status(args)
    elif args.command == "health":
        return cmd_health(args)
    elif args.command == "setup":
        return cmd_setup(args)

    # Bench commands
    elif args.command == "bench":
        if args.bench_cmd == "throughput":
            return cmd_bench_throughput(args)
        elif args.bench_cmd == "quality":
            return cmd_bench_quality(args)
        elif args.bench_cmd == "all":
            return cmd_bench_all(args)
        elif args.bench_cmd == "orchestrate":
            return cmd_bench_orchestrate(args)
        elif args.bench_cmd == "pilot":
            return cmd_bench_pilot(args)
        else:
            parser.parse_args(["bench", "--help"])
            return 0

    # Work engine
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "sessions":
        return cmd_sessions(args)
    elif args.command == "collect":
        return cmd_collect(args)

    # Model management
    elif args.command == "model":
        if args.model_cmd == "status":
            return cmd_model_status(args)
        elif args.model_cmd == "swap":
            return cmd_model_swap(args)
        elif args.model_cmd == "list":
            return cmd_model_list(args)
        else:
            parser.parse_args(["model", "--help"])
            return 0

    # Sandbox management
    elif args.command == "sandbox":
        if args.sandbox_cmd == "create":
            return cmd_sandbox_create(args)
        elif args.sandbox_cmd == "list":
            return cmd_sandbox_list(args)
        elif args.sandbox_cmd == "destroy":
            return cmd_sandbox_destroy(args)
        elif args.sandbox_cmd == "cleanup":
            return cmd_sandbox_cleanup(args)
        else:
            parser.parse_args(["sandbox", "--help"])
            return 0

    # Gitea
    elif args.command == "gitea":
        if args.gitea_cmd == "repos":
            return cmd_gitea_repos(args)
        elif args.gitea_cmd == "branches":
            return cmd_gitea_branches(args)
        elif args.gitea_cmd == "files":
            return cmd_gitea_files(args)
        else:
            parser.parse_args(["gitea", "--help"])
            return 0

    # Telemetry / dashboard
    elif args.command == "dashboard":
        return cmd_dashboard(args)
    elif args.command == "telemetry":
        if args.telemetry_cmd == "export":
            return cmd_telemetry_export(args)
        else:
            parser.parse_args(["telemetry", "--help"])
            return 0

    # Reports
    elif args.command == "report":
        return cmd_report(args)

    # Engine service lifecycle
    elif args.command == "engine":
        if args.engine_cmd == "start":
            return cmd_engine_start(args)
        elif args.engine_cmd == "stop":
            return cmd_engine_stop(args)
        elif args.engine_cmd == "status":
            return cmd_engine_status(args)
        else:
            parser.parse_args(["engine", "--help"])
            return 0

    # Streaming service lifecycle
    elif args.command == "stream":
        if args.stream_cmd == "start":
            return cmd_stream_start(args)
        elif args.stream_cmd == "stop":
            return cmd_stream_stop(args)
        elif args.stream_cmd == "status":
            return cmd_stream_status(args)
        elif args.stream_cmd == "dashboard":
            return cmd_stream_dashboard(args)
        else:
            parser.parse_args(["stream", "--help"])
            return 0

    # ACE — Autonomous Coding Engine
    elif args.command == "ace":
        if args.ace_cmd == "parse":
            from prd_parser import PRDParser
            parser_mod = PRDParser()
            result = parser_mod.parse(args.prd_file)
            if args.output:
                Path(args.output).write_text(json.dumps(result, indent=2))
                print(f"Tasks written to {args.output}")
            else:
                print(json.dumps(result, indent=2))
            return 0

        elif args.ace_cmd == "generate":
            from code_generator import CodeGenerator
            gen = CodeGenerator()
            if args.task:
                task = json.loads(args.task)
            elif args.task_file:
                task = json.loads(Path(args.task_file).read_text())
            else:
                print("Error: provide --task or --task-file", file=sys.stderr)
                return 1
            result = gen.generate(task, project_path=args.project)
            print(json.dumps(result, indent=2, default=str))
            return 0

        elif args.ace_cmd == "test":
            from test_runner import TestRunner
            runner = TestRunner()
            result = runner.run_tests(args.project, test_pattern=args.pattern)
            print(json.dumps(result, indent=2, default=str))
            return 0

        elif args.ace_cmd == "lint":
            from quality_gates import QualityGates
            gates = QualityGates()
            result = gates.check_all(args.project)
            print(json.dumps(result, indent=2, default=str))
            return 0

        elif args.ace_cmd == "commit":
            from git_workflow import GitWorkflow
            gw = GitWorkflow()
            result = gw.commit_all(args.project, args.message)
            print(json.dumps(result, indent=2, default=str))
            return 0

        elif args.ace_cmd == "pr":
            from git_workflow import GitWorkflow
            gw = GitWorkflow()
            result = gw.create_pr(args.repo, args.title, args.body, args.head)
            print(json.dumps(result, indent=2, default=str))
            return 0

        elif args.ace_cmd == "run":
            from task_queue import TaskQueue
            tq = TaskQueue(project_path=args.project)

            # If --manifest provided, inject it into the task queue
            manifest_path = getattr(args, "manifest", None)
            if manifest_path and os.path.isfile(manifest_path):
                tq.manifest_path = manifest_path
                if getattr(args, "verbose", False):
                    print(f"  Using manifest: {manifest_path}")

            # If --compile, generate manifest first
            if getattr(args, "compile", False):
                try:
                    from prompt_compiler import ManifestGenerator
                    prd_result = tq.load_prd(args.prd_file)
                    gen = ManifestGenerator()
                    manifest = gen.generate(prd_result)
                    out_path = manifest_path or "PROMPTS_MANIFEST.json"
                    with open(out_path, "w") as f:
                        json.dump(manifest, f, indent=2)
                    tq.manifest_path = out_path
                    print(f"  Manifest generated: {out_path}")
                except ImportError:
                    print("  prompt_compiler not available, running without manifest")
                except Exception as e:
                    print(f"  Manifest generation failed: {e}, running without manifest")

            result = tq.execute_all()
            print(json.dumps(result, indent=2, default=str))
            return 0

        elif args.ace_cmd == "compile":
            from prompt_compiler import ManifestGenerator
            from prd_parser import PRDParser
            parser_mod = PRDParser()
            prd_result = parser_mod.parse(args.prd_file)
            gen = ManifestGenerator()
            manifest = gen.generate(prd_result)
            out_path = args.output
            with open(out_path, "w") as f:
                json.dump(manifest, f, indent=2)
            print(f"Manifest written to {out_path}")
            print(f"  Tasks: {len(manifest.get('tasks', {}))}")
            return 0

        elif args.ace_cmd == "status":
            from context_manager import ContextManager
            cm = ContextManager()
            result = cm.get_project_summary()
            print(json.dumps(result, indent=2, default=str))
            return 0

        else:
            parser.parse_args(["ace", "--help"])
            return 0

    # Validate
    elif args.command == "validate":
        try:
            from validate_e2e import run_checks, ValidationResult
            results = run_checks()
            for r in results:
                status = "✅" if r.passed else "❌"
                print(f"{status} {r.name}")
            passed = sum(1 for r in results if r.passed)
            print(f"\nResult: {passed}/{len(results)} PASSED")
            return 0 if passed == len(results) else 1
        except ImportError:
            print("validate_e2e.py not found")
            return 1
        except Exception as e:
            print(f"Validation failed: {e}")
            return 1

    # Test
    elif args.command == "test":
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "test_integration", "-v"],
            capture_output=True, text=True, timeout=120,
            cwd=str(_HERE),
        )
        print(r.stdout)
        if r.returncode != 0:
            print(r.stderr)
        return r.returncode

    else:
        parser.print_help()
        return 0


# ===================================================================

if __name__ == "__main__":
    try:
        rc = main()
        sys.exit(rc or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
