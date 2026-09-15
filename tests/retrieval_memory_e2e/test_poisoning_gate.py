"""G4-T04 e2e: poisoning-gate integration sub-test.

Exercises the G3 poisoning gate end-to-end through the CLI dispatch keys:

  1. Seed memory_claims with approved "X always increases"
  2. Submit "X never increases" via the gate's submit_claims
  3. ApproveResult.contradictions non-empty, kind="negation_pair"
  4. Human rejects → status "rejected"
  5. recall_for_objective("X") excludes the rejected claim

DESIGN-G4 §4.3.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh temp-file DB for each test."""
    import engine.state as state_mod
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    state_mod.init_db(db_path)
    return db_path


@pytest.fixture()
def gate(db):
    from engine.memory import gate
    return gate


@pytest.fixture()
def recall(db):
    from engine.memory.recall import recall_for_objective
    return recall_for_objective


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_poisoning_gate_negation_pair(gate, recall):
    """Seed approved claim, submit negation, assert contradiction detected."""
    # 1. Seed an approved claim.
    seed_ids = gate.submit_claims("r-seed", "t0", {"claims": [
        {"id": "s1", "text": "X always increases yield"},
    ]}, source_atom_id="atom-seed")
    assert len(seed_ids) == 1
    seed_approve = gate.approve(seed_ids[0])
    assert seed_approve.approved is True
    assert seed_approve.contradictions == []  # clean (no prior approved claims)

    # 2. Submit a contradicting claim (negation pair).
    new_ids = gate.submit_claims("r2", "t2", {"claims": [
        {"id": "n1", "text": "X never increases yield"},
    ]}, source_atom_id="atom2")
    assert len(new_ids) == 1

    # 3. Approve surfaces the contradiction (non-blocking — human decides).
    result = gate.approve(new_ids[0])
    assert result.approved is True  # human override
    assert len(result.contradictions) >= 1, (
        "negation-pair contradiction not detected")
    assert any(c.kind == "negation_pair" for c in result.contradictions), (
        f"expected negation_pair kind, got {[c.kind for c in result.contradictions]}")


def test_reject_excludes_from_recall(gate, recall):
    """Rejected claim is invisible to recall_for_objective."""
    # Seed an approved claim (so recall has something to return).
    approved_ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "a1", "text": "Python performance optimisation matters"},
    ]}, source_atom_id="atom-a")
    gate.approve(approved_ids[0])

    # Submit + reject a claim.
    rejected_ids = gate.submit_claims("r2", "t2", {"claims": [
        {"id": "r1", "text": "Python performance is irrelevant"},
    ]}, source_atom_id="atom-r")
    reject_result = gate.reject(rejected_ids[0])
    assert reject_result.approved is False
    assert "rejected" in reject_result.message

    # 5. Recall excludes the rejected claim.
    result = recall("Python performance")
    returned_ids = [c["id"] for c in result.claims]
    assert all(rid != rejected_ids[0] for rid in returned_ids), (
        f"rejected claim leaked into recall: {returned_ids}")
    # The approved claim IS recallable.
    assert any(aid in returned_ids for aid in approved_ids), (
        "approved claim missing from recall")


def test_approve_rejected_claim_is_noop(gate):
    """A rejected claim cannot be re-approved (ADR-0002 invariant)."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "temporary claim"},
    ]})
    gate.reject(ids[0])
    # Try to approve the already-rejected claim.
    result = gate.approve(ids[0])
    assert result.approved is False
    assert "not in 'pending' status" in result.message


def test_recall_empty_when_no_approved(gate, recall):
    """Recall returns nothing when there are no approved claims."""
    # Submit but do NOT approve.
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "unapproved claim about Python"},
    ]})
    result = recall("Python")
    assert result.claims == []
    assert result.rendered == ""
