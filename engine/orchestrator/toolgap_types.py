"""Tool-Gap Detector (TGD) — types layer (ORCH-7, Wave-2).

Pure dataclasses for the gap report the detector emits. The detector is
mechanical (SQL + regex over engine.db telemetry); the LLM — later — triages
this report. The detector NEVER invents gaps: every Finding carries concrete
evidence rows, and the report is fully reproducible from the DB state.

Signal catalogue G1–G11 (see .scratch/specs/toolgap-detector.md). G7/G9/G10 are
IIL-gated (L4 LISTEN layer): they degrade to empty results pre-IIL, never
no-op the whole detector.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class SignalId(str, Enum):
    """The 11 mechanical signals over engine.db telemetry."""
    G1_REPEATED_FAILURE_SIGNATURE = "G1"   # same (stage, error_class) >= N times
    G2_ESCALATION_MISSING_INFO = "G2"       # re-plan reason: missing info/context
    G3_RETRY_EXHAUSTION = "G3"              # retry exhaustion -> FAIL
    G4_CONTEXT_BLOAT = "G4"                 # prompt_tokens near cap
    G5_JUDGE_LOW_SUBCLUSTER = "G5"          # judge low sub-scores clustered
    G6_ALLOWLIST_ADDITION = "G6"            # dependency_allowlist additions
    G7_ROUTER_UNCERTAIN = "G7"              # IIL router uncertain (IIL-gated)
    G8_HUMAN_INTERVENTION = "G8"            # stop-file / abort / manual merge
    G9_SKILL_LOOKUP_MISS = "G9"             # skill-lookup miss (skills-gated)
    G10_HALLUCINATED_TOOL_CALL = "G10"      # tool call not in registry (IIL-gated)
    G11_EXTERNAL_REFERENCE_FAILURE = "G11"  # failures citing unreachable refs


# Signals that require IIL/skills machinery and degrade gracefully to no-op
# pre-IIL. The spec calls these out explicitly (G7/G9/G10); we keep them as
# first-class enum members so a report always shows the full catalogue, but
# their extractors return [] until the source data exists.
GATED_SIGNALS: set[SignalId] = {
    SignalId.G7_ROUTER_UNCERTAIN,
    SignalId.G9_SKILL_LOOKUP_MISS,
    SignalId.G10_HALLUCINATED_TOOL_CALL,
}


class TriageCategory(str, Enum):
    """Suggested triage category for a gap — the LLM refines this later."""
    VALIDATOR_REPAIR = "validator_repair"         # G1
    READ_RETRIEVAL_TOOL = "read_retrieval_tool"   # G2/G4
    NEW_CAPABILITY = "new_capability"             # G3
    PRODUCTIVITY_TOOL = "productivity_tool"       # G5
    DEPENDENCY_TOOLING = "dependency_tooling"     # G6
    REGISTRY_ENTRY = "registry_entry"             # G7/G10
    EGRESS_TOOL = "egress_tool"                   # G11
    SKILL_DOC = "skill_doc"                       # G9
    HUMAN_INVESTIGATE = "human_investigate"       # G8
    UNKNOWN = "unknown"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ALWAYS = "always"   # G8: human interventions always reported


@dataclass
class EvidenceRow:
    """One concrete telemetry row backing a finding."""
    run_id: str
    task_id: str | None
    signature: str          # normalized signature / description
    count: int = 1          # occurrences aggregated into this row
    detail: str = ""        # extra context (error snippet, score, etc.)


@dataclass
class Finding:
    """One detected tool-gap signal instance."""
    signal: SignalId
    severity: Severity
    confidence: float       # 0.0–1.0
    triage: TriageCategory
    message: str            # human-readable summary
    evidence: list[EvidenceRow] = field(default_factory=list)
    # Kill-criterion instrumentation: triage outcome (manual for now).
    triage_outcome: str = ""        # "accepted" | "rejected" | "deferred" | ""
    triage_note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signal"] = self.signal.value
        d["severity"] = self.severity.value
        d["triage"] = self.triage.value
        d["evidence"] = [asdict(e) for e in self.evidence]
        return d


@dataclass
class GapReport:
    """Complete gap report for one run (or a history scan)."""
    run_id: str | None          # None for history scans
    scanned_runs: int           # how many runs were examined
    findings: list[Finding]
    generated_at: str           # ISO timestamp
    detector_version: str = "1.0"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "scanned_runs": self.scanned_runs,
            "findings": [f.to_dict() for f in self.findings],
            "generated_at": self.generated_at,
            "detector_version": self.detector_version,
        }

    def findings_for_signal(self, sig: SignalId) -> list[Finding]:
        return [f for f in self.findings if f.signal == sig]

    @property
    def actionable_count(self) -> int:
        """Triage-yield numerator: findings accepted as actionable."""
        return sum(1 for f in self.findings if f.triage_outcome == "accepted")
