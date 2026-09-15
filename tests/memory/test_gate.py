"""Tests for engine/memory/gate.py (G3-T04).

Uses a temp-file SQLite DB (ENGINE_DB_PATH override) so the gate's real SQL
path is exercised. Deterministic, LLM-free.

Fixtures are defined in conftest.py.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Gate flow
# ---------------------------------------------------------------------------

def test_submit_creates_pending_claims(gate):
    """submit_claims persists claims with status=pending."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "claim one", "source_ids": ["s1"]},
        {"id": "c2", "text": "claim two", "source_ids": []},
    ]}, source_atom_id="atom1")
    assert len(ids) == 2
    pending = gate.list_pending()
    assert len(pending) == 2
    assert all(p["id"] in ids for p in pending)


def test_approve_clean_claim(gate):
    """Approve a claim with no contradictions → approved=True, contradictions=[]."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "unique isolated claim", "source_ids": []},
    ]})
    result = gate.approve(ids[0])
    assert result.approved is True
    assert result.contradictions == []
    assert "clean" in result.message


def test_approve_contradictory_claim_attaches_findings(gate):
    """Approve a negation-pair claim → approved=True (human override), contradictions attached."""
    # Seed an approved claim.
    seed = gate.submit_claims("r-seed", "t0", {"claims": [
        {"id": "s1", "text": "X always increases yield"},
    ]}, source_atom_id="atom-seed")
    gate.approve(seed[0])

    # Submit + approve a contradicting claim.
    new = gate.submit_claims("r2", "t2", {"claims": [
        {"id": "n1", "text": "X never increases yield"},
    ]}, source_atom_id="atom2")
    result = gate.approve(new[0])
    assert result.approved is True
    assert len(result.contradictions) >= 1
    assert any(c.kind == "negation_pair" for c in result.contradictions)


def test_reject_claim(gate):
    """Reject a pending claim → approved=False, status→rejected."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "rejectable claim"},
    ]})
    result = gate.reject(ids[0])
    assert result.approved is False
    assert "rejected" in result.message
    # After reject, the pending queue no longer contains it.
    pending = gate.list_pending()
    assert all(p["id"] != ids[0] for p in pending)


def test_recall_invisibility_pending(gate, recall):
    """Pending (unapproved) claims are invisible to recall."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance optimisation"},
    ]})
    # Do NOT approve — leave pending.
    result = recall("python performance optimisation")
    assert result.claims == []
    assert result.rendered == ""


def test_recall_visibility_approved(gate, recall):
    """Approved claims are visible to recall."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance optimisation"},
    ]})
    gate.approve(ids[0])
    result = recall("python performance optimisation")
    assert len(result.claims) == 1
    assert result.claims[0]["id"] == ids[0]


def test_recall_invisibility_rejected(gate, recall):
    """Rejected claims are invisible to recall."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance optimisation"},
    ]})
    # Reject while still pending (ADR-0002: only pending rows transition).
    gate.reject(ids[0])
    result = recall("python performance optimisation")
    assert all(c["id"] != ids[0] for c in result.claims)
    assert result.rendered == ""


def test_one_way_write_recall_is_readonly(gate, recall):
    """Recall path never mutates claim status (one-way write/read contract)."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "python performance optimisation"},
    ]})
    gate.approve(ids[0])
    # Call recall repeatedly — status must stay "approved".
    for _ in range(3):
        recall("python performance optimisation")
    from engine.state import _get_conn
    conn = _get_conn()
    row = conn.execute(
        "SELECT status FROM memory_claims WHERE id=?", (ids[0],)
    ).fetchone()
    conn.close()
    assert row["status"] == "approved"


def test_approve_only_pending_transitions(gate):
    """Only pending rows can be approved; approving twice is a no-op the second time."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "claim text"},
    ]})
    first = gate.approve(ids[0])
    assert first.approved is True
    second = gate.approve(ids[0])
    assert second.approved is False  # no longer pending


def test_list_pending_with_contradiction_counts(gate):
    """list_pending surfaces contradiction counts for the review surface."""
    # Seed approved claim.
    seed = gate.submit_claims("r-seed", "t0", {"claims": [
        {"id": "s1", "text": "X always increases yield"},
    ]}, source_atom_id="atom-seed")
    gate.approve(seed[0])
    # Submit a pending contradicting claim.
    new = gate.submit_claims("r2", "t2", {"claims": [
        {"id": "n1", "text": "X never increases yield"},
    ]}, source_atom_id="atom2")
    pending = gate.list_pending()
    entry = next(p for p in pending if p["id"] == new[0])
    assert entry["contradiction_count"] >= 1


# ---------------------------------------------------------------------------
# F3: contradiction status transitions
# ---------------------------------------------------------------------------

def test_confirm_contradiction(gate):
    """confirm_contradiction transitions pending→confirmed."""
    from engine.state import _get_conn
    # Seed a contradiction row.
    seed = gate.submit_claims("r-seed", "t0", {"claims": [
        {"id": "s1", "text": "X always increases yield"},
    ]}, source_atom_id="atom-seed")
    gate.approve(seed[0])
    new = gate.submit_claims("r2", "t2", {"claims": [
        {"id": "n1", "text": "X never increases yield"},
    ]}, source_atom_id="atom2")
    # Fetch the contradiction row.
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM memory_contradictions WHERE status='pending' LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    cid = row["id"]

    assert gate.confirm_contradiction(cid) is True
    # Second confirm is a no-op (no longer pending).
    assert gate.confirm_contradiction(cid) is False

    conn = _get_conn()
    row = conn.execute(
        "SELECT status FROM memory_contradictions WHERE id=?", (cid,)
    ).fetchone()
    conn.close()
    assert row["status"] == "confirmed"


def test_dismiss_contradiction(gate):
    """dismiss_contradiction transitions pending→dismissed."""
    from engine.state import _get_conn
    seed = gate.submit_claims("r-seed", "t0", {"claims": [
        {"id": "s1", "text": "Y is true"},
    ]}, source_atom_id="atom-seed")
    gate.approve(seed[0])
    gate.submit_claims("r2", "t2", {"claims": [
        {"id": "n1", "text": "Y is not true"},
    ]}, source_atom_id="atom2")

    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM memory_contradictions WHERE status='pending' LIMIT 1"
    ).fetchone()
    conn.close()
    cid = row["id"]

    assert gate.dismiss_contradiction(cid) is True
    assert gate.dismiss_contradiction(cid) is False  # no longer pending


# ---------------------------------------------------------------------------
# F4: approve_for_recall unified dispatch
# ---------------------------------------------------------------------------

def test_approve_for_recall_claims_store(gate):
    """approve_for_recall dispatches to gate.approve for store='claims'."""
    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "claim for unified approve"},
    ]})
    result = gate.approve_for_recall(ids[0], store="claims")
    assert result.approved is True
    assert result.record_id == ids[0]


def test_approve_for_recall_patterns_store(gate):
    """approve_for_recall dispatches to pattern_gate for store='patterns'."""
    from engine.state import _get_conn
    from engine.workflows.ralph.pattern_gate import _ensure_schema
    _ensure_schema()
    conn = _get_conn()
    import uuid
    pid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ralph_patterns (id, project_namespace, pattern_type, body, score, status) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "default", "strategy", "test body", 0.9, "proposed"),
    )
    conn.commit()
    conn.close()

    # Existing proposed pattern → approved.
    result = gate.approve_for_recall(pid, store="patterns")
    assert result.approved is True

    # Nonexistent id → not approved.
    result2 = gate.approve_for_recall("nonexistent", store="patterns")
    assert result2.approved is False
