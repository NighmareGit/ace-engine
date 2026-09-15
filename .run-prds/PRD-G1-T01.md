# PRD: edit-op schema — task_type 'edit' + search/replace block contract

## Task G1-T01: edit-op schema — task_type 'edit' + search/replace block contract

Additive DDL + CATEGORY_TO_TYPE registration for the new 'edit' task_type. init_db() adds the edit_op_results table idempotently (id, run_id, task_id, attempt, match_count, applied, created_at). 'edit' added to CATEGORY_TO_TYPE dict (engine/prd.py:33). Additive-only: no ALTER on existing tables. No engine/models.py (does not exist — atom kind lives in CATEGORY_TO_TYPE, not an enum).

## Output contract (MANDATORY)
This task MODIFIES existing file(s): engine/state.py, engine/prd.py.
Output the COMPLETE updated content of each file in ONE fenced code block whose FIRST LINE inside the block is:
# File: engine/state.py
followed by the full file content. Do NOT create new files and do NOT omit any existing functions — the file content replaces the current file wholesale. NEVER write triple-backtick sequences (```) anywhere inside the file content — not in comments, docstrings, or string literals.
