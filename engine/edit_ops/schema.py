"""Shared edit-ops dataclasses (G1 review gate).

Holds the structured error context that flows from a failed attempt into the
next attempt's LLM prompt, replacing the anonymous ``type("ErrorContext", ...)``
objects previously constructed inline in the handler loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from engine.edit_ops.extract import EditOp


@dataclass
class EditErrorContext:
    """Carries the failure context from one edit attempt into the next.

    Attributes:
        previous_edit_op: the EditOp that failed validation / apply.
        validation_errors: human-readable error strings surfaced to the LLM.
        attempt_number: 1-based index of the attempt that produced this error.
    """
    previous_edit_op: EditOp
    validation_errors: List[str] = field(default_factory=list)
    attempt_number: int = 0
