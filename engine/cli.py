"""Unified CLI for the autonomous coding engine.

Single entry point replacing all 6 old CLIs. Uses argparse with subcommands,
lazy imports for fast --help, and JSON output.

Usage::

    python3 engine/cli.py --help
    python3 engine/cli.py ace parse prd.md
    python3 engine/cli.py ace run --prd prd.md --project /path/to/project --judge
    python3 engine/cli.py ace status [--run <id>]
    python3 engine/cli.py ace resume --run <id>
    python3 engine/cli.py ace cancel
"""

import argparse
import json
import sys
import os

# Ensure the repo root (parent of engine/) is on sys.path so that
# ``python3 engine/cli.py`` can import ``engine.*`` packages.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def cmd_run(args):
    """Handle 'ace run' subcommand."""
    from engine.engine import Engine
    from engine import EngineConfig
    config = EngineConfig(
        model_config=args.config,
        dry_run=args.dry_run,
        judge=args.judge,
    )
    engine = Engine(config=config)
    result = engine.run(args.prd, args.project, dry_run=args.dry_run)
    return {
        "run_id": result.run_id,
        "success": result.success,
        "total_time_s": result.total_time_s,
        "tasks": [
            {"id": t.task_id, "title": t.title, "state": t.state,
             "commit_sha": t.commit_sha, "tokens": t.tokens}
            for t in result.tasks
        ],
    }


def cmd_research(args):
    """Handle 'ace research' subcommand — run a single research atom.

    Thin CLI: loads a PRD JSON, extracts the first research task, and
    dispatches it through the research pipeline (generate -> validate ->
    score). Uses mock transport for deterministic e2e.
    """
    from engine.task_handler import _resolve_handler
    from engine import Task, EngineConfig
    from engine.mock_transport import MockTransport
    import json as _json

    # Load PRD JSON and extract the first research task.
    with open(args.prd, "r", encoding="utf-8") as f:
        prd = _json.load(f)
    raw_tasks = prd.get("tasks", [])
    research_tasks = [t for t in raw_tasks if t.get("category") == "research"]
    if not research_tasks:
        raise ValueError(f"No research tasks found in PRD: {args.prd}")
    raw = research_tasks[0]

    task = Task(
        id=raw.get("id", "R01"),
        title=raw.get("title", "research atom"),
        description=raw.get("description", ""),
        module=raw.get("module", "engine/research/output.md"),
        dependencies=raw.get("dependencies", []),
        task_type="research",
        prd_section=raw.get("spec", ""),
        files=raw.get("files", []),
        acceptance_criteria=raw.get("acceptance_criteria", []),
    )

    # Build a minimal mock engine with mock transport.
    transport = MockTransport(fixture_path="", record_mode=False)
    config = EngineConfig(dry_run=True)

    class _MockEngine:
        config = config
        transport = transport

    handler = _resolve_handler(task, _MockEngine())
    # run() returns a TaskResult-shaped object.
    result = handler.run(task, _MockPipeline(), args.project)
    return {
        "task_id": result.task_id,
        "title": result.title,
        "state": result.state,
        "error_message": result.error_message,
        "role": getattr(result, "role", "researcher"),
        "scores": getattr(result, "scores", None),
    }


class _MockPipeline:
    """Minimal pipeline stand-in for the research CLI."""
    run_id = "research-cli"
    tasks = []
    state = "IDLE"


def cmd_parse(args):
    """Handle 'ace parse' subcommand."""
    # parse_prd lives in engine.prd (T23); engine.engine (T32) re-exports it
    try:
        from engine.engine import parse_prd
    except ImportError:
        from engine.prd import parse_prd
    tasks = parse_prd(args.prd)
    return [{"id": t.id, "title": t.title, "module": t.module,
             "dependencies": t.dependencies} for t in tasks]


def cmd_status(args):
    """Handle 'ace status' subcommand."""
    from engine.engine import Engine
    from engine.state import load_run, list_runs

    # If run_id given, load that specific run
    if args.run:
        data = load_run(args.run)
        if data is None:
            raise ValueError(f"No checkpoint found for run_id={args.run}")
        import json as _json
        tasks_json = data.get("tasks_json", "[]")
        tasks = _json.loads(tasks_json) if tasks_json else []
        total = len(tasks)
        completed = sum(
            1 for t in tasks
            if t.get("state") in ("DONE", "FAILED", "CANCELLED")
        )
        return {
            "run_id": args.run,
            "state": data.get("state", "IDLE"),
            "progress_pct": (completed / total * 100) if total > 0 else 0.0,
            "current_task": data.get("current_task_id"),
            "tasks_completed": completed,
            "tasks_total": total,
        }

    # No run_id — list all runs
    runs = list_runs()
    return {"runs": [{"id": r["id"], "state": r["state"],
                       "started_at": r.get("started_at")} for r in runs]}


def cmd_resume(args):
    """Handle 'ace resume' subcommand."""
    from engine.engine import Engine
    from engine.pipeline import Pipeline
    engine = Engine()
    # Restore pipeline from checkpoint — this reconstructs state, tasks, retry counters
    pipeline = Pipeline.resume(args.run, engine)
    engine.pipeline = pipeline
    # Continue from checkpointed prd_path and project_path
    from engine.state import load_run
    data = load_run(args.run)
    if data is None:
        raise ValueError(f"No checkpoint found for run_id={args.run}")
    prd_path = data.get("prd_path", "")
    project_path = data.get("project_path", "")
    result = engine.run(prd_path, project_path)
    return {"run_id": result.run_id, "success": result.success}


def cmd_cancel(args):
    """Handle 'ace cancel' subcommand."""
    from engine.engine import Engine
    engine = Engine()
    result = engine.cancel()
    return {"cancelled": result}


def cmd_autopsy(args):
    """Handle 'ace autopsy <run_id> [task_id]' subcommand."""
    from engine.autopsy import autopsy
    report = autopsy(args.run, args.task, full=args.full, db_path=args.db)
    return {"report": report}


def cmd_tail(args):
    """Handle 'ace tail <run_id>' subcommand."""
    from engine.tail import tail_run
    events = tail_run(args.run, event_type=args.type, interval=args.interval,
                      timeout=args.timeout, db_path=args.db)
    return {"events": events, "count": len(events)}


def cmd_archive(args):
    """Handle 'ace archive <run_id>' subcommand — re-expose a completed run."""
    from engine.archive import archive_run
    from engine.state import load_run
    data = load_run(args.run)
    if data is None:
        raise ValueError(f"No checkpoint found for run_id={args.run}")
    # Reconstruct a minimal RunReport from the checkpoint for re-archive.
    report = {
        "run_id": args.run,
        "config": data.get("config", ""),
        "prd_path": data.get("prd_path", ""),
        "project_path": data.get("project_path", ""),
        "success": data.get("state") == "DONE",
        "wall_clock_s": 0.0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "thinking_tokens": 0,
        "tokens_per_sec": 0.0,
        "per_task": json.loads(data.get("tasks_json") or "[]"),
        "judge_verdict": None,
        "error_message": data.get("error_message"),
    }
    archive_dir = archive_run(args.run, data.get("project_path", ""),
                              report=report)
    return {"run_id": args.run, "archive_dir": archive_dir}


def cmd_ralph_run(args):
    """Handle 'ralph run' subcommand (M4).

    G4-T02 / DESIGN-G4 §3 flag C: ``--enable-memory-recall`` copies the
    engine's ``enable_memory_recall`` flag into ``RalphConfig`` so that
    ideation's ``recall_for_objective()`` call is gated consistently.  The
    config.py comment claims this copy happens — without it the flag never
    reaches ideation.
    """
    from engine.workflows.ralph import run_ralph, RalphConfig
    config = RalphConfig(
        max_ralph_rounds=args.max_rounds,
        enable_memory_recall=getattr(args, "enable_memory_recall", False),
    )
    result = run_ralph(
        objective=args.objective,
        project_path=args.project,
        config=config,
    )
    return result.to_report_dict()


def cmd_ralph_resume(args):
    """Handle 'ralph resume' subcommand (M4)."""
    from engine.workflows.ralph import resume_ralph, RalphConfig
    config = RalphConfig()
    result = resume_ralph(
        run_id=args.run_id,
        project_path=args.project,
        config=config,
    )
    return result.to_report_dict()


def cmd_ralph_status(args):
    """Handle 'ralph status' subcommand (M4)."""
    from engine.workflows.ralph.round_driver import _ensure_schema
    from engine.state import _get_conn
    _ensure_schema()
    conn = _get_conn()
    if args.run_id:
        row = conn.execute(
            "SELECT * FROM ralph_runs WHERE ralph_run_id=?", (args.run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Ralph run {args.run_id} not found")
        rounds = conn.execute(
            "SELECT round_id, state, workspace_sha FROM ralph_rounds WHERE ralph_run_id=? ORDER BY round_id",
            (args.run_id,),
        ).fetchall()
        return {
            "run": dict(row),
            "rounds": [dict(r) for r in rounds],
            "resume_hint": f"python3 engine/cli.py ralph resume --run-id {args.run_id} --project <path>",
        }
    else:
        rows = conn.execute(
            "SELECT ralph_run_id, state, objective, created_at FROM ralph_runs ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        return {"runs": [dict(r) for r in rows]}


def cmd_ralph_approve_pattern(args):
    """Handle 'ralph approve-pattern' subcommand (S5 #3)."""
    from engine.workflows.ralph.pattern_gate import approve_pattern
    ok = approve_pattern(args.pattern_id, approved_by="cli")
    return {"approved": ok, "pattern_id": args.pattern_id}


def cmd_ralph_reject_pattern(args):
    """Handle 'ralph reject-pattern' subcommand (S5 #3)."""
    from engine.workflows.ralph.pattern_gate import reject_pattern
    ok = reject_pattern(args.pattern_id, rejected_by="cli")
    return {"rejected": ok, "pattern_id": args.pattern_id}


# ---------------------------------------------------------------------------
# G3-T04: ace memory subcommands (poisoning gate CLI)
# ---------------------------------------------------------------------------

def cmd_memory_approve(args):
    """Handle 'ace memory approve <claim_id>' — approve a pending memory claim.

    Runs the contradiction pre-check automatically (non-blocking); findings are
    attached to the output so the human can decide whether to proceed.
    Mirrors cmd_ralph_approve_pattern for the memory_claims store.
    """
    from engine.memory.gate import approve
    result = approve(args.claim_id, approved_by="cli")
    return {
        "approved": result.approved,
        "claim_id": result.record_id,
        "contradictions": [
            {
                "claim_a_id": c.claim_a_id,
                "claim_b_id": c.claim_b_id,
                "run_ref_a": c.run_ref_a,
                "run_ref_b": c.run_ref_b,
                "similarity": c.similarity,
                "kind": c.kind,
            }
            for c in result.contradictions
        ],
        "message": result.message,
    }


def cmd_memory_reject(args):
    """Handle 'ace memory reject <claim_id>' — reject a pending memory claim."""
    from engine.memory.gate import reject
    result = reject(args.claim_id, rejected_by="cli")
    return {
        "rejected": not result.approved and "rejected" in result.message,
        "claim_id": result.record_id,
        "message": result.message,
    }


def cmd_memory_list(args):
    """Handle 'ace memory list [store]' — list pending claims or approved patterns.

    store='claims' (default): lists the memory_claims pending queue with
    contradiction counts (the poisoning-gate review surface).
    store='patterns': lists approved ralph patterns (reuses pattern query).
    """
    store = getattr(args, "store", "claims") or "claims"
    if store == "patterns":
        from engine.workflows.ralph.pattern_gate import export_approved_patterns
        from engine.state import _get_conn
        from engine.workflows.ralph.pattern_gate import _ensure_schema
        _ensure_schema()
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, pattern_type, score, status, created_at FROM ralph_patterns WHERE status='approved' ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        conn.close()
        return {
            "store": "patterns",
            "patterns": [dict(r) for r in rows],
            "count": len(rows),
        }
    # Default: memory_claims pending queue.
    from engine.memory.gate import list_pending
    pending = list_pending()
    return {
        "store": "claims",
        "pending": pending,
        "count": len(pending),
    }


def main():
    parser = argparse.ArgumentParser(prog="engine", description="Autonomous Coding Engine CLI")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--transport", choices=["auto", "http", "ssh", "local"], default="auto",
                        help="Override transport auto-detection")

    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # ace group
    ace_parser = subparsers.add_parser("ace", help="Engine commands")
    ace_sub = ace_parser.add_subparsers(dest="ace_command", help="Engine subcommands")

    # ace run
    run_parser = ace_sub.add_parser("run", help="Run a PRD end-to-end")
    run_parser.add_argument("--prd", required=True, help="Path to PRD markdown file")
    run_parser.add_argument("--project", required=True, help="Project directory path")
    run_parser.add_argument("--config", default="3090-qwen36-35b", help="Model config ID")
    run_parser.add_argument("--dry-run", action="store_true", help="Parse only, no code generation")
    run_parser.add_argument("--judge", action="store_true", help="Enable judge scoring (writes to engine.db judge_verdicts table)")

    # ace parse
    parse_parser = ace_sub.add_parser("parse", help="Parse PRD into task list")
    parse_parser.add_argument("prd", help="Path to PRD markdown file")

    # ace status
    status_parser = ace_sub.add_parser("status", help="Show run status")
    status_parser.add_argument("--run", help="Run ID (defaults to listing all runs)")

    # ace resume
    resume_parser = ace_sub.add_parser("resume", help="Resume an interrupted run")
    resume_parser.add_argument("--run", required=True, help="Run ID to resume")

    # ace cancel
    ace_sub.add_parser("cancel", help="Cancel the current run")

    # ace autopsy
    autopsy_parser = ace_sub.add_parser("autopsy", help="Read-only failure reconstruction")
    autopsy_parser.add_argument("run", help="Run ID to autopsy")
    autopsy_parser.add_argument("task", nargs="?", default=None,
                                 help="Optional task ID to deep-dive")
    autopsy_parser.add_argument("--full", action="store_true",
                                 help="Include full prompt/response payloads from sidecar")
    autopsy_parser.add_argument("--db", default=None, help="Database path override")

    # ace tail
    tail_parser = ace_sub.add_parser("tail", help="Live-tail a running run's events")
    tail_parser.add_argument("run", help="Run ID to tail")
    tail_parser.add_argument("--type", default=None, help="Filter by event type")
    tail_parser.add_argument("--interval", type=float, default=1.0,
                              help="Polling interval in seconds (default: 1.0)")
    tail_parser.add_argument("--timeout", type=float, default=30.0,
                              help="Max seconds to tail (default: 30.0)")
    tail_parser.add_argument("--db", default=None, help="Database path override")

    # ace archive
    archive_parser = ace_sub.add_parser("archive", help="Archive a completed run (re-expose)")
    archive_parser.add_argument("run", help="Run ID to archive")

    # ace research (T10 — thin CLI for a single research atom)
    research_parser = ace_sub.add_parser("research", help="Run a single research atom from a PRD JSON")
    research_parser.add_argument("--prd", required=True, help="Path to PRD JSON file")
    research_parser.add_argument("--project", default="/tmp/research-proj", help="Project directory path")

    # ralph group (M4: ralph status CLI)
    ralph_parser = subparsers.add_parser("ralph", help="Ralph meta-cognitive loop")
    ralph_sub = ralph_parser.add_subparsers(dest="ralph_command", help="Ralph subcommands")

    ralph_run_parser = ralph_sub.add_parser("run", help="Run a Ralph loop")
    ralph_run_parser.add_argument("--objective", required=True, help="Ralph objective")
    ralph_run_parser.add_argument("--project", required=True, help="Project directory path")
    ralph_run_parser.add_argument("--max-rounds", type=int, default=5, help="Max Ralph rounds")
    ralph_run_parser.add_argument("--enable-memory-recall", action="store_true",
                                  help="Enable memory recall in ralph ideation (G3 memory lane, DESIGN-G4 §3 flag C)")

    ralph_resume_parser = ralph_sub.add_parser("resume", help="Resume a failed/cancelled Ralph run")
    ralph_resume_parser.add_argument("--run-id", required=True, help="Ralph run ID to resume")
    ralph_resume_parser.add_argument("--project", required=True, help="Project directory path")

    ralph_status_parser = ralph_sub.add_parser("status", help="Show Ralph run status")
    ralph_status_parser.add_argument("--run-id", help="Ralph run ID (omit to list all)")

    ralph_approve_parser = ralph_sub.add_parser("approve-pattern", help="Approve a proposed pattern")
    ralph_approve_parser.add_argument("pattern_id", help="Pattern ID to approve")

    ralph_reject_parser = ralph_sub.add_parser("reject-pattern", help="Reject a proposed pattern")
    ralph_reject_parser.add_argument("pattern_id", help="Pattern ID to reject")

    # G3-T04: ace memory subcommand group (poisoning gate CLI).
    # Mirrors the ralph approve-pattern/reject-pattern structure for the
    # memory_claims store. review lists the pending queue with contradiction
    # counts.
    memory_parser = subparsers.add_parser("memory", help="Memory poisoning gate (approve/reject/list)")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", help="Memory subcommands")

    memory_approve_parser = memory_sub.add_parser("approve", help="Approve a pending memory claim")
    memory_approve_parser.add_argument("claim_id", help="Memory claim ID to approve")

    memory_reject_parser = memory_sub.add_parser("reject", help="Reject a pending memory claim")
    memory_reject_parser.add_argument("claim_id", help="Memory claim ID to reject")

    memory_list_parser = memory_sub.add_parser("list", help="List pending memory claims (store=claims) or approved patterns (store=patterns)")
    memory_list_parser.add_argument("store", nargs="?", default="claims",
                                     choices=["claims", "patterns"],
                                     help="Store to list: claims (memory_claims pending queue) or patterns (approved ralph patterns)")

    args = parser.parse_args()

    # Set transport override via env if provided
    if args.transport != "auto":
        os.environ["TRANSPORT_MODE"] = args.transport

    # Dispatch
    dispatch = {
        ("ace", "run"): cmd_run,
        ("ace", "parse"): cmd_parse,
        ("ace", "status"): cmd_status,
        ("ace", "resume"): cmd_resume,
        ("ace", "cancel"): cmd_cancel,
        ("ace", "autopsy"): cmd_autopsy,
        ("ace", "tail"): cmd_tail,
        ("ace", "archive"): cmd_archive,
        ("ace", "research"): cmd_research,
        ("memory", "approve"): cmd_memory_approve,
        ("memory", "reject"): cmd_memory_reject,
        ("memory", "list"): cmd_memory_list,
        ("ralph", "run"): cmd_ralph_run,
        ("ralph", "resume"): cmd_ralph_resume,
        ("ralph", "status"): cmd_ralph_status,
        ("ralph", "approve-pattern"): cmd_ralph_approve_pattern,
        ("ralph", "reject-pattern"): cmd_ralph_reject_pattern,
    }

    # Resolve the subcommand for any group (ace / ralph / memory).
    sub = getattr(args, "ace_command", None) or getattr(args, "ralph_command", None) or getattr(args, "memory_command", None)
    handler = dispatch.get((args.command, sub))
    if not handler:
        parser.print_help()
        sys.exit(1)

    try:
        data = handler(args)
        output = {"ok": True, "data": data}
    except Exception as e:
        output = {"ok": False, "error": str(e)}

    indent = 2 if args.pretty else None
    print(json.dumps(output, indent=indent, default=str))
    sys.exit(0 if output["ok"] else 1)


if __name__ == "__main__":
    main()
