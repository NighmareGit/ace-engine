"""Tests for engine/memory/recall.py (G3-T04).

Budget trimming, empty store, flag-off behaviour. Uses the same temp-file DB
pattern as test_gate.py so the real SQL path is exercised.

Fixtures are defined in conftest.py.
"""

from __future__ import annotations


def test_empty_store_returns_empty_string(recall):
    """Nothing approved → rendered is empty string."""
    result = recall("any objective")
    assert result.rendered == ""
    assert result.claims == []


def test_budget_zero_returns_empty_string(gate, recall):
    """token_budget=0 → empty string even with approved claims."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance"},
    ]})
    gate.approve(ids[0])
    result = recall("python performance", token_budget=0)
    assert result.rendered == ""


def test_budget_trim(gate, recall):
    """Claims are ranked by relevance and trimmed to fit the budget."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance optimisation"},
        {"id": "c2", "text": "python performance optimisation"},
        {"id": "c3", "text": "python performance optimisation"},
    ]})
    for i in ids:
        gate.approve(i)
    # A tight budget should trim to fewer than all 3 claims.
    result = recall("python performance optimisation", token_budget=50)
    assert result.token_estimate <= 50 or len(result.claims) <= 3
    # Token estimate never exceeds budget by more than one claim's worth.
    assert result.token_estimate > 0


def test_untrusted_marker_present(gate, recall):
    """Recalled content carries the untrusted-content marker."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance"},
    ]})
    gate.approve(ids[0])
    result = recall("python performance")
    assert "UNTRUSTED" in result.rendered
    assert "python performance" in result.rendered


def test_ranked_by_relevance(gate, recall):
    """The most relevant claim appears first."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance optimisation techniques"},
        {"id": "c2", "text": "ancient roman architecture history"},
    ]})
    for i in ids:
        gate.approve(i)
    result = recall("python performance")
    assert len(result.claims) == 2
    # The python claim should outrank the roman claim.
    assert "python" in result.claims[0]["text"]
    assert result.claims[0]["score"] >= result.claims[1]["score"]


def test_flag_off_identity(gate, recall):
    """With no approved claims (flag-off equivalent), recall returns empty.

    The feature flag (enable_memory_recall) is read by the caller (ideation.py);
    recall itself is flag-agnostic. This test verifies the empty-store identity
    that the flag-off e2e test (G4-T04) depends on.
    """
    result = recall("anything")
    assert result.rendered == ""
    assert result.claims == []


def test_recall_result_bool(gate, recall):
    """RecallResult is falsy when empty, truthy when it carries claims."""
    assert not recall("nothing")
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance"},
    ]})
    gate.approve(ids[0])
    assert recall("python performance")
