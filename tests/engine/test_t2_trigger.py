"""T2 tests: orchestrator trigger (trigger.py).

Normalized error signature (stage, error_class); error_class from the
validator's structured classification (NEVER raw strings). Transient class
(timeout/OOM/5xx) is exempt from trigger AND budget.

Fire when:
  (a) attempts >= max(2, ceil(budget/2)) AND same signature >=2 consecutive, OR
  (b) attempts == budget with >=2 distinct signatures of the same error_class.

12 synthetic histories cover: transient-only, alternating-wrong, all-identical,
below-threshold, exhaustion-distinct-class, mixed, etc.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import trigger


# Signatures: (stage, error_class). error_class is the VALIDATOR stage name
# (empty/ast/syntax/imports/execution) or "transient" — never a raw string.
A = ("validation", "syntax")   # e.g. ast/syntax failure on validation
B = ("validation", "import")   # import-resolution failure
C = ("test", "exec")           # execution failure at test time
D = ("commit", "transient")    # transient (timeout/OOM/5xx) — exempt
E = ("validation", "execution")  # execution failure during validation


# ---------------------------------------------------------------------------
# classify_error: maps validator stage -> normalized error_class
# ---------------------------------------------------------------------------

def test_classify_error_maps_validator_stages():
    assert trigger.classify_error("ast") == "syntax"
    assert trigger.classify_error("syntax") == "syntax"
    assert trigger.classify_error("imports") == "import"
    assert trigger.classify_error("execution") == "exec"
    assert trigger.classify_error("empty") == "empty"


def test_classify_error_transient_timeout():
    assert trigger.classify_error("generate", "request timed out after 30s") == "transient"


def test_classify_error_transient_oom():
    assert trigger.classify_error("generate", "CUDA out of memory") == "transient"


def test_classify_error_transient_5xx():
    assert trigger.classify_error("generate", "HTTP 503 Service Unavailable") == "transient"


def test_classify_error_never_returns_raw_string():
    # A raw validator error string carries paths/phrasing — must be classified.
    cls = trigger.classify_error("imports", "Import 'foo/bar/baz.py' not available")
    assert cls in {"empty", "syntax", "import", "exec", "transient"}
    assert "foo/bar/baz.py" not in cls  # raw path must not leak into the class


# ---------------------------------------------------------------------------
# 12 synthetic histories -> expected fire / no-fire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,signatures,budget,expected", [
    # 1. transient-only: all transient -> exempt, never fires
    ("transient_only", [D, D, D, D], 4, False),
    # 2. alternating-wrong: A,B,A,B -> no 2 consecutive same, classes differ
    ("alternating_wrong", [A, B, A, B], 4, False),
    # 3. all-identical: A,A,A -> 2+ consecutive same at/above threshold
    ("all_identical", [A, A, A], 4, True),
    # 4. below threshold, no consecutive: A,B,C (budget 4, threshold 2 but no repeat)
    ("below_threshold_no_repeat", [A, B, C], 4, False),
    # 5. exactly at threshold with consecutive: A,A (budget 4, threshold 2)
    ("at_threshold_consecutive", [A, A], 4, True),
    # 6. exhaustion, 2 distinct signatures same class: A(syntax) + E(execution? no)
    #    A=(validation,syntax), use a same-class distinct sig. We craft below.
    ("exhaustion_distinct_same_class", [A, ("test", "syntax"), A, ("test", "syntax")], 4, True),
    # 7. exhaustion, all distinct classes: A,B,C,('commit','empty') -> no class has 2 distinct
    ("exhaustion_all_distinct_classes", [A, B, C, ("commit", "empty")], 4, False),
    # 8. mixed transient + real trigger: T,A,A -> non-transient [A,A] consecutive
    ("mixed_transient_then_real", [D, A, A], 4, True),
    # 9. single attempt: never fires
    ("single_attempt", [A], 4, False),
    # 10. consecutive at end after non-consecutive: A,B,A,A -> consecutive A,A
    ("consecutive_at_end", [A, B, A, A], 4, True),
    # 11. exhaustion all identical: A,A,A,A -> fires (cond a)
    ("exhaustion_all_identical", [A, A, A, A], 4, True),
    # 12. just under threshold (budget 6 -> threshold 3): A,A -> only 2 consecutive
    ("just_under_threshold", [A, A], 6, False),
])
def test_should_trigger_histories(name, signatures, budget, expected):
    got, reason = trigger.evaluate_trigger(signatures, budget)
    assert got is expected, f"{name}: expected {expected}, got {got} ({reason})"


# ---------------------------------------------------------------------------
# Transient exemption from budget: a run of only transients must not fire
# even at/over budget, and must not count toward the attempt threshold.
# ---------------------------------------------------------------------------

def test_transient_only_never_fires_even_at_budget():
    got, _ = trigger.evaluate_trigger([D, D, D, D, D, D], 4)
    assert got is False


def test_transient_does_not_count_toward_threshold():
    # 1 real + many transients: the lone real attempt is below threshold.
    got, _ = trigger.evaluate_trigger([A, D, D, D, D], 4)
    assert got is False


# ---------------------------------------------------------------------------
# Reason string is populated (feeds telemetry / AC3 evidence).
# ---------------------------------------------------------------------------

def test_reason_populated_on_fire():
    got, reason = trigger.evaluate_trigger([A, A, A], 4)
    assert got is True
    assert isinstance(reason, str) and len(reason) > 0


def test_reason_populated_on_no_fire():
    got, reason = trigger.evaluate_trigger([A], 4)
    assert got is False
    assert isinstance(reason, str) and len(reason) > 0
