# DESIGN-G1 — edit-ops deep-module design

> Owner: NEW atom kind `edit`. Pattern to mirror: `engine/research/` (non-code
> package precedent, EPIC.md). Grain: `task_type` field is the dispatch key
> (`engine/__init__.py:18`, `engine/task_handler.py:63`); `edit` is a new
> `task_type`, not a new `kind` field. File refs cite `path:line`.

---

## 1. Architecture overview

```
PRD (JSON, kind:"edit")
   │
   ▼
prd.py ─ CATEGORY_TO_TYPE["edit"] = "edit"          (engine/prd.py:26)
   │
   ▼  Task(task_type="edit", module="path/to/file.py")
engine.py:_run_task()
   │
   ├─ _resolve_handler() ──► EditTaskHandler          (engine/task_handler.py:56)
   │       │
   │       ▼
   │   generate() → EditOpExtractor.extract()         (engine/generator.py:148+)
   │       │
   │       ▼
   │   EditValidator.validate()                       (engine/validator.py:10+)
   │       │
   │       ▼
   │   apply_blocks()  ◄── pure function, no I/O     (engine/edit_ops/apply.py)
   │       │
   │       ▼
   │   commit-gate: read-back == applied              (engine/committer.py:169)
   │
   └─ (code path untouched)
```

**Package layout** (mirrors `engine/research/`):

```
engine/edit_ops/
  __init__.py        # EditTaskHandler.run() facade
  schema.py          # EditBlock dataclass + EditOp dataclass
  extract.py         # EditOpExtractor — parser tier
  validate.py        # EditValidator — staged checks
  apply.py           # apply_blocks() — pure function (the seam)
  metrics.py         # exact-apply rate counter
```

---

## 2. `kind` vs `task_type` — decision

**Decision: extend `task_type`, do NOT add a `kind` field.**

Rationale — `task_type` is already the dispatch key everywhere:
- `engine/task_handler.py:63` — `getattr(task, "task_type", "")` selects handler
- `engine/__init__.py:121` — `task_role()` reads `task_type` for max_tokens
- `engine/prd.py:55` — `CATEGORY_TO_TYPE` maps PRD category → `task_type`
- `engine/generator.py:104` — `task_role(task)` drives `max_tokens`

Adding a parallel `kind` field would fork every dispatch point. Instead:
add `"edit"` to `CATEGORY_TO_TYPE` (`engine/prd.py:33` → `"edit": "edit"`),
and `_resolve_handler()` gains one branch at `task_handler.py:64`.

---

## 3. Interfaces

### 3.1 `EditBlock` + `EditOp` (schema.py)

```python
@dataclass
class EditBlock:
    """One search/replace operation."""
    path: str          # relative to project root (sanitized)
    old_string: str    # exact text to find (non-empty)
    new_string: str    # replacement text (may be empty = deletion)

@dataclass
class EditOp:
    """Parsed edit task: target file + ordered edit blocks."""
    target_file: str              # == task.module (primary file)
    blocks: list[EditBlock]       # ordered; applied sequentially
    raw_response: str             # full LLM output (forensic)
```

### 3.2 `EditOpExtractor.extract()` (extract.py)

```python
def extract(raw_response: str, task: Task) -> EditOp:
    """Parse LLM response into EditOp.

    Format: Aider-style SEARCH/REPLACE fences (§4 format rationale).
    Falls back to <edit><old>…</old><new>…</new></edit> XML tags.
    Malformed blocks skipped with warning (never crash).
    """
```

### 3.3 `EditValidator.validate()` (validate.py)

```python
def validate(edit_op: EditOp, project_path: str, task: Task) -> EditValidationResult:
    """Staged validation (mirrors validator.py:10 pattern).

    Returns EditValidationResult(passed, stages, applied_content).
    """
```

### 3.4 `apply_blocks()` (apply.py) — THE seam

```python
def apply_blocks(file_content: str, blocks: list[EditBlock]) -> tuple[str | None, list[str]]:
    """Pure function: (file_content, blocks) -> (new_content | None, errors).

    No I/O. No LLM. Fully deterministic. This is the testable seam.
    On first error: returns (None, [error]) — all-or-nothing (atomic apply).
    """
```

### 3.5 `EditTaskHandler` (__init__.py)

```python
class EditTaskHandler:
    def __init__(self, engine): ...
    def run(self, task, pipeline, project_path) -> TaskResult:
        """generate → extract → validate → apply → commit-gate → TaskResult"""
```

---

## 4. Prompt contract: SEARCH/REPLACE fence format

**Chosen format: Aider-style SEARCH/REPLACE fences.**

````
```python
path/to/file.py
<<<<<<< SEARCH
exact old text here
=======
exact new text here
>>>>>>> REPLACE
```
````

**Rationale over alternatives:**

| Format | Extraction robustness (9B model) | Verdict |
|--------|----------------------------------|---------|
| Aider `<<<<<<< SEARCH`/`=======`/`>>>>>>> REPLACE` fences | Fence tokens are unique, rarely appear in code; parse is line-anchored | **CHOSEN** |
| Claude-Code `old_string`/`new_string` JSON sidecar | JSON in 9B output is fragile (trailing commas, unescaped quotes) | Rejected |
| `<edit><old>` XML tags | Already used as fallback (T02 AC); but `<old>`/`<new>` delimiters are less robust than fence tokens | Secondary fallback |

The fence format leverages the same extraction architecture as `generator.py:155-165` (regex-based block extraction with salvage for unclosed fences). The `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` markers are unambiguous even for a 9B model because they are unique sentinel lines, not balanced delimiters.

---

## 5. Validator stages (mirror validator.py:26-71)

Stage pattern: short-circuit within stage, report-all within stage, carry
`EditValidationResult(passed, stages, applied_content)`.

| Stage | Check | On fail |
|-------|-------|---------|
| 0 — empty | `edit_op.blocks` non-empty | fail: "no edit blocks found" |
| 1 — block format | each block has non-empty `old_string` | fail: "block N: empty old_string" |
| 2 — target file exists | `os.path.isfile(project_path/target_file)` | fail: "target file not found: {path}" |
| 3 — exact-match apply | `file_content.count(block.old_string) == 1` for each block | fail: "block N: match count={n} (expected 1)" — **multi-match fails WITH count** |
| 4 — result parses (Python) | `ast.parse(applied_content)` if target is `.py` | fail: "post-apply syntax error: {err}" |
| 5 — path traversal | all `block.path` sanitized (no `..`, no abs, no `~`) | fail: "path traversal rejected: {path}" |

**Stage 3 is the core oracle** — exact-match, multi-match fails-loud with
match count (GRILL.md delta 6: adversary frame). Never silent first-match.

**Retry ladder** reuses `("edit", check_name)` signature at
`engine/pipeline.py` retry counters (`engine/engine.py:637` pattern:
`pipeline.increment_retry(task.id, "generate")`).

---

## 6. Seam discipline — the `apply` module

The **only** module that touches file content is `apply.py:apply_blocks()`.
It is a **pure function** — no I/O, no LLM, no filesystem. This is the
testable seam:

```
apply_blocks(file_content: str, blocks: list[EditBlock])
    -> (new_content: str | None, errors: list[str])
```

- **Deterministic**: same input → same output, always.
- **Atomic**: first block that fails match → entire apply returns `(None, errors)`.
- **Sequential**: blocks applied in order; each sees the result of the previous.
- **Testable without LLM**: 100% coverage via string fixtures.

The handler (`__init__.py:run()`) orchestrates: read file → `apply_blocks()`
→ write file → commit-gate read-back. The seam lets tests verify the apply
logic in isolation, and tests verify the orchestration with a temp file.

---

## 7. Failure semantics → retry paths

| Failure mode | Error message in error_ctx | Retry path |
|--------------|---------------------------|------------|
| Malformed block (unparseable) | "block N: unparseable — skipped" | Regenerate (all blocks) |
| Empty `old_string` | "block N: empty old_string" | Regenerate with error_ctx |
| Target file not found | "target file not found: {path}" | **Fail** (no retry — deterministic) |
| Multi-match (count=N) | "block N: old_string matches {N} times — need exactly 1. Narrow the search anchor." | Regenerate with error_ctx naming block + count |
| Zero matches | "block N: old_string not found in file" | Regenerate with error_ctx |
| Post-apply syntax error | "post-apply ast.parse failed: {err}" | Regenerate with error_ctx |
| Path traversal | "path traversal rejected: {path}" | **Fail** (never retry) |
| Commit-gate mismatch | "commit-gate: written content != applied content" | **Fail** (Gitea #44) |

**error_ctx format** (mirrors `generator.py:232-237 ErrorContext`):
```python
@dataclass
class EditErrorContext:
    previous_edit_op: EditOp | None
    validation_errors: list[str]
    attempt_number: int
```

---

## 8. Commit path

Post-apply, the new file content is the full file (not a diff). The commit
path mirrors `engine/engine.py:837-894`:

1. `apply_blocks()` → `new_content` (full file)
2. `write_project_file(transport, full_path, new_content)` (committer.py:169)
3. **Commit-gate** (Gitea #44): read back file, assert `read_back == new_content`
4. `commit_code(project_path, task, ...)` (engine.py:889)

The read-back guard is the commit-gate verification — the handler applies it
before declaring success.

---

## 9. Phase 0 metrics instrumentation

**Location**: `engine/edit_ops/metrics.py` + `engine/state.py` (DDL).

**Counter**: `edit_apply_total`, `edit_apply_first_success` — persisted per
atom in a new `edit_op_results` table (additive DDL in `init_db()`):

```sql
CREATE TABLE IF NOT EXISTS edit_op_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    match_count INTEGER,        -- NULL on success; populated on multi-match fail
    applied INTEGER NOT NULL,    -- 1 if apply succeeded, 0 if failed
    created_at TEXT DEFAULT (datetime('now'))
)
```

**Recording points** (mirror `generator.py:124-146` session_log pattern):
- On each validate attempt: `edit_op_results` row with `applied=0/1`, `match_count`
- Session log row per LLM call (existing `SessionLogRecorder.record()` at
  `engine/generator.py:133`)

**First-apply rate** = `COUNT(applied=1 AND attempt=1) / COUNT(*)`.
Exposed via `engine/edit_ops/metrics.py:first_apply_rate(run_id)` — used for
the pre-registered ≥90% threshold (GRILL.md delta 3).

---

## 10. Dispatch integration (task_handler.py + engine.py)

**`engine/task_handler.py:56-67`** — extend `_resolve_handler()`:

```python
def _resolve_handler(task, engine):
    task_type = getattr(task, "task_type", "")
    if str(task_type).lower() == "research":
        from engine.research import ResearchTaskHandler
        return ResearchTaskHandler(engine)
    if str(task_type).lower() == "edit":          # NEW
        from engine.edit_ops import EditTaskHandler
        return EditTaskHandler(engine)
    return CodeTaskHandler(engine)
```

**`engine/engine.py:471-474`** — no change needed. The existing hook
`if not isinstance(handler, CodeTaskHandler): return handler.run(...)` already
diverts to `EditTaskHandler` because it's not a `CodeTaskHandler`.

**`engine/prd.py:26-34`** — add to `CATEGORY_TO_TYPE`:
```python
CATEGORY_TO_TYPE = {
    ...
    "edit": "edit",       # NEW — maps PRD category → task_type
}
```

**`engine/__init__.py:121-131`** — `task_role()`: add `"edit"` role mapping
if edit tasks need a distinct max_tokens (default: coder=4096 is fine for v1).

---

## 11. Test plan

### Unit tests (`tests/edit_ops/`)

| Test | What it covers |
|------|----------------|
| `test_edit_parser.py` | Fence parsing, XML fallback, malformed block skip, empty old_string |
| `test_edit_validator.py` | Each stage (0-5), multi-match returns count, path traversal rejection |
| `test_apply_blocks.py` | Pure function: single block, multi-block sequential, multi-match fail, zero-match fail, empty old_string, whole-file span, near-miss whitespace |

### Adversarial cases (mutation sensitivity ≥90%)

| Case | Expected behavior |
|------|-------------------|
| Multi-match (`old_string` appears 3×) | Fail with `match_count=3` |
| Near-miss whitespace (trailing space differs) | Fail with `match_count=0` (exact match only) |
| Block spans whole file | Succeed if `old_string == entire file content` |
| Empty `old_string` | Stage 1 fail |
| Unclosed fence | Salvage (mirror generator.py:161-165) |
| Path traversal (`../../etc/passwd`) | Stage 5 fail |
| Post-apply syntax error (Python) | Stage 4 fail |

### Integration test (`test_edit_integration.py`)

Apply a real edit to a temp file → assert `disk_content == applied_content`
(commit-gate read-back). Full handler run with mocked transport.

---

## 12. Ticket review (G1-T01..T07)

| Ticket | File list | Verdict | Notes |
|--------|-----------|---------|-------|
| **G1-T01** | `engine/state.py` (DDL), `engine/models.py` (enum) | **AMEND** | No `engine/models.py` exists. Atom kind lives in `CATEGORY_TO_TYPE` dict (`engine/prd.py:26`), not an enum. Add `edit_op_results` table DDL to `init_db()`. |
| **G1-T02** | `engine/generator.py` | **AMEND** | New extraction path in `engine/edit_ops/extract.py`, NOT inline in generator.py. Session-log row per call (mirrors generator.py:124-146). |
| **G1-T03** | `engine/validator.py` | **AMEND** | New stage group in `engine/edit_ops/validate.py`, NOT inline in validator.py. Reuses retry ladder `("edit", check_name)`. |
| **G1-T04** | `engine/task_handler.py`, `engine/engine.py` | **CONFIRM** | One branch in `_resolve_handler()` (task_handler.py:64). engine.py hook unchanged (already diverts non-CodeTaskHandler). |
| **G1-T05** | `tests/edit_ops/test_fuzz_rejection.py` | **CONFIRM** | No production code. Documents Aider-style escalation as upgrade path. |
| **G1-T06** | `engine/metrics.py` | **AMEND** | No `engine/metrics.py` exists. Create `engine/edit_ops/metrics.py` + `edit_op_results` table in `state.py:init_db()`. |
| **G1-T07** | `tests/edit_ops/` | **CONFIRM** | Three test files (parser, validator, integration). Mutation sensitivity ≥90%. |

---

## 13. Summary of amendments to tickets

1. **T01/T06**: `engine/models.py` and `engine/metrics.py` don't exist. The
   atom kind is registered via `CATEGORY_TO_TYPE` in `engine/prd.py:26`.
   Metrics live in `engine/edit_ops/metrics.py` + `state.py` DDL.
2. **T02/T03**: New code goes in `engine/edit_ops/` package, NOT inline in
   `generator.py`/`validator.py`. This mirrors the `engine/research/`
   precedent (EPIC.md: "existing files get registration hooks only").
3. **T04**: Confirmed — the engine.py hook at line 471-474 already diverts
   non-CodeTaskHandler handlers; only `task_handler.py` needs a branch.

---

## 14. Depth analysis

The `apply_blocks()` function is the **deepest module** in this design:
- **Interface**: 2 params, 1 return tuple. Trivial to learn.
- **Implementation**: sequential exact-match, atomic rollback, error accumulation.
- **Leverage**: every edit-ops test, the handler, and the validator's stage 3
  cross this one seam.
- **Locality**: the exact-match policy lives in ONE function. Change the
  matching rule once, fixed everywhere.

The `EditTaskHandler.run()` facade is intentionally **shallow** — it
orchestrates (generate → extract → validate → apply → commit) but holds no
logic of its own. All behaviour sits behind the four module seams.
