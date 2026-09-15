# PRD: edit-op handler integration — dispatch branch + EditTaskHandler facade

## Task G1-T04: edit-op handler integration — dispatch branch + EditTaskHandler facade

One task_type=='edit' branch in _resolve_handler() (engine/task_handler.py:64, lazy import). EditTaskHandler facade in engine/edit_ops/__init__.py orchestrates generate -> extract -> validate -> apply_blocks() -> write -> commit-gate read-back. Code-atom path unchanged. engine.py hook at 471-474 already diverts non-CodeTaskHandler (no change needed).

## Output contract (MANDATORY)
This task MODIFIES existing file(s): engine/task_handler.py.
Output the COMPLETE updated content of each file in ONE fenced code block whose FIRST LINE inside the block is:
# File: engine/task_handler.py
followed by the full file content. Do NOT create new files and do NOT omit any existing functions — the file content replaces the current file wholesale. NEVER write triple-backtick sequences (```) anywhere inside the file content — not in comments, docstrings, or string literals.
