"""O2 tests: PRD pre-digestion (oracle_digest).

The oracle digest layer splits a PRD / escalated task into atom tickets.
Two-layer design: the LLM proposes; the deterministic validator disposes.

These tests cover the 35B-fallback path (the testable-today path) with the
oracle endpoint MOCKED — no network. The validator tests cover:
  - schema rejection + retry
  - acyclic check (inject a cycle) — stdlib DFS, no networkx
  - coverage check (every original task mapped or carried)
  - size limits (atom count + per-atom token budget)
  - code-token rejection
  - recorded-fixture digest (no network)
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


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _make_valid_digest_response(parent_task_id="T01"):
    """A well-formed digest covering tasks T01, T02."""
    return json.dumps({
        "atoms": [
            {
                "atom_id": f"{parent_task_id}-a",
                "title": "Stub the endpoint",
                "description": "Create the route handler that returns 200.",
                "acceptance_criteria": ["route returns 200"],
                "depends_on": [],
                "source_task_ids": ["T01"],
            },
            {
                "atom_id": f"{parent_task_id}-b",
                "title": "Add tests",
                "description": "Write a test that hits the endpoint.",
                "acceptance_criteria": ["test passes"],
                "depends_on": [f"{parent_task_id}-a"],
                "source_task_ids": ["T02"],
            },
        ],
        "carried_task_ids": [],
    })


class _MockDigestTransport:
    """Transport stub: oracle port is UNREACHABLE, fallback (35B) returns a
    canned valid digest. This exercises the testable-today 35B-fallback path."""

    def __init__(self, fallback_response=None, oracle_reachable=False):
        self.fallback_response = fallback_response or _make_valid_digest_response()
        self.oracle_reachable = oracle_reachable
        self.calls = []   # (port, messages, kwargs)

    def curl_beellama(self, port, messages, **kwargs):
        self.calls.append((port, messages, kwargs))
        if port == 8086 and not self.oracle_reachable:
            # Oracle endpoint is NOT running — transport error.
            raise ConnectionError("oracle endpoint unreachable")
        # Fallback (35B judge on 8080) returns the canned digest.
        return {"content": self.fallback_response, "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 300, "prompt_tokens": 200,
                "completion_tokens": 100, "predicted_per_second": 5.0}


def _make_req(run_id="run-1", task_id="T01"):
    from engine.orchestrator.oracle_types import OracleRequest
    return OracleRequest(
        run_id=run_id, task_id=task_id,
        prd_text="Build a health check endpoint and test it.",
        task_title="Health check", task_description="Build api/health.py",
        task_dependencies=["T00"],
        acceptance_criteria=["returns 200", "test passes"],
        error_history=[{"stage": "validation", "error_class": "import"}],
        replan_history=[{"reason": "flask missing"}],
    )


def _make_cfg(enabled=True, **overrides):
    from engine.orchestrator.oracle_types import OracleConfig
    kwargs = {"enabled": enabled}
    kwargs.update(overrides)
    return OracleConfig(**kwargs)


# ---------------------------------------------------------------------------
# Protocol: payload build + response parse.
# ---------------------------------------------------------------------------

def test_build_digest_payload_structure():
    from engine.orchestrator.oracle_digest import build_digest_payload
    req = _make_req()
    cfg = _make_cfg()
    payload = build_digest_payload(req, cfg)
    assert "messages" in payload
    assert payload["max_tokens"] == cfg.max_tokens
    assert payload["temperature"] == cfg.temperature
    system = payload["messages"][0]["content"]
    user = payload["messages"][1]["content"]
    assert "atom" in system.lower()
    assert "JSON" in system
    # User prompt carries the PRD context.
    assert "T01" in user
    assert "health" in user.lower()


def test_parse_valid_digest_response():
    from engine.orchestrator.oracle_digest import parse_digest_response
    data = parse_digest_response(_make_valid_digest_response())
    assert "atoms" in data
    assert len(data["atoms"]) == 2
    assert data["atoms"][0]["atom_id"] == "T01-a"


def test_parse_fenced_digest_response():
    """Tolerates markdown fences (models do this)."""
    from engine.orchestrator.oracle_digest import parse_digest_response
    raw = "Here you go:\n```json\n" + _make_valid_digest_response() + "\n```"
    data = parse_digest_response(raw)
    assert data["atoms"][0]["atom_id"] == "T01-a"


def test_parse_empty_response_raises():
    from engine.orchestrator.oracle_digest import parse_digest_response
    with pytest.raises(ValueError, match="empty"):
        parse_digest_response("")


def test_parse_missing_atoms_key_raises():
    """A JSON object without 'atoms' is a schema violation."""
    from engine.orchestrator.oracle_digest import parse_digest_response
    with pytest.raises(ValueError, match="atoms"):
        parse_digest_response(json.dumps({"foo": "bar"}))


def test_parse_no_json_object_raises():
    from engine.orchestrator.oracle_digest import parse_digest_response
    with pytest.raises(ValueError, match="no JSON object"):
        parse_digest_response("just prose, no json")


# ---------------------------------------------------------------------------
# Validator: size limits.
# ---------------------------------------------------------------------------

def test_validate_size_limit_too_many_atoms():
    """More atoms than max_atoms_per_task is rejected."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg(max_atoms_per_task=1)
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
            {"atom_id": "T01-b", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T02"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01", "T02"], cfg)
    assert ok is False
    assert "too many" in err.lower()


def test_validate_size_limit_too_few_atoms():
    """Zero atoms is rejected (min_atoms_per_task=1)."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {"atoms": [], "carried_task_ids": []}
    ok, err = validate_digest(data, ["T01"], cfg)
    assert ok is False
    assert "too few" in err.lower()


def test_validate_atom_description_too_long():
    """An atom description exceeding the token budget is rejected."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg(max_atom_tokens=10)  # ~10 tokens budget
    long_desc = "word " * 100  # ~500 chars = ~125 tokens > 10
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": long_desc,
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01"], cfg)
    assert ok is False
    assert "too long" in err.lower()


# ---------------------------------------------------------------------------
# Validator: acyclicity (stdlib DFS).
# ---------------------------------------------------------------------------

def test_validate_detects_cycle():
    """A dependency cycle is detected by the stdlib DFS check."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-b"],
             "source_task_ids": ["T01"]},
            {"atom_id": "T01-b", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-a"],
             "source_task_ids": ["T02"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01", "T02"], cfg)
    assert ok is False
    assert "cycle" in err.lower()


def test_validate_detects_longer_cycle():
    """A 3-node cycle (a->b->c->a) is detected."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-c"],
             "source_task_ids": ["T01"]},
            {"atom_id": "T01-b", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-a"],
             "source_task_ids": ["T02"]},
            {"atom_id": "T01-c", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-b"],
             "source_task_ids": ["T03"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01", "T02", "T03"], cfg)
    assert ok is False
    assert "cycle" in err.lower()


def test_validate_allows_dag():
    """A valid DAG (a->b, a->c) passes acyclicity."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
            {"atom_id": "T01-b", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-a"],
             "source_task_ids": ["T02"]},
            {"atom_id": "T01-c", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-a"],
             "source_task_ids": ["T03"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01", "T02", "T03"], cfg)
    assert ok is True, err


def test_validate_rejects_unknown_dependency():
    """A depends_on referencing a non-existent atom_id is rejected."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": ["T01-z"],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01"], cfg)
    assert ok is False
    assert "unknown" in err.lower()


# ---------------------------------------------------------------------------
# Validator: coverage.
# ---------------------------------------------------------------------------

def test_validate_coverage_gap_rejected():
    """An original task not mapped to any atom or carried is rejected."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": [],
    }
    # T02 is not covered.
    ok, err = validate_digest(data, ["T01", "T02"], cfg)
    assert ok is False
    assert "uncovered" in err.lower()


def test_validate_coverage_via_carried():
    """A task in carried_task_ids counts as covered."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": ["T02"],
    }
    ok, err = validate_digest(data, ["T01", "T02"], cfg)
    assert ok is True, err


def test_validate_full_coverage_passes():
    """Every original task mapped to an atom passes coverage."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = json.loads(_make_valid_digest_response())
    ok, err = validate_digest(data, ["T01", "T02"], cfg)
    assert ok is True, err


# ---------------------------------------------------------------------------
# Validator: code-token rejection (ADR-0002).
# ---------------------------------------------------------------------------

def test_validate_rejects_code_in_atom_description():
    """A code-like token in an atom field is rejected."""
    from engine.orchestrator.oracle_digest import validate_digest
    cfg = _make_cfg()
    data = {
        "atoms": [
            {"atom_id": "T01-a", "title": "t",
             "description": "import os; os.system('rm -rf /')",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": [],
    }
    ok, err = validate_digest(data, ["T01"], cfg)
    assert ok is False
    assert "code" in err.lower()


# ---------------------------------------------------------------------------
# digest_prd: disabled -> degraded result (no transport call).
# ---------------------------------------------------------------------------

def test_digest_prd_disabled_returns_degraded():
    from engine.orchestrator.oracle_digest import digest_prd
    transport = _MockDigestTransport()
    req = _make_req()
    cfg = _make_cfg(enabled=False)
    result = digest_prd(req, cfg, transport, ["T01", "T02"])
    assert result.ok is False
    assert result.degraded is True
    assert "disabled" in result.error.lower()
    # No transport call when disabled.
    assert len(transport.calls) == 0


# ---------------------------------------------------------------------------
# digest_prd: 35B-fallback path (oracle unreachable, fallback succeeds).
# ---------------------------------------------------------------------------

def test_digest_prd_fallback_path(tmp_db):
    """Oracle endpoint unreachable -> falls back to 35B judge -> succeeds."""
    from engine.orchestrator.oracle_digest import digest_prd
    transport = _MockDigestTransport(oracle_reachable=False)
    req = _make_req()
    cfg = _make_cfg(enabled=True)
    result = digest_prd(req, cfg, transport, ["T01", "T02"])
    assert result.ok is True
    assert result.fallback_used is True
    assert result.atom_count == 2
    # The fallback response covered T01 and T02.
    source_ids = set()
    for a in result.atoms:
        source_ids.update(a.source_task_ids)
    assert {"T01", "T02"}.issubset(source_ids)
    # Two transport attempts: oracle (failed) + fallback (succeeded).
    assert len(transport.calls) == 2
    # First call to oracle port 8086, second to fallback port 8080.
    assert transport.calls[0][0] == 8086
    assert transport.calls[1][0] == 8080


def test_digest_prd_persists_atoms(tmp_db):
    """Successful digest persists atom tickets to oracle_atoms."""
    from engine.orchestrator.oracle_digest import digest_prd
    from engine.state import _get_conn
    transport = _MockDigestTransport()
    req = _make_req()
    cfg = _make_cfg(enabled=True)
    result = digest_prd(req, cfg, transport, ["T01", "T02"])
    assert result.ok is True
    # Verify persistence.
    conn = _get_conn()
    rows = conn.execute(
        "SELECT atom_id, parent_task_id FROM oracle_atoms WHERE run_id = ?",
        ("run-1",)
    ).fetchall()
    conn.close()
    atom_ids = {r["atom_id"] for r in rows}
    assert "T01-a" in atom_ids
    assert "T01-b" in atom_ids


def test_digest_prd_records_telemetry(tmp_db):
    """Successful digest records oracle_events telemetry."""
    from engine.orchestrator.oracle_digest import digest_prd
    from engine.state import _get_conn
    transport = _MockDigestTransport()
    req = _make_req()
    cfg = _make_cfg(enabled=True)
    digest_prd(req, cfg, transport, ["T01", "T02"])
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_type, atoms_count FROM oracle_events WHERE run_id = ? "
        "ORDER BY id",
        ("run-1",)
    ).fetchall()
    conn.close()
    assert len(rows) >= 1
    # The final event is digest_ok with 2 atoms (preceded by oracle_unreachable).
    assert rows[-1]["event_type"] == "digest_ok"
    assert rows[-1]["atoms_count"] == 2


# ---------------------------------------------------------------------------
# digest_prd: recorded fixture (no network) — both attempts fail.
# ---------------------------------------------------------------------------

def test_digest_prd_both_attempts_fail(tmp_db):
    """When oracle AND fallback both return invalid output, digest fails."""
    from engine.orchestrator.oracle_digest import digest_prd
    # Fallback returns invalid JSON (missing 'atoms' key).
    bad_transport = _MockDigestTransport(
        fallback_response=json.dumps({"foo": "bar"}))
    req = _make_req()
    cfg = _make_cfg(enabled=True)
    result = digest_prd(req, cfg, bad_transport, ["T01", "T02"])
    assert result.ok is False
    assert result.degraded is True


# ---------------------------------------------------------------------------
# digest_prd: retry on schema violation.
# ---------------------------------------------------------------------------

def test_digest_prd_retry_on_schema_violation(tmp_db):
    """A schema-violation response on the first attempt triggers a retry
    (bounded by max_digest_retries). With the fallback also failing, the
    digest ultimately fails but telemetry records the retry."""
    from engine.orchestrator.oracle_digest import digest_prd
    # Fallback returns a response that fails validation (coverage gap: T02
    # not covered).
    incomplete = json.dumps({
        "atoms": [
            {"atom_id": "T01-a", "title": "t", "description": "d",
             "acceptance_criteria": [], "depends_on": [],
             "source_task_ids": ["T01"]},
        ],
        "carried_task_ids": [],
    })
    transport = _MockDigestTransport(fallback_response=incomplete)
    req = _make_req()
    cfg = _make_cfg(enabled=True, max_digest_retries=1)
    result = digest_prd(req, cfg, transport, ["T01", "T02"])
    assert result.ok is False  # T02 uncovered -> validation fails
    assert result.degraded is True
