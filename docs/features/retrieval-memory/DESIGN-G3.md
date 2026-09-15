# DESIGN — GROUP 3 "memory" (workstream C tier-1)

> Deep-module design pass for `ace-retrieval-memory` G3-T01..T04.
> Sources: `engine/research/sidecar.py` (evidence schema), `engine/research/evidence.py:623-648` (negation-pair heuristic), `engine/intent/similarity.py` (TF-IDF primitive), `engine/workflows/ralph/pattern_gate.py` (ADR-0002 gate contract), `engine/state.py:168-193` (research DDL), `harvest/adoresever/graph-memory/src/types.ts:37-53` (`CONFLICTS_WITH` edge), `harvest/adoresever/graph-memory/src/store/db.ts:110-122` (edge DDL).

---

## 1. Locality decision — `engine/memory/` (new top-level package)

**Decision:** a new top-level `engine/memory/` package owns all memory logic.

**Justification — the deletion test.** Memory spans two atoms: `ralph_patterns` (code-atom lessons) and `research_verdicts` (research-atom claims). If memory logic lives inside `engine/workflows/ralph/`, the research-atom half has to reach sideways into a sibling package — violating locality. If it lives inside `engine/research/`, the ralph-pattern half has the same problem. A top-level `engine/memory/` package sits at the natural seam: one interface, two backends (pattern store + verdict store), one implementation. Delete it and cross-run contradiction detection + poisoning gate + recall vanish from N call sites — it earns its keep.

**Not extending `engine/workflows/ralph/`:** `pattern_gate.py` stays put (ralph-owned). `engine/memory/` imports it; ralph does not import memory. One-way dependency.

---

## 2. `CrossRunComparator` module (`engine/memory/comparator.py`)

### Interface (small surface, deep implementation)

```python
@dataclass
class Contradiction:
    claim_a: str          # new claim text
    claim_b: str          # stored claim text
    run_a: str            # source run_id of claim_a
    run_b: str            # source run_id of claim_b
    similarity: float     # TF-IDF cosine
    kind: str             # "negation_pair" | "verdict_conflict" | "semantic_overlap"

def compare(new_sidecar: dict, stored_claims: list[dict]) -> list[Contradiction]:
    """Compare new verdict claims against stored claims → contradiction candidates."""
```

**Depth:** one public function hides (a) TF-IDF vectorization, (b) negation-pair heuristic reuse, (c) verdict-position conflict check, (d) run-ref tracing. Callers learn one signature; tests cross one seam.

### Claim matching — two-layer

1. **TF-IDF cosine clustering** — reuses `engine/intent/similarity.py:18` `plan_similarity(a, b)` (word 1-2grams, `TfidfVectorizer`). Threshold ≥ 0.60 → candidate pair. This is the same primitive the evidence oracle stage 5 already uses (`evidence.py:673`), so scores are comparable across validation and memory.

2. **Negation-pair heuristic** — reuses the exact `_NEG_PAIRS` from `engine/research/evidence.py:630`:
   ```python
   _NEG_PAIRS = [("always", "never"), ("is", "is not"),
                 ("increases", "decreases"), ("true", "false")]
   ```
   The comparator imports and applies this tuple (does not duplicate). Shared keyword overlap check: `words_i & words_j` non-empty after removing the negation tokens. This catches the deterministic-floor cases the LLM judge never sees.

3. **Verdict-position conflict** — if two claims have the same `source_atom_id` question but opposite `verdict_position` (supported vs. refuted), flag regardless of textual similarity. Catches cases where wording diverges but the conclusion flips.

**Why two layers:** TF-IDF catches paraphrase contradictions; negation-pair catches syntactic opposites with divergent vocabulary; verdict-position catches same-question flips. Together they cover the contradiction surface no single layer reaches.

### Schema — additive DDL in `engine/state.py`

Extends `init_db()` (alongside `research_verdicts` at `state.py:168`):

```sql
CREATE TABLE IF NOT EXISTS memory_claims (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    verdict_position TEXT NOT NULL,  -- supported|refuted|inconclusive
    source_atom_id TEXT NOT NULL,    -- the research atom that produced this claim
    sidecar_path TEXT,               -- path to verdict.evidence.json
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS memory_claims_position ON memory_claims(verdict_position);
CREATE INDEX IF NOT EXISTS memory_claims_atom ON memory_claims(source_atom_id);

CREATE TABLE IF NOT EXISTS memory_contradictions (
    id TEXT PRIMARY KEY,
    claim_a_id TEXT NOT NULL REFERENCES memory_claims(id),
    claim_b_id TEXT NOT NULL REFERENCES memory_claims(id),
    kind TEXT NOT NULL,              -- negation_pair|verdict_conflict|semantic_overlap
    similarity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|dismissed
    detected_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS memory_contradictions_status ON memory_contradictions(status);
```

**Where verdict sidecars are persisted today:** `research_verdicts` table at `state.py:168-175` stores `verdict_path` (the `.md`) but not the sidecar JSON path. The sidecar (`verdict.evidence.json`) is written alongside the `.md` by the research handler but its path is not currently in the DB. `memory_claims.sidecar_path` fills this gap — it records the sidecar location so the comparator can read claims without re-parsing markdown.

**The `CONFLICTS_WITH` primitive** (adopted from `graph-memory/src/types.ts:42`, `db.ts:114`) maps onto `memory_contradictions` with `kind='semantic_overlap'` and `status` replacing graph-memory's implicit active/deprecated node status. The typed edge pattern (`from_id, to_id, type`) is mirrored by `(claim_a_id, claim_b_id, kind)` — a claim-to-claim typed edge, exactly the `CONFLICTS_WITH` semantics (two claims are mutually exclusive) scoped to the research domain.

---

## 3. Poisoning gate (`engine/memory/gate.py` + `engine/cli.py`)

### Design — extend, don't fork

The existing `approve_pattern`/`reject_pattern` handlers (`pattern_gate.py:192-224`) implement the ADR-0002 gate: `pending→approved` transition, only `pending` rows, returns bool. The poisoning gate extends this contract:

**New unified CLI surface:** `ace memory review` (new subcommand group in `engine/cli.py`). This surfaces the pending queue with contradiction findings attached — NOT a replacement for `ralph approve-pattern`, but a parallel entry for the `memory_claims` store. Both stores share the same status-column contract (`pending|approved|rejected`).

**Minimal-path alternative (adopted for v1):** keep `approve-pattern`/`reject-pattern` CLI as-is for `ralph_patterns`; add `ace memory approve <claim_id>` / `ace memory reject <claim_id>` for `memory_claims`. The gate logic is shared:

```python
def approve_for_recall(record_id: str, store: str, approved_by: str = "cli") -> ApprovalResult:
    """Unified approval with contradiction pre-check.
    
    store ∈ {'ralph_patterns', 'memory_claims'}.
    Runs contradiction pre-check automatically; attaches findings to result.
    Only 'pending' rows transition to 'approved' (ADR-0002 invariant).
    """
```

### Contradiction pre-check

Before human approval, the gate calls `compare(new_sidecar, stored_claims)` against all `approved` claims in `memory_claims`. Findings are **attached, not blocking** — the human decides:

```python
@dataclass
class ApprovalResult:
    record_id: str
    approved: bool
    contradictions: list[Contradiction]   # empty = clean
    message: str
```

The CLI renders contradictions as:
```
⚠ 2 contradiction(s) detected:
  - claim "X always increases" CONFLICTS_WITH claim "X never increases" (run R03, sim=0.82, kind=negation_pair)
  - claim "Y is supported" CONFLICTS_WITH claim "Y is refuted" (run R07, sim=0.71, kind=verdict_conflict)
Approve anyway? [y/N]
```

**One-way write, gated read:** approved rows are recallable; `pending`/`rejected` rows are invisible to recall. This is the ralph pattern contract (`pattern_gate.py:6`: "Prompt injection reads `approved` rows only (R2)") applied to memory_claims.

### Pending queue

`memory_claims.status` column (default `pending`) is the pending queue. The `ace memory review` command lists all `pending` claims ordered by `created_at DESC`, each with its contradiction count. This is the status-column pattern from `ralph_patterns.status` (`pattern_gate.py:39`) reused for a second store.

---

## 4. Recall integration (`engine/memory/recall.py`)

### Where approved claims enter ideation/context

**Seam:** `engine/workflows/ralph/ideation.py:96-104` — `_build_novelty_signals(prior_plans)` constructs novelty signals from prior rounds. The recall module plugs in **before** ideation: approved claims relevant to the current objective are fetched and injected as additional novelty signals (things the engine already concluded — avoid re-deriving them).

**Interface:**

```python
def recall_for_objective(objective: str, token_budget: int = 2048) -> RecallResult:
    """Retrieve approved claims relevant to the objective, ranked, budget-trimmed."""
```

**Token budget respect:** `RecallResult` carries a `token_estimate` field. The caller (`ideation.py` or the context builder from G2-T03) enforces the budget: overflow → rank by score, trim from the bottom. This mirrors the retrieval token budget ceiling from `GRILL.md:39-40` (Q10 sharpening: "repo map + retrieved chunks must fit a stated budget; overflow → rank and trim").

**Recall is read-only:** `recall_for_objective` queries only `memory_claims` rows with `status='approved'`. No write path. The write path is exclusively through `approve_for_recall` (the poisoning gate). This enforces the one-way contract: gate writes, recall reads.

**Untrusted marker:** recalled claims are injected with an `untrusted-content` marker (same defense as retrieved code chunks from G2-T03) — they are inputs to reasoning, not ground truth.

### Wiring point

`engine/workflows/ralph/ideation.py:76-94` — `generate_candidate_plans()` receives `previous_rounds`. The recall module is called at line ~96 (before `_build_novelty_signals`) to inject approved claims as novelty context. The existing `novelty_signals` string is extended, not replaced.

For research atoms, recall plugs into the context builder (G2-T03's `prompt_assemble.py` or the research handler's prompt construction at `verdict.py:53-89`). The same `recall_for_objective` interface serves both.

---

## 5. Test plan (offline; seeded store)

All tests use a seeded in-memory SQLite store (`:memory:`) — no network, no GPU, no LLM.

### Contradiction detection (`tests/memory/test_comparator.py`)

| Case | Input | Expected |
|---|---|---|
| Negation pair | "X always increases" vs "X never increases" (shared keyword) | `Contradiction(kind="negation_pair")` |
| Verdict conflict | same `source_atom_id`, position=supported vs refuted | `Contradiction(kind="verdict_conflict")` |
| Semantic overlap | TF-IDF sim=0.85, no negation, same position | `Contradiction(kind="semantic_overlap")` |
| Non-contradiction | sim=0.30, no negation, same position | empty list |
| Self-same run | both claims from same `run_id` | filtered (no self-contradiction) |
| Empty sidecar | `new_sidecar = {}` | empty list (no crash) |

### Gate flow (`tests/memory/test_gate.py`)

| Case | Input | Expected |
|---|---|---|
| Approve clean claim | no contradictions | `approved=True`, `contradictions=[]` |
| Approve contradictory claim | 2 contradictions found | `approved=True` (human override), `contradictions=[...]` attached |
| Reject claim | human rejects | `approved=False`, status→`rejected` |
| Recall invisibility | claim status=`pending` | excluded from `recall_for_objective` |
| Recall visibility | claim status=`approved` | included in `recall_for_objective` |
| One-way write | recall path attempts write | read-only; no status change |

### Mutation sensitivity targets (from `EPIC.md:84-85`)

- Contradiction precision ≥ 0.9, recall ≥ 0.85 on a seeded 100 claim-pair corpus.
- Gate blocks ≥ 90% of injected negation-pair contradictions pre-approval.

---

## 6. Tickets review — confirm/amend

| ID | Verdict | Amendment |
|---|---|---|
| **G3-T01** | **Confirm** with clarification | Module is `engine/memory/comparator.py` (new package, not `engine/research/`). DDL adds `memory_claims` + `memory_contradictions` (not extending `research_verdicts` — that table stores verdict-level summary, not individual claims). Negation-pair heuristic imported from `evidence.py:630`, not rewritten. |
| **G3-T02** | **Confirm** with amendment | CLI is `ace memory approve/reject` (new subcommand group), not a modification of `ralph approve-pattern`. Shared gate logic in `engine/memory/gate.py` with a `store` parameter. Contradiction pre-check attaches findings (non-blocking). |
| **G3-T03** | **Confirm** with detail | Recall plugs into `ideation.py:96` (novelty signals) and research context builder. Token budget enforced (rank + trim). One-way write/read contract: `approve_for_recall` writes, `recall_for_objective` reads `approved` only. |
| **G3-T04** | **Confirm** | Test plan as §5. Add: mutation sensitivity ≥ 90% on negation-pair injection; recall invisibility test for non-approved rows. |

**One new dependency:** G3-T01's `memory_claims` table must be populated by the research handler when a verdict atom completes. This is a soft dependency on the research-task-type implementation (T01-T10). The comparator works on whatever claims exist — zero claims = zero contradictions = clean pass. No hard block.
