"""Cross-run contradiction comparator (G3-T01).

``CrossRunComparator.compare(new_sidecar, stored_claims)`` detects contradiction
candidates between a freshly-produced verdict sidecar and the set of already
stored claims. Three deterministic, LLM-free matching layers (in precedence
order):

  1. Negation-pair heuristic — imports ``_NEG_PAIRS`` from
     ``engine.research.evidence`` (single source of truth, not duplicated).
     Catches syntactic opposites with divergent vocabulary. Highest label
     precedence: if a negation pair matches, ``kind="negation_pair"``
     regardless of TF-IDF sim.
  2. TF-IDF cosine clustering — reuses ``engine.intent.similarity.plan_similarity``
     (word 1-2grams). Score >= ``SIM_THRESHOLD`` AND position disagreement →
     candidate pair. Agreeing claims (same ``verdict_position``) are NOT
     contradictions — two runs concluding the same thing is consensus.
  3. Verdict-position conflict — same ``source_atom_id`` question but opposite
     ``verdict_position`` (supported vs refuted) flags regardless of textual
     similarity. Catches same-question conclusion flips.

Self-same-run pairs are filtered (a run never contradicts itself).

Pure functions — no LLM, no network, no IO in the matching itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from engine.intent.similarity import plan_similarity
from engine.research.evidence import _NEG_PAIRS


# ---------------------------------------------------------------------------
# Thresholds — module constants with rationale
# ---------------------------------------------------------------------------

# TF-IDF cosine threshold for "same topic" candidate pairs.
# Rationale: 0.60 is the same primitive the evidence oracle stage 5 uses
# (evidence.py). At >= 0.60 the two claim texts share enough unigram/bigram
# overlap to be plausibly about the same proposition. Below this the texts
# are effectively disjoint and a contradiction is unlikely.
SIM_THRESHOLD = 0.60

# Word tokenizer for the negation-pair shared-keyword check.
_WORD_RE = re.compile(r"[a-z]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Contradiction:
    """One detected contradiction candidate between two claims.

    Field names follow the G3-T01 ticket contract (claim ids + run refs,
    not the human-readable claim text — callers join on id for display).
    """

    claim_a_id: str
    claim_b_id: str
    run_ref_a: str
    run_ref_b: str
    similarity: float
    kind: str  # "negation_pair" | "verdict_conflict" | "semantic_overlap"


class CrossRunComparator:
    """Stateless comparator — three-layer deterministic contradiction detector.

    Holds no state; ``compare`` is a pure function of its inputs so tests can
    call it without any DB or sidecar IO.
    """

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @staticmethod
    def compare(new_sidecar: dict, stored_claims: list[dict]) -> list[Contradiction]:
        """Compare new verdict claims against stored claims → contradiction candidates.

        Args:
            new_sidecar: freshly-produced verdict sidecar dict, schema
                ``{"claims": [{"id", "text", "source_ids", ...}], ...}``.
                The ``"claims"`` key may be absent/empty → returns ``[]``.
            stored_claims: rows from ``memory_claims`` (or equivalent dicts)
                each carrying at least ``id``, ``claim_text``/``text``,
                ``run_id``, ``verdict_position``, ``source_atom_id``.

        Returns:
            List of ``Contradiction`` candidates (possibly empty). Self-same-run
            pairs are excluded.
        """
        contradictions: list[Contradiction] = []

        new_claims = new_sidecar.get("claims", []) if new_sidecar else []
        if not new_claims or not stored_claims:
            return contradictions

        for new_claim in new_claims:
            new_text = new_claim.get("text", "")
            new_id = new_claim.get("id", "")
            new_run = new_claim.get("run_id", "")
            new_position = new_claim.get("verdict_position", "")
            new_atom = new_claim.get("source_atom_id", "")

            for stored in stored_claims:
                stored_text = stored.get("claim_text") or stored.get("text", "")
                stored_id = stored.get("id", "")
                stored_run = stored.get("run_id", "")
                stored_position = stored.get("verdict_position", "")
                stored_atom = stored.get("source_atom_id", "")

                # Layer 0: never flag a run against itself.
                if new_run and stored_run and new_run == stored_run:
                    continue
                # Also skip identical claim ids (same logical claim re-submitted).
                if new_id and stored_id and new_id == stored_id:
                    continue

                sim = plan_similarity(new_text, stored_text)

                # Layer 1 — negation-pair heuristic (highest label precedence).
                # If a negation pair is matched, kind=negation_pair regardless of
                # TF-IDF sim — the cap was meant to prevent layer 1 from also
                # claiming the pair, not to suppress the negation label.
                if CrossRunComparator._is_negation_pair(new_text, stored_text):
                    contradictions.append(Contradiction(
                        claim_a_id=new_id,
                        claim_b_id=stored_id,
                        run_ref_a=new_run,
                        run_ref_b=stored_run,
                        similarity=round(sim, 4),
                        kind="negation_pair",
                    ))
                    continue  # negation_pair owns this pair; don't double-flag.

                # Layer 2 — TF-IDF semantic overlap.
                # Agreeing claims (same verdict_position) are NOT contradictions —
                # two runs independently concluding the same thing is consensus,
                # not conflict. Only flag when positions disagree (or one side
                # lacks a position, in which case the high-sim pair is still a
                # candidate for review).
                positions_agree = (
                    new_position and stored_position
                    and new_position == stored_position
                )
                if sim >= SIM_THRESHOLD and not positions_agree:
                    contradictions.append(Contradiction(
                        claim_a_id=new_id,
                        claim_b_id=stored_id,
                        run_ref_a=new_run,
                        run_ref_b=stored_run,
                        similarity=round(sim, 4),
                        kind="semantic_overlap",
                    ))
                    continue  # layer 2 owns this pair; don't double-flag.

                # Layer 3 — verdict-position conflict (same question, opposite
                # conclusion). Independent of textual similarity.
                if (new_atom and stored_atom and new_atom == stored_atom
                        and new_position and stored_position
                        and new_position != stored_position
                        and CrossRunComparator._positions_opposite(
                            new_position, stored_position)):
                    contradictions.append(Contradiction(
                        claim_a_id=new_id,
                        claim_b_id=stored_id,
                        run_ref_a=new_run,
                        run_ref_b=stored_run,
                        similarity=round(sim, 4),
                        kind="verdict_conflict",
                    ))

        return contradictions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_negation_pair(text_a: str, text_b: str) -> bool:
        """Negation-pair heuristic (imports ``_NEG_PAIRS``, does not duplicate).

        Returns True when one text contains the positive member and the other
        the negative member of a known pair AND they share at least one
        non-negation keyword. The shared-keyword guard prevents false matches
        between unrelated sentences that happen to contain "always"/"never".
        """
        ta = (text_a or "").lower()
        tb = (text_b or "").lower()
        for pos, neg in _NEG_PAIRS:
            if (pos in ta and neg in tb) or (neg in ta and pos in tb):
                # Shared keyword check (excluding the negation tokens themselves).
                words_a = set(_WORD_RE.findall(ta)) - {pos, neg}
                words_b = set(_WORD_RE.findall(tb)) - {pos, neg}
                if words_a & words_b:
                    return True
        return False

    @staticmethod
    def _positions_opposite(a: str, b: str) -> bool:
        """True when two verdict positions are mutually exclusive."""
        a_norm = a.strip().lower()
        b_norm = b.strip().lower()
        # supported vs refuted are the canonical opposite pair; inconclusive
        # does not contradict either.
        opposites = ({'supported', 'refuted'},)
        return a_norm != b_norm and {a_norm, b_norm} in opposites
