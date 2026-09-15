"""Ralph-specific configuration (S2 §2 config.py).

Extends the engine's ``EngineConfig`` without modifying it. All Ralph tunables
live here so the rest of the engine stays unaware of Ralph.

Defaults are authoritative per EPIC.md "Config defaults" table and the S5/S6
amendments (G12 thresholds, workspace denylist per M8).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RalphConfig:
    """Ralph-loop configuration. All fields have safe defaults."""

    # -- Outer loop -------------------------------------------------------
    max_ralph_rounds: int = 5          # R7/S5: outer loop cap
    max_round_retries: int = 3         # per-round gate retry cap

    # -- Ideation (S5 #1, #8) --------------------------------------------
    ideation_candidates: int = 5       # N for divergent ideation (max 8)
    ideation_model_port: int = 8082    # 9B for ideation (speed)

    # -- Gates (ADR-0004) -------------------------------------------------
    review_model_port: int = 8080      # 35B judge for review
    redteam_model_port: int = 8080     # 35B judge for red-team
    redteam_escalate_to_oracle: bool = False  # P6 oracle escalation

    # -- Patterns (ADR-0002, S5 #3) --------------------------------------
    pattern_approval_required: bool = True   # ADR-0002 gate
    max_pattern_length_bytes: int = 8192     # per-pattern size cap
    max_patterns_per_run: int = 8            # M3: pattern spam cap

    # -- Budget caps (R3/T8, M2) ------------------------------------------
    # Per-round ceilings mirroring run-3's per-atom caps. 0 = unbounded.
    max_llm_calls: int = 40            # max LLM calls per round
    max_total_tokens: int = 2_000_000  # max total tokens per round
    max_wall_clock_s: int = 1800       # max wall-clock seconds per round

    # -- Per-round limits -------------------------------------------------
    round_timeout_s: int = 1800        # per-round wall-clock cap

    # -- G12 ideation-collapse (S5 #8) -----------------------------------
    g12_similarity_threshold: float = 0.75   # TF-IDF cosine trigger
    g12_consecutive_rounds: int = 2          # consecutive rounds before fire

    # -- Workspace denylist (M8) -----------------------------------------
    workspace_denylist: list[str] = field(default_factory=list)

    # -- Round task-type hint (T08 / S3) ----------------------------------
    # Drives _gate_sequence_type_for_round() routing: "research" →
    # Evidence->Review gate sequence; "edit"/anything-else → code sequence
    # (Dev->Review->Test->RedTeam).  An edit atom modifies code, so it gets
    # the full code gate sequence.  None/empty → defaults to "code".
    task_types: list[str] = field(default_factory=list)

    # -- G4-T02: memory-recall feature flag (DESIGN-G4 §3 flag C) ---------
    # Mirrors EngineConfig.enable_memory_recall.  The ralph loop copies the
    # engine flag into RalphConfig so ideation's recall_for_objective() call
    # is gated consistently.  Default False = flag-off identity.
    enable_memory_recall: bool = False

    def __post_init__(self) -> None:
        """Enforce invariants that the spec mandates."""
        # Ideation candidates: default 5, max 8 (EPIC §5 / S2 §5).
        if self.ideation_candidates < 1:
            self.ideation_candidates = 1
        if self.ideation_candidates > 8:
            self.ideation_candidates = 8
        if self.max_ralph_rounds < 1:
            self.max_ralph_rounds = 1
        if self.max_round_retries < 0:
            self.max_round_retries = 0
        if self.g12_similarity_threshold < 0.0:
            self.g12_similarity_threshold = 0.0
        if self.g12_similarity_threshold > 1.0:
            self.g12_similarity_threshold = 1.0
        if self.g12_consecutive_rounds < 1:
            self.g12_consecutive_rounds = 1
