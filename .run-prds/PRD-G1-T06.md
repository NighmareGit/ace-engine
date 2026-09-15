# PRD: edit-op metrics instrumentation — exact-apply rate counter + session log

## Task G1-T06: edit-op metrics instrumentation — exact-apply rate counter + session log

engine/edit_ops/metrics.py exposes first_apply_rate(run_id). edit_op_results table records applied=0/1 + match_count per attempt. Session log captures match count on failure. Enables the pre-registered >=90% first-apply success threshold to be measured. No engine/metrics.py (does not exist).

## Output contract (MANDATORY)
This task MODIFIES existing file(s): engine/state.py.
Output the COMPLETE updated content of each file in ONE fenced code block whose FIRST LINE inside the block is:
# File: engine/state.py
followed by the full file content. Do NOT create new files and do NOT omit any existing functions — the file content replaces the current file wholesale. NEVER write triple-backtick sequences (```) anywhere inside the file content — not in comments, docstrings, or string literals.
