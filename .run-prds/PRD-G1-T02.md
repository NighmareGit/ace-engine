# PRD: edit-op parser tier — Aider fence + XML fallback

## Task G1-T02: edit-op parser tier — Aider fence + XML fallback

EditOpExtractor.extract() in engine/edit_ops/extract.py parses Aider-style SEARCH/REPLACE fences (<<<<<<< SEARCH / ======= / >>>>>>> REPLACE) into EditOp dataclass {target_file, blocks: [{path, old_string, new_string}], raw_response}. XML <edit><old>...</old><new>...</new></edit> is the secondary fallback. Malformed blocks skipped with warning (never crash). Session-log row per LLM call. New code in engine/edit_ops/extract.py, NOT inline in generator.py.
