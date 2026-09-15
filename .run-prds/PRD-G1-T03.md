# PRD: edit-op validator stages 0-5 — exact-match apply, multi-match fail-loud

## Task G1-T03: edit-op validator stages 0-5 — exact-match apply, multi-match fail-loud

EditValidator.validate() with stages 0-5: (0) blocks non-empty; (1) each block has non-empty old_string; (2) target file exists; (3) exact-match apply — file.count(old)==1, else fail with match count (multi-match fails WITH count, never silent first-match); (4) result parses (ast.parse if .py); (5) path traversal rejected (no .., no abs, no ~). Reuses retry ladder ('edit', check_name). New code in engine/edit_ops/validate.py, NOT inline in validator.py.
