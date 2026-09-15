#!/usr/bin/env python3
"""Simplified work engine — does the same thing as work_engine.py in ~300 lines.

Pipeline: swap → infer → push
No Docker sandboxes (unnecessary overhead for curl-based inference).

═══════════════════════════════════════════════════════════════════════
DIFFERENCES vs work_engine.py (1477 lines → ~300 lines)
═══════════════════════════════════════════════════════════════════════

What was REMOVED (and why):

1. Docker sandbox creation/destruction (Steps 2, 3, 7)
   - Created a ubuntu:24.04 container, installed apt packages (~60s overhead)
   - Volume-mounted the project into the container
   - Then ran curl FROM the container to hit BeeLlama on the host network
   - BeeLlama already listens on host networking — curl from Triton directly
   - This was the single biggest source of latency for zero benefit

2. TelemetryDB class (110 lines → 30 lines of inline functions)
   - Original: full class with WAL mode, Row factory, schema migration
   - Simplified: connect, execute, close — same tables, same schema
   - The original's abstraction bought nothing; SQLite is already the abstraction

3. WorkEngine class (990 lines → flat functions)
   - Original: class with self.host, self.user, self.dry_run, self.compose_dir,
     self.gitea_url, self.gitea_token, self.db instance
   - Simplified: module-level constants, plain functions
   - No state to manage — each call is stateless

4. bench() command (90 lines)
   - Ran shell scripts (bench-throughput.sh, bench-quality.sh) on Triton
   - These scripts already exist and can be called directly
   - Added no logic beyond "SSH the script path"

5. collect() command (50 lines)
   - SCP'd results JSON from Triton to local
   - The simplified version prints results as JSON to stdout
   - Pipe to jq if you need it locally

6. cleanup() command (50 lines)
   - Listed and destroyed all harness-* Docker containers
   - No containers = no cleanup needed

7. status() command (100 lines)
   - Checked model health, GPU status, sandboxes, Gitea, telemetry
   - Equivalent: run `python3 preflight.py` + `nvidia-smi`

8. _ssh_json() helper (10 lines)
   - Wrapped ssh_run + json.loads in a method
   - Not worth a method; inline the 2 lines where needed

9. _compose() helper (5 lines)
   - cd to compose dir + run docker compose
   - Only used in _swap_model; inline it there

10. SCP transport (scp_get, 30 lines)
    - Used only by collect(), which is removed

What was KEPT (same behavior):

1. ssh_run() — SSH to Triton, same pattern
2. _check_health() — curl /health endpoint
3. _swap_model() — state machine → docker compose fallback → health poll
4. _run_inference() — write payload to temp file, curl BeeLlama, parse response
5. _push_results() — git branch/commit/push to Gitea
6. record_session() / record_event() — same telemetry tables
7. CLI with run, sessions, status subcommands
8. All constants (TRITON_HOST, CONFIG_TO_PROFILE, CONFIG_TO_PORT, etc.)

═══════════════════════════════════════════════════════════════════════
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
import time
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(format=LOG_FMT, level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("work_engine")

# ---------------------------------------------------------------------------
# Constants (same as work_engine.py)
# ---------------------------------------------------------------------------

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"
COMPOSE_DIR = "~/dockers/beellama-benchmark"
BEE_LLAMA_PORT_3090 = 8080
BEE_LLAMA_PORT_3070 = 8082
GITEA_URL = "http://localhost:3000"
GITEA_TOKEN = "<GITEA_TOKEN>"
TELEMETRY_DB = os.path.expanduser("~/coder-harness-telemetry.db")
DEFAULT_PROJECT_PATH = "/home/<user>/projects"

CONFIG_TO_PROFILE = {
    "3090-qwen36-35b":   "config-i",
    "3090-muse-glimmer":  "config-h",
    "3090-laguna-xs":     "config-g-laguna",
    "3090-qwen35-9b":     "config-h-qwen35",
    "3070-qwen35-9b":     None,
    "3070-qwen35-4b":     None,
}

CONFIG_TO_PORT = {
    "3090-qwen36-35b":   BEE_LLAMA_PORT_3090,
    "3090-muse-glimmer":  BEE_LLAMA_PORT_3090,
    "3090-laguna-xs":     BEE_LLAMA_PORT_3090,
    "3090-qwen35-9b":     BEE_LLAMA_PORT_3090,
    "3070-qwen35-9b":     BEE_LLAMA_PORT_3070,
    "3070-qwen35-4b":     BEE_LLAMA_PORT_3070,
}

_TELEMETRY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS work_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_name    TEXT NOT NULL,
    project         TEXT NOT NULL,
    branch          TEXT,
    config_id       TEXT,
    task_prompt     TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    completed_at    TEXT,
    status          TEXT CHECK(status IN (
        'created','running','completed','failed','destroyed',
        'swapping','sandbox_ready','inferencing','collecting','pushing'
    )),
    inference_tokens       INTEGER,
    inference_seconds      REAL,
    inference_tokens_per_sec REAL,
    git_commits            INTEGER DEFAULT 0,
    git_files_changed      INTEGER DEFAULT 0,
    error_message          TEXT,
    session_uuid           TEXT
);

CREATE TABLE IF NOT EXISTS work_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES work_sessions(id),
    event_type      TEXT NOT NULL,
    event_data      TEXT,
    timestamp       TEXT DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# SSH transport
# ---------------------------------------------------------------------------

def ssh_run(cmd: str, host: str = TRITON_HOST, user: str = TRITON_USER,
            timeout: int = 30) -> tuple[str, int]:
    """Execute cmd on host via SSH. Returns (stdout, returncode)."""
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        cmd,
    ]
    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return f"SSH timed out after {timeout}s", 1
    except Exception as exc:
        return f"SSH error: {exc}", 1


# ---------------------------------------------------------------------------
# Telemetry DB (same schema, flat functions instead of a class)
# ---------------------------------------------------------------------------

def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(TELEMETRY_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_schema() -> None:
    conn = _db_connect()
    try:
        conn.executescript(_TELEMETRY_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_session(sandbox_name: str, project: str, branch: str | None = None,
                   config_id: str | None = None,
                   task_prompt: str | None = None) -> int:
    conn = _db_connect()
    try:
        cur = conn.execute(
            """INSERT INTO work_sessions
               (sandbox_name, project, branch, config_id, task_prompt,
                status, session_uuid)
               VALUES (?, ?, ?, ?, ?, 'created', ?)""",
            (sandbox_name, project, branch, config_id, task_prompt,
             str(uuid.uuid4())),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_session(session_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [session_id]
    conn = _db_connect()
    try:
        conn.execute(f"UPDATE work_sessions SET {sets} WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()


def record_event(session_id: int, event_type: str, data=None) -> None:
    conn = _db_connect()
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


def get_sessions(status: str | None = None) -> list[dict]:
    conn = _db_connect()
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


def get_events(session_id: int) -> list[dict]:
    conn = _db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM work_events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def telemetry_summary() -> dict:
    conn = _db_connect()
    try:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                   COALESCE(SUM(inference_tokens), 0) AS total_tokens,
                   COALESCE(SUM(inference_seconds), 0.0) AS total_seconds
               FROM work_sessions"""
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_health(port: int = BEE_LLAMA_PORT_3090) -> bool:
    """Check BeeLlama /health endpoint."""
    raw, rc = ssh_run(f"curl -sf http://localhost:{port}/health", timeout=10)
    if rc != 0:
        return False
    try:
        return json.loads(raw).get("status") == "ok"
    except (json.JSONDecodeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Step 1: Model swap (same logic as original, inlined)
# ---------------------------------------------------------------------------

def swap_model(config_id: str, session_id: int) -> dict:
    """Swap BeeLlama to the requested model config."""
    profile = CONFIG_TO_PROFILE.get(config_id)
    result = {"config_id": config_id, "profile": profile}

    if profile is None and config_id not in CONFIG_TO_PROFILE:
        result["status"] = "error"
        result["message"] = f"Unknown config: {config_id}"
        return result

    update_session(session_id, status="swapping")
    record_event(session_id, "model_swap_start",
                 {"config_id": config_id, "profile": profile})

    t0 = time.monotonic()

    if profile:
        # Try state machine first
        sm_cmd = (
            f"cd {COMPOSE_DIR} && "
            f"python3 deploy-state-machine.py swap {profile} 2>/dev/null"
        )
        raw, rc = ssh_run(sm_cmd, timeout=300)

        if rc == 0 and "READY" in raw.upper():
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            result.update(status="ok", load_time_ms=elapsed_ms,
                          method="state_machine")
            record_event(session_id, "model_swap_done", result)
            return result

        # Fallback: direct docker compose
        ssh_run(f"cd {COMPOSE_DIR} && docker compose --profile {profile} down 2>/dev/null",
                timeout=60)
        ssh_run(f"cd {COMPOSE_DIR} && docker compose --profile {profile} up -d",
                timeout=60)

        # Poll health (max 180s)
        for _ in range(60):
            if check_health(BEE_LLAMA_PORT_3090):
                break
            time.sleep(3)
        else:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            result.update(status="timeout", load_time_ms=elapsed_ms,
                          message="BeeLlama not healthy within 180s")
            record_event(session_id, "model_swap_timeout", result)
            return result
    else:
        result["status"] = "no_swap_needed"
        result["message"] = f"{config_id} uses the shared 3070 service"
        record_event(session_id, "model_swap_skip", result)
        return result

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    result.update(status="ok", load_time_ms=elapsed_ms, method="docker_compose")
    record_event(session_id, "model_swap_done", result)
    return result


# ---------------------------------------------------------------------------
# Step 2: Run inference (same logic, no Docker sandbox)
# ---------------------------------------------------------------------------

def run_inference(prompt: str, config: str, session_id: int,
                  max_tokens: int = 2048) -> dict:
    """Run inference via BeeLlama API directly on Triton (no sandbox)."""
    port = CONFIG_TO_PORT.get(config, BEE_LLAMA_PORT_3090)
    update_session(session_id, status="inferencing")
    record_event(session_id, "inference_start", {
        "config": config, "port": port, "max_tokens": max_tokens,
        "prompt_preview": prompt[:200],
    })

    t0 = time.monotonic()

    # Write payload to temp file on Triton to avoid shell-escaping hell
    escaped = prompt.replace("\\", "\\\\").replace("'", "\\'")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")

    payload = (
        f'{{"model":"q",'
        f'"messages":[{{"role":"user","content":"{escaped}"}}],'
        f'"max_tokens":{max_tokens},'
        f'"temperature":0.3,"top_p":0.95,"top_k":40}}'
    )

    tmp_payload = f"/tmp/work-engine-payload-{os.getpid()}.json"
    write_cmd = (
        f"python3 -c \"import sys; "
        f"open('{tmp_payload}','w').write(sys.stdin.read())\" "
        f"<<'PAYLOAD_EOF'\n{payload}\nPAYLOAD_EOF"
    )
    ssh_run(write_cmd, timeout=10)

    # Run inference directly — no Docker container needed
    curl_cmd = (
        f"curl -s -X POST http://localhost:{port}/v1/chat/completions "
        f"-H 'Content-Type: application/json' "
        f"-d @{tmp_payload}"
    )
    raw, rc = ssh_run(curl_cmd, timeout=120)

    # Cleanup
    ssh_run(f"rm -f {tmp_payload}", timeout=5)

    elapsed_s = time.monotonic() - t0

    if rc != 0:
        error_result = {"status": "error", "error": raw,
                        "elapsed_seconds": round(elapsed_s, 2)}
        record_event(session_id, "inference_fail", error_result)
        return error_result

    # Parse response
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        error_result = {
            "status": "error",
            "error": f"Bad JSON: {raw[:500]}",
            "elapsed_seconds": round(elapsed_s, 2),
        }
        record_event(session_id, "inference_parse_fail", error_result)
        return error_result

    # Extract content + timings
    choices = response.get("choices", [])
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    reasoning = choices[0].get("message", {}).get("reasoning_content", "") if choices else ""

    timings = response.get("timings", {})
    usage = response.get("usage", {})
    tps = timings.get("predicted_per_second", 0.0)
    predicted_ms = timings.get("predicted_ms", 0.0)
    predicted_n = timings.get("predicted_n", 0)
    total_tokens = usage.get("total_tokens", 0)
    thinking_tokens = timings.get("thinking_tokens", 0)
    if thinking_tokens == 0 and reasoning:
        thinking_tokens = len(reasoning) // 4

    # Record metrics
    update_session(session_id,
                   inference_tokens=predicted_n,
                   inference_seconds=round(predicted_ms / 1000.0, 3)
                       if predicted_ms else round(elapsed_s, 3),
                   inference_tokens_per_sec=tps)

    result = {
        "status": "ok",
        "content": content,
        "reasoning_content": reasoning[:2000] if reasoning else "",
        "timings": {
            "predicted_per_second": tps,
            "prompt_per_second": timings.get("prompt_per_second", 0.0),
            "predicted_ms": predicted_ms,
            "prompt_ms": timings.get("prompt_ms", 0.0),
            "predicted_n": predicted_n,
            "thinking_tokens": thinking_tokens,
        },
        "usage": {"total_tokens": total_tokens},
        "elapsed_seconds": round(elapsed_s, 2),
    }

    record_event(session_id, "inference_done", {
        "tokens": predicted_n, "tps": tps,
        "elapsed_s": round(elapsed_s, 2),
    })
    return result


# ---------------------------------------------------------------------------
# Step 3: Push results to Gitea
# ---------------------------------------------------------------------------

def push_results(project: str, branch: str, name: str,
                 results: dict, session_id: int) -> dict:
    """Commit results to a Gitea feature branch on Triton."""
    project_path = f"{DEFAULT_PROJECT_PATH}/{project}"
    results_branch = f"results/{name}"

    update_session(session_id, status="pushing")
    record_event(session_id, "push_start", {"branch": results_branch})

    # Write results JSON to project dir
    results_json = json.dumps(results, indent=2, default=str)
    results_file = f"{project_path}/results-{name}.json"
    write_cmd = (
        f"mkdir -p {project_path} && "
        f"cat > {results_file} << 'RESULTSEOF'\n"
        f"{results_json}\n"
        f"RESULTSEOF"
    )
    ssh_run(write_cmd, timeout=15)

    # Create branch, stage, commit, push
    ssh_run(
        f"cd {project_path} && "
        f"git checkout -b {results_branch} 2>/dev/null || "
        f"git checkout {results_branch}",
        timeout=15,
    )
    ssh_run(f"cd {project_path} && git add results-{name}.json", timeout=10)

    commit_msg = f"results: {name} [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}]"
    commit_out, commit_rc = ssh_run(
        f"cd {project_path} && git commit -m '{commit_msg}' 2>&1",
        timeout=15,
    )

    # Count files changed
    diff_out, _ = ssh_run(
        f"cd {project_path} && git diff --stat HEAD~1 2>/dev/null | tail -1",
        timeout=10,
    )
    files_changed = 0
    if diff_out:
        m = re.search(r"(\d+) files? changed", diff_out)
        if m:
            files_changed = int(m.group(1))

    # Get commit SHA
    sha_out, _ = ssh_run(
        f"cd {project_path} && git rev-parse --short HEAD", timeout=10,
    )

    # Push
    push_out, push_rc = ssh_run(
        f"cd {project_path} && git push origin {results_branch} 2>&1",
        timeout=60,
    )

    # Switch back
    ssh_run(f"cd {project_path} && git checkout {branch}", timeout=10)

    update_session(session_id, git_commits=1, git_files_changed=files_changed)

    result = {
        "status": "ok" if push_rc == 0 else "push_failed",
        "branch": results_branch,
        "commit_sha": sha_out.strip() if sha_out else None,
        "files_changed": files_changed,
        "push_output": push_out[:500] if push_out else "",
    }
    if push_rc != 0:
        result["error"] = push_out

    record_event(session_id, "push_done", result)
    return result


# ---------------------------------------------------------------------------
# Pipeline: swap → infer → push
# ---------------------------------------------------------------------------

def run_pipeline(project: str, branch: str = "main",
                 config: str = "3090-qwen36-35b",
                 task: str = "", name: str | None = None,
                 max_tokens: int = 2048) -> dict:
    """Execute the simplified pipeline: swap → infer → push.

    No Docker sandbox. No volume mounts. No apt-get install.
    Just SSH → curl → SSH → git.
    """
    if not task:
        return {"status": "error", "message": "Task prompt is required"}

    if name is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"work-{ts}"

    pipeline_start = time.monotonic()
    results = {
        "name": name, "project": project, "branch": branch,
        "config": config, "task_preview": task[:300], "steps": {},
    }

    session_id = record_session(
        sandbox_name=name, project=project, branch=branch,
        config_id=config, task_prompt=task,
    )
    results["session_id"] = session_id

    try:
        # Step 1: Swap model
        log.info("Step 1/3: Swapping model to %s", config)
        swap_result = swap_model(config, session_id)
        results["steps"]["swap"] = swap_result

        if swap_result.get("status") not in ("ok", "no_swap_needed"):
            results["status"] = "failed"
            results["error"] = f"Model swap failed: {swap_result}"
            update_session(session_id, status="failed",
                           error_message=results["error"])
            record_event(session_id, "pipeline_abort",
                         {"reason": "model_swap_failed"})
            return results

        # Step 2: Run inference (directly, no sandbox)
        log.info("Step 2/3: Running inference (config=%s, max_tokens=%d)",
                 config, max_tokens)
        update_session(session_id, status="running")
        infer_result = run_inference(task, config, session_id,
                                     max_tokens=max_tokens)
        results["steps"]["inference"] = infer_result

        if infer_result.get("status") != "ok":
            results["status"] = "failed"
            results["error"] = f"Inference failed: {infer_result}"
            update_session(session_id, status="failed",
                           error_message=results["error"])
            record_event(session_id, "pipeline_inference_failed", infer_result)

        # Step 3: Push results to Gitea
        if infer_result.get("status") == "ok":
            log.info("Step 3/3: Pushing results to Gitea")
            push_result = push_results(project, branch, name,
                                       results, session_id)
            results["steps"]["push"] = push_result
        else:
            log.info("Step 3/3: Skipping push (inference failed)")
            results["steps"]["push"] = {"status": "skipped",
                                        "reason": "inference_failed"}

    except Exception as exc:
        log.error("Pipeline error: %s", exc)
        results["status"] = "failed"
        results["error"] = str(exc)
        update_session(session_id, status="failed", error_message=str(exc))
        record_event(session_id, "pipeline_exception", {"error": str(exc)})

    # Final status
    total_s = time.monotonic() - pipeline_start
    results["elapsed_seconds"] = round(total_s, 2)

    if "status" not in results:
        results["status"] = "completed"

    update_session(session_id,
                   status="completed" if results["status"] == "completed" else "failed",
                   completed_at=datetime.now(timezone.utc).isoformat())
    record_event(session_id, "pipeline_done", {
        "status": results["status"], "elapsed_s": round(total_s, 2),
    })

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="work_engine_simple",
        description="Simplified work engine — no Docker sandboxes.",
    )
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print JSON output.")
    p.add_argument("--verbose", "-v", action="store_true")

    sub = p.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Execute a coding task.")
    run_p.add_argument("--project", required=True)
    run_p.add_argument("--branch", default="main")
    run_p.add_argument("--config", default="3090-qwen36-35b")
    run_p.add_argument("--task", required=True)
    run_p.add_argument("--name", default=None)
    run_p.add_argument("--max-tokens", type=int, default=2048)

    # sessions
    sess_p = sub.add_parser("sessions", help="List work sessions.")
    sess_p.add_argument("--status", default=None)

    # status
    sub.add_parser("status", help="Telemetry summary.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return 0

    ensure_schema()
    pretty = args.pretty

    if args.command == "run":
        result = run_pipeline(
            project=args.project, branch=args.branch,
            config=args.config, task=args.task,
            name=args.name, max_tokens=args.max_tokens,
        )
    elif args.command == "sessions":
        result = get_sessions(status=getattr(args, "status", None))
    elif args.command == "status":
        result = telemetry_summary()
    else:
        parser.print_help()
        return 1

    print(json.dumps(result, indent=2 if pretty else None, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
