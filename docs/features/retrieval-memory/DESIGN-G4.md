# DESIGN-G4 — Integration (dispatch, wiring, docs, e2e)

> Group 4 of `ace-retrieval-memory`. Integrates G1 (edit-ops), G2 (retrieval),
> G3 (memory) into the live engine run loop. No new behaviour — this document
> defines the **wiring contract**: every registration point, the full run flow
> with all three features active, the feature-flag strategy, the e2e test plan,
> and the docs updates. File refs cite `path:line`.

---

## 1. Full run flow — mixed-PRD with all three features

```
PRD (JSON) [code, edit, research]
  │ parse_prd()  (engine/prd.py:26; CATEGORY_TO_TYPE maps edit→edit)
  ▼
engine.py:165  _run_task() → task_handler.py:56  _resolve_handler()
  │
  ├─ task_type="edit" ──► EditTaskHandler (G1-T04 branch at task_handler.py:64)
  │     generate → EditOpExtractor.extract() → EditValidator.validate()
  │     → apply_blocks() [pure seam] → write → commit-gate read-back
  │
  ├─ task_type="research" ──► ResearchTaskHandler (existing)
  │     CONTEXT: RepoMapBuilder.build()/render() → assemble_retrieved()
  │              → inject into {{context_block}} (prompt_compiler.py:745)
  │              + recall_for_objective() → novelty signals (ideation.py:96)
  │     GENERATE (enriched prompt) → VALIDATE → insert memory_claims
  │
  └─ default ──► CodeTaskHandler (inline path UNTOUCHED; no retrieval/memory)
```

**Key insight:** the three features activate on **disjoint dispatch branches**.
`edit` and `research` divert at `engine.py:473`; code atoms fall through.
Retrieval (G2) and memory (G3) only fire inside the research handler's CONTEXT
stage. Code atoms are structurally unaffected — the integration is purely
additive.

---

## 2. Wiring inventory — every registration point

This table is the implementation contract for G4-T01 and G4-T02. Each row
is a single change; the "Design doc" column names the upstream section.

| # | File | Line | Change | Design doc |
|---|------|------|--------|------------|
| 1 | `engine/task_handler.py` | 64 | Add `if task_type == "edit": return EditTaskHandler(engine)` (lazy import) | G1 §10 |
| 2 | `engine/prd.py` | 33 | Add `"edit": "edit"` to `CATEGORY_TO_TYPE` | G1 §10 |
| 3 | `engine/__init__.py` | 121-131 | `task_role()`: "edit" → coder role (no change needed; document only) | G1 §10 |
| 4 | `engine/state.py` | 102-196 | Add `edit_op_results` DDL in `init_db()` | G1 §9 |
| 5 | `engine/state.py` | 102-196 | Add `memory_claims` + `memory_contradictions` DDL | G3 §2 |
| 6 | `engine/state.py` | 102-196 | Add `embedding_chunks` + `embedding_chunks_vec` DDL (conditional) | G2 §6 |
| 7 | `engine/prompts.py` | 88 | Add optional `retrieved=` param → `assemble_retrieved()` | G2 §4 |
| 8 | `engine/pipeline.py` | 94-104 | Add `"edit"` retry key to `task_retries` dict init | G1 §5 |
| 9 | `engine/cli.py` | 303-404 | Add `ace memory` subcommand group (review/approve/reject) | G3 §3 |
| 10 | `engine/cli.py` | 389-404 | Register `("ace","memory",*)` in dispatch dict | G3 §3 |
| 11 | `engine/workflows/ralph/ideation.py` | 96 | Call `recall_for_objective()` before `_build_novelty_signals()` | G3 §4 |
| 12 | `engine/__init__.py` | 25-81 | Add `enable_retrieval` + `enable_memory_recall` flags to `EngineConfig` | §3 |

**Import-cycle guard:** all new imports inside `task_handler.py`, `prompts.py`,
and `ideation.py` must be **lazy** (inside the function body, not at module
top). The existing pattern at `task_handler.py:65` (`from engine.research import
ResearchTaskHandler` inside `_resolve_handler`) is the template. G4-T01 AC
explicitly requires "retrieval/memory hooks resolve lazily (no import cycles)".

---

## 3. Feature-flag strategy

**Recommendation: ship B (retrieval) and C (memory recall) behind `EngineConfig`
flags; no flag needed for A (edit-ops).**

| Feature | Flag | Default | Justification |
|---------|------|---------|---------------|
| A: edit-ops | *(none)* | always on when PRD has `category: edit` | New atom kind only reachable via new PRDs. Existing PRDs have no `edit` atoms, so the new dispatch branch is never hit. Zero blast radius on existing runs. |
| B: retrieval (repo map + embeddings) | `enable_retrieval: bool` | `False` | Adds tree-sitter walk + prompt mutation to the research CONTEXT stage. Must be opt-in until Phase 0 (G2-T01) proves recall improvement. Staged enablement: flag on → repo map only → +embeddings if G2-T05 ships. |
| C: memory recall | `enable_memory_recall: bool` | `False` | Injects approved claims into ideation novelty signals. Requires `memory_claims` rows to exist (populated by research handler). Opt-in until poisoning gate (G3-T02) is live and claims are being approved. |

**Why flags for B/C but not A:** A is a new dispatch branch — it cannot
activate unless an operator writes a PRD with `category: edit`. B and C
mutate the **existing** research-atom path; without flags they would fire on
every research atom in every existing run, changing prompt composition and
behaviour retroactively. Flags let operators enable per-run:

```python
config = EngineConfig(enable_retrieval=True, enable_memory_recall=True)
```

**Flag propagation:** the flags are read at the research handler's CONTEXT
stage. `enable_retrieval` gates the `RepoMapBuilder` + `assemble_retrieved`
call chain. `enable_memory_recall` gates the `recall_for_objective` call in
`ideation.py`. Both flags are read from `self.config` (already threaded
through `_run_task` → handler).

---

## 4. E2E test plan (G4-T04)

### 4.1 Golden-path mixed-PRD test

**File:** `tests/retrieval_memory_e2e/test_mixed_prd.py`

**Setup:** one PRD JSON with three atoms — `[code, edit, research]` — and a
`MockTransport` (from `engine/mock_transport.py`) returning canned LLM
responses for each kind.

**Assertions (8 steps):**

1. All three atoms reach terminal state → dispatch routes correctly
2. Edit atom: `disk_content == applied_content` → G1 apply_blocks + commit-gate
3. Research prompt contains `--- UNTRUSTED RETRIEVAL CONTENT ---` → G2 injection
4. Research prompt contains repo-map signatures → G2 RepoMapBuilder
5. Research prompt contains recalled claims (flag on) → G3 recall wiring
6. Code prompt contains NO retrieval markers or repo-map → code path isolation
7. `edit_op_results` row with `applied=1` → G1 metrics DDL
8. `memory_claims` row on research success → G3 DDL

### 4.2 Regression assertions

| Test | What it guards |
|------|----------------|
| Existing suite (`pytest tests/`, ~86 files) | All pass — no code-atom path mutated |
| `test_mixed_prd.py` with `enable_retrieval=False, enable_memory_recall=False` | Flags off → research atom behaves identically to baseline (no retrieval, no recall) |
| `test_mixed_prd.py` with flags on | All three features activate per §4.1 |

### 4.3 Poisoning-gate integration sub-test

**File:** `tests/retrieval_memory_e2e/test_poisoning_gate.py`

1. Seed `memory_claims` with approved "X always increases"
2. Submit "X never increases" via `ace memory approve`
3. `ApprovalResult.contradictions` non-empty, `kind="negation_pair"`
4. Human rejects → status `rejected`
5. `recall_for_objective("X")` excludes the rejected claim

---

## 5. Docs updates (G4-T03)

### 5.1 `docs/features/retrieval-memory/README.md` (new)

A single entry point linking the four design docs and documenting:

1. **Edit-op contract** — exact-match apply, multi-match fail-loud, Aider-style
   fences, commit-gate read-back. Link to DESIGN-G1.
2. **Retrieval ladder** — repo-map → embeddings, Phase 0 gating, untrusted
   markers, per-atom-type token budgets. Link to DESIGN-G2.
3. **Memory model** — `CONFLICTS_WITH` edge, poisoning gate, one-way recall,
   `ace memory approve/reject` CLI. Link to DESIGN-G3.
4. **Feature flags** — `enable_retrieval`, `enable_memory_recall`, defaults,
   how to enable per-run. From §3 of this doc.
5. **CLI reference** — `ace memory list | approve | reject` usage.

### 5.2 `CONTEXT.md` updates (if it exists at repo root or engine/)

Add a "Retrieval + Memory" section summarizing:
- When retrieval fires (research atoms, flag-gated)
- When memory recall fires (ralph ideation + research context, flag-gated)
- That edit-ops are a new atom kind dispatched via `task_type="edit"`

### 5.3 Inline docstrings

The wiring points (task_handler.py:64, prompts.py:88, ideation.py:96) each get
a one-line docstring comment naming the upstream design section, e.g.:

```python
# G1 §10: edit-ops dispatch branch (DESIGN-G4 §2 row 1)
if str(task_type).lower() == "edit":
    from engine.edit_ops import EditTaskHandler
    return EditTaskHandler(engine)
```

---

## 6. Tickets review — confirm/amend G4-T01..T04

| ID | Verdict | Amendment |
|----|---------|-----------|
| **G4-T01** | **Confirm** with additions | Title: "state-machine dispatch wiring for new atom kind `edit`". Add: (a) lazy import guard at `task_handler.py:64` (no import cycle), (b) `edit_op_results` DDL in `state.py:init_db()`, (c) `"edit"` retry key in `pipeline.py:94-104`. AC: edit atoms dispatch end-to-end; code-atom path unchanged; retrieval/memory hooks resolve lazily. |
| **G4-T02** | **Confirm** with additions | Title: "retrieval + memory registration hooks". Add: (a) `enable_retrieval` + `enable_memory_recall` flags in `EngineConfig` (`__init__.py:25-81`), (b) `retrieved=` param on `compile_prompt()` (`prompts.py:88`), (c) `recall_for_objective()` call in `ideation.py:96`, (d) `ace memory` CLI subcommand group in `cli.py:303-404`, (e) `memory_claims` + `memory_contradictions` DDL in `state.py`. AC: all registration points wired; flags default off; existing suite green. |
| **G4-T03** | **Confirm** | Title: "docs + README". Deliverables: `README.md` (§5.1), `CONTEXT.md` updates (§5.2), inline docstring pointers (§5.3). AC: edit-op contract, retrieval ladder, memory model, feature flags, and CLI reference all documented. |
| **G4-T04** | **Confirm** with additions | Title: "e2e integration test". Add: (a) golden-path mixed-PRD test (§4.1), (b) regression assertions — existing suite green + flag-off identity test (§4.2), (c) poisoning-gate integration sub-test (§4.3). AC: all three features activate correctly with flags on; code atoms unaffected; flags-off behaviour is baseline-identical. |

---

## 7. Dependency & ordering notes

```
G1-T04 ──► G4-T01 (dispatch branch + DDL + retry key)
G2-T03 ──► G4-T02 (prompt_assemble hook)
G2-T05 ──► G4-T02 (embedding DDL, conditional)
G3-T03 ──► G4-T02 (recall wiring into ideation)
G4-T01 + G4-T02 ──► G4-T03 (docs describe what was wired)
G4-T01 + G4-T02 ──► G4-T04 (e2e proves the wiring)
```

G4-T01 and G4-T02 are independent and can run in parallel. G4-T03 and G4-T04
both depend on G4-T01 + G4-T02. Within G4-T02, the DDL changes (state.py) and
the CLI changes (cli.py) are independent; the `compile_prompt` param and the
`ideation` recall call are independent. The only intra-G4 dependency is that
the e2e test (G4-T04) needs both the dispatch wiring and the registration
hooks in place.

---

## 8. Depth analysis — the integration seam

The integration layer is intentionally **shallow**: it holds no logic of its
own. Every behaviour sits behind one of the three upstream module seams:

- `apply_blocks()` (G1) — the pure edit-apply seam
- `assemble_retrieved()` (G2) — the prompt-injection seam
- `recall_for_objective()` (G3) — the memory-recall seam

G4's job is to **register** these seams at the right dispatch points and
**gate** them behind flags. The deletion test confirms this: deleting G4's
wiring leaves G1/G2/G3 modules fully functional but unreachable from the
engine — the integration layer earns its keep by being the single place that
connects three independent deep modules to the live run loop.
