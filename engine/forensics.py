"""Forensics — bisect a trace to find the first divergent atom.

``forensics.py bisect --run <id>`` replays a recorded trace in mock mode,
walks each atom, and reports the first one whose recomputed ``output_hash``
differs from the recorded value.

Rule 6: Traces live in engine.db only — never benchmark-results.db.
"""

from __future__ import annotations

import argparse
import json
import sys
import time


# ── Bisect API ─────────────────────────────────────────────────────────────────


def bisect(
    run_id: str, db_path: str = "engine.db", verbose: bool = False
) -> dict:
    """Replay a trace and find the first divergent atom.

    Returns
    -------
    dict
        If no divergence:
            ``{"diverged": False, "atom_count": int, "all_match": True}``
        If divergence:
            ``{"diverged": True, "atom_id", "seq", "recorded_hash",
              "recomputed_hash", "diff", "atoms_before_divergence"}``
    """
    from engine.atom_replay import replay_trace

    results = replay_trace(run_id, db_path, verbose)
    for i, r in enumerate(results):
        if "stopped_at" in r:
            # Sentinel from replay_trace — skip it.
            continue
        if not r["match"]:
            return {
                "diverged": True,
                "atom_id": r["atom_id"],
                "seq": i + 1,
                "recorded_hash": r["recorded_hash"],
                "recomputed_hash": r["recomputed_hash"],
                "diff": r.get("diff"),
                "atoms_before_divergence": i,
            }
    return {"diverged": False, "atom_count": len(results), "all_match": True}


# ── Report formatting ─────────────────────────────────────────────────────────


def bisect_report(result: dict) -> str:
    """Format a bisect result as a human-readable report.

    Returns
    -------
    str
        Multi-line string.  Divergence: header + diff.  No divergence: summary.
    """
    if result["diverged"]:
        lines = [
            f"DIVERGENCE FOUND at atom {result['atom_id']} (seq {result['seq']})",
            f"Atoms before divergence: {result['atoms_before_divergence']}",
            f"Recorded hash:  {result['recorded_hash']}",
            f"Recomputed hash: {result['recomputed_hash']}",
        ]
        if result.get("diff"):
            lines.append(f"Diff:\n{result['diff']}")
        return "\n".join(lines)
    else:
        return (
            f"ALL ATOMS MATCH — trace is deterministic "
            f"({result['atom_count']} atoms verified)"
        )


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python3 engine/forensics.py``."""
    parser = argparse.ArgumentParser(
        description="Bisect a trace to find the first divergent atom."
    )
    sub = parser.add_subparsers(dest="command")
    bisect_p = sub.add_parser("bisect", help="Find first divergent atom in a trace")
    bisect_p.add_argument(
        "--run", required=True, help="Run ID to bisect"
    )
    bisect_p.add_argument(
        "--db", default="engine.db", help="Database path (default: engine.db)"
    )
    bisect_p.add_argument(
        "--verbose", action="store_true", help="Show input/output diff on mismatch"
    )
    args = parser.parse_args(argv)

    if args.command == "bisect":
        start = time.time()
        result = bisect(args.run, args.db, args.verbose)
        elapsed = time.time() - start
        report = bisect_report(result)
        print(report)
        if elapsed > 1:
            print(f"\n(bisect completed in {elapsed:.1f}s)", file=sys.stderr)
        sys.exit(1 if result["diverged"] else 0)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
