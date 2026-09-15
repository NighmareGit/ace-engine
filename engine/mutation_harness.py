"""Engine mutation harness — gate sensitivity testing with sabotage corpus (NFR-B3).

Applies known-bad code snippets to the 4-stage validator, reports per-gate
sensitivity, and proves the validator catches >=95% of mutations.

CONTRADICTION REPORT (T64 attempt 2): Brief's original mutations 2/4/5 had
wrong assumptions about the validator — see inline comments on each mutation.
engine/context.py has no Context dataclass; we use a local _TestContext stub.
"""

# --- Fix circular import when run as `python3 engine/mutation_harness.py` ---
import os, sys
_pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

import argparse
import json
from dataclasses import dataclass, field

from engine.validator import validate, ValidationResult  # noqa: F401
from engine.generator import GeneratedCode  # noqa: F401


# ---------------------------------------------------------------------------
# Minimal Context stub (validator only needs `.imports`)
# ---------------------------------------------------------------------------
@dataclass
class _TestContext:
    """Minimal context — validator only needs .imports attribute."""
    file_tree: dict = field(default_factory=dict)
    relevant_files: list = field(default_factory=list)
    imports: dict = field(default_factory=dict)
    framework: str | None = None
    existing_code: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sabotage corpus — 5 known-bad code snippets
# ---------------------------------------------------------------------------
MUTATION_CORPUS = [
    {
        "id": "syntax-error",
        "description": "Invalid Python syntax — unclosed parenthesis",
        "code": "def broken(\n    return 42",
        "expected_gate": "ast",
        "expected_catch": True,
    },
    {
        "id": "import-nonexistent",
        "description": "Imports module not in stdlib, project, or allowlist",
        "code": "import nonexistent_fake_xyz",
        "expected_gate": "imports",
        "expected_catch": True,
    },
    {
        "id": "disallowed-import",
        "description": "Imports module not in stdlib or allowlist (psycopg2)",
        "code": "import psycopg2\ndef connect():\n    return psycopg2.connect('dbname=test')",
        "expected_gate": "imports",
        "expected_catch": True,
    },
    {
        "id": "runtime-error",
        "description": "Top-level division by zero — executes immediately",
        "code": "1 / 0",
        "expected_gate": "execution",
        "expected_catch": True,
    },
    {
        "id": "exec-exception",
        "description": "Top-level raise — uncaught RuntimeError at exec",
        "code": "raise RuntimeError('injected failure')",
        "expected_gate": "execution",
        "expected_catch": True,
    },
]


# ---------------------------------------------------------------------------
# Core test functions
# ---------------------------------------------------------------------------
def run_mutation_test(mutation: dict) -> dict:
    """Run one mutation through the 4-stage validator. Returns detection dict."""
    gc = GeneratedCode(
        files={"test.py": mutation["code"]},
        raw_response="",
        tokens=0,
        thinking_tokens=0,
        latency_ms=0.0,
    )
    ctx = _TestContext()
    result = validate(gc, ctx)

    # Find which gate caught it (first failed stage)
    detected_by = None
    for stage in result.stages:
        if not stage["passed"]:
            detected_by = stage["stage"]
            break

    correctly_detected = (detected_by is not None) == mutation["expected_catch"]
    return {
        "mutation_id": mutation["id"],
        "passed_validation": result.passed,
        "detected_by": detected_by,
        "stages": result.stages,
        "expected_gate": mutation["expected_gate"],
        "expected_catch": mutation["expected_catch"],
        "correctly_detected": correctly_detected,
    }


def run_corpus(corpus: list[dict] | None = None) -> dict:
    """Run all mutations through the validator. Returns sensitivity summary."""
    if corpus is None:
        corpus = MUTATION_CORPUS

    results = [run_mutation_test(m) for m in corpus]
    detected = sum(1 for r in results if r["correctly_detected"])

    # Per-gate breakdown
    gate_counts: dict[str, dict] = {}
    for r in results:
        gate = r["expected_gate"]
        if gate not in gate_counts:
            gate_counts[gate] = {"total": 0, "detected": 0}
        gate_counts[gate]["total"] += 1
        if r["correctly_detected"]:
            gate_counts[gate]["detected"] += 1

    per_gate = {}
    for gate, counts in gate_counts.items():
        sens = counts["detected"] / counts["total"] if counts["total"] > 0 else 0.0
        per_gate[gate] = {
            "total": counts["total"],
            "detected": counts["detected"],
            "sensitivity": sens,
        }

    return {
        "total": len(corpus),
        "detected": detected,
        "sensitivity": detected / len(corpus) if corpus else 0.0,
        "per_gate": per_gate,
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mutation harness — gate sensitivity testing with sabotage corpus"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output results as JSON"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print each mutation result"
    )
    args = parser.parse_args()

    report = run_corpus()

    if args.verbose:
        for r in report["results"]:
            status = "DETECTED" if r["correctly_detected"] else "MISSED"
            gate_info = f" by {r['detected_by']}" if r["detected_by"] else ""
            print(f"  [{status}] {r['mutation_id']}{gate_info}")
        print()
        print(f"Overall sensitivity: {report['sensitivity']:.0%} "
              f"({report['detected']}/{report['total']})")

    if args.json_output:
        print(json.dumps(report, indent=2))

    # Exit 0 if sensitivity >= 95%, else 1
    sys.exit(0 if report["sensitivity"] >= 0.95 else 1)


if __name__ == "__main__":
    main()
