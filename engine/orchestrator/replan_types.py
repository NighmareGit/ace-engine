"""Re-plan brain — types layer (T4a).

Transport-independent dataclasses for the re-plan request/response cycle.
These carry no HTTP or transport assumptions: the protocol layer adapts
them to the wire, the runtime layer consumes/produces them.
"""

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class RePlanRequest:
    """Everything the re-plan brain needs to build a re-plan prompt.

    Independent of transport: pure data, no I/O.
    """
    run_id: str
    task_id: str
    trigger_signature: tuple[str, str]       # (stage, error_class) from T2
    trigger_reason: str                       # human-readable trigger reason
    task_title: str
    task_description: str
    task_dependencies: list[str] = field(default_factory=list)
    error_history: list[dict] = field(default_factory=list)
    hypothesis: str | None = None             # prior hypothesis, if any
    replan_depth: int = 0                     # how many re-plans so far

    def to_dict(self) -> dict:
        d = asdict(self)
        # JSON-serialize the tuple signature as a list.
        d["trigger_signature"] = list(self.trigger_signature)
        return d


@dataclass
class RePlanPatch:
    """A validated patch produced by the re-plan brain.

    Mirrors the T3 strict schema; produced by parsing + validating the LLM
    response through T3's validate_patch before construction.
    """
    action: str
    task_id: str
    spec_patch: dict
    model_override: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RePlanResult:
    """Outcome of one re-plan attempt.

    ok=False means the LLM response was unusable (validation failure, parse
    failure, depth cap, transport error). The caller decides whether to fail
    the task or continue with the deterministic retry loop.
    """
    ok: bool
    validated: bool = False
    patch: dict | None = None
    raw_response: str | None = None
    error: str = ""
    tokens: int = 0
