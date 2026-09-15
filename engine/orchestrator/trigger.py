"""Orchestrator trigger — normalized error-signature escalation (T2).

Error identity is a normalized signature ``(stage, error_class)`` where
error_class comes from the validator's structured classification
(empty/ast/syntax/imports/execution), NEVER the raw error string (which
carries file paths + model phrasing). Transient errors (timeout, GPU OOM,
HTTP 5xx) are classed ``transient`` and are exempt from BOTH the trigger
and the budget.

Fire when:
  (a) attempts >= max(2, ceil(budget/2)) AND the same signature appears
      >=2 times consecutively, OR
  (b) attempts == budget AND there are >=2 distinct signatures sharing
      the same error_class (catches different-but-equally-wrong).

``attempts`` counts only non-transient attempts — transients are excluded
entirely (they neither advance the threshold nor burn budget).
"""

import math

# Validator stage -> normalized error class. The validator's stage names are
# the structured classification; we collapse ast/syntax into one "syntax" class.
_STAGE_CLASS = {
    "empty": "empty",
    "ast": "syntax",
    "syntax": "syntax",
    "imports": "import",
    "execution": "exec",
}

# Substrings (lowercased) in an error message that mark a transient error.
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "time out",
    "out of memory", "oom",
    "500", "502", "503", "504",
    "connectionerror", "connection refused", "temporary failure",
)


def _is_transient_message(error_msg: str) -> bool:
    """True if error_msg reads as a transient (timeout/OOM/5xx) error."""
    msg = (error_msg or "").lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def classify_error(stage: str, error_msg: str = "") -> str:
    """Map (validator stage, raw error) -> normalized error class.

    Returns one of {empty, syntax, import, exec, transient}. Never returns
    the raw error string, so file paths and model phrasing can't leak into
    the signature.
    """
    if _is_transient_message(error_msg):
        return "transient"
    return _STAGE_CLASS.get(stage, stage)


def make_signature(stage: str, error_class: str) -> tuple[str, str]:
    """Build a normalized (stage, error_class) signature."""
    return (stage, error_class)


def evaluate_trigger(signatures: list[tuple[str, str]], budget: int) -> tuple[bool, str]:
    """Decide whether to fire the re-plan trigger.

    Args:
        signatures: ordered list of (stage, error_class) per attempt.
        budget: retry budget (max attempts) for the task.

    Returns:
        (should_fire, reason) — reason is a human-readable explanation that
        feeds telemetry / AC3 evidence.
    """
    # Transients are exempt from trigger AND budget: drop them entirely.
    non_transient = [(stage, cls) for (stage, cls) in signatures if cls != "transient"]
    attempts = len(non_transient)

    threshold = max(2, math.ceil(budget / 2))

    # (a) Mid-budget: enough attempts AND a repeated identical signature.
    if attempts >= threshold:
        for i in range(len(non_transient) - 1):
            if non_transient[i] == non_transient[i + 1]:
                sig = non_transient[i]
                return (True, f"repeated signature {sig} (>=2 consecutive) "
                              f"at attempt {attempts} >= threshold {threshold}")

    # (b) Exhaustion: budget spent with >=2 distinct signatures of one class
    # (different-but-equally-wrong).
    if attempts >= budget:
        by_class: dict[str, set] = {}
        for stage, cls in non_transient:
            by_class.setdefault(cls, set()).add((stage, cls))
        for cls, sigs in by_class.items():
            if len(sigs) >= 2:
                return (True, f"budget exhausted ({attempts}>={budget}) with "
                              f"{len(sigs)} distinct signatures of class '{cls}'")

    return (False, f"no trigger: {attempts} non-transient attempt(s), "
                   f"budget {budget}, threshold {threshold}")
