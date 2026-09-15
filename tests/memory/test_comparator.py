"""Tests for engine/memory/comparator.py (G3-T04).

Deterministic, LLM-free, no IO. All cases build sidecar/stored dicts inline.
"""

from __future__ import annotations

import pytest

from engine.memory.comparator import Contradiction, CrossRunComparator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claim(cid: str, text: str, run: str = "r1",
           position: str = "supported", atom: str = "a1") -> dict:
    return {
        "id": cid, "text": text, "run_id": run,
        "verdict_position": position, "source_atom_id": atom,
    }


def _stored(cid: str, text: str, run: str = "r1",
            position: str = "supported", atom: str = "a1") -> dict:
    # Stored claims use ``claim_text`` (DB column) rather than ``text``.
    return {
        "id": cid, "claim_text": text, "run_id": run,
        "verdict_position": position, "source_atom_id": atom,
    }


# ---------------------------------------------------------------------------
# Contradiction detection cases
# ---------------------------------------------------------------------------

def test_negation_pair_detected():
    """Negation pair ('always' vs 'never') with shared keyword → negation_pair."""
    sidecar = {"claims": [_claim("c1", "X always increases yield", run="r1")]}
    stored = [_stored("c2", "X never increases yield", run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert len(result) == 1
    assert result[0].kind == "negation_pair"
    assert result[0].claim_a_id == "c1"
    assert result[0].claim_b_id == "c2"


def test_verdict_conflict_detected():
    """Same source_atom_id, opposite position (supported vs refuted) → verdict_conflict."""
    sidecar = {"claims": [_claim("c1", "topic Z discussion", run="r1",
                                    position="supported", atom="atom-shared")]}
    stored = [_stored("c2", "different wording about Z", run="r2",
                       position="refuted", atom="atom-shared")]
    result = CrossRunComparator.compare(sidecar, stored)
    kinds = [r.kind for r in result]
    assert "verdict_conflict" in kinds


def test_semantic_overlap_detected():
    """High TF-IDF similarity (near-duplicate), opposite positions → semantic_overlap."""
    sidecar = {"claims": [_claim("c1",
        "the quick brown fox jumps over the lazy dog repeatedly", run="r1",
        position="supported")]}
    stored = [_stored("c2",
        "the quick brown fox jumps over the lazy dog repeatedly", run="r2",
        position="refuted")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert len(result) == 1
    assert result[0].kind == "semantic_overlap"
    assert result[0].similarity >= 0.9


def test_non_contradiction_low_similarity():
    """Low similarity, no negation, same position → empty list."""
    sidecar = {"claims": [_claim("c1", "alpha beta gamma delta", run="r1")]}
    stored = [_stored("c2", "epsilon zeta eta theta iota", run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert result == []


def test_self_same_run_filtered():
    """Both claims from the same run_id → filtered (no self-contradiction)."""
    sidecar = {"claims": [_claim("c1", "X always increases yield", run="r-same")]}
    stored = [_stored("c2", "X never increases yield", run="r-same")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert result == []


def test_empty_sidecar_no_crash():
    """Empty sidecar → empty list, no crash."""
    stored = [_stored("c2", "some claim", run="r2")]
    assert CrossRunComparator.compare({}, stored) == []
    assert CrossRunComparator.compare({"claims": []}, stored) == []
    assert CrossRunComparator.compare(None, stored) == []


def test_empty_stored_no_crash():
    """No stored claims → empty list."""
    sidecar = {"claims": [_claim("c1", "some claim", run="r1")]}
    assert CrossRunComparator.compare(sidecar, []) == []


# ---------------------------------------------------------------------------
# Contradiction dataclass
# ---------------------------------------------------------------------------

def test_contradiction_dataclass_fields():
    """Contradiction carries all required fields (G3-T01 contract)."""
    c = Contradiction(
        claim_a_id="a", claim_b_id="b",
        run_ref_a="r1", run_ref_b="r2",
        similarity=0.82, kind="negation_pair",
    )
    assert c.claim_a_id == "a"
    assert c.claim_b_id == "b"
    assert c.run_ref_a == "r1"
    assert c.run_ref_b == "r2"
    assert c.similarity == 0.82
    assert c.kind == "negation_pair"


# ---------------------------------------------------------------------------
# Negation-pair specifics
# ---------------------------------------------------------------------------

def test_negation_pair_is_vs_is_not():
    """'is' vs 'is not' with shared keyword → detected (negation_pair or
    semantic_overlap depending on TF-IDF; the pair is always flagged)."""
    sidecar = {"claims": [_claim("c1", "the model is accurate", run="r1")]}
    stored = [_stored("c2", "the model is not accurate", run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    # TF-IDF sim ~= 0.615 → caught as semantic_overlap (layer 1 owns it), but
    # the pair is always detected by some layer.
    assert len(result) >= 1


def test_negation_pair_increases_vs_decreases():
    """'increases' vs 'decreases' with shared keyword → negation_pair."""
    sidecar = {"claims": [_claim("c1", "memory usage increases latency", run="r1")]}
    stored = [_stored("c2", "memory usage decreases latency", run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert any(r.kind == "negation_pair" for r in result)


def test_negation_pair_no_shared_keyword_no_match():
    """Negation tokens present but NO shared keyword → not flagged (guard works)."""
    sidecar = {"claims": [_claim("c1", "alpha always beta", run="r1")]}
    stored = [_stored("c2", "gamma never delta", run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert result == []


def test_verdict_conflict_inconclusive_does_not_fire():
    """inconclusive vs supported is NOT an opposite pair → no verdict_conflict."""
    sidecar = {"claims": [_claim("c1", "vague wording one", run="r1",
                                    position="supported", atom="atom-q")]}
    stored = [_stored("c2", "vague wording two", run="r2",
                       position="inconclusive", atom="atom-q")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert not any(r.kind == "verdict_conflict" for r in result)


def test_multiple_claims_all_pairs_evaluated():
    """Two new claims × three stored → every pair is evaluated."""
    sidecar = {"claims": [
        _claim("n1", "X always increases yield", run="rn"),
        _claim("n2", "Y is true", run="rn"),
    ]}
    stored = [
        _stored("s1", "X never increases yield", run="rs1"),
        _stored("s2", "Y is not true", run="rs2"),
        _stored("s3", "unrelated claim text here", run="rs3"),
    ]
    result = CrossRunComparator.compare(sidecar, stored)
    # n1 vs s1 → negation_pair; n2 vs s2 → negation_pair.
    kinds = [r.kind for r in result]
    assert kinds.count("negation_pair") >= 2


# ---------------------------------------------------------------------------
# F2 regression: agreeing claims are NOT contradictions
# ---------------------------------------------------------------------------

def test_agreeing_claims_same_position_no_contradiction():
    """Two runs, same claim, same position, sim high → NO contradiction (F2).

    Agreeing claims (same verdict_position) are consensus, not conflict.
    Layer 1 (semantic_overlap) must NOT fire when positions agree.
    """
    sidecar = {"claims": [_claim("c1",
        "the quick brown fox jumps over the lazy dog repeatedly",
        run="r1", position="supported")]}
    stored = [_stored("c2",
        "the quick brown fox jumps over the lazy dog repeatedly",
        run="r2", position="supported")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert result == [], (
        "Agreeing claims (same position) must NOT be flagged as contradictions"
    )


def test_agreeing_claims_verdict_conflict_also_excluded():
    """Same claim text, same atom, same position → no verdict_conflict either."""
    sidecar = {"claims": [_claim("c1",
        "memory usage increases latency significantly",
        run="r1", position="supported", atom="atom-shared")]}
    stored = [_stored("c2",
        "memory usage increases latency significantly",
        run="r2", position="supported", atom="atom-shared")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert result == [], (
        "Fully agreeing claims must not fire any contradiction layer"
    )


# ---------------------------------------------------------------------------
# F7 regression: negation-pair label precedence over semantic_overlap
# ---------------------------------------------------------------------------

def test_negation_pair_wins_over_semantic_overlap():
    """Negation pair with sim in [0.60, 0.85) → kind=negation_pair (F7).

    Before the fix, layer 1 (semantic_overlap) fired first and the pair was
    mislabeled. After the fix, negation-pair has highest precedence.
    """
    # "is" vs "is not" with shared keyword → sim ~= 0.615 (in [0.60, 0.85)).
    sidecar = {"claims": [_claim("c1", "the model is accurate", run="r1")]}
    stored = [_stored("c2", "the model is not accurate", run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert len(result) == 1, f"Expected exactly 1 contradiction, got {result}"
    assert result[0].kind == "negation_pair", (
        f"Negation pair must win over semantic_overlap, got kind={result[0].kind}"
    )


def test_negation_pair_high_sim_still_negation():
    """Negation pair with sim >= SIM_THRESHOLD → still negation_pair."""
    # Near-duplicate with negation → high sim but negation_pair must win.
    sidecar = {"claims": [_claim("c1",
        "X always increases yield significantly in production",
        run="r1")]}
    stored = [_stored("c2",
        "X never increases yield significantly in production",
        run="r2")]
    result = CrossRunComparator.compare(sidecar, stored)
    assert len(result) >= 1
    assert any(r.kind == "negation_pair" for r in result), (
        "Negation pair must win regardless of TF-IDF sim"
    )
