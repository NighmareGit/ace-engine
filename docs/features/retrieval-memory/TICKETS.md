# TICKETS — ace-retrieval-memory

Source: `EPIC.md` (authoritative), `GRILL.md` (owner answers + 7 adopted
deltas), `FIREPLACE.md` (42 ideas, top-10 scores), `RESEARCH-embedding-ast-memory.md`
(survey verdicts), `DESIGN-G1..G4.md` (deep-module design passes — amendments
folded in below). Grain follows the proven law: single-file tickets
≤~150 lines where possible; multi-file tickets explicitly flagged.
Dependency order: G1 → G2 (Phase 0 first) → G3 → G4.

A codebase-design pass runs on each group before implementation.

---

## GROUP 1 — edit-ops (workstream A): new `task_type` `edit`

A new `task_type` `edit` (NOT a new `kind` field — see G1 §2 decision).
Own `engine/edit_ops/` package (mirrors `engine/research/` precedent), Aider
SEARCH/REPLACE fence format, pure `apply_blocks()` seam, validator stages
0–5 (incl. multi-match count + path traversal), handler dispatch branch.
Fuzzy escalation rejected for v1. Prerequisite: commit-gate fix (Gitea #44).

| ID | Title | Module | Deps | ~ln | Multi-file |
|---|---|---|---|---|---|
| G1-T01 | edit-op schema — `task_type` `edit` + search/replace block contract | engine/state.py (DDL), engine/prd.py (CATEGORY_TO_TYPE) | — | 80 | yes (2 files) |
| G1-T02 | edit-op parser tier — Aider fence + XML fallback | engine/edit_ops/extract.py | G1-T01 | 140 | no |
| G1-T03 | edit-op validator stages 0–5 — exact-match apply, multi-match fail-loud | engine/edit_ops/validate.py | G1-T02 | 150 | no |
| G1-T04 | edit-op handler integration — dispatch branch + `EditTaskHandler` facade | engine/task_handler.py, engine/edit_ops/__init__.py | G1-T03 | 120 | yes (2 files) |
| G1-T05 | fuzz-escalation-rejected v1 — document + test the rejection boundary | tests/edit_ops/ | G1-T03 | 60 | no |
| G1-T06 | edit-op metrics instrumentation — exact-apply rate counter + session log | engine/edit_ops/metrics.py, engine/state.py (DDL) | G1-T02 | 80 | no |
| G1-T07 | edit-op unit + integration tests | tests/edit_ops/ | G1-T03, G1-T04 | 150 | no |

### Acceptance criteria (per ticket)

- **G1-T01:** `init_db()` adds `edit_op_results` table idempotently (DDL: id,
  run_id, task_id, attempt, match_count, applied, created_at); `edit` added
  to `CATEGORY_TO_TYPE` dict (`engine/prd.py:33`); additive-only; full suite
  green. No `engine/models.py` (does not exist — atom kind lives in
  `CATEGORY_TO_TYPE`, not an enum).
- **G1-T02:** `EditOpExtractor.extract()` parses Aider-style SEARCH/REPLACE
  fences (`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE`) into `EditOp`
  dataclass {target_file, blocks: [{path, old_string, new_string}],
  raw_response}; XML `<edit><old>…</old><new>…</new></new></edit>` is the
  secondary fallback; malformed blocks skipped with warning; session-log row
  per call. New code lives in `engine/edit_ops/extract.py`, NOT inline in
  `generator.py`.
- **G1-T03:** validator stages 0–5: (0) blocks non-empty; (1) each block has
  non-empty `old_string`; (2) target file exists; (3) exact-match apply —
  `file.count(old)==1` → apply, else fail with match count (multi-match fails
  WITH count, never silent first-match); (4) result parses (`ast.parse` if
  `.py`); (5) path traversal rejected (no `..`, no abs, no `~`). Reuses retry
  ladder `("edit", check_name)`. New code in `engine/edit_ops/validate.py`,
  NOT inline in `validator.py`.
- **G1-T04:** one `task_type=="edit"` branch in `_resolve_handler()`
  (`engine/task_handler.py:64`, lazy import); `EditTaskHandler` facade in
  `engine/edit_ops/__init__.py` orchestrates generate → extract → validate →
  `apply_blocks()` → write → commit-gate read-back; code-atom path unchanged
  (suite green); `engine.py` hook at 471-474 already diverts non-CodeTaskHandler
  (no change needed there).
- **G1-T05:** document that fuzzy escalation is rejected for v1; test that
  the validator refuses fuzzy-match paths; reference Aider-style escalation
  as the documented upgrade path.
- **G1-T06:** `engine/edit_ops/metrics.py` exposes `first_apply_rate(run_id)`;
  `edit_op_results` table records `applied=0/1` + `match_count` per attempt;
  pre-registered ≥90% first-apply threshold measurable. No `engine/metrics.py`
  (does not exist).
- **G1-T07:** three test files (`test_edit_parser.py`, `test_edit_validator.py`,
  `test_edit_integration.py`); integration test applies a real edit to a temp
  file and asserts disk==approved (commit-gate read-back); mutation sensitivity
  ≥90% on ambiguous-anchor cases (multi-match, empty old_string, path
  traversal, near-miss whitespace).

---

## GROUP 2 — retrieval (workstream B tier-1 + tier-2)

Phase 0 (baselines + metrics harness) runs first. Tier-1 (repo map) ships
before tier-2 (embeddings). Tier-2 is conditional on Phase 0 data
(pre-registered thresholds). `PrepStage` is the VRAM lifecycle manager
(renamed from "harness").

| ID | Title | Module | Deps | ~ln | Multi-file |
|---|---|---|---|---|---|
| G2-T01 | **PHASE 0** — grep baseline benchmark + metrics harness | scripts/retrieval_baseline.py, tests/retrieval/ | — | 150 | yes (2 files) |
| G2-T02 | tree-sitter repo map builder — whole-workspace, mtime cache | engine/retrieval/repo_map.py | — | 150 | no |
| G2-T03 | prompt-assembly integration — untrusted-marker injection guard + token budget ceiling | engine/retrieval/prompt_assemble.py, engine/prompts.py | G2-T02 | 140 | yes (2 files) |
| G2-T04 | PrepStage — dynamic VRAM load/embed/unload lifecycle manager | engine/retrieval/vram_scheduler.py | — | 130 | no |
| G2-T05 | sqlite-vec + embedding ingestion + query (conditional on Phase 0) | engine/retrieval/embeddings.py, engine/state.py (DDL) | G2-T01, G2-T04 | 180 | yes (2 files) |
| G2-T06 | retrieval unit + integration tests | tests/retrieval/ | G2-T02, G2-T03, G2-T05 | 150 | no |

### Acceptance criteria (per ticket)

- **G2-T01:** script takes 20 historical research atoms; ground truth
  reconstructed from `session_logs.prompt_payload_path` sidecars
  (`engine_run_<id>_session_payloads.jsonl` → `context_block` file paths);
  records `grep_recall@10` + `repo_map_recall@10`; emits baseline report to
  `reports/retrieval_baseline_<timestamp>.json`; pre-registered thresholds
  documented in report header: `≥0.85` → skip embeddings, `0.70–0.84` →
  justified as enhancement, `<0.70` → required. No production code changes.
- **G2-T02:** walks workspace, parses each supported file with tree-sitter
  (`tree-sitter>=0.23` pinned; Python + C + C++ grammars), extracts top-level
  function/class signatures with line numbers; formats compact text map
  (≤3k tokens); mtime-cached via `.ace/repo_map_mtime.json` sidecar (skip
  unchanged files); `.aceignore`-style exclusion support (`pathspec` if
  available, else fnmatch rollup).
- **G2-T03:** retrieved chunks injected at `prompt_compiler.py:745`
  `{{context_block}}` carry `--- UNTRUSTED RETRIEVAL CONTENT ---` marker
  (prompt-injection defense); token budget ceiling enforced from
  `RETRIEVAL_BUDGETS` dict keyed by `atom_type`
  (`{research:3000, implement:1500, fix:1000, _default:1500}`); overflow →
  rank-and-trim (never silent mid-symbol truncation); markers count against
  budget; `compile_prompt()` gains optional `retrieved=` param (additive —
  existing callers unaffected).
- **G2-T04:** `PrepStage` lifecycle manager: `embed_batch()` = load → embed →
  unload (fire-and-forget VRAM cleanup); `swap_for_judge()` = unload
  orchestrator → load judge; `HttpTransport` protocol is internal seam
  (mockable); near-term default is `server-vulkan` Docker image on Intel iGPU
  (`ghcr.io/ggml-org/llama.cpp:server-vulkan --embeddings`); CPU fallback is
  `server` image; runs off the critical latency path (prep stage only).
- **G2-T05:** `init_db()` adds `embedding_chunks` + `embedding_chunks_vec`
  virtual table idempotently; chunks function/class-level; embeds via
  llama.cpp `/embeddings` via `PrepStage.embed_batch()`; query returns top-k
  chunks for a research question. **Only ships if G2-T01 shows
  `repo_map_recall@10 < 0.85`** (pre-registered threshold from §2).
- **G2-T06:** unit tests for repo map, prompt assembly, VRAM scheduler,
  embeddings; integration test: repo map injected into a mock research-atom
  prompt; token-budget overflow test; integration tests CI-gated behind
  `@pytest.mark.integration`.

### FMD / iGPU fallbacks — INVESTIGATION (not tickets)

- FMD embeddings endpoint: can free-model-dispatcher serve free embedding
  models via dockerized llama.cpp embeddings server?
- CPU/iGPU fallback: which llama.cpp build / Docker image supports Intel iGPU
  for embedding inference?
- These feed G2-T04/G2-T05 design but are not themselves tickets.

---

## GROUP 3 — memory (workstream C): native contradiction memory + poisoning gate

Cross-run contradiction detection using a `CONFLICTS_WITH` edge primitive;
poisoning gate extends the CLI with `ace memory approve/reject` subcommands.
Wired into ideation/context recall. New top-level `engine/memory/` package.

| ID | Title | Module | Deps | ~ln | Multi-file |
|---|---|---|---|---|---|
| G3-T01 | research_claims/sources comparator — cross-run contradiction detection | engine/memory/comparator.py, engine/state.py (DDL) | — | 150 | yes (2 files) |
| G3-T02 | poisoning gate — `ace memory approve/reject` with contradiction pre-check | engine/memory/gate.py, engine/cli.py | G3-T01 | 140 | yes (2 files) |
| G3-T03 | one-way gated recall wiring — ideation + context integration | engine/memory/recall.py, engine/retrieval/prompt_assemble.py | G3-T02, G2-T03 | 130 | yes (2 files) |
| G3-T04 | contradiction memory unit + integration tests | tests/memory/ | G3-T01, G3-T02 | 150 | no |

### Acceptance criteria (per ticket)

- **G3-T01:** `init_db()` adds `memory_claims` (id, run_id, task_id,
  claim_text, verdict_position, source_atom_id, sidecar_path, created_at) +
  `memory_contradictions` (id, claim_a_id→memory_claims, claim_b_id→
  memory_claims, kind, similarity, status, detected_at) tables
  idempotently; `compare(new_sidecar, stored_claims)` two-layer matcher —
  (1) TF-IDF cosine ≥0.60 reuses `engine/intent/similarity.py:18
  plan_similarity`, (2) negation-pair heuristic imports `_NEG_PAIRS` from
  `engine/research/evidence.py:630`, (3) verdict-position conflict (same
  `source_atom_id`, opposite position) flags regardless of textual
  similarity; returns `list[Contradiction]`; self-same-run claims filtered.
- **G3-T02:** `ace memory approve <claim_id>` / `ace memory reject <claim_id>`
  subcommands (new group in `engine/cli.py`); shared `approve_for_recall(
  record_id, store, approved_by)` with `store ∈ {ralph_patterns,
  memory_claims}`; contradiction pre-check runs `compare()` before human
  approval, attaches findings (non-blocking — human decides); `ApprovalResult(
  record_id, approved, contradictions, message)`; pending queue surfaced via
  `ace memory review` (lists `proposed` claims by `created_at DESC` with
  contradiction counts); `pattern_gate.py` stays put (ralph-owned).
- **G3-T03:** `recall_for_objective(objective, token_budget=2048)` retrieves
  approved claims ranked + budget-trimmed; plugs into `ideation.py:96` before
  `_build_novelty_signals()`; also serves research context builder
  (G2-T03 seam); one-way write (`approve_for_recall`) / gated read (
  `recall_for_objective` queries `status='approved'` only); recalled claims
  injected with untrusted-content marker.
- **G3-T04:** unit tests for comparator (negation-pair, verdict-conflict,
  semantic-overlap, non-contradiction, self-same-run, empty sidecar); gate
  flow (approve clean, approve contradictory, reject, recall invisibility,
  recall visibility, one-way write); integration test: `ace memory approve`
  with negation-pair contradiction surfaces findings pre-approval; mutation
  sensitivity ≥90% on negation-pair injection; contradiction precision ≥0.9,
  recall ≥0.85 on seeded 100 claim-pair corpus.

---

## GROUP 4 — integration: dispatch, wiring, docs, e2e

The integration seam. Depends on groups 1, 2, 3. The 12-row wiring inventory
table below (from DESIGN-G4 §2) is the implementation contract for G4-T01/T02.

| ID | Title | Module | Deps | ~ln | Multi-file |
|---|---|---|---|---|---|
| G4-T01 | state-machine dispatch wiring for `task_type` `edit` | engine/task_handler.py, engine/state.py, engine/pipeline.py | G1-T04 | 80 | yes (3 files) |
| G4-T02 | retrieval + memory registration hooks + feature flags | engine/__init__.py, engine/prompts.py, engine/cli.py, engine/state.py | G2-T03, G2-T05, G3-T03 | 90 | yes (4 files) |
| G4-T03 | docs + README — edit-op contract, retrieval lane, memory model | docs/features/retrieval-memory/README.md | G4-T01, G4-T02 | 80 | no |
| G4-T04 | e2e integration test (golden-path + flag-off identity) | tests/retrieval_memory_e2e/ | G4-T01, G4-T02 | 120 | no |

### G4-T01/T02 wiring inventory (implementation contract)

| # | File | Line | Change | Source |
|---|------|------|--------|--------|
| 1 | `engine/task_handler.py` | 64 | `if task_type=="edit": return EditTaskHandler(engine)` (lazy import) | G1 §10 |
| 2 | `engine/prd.py` | 33 | `"edit":"edit"` in `CATEGORY_TO_TYPE` | G1 §10 |
| 3 | `engine/__init__.py` | 121-131 | `task_role()`: "edit"→coder role (document only; no change needed) | G1 §10 |
| 4 | `engine/state.py` | 102-196 | `edit_op_results` DDL in `init_db()` | G1 §9 |
| 5 | `engine/state.py` | 102-196 | `memory_claims` + `memory_contradictions` DDL | G3 §2 |
| 6 | `engine/state.py` | 102-196 | `embedding_chunks` + `embedding_chunks_vec` DDL (conditional) | G2 §6 |
| 7 | `engine/prompts.py` | 88 | optional `retrieved=` param → `assemble_retrieved()` | G2 §4 |
| 8 | `engine/pipeline.py` | 94-104 | `"edit"` retry key in `task_retries` dict init | G1 §5 |
| 9 | `engine/cli.py` | 303-404 | `ace memory` subcommand group (review/approve/reject) | G3 §3 |
| 10 | `engine/cli.py` | 389-404 | register `("ace","memory",*)` in dispatch dict | G3 §3 |
| 11 | `engine/workflows/ralph/ideation.py` | 96 | `recall_for_objective()` before `_build_novelty_signals()` | G3 §4 |
| 12 | `engine/__init__.py` | 25-81 | `enable_retrieval` + `enable_memory_recall` flags in `EngineConfig` | G4 §3 |

All new imports inside `task_handler.py`, `prompts.py`, `ideation.py` must be
lazy (inside the function body) — no import cycles.

### Feature flags

| Flag | Default | Gates |
|---|---|---|
| *(none for edit-ops)* | always on via PRD `category: edit` | new branch, zero blast radius |
| `enable_retrieval: bool` | `False` | RepoMapBuilder + `assemble_retrieved` call chain |
| `enable_memory_recall: bool` | `False` | `recall_for_objective` in `ideation.py` |

### Acceptance criteria (per ticket)

- **G4-T01:** rows 1-4 + 8 of wiring inventory applied; edit atoms dispatch
  through the state machine end-to-end; code-atom path unchanged (suite
  green); retrieval/memory hooks resolve lazily (no import cycles).
- **G4-T02:** rows 5-7 + 9-12 of wiring inventory applied; `enable_retrieval`
  + `enable_memory_recall` flags added to `EngineConfig` (default `False`);
  `retrieved=` param on `compile_prompt()`; `ace memory` CLI group registered;
  `recall_for_objective()` called in `ideation.py:96`; `memory_claims` +
  `memory_contradictions` DDL in `state.py`; existing suite green.
- **G4-T03:** README documents edit-op contract (exact-match, fail-loud,
  Aider fences, commit-gate read-back), retrieval ladder (repo-map →
  embeddings, Phase 0 gating, untrusted markers, per-atom-type budgets),
  memory model (`CONFLICTS_WITH` edge, poisoning gate, one-way recall,
  `ace memory approve/reject` CLI), feature flags + defaults, CLI reference.
- **G4-T04:** golden-path mixed-PRD test (`test_mixed_prd.py`, 8 assertions:
  1. all three atoms reach terminal state; 2. edit atom disk==applied;
  3. research prompt has UNTRUSTED marker; 4. research prompt has repo-map
  signatures; 5. research prompt has recalled claims (flag on); 6. code
  prompt has NO retrieval markers; 7. `edit_op_results` row with applied=1;
  8. `memory_claims` row on research success); regression — existing suite
  green + flag-off identity test (flags off → research atom behaves
  identically to baseline); poisoning-gate sub-test (`test_poisoning_gate.py`:
  seed approved "X always increases", submit "X never increases", assert
  `ApprovalResult.contradictions` non-empty with kind=negation_pair, reject →
  status=rejected, `recall_for_objective("X")` excludes rejected claim).

---

## Dependency graph (between groups)

```
G1 (edit-ops)  ─────────────────────────────┐
                                             ▼
G2 (retrieval, Phase 0 first) ──────► G4 (integration)
                                             ▲
G3 (memory) ─────────────────────────┘
```

- **G1 → G4:** edit-op handler must be wired before integration.
- **G2 → G4:** retrieval registration hooks + prompt assembly must exist
  before integration.
- **G3 → G4:** memory recall wiring must exist before integration.
- **G2 → G3 (soft):** C's TF-IDF version is independent of B; if C later
  upgrades to embedding-based similarity, it depends on G2-T05. Not a v1
  dependency.
- **G2-T01 (Phase 0) → G2-T05 (embeddings):** embeddings are conditional
  on Phase 0 data (`repo_map_recall@10 < 0.85`).
- **G2-T03 (prompt assembly) → G3-T03 (recall wiring):** gated recall
  plugs into the prompt-assembly seam.

---

## Skill mapping (state-machine run)

Skills the implementing agent should load per ticket, plus run-lane notes.
Skill keys: `tdd` (test-driven development), `worktree-guard` (isolated
worktree pre-flight), `gpu-lease` (exclusive GPU lease), `perf-verification`
(pre-completion quality/throughput gate), `check-work` (completion gate),
`code-review` (group closing gate), `debugging`/`diagnosing-bugs` (only if
failures arise at run time).

| Ticket | Skills | Notes |
|---|---|---|
| G1-T01 | worktree-guard, check-work | DDL + CATEGORY_TO_TYPE registration |
| G1-T02 | tdd, worktree-guard, check-work | pure-seam: parser (fence + XML fallback) |
| G1-T03 | tdd, worktree-guard, check-work | pure-seam: validator stages 0–5 |
| G1-T04 | tdd, worktree-guard, check-work | pure-seam: `apply_blocks()` + handler facade |
| G1-T05 | tdd, worktree-guard, check-work | pure-seam: rejection boundary test |
| G1-T06 | tdd, worktree-guard, check-work | pure-seam: metrics math |
| G1-T07 | tdd, worktree-guard, check-work | test suite gate for G1 |
| G2-T01 | perf-verification, worktree-guard, check-work | Phase 0 measurements (baseline report) |
| G2-T02 | tdd, worktree-guard, check-work | pure-seam: repo map builder (offline-testable) |
| G2-T03 | tdd, worktree-guard, check-work | pure-seam: `assemble_retrieved()` |
| G2-T04 | gpu-lease, worktree-guard, check-work | PrepStage integration testing only (GPU lease) |
| G2-T05 | gpu-lease, tdd, worktree-guard, check-work | conditional; GPU lease for llama.cpp embed |
| G2-T06 | tdd, worktree-guard, check-work | test suite gate for G2 |
| G3-T01 | tdd, worktree-guard, check-work | pure-seam: comparator (TF-IDF + negation-pair) |
| G3-T02 | worktree-guard, check-work | CLI extension + gate logic |
| G3-T03 | tdd, worktree-guard, check-work | pure-seam: `recall_for_objective()` |
| G3-T04 | tdd, worktree-guard, check-work | test suite gate for G3 |
| G4-T01 | worktree-guard, check-work | dispatch wiring (3 files) |
| G4-T02 | worktree-guard, check-work | registration hooks + feature flags (4 files) |
| G4-T03 | worktree-guard, check-work | docs only |
| G4-T04 | tdd, worktree-guard, check-work | golden-path e2e + flag-off identity + poisoning sub-test |
| **Group closing gates** | code-review | run at end of G1, G2, G3, G4 (per group, not per ticket) |

### Group run order with gates

```
G1 run → code-review gate (+ implement any findings tickets)
       → G2 run (Phase 0 / G2-T01 first) → review gate
       → G3 run → review gate
       → G4 run → full-suite + golden-path e2e → code-review
```

- **G1 run:** T01→T02→T03→T04→T05/T06 (parallel after T03)→T07.
- **G1 review gate:** code-review on all G1 modules; re-open any findings as
  implementation tickets before proceeding.
- **G2 run:** T01 (Phase 0) runs FIRST and gates T05; T02/T04 independent
  (parallel); T03 after T02; T05 after T01+T04 gate decision; T06 last.
- **G2 review gate:** code-review on all G2 modules + Phase 0 report review.
- **G3 run:** T01→T02→T03→T04.
- **G3 review gate:** code-review on all G3 modules.
- **G4 run:** T01/T02 independent (parallel); T03+T04 after both.
- **G4 review gate:** full-suite regression + golden-path e2e passing, then
  code-review on all integration modules. Final gate closes the epic.
