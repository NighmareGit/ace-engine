"""O3 tests: oracle escalation wiring (trigger + barrier + budget + aggregate).

The oracle escalation runtime wires the P6 oracle into the engine's escalation
ladder (9B → 35B → ORACLE). Tests cover:

  - Trigger fires ONLY on re-plan exhaustion + enabled
  - Disabled/degraded → graceful fall-through (no crash, no transport call)
  - Per-run cap (max_oracle_escalations_per_run)
  - Run-barrier lock semantics (one oracle escalation in flight)
  - Budget counting (oracle call counted against T8)
  - Aggregation of atom results (deterministic)
"""

import os
import sys
import json

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Isolate every test in its own temp engine.db."""
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    yield db_path


def _make_cfg(enabled=True, max_escalations=1, **overrides):
    from engine.orchestrator.oracle_types import OracleConfig
    kwargs = {"enabled": enabled,
              "max_oracle_escalations_per_run": max_escalations}
    kwargs.update(overrides)
    return OracleConfig(**kwargs)


# ---------------------------------------------------------------------------
# Trigger evaluation.
# ---------------------------------------------------------------------------

def test_trigger_fires_on_exhaustion_and_enabled():
    """Oracle escalation fires when re-plan exhausted AND oracle enabled."""
    from engine.orchestrator.oracle_escalate import should_escalate_to_oracle
    cfg = _make_cfg(enabled=True)
    should, reason = should_escalate_to_oracle(
        "run-1", "T01", replan_exhausted=True, cfg=cfg)
    assert should is True
    assert "exhausted" in reason.lower() or "enabled" in reason.lower()


def test_trigger_disabled_no_fire():
    """When oracle is disabled, never escalate (graceful fall-through)."""
    from engine.orchestrator.oracle_escalate import should_escalate_to_oracle
    cfg = _make_cfg(enabled=False)
    should, reason = should_escalate_to_oracle(
        "run-1", "T01", replan_exhausted=True, cfg=cfg)
    assert should is False
    assert "disabled" in reason.lower()


def test_trigger_not_exhausted_no_fire():
    """When the re-plan ladder is NOT exhausted, never escalate."""
    from engine.orchestrator.oracle_escalate import should_escalate_to_oracle
    cfg = _make_cfg(enabled=True)
    should, reason = should_escalate_to_oracle(
        "run-1", "T01", replan_exhausted=False, cfg=cfg)
    assert should is False
    assert "not exhausted" in reason.lower()


# ---------------------------------------------------------------------------
# Per-run escalation cap.
# ---------------------------------------------------------------------------

def test_per_run_cap_blocks_after_hit(tmp_db):
    """After max_oracle_escalations_per_run escalations, further ones are blocked."""
    from engine.orchestrator.oracle_escalate import (
        should_escalate_to_oracle,
        increment_oracle_escalation_count,
        _ensure_oracle_columns,
    )
    _ensure_oracle_columns()
    cfg = _make_cfg(enabled=True, max_escalations=1)
    # Simulate one escalation already done.
    increment_oracle_escalation_count("run-1")
    should, reason = should_escalate_to_oracle(
        "run-1", "T01", replan_exhausted=True, cfg=cfg)
    assert should is False
    assert "cap" in reason.lower()


def test_per_run_cap_allows_first(tmp_db):
    """The first escalation under the cap is allowed."""
    from engine.orchestrator.oracle_escalate import (
        should_escalate_to_oracle, _ensure_oracle_columns,
    )
    _ensure_oracle_columns()
    cfg = _make_cfg(enabled=True, max_escalations=2)
    should, _ = should_escalate_to_oracle(
        "run-1", "T01", replan_exhausted=True, cfg=cfg)
    assert should is True


# ---------------------------------------------------------------------------
# Run-barrier lock semantics (grill fix G5).
# ---------------------------------------------------------------------------

def test_barrier_lock_acquire_release(tmp_db):
    """A lock can be acquired and released."""
    from engine.orchestrator.oracle_escalate import (
        acquire_oracle_lock, release_oracle_lock, is_oracle_in_flight,
        _ensure_oracle_columns,
    )
    _ensure_oracle_columns()
    assert is_oracle_in_flight("run-1") is False
    assert acquire_oracle_lock("run-1", "T01") is True
    assert is_oracle_in_flight("run-1") is True
    release_oracle_lock("run-1")
    assert is_oracle_in_flight("run-1") is False


def test_barrier_lock_excludes_concurrent(tmp_db):
    """While an oracle escalation is in flight, a second cannot start."""
    from engine.orchestrator.oracle_escalate import (
        acquire_oracle_lock, is_oracle_in_flight, should_escalate_to_oracle,
        _ensure_oracle_columns,
    )
    _ensure_oracle_columns()
    cfg = _make_cfg(enabled=True)
    # First escalation acquires the lock.
    assert acquire_oracle_lock("run-1", "T01") is True
    # Trigger evaluation sees an oracle in flight -> blocked.
    should, reason = should_escalate_to_oracle(
        "run-1", "T02", replan_exhausted=True, cfg=cfg)
    assert should is False
    assert "in flight" in reason.lower() or "barrier" in reason.lower()


def test_barrier_lock_different_runs_independent(tmp_db):
    """The oracle lock is per-run; two runs can escalate independently."""
    from engine.orchestrator.oracle_escalate import (
        acquire_oracle_lock, _ensure_oracle_columns,
    )
    _ensure_oracle_columns()
    assert acquire_oracle_lock("run-1", "T01") is True
    assert acquire_oracle_lock("run-2", "T01") is True  # different run


# ---------------------------------------------------------------------------
# escalate_task_to_oracle: disabled -> graceful degradation.
# ---------------------------------------------------------------------------

class _MockEscDigestTransport:
    """Transport stub: oracle port unreachable, fallback (35B) returns a
    valid digest. Exercises the 35B-fallback path for escalation."""

    def __init__(self, fallback_response=None, oracle_reachable=False):
        self.fallback_response = fallback_response or _make_digest_response()
        self.oracle_reachable = oracle_reachable
        self.calls = []

    def curl_beellama(self, port, messages, **kwargs):
        self.calls.append((port, messages, kwargs))
        if port == 8086 and not self.oracle_reachable:
            raise ConnectionError("oracle endpoint unreachable")
        return {"content": self.fallback_response, "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 300, "prompt_tokens": 200,
                "completion_tokens": 100, "predicted_per_second": 5.0}


def _make_digest_response():
    return json.dumps({
        "atoms": [
            {"atom_id": "T01-a", "title": "Stub endpoint",
             "description": "Create the route handler.",
             "acceptance_criteria": ["returns 200"], "depends_on": [],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": [],
    })


class _FakePipeline:
    """Minimal pipeline stand-in for escalation tests."""
    def __init__(self, run_id="run-1"):
        self.run_id = run_id


def test_escalate_disabled_returns_degraded():
    """When oracle disabled, escalate_task_to_oracle returns degraded (no crash)."""
    from engine.orchestrator.oracle_escalate import escalate_task_to_oracle
    from engine import Task
    cfg = _make_cfg(enabled=False)
    task = Task(id="T01", title="t", description="d", module="m.py")
    transport = _MockEscDigestTransport()
    result = escalate_task_to_oracle(
        task, _FakePipeline(), cfg, transport, ["T01"])
    assert result["escalated"] is False
    assert result["degraded"] is True
    # No transport call when disabled.
    assert len(transport.calls) == 0


def test_escalate_fallback_path(tmp_db):
    """Oracle unreachable -> 35B fallback succeeds -> escalated with atoms."""
    from engine.orchestrator.oracle_escalate import escalate_task_to_oracle
    from engine import Task
    cfg = _make_cfg(enabled=True)
    task = Task(id="T01", title="t", description="d", module="m.py",
                prd_section="Build a health check endpoint.")
    transport = _MockEscDigestTransport(oracle_reachable=False)
    result = escalate_task_to_oracle(
        task, _FakePipeline(), cfg, transport, ["T01"])
    assert result["escalated"] is True
    assert len(result["atoms"]) == 1
    assert result["atoms"][0].atom_id == "T01-a"
    assert result["fallback_used"] is True


def test_escalate_releases_lock_on_success(tmp_db):
    """The run-barrier lock is released after a successful escalation."""
    from engine.orchestrator.oracle_escalate import (
        escalate_task_to_oracle, is_oracle_in_flight,
    )
    from engine import Task
    cfg = _make_cfg(enabled=True)
    task = Task(id="T01", title="t", description="d", module="m.py",
                prd_section="Build a health check endpoint.")
    transport = _MockEscDigestTransport()
    escalate_task_to_oracle(
        task, _FakePipeline(), cfg, transport, ["T01"])
    assert is_oracle_in_flight("run-1") is False


def test_escalate_releases_lock_on_failure(tmp_db):
    """The run-barrier lock is released even when the digest fails."""
    from engine.orchestrator.oracle_escalate import (
        escalate_task_to_oracle, is_oracle_in_flight,
    )
    from engine import Task
    cfg = _make_cfg(enabled=True)
    task = Task(id="T01", title="t", description="d", module="m.py",
                prd_section="Build a health check endpoint.")
    # Both oracle and fallback return invalid output.
    bad_transport = _MockEscDigestTransport(
        fallback_response=json.dumps({"foo": "bar"}))
    result = escalate_task_to_oracle(
        task, _FakePipeline(), cfg, bad_transport, ["T01"])
    assert result["escalated"] is False
    assert is_oracle_in_flight("run-1") is False


def test_escalate_counts_against_budget(tmp_db):
    """A successful oracle escalation increments the per-run cap counter."""
    from engine.orchestrator.oracle_escalate import (
        escalate_task_to_oracle, get_oracle_escalation_count,
    )
    from engine import Task
    cfg = _make_cfg(enabled=True, max_escalations=2)
    task = Task(id="T01", title="t", description="d", module="m.py",
                prd_section="Build a health check endpoint.")
    transport = _MockEscDigestTransport()
    escalate_task_to_oracle(
        task, _FakePipeline(), cfg, transport, ["T01"])
    assert get_oracle_escalation_count("run-1") == 1


def test_escalate_records_telemetry(tmp_db):
    """A successful escalation records an escalation_ok telemetry event."""
    from engine.orchestrator.oracle_escalate import escalate_task_to_oracle
    from engine import Task
    from engine.state import _get_conn
    cfg = _make_cfg(enabled=True)
    task = Task(id="T01", title="t", description="d", module="m.py",
                prd_section="Build a health check endpoint.")
    transport = _MockEscDigestTransport()
    escalate_task_to_oracle(
        task, _FakePipeline(), cfg, transport, ["T01"])
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_type, atoms_count FROM oracle_events WHERE run_id = ? "
        "ORDER BY id", ("run-1",)).fetchall()
    conn.close()
    types = [r["event_type"] for r in rows]
    assert "escalation_ok" in types


# ---------------------------------------------------------------------------
# Aggregation of atom results (deterministic).
# ---------------------------------------------------------------------------

def test_aggregate_all_passed():
    """When all atoms pass, aggregation reports all_passed=True."""
    from engine.orchestrator.oracle_escalate import aggregate_atom_results
    atoms = []
    results = [
        {"atom_id": "T01-a", "ok": True},
        {"atom_id": "T01-b", "ok": True},
    ]
    agg = aggregate_atom_results(atoms, results)
    assert agg["all_passed"] is True
    assert agg["passed_count"] == 2
    assert agg["failed_count"] == 0


def test_aggregate_some_failed():
    """When some atoms pass and some fail, aggregation reports correctly."""
    from engine.orchestrator.oracle_escalate import aggregate_atom_results
    atoms = []
    results = [
        {"atom_id": "T01-a", "ok": True},
        {"atom_id": "T01-b", "ok": False, "error": "test failed"},
    ]
    agg = aggregate_atom_results(atoms, results)
    assert agg["all_passed"] is False
    assert agg["passed_count"] == 1
    assert agg["failed_count"] == 1
    assert "T01-b" in agg["failed_atoms"]


def test_aggregate_empty_results():
    """No atom results -> all_passed=False (nothing executed)."""
    from engine.orchestrator.oracle_escalate import aggregate_atom_results
    agg = aggregate_atom_results([], [])
    assert agg["all_passed"] is False
    assert agg["total"] == 0


def test_aggregate_all_failed():
    """When all atoms fail, aggregation reports all_passed=False."""
    from engine.orchestrator.oracle_escalate import aggregate_atom_results
    results = [
        {"atom_id": "T01-a", "ok": False},
        {"atom_id": "T01-b", "ok": False},
    ]
    agg = aggregate_atom_results([], results)
    assert agg["all_passed"] is False
    assert agg["failed_count"] == 2
    assert set(agg["failed_atoms"]) == {"T01-a", "T01-b"}


# ---------------------------------------------------------------------------
# Engine-level wiring: oracle escalation is invoked on re-plan exhaustion.
# ---------------------------------------------------------------------------

class _FailingGenTransport:
    """Generate always fails validation; oracle escalation is enabled and
    the fallback returns a valid digest. Verifies the engine invokes the
    oracle escalation path on re-plan exhaustion."""

    def __init__(self):
        self.generate_calls = 0
        self.oracle_calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        if port == 8086:
            # Oracle endpoint unreachable -> triggers fallback.
            self.oracle_calls += 1
            raise ConnectionError("oracle unreachable")
        if port == 8080:
            # 35B fallback returns a valid digest.
            self.oracle_calls += 1
            return {"content": _make_digest_response(),
                    "finish_reason": "stop", "reasoning_content": "",
                    "thinking_tokens": 0, "total_tokens": 300,
                    "prompt_tokens": 200, "completion_tokens": 100,
                    "predicted_per_second": 5.0}
        # Generation: always emit a bad import so validation fails.
        self.generate_calls += 1
        return {"content": "```python\nimport nonexistent_module_12345\n```",
                "finish_reason": "stop", "reasoning_content": "",
                "thinking_tokens": 0, "total_tokens": 50,
                "prompt_tokens": 20, "completion_tokens": 30,
                "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def _run_with_oracle(transport, max_retries_generate=2, max_replans_per_task=1):
    """Run the engine with oracle escalation enabled."""
    import engine.engine as eng_mod
    import engine.committer as cm
    from engine.engine import Engine, EngineConfig, Task

    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d",
                                        module="app.py",
                                        prd_section="Build a health check.")]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    cfg = EngineConfig(
        max_retries_generate=max_retries_generate,
        max_replans_per_task=max_replans_per_task,
        replan_model_port=8080,
        model_config="3070-qwen35-9b",
        oracle_enabled=True,
        oracle_port=8086,
    )
    try:
        eng = Engine(transport=transport, config=cfg)
        result = eng.run("/tmp/prd.md", "/tmp/project")
        return result
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


def test_engine_invokes_oracle_on_replan_exhaustion(tmp_db):
    """When the re-plan ladder exhausts and oracle is enabled, the engine
    invokes the oracle escalation path (telemetry records it)."""
    from engine.state import _get_conn
    transport = _FailingGenTransport()
    result = _run_with_oracle(transport, max_retries_generate=2,
                              max_replans_per_task=1)
    # The oracle escalation should have been attempted (oracle_calls > 0).
    assert transport.oracle_calls > 0
    # Telemetry: an oracle event was recorded.
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_type FROM oracle_events WHERE run_id = ?",
        (result.run_id,)).fetchall()
    conn.close()
    types = [r["event_type"] for r in rows]
    # At least one oracle-related event (unreachable, escalation_ok, etc.).
    assert len(types) > 0
