"""Atom replay — deterministic hash-check of recorded trace atoms.

In mock mode, ``replay_atom`` recomputes an atom's output hash from its
stored metadata and compares it to the recorded ``output_hash``.  A match
proves determinism; a mismatch signals a regression or corrupt transcript.

``replay_trace`` replays every atom in a run, stopping at the first
divergence.

Rule 6: Traces live in engine.db only — never benchmark-results.db.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys


# ── Helpers ────────────────────────────────────────────────────────────────────


def _hash_dict(d: dict) -> str:
    """Deterministic SHA-256 hex of a dict (keys sorted, default=str).

    Duplicated from trace.py to avoid a circular-import dependency.
    """
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode()
    ).hexdigest()


def compare_hashes(recorded: str, recomputed: str) -> tuple[bool, str | None]:
    """Return ``(match, diff)`` for two hash strings."""
    if recorded == recomputed:
        return True, None
    return False, f"RECORDED: {recorded}\nRECOMPUTED: {recomputed}"


# ── Core replay API ────────────────────────────────────────────────────────────


def replay_atom(atom: dict, verbose: bool = False) -> dict:
    """Replay a single atom and check hash determinism.

    Parameters
    ----------
    atom : dict
        An atom row from ``AtomRecorder.get_trace()``.
    verbose : bool
        If ``True`` and there is a mismatch, include a ``diff`` string with
        the full atom dump.

    Returns
    -------
    dict
        ``{"atom_id", "match", "recorded_hash", "recomputed_hash", "diff"}``
    """
    recorded_hash = atom["output_hash"]

    # Recompute from stored metadata — in full integration this would
    # re-run the actual pipeline step via mock transport.
    recomputed_hash = _hash_dict({
        "from_state": atom["from_state"],
        "to_state": atom["to_state"],
        "meta": json.loads(atom["meta_json"]),
    })

    match, diff = compare_hashes(recorded_hash, recomputed_hash)

    if verbose and not match:
        diff = (
            f"RECORDED: {recorded_hash}\n"
            f"RECOMPUTED: {recomputed_hash}\n"
            f"ATOM: {json.dumps(atom, indent=2)}"
        )

    return {
        "atom_id": atom["atom_id"],
        "match": match,
        "recorded_hash": recorded_hash,
        "recomputed_hash": recomputed_hash,
        "diff": diff,
    }


def replay_trace(
    run_id: str, db_path: str = "engine.db", verbose: bool = False
) -> list[dict]:
    """Replay every atom in a run, stopping at the first divergence.

    Parameters
    ----------
    run_id : str
        The run to replay.
    db_path : str
        Path to the engine SQLite database.
    verbose : bool
        Forwarded to ``replay_atom``.

    Returns
    -------
    list[dict]
        One result per atom (in ``seq`` order).  If a divergence is found,
        a ``{"stopped_at": atom_id}`` sentinel is appended and no further
        atoms are replayed.
    """
    # Lazy import to avoid circular dependency — trace.py is the canonical
    # owner of AtomRecorder, but we only need get_trace here.
    from engine.trace import AtomRecorder

    rec = AtomRecorder(run_id, db_path)
    try:
        atoms = rec.get_trace(run_id)
    finally:
        rec.close()

    results: list[dict] = []
    for atom in atoms:
        result = replay_atom(atom, verbose)
        results.append(result)
        if not result["match"]:
            results.append({"stopped_at": result["atom_id"]})
            break

    return results


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python3 engine/atom_replay.py``."""
    parser = argparse.ArgumentParser(
        description="Replay atoms and check hash determinism."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--atom", help="Atom ID to replay (e.g. run-001-0003)")
    group.add_argument("--run", help="Run ID to replay (full trace)")
    parser.add_argument("--db", default="engine.db", help="Database path")
    parser.add_argument(
        "--verbose", action="store_true", help="Show input/output diff on mismatch"
    )
    args = parser.parse_args(argv)

    if args.atom:
        conn = sqlite3.connect(args.db)
        row = conn.execute(
            "SELECT atom_id, run_id, task_id, seq, from_state, to_state, "
            "input_hash, output_hash, gate_hash, duration_ms, meta_json, "
            "created_at "
            "FROM atoms WHERE atom_id = ?",
            (args.atom,),
        ).fetchone()
        conn.close()

        if row is None:
            print(f"ERROR: atom {args.atom} not found in {args.db}", file=sys.stderr)
            sys.exit(1)

        cols = [
            "atom_id", "run_id", "task_id", "seq", "from_state", "to_state",
            "input_hash", "output_hash", "gate_hash", "duration_ms",
            "meta_json", "created_at",
        ]
        atom = dict(zip(cols, row))
        result = replay_atom(atom, args.verbose)
        if result["match"]:
            print(f"PASS {result['atom_id']}")
        else:
            print(f"FAIL {result['atom_id']} {result['diff']}")
            sys.exit(1)

    elif args.run:
        results = replay_trace(args.run, args.db, args.verbose)
        has_divergence = False
        for r in results:
            if "stopped_at" in r:
                continue
            if r["match"]:
                print(f"PASS {r['atom_id']}")
            else:
                print(f"FAIL {r['atom_id']} {r['diff']}")
                has_divergence = True
        if has_divergence:
            sys.exit(1)


if __name__ == "__main__":
    main()
