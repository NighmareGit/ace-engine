"""apply_blocks() — the pure seam for edit operations.

The only module that touches file content. No I/O, no LLM, no filesystem.
Fully deterministic and atomic: the first block that fails its exact-match
aborts the whole apply, returning (None, errors).

Mirrors the design contract in docs/features/retrieval-memory/DESIGN-G1.md §6.
"""

from __future__ import annotations

from typing import List, Tuple, Optional


def apply_blocks(
    file_content: str,
    blocks: list[object],
) -> Tuple[Optional[str], list[str]]:
    """Pure function: (file_content, blocks) -> (new_content | None, errors).

    Each block must expose ``old_string`` and ``new_string`` attributes (or
    dict-like access).  Blocks are applied sequentially; each sees the result
    of the previous block (so a block may replace text introduced by an
    earlier block).

    Matching is EXACT — ``str.count`` must equal 1.  Multi-match and zero-match
    both fail, reporting the match count so the caller can surface a precise
    error (never silent first-match).

    On the first failing block the entire apply returns ``(None, errors)`` —
    all-or-nothing (atomic).

    Args:
        file_content: The full current text of the target file.
        blocks: Ordered edit blocks; each has ``old_string``/``new_string``.

    Returns:
        ``(new_content, [])`` on success, or ``(None, [error, ...])`` on the
        first failure.
    """
    errors: list[str] = []
    content = file_content

    for i, block in enumerate(blocks):
        # Support both dataclass-style attribute access and dict-style access
        # (the validator/handler may pass either form).
        if isinstance(block, dict):
            old = block.get("old_string", "")
            new = block.get("new_string", "")
        else:
            old = getattr(block, "old_string", "")
            new = getattr(block, "new_string", "")

        count = content.count(old)
        if count == 0:
            errors.append(
                f"block {i}: old_string not found in file (match count=0)"
            )
            return None, errors
        if count > 1:
            errors.append(
                f"block {i}: old_string matches {count} times — "
                f"need exactly 1. Narrow the search anchor."
            )
            return None, errors

        content = content.replace(old, new)

    return content, []
