"""TGD tests: Tool-Gap Detector (ORCH-7, Wave-2).

Mechanical detector over engine.db telemetry. 11 signals G1–G11. The detector
never invents gaps — every Finding carries concrete evidence rows. Tests
assert each signal fires on its fixture and does NOT fire on clean data
(false-positive controls). Target >= 25 tests.

G7/G9/G10 are IIL/skills-gated and degrade to no-op (verified, not skipped).
"""

import json
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

# Only collect if the TGD module is present.
pytest.importorskip("engine.orchestrator.toolgap_detector")

from engine.orchestrator.toolgap_types import (
    SignalId, Severity, TriageCategory, GATED_SIGNALS,
)
from engine.orchestrator import toolgap_detector as tgd
from engine.orchestrator import toolgap_renderer as tgr
from engine.orchestrator import toolgap_persistence as tgp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


def _seed_minimal(conn):
    """Seed the base tables the detector reads from."""
    from engine.state import init_db
    init_db()
    from engine.orchestrator.session import _ensure_tables
    _ensure_tables()
    # Ensure session_logs table exists (OBS-01/OBS-05).
    conn.execute("""CREATE TABLE IF NOT EXISTS session_logs (
        session_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT,
        attempt INTEGER, stage TEXT, atom_id TEXT,
        model TEXT, port INTEGER, prompt_hash TEXT, prompt_truncated TEXT,
        prompt_payload_path TEXT, system_message TEXT, user_message TEXT,
        response_content TEXT, response_reasoning TEXT, finish_reason TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
        thinking_tokens INTEGER, latency_ms INTEGER, exhausted INTEGER,
        error TEXT, created_at TEXT
    )""")


def _insert_run(conn, run_id, state="DONE", error_message=None):
    conn.execute(
        "INSERT INTO engine_runs (id, prd_path, project_path, config, state, "
        "error_message) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "/tmp/prd.md", "/tmp/proj", "test", state, error_message),
    )


def _insert_task_result(conn, run_id, task_id, state="COMMIT", attempts=1,
                        validation_result=None, error_message=None,
                        generated_code=None):
    conn.execute(
        "INSERT INTO engine_task_results (run_id, task_id, state, attempts, "
        "validation_result, error_message, generated_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, task_id, state, attempts, validation_result, error_message,
         generated_code),
    )


def _insert_replan(conn, run_id, task_id, trigger_reason="",
                   raw_response=None, applied=0):
    conn.execute(
        "INSERT INTO orchestrator_replans (run_id, task_id, trigger_signature, "
        "trigger_reason, raw_response, applied, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        (run_id, task_id, json.dumps(["validation", "syntax"]),
         trigger_reason, raw_response, applied),
    )


def _insert_scores(conn, run_id, task_id, completeness=8, correctness=8,
                   quality=8, intelligence=8, role_fit=8):
    conn.execute(
        "INSERT INTO engine_scores (run_id, task_id, judge_model, "
        "completeness, correctness, quality, intelligence, role_fit) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, task_id, "test-judge", completeness, correctness, quality,
         intelligence, role_fit),
    )


# ---------------------------------------------------------------------------
# Fixture DB builder exercising all 11 signals
# ---------------------------------------------------------------------------

def build_fixture_db(conn):
    """Populate a DB with synthetic telemetry exercising all 11 signals."""
    _seed_minimal(conn)

    # G1: repeated (stage, error_class) >= 3 times across tasks/runs.
    # Three tasks in ONE run with the same failing signature -> fires on a
    # single-run scan. (G1 also aggregates across runs for history scans.)
    vfail = json.dumps([{"stage": "syntax", "file": "a.py", "passed": False,
                         "error": "SyntaxError: invalid syntax"}])
    _insert_run(conn, "run-g1-0")
    for i in range(3):
        _insert_task_result(conn, "run-g1-0", f"T0{i}", state="FAILED",
                            attempts=3, validation_result=vfail)

    # G2: re-plan reason mentioning missing info.
    _insert_run(conn, "run-g2")
    _insert_task_result(conn, "run-g2", "T01", state="FAILED", attempts=3)
    _insert_replan(conn, "run-g2", "T01",
                   trigger_reason="missing context: no access to schema")

    # G3: retry exhaustion -> FAIL, no re-plan recovery.
    _insert_run(conn, "run-g3")
    _insert_task_result(conn, "run-g3", "T01", state="FAILED", attempts=3,
                        error_message="validation exhausted")

    # G4: high prompt_tokens near model context cap (OBS-05: real prompt_tokens
    # from session_logs, upgraded from the code-blob proxy).
    _insert_run(conn, "run-g4")
    big_code = "x" * 60_000
    _insert_task_result(conn, "run-g4", "T01", generated_code=big_code)
    # Insert session_logs row with high prompt_tokens to trigger G4.
    conn.execute(
        "INSERT INTO session_logs "
        "(session_id, run_id, task_id, attempt, stage, model, port, "
        "prompt_hash, prompt_truncated, response_content, finish_reason, "
        "prompt_tokens, completion_tokens, total_tokens, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-g4-T01-1-generate", "run-g4", "T01", 1, "generate",
         "3090-qwen36-35b", 8080, "hash", "prompt...", "code...", "stop",
         15000, 100, 15100, 3000),
    )

    # G5: judge low sub-scores clustered on one dimension.
    _insert_run(conn, "run-g5")
    for tid in ["T01", "T02", "T03"]:
        _insert_scores(conn, "run-g5", tid, completeness=2, correctness=8)

    # G6: dependency_allowlist additions in a re-plan patch.
    _insert_run(conn, "run-g6")
    _insert_task_result(conn, "run-g6", "T01", state="FAILED", attempts=3)
    patch = json.dumps({"spec_patch": {"dependency_allowlist_additions": ["flask"]}})
    _insert_replan(conn, "run-g6", "T01", raw_response=patch)

    # G8: human intervention (stop-file cancellation).
    _insert_run(conn, "run-g8", state="CANCELLED",
                error_message="cancelled by stop-file")

    # G11: external reference failure.
    _insert_run(conn, "run-g11")
    _insert_task_result(conn, "run-g11", "T01", state="FAILED", attempts=2,
                        error_message="https://pypi.org/project/nonexistent 404")

    # G7/G9/G10: no source data (gated) — nothing to seed.
    conn.commit()


# ---------------------------------------------------------------------------
# (a) Signal firing: each signal fires on its fixture
# ---------------------------------------------------------------------------

class TestSignalFiring:
    """Each signal fires on its fixture data."""

    def test_g1_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        g1 = report.findings_for_signal(SignalId.G1_REPEATED_FAILURE_SIGNATURE)
        assert len(g1) >= 1
        assert g1[0].evidence  # carries evidence rows

    def test_g2_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g2")
        g2 = report.findings_for_signal(SignalId.G2_ESCALATION_MISSING_INFO)
        assert len(g2) >= 1

    def test_g3_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g3")
        g3 = report.findings_for_signal(SignalId.G3_RETRY_EXHAUSTION)
        assert len(g3) >= 1

    def test_g4_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g4")
        g4 = report.findings_for_signal(SignalId.G4_CONTEXT_BLOAT)
        assert len(g4) >= 1

    def test_g5_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g5")
        g5 = report.findings_for_signal(SignalId.G5_JUDGE_LOW_SUBCLUSTER)
        assert len(g5) >= 1

    def test_g6_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g6")
        g6 = report.findings_for_signal(SignalId.G6_ALLOWLIST_ADDITION)
        assert len(g6) >= 1

    def test_g8_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g8")
        g8 = report.findings_for_signal(SignalId.G8_HUMAN_INTERVENTION)
        assert len(g8) >= 1
        assert g8[0].severity == Severity.ALWAYS

    def test_g11_fires(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g11")
        g11 = report.findings_for_signal(SignalId.G11_EXTERNAL_REFERENCE_FAILURE)
        assert len(g11) >= 1


# ---------------------------------------------------------------------------
# (b) Gated signals: G7/G9/G10 degrade gracefully to no-op
# ---------------------------------------------------------------------------

class TestGatedSignals:
    """G7/G9/G10 produce no findings pre-IIL (graceful no-op)."""

    def test_g7_noop(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        assert report.findings_for_signal(SignalId.G7_ROUTER_UNCERTAIN) == []

    def test_g9_noop(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        assert report.findings_for_signal(SignalId.G9_SKILL_LOOKUP_MISS) == []

    def test_g10_noop(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        assert report.findings_for_signal(SignalId.G10_HALLUCINATED_TOOL_CALL) == []

    def test_gated_signals_are_catalogued(self):
        """Gated signals still appear in the SignalId enum (catalogue complete)."""
        assert SignalId.G7_ROUTER_UNCERTAIN in GATED_SIGNALS
        assert SignalId.G9_SKILL_LOOKUP_MISS in GATED_SIGNALS
        assert SignalId.G10_HALLUCINATED_TOOL_CALL in GATED_SIGNALS


# ---------------------------------------------------------------------------
# (c) False-positive controls: signals do NOT fire on clean data
# ---------------------------------------------------------------------------

def _build_clean_db(conn):
    """A healthy run: all tasks COMMIT, no failures, good scores."""
    _seed_minimal(conn)
    _insert_run(conn, "run-clean")
    vok = json.dumps([{"stage": "syntax", "file": "a.py", "passed": True,
                       "error": ""}])
    _insert_task_result(conn, "run-clean", "T01", state="COMMIT", attempts=1,
                        validation_result=vok, generated_code="print('hi')")
    _insert_task_result(conn, "run-clean", "T02", state="COMMIT", attempts=1,
                        validation_result=vok, generated_code="print('ok')")
    _insert_scores(conn, "run-clean", "T01", completeness=9, correctness=9)
    _insert_scores(conn, "run-clean", "T02", correctness=9)
    conn.commit()


class TestFalsePositiveControls:
    """Signals must NOT fire on clean (healthy) data."""

    def test_no_g1_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(
            SignalId.G1_REPEATED_FAILURE_SIGNATURE) == []

    def test_no_g2_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(
            SignalId.G2_ESCALATION_MISSING_INFO) == []

    def test_no_g3_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(SignalId.G3_RETRY_EXHAUSTION) == []

    def test_no_g4_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(SignalId.G4_CONTEXT_BLOAT) == []

    def test_no_g5_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(
            SignalId.G5_JUDGE_LOW_SUBCLUSTER) == []

    def test_no_g6_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(SignalId.G6_ALLOWLIST_ADDITION) == []

    def test_no_g8_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(SignalId.G8_HUMAN_INTERVENTION) == []

    def test_no_g11_on_clean(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _build_clean_db(conn)
        conn.close()
        report = tgd.scan_run("run-clean")
        assert report.findings_for_signal(
            SignalId.G11_EXTERNAL_REFERENCE_FAILURE) == []


# ---------------------------------------------------------------------------
# (d) Evidence invariant: every finding carries concrete evidence rows
# ---------------------------------------------------------------------------

class TestEvidenceInvariant:
    """The detector never invents gaps — every finding cites evidence."""

    def test_all_findings_have_evidence(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        for f in report.findings:
            assert len(f.evidence) >= 1, (
                f"Signal {f.signal.value} produced a finding with no evidence")

    def test_evidence_cites_run_ids(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        for f in report.findings:
            for e in f.evidence:
                assert e.run_id, "Evidence row missing run_id"
                assert e.signature, "Evidence row missing signature"


# ---------------------------------------------------------------------------
# (e) Report rendering: JSON + markdown
# ---------------------------------------------------------------------------

class TestRendering:
    def test_json_roundtrip(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        js = tgr.render_gap_report_json(report)
        data = json.loads(js)
        assert data["run_id"] == "run-g1-0"
        assert len(data["findings"]) >= 1

    def test_markdown_contains_signal_ids(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        md = tgr.render_gap_report_markdown(report)
        assert "G1" in md
        assert "Tool-Gap Detector Report" in md

    def test_markdown_lists_gated_note(self, tmp_db):
        report = tgd.scan_run("run-g1-0")
        md = tgr.render_gap_report_markdown(report)
        assert "G7" in md
        assert "gated" in md.lower()


# ---------------------------------------------------------------------------
# (f) Persistence + triage-yield kill-criterion instrumentation
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persist_and_recall(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        row_ids = tgp.persist_gap_report(report)
        assert len(row_ids) == len(report.findings)
        history = tgp.get_gap_history("run-g1-0")
        assert len(history) >= len(report.findings)

    def test_triage_recording(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        row_ids = tgp.persist_gap_report(report)
        tgp.record_triage(row_ids[0], "accepted", "add syntax validator")
        history = tgp.get_gap_history("run-g1-0")
        accepted = [h for h in history if h["triage_outcome"] == "accepted"]
        assert len(accepted) >= 1

    def test_triage_yield_computable(self, tmp_db):
        """The triage-yield metric is computable from telemetry."""
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_run("run-g1-0")
        row_ids = tgp.persist_gap_report(report)
        tgp.record_triage(row_ids[0], "accepted")
        metric = tgp.compute_triage_yield(window=10)
        assert "accepted" in metric
        assert "yield_rate" in metric
        assert "passes" in metric
        assert metric["accepted"] >= 1


# ---------------------------------------------------------------------------
# (g) History scan mode
# ---------------------------------------------------------------------------

class TestHistoryScan:
    def test_scan_history_covers_all_runs(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        report = tgd.scan_history()
        assert report.run_id is None
        assert report.scanned_runs >= 5
        # G1 aggregates across runs.
        g1 = report.findings_for_signal(SignalId.G1_REPEATED_FAILURE_SIGNATURE)
        assert len(g1) >= 1


# ---------------------------------------------------------------------------
# (h) Performance: post-run scan < 100ms on fixture DB
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_scan_run_under_100ms(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        build_fixture_db(conn)
        conn.close()
        start = time.perf_counter()
        tgd.scan_run("run-g1-0")
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 100, f"TGD scan took {elapsed_ms:.1f}ms (>= 100ms)"


# ---------------------------------------------------------------------------
# (i) IIL-gated signals: G7/G9/G10 fire when IIL telemetry exists (Wave-3)
# ---------------------------------------------------------------------------

def _ensure_intent_events(conn):
    """Create the additive intent_events table (IIL telemetry contract)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS intent_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        model TEXT, channel TEXT, task_id TEXT, run_id TEXT,
        verdict TEXT, lane TEXT, action TEXT,
        confidence REAL, uncertain INTEGER DEFAULT 0,
        latency_ms REAL, route_action TEXT, route_table TEXT
    )""")


def _ensure_skill_lookups(conn):
    """Create the additive skill_lookups table (skills tier telemetry)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS skill_lookups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        skill_id TEXT NOT NULL, hit INTEGER DEFAULT 0
    )""")


def _insert_intent_event(conn, run_id, task_id, action, uncertain=0,
                         confidence=0.9, lane="router"):
    _ensure_intent_events(conn)
    conn.execute(
        "INSERT INTO intent_events "
        "(run_id, task_id, action, uncertain, confidence, lane) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, task_id, action, uncertain, confidence, lane),
    )


class TestIILGatedSignals:
    """G7/G9/G10 fire when IIL telemetry exists (Wave-3 wiring)."""

    def test_g7_fires_on_uncertain_router_verdicts(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _seed_minimal(conn)
        _insert_run(conn, "run-g7")
        _insert_intent_event(conn, "run-g7", "T01", "run_tests",
                             uncertain=1, confidence=0.35, lane="router")
        _insert_intent_event(conn, "run-g7", "T02", "commit",
                             uncertain=1, confidence=0.42, lane="router")
        conn.commit()
        conn.close()
        report = tgd.scan_run("run-g7")
        g7 = report.findings_for_signal(SignalId.G7_ROUTER_UNCERTAIN)
        assert len(g7) >= 1
        assert g7[0].evidence  # carries evidence rows

    def test_g7_noop_without_telemetry(self, tmp_db):
        """G7 still degrades gracefully when intent_events is absent."""
        from engine.state import _get_conn
        conn = _get_conn()
        _seed_minimal(conn)
        _insert_run(conn, "run-no-g7")
        conn.close()
        report = tgd.scan_run("run-no-g7")
        assert report.findings_for_signal(SignalId.G7_ROUTER_UNCERTAIN) == []

    def test_g10_fires_on_hallucinated_tool_call(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _seed_minimal(conn)
        _insert_run(conn, "run-g10")
        # "launch_rockets" is NOT in the ACE registry → hallucination.
        _insert_intent_event(conn, "run-g10", "T01", "launch_rockets",
                             uncertain=0, confidence=0.8, lane="native")
        conn.commit()
        conn.close()
        report = tgd.scan_run("run-g10")
        g10 = report.findings_for_signal(SignalId.G10_HALLUCINATED_TOOL_CALL)
        assert len(g10) >= 1
        assert g10[0].severity == Severity.HIGH

    def test_g10_ignores_registered_actions(self, tmp_db):
        """Actions in the registry must NOT fire G10."""
        from engine.state import _get_conn
        conn = _get_conn()
        _seed_minimal(conn)
        _insert_run(conn, "run-g10-clean")
        _insert_intent_event(conn, "run-g10-clean", "T01", "run_tests")
        _insert_intent_event(conn, "run-g10-clean", "T02", "commit")
        conn.commit()
        conn.close()
        report = tgd.scan_run("run-g10-clean")
        assert report.findings_for_signal(SignalId.G10_HALLUCINATED_TOOL_CALL) == []

    def test_g9_fires_on_skill_lookup_miss(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _seed_minimal(conn)
        _ensure_skill_lookups(conn)
        conn.execute(
            "INSERT INTO skill_lookups (skill_id, hit) VALUES (?, ?)",
            ("missing-skill", 0),
        )
        conn.execute(
            "INSERT INTO skill_lookups (skill_id, hit) VALUES (?, ?)",
            ("also-missing", 0),
        )
        conn.commit()
        conn.close()
        report = tgd.scan_history()
        g9 = report.findings_for_signal(SignalId.G9_SKILL_LOOKUP_MISS)
        assert len(g9) >= 1
        assert g9[0].evidence

    def test_g9_noop_without_telemetry(self, tmp_db):
        from engine.state import _get_conn
        conn = _get_conn()
        _seed_minimal(conn)
        conn.close()
        report = tgd.scan_history()
        assert report.findings_for_signal(SignalId.G9_SKILL_LOOKUP_MISS) == []
