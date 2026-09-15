#!/usr/bin/env python3
"""S8 Run 2 — wave-dispatch the 10 ralph-loop tickets through the ACE engine.

Sequentially runs one engine.run() per ticket (RL1-1..RL1-10, Gitea #14-#23),
each with a single-task PREPARED PRD. The workspace persists across runs so
later tasks see earlier modules (fixing run-1's isolation failure).

Wave-0 rails active (D1-D5): 3-tier import classifier, contract-stub pre-pass,
targeting rail, personas + re-plan actions, skills wiring.
Observability active: session_logs table + per-run sidecar JSONL + ace autopsy.

Budget: max_llm_calls=100, max_total_tokens=5M, max_wall_clock_s=7200 per run.
Loop guard: abort if cumulative wall-clock > 4h or 2 consecutive infra failures.
"""
import os, sys, json, time, traceback

# Engine on PATH
sys.path.insert(0, "/home/<user>/projects/ace-engine")
os.environ["GITEA_TOKEN"] = "<GITEA_TOKEN>"
os.environ["GITEA_URL"] = "http://<LAN_IP>:3000"

# engine.db lives in the engine checkout; point autopsy + state at it explicitly
os.environ["ENGINE_DB_PATH"] = "/home/<user>/projects/ace-engine/engine.db"

# Transport: models are on <LAN_IP> (Triton). LocalTransport is selected
# because localhost:8080 health passes, but localhost:8082 (subject) is NOT
# reachable locally — point both BeeLlama URLs at Triton explicitly.
os.environ["BEE_LLAMA_3090"] = "http://<LAN_IP>:8080"
os.environ["BEE_LLAMA_3070"] = "http://<LAN_IP>:8082"
os.environ["TRANSPORT_MODE"] = "local"

from engine.engine import Engine, EngineConfig
import engine.prd as prd_mod
import engine.session_log as sl_mod
from engine.session_log import SessionLogRecorder

# FIX: SessionLogRecorder's SQLite connection is created in the main thread but
# generate_code runs in a worker thread (engine uses ThreadPoolExecutor for
# timeouts). SQLite defaults to check_same_thread=True, which raises
# ProgrammingError. Patch _get_conn to allow cross-thread use.
_original_get_conn = sl_mod._get_conn
def _thread_safe_get_conn(db_path="engine.db"):
    conn = _original_get_conn(db_path)
    # The connection was created with check_same_thread=True (default).
    # We need a new connection that allows cross-thread access.
    # Close this one and reopen with check_same_thread=False.
    conn.close()
    import sqlite3 as _sqlite3
    conn2 = _sqlite3.connect(db_path, check_same_thread=False)
    conn2.row_factory = _sqlite3.Row
    conn2.execute("PRAGMA busy_timeout=5000")
    conn2.execute("PRAGMA journal_mode=WAL")
    return conn2
sl_mod._get_conn = _thread_safe_get_conn

# ── Task list in dependency order (RL1-1..RL1-10) ──────────────────────────
RUN2_DIR = "/home/<user>/projects/ace-engine/.scratch/ralph-loop/run2"
TASKS = [
    "RL1-1", "RL1-2", "RL1-3", "RL1-4", "RL1-5",
    "RL1-6", "RL1-7", "RL1-8", "RL1-9", "RL1-10",
]
TICKET_MAP = {t: 13 + int(t.split("-")[1]) for t in TASKS}  # RL1-1→#14, etc.

WORKSPACE = "/home/<user>/projects/ace-engine-ralph-ws2"

# ── Monkeypatch: inject full files[] + acceptance_criteria from sidecar ────
_original_parse = prd_mod.parse_prd

def _patched_parse(prd_path):
    """Parse PRD then inject files[]/acceptance_criteria from sidecar JSON.

    The regex parser only extracts the first backtick file as `module`; the
    full files[] list lives in the sidecar JSON. Injecting it lets D2 (stubs)
    and D3 (targeting) rails see every declared file.
    """
    tasks = _original_parse(prd_path)
    sidecar_path = prd_path.replace(".md", ".json")
    if os.path.exists(sidecar_path):
        with open(sidecar_path) as f:
            sc = json.load(f)
        for t in tasks:
            t.files = list(sc.get("files", []))
            t.acceptance_criteria = list(sc.get("acceptance_criteria", []))
            t.dependencies = list(sc.get("dependencies", []))
    return tasks

prd_mod.parse_prd = _patched_parse


def make_config(task_id):
    """Build EngineConfig matching run-1 parameters.

    The session_log_recorder is attached with a per-task run_id prefix so
    session_logs can be correlated with the engine's run_id (which is
    generated inside engine.run() as run-{timestamp}).
    """
    cfg = EngineConfig(
        model_config="3070-qwen35-9b",  # 9B subject on :8082
        judge_mode="deterministic",
        judge_port=8080,
        subject_port=8082,
        judge_temperature=0.0,
        max_retries_generate=3,
        max_retries_test=2,
        timeout_inference=300,
        timeout_test=180,
        timeout_commit=60,
        dry_run=False,
        sandbox=False,
        judge=True,
        max_llm_calls=100,
        max_total_tokens=5_000_000,
        max_wall_clock_s=7200,
    )
    # Attach session log recorder (observability, additive).
    # Run ID prefix = task_id; engine generates its own run-{timestamp} but
    # the recorder captures all LLM calls keyed by task_id for later correlation.
    try:
        recorder = SessionLogRecorder(f"run2-{task_id}",
                                      db_path=os.environ["ENGINE_DB_PATH"])
        recorder.__enter__()
        cfg.session_log_recorder = recorder
    except Exception as e:
        print(f"  [warn] SessionLogRecorder init failed: {e}")
        cfg.session_log_recorder = None
    return cfg


def count_session_logs(run_id, db_path):
    """Count session_logs rows for a run."""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.execute("SELECT COUNT(*) FROM session_logs WHERE run_id=?", (run_id,))
        n = cur.fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def get_commit_sha(workspace):
    """Get current HEAD sha from workspace."""
    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def main():
    log_path = os.path.join(RUN2_DIR, "run2.log")
    os.makedirs(RUN2_DIR, exist_ok=True)

    def log(msg):
        print(msg, flush=True)
        with open(log_path, "a") as f:
            f.write(msg + "\n")

    log("=" * 70)
    log("S8 Run 2 — Ralph Loop wave-dispatch (10 tickets, sequential)")
    log(f"Workspace: {WORKSPACE}")
    log(f"Start: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 70)

    # Verify models
    import urllib.request
    for name, port in [("subject", 8082), ("judge", 8080)]:
        try:
            r = urllib.request.urlopen(f"http://<LAN_IP>:{port}/health", timeout=5)
            log(f"  model {name} :{port} -> {r.status}")
        except Exception as e:
            log(f"  [FATAL] model {name} :{port} unreachable: {e}")
            # Write blocked report
            with open(os.path.join(RUN2_DIR, "RUN-REPORT.md"), "w") as f:
                f.write("# Run Report: S8 Ralph Loop Run 2 — BLOCKED\n\n")
                f.write(f"**BLOCKED**: model {name} :{port} unreachable at run start.\n")
            sys.exit(1)

    accumulator = []
    loop_start = time.time()
    consecutive_infra_failures = 0
    any_infra_failure = False

    for idx, task_id in enumerate(TASKS):
        ticket = TICKET_MAP[task_id]
        prd_path = os.path.join(RUN2_DIR, f"PRD-{task_id}.md")
        log(f"\n{'─' * 60}")
        log(f"[{idx+1}/10] {task_id} (Gitea #{ticket})")
        log(f"  PRD: {prd_path}")

        if not os.path.exists(prd_path):
            log(f"  [ERROR] PRD not found: {prd_path}")
            accumulator.append({
                "task_id": task_id, "ticket": ticket, "state": "ERROR",
                "attempts": 0, "tokens": 0, "error": "PRD not found",
                "commit_sha": None, "session_logs": 0, "time_s": 0,
            })
            continue

        task_start = time.time()
        run_id = None
        recorder = None
        try:
            cfg = make_config(task_id)
            recorder = getattr(cfg, "session_log_recorder", None)

            engine = Engine(config=cfg)
            log(f"  engine.run() ...")
            result = engine.run(prd_path, WORKSPACE)
            run_id = result.run_id

            # Extract outcome from the single task
            task_result = result.tasks[0] if result.tasks else None
            state = task_result.state if task_result else "UNKNOWN"
            attempts = task_result.attempts if task_result else 0
            tokens = result.total_tokens
            error = task_result.error_message if task_result else None
            commit_sha = task_result.commit_sha if task_result else None
            task_time = time.time() - task_start

            # Count session logs (recorder uses run2-{task_id} prefix)
            sl_count = count_session_logs(f"run2-{task_id}",
                                              os.environ["ENGINE_DB_PATH"])

            # Get workspace commit sha
            ws_sha = get_commit_sha(WORKSPACE)

            log(f"  → state={state} attempts={attempts} tokens={tokens} "
                f"time={task_time:.1f}s commit={commit_sha} session_logs={sl_count}")

            entry = {
                "task_id": task_id, "ticket": ticket, "state": state,
                "attempts": attempts, "tokens": tokens, "error": error,
                "commit_sha": commit_sha or ws_sha,
                "session_logs": sl_count, "time_s": round(task_time, 1),
                "run_id": run_id,
            }
            accumulator.append(entry)

            # Infra-failure detection
            if state == "FAILED":
                err_lower = (error or "").lower()
                infra_keywords = ["unreachable", "connection", "timeout", "import",
                                  "modulenotfound", "infrastructure"]
                is_infra = any(k in err_lower for k in infra_keywords)
                if is_infra:
                    consecutive_infra_failures += 1
                    any_infra_failure = True
                else:
                    consecutive_infra_failures = 0
            else:
                consecutive_infra_failures = 0

        except Exception as e:
            task_time = time.time() - task_start
            err_str = str(e)
            log(f"  [EXCEPTION] {err_str}")
            tb = traceback.format_exc()
            log(f"  {tb[:500]}")
            accumulator.append({
                "task_id": task_id, "ticket": ticket, "state": "EXCEPTION",
                "attempts": 0, "tokens": 0, "error": err_str,
                "commit_sha": get_commit_sha(WORKSPACE),
                "session_logs": 0, "time_s": round(task_time, 1),
                "run_id": run_id,
            })
            consecutive_infra_failures += 1
            any_infra_failure = True

        finally:
            if recorder is not None:
                try:
                    recorder.__exit__(None, None, None)
                except Exception:
                    pass

        # Save accumulator after each task
        with open(os.path.join(RUN2_DIR, "run2-accumulator.json"), "w") as f:
            json.dump(accumulator, f, indent=2)

        # Budget guards
        elapsed = time.time() - loop_start
        if elapsed > 4 * 3600:
            log(f"\n[BUDGET] Cumulative wall-clock {elapsed:.0f}s > 4h — aborting loop")
            break
        if consecutive_infra_failures >= 2:
            log(f"\n[BUDGET] 2 consecutive infra failures — aborting loop")
            break

    # ── Final summary ──────────────────────────────────────────────────────
    total_time = time.time() - loop_start
    committed = sum(1 for a in accumulator if a["state"] in ("COMMIT", "committed"))
    failed = sum(1 for a in accumulator if a["state"] in ("FAILED", "failed"))
    total_tokens = sum(a["tokens"] for a in accumulator)

    log(f"\n{'=' * 70}")
    log(f"RUN 2 COMPLETE — {len(accumulator)}/{len(TASKS)} tasks attempted")
    log(f"  Committed: {committed}  Failed: {failed}  Total tokens: {total_tokens}")
    log(f"  Total time: {total_time:.1f}s ({total_time/60:.1f} min)")
    log(f"  N4 (committed/attempted): {committed}/{len(accumulator)} = "
        f"{100*committed/max(len(accumulator),1):.0f}%")
    log(f"{'=' * 70}")

    # Final save
    with open(os.path.join(RUN2_DIR, "run2-accumulator.json"), "w") as f:
        json.dump(accumulator, f, indent=2)

    # Print per-task table
    log(f"\n{'Task':<8} {'Ticket':<8} {'State':<12} {'Attempts':<10} {'Tokens':<10} "
        f"{'SL':<6} {'Time':<8} {'Commit':<12}")
    for a in accumulator:
        log(f"{a['task_id']:<8} #{a['ticket']:<7} {a['state']:<12} {a['attempts']:<10} "
            f"{a['tokens']:<10} {a['session_logs']:<6} {a['time_s']:<8} "
            f"{(a['commit_sha'] or '')[:10]:<12}")

    return accumulator


if __name__ == "__main__":
    main()
