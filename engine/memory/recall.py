"""One-way gated recall of approved memory claims (G3-T03).

Read-only retrieval path: ``recall_for_objective`` queries ONLY ``approved``
rows from ``memory_claims`` (never pending/rejected — the poisoning gate is
the exclusive write path). Approved claims relevant to the current objective
are TF-IDF-ranked, budget-trimmed, and rendered with an untrusted-content
marker (prompt-injection defence — recalled claims are inputs to reasoning,
not ground truth).

The write path is ``engine.memory.gate.approve``; this module never mutates
status. That separation enforces the one-way contract: gate writes, recall
reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from engine.intent.similarity import plan_similarity
from engine.state import _get_conn

log = logging.getLogger("engine.memory.recall")


# ---------------------------------------------------------------------------
# Budget + marker constants
# ---------------------------------------------------------------------------

# Default token budget when the caller does not specify one. Chosen to fit
# comfortably inside a single novelty-signals injection without blowing the
# research context window (DESIGN-G4: recall plugs into ideation novelty).
DEFAULT_TOKEN_BUDGET = 2048

# Rough tokens-per-character estimate for budget trimming. Conservative
# (over-estimates) so we stay under budget rather than blow it.
TOKENS_PER_CHAR = 0.25

# Untrusted-content marker (prompt-injection defence). Recalled claims are
# inputs to reasoning, never ground truth — same convention as the retrieval
# chunks from G2-T03.
UNTRUSTED_MARKER = "--- UNTRUSTED MEMORY RECALL CONTENT ---"
UNTRESTED_END_MARKER = "--- END UNTRUSTED MEMORY RECALL CONTENT ---"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RecallResult:
    """Result of a recall query — ranked, budget-trimmed approved claims."""

    objective: str
    token_budget: int
    token_estimate: int = 0
    claims: list[dict] = field(default_factory=list)
    rendered: str = ""

    def __bool__(self) -> bool:
        return len(self.claims) > 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recall_for_objective(
    objective: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> RecallResult:
    """Retrieve approved claims relevant to ``objective``, ranked + budget-trimmed.

    Reads ONLY ``memory_claims`` rows with ``status='approved'`` (one-way gated
    read). Claims are ranked by TF-IDF cosine similarity to the objective and
    trimmed to fit within ``token_budget`` (rank + trim from the bottom, never
    mid-claim truncation).

    Args:
        objective: the current research/ideation objective to rank claims against.
        token_budget: max tokens of recalled content to return. 0 → empty string.

    Returns:
        ``RecallResult`` with the rendered text (empty when nothing approved or
        budget is 0) and the token estimate actually used.
    """
    result = RecallResult(objective=objective, token_budget=token_budget)

    if token_budget <= 0 or not objective or not objective.strip():
        return result

    approved = _fetch_approved_claims()
    if not approved:
        return result

    # Rank by TF-IDF cosine similarity to the objective.
    scored: list[tuple[float, dict]] = []
    for claim in approved:
        text = claim.get("claim_text", "")
        sim = plan_similarity(objective, text) if text.strip() else 0.0
        scored.append((sim, claim))

    # Descending by score; tie-break by claim id for determinism.
    scored.sort(key=lambda item: (-item[0], item[1].get("id", "")))

    # Budget-trim: accumulate claims from the top until the budget is exceeded.
    selected: list[dict] = []
    token_used = 0
    # Account for the markers themselves against the budget (markers count, per
    # G2-T03 convention).
    marker_tokens = int((len(UNTRUSTED_MARKER) + len(UNTRESTED_END_MARKER))
                        * TOKENS_PER_CHAR)
    token_used += marker_tokens

    for sim, claim in scored:
        text = claim.get("claim_text", "")
        claim_tokens = max(int(len(text) * TOKENS_PER_CHAR), 1)
        if token_used + claim_tokens > token_budget and selected:
            break
        entry = {
            "id": claim.get("id", ""),
            "claim_id": claim.get("claim_id", ""),
            "text": text,
            "verdict_position": claim.get("verdict_position", ""),
            "source_atom_id": claim.get("source_atom_id", ""),
            "run_id": claim.get("run_id", ""),
            "score": round(sim, 4),
        }
        selected.append(entry)
        token_used += claim_tokens
        if token_used >= token_budget:
            break

    result.claims = selected
    result.token_estimate = token_used
    result.rendered = _render(selected)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render(claims: list[dict]) -> str:
    """Render approved claims as a budget-trimmed, untrusted-marked string."""
    if not claims:
        return ""
    lines = [UNTRUSTED_MARKER, ""]
    for c in claims:
        position = c.get("verdict_position", "")
        tag = f" [{position}]" if position else ""
        lines.append(f"- (score={c['score']}){tag} {c['text']}")
    lines.append("")
    lines.append(UNTRESTED_END_MARKER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_approved_claims() -> list[dict]:
    """Fetch all ``approved`` claims as dicts for ranking."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, claim_id, text AS claim_text, run_id,
                      verdict_position, source_atom_id
               FROM memory_claims
               WHERE status='approved'
               ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 — read-only, never break the caller
        log.warning("recall fetch failed: %s", exc)
        return []
    finally:
        conn.close()
