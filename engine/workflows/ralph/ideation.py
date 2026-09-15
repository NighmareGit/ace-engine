"""Bounded divergent ideation for the Ralph loop (S2 §5, S5 #1/#8).

Ralph Phase 1 ("firebrainstorm"): generate N candidate plans, score them on
three dimensions (feasibility, alignment, novelty), select one. Budget-capped.

Novelty is **mechanical** (S5 #1): TF-IDF/trigram overlap vs prior selected
plans, fed to the scoring prompt as ``novelty_signals`` — never LLM-judged.
G12 ideation-collapse detection (S5 #8): fires forced-diversity when the
selected plan's text is too similar (≥0.75 cosine) to any of the last 3 rounds'
plans for 2 consecutive rounds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from engine.workflows.ralph.config import RalphConfig
from engine.workflows.ralph.report_types import IdeationSummary

log = logging.getLogger("engine.workflows.ralph.ideation")


# ---------------------------------------------------------------------------
# Bounds (S2 §5, S5 #1)
# ---------------------------------------------------------------------------

DEFAULT_N = 5
MAX_N = 8
MAX_TOKENS_PER_CANDIDATE = 2048
MAX_IDEATION_TOKENS = 16384  # 8 candidates × 2048


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ScoredPlan:
    """A candidate plan with its 3-axis scores."""
    text: str
    feasibility: float = 5.0
    alignment: float = 5.0
    novelty: float = 5.0

    @property
    def total(self) -> float:
        """Tie-break order: feasibility → alignment → novelty (S5 #1)."""
        # Use a weighted tuple for tie-breaking: feasibility dominates.
        return self.feasibility * 100 + self.alignment * 10 + self.novelty


@dataclass
class IdeationResult:
    """Outcome of one ideation phase."""
    candidates: list[ScoredPlan] = field(default_factory=list)
    selected_idx: int = 0
    selection_reason: str = ""
    forced_diversity: bool = False
    collapse_detected: bool = False

    def to_summary(self) -> IdeationSummary:
        return IdeationSummary(
            candidates_considered=len(self.candidates),
            selected_idx=self.selected_idx,
            selection_reason=self.selection_reason[:2048],
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_candidate_plans(
    objective: str,
    previous_rounds: list[Any],
    n_candidates: int = DEFAULT_N,
    model_port: int = 8082,
    max_tokens_per_candidate: int = MAX_TOKENS_PER_CANDIDATE,
    config: RalphConfig | None = None,
    transport: Any = None,
    run_id: str = "ralph",
) -> IdeationResult:
    """Generate + score N candidate plans. Return scored list + selection.

    Bounded: ``n_candidates`` is clamped to [1, MAX_N]. Novelty is computed
    mechanically via ``plan_similarity`` against prior selected plans. G12
    collapse detection fires forced-diversity when the same solution repeats.
    """
    if config is not None:
        n_candidates = min(n_candidates, config.ideation_candidates)
    n_candidates = max(1, min(n_candidates, MAX_N))

    # Compute novelty signals from prior selected plans (mechanical, S5 #1).
    prior_plans = _collect_prior_plans(previous_rounds)
    novelty_signals = _build_novelty_signals(prior_plans)

    # G3-T03: recall approved memory claims as additional novelty signals
    # (DESIGN-G4 §2 row 11). Wrapped in try/except so ideation never breaks;
    # gated behind enable_memory_recall (DESIGN-G4 §3 flag C).  G4-T02: the
    # flag now lives on RalphConfig (copied from EngineConfig.enable_memory_recall
    # when the ralph loop is launched) — read directly, getattr fallback keeps
    # it safe for callers that pass a config without the field.
    try:
        if getattr(config, "enable_memory_recall", False):
            from engine.memory.recall import recall_for_objective
            recall_result = recall_for_objective(objective)
            if recall_result and recall_result.rendered:
                novelty_signals = (
                    novelty_signals + "\n\n" + recall_result.rendered
                    if novelty_signals else recall_result.rendered
                )
    except Exception as exc:  # noqa: BLE001 — never break ideation
        log.warning("recall injection failed (non-blocking): %s", exc)

    # Generate candidate plan texts.
    candidates_text = _generate_texts(
        objective, n_candidates, model_port, max_tokens_per_candidate,
        transport, run_id=run_id, novelty_signals=novelty_signals,
        prior_plans=prior_plans,
    )

    # Score candidates (one call, JSON contract, one repair retry, then default).
    scored = _score_candidates(
        objective, candidates_text, model_port, transport, run_id=run_id,
        prior_plans=prior_plans,
    )

    # G12 collapse detection (S5 #8).
    forced_diversity, collapse = _detect_collapse(
        scored, previous_rounds, config,
    )

    # Select: highest total, tie-break feasibility→alignment→novelty (S5 #1).
    selected_idx, reason = _select(scored)

    return IdeationResult(
        candidates=scored,
        selected_idx=selected_idx,
        selection_reason=reason,
        forced_diversity=forced_diversity,
        collapse_detected=collapse,
    )


# ---------------------------------------------------------------------------
# Selection (S5 #1: tie-break feasibility → alignment → novelty)
# ---------------------------------------------------------------------------

def _select(scored: list[ScoredPlan]) -> tuple[int, str]:
    """Select the highest-scoring candidate. Returns (idx, reason)."""
    if not scored:
        return 0, "no candidates"
    best_idx = 0
    best = scored[0]
    for i, plan in enumerate(scored[1:], 1):
        if plan.total > best.total:
            best = plan
            best_idx = i
    reason = (
        f"selected #{best_idx}: feasibility={best.feasibility:.1f} "
        f"alignment={best.alignment:.1f} novelty={best.novelty:.1f}"
    )
    return best_idx, reason


# ---------------------------------------------------------------------------
# G12 collapse detection (S5 #8)
# ---------------------------------------------------------------------------

def _detect_collapse(
    scored: list[ScoredPlan],
    previous_rounds: list[Any],
    config: RalphConfig | None,
) -> tuple[bool, bool]:
    """Detect ideation collapse: selected plan too similar to recent plans.

    Returns (forced_diversity, collapse_detected). Trigger: similarity ≥
    ``g12_similarity_threshold`` vs ANY of the last 3 rounds' plans, for
    ``g12_consecutive_rounds`` consecutive rounds.
    """
    if not scored or not previous_rounds:
        return False, False
    threshold = 0.75
    consecutive_needed = 2
    if config is not None:
        threshold = config.g12_similarity_threshold
        consecutive_needed = config.g12_consecutive_rounds

    # Find the selected plan (highest total).
    selected_idx, _ = _select(scored)
    selected_text = scored[selected_idx].text if selected_idx < len(scored) else ""
    if not selected_text.strip():
        return False, False

    from engine.intent.similarity import plan_similarity

    # Check against last 3 rounds' selected plans.
    recent = previous_rounds[-3:]
    consecutive = 0
    for rnd in reversed(recent):
        prior_text = _extract_selected_plan(rnd)
        if not prior_text:
            continue
        sim = plan_similarity(selected_text, prior_text)
        if sim >= threshold:
            consecutive += 1
        else:
            break  # must be consecutive

    collapse = consecutive >= consecutive_needed
    return collapse, collapse


# ---------------------------------------------------------------------------
# Novelty signals (mechanical, S5 #1)
# ---------------------------------------------------------------------------

def _collect_prior_plans(previous_rounds: list[Any]) -> list[str]:
    """Extract selected plan texts from prior round reports."""
    plans: list[str] = []
    for rnd in previous_rounds:
        plan = _extract_selected_plan(rnd)
        if plan:
            plans.append(plan)
    return plans


def _extract_selected_plan(rnd: Any) -> str | None:
    """Extract the selected plan text from a RoundReport or IdeationSummary."""
    if rnd is None:
        return None
    # RoundReport with ideation_summary.
    isummary = getattr(rnd, "ideation_summary", None)
    if isummary is not None:
        idx = getattr(isummary, "selected_idx", 0)
        candidates = getattr(rnd, "candidates", None)
        if candidates and idx < len(candidates):
            return getattr(candidates[idx], "text", str(candidates[idx]))
    # RoundReport with plan field.
    plan = getattr(rnd, "plan", None)
    if plan:
        return plan
    return None


def _build_novelty_signals(prior_plans: list[str]) -> str:
    """Build a novelty-signal string for the scoring prompt."""
    if not prior_plans:
        return "none"
    return " | ".join(prior_plans[-3:])


# ---------------------------------------------------------------------------
# Text generation (mockable)
# ---------------------------------------------------------------------------

def _generate_texts(
    objective: str,
    n: int,
    model_port: int,
    max_tokens: int,
    transport: Any,
    run_id: str = "ralph",
    novelty_signals: str = "none",
    prior_plans: list[str] | None = None,
) -> list[str]:
    """Generate N candidate plan texts.

    If no transport is provided (testing), returns stub candidates.
    Otherwise calls the model on ``model_port``.
    """
    if transport is None:
        # Testing path: generate diverse stub candidates.
        return [
            f"candidate {i}: approach {i} for {objective[:50]}"
            for i in range(n)
        ]
    # Real path: call the model.
    return _call_ideation_model(
        objective, n, model_port, max_tokens, transport, run_id,
        novelty_signals=novelty_signals,
    )


def _call_ideation_model(
    objective: str,
    n: int,
    model_port: int,
    max_tokens: int,
    transport: Any,
    run_id: str,
    novelty_signals: str = "none",
) -> list[str]:
    """Call the ideation model to generate N candidate plans."""
    prompt = (
        f"Generate {n} diverse candidate plans for this objective:\n"
        f"{objective}\n\n"
        f"Avoid these already-tried approaches: {novelty_signals}\n\n"
        "Return a JSON array of strings, one per plan."
    )
    try:
        raw = transport.curl_beellama(
            model_port, [{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=0.8,
        )
        content = raw.get("content", "") if isinstance(raw, dict) else str(raw)
        # Try to parse JSON array.
        try:
            plans = json.loads(content)
            if isinstance(plans, list):
                return [str(p) for p in plans[:n]]
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: split by newlines.
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        return lines[:n] if lines else [f"plan {i}" for i in range(n)]
    except Exception as e:
        log.warning("ideation model call failed: %s", e)
        return [f"fallback plan {i}" for i in range(n)]


# ---------------------------------------------------------------------------
# Scoring (S5 #1: JSON contract, one repair retry, then 5/5/5 default)
# ---------------------------------------------------------------------------

def _score_candidates(
    objective: str,
    candidates_text: list[str],
    model_port: int,
    transport: Any,
    run_id: str = "ralph",
    prior_plans: list[str] | None = None,
) -> list[ScoredPlan]:
    """Score candidates on feasibility, alignment, novelty.

    Novelty is computed mechanically (TF-IDF vs prior plans) and fed to the
    scoring prompt as a signal — the LLM scores feasibility and alignment, but
    novelty is overridden by the mechanical computation (S5 #1).
    """
    n = len(candidates_text)
    if n == 0:
        return []

    # Compute mechanical novelty for each candidate.
    from engine.intent.similarity import plan_similarity
    mechanical_novelty: list[float] = []
    for text in candidates_text:
        if prior_plans:
            # Novelty = 1 - max similarity to any prior plan, scaled 0-10.
            max_sim = max(plan_similarity(text, pp) for pp in prior_plans)
            novelty = (1.0 - max_sim) * 10.0
        else:
            novelty = 5.0  # neutral when no prior plans
        mechanical_novelty.append(round(max(0.0, min(10.0, novelty)), 1))

    if transport is None:
        # Testing path: return default 5/5/5 with mechanical novelty.
        return [
            ScoredPlan(text=text, feasibility=5.0, alignment=5.0,
                       novelty=mechanical_novelty[i])
            for i, text in enumerate(candidates_text)
        ]

    # Build scoring prompt with mechanical novelty signals.
    prompt = _build_scoring_prompt(objective, candidates_text, mechanical_novelty)

    scores = _call_scoring_model(prompt, n, model_port, transport, run_id)
    scored: list[ScoredPlan] = []
    for i, text in enumerate(candidates_text):
        if i < len(scores):
            s = scores[i]
            # Override novelty with mechanical value (S5 #1).
            scored.append(ScoredPlan(
                text=text,
                feasibility=float(s.get("feasibility", 5.0)),
                alignment=float(s.get("alignment", 5.0)),
                novelty=mechanical_novelty[i],
            ))
        else:
            scored.append(ScoredPlan(
                text=text, feasibility=5.0, alignment=5.0,
                novelty=mechanical_novelty[i],
            ))
    return scored


def _build_scoring_prompt(
    objective: str,
    candidates: list[str],
    mechanical_novelty: list[float],
) -> str:
    """Build the scoring prompt with mechanical novelty signals."""
    lines = [
        "Score each candidate plan on feasibility and alignment (0-10 each).",
        f"Objective: {objective}",
        "Novelty is pre-computed mechanically (shown for reference).",
        "Return JSON array of objects: [{\"feasibility\": N, \"alignment\": N}]",
        "",
    ]
    for i, (text, nov) in enumerate(zip(candidates, mechanical_novelty)):
        lines.append(f"Candidate {i}: {text}")
        lines.append(f"  mechanical_novelty={nov:.1f}")
    return "\n".join(lines)


def _call_scoring_model(
    prompt: str,
    n: int,
    model_port: int,
    transport: Any,
    run_id: str,
) -> list[dict]:
    """Call the scoring model. One repair retry on malformed JSON (S5 #1)."""
    for attempt in range(2):  # one retry
        try:
            raw = transport.curl_beellama(
                model_port, [{"role": "user", "content": prompt}],
                max_tokens=2048, temperature=0.2,
            )
            content = raw.get("content", "") if isinstance(raw, dict) else str(raw)
            scores = json.loads(content)
            if isinstance(scores, list) and len(scores) >= n:
                return scores[:n]
            if isinstance(scores, list):
                return scores + [{"feasibility": 5.0, "alignment": 5.0}] * (n - len(scores))
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.warning("ideation scoring parse failed (attempt %d): %s", attempt, e)
            if attempt == 0:
                # One repair retry: ask for valid JSON.
                prompt = "Return valid JSON array: " + prompt
            continue
    # Default 5/5/5 after failed retries (S5 #1).
    return [{"feasibility": 5.0, "alignment": 5.0} for _ in range(n)]
