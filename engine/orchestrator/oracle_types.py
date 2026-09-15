"""Oracle tier — types layer (O1a).

Transport-independent dataclasses for the P6 oracle escalation path. Three-
layer separation (mirrors the re-plan brain):

  (a) types     — this file: OracleConfig / OracleRequest / OracleResult / AtomTicket
  (b) protocol  — oracle_digest.py: prompt build + response parse + validation
  (c) runtime   — oracle_escalate.py: trigger + escalation wiring into engine.py

The oracle is the capstone of the escalation ladder (9B subject → 35B re-plan
→ ORACLE). It is NEVER on the hot path and never default-enabled: OracleConfig
defaults keep it disabled until an owner explicitly configures an endpoint.

Design invariants (ADR-0002 + P6 spec):
  - The oracle PROPOSES atom tickets; the existing pipeline EXECUTES them; the
    validator GATE stays authoritative.
  - OracleUnavailable is a normal state: every call site degrades gracefully
    (log + telemetry + fall through to existing behavior).
  - One oracle escalation per task per run (no recursive oracle).
"""

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Oracle configuration (disabled by default).
# ---------------------------------------------------------------------------

@dataclass
class OracleConfig:
    """Oracle endpoint + behaviour configuration.

    Defaults keep the oracle DISABLED: enabled=False and no endpoint mean every
    oracle call site falls through to existing behaviour. The owner must
    explicitly enable + configure an endpoint (port 8086 by convention — NOT
    running yet).
    """
    enabled: bool = False
    endpoint_url: str = ""          # e.g. "http://host:8086" (empty = unset)
    port: int = 8086                # llama-server oracle endpoint (convention)
    model_name: str = ""            # e.g. "Qwen3-30B-A3B-Q4_K_M.gguf"
    context_budget: int = 32768      # max context tokens for the oracle call
    max_tokens: int = 8192           # max completion tokens
    temperature: float = 0.2
    timeout_s: int = 600             # oracle calls are latency-tolerant (digest)
    # Expert-offload flags — passed through to llama-server flags verbatim.
    n_gpu_layers: int = 999          # layers offloaded to GPU (-ngl)
    enable_mmap: bool = True         # use mmap for weight offload
    enable_ssd_expert_offload: bool = False  # -ot expert offload patterns
    expert_offload_pattern: str = ""  # e.g. "0-31" for -ot
    # Ticket-size limits (validator-enforced).
    max_atom_tokens: int = 2000      # max tokens per atom ticket description
    max_atoms_per_task: int = 12     # max atom tickets per digested task
    min_atoms_per_task: int = 1      # min atom tickets (else digest rejected)
    # Escalation budget (T8 extension).
    max_oracle_escalations_per_run: int = 1   # one oracle escalation per run
    # Retry on schema violation (strict-validation pattern, mirrors T3/replan).
    max_digest_retries: int = 1

    def effective_url(self) -> str:
        """Return the full endpoint URL, deriving from endpoint_url or port."""
        if self.endpoint_url:
            return self.endpoint_url
        if self.port:
            return f"http://127.0.0.1:{self.port}"
        return ""


# ---------------------------------------------------------------------------
# Oracle request / response.
# ---------------------------------------------------------------------------

@dataclass
class OracleRequest:
    """Everything the oracle digest layer needs to build a digest prompt.

    Independent of transport: pure data, no I/O. Built from the parent task's
    PRD text + the task list the oracle must decompose into atoms.
    """
    run_id: str
    task_id: str                     # the parent task being escalated
    prd_text: str                    # the full PRD (or the task's PRD section)
    task_title: str
    task_description: str
    task_dependencies: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    error_history: list[dict] = field(default_factory=list)  # from re-plan exhaustion
    replan_history: list[dict] = field(default_factory=list)  # prior re-plan attempts

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "prd_text": self.prd_text,
            "task_title": self.task_title,
            "task_description": self.task_description,
            "task_dependencies": list(self.task_dependencies),
            "acceptance_criteria": list(self.acceptance_criteria),
            "error_history": list(self.error_history),
            "replan_history": list(self.replan_history),
        }


@dataclass
class AtomTicket:
    """One atom ticket produced by oracle pre-digestion.

    Each ticket is independently implementable + verifiable, carries explicit
    acceptance criteria and dependency edges to other atoms. The LLM proposes;
    the validator (oracle_digest.py) disposes — enforcing size limits, acyclicity,
    and coverage.
    """
    atom_id: str                     # e.g. "T01-a"
    parent_task_id: str              # the task this atom was derived from
    title: str
    description: str                 # full implementation description
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)  # other atom_ids
    source_task_ids: list[str] = field(default_factory=list)  # original PRD tasks covered

    def to_dict(self) -> dict:
        return {
            "atom_id": self.atom_id,
            "parent_task_id": self.parent_task_id,
            "title": self.title,
            "description": self.description,
            "acceptance_criteria": list(self.acceptance_criteria),
            "depends_on": list(self.depends_on),
            "source_task_ids": list(self.source_task_ids),
        }


@dataclass
class OracleResult:
    """Outcome of one oracle call (digest or escalation).

    ok=False means the oracle was unreachable, returned an unparseable response,
    or the validator rejected the output after retries. The caller degrades
    gracefully (log + telemetry + fall through).
    """
    ok: bool
    atoms: list[AtomTicket] = field(default_factory=list)
    raw_response: str | None = None
    error: str = ""
    tokens: int = 0
    fallback_used: bool = False   # True when the 35B judge was used instead
    degraded: bool = False        # True when oracle unavailable, fell through

    @property
    def atom_count(self) -> int:
        return len(self.atoms)


# ---------------------------------------------------------------------------
# OracleUnavailable — a normal state, never an exception that escapes.
# ---------------------------------------------------------------------------

class OracleUnavailable(Exception):
    """Raised internally when the oracle endpoint is unreachable.

    Caught at every oracle call site and converted into a graceful degradation
    event (log + telemetry + fall through). Never propagates to the engine.
    """
    pass
