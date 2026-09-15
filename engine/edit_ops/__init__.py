"""Edit-ops facade (G1-T04): EditTaskHandler + pipeline entry points.

Thin facade — all logic lives in submodules (extract, apply, handler,
metrics, schema).  This module re-exports the handler and the core
dataclasses so ``from engine.edit_ops import EditTaskHandler`` works.
"""

from engine.edit_ops.extract import EditOpExtractor, EditBlock, EditOp  # noqa: F401
from engine.edit_ops.handler import EditTaskHandler, EditGenerateResult  # noqa: F401
from engine.edit_ops.apply import apply_blocks  # noqa: F401
from engine.edit_ops.schema import EditErrorContext  # noqa: F401
