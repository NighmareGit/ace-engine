# PRD: Retrieval + Memory (v1)

Source epic: `EPIC.md` (authoritative). Amendments:
`.scratch/retrieval-memory/GRILL.md` (7 deltas + owner answers supersede
PRE-EPIC where they differ). Machine PRD: `.scratch/retrieval-memory/PRD-set/`.

## Problem Statement

The ACE engine has three gaps that compound at scale: (1) modify-existing-file
atoms fail ~100% because the emit-whole-file contract is the wrong shape for
edits (ledger 56–58); (2) context is built by file-hopping — research atoms
and ralph rounds re-read whole files instead of retrieving relevant structure;
(3) there is no cross-run memory — `ralph_patterns` keyword `LIKE` only, no
contradiction detection, so each research run is independent and the engine
cannot flag when a new verdict contradicts an old one.

## Solution

Three workstreams, sequenced measure-first:

- **A — edit-ops (new atom kind `edit`):** the model emits search/replace
  blocks; the harness applies with exact match or fails loud with the match
  count. A new extraction tier in the generator, not a tool loop.
- **B — retrieval lane:** tier-1 tree-sitter repo map (whole-workspace, mtime
  cached) replaces file-hopping; tier-2 sqlite-vec embeddings conditional on
  Phase 0 data, served via llama.cpp with a dynamic VRAM load/unload design
  (load at prep, embed, unload). Prompt assembly marks retrieved chunks
  untrusted and enforces a per-atom token budget ceiling.
- **C — native contradiction memory:** a `research_verdicts` comparator
  using a `CONFLICTS_WITH` edge primitive; the poisoning gate extends the
  ralph pattern CLI (`approve-pattern`/`reject-pattern`) with a
  contradiction pre-check before human approval.

Phase 0 (grep baseline + pre-registered metrics) gates the tier-2 embedding
investment.

## User Stories

1. As an operator, I want modify-existing-file atoms to succeed via exact
   edit-ops instead of emitting whole files, so that ENG-4's 100% failure
   class is resolved.
2. As an operator, I want research atoms to receive a repo map instead of
   whole-file hops, so that context quality improves and token cost drops.
3. As the engine, I want retrieved chunks marked untrusted in assembled
   prompts, so that indexed comment-injection cannot redirect the model.
4. As the engine, I want a contradiction pre-check before pattern/verdict
   approval, so that poisoned memory cannot silently contaminate recall.
5. As an epic owner, I want a grep baseline and pre-registered thresholds
   before any retrieval infrastructure ships, so that we only build what the
   measurement justifies.

## Success Metrics

- Edit-op exact-apply rate ≥90% first apply on real modify-existing-file
  atoms (≥5 runs, varied temperature).
- Retrieval reduces median per-atom token consumption by ≥20% vs.
  file-hopping (10 research atoms, retrieval on vs. off).
- Repo map alone achieves recall@5 ≥0.60 on the Phase-0 labeled query set;
  embeddings are justified only if grep/repo-map recall is below that floor.
- Contradiction memory precision ≥0.9, recall ≥0.85 on a 100 claim-pair
  test corpus.
- Full engine suite green at every wave; zero behavior change on code atoms.

## Out of Scope

External vector service (Qdrant/LanceDB), external memory framework
ingestion, fuzzy-edit-first contract, per-line embedding granularity,
comment-text indexing in the repo map, tool-loop architecture, HTTP source
accessibility (v2, inherited from research-task-type).
