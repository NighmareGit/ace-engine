"""Epic-5 LLM judge — 5-dimension code scoring via the judge model.

Scores generated task code on completeness, correctness, quality,
intelligence, role_fit (0-10 each) using the judge model on :8080
(pinned, never the model under test on :8082). Results persist to the
engine_scores table in engine.db (Rule 6: never benchmark-results.db).

Prompt/rubric assets are imported from the top-level judge.py module
(single source of truth) — but its benchmark-results.db default is
NEVER used here; DB writes go through engine.state.save_engine_scores.

Retry discipline (ACE-RUNBOOK): max ONE retry on transient failures
(timeout / 5xx / malformed JSON). A bad score is final data — a parse
failure records zeros + an error string, never a reroll trigger.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Judge / subject port constants — pinned explicitly, never defaulted away
JUDGE_PORT = int(os.environ.get("EPIC5_JUDGE_PORT", "8080"))
SUBJECT_PORT = int(os.environ.get("EPIC5_SUBJECT_PORT", "8082"))

DIMENSIONS = ["completeness", "correctness", "quality", "intelligence", "role_fit"]

_MAX_TOKENS_JUDGE = 4096  # live evidence: thinking-style models exhaust 1024
                          # on reasoning before emitting any content
# P1: judge reasoning-escalation ceiling (owner override — long contexts fine).
# First call at _MAX_TOKENS_JUDGE; on exhaustion the ONE retry doubles the
# budget, capped at _JUDGE_REASONING_CEILING.
_JUDGE_REASONING_CEILING = 32768
_JUDGE_TEMPERATURE = 0.0
_JUDGE_TIMEOUT_S = 120
_MAX_ATTEMPTS = 2  # initial + ONE retry

# Import prompt/rubric assets from the top-level judge module (read-only use)
_top = None


def _top_level_judge():
    """Import the top-level judge.py module (prompt + rubric assets).

    Raises RuntimeError if only the DB default differs — we never touch it.
    """
    global _top
    if _top is None:
        harness_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if harness_dir not in sys.path:
            sys.path.insert(0, harness_dir)
        import judge as _judge_mod  # top-level judge.py
        _top = _judge_mod
    return _top


# 5-dimension judge prompt — mirrors the top-level judge.py template's
# structure/rubric anchors but includes correctness (the benchmark template
# delegates correctness to execution; the engine has no separate execution
# lane per task, so the LLM judges it from test results embedded in code_text)
JUDGE_PROMPT_5DIM = """You are a code quality judge. Score the following generated code on {num_dimensions} dimensions.

## Task
{task_prompt}

## Expected Behavior
{expected_behavior}

## Generated Code
{response}

## Scoring Rubric
{rubric}

## Instructions
Score each dimension INDEPENDENTLY on a 0-10 integer scale, referencing
specific evidence from the code.

Respond in this EXACT JSON format:
{{
  "completeness": N,
  "correctness": N,
  "quality": N,
  "intelligence": N,
  "role_fit": N
}}
"""


def build_judge_messages(task_title, code_text, role="coder",
                         expected_behavior="", task_prompt=""):
    """Build chat messages for the judge call.

    Uses the shared rubric anchors extracted from the top-level judge module's
    rubric_anchors.md (single source of truth for anchors) with a 5-dimension
    prompt contract. Kept as a function so tests can assert content offline.
    """
    top = _top_level_judge()
    rubric = ""
    try:
        probe = top.JudgeScorer.__new__(top.JudgeScorer)
        probe.rubric = open(os.path.join(
            os.path.dirname(top.__file__), "rubric_anchors.md")).read()
        rubric = probe._extract_rubric_section(DIMENSIONS)
    except Exception:
        rubric = ""  # rubric is enrichment, not a hard dependency
    prompt = JUDGE_PROMPT_5DIM.format(
        num_dimensions=len(DIMENSIONS),
        task_prompt=task_prompt or task_title,
        expected_behavior=expected_behavior or "(see task)",
        response=code_text[:20000],  # blinding: content only, capped
        rubric=rubric or "(no rubric anchors loaded)",
    )
    return [
        {"role": "system", "content":
            f"Task role: {role}. Task: {task_title}. "
            "Do not show your thinking. Respond with ONLY the JSON object."},
        {"role": "user", "content": prompt},
    ]


def _parse_scores(text):
    """Parse the judge's JSON reply into a {dim: int} dict.

    Tolerates code fences / surrounding prose (first JSON object) and both
    the flat Epic-5 format and the benchmark's nested {"scores": {...}}
    format with {"score": N, "justification": ...} values. Raises
    ValueError on unparseable output.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in judge reply")
    data = json.loads(text[start:end + 1])
    if "scores" in data and isinstance(data["scores"], dict):
        data = data["scores"]  # nested benchmark format

    def _scalar(v):
        if isinstance(v, dict):
            v = v.get("score", 0)
        return int(round(float(v)))

    out = {}
    for dim in DIMENSIONS:
        if dim not in data:
            raise ValueError(f"missing dimension '{dim}' in judge reply")
        val = _scalar(data[dim])
        out[dim] = max(0, min(10, val))
    return out


def _zero_scores(error):
    return ({dim: 0 for dim in DIMENSIONS}, error)


def _judge_reasoning_exhausted(raw) -> bool:
    """Detect reasoning exhaustion in the judge's raw response (P1).

    Tolerates the transport's flat dict ("content"/"finish_reason" at top
    level) and an OpenAI-style payload ("choices"[0]...).
    """
    if not isinstance(raw, dict):
        return False
    if "content" in raw:
        content = raw.get("content", "") or ""
        finish_reason = raw.get("finish_reason")
        reasoning = raw.get("reasoning_content") or ""
        thinking = raw.get("thinking_tokens", 0) or 0
    else:
        # OpenAI-style: choices[0]["message"]["content"], ["finish_reason"]
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


def score_task(run_id, task_id, task_title, code_text, transport, role="coder",
               save=True, scored_state=None):
    """Score one task's generated code via the judge model on :8080.

    Returns dict: {run_id, task_id, judge_model, scores, overall, reasoning,
    error, scored_state}. 'overall' is the mean of the 5 dims (0-10 scale).
    Persisted to engine_scores (engine.db) when save=True.

    Args:
        scored_state: P5 — state code the code was in when scored:
            "validation_failed" | "test_failed" | "committed".
    """
    if JUDGE_PORT == SUBJECT_PORT:
        raise ValueError(
            f"judge port ({JUDGE_PORT}) == subject port ({SUBJECT_PORT}): "
            "the judge must never be the model under test")

    messages = build_judge_messages(task_title, code_text, role=role)
    judge_model = None
    reasoning = None
    scores = None
    error = None
    # P1: judge-side reasoning escalation. First call at _MAX_TOKENS_JUDGE; on
    # exhaustion the ONE retry doubles the budget, capped at the ceiling.
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
            # Two shapes tolerated: transport's flat dict ("content") and a
            # raw OpenAI-style payload ("choices"[0]["message"]["content"])
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
        except Exception as e:  # noqa: BLE001 — transient OR final; capped below
            # P1: on reasoning exhaustion, escalate budget for the ONE retry.
            if (_judge_reasoning_exhausted(raw) and not judge_escalated):
                judge_max_tokens = min(judge_max_tokens * 2,
                                       _JUDGE_REASONING_CEILING)
                judge_escalated = True
            error = f"{type(e).__name__}: {e}"
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2)  # brief backoff before the ONE retry
            continue

    if scores is None:
        scores, final_err = _zero_scores(error)
        error = final_err
        if judge_model is None:
            judge_model = f":{JUDGE_PORT}"
    elif judge_model is None:
        # flat transport dict carries no model id — resolve via /v1/models
        try:
            models = transport.get_model_list(JUDGE_PORT)
            judge_model = models[0] if models else f":{JUDGE_PORT}"
        except Exception:  # noqa: BLE001 — identity is audit info, not critical
            judge_model = f":{JUDGE_PORT}"

    overall = round(sum(scores.values()) / len(DIMENSIONS), 2)
    result = {
        "run_id": run_id,
        "task_id": task_id,
        "judge_model": judge_model,
        "scores": scores,
        "overall": overall,
        "reasoning": (reasoning or "")[:4000],
        "error": error,
        "scored_state": scored_state,                 # P5
    }

    if save:
        from engine.state import save_engine_scores
        save_engine_scores(run_id, task_id, judge_model, scores,
                           reasoning=result["reasoning"], error=error,
                           scored_state=scored_state)
    return result
