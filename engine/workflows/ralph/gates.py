"""Mechanical gate enforcement for the Ralph loop (S2 §4, ADR-0004).

Each Ralph gate (Dev→Review→Test→RedTeam) is backed by an ACE-native
deterministic or model-separated mechanism. **No LLM self-reporting** (R1):
the ``gate_status`` / ``passed`` field is set by this module based on the
actual execution of ``validator.py``, ``tester.py``, or ``llm_judge.py`` —
never by the orchestrator grading its own homework.

ADR-0004: the LLM Judge's scores feed a *Ralph gate* whose pass/fail the
round driver computes and enforces via post-commit revert. The judge never
scores itself (``judge_port != subject_port`` guard inherited from
``llm_judge.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from engine.workflows.ralph.report_types import GateResult, GateVerdict

log = logging.getLogger("engine.workflows.ralph.gates")


# ---------------------------------------------------------------------------
# Thresholds (S5 #1: 0–10 scale from llm_judge.py)
# ---------------------------------------------------------------------------

REVIEW_PASS_THRESHOLD = 5.0     # overall score ≥ 5.0 → pass
REDTEAM_PASS_THRESHOLD = 5.0    # overall score ≥ 5.0 → pass


# ---------------------------------------------------------------------------
# Dev gate (deterministic — validator.py)
# ---------------------------------------------------------------------------

def dev_gate(code: Any, task: Any, project_path: str) -> GateResult:
    """Run the deterministic Dev gate via ``validator.py``.

    ``code`` is the generated-code object (has ``.files`` dict). Returns a
    ``GateResult`` whose ``passed`` reflects the actual validation outcome —
    **not** an LLM claim.
    """
    try:
        from engine.validator import validate
    except ImportError:
        # validator.py is unavailable — fail closed (R1: mechanical, not LLM).
        return GateResult(gate="dev", passed=False,
                          detail="validator.py import failed")

    try:
        result = validate(code, _build_context(code, task, project_path), task=task)
        passed = bool(getattr(result, "passed", False))
        stages = getattr(result, "stages", [])
        # Build a concise detail from failed stages.
        failures = [s for s in stages if not s.get("passed", True)]
        detail = "; ".join(
            f"{s.get('stage','?')}:{s.get('file','?')}:{s.get('error','')}"
            for s in failures
        )[:4096]
        return GateResult(gate="dev", passed=passed, detail=detail)
    except Exception as e:
        # Deterministic gate crashed → fail closed, record the error.
        return GateResult(gate="dev", passed=False,
                          detail=f"validator crash: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Review gate (LLM judge, model-separated — ADR-0004)
# ---------------------------------------------------------------------------

def review_gate(code_text: str, task: Any, transport, port: int,
                run_id: str = "ralph") -> GateResult:
    """Review gate via the judge model on the configured port (default :8080).

    The verdict is derived from the judge's *score*, not from the orchestrator's
    self-evaluation (R1). The ``model`` field records the judge's identity for
    audit (R4).
    """
    return _judge_gate(code_text, task, transport, port, "review",
                       REVIEW_PASS_THRESHOLD, run_id=run_id)


# ---------------------------------------------------------------------------
# Test gate (deterministic — tester.py)
# ---------------------------------------------------------------------------

def run_test_gate(project_path: str, transport, timeout: int = 180) -> GateResult:
    """Run the deterministic Test gate via ``tester.py``.

    Returns a ``GateResult`` reflecting actual test execution outcome.
    """
    try:
        from engine.tester import run_tests
    except ImportError:
        return GateResult(gate="test", passed=False,
                          detail="tester.py import failed")

    try:
        result = run_tests(project_path, transport, timeout=timeout)
        passed = bool(getattr(result, "passed", False))
        tests_passed = getattr(result, "tests_passed", 0)
        tests_failed = getattr(result, "tests_failed", 0)
        detail = f"passed={tests_passed} failed={tests_failed}"[:4096]
        return GateResult(gate="test", passed=passed, detail=detail)
    except Exception as e:
        return GateResult(gate="test", passed=False,
                          detail=f"tester crash: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# RedTeam gate (LLM judge or oracle, model-separated — ADR-0004)
# ---------------------------------------------------------------------------

def redteam_gate(code_text: str, task: Any, transport, config,
                 run_id: str = "ralph") -> GateResult:
    """RedTeam gate via the judge model (or P6 oracle if escalated).

    With ``redteam_escalate_to_oracle=True`` and a failing judge verdict, the
    P6 oracle (``oracle_escalate.py``) is consulted as a second opinion.
    """
    port = config.redteam_model_port if config else 8080
    result = _judge_gate(code_text, task, transport, port, "redteam",
                         REDTEAM_PASS_THRESHOLD, run_id=run_id)
    # Oracle escalation path.
    if not result.passed and config and getattr(config, "redteam_escalate_to_oracle", False):
        oracle_result = _oracle_redteam(code_text, task, transport, run_id)
        if oracle_result is not None:
            # Oracle overrides: record both in detail.
            result = GateResult(
                gate="redteam",
                passed=oracle_result.passed,
                detail=f"judge={result.detail[:500]}; oracle={oracle_result.detail[:500]}",
                model=oracle_result.model,
                tokens=result.tokens + oracle_result.tokens,
            )
    return result


# ---------------------------------------------------------------------------
# Evidence gate (T08 — deterministic floor + EvidenceScorer, threshold 6.0)
# ---------------------------------------------------------------------------

# EVIDENCE_PASS_THRESHOLD is defined in engine/research/scoring.py (single
# source of truth).  Import it here for the gate; fall back to the spec
# default only if the scorer module is unavailable.
try:
    from engine.research.scoring import EVIDENCE_PASS_THRESHOLD
except ImportError:
    EVIDENCE_PASS_THRESHOLD = 6.0  # mean of 5 evidence dimensions (GRILL-S4 Q8).


def evidence_gate(verdict_text: str, task: Any, transport, config,
                  run_id: str = "ralph") -> GateResult:
    """Evidence gate for research atoms (T08).

    Two-layer per ADR-0004 / EPIC.md:
      1. Deterministic floor — ``EvidenceValidator`` (stages 0-5). Any
         failure → gate fails (the LLM judge never overrides a structural
         failure).
      2. LLM entailment scoring — ``EvidenceScorer`` overall >= 6.0.

    The verdict text is the research verdict document (markers included),
    not code. The gate consumes the scores; the scorer never sets ``passed``.
    """
    question = getattr(task, "description", "") or getattr(task, "title", "") or ""

    # Layer 1: deterministic floor.
    try:
        from engine.research.evidence import EvidenceValidator
    except ImportError:
        return GateResult(gate="evidence", passed=False,
                          detail="EvidenceValidator import failed")
    try:
        ev_result = EvidenceValidator().validate(verdict_text, question=question)
    except Exception as e:
        return GateResult(gate="evidence", passed=False,
                          detail=f"EvidenceValidator crash: {e}")
    if not ev_result.passed:
        fail_names = [c.get("name", "?") for c in ev_result.checks
                      if not c.get("passed", True)]
        return GateResult(gate="evidence", passed=False,
                          detail=f"deterministic floor failed: {fail_names}")

    # Layer 2: LLM entailment scoring (overall >= 6.0).
    try:
        from engine.research.scoring import EvidenceScorer
    except ImportError:
        return GateResult(gate="evidence", passed=False,
                          detail="EvidenceScorer import failed")
    try:
        scorer = EvidenceScorer()
        scores = scorer.score(
            run_id=run_id,
            task_id=getattr(task, "id", "T01"),
            verdict_text=verdict_text,
            evidence_result=ev_result,
            transport=transport,
        )
    except Exception as e:
        return GateResult(gate="evidence", passed=False,
                          detail=f"EvidenceScorer error: {e}")
    overall = float((scores or {}).get("overall", 0.0))
    passed = overall >= EVIDENCE_PASS_THRESHOLD
    detail = f"overall={overall:.2f} threshold={EVIDENCE_PASS_THRESHOLD:.1f}"[:4096]
    return GateResult(gate="evidence", passed=passed, detail=detail,
                      model=(scores or {}).get("judge_model"), tokens=0)


# ---------------------------------------------------------------------------
# Gate sequence (deterministic state machine — S2 §4, T08)
# ---------------------------------------------------------------------------

def evaluate_gate_sequence(code: Any, task: Any, project_path: str,
                           transport, config, run_id: str = "ralph",
                           gate_sequence_type: str = "code") -> GateVerdict:
    """Run the gate sequence for a round. Return combined ``GateVerdict``.

    ``gate_sequence_type`` selects the sequence:

      * ``"code"`` (default) — Dev→Review→Test→RedTeam (unchanged).
      * ``"research"`` — Evidence→Review only (skips Dev/Test/RedTeam,
        which are code-atom gates with no meaning for a verdict document).

    The sequence is mechanically enforced: a failing gate short-circuits the
    rest. The ``passed`` field of each ``GateResult`` is derived from the
    actual execution of the underlying mechanism — never from an LLM's
    self-claim (R1).
    """
    if gate_sequence_type == "research":
        return _evaluate_research_sequence(code, task, transport, config, run_id)
    return _evaluate_code_sequence(code, task, project_path, transport, config, run_id)


def _evaluate_code_sequence(code: Any, task: Any, project_path: str,
                            transport, config, run_id: str) -> GateVerdict:
    """Dev→Review→Test→RedTeam (the original code-atom sequence)."""
    gates: list[GateResult] = []

    # Dev (deterministic).
    code_text = _extract_code_text(code)
    dev = dev_gate(code, task, project_path)
    gates.append(dev)
    if not dev.passed:
        return GateVerdict(overall_pass=False, gates=gates,
                           retry_recommended=True, retry_target="dev")

    # Review (judge model).
    review = review_gate(code_text, task, transport,
                         config.review_model_port if config else 8080,
                         run_id=run_id)
    gates.append(review)
    if not review.passed:
        return GateVerdict(overall_pass=False, gates=gates,
                           retry_recommended=True, retry_target="review")

    # Test (deterministic).
    test = run_test_gate(project_path, transport,
                         timeout=config.round_timeout_s if config else 1800)
    gates.append(test)
    if not test.passed:
        return GateVerdict(overall_pass=False, gates=gates,
                           retry_recommended=True, retry_target="test")

    # RedTeam (judge model or oracle).
    redteam = redteam_gate(code_text, task, transport, config, run_id=run_id)
    gates.append(redteam)
    if not redteam.passed:
        return GateVerdict(overall_pass=False, gates=gates,
                           retry_recommended=False, retry_target="redteam")

    return GateVerdict(overall_pass=True, gates=gates)


def _evaluate_research_sequence(code: Any, task: Any, transport, config,
                                run_id: str) -> GateVerdict:
    """Evidence→Review for research atoms (T08)."""
    gates: list[GateResult] = []
    code_text = _extract_code_text(code)

    # Evidence gate (deterministic floor + EvidenceScorer).
    ev = evidence_gate(code_text, task, transport, config, run_id=run_id)
    gates.append(ev)
    if not ev.passed:
        return GateVerdict(overall_pass=False, gates=gates,
                           retry_recommended=True, retry_target="evidence")

    # Review (judge model).
    review = review_gate(code_text, task, transport,
                         config.review_model_port if config else 8080,
                         run_id=run_id)
    gates.append(review)
    if not review.passed:
        return GateVerdict(overall_pass=False, gates=gates,
                           retry_recommended=True, retry_target="review")

    return GateVerdict(overall_pass=True, gates=gates)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _judge_gate(code_text: str, task: Any, transport, port: int,
                gate_name: str, threshold: float,
                run_id: str = "ralph") -> GateResult:
    """Consume the LLM Judge's score to produce a Ralph gate (ADR-0004)."""
    try:
        from engine.llm_judge import score_task
    except ImportError:
        return GateResult(gate=gate_name, passed=False,
                          detail="llm_judge.py import failed", model=None)

    task_id = getattr(task, "id", "T1")
    task_title = getattr(task, "title", "task")
    try:
        verdict = score_task(
            run_id=run_id,
            task_id=str(task_id),
            task_title=str(task_title),
            code_text=code_text,
            transport=transport,
            save=False,  # Ralph gates are round-scored, not persisted to engine_scores
        )
    except Exception as e:
        # score_task raises if judge_port == subject_port (R4 guard) — that is
        # a real configuration error worth surfacing.
        return GateResult(gate=gate_name, passed=False,
                          detail=f"judge error: {type(e).__name__}: {e}",
                          model=None)

    overall = float(verdict.get("overall", 0.0))
    judge_model = verdict.get("judge_model", f":{port}")
    passed = overall >= threshold
    detail = f"overall={overall:.2f} threshold={threshold:.1f}"[:4096]
    return GateResult(gate=gate_name, passed=passed, detail=detail,
                      model=judge_model, tokens=0)


def _oracle_redteam(code_text: str, task: Any, transport,
                    run_id: str) -> GateResult | None:
    """Consult the P6 oracle for a second opinion on RedTeam."""
    try:
        from engine.orchestrator.oracle_escalate import escalate_task_to_oracle
    except ImportError:
        return None
    try:
        task_id = getattr(task, "id", "T1")
        task_title = getattr(task, "title", "task")
        result = escalate_task_to_oracle(
            run_id=run_id,
            task_id=str(task_id),
            task_title=str(task_title),
            code_text=code_text,
            transport=transport,
        )
        passed = bool(getattr(result, "passed", False))
        model = getattr(result, "model", "p6-oracle")
        return GateResult(gate="redteam", passed=passed,
                          detail="oracle verdict", model=model)
    except Exception as e:
        return GateResult(gate="redteam", passed=False,
                          detail=f"oracle error: {e}", model="p6-oracle")


def _extract_code_text(code: Any) -> str:
    """Extract a single code string from a generated-code object or string."""
    if isinstance(code, str):
        return code
    files = getattr(code, "files", None)
    if isinstance(files, dict) and files:
        return "\n\n".join(files.values())
    return str(code)


def _build_context(code: Any, task: Any, project_path: str) -> Any:
    """Build a minimal context object for the validator."""
    files = getattr(code, "files", {}) if not isinstance(code, str) else {}
    imports: dict[str, list[str]] = {}
    if isinstance(files, dict):
        for fname, src in files.items():
            imports[fname] = _extract_imports(src)
    return _SimpleContext(imports=imports, project_path=project_path,
                          dag_files=list(files.keys()) if isinstance(files, dict) else [])


def _extract_imports(src: str) -> list[str]:
    """Naive import extraction (best-effort; the real validator does AST)."""
    imps: list[str] = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            imps.append(stripped)
    return imps


@dataclass
class _SimpleContext:
    """Minimal context stand-in for the validator when no real context exists."""
    imports: dict[str, list[str]]
    project_path: str | None = None
    dag_files: list[str] | None = None
