# EPIC: ace-retrieval-memory — edit ops, retrieval lane, native contradiction memory

> Authoritative sources (priority order):
> `.scratch/retrieval-memory/GRILL.md` (owner answers + 7 adopted fireplace
> deltas — these AMEND the PRE-EPIC where they differ),
> `.scratch/retrieval-memory/PRE-EPIC.md` (workstreams + positioning),
> `.scratch/retrieval-memory/FIREPLACE.md` (42 ideas, top-10 scores),
> `.scratch/research/RESEARCH-embedding-ast-memory.md` (survey verdicts +
> appendix). Ledger entries 56–60. Gitea issue #44 (commit gate fix) is a
> prerequisite; this epic builds on it.

## Problem (three gaps, one epic)

1. **ENG-4 — modify-existing-file atoms fail ~100%.** The engine's
   emit-whole-file contract is the wrong shape for edits; both model tiers
   fail it (ledger 56–58). The survey verdict: adopt exact
   `old_string`/`new_string` edit ops, fail-loud (Claude Code/DSH style)
   as a new extraction tier; fuzzy matching only as escalation retry.
2. **No retrieval lane** — context is built by file-hopping; research
   atoms and ralph rounds re-read whole files.
3. **No semantic / cross-run memory** — `ralph_patterns` keyword matching
   only; no contradiction memory, which the research task type now makes
   valuable.

## Solution

### Workstream A — new atom kind `edit` (NEW ATOM KIND)

A new atom kind `kind: edit` with its own schema (search/replace blocks),
parser tier in the generator, validator stages, and handler integration.
Exact-match apply to a real file; multi-match fails loud with the match
count (never silent first-match). Fuzzy escalation is rejected for v1 —
same format-drift class as our fence mangling. This slots into the
existing pipeline as another extraction tier, not a rewrite and not a
tool loop. The commit-gate fix (Gitea #44) is a prerequisite so every
edit is verified on write.

### Workstream B — retrieval lane (tier-1 repo map + tier-2 embeddings)

- **Tier 1 (Adopt now):** tree-sitter repo map, whole-workspace per run,
  cached by mtime. Replaces file-hopping for structure queries at zero
  new infrastructure cost. Prompt-assembly integration with an
  untrusted-marker injection guard (retrieved chunks are marked untrusted
  in the assembled prompt) and a per-atom token budget ceiling (repo map
  + retrieved chunks must fit a stated budget; overflow → rank and trim).
- **Tier 2 (conditional on Phase 0 data):** sqlite-vec on `engine.db`
  (zero new infra — ACE already runs SQLite WAL) plus a small embedding
  model served via llama.cpp. The owner's **dynamic VRAM load/unload**
  design is the core de-risking mechanism: the embedding model is loaded
  at the prep stage, used to embed, then unloaded to free VRAM for the
  judge/orchestrator. The same on-demand swap applies to the judge. FMD
  (free-model-dispatcher embeddings endpoint) and CPU/iGPU fallbacks are
  under investigation (not tickets — see Open Investigations).

### Workstream C — native contradiction memory with poisoning gate

A `research_verdicts` comparator that detects cross-run claim-level
conflicts, using the graph-memory `CONFLICTS_WITH` edge as the schema
primitive (adopted from `adoresever/graph-memory`, cloned and inspected).
The poisoning gate extends the ralph pattern CLI
(`approve-pattern`/`reject-pattern`) with a contradiction pre-check
before human approval — one CLI, both stores. Ingestion is one-way
write, gated read. Wired into ideation/context recall.

## PHASE 0 — measure before building (no production code)

Phase 0 is the highest-ROI item in the entire epic (fireplace idea 1.1,
score 125). It determines whether the embedding tier is justified at all.

1. **Grep baseline benchmark.** Take 20 historical research atoms, record
   which files each actually needed, measure grep recall@10 and tree-sitter
   repo-map recall against that ground truth. If grep catches >80%, the
   embedding tier is premature optimization for months.
2. **Pre-registered success thresholds:**
   - **Edit-op exact-apply rate** (workstream A): ≥90% first apply on
     real modify-existing-file atoms, measured over 5 runs at varied
     subject-model temperatures. Falsification: <50% after 3 iterations
     → exact-match contract insufficient → escalate to fuzzy retry.
   - **Retrieval token budget ceiling** (workstream B): retrieved context
     per atom must decrease median token consumption by ≥20% vs.
     file-hopping (10 research atoms, retrieval on vs. off).
   - **Retrieval recall@5** (workstream B tier-2): ≥0.75 on a 50-query
     labeled set to justify the embedding tier.
   - **Contradiction precision** (workstream C): ≥0.9 precision, ≥0.85
     recall on a 100 claim-pair test corpus.
3. Build the labeled query set, the edit-op success criteria, and the
   contradiction test corpus — data tasks that must exist *before*
   implementation to avoid moving goalposts.

## Positioning — the convergence table

> Why these workstreams, from the architectural analysis. The root
> divergence: ACE generates model output as TEXT and EXTRACTS files from
> it; terminal coding tools have the model call a STRUCTURED EDIT TOOL.
> That single choice dissolves ENG-3 (fence extraction) and ENG-4
> (modify-existing-file). ACE should NOT become a tool loop — tool loops
> collapse gates into the loop and fail quiet/expensive; ACE's state
> machine fails loud and forensic. The borrow is the edit contract, not
> the loop.

| Invariant | Tool harnesses | ACE (after run1 + fixes) |
|---|---|---|
| disk == what was approved | edit applies to live file, fail-loud | read-back verification (ledger 58 fix, Gitea #44) |
| stale-context protection | "file changed since read → re-read" | explicit staging, no `git add .` |
| fresh state per unit of work | new conversation/subagent | fresh-agent ralph rounds + worktrees |
| memory | CLAUDE.md/AGENTS.md, vector stores | ralph_patterns + skill projection (+ native contradiction memory, workstream C) |

Where ACE is genuinely ahead and must not be lost in the borrow:
hollow-output defense (adversarial review gate, mutation-sensitivity
check), deterministic-first gating as a design principle, and research
atoms with evidence oracles (all terminal coding tools are code-only).

## Build order

1. **Phase 0** — baselines + metrics (1-2 days, no production code).
2. **Workstream A** — edit-ops (2-3 days, highest ROI, unblocks beellama).
3. **Workstream B tier-1** — repo map (2-3 days, zero new infra).
4. **Workstream C tier-1** — native contradiction memory (3-4 days).
5. **Workstream B tier-2** — embeddings (1-2 weeks, conditional on Phase 0).
6. **Workstream C tier-2** — semantic contradiction (future).

Rationale: A fixes a 100% failure class now; repo-map before embeddings
(free before costly); C's TF-IDF version is independent of B; measure
before build prevents shipping features that fail the falsification test.

## Non-goals

1. No Qdrant/LanceDB/external vector service in v1 (sqlite-vec only).
2. No external memory framework ingestion in v1 (native only).
3. No fuzzy-edit-first contract (exact-match first, fuzzy escalation only).
4. No changes to the evidence oracle (just shipped).
5. No tool-loop architecture (ACE stays a state-machine engine).
6. No per-line embedding granularity (function/class-level chunks).
7. No comment-text indexing in the repo map (tree-sitter indexes
   structure, not comment text — prompt-injection defense).

## Open investigations (not tickets)

- **FMD embeddings endpoint:** can free-model-dispatcher serve free
  embedding models via a dockerized llama.cpp embeddings server? Separate
  small research task, not blocking A/B-tier-1.
- **Dynamic VRAM scheduler:** load/unload via llama.cpp API or server
  restart; CPU/iGPU fallback Docker images. Feeds workstream B-tier-2
  design.
- **sqlite-vec scale ceiling:** at what chunk count does sqlite-vec
  degrade, and what is the migration story (MemoryProviderAdapter
  pattern from `dsh-mnemon`)?

## Sources

- `.scratch/retrieval-memory/GRILL.md` — owner answers + 7 adopted deltas.
- `.scratch/retrieval-memory/PRE-EPIC.md` — workstreams + positioning.
- `.scratch/retrieval-memory/FIREPLACE.md` — 42 ideas, top-10 scores.
- `.scratch/research/RESEARCH-embedding-ast-memory.md` — survey verdicts + appendix.
- Ledger 56–60 (ENG-1..6 defect register).
- `docs/features/research-task-type/TICKETS.md` — ENG-4 context.
- `harvest/adoresever/graph-memory/` — `CONFLICTS_WITH` edge primitive (cloned).
- `harvest/omdsh-dev/dsh-mnemon/` — MemoryProviderAdapter pattern (REPORT.md).
