# PRD: edit-op unit + integration tests

## Task G1-T07: edit-op unit + integration tests

Three test files: parser (fence + XML fallback + malformed skip), validator (stages 0-5 + multi-match count + path traversal), integration (apply a real edit to a temp file, assert disk==approved via commit-gate read-back). Mutation sensitivity >=90% on ambiguous-anchor cases (multi-match, empty old_string, path traversal, near-miss whitespace).
