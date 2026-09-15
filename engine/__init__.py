"""Engine package — the one autonomous coding engine."""

from dataclasses import dataclass, field
from typing import Callable

from engine.judge import judge_run, JudgeVerdict, ScoreDimension  # noqa: F401

VERSION = "0.1.0"

@dataclass
class Task:
    """A single atomic coding unit derived from a PRD."""
    id: str                      # e.g. "T01"
    title: str                   # e.g. "Create health check endpoint"
    description: str             # Full task description from PRD
    module: str                  # PRIMARY target file, e.g. "api/health.py"
    dependencies: list[str] = field(default_factory=list)  # Task IDs this depends on
    task_type: str = "implementation"  # implementation | refactoring | debug | test
    priority: int = 1            # 1=highest
    prd_section: str = ""        # Original PRD section text
    files: list[str] = field(default_factory=list)  # ALL target files (module + extras); empty = [module]
    acceptance_criteria: list[str] = field(default_factory=list)  # from PRDParser

@dataclass
class EngineConfig:
    """Configuration for an engine run."""
    model_config: str = "3090-qwen36-35b"
    max_retries_generate: int = 3
    max_retries_test: int = 2
    max_retries_commit: int = 2  # P2 — bounds commit retry on retryable push failure
    timeout_inference: int = 300
    timeout_test: int = 180
    timeout_commit: int = 60
    timeout_context: int = 120
    dry_run: bool = False
    sandbox: bool = False
    judge: bool = False
    # --- Epic-5 self-scoring config (spec: docs/epic5-self-scoring-spec.md) ---
    judge_mode: str = "off"  # off | deterministic | llm | full
    judge_port: int = 8080   # pinned judge (3090); never the model under test
    subject_port: int = 8082
    judge_temperature: float = 0.0
    # T08 evidence: 2048 truncated long-form orchestrator/multi-turn answers.
    # Live evidence 2026-09-09: the 9B-MTP subject spends ~650 tokens on
    # reasoning before any content (finish_reason=length at 2048, clean stop
    # at 4096) — so the coder role gets 4096 too.
    max_tokens_by_role: dict = field(default_factory=lambda: {
        "orchestrator": 4096,
        "multi-turn": 4096,
        "coder": 4096,
        "researcher": 8192,
    })
    on_event: Callable | None = None
    # --- P4 results archive ---
    archive_results: bool = True  # opt-out for pure-local dry runs
    archive_keep: int = 30        # .db snapshots retained per project (JSON forever)
    # --- T8 orchestrator budget caps (0 = unbounded) ---
    max_llm_calls: int = 0
    max_total_tokens: int = 0
    max_wall_clock_s: int = 0
    # --- T4 re-plan brain ---
    replan_model_port: int = 8080     # 35B judge endpoint for re-plans
    max_replans_per_task: int = 2    # depth cap per task per run
    # --- Research verdict generation ---
    max_research_generate: int = 3   # generate+validate retry budget for research atoms
    # --- P6 oracle tier (O1) ---
    oracle_enabled: bool = False           # oracle DISABLED by default
    oracle_endpoint_url: str = ""          # e.g. "http://host:8086"
    oracle_port: int = 8086                # llama-server oracle endpoint (convention)
    oracle_model_name: str = ""            # e.g. "Qwen3-30B-A3B-Q4_K_M.gguf"
    oracle_context_budget: int = 32768     # max context tokens
    oracle_max_tokens: int = 8192          # max completion tokens
    oracle_temperature: float = 0.2
    oracle_timeout_s: int = 600            # latency-tolerant digest
    oracle_n_gpu_layers: int = 999         # -ngl passthrough
    oracle_enable_mmap: bool = True
    oracle_enable_ssd_expert_offload: bool = False
    oracle_expert_offload_pattern: str = ""  # -ot pattern passthrough
    oracle_max_atom_tokens: int = 2000     # validator: max tokens per atom
    oracle_max_atoms_per_task: int = 12    # validator: max atoms per digest
    oracle_min_atoms_per_task: int = 1     # validator: min atoms per digest
    oracle_max_escalations_per_run: int = 1  # one oracle escalation per run
    oracle_max_digest_retries: int = 1     # schema-violation retries
    # --- G4-T02: retrieval + memory-recall feature flags (DESIGN-G4 §3) ---
    # Both default OFF so existing runs are byte-identical (flag-off identity).
    # enable_retrieval gates the RepoMapBuilder + assemble_retrieved call chain
    # inside the research handler's CONTEXT stage.  enable_memory_recall gates
    # the recall_for_objective() call in ralph ideation.
    enable_retrieval: bool = False         # G2 retrieval lane (repo-map → embeddings)
    enable_memory_recall: bool = False     # G3 memory lane (poisoning-gated recall)
    # LEDGER-63: fail fast when a research task declares no source files.
    # When True (default), ResearchTaskHandler.run() raises ResearchSourceError
    # for tasks whose description references zero existing source files.
    # Set False in tests that exercise the handler with mock transports.
    enforce_research_sources: bool = True


def oracle_config_from_engine(cfg: "EngineConfig") -> "OracleConfig":
    """Build an OracleConfig from an EngineConfig's oracle_* fields.

    Keeps the two config objects decoupled: EngineConfig stays the single
    user-facing config surface; OracleConfig is the oracle layer's internal
    view. Defaults keep the oracle disabled.
    """
    # Local import to avoid a circular import at module load.
    from engine.orchestrator.oracle_types import OracleConfig
    return OracleConfig(
        enabled=cfg.oracle_enabled,
        endpoint_url=cfg.oracle_endpoint_url,
        port=cfg.oracle_port,
        model_name=cfg.oracle_model_name,
        context_budget=cfg.oracle_context_budget,
        max_tokens=cfg.oracle_max_tokens,
        temperature=cfg.oracle_temperature,
        timeout_s=cfg.oracle_timeout_s,
        n_gpu_layers=cfg.oracle_n_gpu_layers,
        enable_mmap=cfg.oracle_enable_mmap,
        enable_ssd_expert_offload=cfg.oracle_enable_ssd_expert_offload,
        expert_offload_pattern=cfg.oracle_expert_offload_pattern,
        max_atom_tokens=cfg.oracle_max_atom_tokens,
        max_atoms_per_task=cfg.oracle_max_atoms_per_task,
        min_atoms_per_task=cfg.oracle_min_atoms_per_task,
        max_oracle_escalations_per_run=cfg.oracle_max_escalations_per_run,
        max_digest_retries=cfg.oracle_max_digest_retries,
    )


def task_role(task) -> str:
    """Classify a task's role for max_tokens selection (spec §3).

    Multi-turn tasks and orchestrator-flavoured task types get the raised
    budget; research tasks get the researcher role (max_tokens 8192);
    everything else defaults to coder.
    """
    ttype = (getattr(task, "task_type", "") or "").lower()
    role = getattr(task, "role", None)
    if role:
        return str(role).lower()
    if ttype == "research":
        return "researcher"
    if "multi" in ttype:
        return "multi-turn"
    if any(k in ttype for k in ("orchestr", "plan", "review")):
        return "orchestrator"
    return "coder"
