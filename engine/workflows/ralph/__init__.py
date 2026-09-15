"""Ralph loop — first-class ACE engine workflow (EPIC: ace-meta-cognitive-ralph-loop).

Public facade: ``run_ralph`` / ``resume_ralph``. All other modules are
implementation details — import them only if you need the internals.

Submodules (S2 §2 module map):
- ``round_driver`` — outer loop, the Ralph workflow core
- ``config`` — RalphConfig dataclass
- ``report_types`` — RoundReport + JSON schema validation
- ``gates`` — mechanical gate enforcement (R1/R4, ADR-0004)
- ``ideation`` — bounded divergent ideation (S5 #1/#8)
- ``pattern_gate`` — ADR-0002 pattern registry (R2/R6)
- ``pattern_query`` — SQL query interface (S5 #5)
"""

from engine.workflows.ralph.round_driver import run_ralph, resume_ralph, RalphResult
from engine.workflows.ralph.config import RalphConfig

__all__ = [
    "run_ralph",
    "resume_ralph",
    "RalphResult",
    "RalphConfig",
]
