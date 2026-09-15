"""EvidenceScorer — LLM judge for research evidence quality.

Scores research evidence by sending it to a judge model via HTTP transport,
parsing the JSON response, and returning dimension scores with an overall
mean. The scorer does NOT set a 'passed' field — that is the gate's
responsibility (ADR-0004).

Mirrors ``engine/llm_judge.py`` score_task() discipline: port guards,
ONE-retry on transient failures, JSON score parsing with fence tolerance,
reasoning-escalation budget doubling.

Persists to ``research_scores`` table (engine.db). The
``EVIDENCE_PASS_THRESHOLD = 6.0`` constant lives here and is consumed by
``engine/workflows/ralph/gates.py`` and ``engine/research/__init__.py``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

log = logging.getLogger("engine.research.scoring")

# --- Threshold ------------------------------------------------------------
# Mean of the 5 evidence dimensions below which the atom FAILS (ADR-0004).
EVIDENCE_PASS_THRESHOLD = 6.0

# --- Dimensions (T05) -----------------------------------------------------
DIMENSIONS_RESEARCH = [
    "citation_coverage",
    "claim_traceability",
    "contradiction_handling",
    "verdict_justification",
    "source_quality",
]

# --- Judge constants (mirror llm_judge.py) --------------------------------
JUDGE_PORT = int(os.environ.get("EPIC5_JUDGE_PORT", "8080"))
SUBJECT_PORT = int(os.environ.get("EPIC5_SUBJECT_PORT", "8082"))

_MAX_TOKENS_JUDGE = 4096
_JUDGE_REASONING_CEILING = 32768
_JUDGE_TEMPERATURE = 0.0
_MAX_ATTEMPTS = 2  # initial + ONE retry


def _build_judge_messages(verdict_text: str, evidence_result: Any = None) -> list[dict]:
    """Build chat messages for the evidence judge call.

    Asks for entailment-grounded scores on the 5 research dimensions.
    """
    # Summarize the evidence result for the judge.
    ev_summary = ""
    if evidence_result is not None:
        checks = getattr(evidence_result, "checks", []) or []
        citations = getattr(evidence_result, "citations", []) or []
        claims = getattr(evidence_result, "claims", []) or []
        sources = getattr(evidence_result, "sources", []) or []
        ev_summary = (
            f"\n\n## Evidence Summary\n"
            f"- Claims: {len(claims)}\n"
            f"- Sources: {len(sources)}\n"
            f"- Citations: {len(citations)}\n"
            f"- Validation stages passed: "
            f"{sum(1 for c in checks if c.get('passed', True))}/{len(checks)}\n"
        )

    prompt = (
        "You are an evidence quality judge. Score the following research "
        "verdict on 5 dimensions.\n\n"
        "## Verdict Document\n"
        f"{verdict_text[:20000]}\n"
        f"{ev_summary}\n\n"
        "## Scoring Rubric\n"
        "Score each dimension INDEPENDENTLY on a 0-10 integer scale, "
        "grounded in textual entailment evidence from the verdict:\n\n"
        "1. **citation_coverage** (0-10): How well are claims backed by "
        "explicit source citations? Every factual claim should have at least "
        "one source edge.\n"
        "2. **claim_traceability** (0-10): Can each claim be traced to a "
        "specific source span? Are evidence spans concrete and verifiable?\n"
        "3. **contradiction_handling** (0-10): Are contradictions between "
        "sources identified and resolved with explicit reasoning?\n"
        "4. **verdict_justification** (0-10): Is the final verdict position "
        "(supported/refuted/inconclusive) justified by the evidence presented?\n"
        "5. **source_quality** (0-10): Are sources authoritative, relevant, "
        "and sufficient for the claims made?\n\n"
        "## Instructions\n"
        "Score each dimension INDEPENDENTLY on a 0-10 integer scale, referencing "
        "specific evidence from the verdict text. A high score requires the "
        "verdict to ENTAIL the quality described — not merely assert it.\n\n"
        "Respond in this EXACT JSON format:\n"
        "{{\n"
        '  "citation_coverage": N,\n'
        '  "claim_traceability": N,\n'
        '  "contradiction_handling": N,\n'
        '  "verdict_justification": N,\n'
        '  "source_quality": N,\n'
        '  "reasoning": "brief justification per dimension"\n'
        "}}\n"
    )
    return [
        {"role": "system", "content":
            "You are a rigorous evidence quality judge. "
            "Do not show your thinking. Respond with ONLY the JSON object."},
        {"role": "user", "content": prompt},
    ]


def _parse_scores(text: str) -> Dict[str, Any]:
    """Parse the judge's JSON reply into a {dim: int} dict.

    Tolerates code fences / surrounding prose (first JSON object). Raises
    ValueError on unparseable output.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in judge reply")
    data = json.loads(text[start:end + 1])

    def _scalar(v):
        if isinstance(v, dict):
            v = v.get("score", 0)
        return int(round(float(v)))

    out: Dict[str, Any] = {}
    for dim in DIMENSIONS_RESEARCH:
        if dim not in data:
            raise ValueError(f"missing dimension '{dim}' in judge reply")
        val = _scalar(data[dim])
        out[dim] = max(0, min(10, val))
    return out


def _zero_scores(error: Optional[str]) -> tuple[Dict[str, int], str]:
    return ({dim: 0 for dim in DIMENSIONS_RESEARCH}, error or "unknown error")


def _judge_reasoning_exhausted(raw: Any) -> bool:
    """Detect reasoning exhaustion in the judge's raw response (P1)."""
    if not isinstance(raw, dict):
        return False
    if "content" in raw:
        content = raw.get("content", "") or ""
        finish_reason = raw.get("finish_reason")
        reasoning = raw.get("reasoning_content") or ""
        thinking = raw.get("thinking_tokens", 0) or 0
    else:
        try:
            choice = (raw.get("choices") or [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "") or ""
            finish_reason = choice.get("finish_reason")
        except Exception:
            return False
        reasoning = ""
        thinking = 0
    return (not content.strip()
            and finish_reason == "length"
            and bool(reasoning or thinking))


class EvidenceScorer:
    """LLM judge for research evidence quality (T05).

    Scores a research verdict document on 5 dimensions (0-10 each),
    computes overall = mean, and persists to ``research_scores``.

    The scorer NEVER sets ``passed`` — that is the gate's responsibility
    (ADR-0004).  ``EVIDENCE_PASS_THRESHOLD`` lives in this module.
    """

    def score(
        self,
        verdict: str,
        task: Any = None,
        transport: Any = None,
        run_id: str = "research-run",
        save: bool = True,
    ) -> Dict[str, Any]:
        """Score one research verdict via the judge model.

        Returns dict: {run_id, task_id, judge_model, scores, overall,
        reasoning, error}. 'overall' is the mean of the 5 dims (0-10 scale).
        Persisted to research_scores (engine.db) when save=True.

        Args:
            verdict: The full verdict markdown text (markers included).
            task: The task object (used for task_id / title).
            transport: HTTP transport with curl_beellama() / get_model_list().
            run_id: Engine run identifier for persistence.
            save: If True, persist to research_scores table.
        """
        if JUDGE_PORT == SUBJECT_PORT:
            raise ValueError(
                f"judge port ({JUDGE_PORT}) == subject port ({SUBJECT_PORT}): "
                "the judge must never be the model under test")

        task_id = getattr(task, "id", "T00") if task else "T00"
        task_title = getattr(task, "title", "research") if task else "research"

        messages = _build_judge_messages(verdict)
        judge_model: Optional[str] = None
        reasoning: Optional[str] = None
        scores: Optional[Dict[str, int]] = None
        error: Optional[str] = None
        judge_max_tokens = _MAX_TOKENS_JUDGE
        judge_escalated = False
        raw = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                raw = transport.curl_beellama(
                    JUDGE_PORT, messages,
                    max_tokens=judge_max_tokens,
                    temperature=_JUDGE_TEMPERATURE,
                )
                if isinstance(raw, dict) and "content" in raw:
                    content = raw.get("content", "")
                else:
                    content = raw["choices"][0]["message"]["content"]
                if judge_model is None:
                    judge_model = (raw.get("model")
                                   if isinstance(raw, dict) else None)
                scores = _parse_scores(content)
                reasoning = content
                error = None
                break
            except Exception as e:  # noqa: BLE001
                if _judge_reasoning_exhausted(raw) and not judge_escalated:
                    judge_max_tokens = min(judge_max_tokens * 2,
                                           _JUDGE_REASONING_CEILING)
                    judge_escalated = True
                error = f"{type(e).__name__}: {e}"
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(2)
                continue

        if scores is None:
            scores, final_err = _zero_scores(error)
            error = final_err
            if judge_model is None:
                judge_model = f":{JUDGE_PORT}"
        elif judge_model is None:
            try:
                models = transport.get_model_list(JUDGE_PORT)
                judge_model = models[0] if models else f":{JUDGE_PORT}"
            except Exception:  # noqa: BLE001
                judge_model = f":{JUDGE_PORT}"

        overall = round(sum(scores.values()) / len(DIMENSIONS_RESEARCH), 2)
        result = {
            "run_id": run_id,
            "task_id": task_id,
            "judge_model": judge_model,
            "scores": scores,
            "overall": overall,
            "reasoning": (reasoning or "")[:4000],
            "error": error,
        }

        if save:
            from engine.state import save_research_scores
            save_research_scores(
                run_id=run_id,
                task_id=task_id,
                judge_model=judge_model or f":{JUDGE_PORT}",
                scores=scores,
                reasoning=result["reasoning"],
                error=error,
            )
        return result
