"""engine/prd_replay.py — full PRD replay runner + JSON verdict.

Runs a full PRD-level replay using MockTransport and the golden corpus,
emitting a JSON verdict with run_id, atoms, replayed, diverged_at,
gate_sensitivity, and pass.

Rule 6: Traces live in engine.db only — never benchmark-results.db.
"""

# --- Fix circular import when run as `python3 engine/prd_replay.py` ---
import os, sys
_pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

import argparse
import json
import time

from engine.trace import init_atoms_db
from engine.atom_replay import replay_trace
from engine.forensics import bisect, bisect_report
from engine.mutation_harness import run_corpus


# ---------------------------------------------------------------------------
# Core replay API
# ---------------------------------------------------------------------------

def replay_prd(prd_path: str, db_path: str = "engine.db", verbose: bool = False) -> dict:
    """Run full PRD-level replay and return a JSON verdict.

    Steps:
    1. Derive run_id from PRD filename.
    2. Replay all atoms for that run.
    3. Bisect to find first divergence.
    4. Run mutation corpus for gate sensitivity.
    5. Build and return verdict dict.
    """
    run_id = os.path.basename(prd_path).replace(".md", "")
    init_atoms_db(db_path)
    results = replay_trace(run_id, db_path, verbose)
    bisect_result = bisect(run_id, db_path, verbose)
    corpus_result = run_corpus()
    diverged_at = bisect_result.get("atom_id") if bisect_result.get("diverged") else None
    replayed = sum(1 for r in results if r.get("atom_id") and r.get("match"))
    return make_verdict(
        run_id=run_id,
        atoms=len(results),
        replayed=replayed,
        diverged_at=diverged_at,
        gate_sensitivity=corpus_result["sensitivity"],
        passed=(not bisect_result.get("diverged", False) and corpus_result["sensitivity"] >= 0.95),
    )


# ---------------------------------------------------------------------------
# Verdict builder
# ---------------------------------------------------------------------------

def make_verdict(
    run_id: str,
    atoms: int,
    replayed: int,
    diverged_at: str | None,
    gate_sensitivity: float,
    passed: bool,
) -> dict:
    """Build a verdict dict matching the BACKTEST-DESIGN.md schema exactly.

    Returns:
        {"run_id": str, "atoms": int, "replayed": int,
         "diverged_at": str | None, "gate_sensitivity": float, "pass": bool}
    """
    return {
        "run_id": run_id,
        "atoms": atoms,
        "replayed": replayed,
        "diverged_at": diverged_at,
        "gate_sensitivity": gate_sensitivity,
        "pass": passed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python3 engine/prd_replay.py``."""
    parser = argparse.ArgumentParser(
        description="Full PRD-level replay runner with JSON verdict.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prd", help="Path to a golden PRD file (e.g. tests/golden/test-prd.md)")
    group.add_argument("--all", action="store_true",
                       help="Replay all golden PRDs in tests/golden/")
    parser.add_argument("--db", default="engine.db",
                        help="Database path (default: engine.db)")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose output")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output verdict as JSON to stdout")
    args = parser.parse_args(argv)

    start = time.time()

    if args.prd:
        verdict = replay_prd(args.prd, args.db, args.verbose)
        if args.json_output:
            print(json.dumps(verdict, indent=2))
        else:
            status = "PASS" if verdict["pass"] else "FAIL"
            print(f"{status}  run_id={verdict['run_id']}  "
                  f"atoms={verdict['atoms']}  replayed={verdict['replayed']}  "
                  f"diverged_at={verdict['diverged_at']}  "
                  f"sensitivity={verdict['gate_sensitivity']:.0%}")
        sys.exit(0 if verdict["pass"] else 1)

    if args.all:
        golden_dir = "tests/golden"
        if not os.path.isdir(golden_dir):
            print(f"ERROR: golden directory not found: {golden_dir}", file=sys.stderr)
            sys.exit(1)
        prd_files = sorted(
            f for f in os.listdir(golden_dir)
            if f.endswith(".md") and f != "README.md"
        )
        if not prd_files:
            print("No golden PRD files found.", file=sys.stderr)
            sys.exit(1)

        verdicts = []
        for prd_file in prd_files:
            prd_path = os.path.join(golden_dir, prd_file)
            atoms_json = os.path.join(golden_dir, prd_file.replace(".md", ".atoms.json"))
            if not os.path.exists(atoms_json):
                if args.verbose:
                    print(f"SKIP  {prd_file} (no matching .atoms.json)")
                continue
            verdict = replay_prd(prd_path, args.db, args.verbose)
            verdicts.append(verdict)

        elapsed = time.time() - start

        if args.json_output:
            print(json.dumps(verdicts, indent=2))
        else:
            print(f"\n{'='*70}")
            print(f"PRD Replay Summary — {len(verdicts)} PRDs, {elapsed:.1f}s")
            print(f"{'='*70}")
            for v in verdicts:
                status = "PASS" if v["pass"] else "FAIL"
                print(f"  {status}  {v['run_id']:30s}  "
                      f"atoms={v['atoms']:3d}  replayed={v['replayed']:3d}  "
                      f"sensitivity={v['gate_sensitivity']:.0%}")
            all_pass = all(v["pass"] for v in verdicts)
            print(f"\n{'='*70}")
            print(f"Overall: {'ALL PASS' if all_pass else 'SOME FAILED'}")
            print(f"{'='*70}")

        sys.exit(0 if all(v["pass"] for v in verdicts) else 1)


if __name__ == "__main__":
    main()
