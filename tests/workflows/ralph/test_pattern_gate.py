"""Tests for engine.workflows.ralph.pattern_gate — ADR-0002 pattern registry."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    """Use a fresh temp DB for each test (additive-only, no pollution)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "engine.db")
        monkeypatch.setenv("ENGINE_DB_PATH", db_path)
        # Re-import to pick up the new env.
        import importlib
        import engine.state
        importlib.reload(engine.state)
        yield db_path


from engine.workflows.ralph.pattern_gate import (
    propose_pattern_update, persist_pattern, approve_pattern,
    reject_pattern, expire_pattern, export_approved_patterns,
    PatternProposal, PatternCapHit,
)
from engine.workflows.ralph.pattern_query import query_patterns


class _FakeRound:
    def __init__(self):
        self.ralph_run_id = "run-123"
        self.round_id = 2


class TestProposePatternUpdate:
    def test_creates_proposal_with_uuid(self):
        prop = propose_pattern_update(_FakeRound(), title="lesson1", body="body")
        assert prop.pattern_id is not None
        assert len(prop.pattern_id) == 36  # UUID
        assert prop.body == "body"

    def test_body_truncated_to_8192(self):
        prop = propose_pattern_update(_FakeRound(), body="x" * 10000)
        assert len(prop.body) == 8192


class TestPersistPattern:
    def test_persists_as_proposed(self):
        prop = propose_pattern_update(_FakeRound(), title="t", body="lesson body",
                                      keywords=["cache", "perf"])
        pid = persist_pattern(prop)
        # Query proposed rows directly.
        from engine.state import _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM ralph_patterns WHERE id=?", (pid,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "proposed"
        assert row["project_namespace"] == "default"

    def test_persists_keywords_as_json(self):
        prop = propose_pattern_update(_FakeRound(), body="b",
                                      keywords=["a", "b"])
        pid = persist_pattern(prop)
        rows = query_patterns(keywords=["a"])
        # Should NOT appear — it's proposed, not approved.
        assert len(rows) == 0


class TestApprovePattern:
    def test_approve_transitions_proposed_to_approved(self):
        prop = propose_pattern_update(_FakeRound(), body="good pattern")
        pid = persist_pattern(prop)
        assert approve_pattern(pid, approved_by="owner") is True
        rows = query_patterns()
        assert len(rows) == 1
        assert rows[0]["id"] == pid
        assert rows[0]["status"] == "approved"

    def test_approve_already_approved_returns_false(self):
        prop = propose_pattern_update(_FakeRound(), body="b")
        pid = persist_pattern(prop)
        approve_pattern(pid)
        assert approve_pattern(pid) is False  # already approved

    def test_reject_transitions_proposed_to_rejected(self):
        prop = propose_pattern_update(_FakeRound(), body="bad pattern")
        pid = persist_pattern(prop)
        assert reject_pattern(pid) is True
        rows = query_patterns(status="rejected")
        assert len(rows) == 1


class TestQueryPatterns:
    def test_returns_only_approved_by_default(self, _tmp_db):
        """R2/R6: query_patterns returns only approved rows by default."""
        prop1 = propose_pattern_update(_FakeRound(), body="approved one",
                                       keywords=["cache"])
        prop2 = propose_pattern_update(_FakeRound(), body="still proposed",
                                       keywords=["cache"])
        pid1 = persist_pattern(prop1)
        persist_pattern(prop2)
        approve_pattern(pid1)
        rows = query_patterns()
        assert len(rows) == 1
        assert rows[0]["id"] == pid1

    def test_keyword_and_matching(self, _tmp_db):
        """S5 #5: LIKE multi-keyword AND."""
        p1 = propose_pattern_update(_FakeRound(), body="cache perf",
                                    keywords=["cache", "perf"])
        p2 = propose_pattern_update(_FakeRound(), body="cache only",
                                    keywords=["cache"])
        pid1 = persist_pattern(p1)
        pid2 = persist_pattern(p2)
        approve_pattern(pid1)
        approve_pattern(pid2)
        # Both keywords must match.
        rows = query_patterns(keywords=["cache", "perf"])
        assert len(rows) == 1
        assert rows[0]["id"] == pid1

    def test_ranking_score_desc(self, _tmp_db):
        """S5 #5: ranked score DESC."""
        p1 = propose_pattern_update(_FakeRound(), body="low", score=2.0)
        p2 = propose_pattern_update(_FakeRound(), body="high", score=9.0)
        pid1 = persist_pattern(p1)
        pid2 = persist_pattern(p2)
        approve_pattern(pid1)
        approve_pattern(pid2)
        rows = query_patterns()
        assert rows[0]["id"] == pid2  # higher score first
        assert rows[1]["id"] == pid1

    def test_limit_respected(self, _tmp_db):
        for i in range(15):
            p = propose_pattern_update(_FakeRound(), body=f"pat {i}")
            pid = persist_pattern(p)
            approve_pattern(pid)
        rows = query_patterns(limit=5)
        assert len(rows) == 5

    def test_project_namespace_isolation(self, _tmp_db):
        """R2: per-project namespacing kills cross-project contamination."""
        p1 = propose_pattern_update(_FakeRound(), body="proj-a pattern")
        p1.project_namespace = "project-a"
        p2 = propose_pattern_update(_FakeRound(), body="proj-b pattern")
        p2.project_namespace = "project-b"
        pid1 = persist_pattern(p1)
        pid2 = persist_pattern(p2)
        approve_pattern(pid1)
        approve_pattern(pid2)
        rows = query_patterns(project_namespace="project-a")
        assert len(rows) == 1
        assert rows[0]["id"] == pid1


class TestExportApprovedPatterns:
    def test_markdown_regenerated_from_sql(self, _tmp_db, tmp_path):
        """R6: markdown projection regenerated from SQL, not read back."""
        p = propose_pattern_update(_FakeRound(), body="exported pattern body",
                                   keywords=["export"])
        pid = persist_pattern(p)
        approve_pattern(pid)
        written = export_approved_patterns(
            skills_dir=tmp_path,
        )
        assert len(written) == 1
        content = written[0].read_text()
        assert "exported pattern body" in content
        assert "pattern_type:" in content

    def test_export_only_approved(self, _tmp_db, tmp_path):
        p1 = propose_pattern_update(_FakeRound(), body="approved")
        p2 = propose_pattern_update(_FakeRound(), body="proposed")
        pid1 = persist_pattern(p1)
        persist_pattern(p2)
        approve_pattern(pid1)
        written = export_approved_patterns(skills_dir=tmp_path)
        assert len(written) == 1


class TestMaxPatternsPerRun:
    """S9 FIX #3: max_patterns_per_run must be enforced."""

    def test_enforces_cap(self, _tmp_db):
        """At cap, persist_pattern raises PatternCapHit (doesn't silently drop)."""
        cap = 2
        p1 = propose_pattern_update(_FakeRound(), body="first")
        p2 = propose_pattern_update(_FakeRound(), body="second")
        p3 = propose_pattern_update(_FakeRound(), body="third")
        persist_pattern(p1, max_patterns_per_run=cap)
        persist_pattern(p2, max_patterns_per_run=cap)
        with pytest.raises(PatternCapHit, match="max_patterns_per_run=2"):
            persist_pattern(p3, max_patterns_per_run=cap)

    def test_no_cap_when_zero(self, _tmp_db):
        """max_patterns_per_run=0 means unbounded."""
        for i in range(10):
            p = propose_pattern_update(_FakeRound(), body=f"pat {i}")
            persist_pattern(p, max_patterns_per_run=0)  # no raise

    def test_cap_counts_per_run(self, _tmp_db):
        """Cap is per source_ralph_run_id, not global."""
        cap = 1
        p1 = propose_pattern_update(_FakeRound(), body="run-123 pattern")
        persist_pattern(p1, max_patterns_per_run=cap)
        # Different run_id should not be blocked.
        class _OtherRound:
            ralph_run_id = "run-999"
            round_id = 1
        p2 = propose_pattern_update(_OtherRound(), body="run-999 pattern")
        persist_pattern(p2, max_patterns_per_run=cap)  # no raise


class TestPatternApprovalRequired:
    """S9 FIX #4: pattern_approval_required must be consumed."""

    def test_approval_required_true_stays_proposed(self, _tmp_db):
        """When pattern_approval_required=True (default), rows stay proposed."""
        p = propose_pattern_update(_FakeRound(), body="needs approval")
        pid = persist_pattern(p, pattern_approval_required=True)
        rows = query_patterns(status="proposed")
        assert len(rows) == 1
        assert rows[0]["id"] == pid
        # Should NOT appear in approved query.
        approved = query_patterns(status="approved")
        assert len(approved) == 0

    def test_approval_required_false_auto_approves(self, _tmp_db):
        """When pattern_approval_required=False, rows are auto-approved."""
        p = propose_pattern_update(_FakeRound(), body="auto approved")
        pid = persist_pattern(p, pattern_approval_required=False)
        rows = query_patterns(status="approved")
        assert len(rows) == 1
        assert rows[0]["id"] == pid
        assert rows[0]["approved_by"] == "auto"

    def test_default_is_approval_required(self, _tmp_db):
        """Default persist_pattern behavior requires approval."""
        p = propose_pattern_update(_FakeRound(), body="default")
        pid = persist_pattern(p)  # default pattern_approval_required=True
        rows = query_patterns(status="proposed")
        assert len(rows) == 1
        assert rows[0]["id"] == pid
