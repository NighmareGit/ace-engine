"""O1 tests: oracle types + config.

The oracle is the capstone of the escalation ladder (9B → 35B → ORACLE).
It is NEVER on the hot path and never default-enabled. These tests cover:

  (a) OracleConfig defaults (disabled, port 8086 convention)
  (b) OracleConfig.effective_url derivation
  (c) EngineConfig oracle_* defaults (all disabled)
  (d) oracle_config_from_engine mapping
  (e) OracleRequest / AtomTicket / OracleResult dataclasses
  (f) OracleUnavailable is a normal, catchable state
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# (a) OracleConfig defaults — oracle DISABLED by default.
# ---------------------------------------------------------------------------

def test_oracle_config_disabled_by_default():
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    assert oc.enabled is False
    assert oc.endpoint_url == ""
    assert oc.model_name == ""


def test_oracle_config_port_convention():
    """The oracle convention port is 8086 (NOT running yet)."""
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    assert oc.port == 8086


def test_oracle_config_context_budget_default():
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    assert oc.context_budget == 32768
    assert oc.max_tokens == 8192


def test_oracle_config_expert_offload_defaults():
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    # Offload flags passthrough defaults.
    assert oc.n_gpu_layers == 999
    assert oc.enable_mmap is True
    assert oc.enable_ssd_expert_offload is False
    assert oc.expert_offload_pattern == ""


def test_oracle_config_ticket_limits_default():
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    assert oc.max_atom_tokens == 2000
    assert oc.max_atoms_per_task == 12
    assert oc.min_atoms_per_task == 1


def test_oracle_config_escalation_budget_default():
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    assert oc.max_oracle_escalations_per_run == 1
    assert oc.max_digest_retries == 1


# ---------------------------------------------------------------------------
# (b) effective_url derivation.
# ---------------------------------------------------------------------------

def test_effective_url_from_endpoint_url():
    """When endpoint_url is set, it wins."""
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig(endpoint_url="http://host:8086")
    assert oc.effective_url() == "http://host:8086"


def test_effective_url_falls_back_to_port():
    """When endpoint_url is empty, derive from port."""
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig(port=9090)
    assert oc.effective_url() == "http://127.0.0.1:9090"


def test_effective_url_default_port():
    from engine.orchestrator.oracle_types import OracleConfig
    oc = OracleConfig()
    assert oc.effective_url() == "http://127.0.0.1:8086"


# ---------------------------------------------------------------------------
# (c) EngineConfig oracle_* defaults — all disabled.
# ---------------------------------------------------------------------------

def test_engineconfig_oracle_disabled_by_default():
    from engine import EngineConfig
    cfg = EngineConfig()
    assert cfg.oracle_enabled is False
    assert cfg.oracle_endpoint_url == ""
    assert cfg.oracle_model_name == ""


def test_engineconfig_oracle_port_default():
    from engine import EngineConfig
    cfg = EngineConfig()
    assert cfg.oracle_port == 8086


def test_engineconfig_oracle_budget_defaults():
    from engine import EngineConfig
    cfg = EngineConfig()
    assert cfg.oracle_context_budget == 32768
    assert cfg.oracle_max_tokens == 8192
    assert cfg.oracle_max_escalations_per_run == 1
    assert cfg.oracle_max_digest_retries == 1


def test_engineconfig_oracle_validator_defaults():
    from engine import EngineConfig
    cfg = EngineConfig()
    assert cfg.oracle_max_atom_tokens == 2000
    assert cfg.oracle_max_atoms_per_task == 12
    assert cfg.oracle_min_atoms_per_task == 1


# ---------------------------------------------------------------------------
# (d) oracle_config_from_engine mapping.
# ---------------------------------------------------------------------------

def test_oracle_config_from_engine_disabled():
    """Default EngineConfig maps to a disabled OracleConfig."""
    from engine import EngineConfig, oracle_config_from_engine
    cfg = EngineConfig()
    oc = oracle_config_from_engine(cfg)
    assert oc.enabled is False
    assert oc.port == 8086


def test_oracle_config_from_engine_enabled():
    """An enabled oracle config maps through faithfully."""
    from engine import EngineConfig, oracle_config_from_engine
    cfg = EngineConfig(
        oracle_enabled=True,
        oracle_endpoint_url="http://host:8086",
        oracle_model_name="Qwen3-30B-A3B-Q4_K_M.gguf",
        oracle_max_atoms_per_task=8,
        oracle_max_escalations_per_run=2,
    )
    oc = oracle_config_from_engine(cfg)
    assert oc.enabled is True
    assert oc.endpoint_url == "http://host:8086"
    assert oc.model_name == "Qwen3-30B-A3B-Q4_K_M.gguf"
    assert oc.max_atoms_per_task == 8
    assert oc.max_oracle_escalations_per_run == 2


def test_oracle_config_from_engine_custom_port():
    from engine import EngineConfig, oracle_config_from_engine
    cfg = EngineConfig(oracle_port=9090)
    oc = oracle_config_from_engine(cfg)
    assert oc.port == 9090


# ---------------------------------------------------------------------------
# (e) OracleRequest / AtomTicket / OracleResult dataclasses.
# ---------------------------------------------------------------------------

def test_oracle_request_dataclass():
    from engine.orchestrator.oracle_types import OracleRequest
    req = OracleRequest(
        run_id="run-1", task_id="T01",
        prd_text="Build a health check endpoint.",
        task_title="Health check", task_description="Build api/health.py",
        task_dependencies=["T00"],
        acceptance_criteria=["returns 200"],
    )
    assert req.task_id == "T01"
    d = req.to_dict()
    assert d["task_id"] == "T01"
    assert d["task_dependencies"] == ["T00"]


def test_atom_ticket_dataclass():
    from engine.orchestrator.oracle_types import AtomTicket
    at = AtomTicket(
        atom_id="T01-a", parent_task_id="T01",
        title="Stub endpoint", description="Create the route handler",
        acceptance_criteria=["route registered"],
        depends_on=[], source_task_ids=["T01"],
    )
    assert at.atom_id == "T01-a"
    d = at.to_dict()
    assert d["parent_task_id"] == "T01"
    assert d["source_task_ids"] == ["T01"]


def test_oracle_result_empty():
    from engine.orchestrator.oracle_types import OracleResult
    r = OracleResult(ok=False, error="unreachable")
    assert r.ok is False
    assert r.atom_count == 0
    assert r.atoms == []


def test_oracle_result_with_atoms():
    from engine.orchestrator.oracle_types import OracleResult, AtomTicket
    atoms = [
        AtomTicket(atom_id="T01-a", parent_task_id="T01", title="t", description="d"),
        AtomTicket(atom_id="T01-b", parent_task_id="T01", title="t2", description="d2"),
    ]
    r = OracleResult(ok=True, atoms=atoms, tokens=500)
    assert r.ok is True
    assert r.atom_count == 2
    assert r.tokens == 500


def test_oracle_result_fallback_flag():
    """fallback_used=True marks the 35B-judge fallback path."""
    from engine.orchestrator.oracle_types import OracleResult
    r = OracleResult(ok=True, fallback_used=True)
    assert r.fallback_used is True


def test_oracle_result_degraded_flag():
    """degraded=True marks graceful degradation (oracle down)."""
    from engine.orchestrator.oracle_types import OracleResult
    r = OracleResult(ok=False, degraded=True, error="oracle unreachable")
    assert r.degraded is True


# ---------------------------------------------------------------------------
# (f) OracleUnavailable — a normal, catchable state.
# ---------------------------------------------------------------------------

def test_oracle_unavailable_is_exception():
    from engine.orchestrator.oracle_types import OracleUnavailable
    with pytest.raises(OracleUnavailable):
        raise OracleUnavailable("endpoint down")


def test_oracle_unavailable_catchable_as_exception():
    """Every call site catches OracleUnavailable and degrades gracefully."""
    from engine.orchestrator.oracle_types import OracleUnavailable
    caught = False
    try:
        raise OracleUnavailable("down")
    except OracleUnavailable as e:
        caught = True
        assert "down" in str(e)
    assert caught


def test_oracle_unavailable_message():
    from engine.orchestrator.oracle_types import OracleUnavailable
    e = OracleUnavailable("http://host:8086 unreachable: connection refused")
    assert "8086" in str(e)
