# EPIC: ace-meta-cognitive-ralph-loop

**Status:** draft (S4 — post-design, pre-grill)
**Source phases:** S1 ANALYZE, S2 REDESIGN, S3 FIREPLACE, PRE-EPIC-ace-meta-cognitive-ralph-loop
**Linked ADR:** ADR-0003 (workflow-over-states, drafted in S2 §8; to be finalized in S5)
**Owner:** ACE engine team
**Date:** 2026-09-04

---

## Decision summary

This epic delivers the Ralph loop as a **first-class ACE engine workflow** — not an external script poking at ACE. The `meta-cognitive-ralph-loop` skill becomes `engine/workflows/ralph/`, a library core (`round_driver`, `gates`, `ideation`, `report_types`, `pattern_gate`, `config`) exposed via a **synchronous-but-resumable `run_ralph` action** in `routes/ace_actions.json` (F4-d). Each Ralph round dispatches one ACE run through the existing pipeline; post-commit gate enforcement (Dev→Review→Test→RedTeam) is mechanical, backed by ACE's deterministic validator, tester, and a separate judge model — never LLM self-report. The fresh-agent rule (next round sees only RoundReport + pinned workspace + objective) is enforced by round-driver prompt construction. Learned patterns live in a `ralph_patterns` table in engine.db (canonical, machine-queryable store per owner amendment); approved patterns project to markdown skill-docs as a one-way export that seeds the G9-empty registry. This is ADR-0003: Ralph is a workflow over existing states — no new State enum entries, no new VALID_TRANSITIONS, no parallel pipeline.

**Implementation status:** COMPLETE. See S10 Integration section below.

---

## Motivation

ACE today has bounded re-plan (T4), code-as-intent lanes (Lane-B), an escalation ladder (9B→35B→oracle), TGD signals, and an empty skill-as-docs registry (G9). What it lacks is the core capability the `meta-cognitive-ralph-loop` skill provides: **fresh-agent rounds with no conversation carry-over**, bounded structured hand-off between rounds, **divergent ideation before convergence**, and **adversarial verification** (S1 surprise #5 — ACE's pipeline is purely convergent).

The S1 analysis (skill-analysis-s1.md) found that of the skill's six claimed invariants, **only one** (SQLite persistence) is mechanically enforced by code. The rest — gate ordering, retry caps, iteration limits, JSON output contracts — are instruction-following conventions that a forgetful or compromised LLM can silently violate. ACE's state machine (`state.py` `VALID_TRANSITIONS`) mechanically enforces its gate ordering; the skill has no equivalent. The Ralph loop must inherit ACE's mechanical enforcement, not replicate the skill's convention-only guarantees.

Five hard gaps (S1 §5) drive the requirements below. Each is a reliability or security failure mode that the epic must close mechanically.

---

## Hard requirements

The S1 top-5 gaps are **hard requirements** — they are testable and non-negotiable. The owner amendment adds two more.

| ID | Requirement | Source gap | Testable criterion |
|----|-------------|------------|--------------------|
| **R1** | Gate enforcement is mechanical, not LLM self-report. Each Ralph gate (Dev→Review→Test→RedTeam) is backed by an ACE-native deterministic or model-separated mechanism. The `gate_status` field is set by the harness based on actual execution, never by the orchestrator grading its own homework. | S1 gap #1 | A prompt-audit test confirms no LLM self-reported gate outcome exists in the gate path. Dev uses `validator.py`; Test uses `tester.py`; Review and RedTeam use the judge model on :8080 (separate from the orchestrator). |
| **R2** | Pattern storage complies with ADR-0002. Pattern proposals are `proposed` by the LLM but only enter the prompt-injection path after a mechanical approval gate transitions them to `approved`. No unvalidated content auto-propagates. | S1 gap #2 | A pattern-gate test confirms that `proposed` rows are never injected into agent prompts; only `approved` rows are readable by the query interface. |
| **R3** | Budget accounting is harness-enforced (T8), not LLM-tracked. The round driver checks cumulative `llm_calls` / `total_tokens` / `wall_clock_s` across all rounds in a `ralph_run_id` against `EngineConfig` caps. Exceeding any cap terminates the loop via the shared CANCELLED path. | S1 gap #3 | A budget-exhaustion test confirms the loop terminates when cumulative caps are hit, and no single round can exceed per-run caps. |
| **R4** | Red-Team uses an independent model (ACE judge on :8080 or P6 oracle), not the orchestrator itself. | S1 gap #4 | A model-identity test confirms Red-Team verdicts carry a `model` field that is not the orchestrator's model. |
| **R5** | Fresh-agent execution uses ACE's sandbox (`sandbox.py`: Docker + seccomp + no-network + read-only rootfs + cap-drop ALL). No bare subagent dispatch. | S1 gap #5 | A sandbox-audit test confirms all generated code runs through the Docker sandbox boundary. |
| **R6** | The `ralph_patterns` schema + `pattern_query.py` + approval gate + markdown export projection are first-class deliverables. Canonical store is the `ralph_patterns` table in engine.db (additive-only, WAL). Markdown skill-docs are a one-way export of approved rows, never the source of truth. | Owner amendment (S2 §2, S3 hand-off #8) | A query-interface test confirms `pattern_query.py` returns only `approved` rows; an export test confirms markdown is regenerated from SQL, not read back. |
| **R7** | The fresh-agent rule is mechanically enforced by round-driver prompt construction. Round N+1's LLM prompt is built from ONLY the RoundReport (validated against schema) + pinned workspace at `workspace_sha` + the original objective. Nothing else from the previous round's conversation is carried. | S1 gap #1 (applied to round boundary) | A prompt-audit test inspects the constructed prompt and confirms it contains only the three allowed inputs. |

---

## Scope

### In-scope module list

All modules live under `engine/workflows/ralph/` (new package). Per-module responsibility and key interfaces:

| Module | Responsibility | Key interface |
|--------|---------------|---------------|
| `round_driver.py` | Owns the outer Ralph loop. The ONLY module that knows "we are in a Ralph round." Adapter boundary between Ralph protocol (rounds, reports, gates) and ACE protocol (runs, RunResults, state transitions). | `run_ralph(objective, project_path, config, on_event=None) -> RalphResult`; `resume_ralph(run_id, project_path, config) -> RalphResult` |
| `report_types.py` | Typed round report crossing the round boundary. Pure dataclass, transport-independent, JSON-serializable, validated on construction. Mirrors `replan_types.py` discipline. | `RoundReport` dataclass with `to_dict()`, `to_json()` (≤64KB), `from_json(raw)` (validates schema + bounds) |
| `gates.py` | Mechanical gate enforcement. Each gate backed by ACE-native deterministic or model-separated mechanism. No LLM self-reporting. | `evaluate_gate_sequence(code, task, project_path, transport, config) -> GateVerdict`; `review_gate()`, `test_gate()`, `redteam_gate()` |
| `ideation.py` | Bounded divergent ideation (Ralph Phase 1). Generate N candidate plans, score, select one. Budget-capped. | `generate_candidate_plans(objective, previous_rounds, n_candidates, model_port, max_tokens_per_candidate) -> IdeationResult` |
| `pattern_gate.py` | ADR-0002-compliant pattern registry. Writes `proposed` rows to `ralph_patterns`; approval gate transitions to `approved`; prompt injection reads `approved` only. | `propose_pattern_update(round_report, skill_store) -> PatternProposal`; `persist_pattern(proposal, approval, skills_dir) -> str`; `approve_pattern(pattern_id, approval_token)` |
| `config.py` | Ralph-specific configuration. Extends `EngineConfig` without modifying it. | `RalphConfig` dataclass (see Deliverables for defaults) |

### In-scope engine changes (additive-only)

| Change | Location | Nature |
|--------|----------|--------|
| `ralph_runs` table | `engine.db` (new table) | Top-level grouping: one Ralph execution = one `ralph_run_id` |
| `ralph_rounds` table | `engine.db` (new table) | Per-round ledger: round_id, run_id link, budget, state, workspace_sha |
| `ralph_patterns` table | `engine.db` (new table) | Canonical pattern store (full schema in Deliverables) |
| `run_ralph` action | `routes/ace_actions.json` | New action entry with `objective`, `max_rounds`, `resume_id` params |
| Ralph event types | `engine/events.py` (or equivalent) | `ralph_round_start`, `ralph_round_complete`, `ralph_round_failed`, `ralph_complete` |
| `pattern_query.py` | `engine/workflows/ralph/` or `engine/intent/` | Programmatic query interface for external consumers |

### Key interfaces (signatures from S2)

```python
# round_driver.py
def run_ralph(
    objective: str,
    project_path: str,
    config: RalphConfig,
    on_event: Callable | None = None,
) -> RalphResult: ...

def resume_ralph(
    run_id: str,
    project_path: str,
    config: RalphConfig,
) -> RalphResult: ...

# gates.py
def evaluate_gate_sequence(
    code: str, task: Task, project_path: str, transport, config: RalphConfig,
) -> GateVerdict: ...

# ideation.py
def generate_candidate_plans(
    objective: str, previous_rounds: list[RoundReport],
    n_candidates: int, model_port: int, max_tokens_per_candidate: int,
) -> IdeationResult: ...

# pattern_gate.py
def propose_pattern_update(
    round_report: RoundReport, skill_store: SkillStore,
) -> PatternProposal: ...

def persist_pattern(
    proposal: PatternProposal, approval: ApprovalToken, skills_dir: Path,
) -> str: ...
```

---

## Non-goals

From pre-epic §4:
- **No merges to main without S10's rollback-point commit.** Integration (S10) is the only path to main.
- **No Ralph loop for ACE's own development in this epic.** Dogfooding is S8 output, not S10 input (F1-d ruled out).
- **No change to ADR-0002** (LLMs edit task definitions, never engine code). The Ralph loop drives ACE; humans + S10 integrate.

From S2/S3 rejections:
- **No new state lane.** Adding `RALPH_IDEATION`, `RALPH_DEVELOP`, etc. to the State enum is rejected (S2 §1 Option B). Doubles the transition matrix, violates locality.
- **No Lane-B primitive extension.** Lane-B is code-as-intent with sandbox-only execution — too narrow for the full pipeline (S2 §9, F3-c ruled out).
- **No external orchestrator as end-state.** The pre-epic (line 17-19) explicitly rejects "external script poking at ACE" (F1-e, F3-d ruled out).
- **No new async action semantic.** The action is synchronous-but-resumable via T6 checkpointing (F4-d). Async polling is a future enhancement.
- **No trigger.py integration.** T2 triggers are task-retries, not round-iterations — different abstraction layers (F2-d revisited).

---

## Architecture decisions

### ADR-0003 — Workflow over existing states (embedded summary)

**Status:** proposed (drafted S2 §8, to be finalized in S5)
**Decision:** Implement Ralph as a workflow over existing states. A new `engine/workflows/ralph/round_driver.py` owns the outer loop. Each Ralph round dispatches one ACE run through the existing pipeline (`Engine.run()`). Post-commit gate enforcement is owned by the round driver via `gates.py`. Do NOT add new State enum entries or VALID_TRANSITIONS.

**Consequences:**
- Zero changes to `engine/state.py`, `engine/pipeline.py`, or `engine/engine.py`'s `run()` method.
- Full reuse of T8 budget (`budget.py`), T5 stop/timeout (`stop.py`), T6 resume (`checkpoint.py`), P6 oracle (`oracle_escalate.py`).
- The round driver is a new adapter boundary — "Ralph round" ≠ "ACE run" even though each round dispatches one run.
- Post-commit gates run AFTER the engine commits code. A failed RedTeam gate requires a `git reset` to the pre-round SHA before the next round (new operation on git state — see Risks).

**Alternatives rejected:** new state lane (doubles transition matrix), external orchestrator (rejected by pre-epic), Lane-B extension (too narrow).

### Pattern-store amendment (owner directive)

The canonical pattern store is the `ralph_patterns` table in engine.db (additive-only, WAL, backup discipline). Rationale: external consumers (other agents, projects, dashboards, future tooling) must query learned patterns programmatically — the skill's `query.py` exists for exactly this, and markdown would force a rework later. The ADR-0002 approval gate is **medium-independent**: rows move `proposed→approved` via the mechanical gate; prompt injection reads `approved` rows only. The contamination vector was unvalidated content, not the storage medium. Markdown skill-docs are a **one-way export** of approved patterns (seeds G9), never the source of truth. TTL/expiry and integrity become SQL (`status` transitions, `expires_at` column).

### F4-d — Synchronous-but-resumable action surface

`run_ralph` is added to `routes/ace_actions.json`. The action handler calls `round_driver.execute()` **synchronously** (blocks until done). After each round, the round driver persists a `ralph_rounds` row with `workspace_sha` and cumulative budget. On crash, the operator re-issues `run_ralph` with `resume=ralph_run_id`. The round driver reads the `ralph_rounds` table, finds the last completed round, and continues from N+1. This is Option 1's seam + Option 2's crash resilience + no new async semantic.

### Model routing

| Function | Model | Port | Rationale |
|----------|-------|------|-----------|
| Ideation (plan generation) | 9B (subject) | :8082 | Speed; same model ACE uses for code generation |
| Review gate | Qwen3.6-35B (judge) | :8080 | Separate model; quality over speed |
| RedTeam gate | Qwen3.6-35B (judge) | :8080 | Independent from orchestrator |
| RedTeam escalation | P6 oracle | — | When `redteam_escalate_to_oracle=True` |

---

## Deliverables

### Prototype stages (S3 hand-off items 2a–2e)

#### Stage (a) — Core library + CLI seam
- `round_driver.py`, `gates.py`, `report_types.py` as library modules.
- CLI seam: `cli.py ralph --objective "..."` constructs `RalphConfig` and calls `run_ralph()`.
- **Acceptance:** A single-round Ralph run completes end-to-end through the CLI; a `RoundReport` is persisted and validated.

#### Stage (b) — Action route thin wrapper
- Add `run_ralph` to `routes/ace_actions.json` with `objective`, `max_rounds`, `resume_id` parameters.
- Action handler is a thin adapter over `round_driver.execute()`.
- **Acceptance:** The same Ralph run is dispatchable via the action route with identical behavior to the CLI seam.

#### Stage (c) — Resume + round ledger
- `ralph_rounds` table in engine.db (additive-only).
- `resume=ralph_run_id` parameter: continues from last completed round.
- **Acceptance:** A Ralph run that crashes at round 3 resumes at round 3 (not round 1) — verified by `resume_ralph()` test.

#### Stage (d) — Divergent ideation
- `ideation.py`: bounded divergent ideation (default 5 candidates, max 8, 9B on :8082).
- G12 ideation-collapse detection wired via round ledger.
- **Acceptance:** Ideation produces N scored candidates; selection reason persisted in `RoundReport.ideation_summary`; G12 signal fires when the same solution repeats across rounds.

#### Stage (e) — Pattern registry (ADR-0002)
- `pattern_gate.py` writing to `ralph_patterns` table.
- `pattern_query.py` query interface.
- `pattern_export.py` markdown projection to skill registry.
- **Acceptance:** Pattern proposals require approval before injection (ADR-0002) — verified by pattern-gate test; markdown export is regenerated from SQL, not read back.

### RoundReport v1 schema

The full JSON schema is defined in redesign-s2.md §3. Key validation bounds (restated; see source for the complete schema):

| Field | Bound | Rationale |
|-------|-------|-----------|
| `round_id` | Monotonic, gapless | Fresh-agent rule: round N+1 must reference round N |
| `objective` | ≤4096 chars | Prevents context bleed from prior rounds |
| `plan` | ≤16384 chars | Selected plan only, not all candidates |
| `tasks_executed` | ≤32 items | One round = one logical task; 32 is a safety bound |
| `pattern_proposals` | ≤8 per round, each ≤8192 chars | Prevents pattern spam; ADR-0002 scope limit |
| `workspace_sha` | Valid git SHA (7–40 hex) | Pinned workspace state for the next round |
| Total JSON | ≤64KB serialized | Size bound for the fresh-agent context |

### `ralph_patterns` schema (full, from S3 hand-off item 8)

```sql
CREATE TABLE IF NOT EXISTS ralph_patterns (
    id TEXT PRIMARY KEY,                     -- pattern UUID
    project_namespace TEXT NOT NULL,          -- per-project isolation (kills cross-project contamination)
    pattern_type TEXT NOT NULL,               -- 'strategy' | 'lesson' | 'anti_pattern' | 'heuristic'
    body TEXT NOT NULL,                       -- pattern content (validated, max 8192 chars)
    keywords TEXT,                            -- JSON array for programmatic query (replaces skill's LIKE %keyword%)
    score REAL DEFAULT 0.0,                   -- quality/confidence score
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | rejected | expired
    source_ralph_run_id TEXT,                 -- which Ralph execution produced this
    source_round_id INTEGER,                  -- which round within that run
    approved_by TEXT,                         -- NULL until approval gate passes
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT,                          -- TTL/expiry (NULL = never)
    FOREIGN KEY (source_ralph_run_id) REFERENCES ralph_runs(ralph_run_id)
);
CREATE INDEX IF NOT EXISTS ralph_patterns_status_ns ON ralph_patterns(status, project_namespace);
CREATE INDEX IF NOT EXISTS ralph_patterns_keywords ON ralph_patterns(keywords);
```

### `ralph_rounds` table

```sql
CREATE TABLE IF NOT EXISTS ralph_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ralph_run_id TEXT NOT NULL,          -- groups rounds in one Ralph execution
    round_id INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES engine_runs(id),  -- link to ACE run
    llm_calls INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    wall_clock_s REAL DEFAULT 0.0,
    state TEXT NOT NULL,                 -- completed | failed | cancelled
    workspace_sha TEXT,                  -- pinned git SHA after commit
    created_at TEXT DEFAULT (datetime('now'))
);
```

### Gate table

| Ralph gate | ACE mechanism | Deterministic? | Model |
|------------|---------------|----------------|-------|
| Dev | `validator.py:validate()` — AST/import/style/execution | Yes | None |
| Review | `llm_judge.py:score_task()` via judge on :8080 | No (LLM) | Qwen3.6-35B (:8080) |
| Test | `tester.py:run_tests()` — actual test execution | Yes | None |
| RedTeam | `llm_judge.py:score_task()` with red-team rubric, OR `oracle_escalate.py` for P6 | No (LLM) | 35B (:8080) or P6 oracle |

### Budget / stop / resume behavior

- **Budget:** Round driver sums `llm_calls` + `total_tokens` across all prior rounds in the `ralph_run_id`; checks against `EngineConfig.max_llm_calls` / `max_total_tokens` before each round. Cumulative exhaustion → CANCELLED.
- **Stop:** Round driver checks `stop.check_stop_file(run_id)` before each round and between gate evaluations. Stop-file detected → current round CANCELLED, loop terminates, last successful `workspace_sha` recorded.
- **Resume:** `resume_ralph(run_id)` loads `ralph_rounds`, finds last completed round, checks out workspace at `workspace_sha`, reconstructs RoundReport, continues from N+1. Reuses `checkpoint.reconcile_git_state()`.

### Config defaults (`RalphConfig`)

| Field | Default | Rationale |
|-------|---------|-----------|
| `max_ralph_rounds` | 5 | Outer loop cap (answers pre-epic Open Q #4) |
| `max_round_retries` | 3 | Per-round gate retry cap |
| `ideation_candidates` | 5 | N for divergent ideation (max 8) |
| `ideation_model_port` | 8082 | 9B for ideation (speed) |
| `review_model_port` | 8080 | 35B judge for review |
| `redteam_model_port` | 8080 | 35B judge for red-team |
| `redteam_escalate_to_oracle` | False | P6 oracle for red-team |
| `pattern_approval_required` | True | ADR-0002 gate |
| `max_pattern_length_bytes` | 8192 | Per-pattern size cap |
| `round_timeout_s` | 1800 | Per-round wall-clock cap |

---

## Metrics / success criteria

**Headline: Ralph pass rate.** Objective achieved within `max_ralph_rounds`, N4-style ≥60% target on a real workload (S8 dogfood). A "pass" = the Ralph loop terminates with state "completed" (objective achieved) within the round cap.

**Mechanical-gate coverage:** 0 LLM-self-reported gate outcomes. Every gate verdict is produced by `validator.py`, `tester.py`, `llm_judge.py`, or `oracle_escalate.py` — never by the orchestrator's self-evaluation.

**Budget compliance:** No Ralph run exceeds T8 cumulative caps (`max_llm_calls`, `max_total_tokens`, `max_wall_clock_s`). Verified by budget-exhaustion test.

**G12 ideation-collapse detection:** Wired. The round ledger records `ideation_summary.selected_idx` per round; repeated selection of the same plan across ≥2 rounds triggers the G12 signal.

**G9 skill-registry seeding:** Approved patterns project to markdown skill-docs in the skill registry. The G9-empty registry is seeded via `pattern_export.py`.

---

## Risks & mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Post-commit gate failure requires git-reset revert** | HIGH | A failed RedTeam gate means the engine already committed code. The round driver must `git reset` to the pre-round SHA before the next round. This is a new operation on git state. Mitigation: the reset is a single deterministic command; the pre-round SHA is always recorded in `ralph_rounds.workspace_sha`. **Open question for S5:** safety of git-reset revert with engine checkpoints (T6 `reconcile_git_state` may conflict). |
| **Fresh-agent rule is driver-enforced, not harness-level** | MEDIUM | The round driver constructs the next round's prompt from only three inputs. A buggy driver could leak context. Mitigation: prompt-audit test (R7) catches leaks; the RoundReport schema bounds size (≤64KB). |
| **Round-report poisoning** | MEDIUM | The RoundReport is LLM-written content that becomes the next round's context. A poisoned report (e.g., injected instructions) could steer the next round. Mitigation: schema validation + size bounds; the report is treated as untrusted model output (ACE threat model). **Open question for S5:** whether to add content sanitization beyond schema validation. |
| **Mid-round budget exhaustion** | MEDIUM | A round that exhausts T8 budget mid-execution (e.g., during ideation) leaves no RoundReport. Mitigation: the round is marked CANCELLED; the `ralph_rounds` row records partial budget; resume continues from N+1 with the last successful round's report. |
| **Pattern-library semantic poisoning** | LOW-MEDIUM | Even with the approval gate, a human could approve a bad pattern. Mitigation: per-project namespacing (`project_namespace`), TTL (`expires_at`), and the ability to transition `approved→rejected`. The gate is necessary but not sufficient — human judgment remains. |
| **Judge model availability** | MEDIUM | Review and RedTeam gates depend on the 35B judge on :8080. If the judge is down, gates block. Mitigation: configurable `review_model_port` / `redteam_model_port`; future fallback to P6 oracle. |
| **Ideation budget blowup** | LOW | Generating 8 candidates × 2048 tokens = 16K tokens/round on ideation alone. Mitigation: hard cap of 16K tokens per round for ideation; counts against T8; mid-ideation exhaustion uses whatever candidates exist (minimum 1). |

---

## Open items for S5 grill — RESOLVED (see "S5 Grill resolutions" below)

The genuinely uncertain decisions the grill attacked:

1. **Ideation scoring rubric details.** The current design scores on 3 dimensions (feasibility, alignment, novelty) in a single LLM call. Are the rubric weights right? Should novelty be penalized (to avoid gold-plating) or rewarded (to escape local optima)?
2. **Git-reset revert safety with engine checkpoints.** T6 `reconcile_git_state()` already manages git state. How does a post-commit gate failure (which needs `git reset`) interact with T6's reconciliation? Could a reset corrupt the checkpoint?
3. **Pattern approval UX.** The approval gate is mechanical (`proposed→approved`), but *who* approves and through what interface? CLI command? Gitea issue? Auto-approve for testing (`pattern_approval_required=False`)?
4. **Resume semantics edge cases.** What happens if the operator resumes with a *different* `RalphConfig` (e.g., more rounds)? What if the workspace has been manually modified between crash and resume? Should resume validate workspace cleanliness?
5. **`ralph_patterns` keyword/search design.** The schema uses a JSON `keywords` array. Should this be a full-text search (FTS5) instead? How does `pattern_query.py` rank results — by `score`, recency, or keyword overlap?
6. **Round-report content sanitization.** Beyond schema validation and size bounds, should the report be sanitized for injected instructions? Or is "untrusted model output" the correct threat model (sandbox contains the damage)?
7. **Model routing for ideation.** 9B on :8082 is the default, but should ideation be upgradeable to 35B for harder objectives? Is the port-based routing too coarse?
8. **G12 ideation-collapse threshold.** How many rounds of repeated selection trigger the signal? 2? 3? Should it be configurable?

---

## S5 Grill resolutions (authoritative amendments — supersede any contradicting text above)

All 8 open items resolved by the S5 grill (planner answers, owner-delegated;
round-2 challenges applied). Glossary terms recorded in `CONTEXT.md`; judge
boundary recorded in ADR-0004.

| # | Item | Resolution |
|---|------|-----------|
| 1 | Ideation rubric | Scoring call outputs JSON `[{feasibility, alignment, novelty}]`, 0–10, all candidates in one call. Malformed JSON: one repair retry, then default 5/5/5 + telemetry event (mirrors `llm_judge.py` one-retry discipline) — never abort ideation. Tie-break order hardcoded: feasibility → alignment → novelty. Novelty is **mechanical**: TF-IDF/trigram overlap vs prior selected plans, fed to the scoring prompt as `novelty_signals`, never LLM-judged. |
| 2 | Revert safety | Round driver uses `git reset --hard <pre-round-sha>` (`ralph_rounds.workspace_sha` records the pre-round SHA). Aborted run → existing `FAILED` state (no new state; ADR-0003 holds). `reconcile_git_state()` is provably a no-op post-reset (it only pushes orphan local commits, never pulls/force-pushes) — resume cannot land on a reverted SHA. Remote feature branch retains the bad commit (acceptable; ephemeral). New `revert_round()` in round_driver emits `ralph_round_failed`. |
| 3 | Pattern approval | **Async.** Patterns stay `proposed`; the run finishes without blocking; owner approves via `cli.py ralph approve-pattern/reject-pattern <pattern_id>`; next run picks up approved rows. `pattern_approval_required=False` auto-approves with `approved_by="auto"` (testing kill switch only). |
| 4 | Resume semantics | Resumable from `failed`/`cancelled` only (`completed` is terminal). Exhausted cumulative budget on resume → hard stop (caps are immutable invariants, R3). Concurrency: `BEGIN IMMEDIATE` on the `ralph_runs` row → `RalphRunBusyError` for a second invoker. Dirty worktree: `git stash` then checkout; failure → `DirtyWorktreeError`, never auto-discard operator work. |
| 5 | Pattern search | LIKE-based multi-keyword AND (no FTS5 — its virtual table + sync triggers conflict with the additive-only DB rule). `query_patterns(project_namespace, keywords, status='approved', limit=10)`, ranked `score DESC, created_at DESC`. Namespace defaults to current project. |
| 6 | Report sanitization | Deliberate threat-model decision: schema validation + maxLength truncation (not rejection) + sandbox containment are the boundaries; gate details stay free text. Cross-round prompt-injection surface is *accepted and documented in ADR-0004 trail* — both LLMs share the engine trust level; no content-level sanitization (whack-a-mole). |
| 7 | Model routing | Confirmed: ideation 9B `:8082`; review/redteam 35B `:8080` (shared with pipeline judge — multi-serve, `JUDGE_PORT != SUBJECT_PORT` guard holds); oracle stays escalation-only (`redteam_escalate_to_oracle=False` default). |
| 8 | G12 collapse | **Redecided in round 2** (the planner's original `selected_idx` metric was invalid — candidate lists regenerate each round, making index identity meaningless): TF-IDF cosine similarity over selected `plan` text, reusing the `IntentRouter` vectorizer primitive (`TfidfVectorizer(ngram_range=(1,2), word)`) extracted into `engine/intent/similarity.py: plan_similarity(a, b) -> float`, reused by both router and G12. Trigger: similarity ≥ 0.75 vs ANY of the last 3 rounds' plans, 2 consecutive rounds → forced-diversity instruction in next ideation + `ralph_ideation_collapse` event; never fails the run. Config: `g12_similarity_threshold=0.75`, `g12_consecutive_rounds=2`. |
| QB | Judge-gate boundary | **ADR-0004**: the telemetry/gate distinction is pipeline-context-dependent. Normal pipeline: LLM Judge stays telemetry-only (ADR-0002 unchanged). Ralph: the unchanged `llm_judge.py` scorer feeds a *Ralph gate* whose pass/fail the round driver computes and enforces via post-commit revert. ADR-0002 is narrowed, not amended. Glossary updated (*Ralph gate*, *Judge Gate vs LLM Judge*). |

New modules implied by the grill: `engine/intent/similarity.py` (shared
`plan_similarity`), `pattern_query.py`, CLI `ralph approve-pattern` /
`ralph reject-pattern`. These join the Stage (d)/(e) deliverables.

---

## S6 Red-team amendments (MUST-FIX before S7 — authoritative, like S5 resolutions)

8 findings, 3 Critical/High (`.scratch/ralph-loop/research/redteam-s6.md`). All mandatory amendments folded into scope:

| # | Amendment | Epic change |
|---|-----------|-------------|
| M1 | **Revert mechanism (F12, High)** — `git reset --hard` leaves remote ahead of local; next round's push is non-FF. | Revert = `git revert` of the round's commits (keeps history linear, no force-push). If artifacts make revert ambiguous, `push --force-with-lease` is the documented fallback. `revert_round()` implements revert; the S5 "remote retains bad commit" note is superseded. |
| M2 | **Cumulative budget clamp (F4, Medium)** — cumulative cap only checked between rounds; one round can burn everything. | Per-round T8 caps are set to `min(config.cap, remaining_cumulative)` before each round. R3's testable criterion extended accordingly. |
| M3 | **Gate-gaming via patterns (F3, High)** — approved patterns can teach future rounds to optimize the judge's rubric instead of the objective (learning loop × verification loop reinforcement). | Pattern poisoning elevated to HIGH: `max_patterns_per_run` cap; pattern-proposal similarity check surfaced in the approve CLI; new detection signal — Review pass rate vs objective-completion rate divergence (G13) wired to `ralph_*` telemetry. Patterns remain advisory-only. |
| M4 | **Resume operability (F6)** — resume is theoretical without a status surface. | `cli.py ralph status [ralph_run_id]` added to Stage (c) deliverables: lists runs, round states, last workspace_sha, resume command hint. |
| M5 | **Ralph event projection (F8/F11)** — `ralph_*` events declared but JSONL handler drops them. | `RalphJsonlHandler` (mirrors `EscalationJsonlHandler`) writes `ralph_run_<id>_rounds.jsonl`; auto-wired in `run_ralph()`. |
| M6 | **Ralph-run report (F9)** — autopsies need one file, not N round files. | `RalphResult.to_report_dict()` → `ralph_report.json` persisted per ralph run (Archive-compatible shape). |
| M7 | **`ralph_runs` schema (F13)** — declared but unowned; `ralph_patterns` FKs to it. | Full `ralph_runs` schema added to Deliverables; `round_driver` owns row creation + the `BEGIN IMMEDIATE` lock (F14's WAL subtlety noted: lock via explicit transaction on a dedicated connection). |
| M8 | **Workspace denillist (F1)** — workspace files are an unaudited unstructured channel across the round boundary. | `RalphConfig.workspace_denylist` (e.g., prior rounds' scratch dirs); R7 prompt-audit test extended to assert no denillisted paths present at round start. |

**Add-ons accepted into epic:** Ralph autopsy/Archive integration (M6 makes ralph runs first-class autopsy subjects) and G11 round-churn signal wiring (rounds-per-task churn into `ralph_*` telemetry). **Deferred to ledger:** pattern TTL sweeper UI, dashboard surface, ralph-dogfooding (post-v1).

---

## Traceability appendix

| Epic item | Source doc | Section |
|-----------|------------|---------|
| Decision summary (workflow-over-states) | redesign-s2.md | §1 (Option A) |
| Decision summary (F4-d action surface) | fireplace-s3.md | F4-d, Convergence |
| Decision summary (pattern store) | redesign-s2.md §2; fireplace-s3.md | `pattern_gate.py` block; owner amendment |
| Motivation | PRE-EPIC-ace-meta-cognitive-ralph-loop.md | §1 |
| Motivation (1/6 invariants) | skill-analysis-s1.md | §1 summary |
| R1 (mechanical gates) | skill-analysis-s1.md | §5 gap #1 |
| R2 (ADR-0002 patterns) | skill-analysis-s1.md | §5 gap #2 |
| R3 (T8 budget) | skill-analysis-s1.md | §5 gap #3 |
| R4 (independent RedTeam) | skill-analysis-s1.md | §5 gap #4 |
| R5 (sandbox) | skill-analysis-s1.md | §5 gap #5 |
| R6 (ralph_patterns deliverable) | fireplace-s3.md | hand-off #8; redesign-s2.md §2 owner amendment |
| R7 (fresh-agent enforcement) | redesign-s2.md | §3 (fresh-agent rule) |
| Scope module list | redesign-s2.md | §2 |
| Key interfaces | redesign-s2.md | §2 (per-module blocks) |
| Non-goals (no main merge) | PRE-EPIC-ace-meta-cognitive-ralph-loop.md | §4 |
| Non-goals (no recursive Ralph) | PRE-EPIC-ace-meta-cognitive-ralph-loop.md | §4; fireplace-s3.md F1-d |
| Non-goals (no Lane-B) | redesign-s2.md | §9; fireplace-s3.md F3-c |
| Non-goals (no external harness) | PRE-EPIC-ace-meta-cognitive-ralph-loop.md | line 17-19; fireplace-s3.md F1-e |
| ADR-0003 | redesign-s2.md | §8 |
| Pattern-store amendment | redesign-s2.md §2; fireplace-s3.md | `pattern_gate.py` block; owner amendment |
| F4-d surface | fireplace-s3.md | F4-d, Convergence |
| Model routing | redesign-s2.md | §2 config.py; §4 gate mapping |
| Prototype stages (a)–(e) | fireplace-s3.md | hand-off #2 |
| RoundReport schema bounds | redesign-s2.md | §3 |
| ralph_patterns schema | fireplace-s3.md | hand-off #8 |
| ralph_rounds schema | redesign-s2.md | §7 |
| Gate table | redesign-s2.md | §4 |
| Budget/stop/resume behavior | redesign-s2.md | §7 |
| Config defaults | redesign-s2.md | §2 config.py |
| Metrics (Ralph pass rate) | PRE-EPIC-ace-meta-cognitive-ralph-loop.md | §5 (metrics suggestion) |
| Metrics (G12, G9) | PRE-EPIC-ace-meta-cognitive-ralph-loop.md | §5 (round ledger suggestion) |
| Risks (git-reset revert) | redesign-s2.md | §8 ADR consequences |
| Risks (fresh-agent convention) | redesign-s2.md | §8 ADR consequences |
| Risks (S1 gaps) | skill-analysis-s1.md | §3, §5 |
| Open items | fireplace-s3.md | implicit in F-frames; explicit in hand-off |
| Traceability | — | this table |

---

## Source disagreements flagged

1. **S2 §6 vs. owner amendment on pattern storage.** S2 §6 originally rejected SQLite entirely in favor of markdown skill-docs as canonical store. The owner amendment (recorded in S2 §2 `pattern_gate.py` block and S3 opening directive) **partially overrules** this: the canonical store is now the `ralph_patterns` table in engine.db. Markdown remains a one-way export. The epic follows the owner amendment as the binding decision; S2 §6's "SQLite as primary storage" rejection is superseded, while its other rejections (cross-project auto-injection, /tmp staging, unvalidated content, executable patterns) stand.

2. **S2 §9 vs. S3 on "Option 2 first."** S2 §9 recommended "prototype Option 2 (loop-over-runs), migrate to Option 1 (action route)." S3 disagrees: the decision is "build the core once, expose via action route immediately." The epic follows S3's convergence (F4-d synchronous-but-resumable action from day one), not S2's two-phase migration.

---

## S10 Integration — Implementation complete

**Status:** INTEGRATED to main (commit `10eb077`), suite **956 passed / 4 skipped**.

### S9 Code Review

The engine-generated output was **rejected** by S9 review:
- 6/7 atoms were hollow commits (gates.py, pattern_gate.py absent despite commit messages)
- Mock-theater tests failed
- Schema violations in round_driver and pattern_gate

**Owner decision:** Option A — direct implementation by coding agent.

### Direct Implementation

Feature branch `feature/ace-meta-cognitive-ralph-impl` implemented 11 deliverables:
- `engine/workflows/ralph/similarity.py` — shared TF-IDF primitive
- `engine/workflows/ralph/config.py` — RalphConfig with all defaults
- `engine/workflows/ralph/report_types.py` — RoundReport dataclass
- `engine/workflows/ralph/gates.py` — mechanical gate enforcement
- `engine/workflows/ralph/ideation.py` — divergent ideation with G12 collapse detection
- `engine/workflows/ralph/pattern_gate.py` — pattern registry with approval gate
- `engine/workflows/ralph/pattern_query.py` — query interface for approved patterns
- `engine/workflows/ralph/round_driver.py` — core Ralph loop with resume + budget clamp
- `engine/workflows/ralph/__init__.py` — facade run_ralph/resume_ralph
- `engine/workflows/ralph/cli.py` — CLI commands: ralph run/resume/status/approve-pattern/reject-pattern
- `routes/ace_actions.json` — run_ralph action entry

Plus 100+ tests added.

### Re-review and Fixes

Re-review identified 4 must-fixes:
1. resume_ralph didn't execute rounds
2. budget config fields missing → clamp dead code
3. max_patterns_per_run unconsumed
4. pattern_approval_required unconsumed

All fixes applied in commit `0ff9589`, verified, then integrated.

### Integration

- Rollback point tag `pre-ralph-integration` → `d5f4f20`
- Merged `feature/ace-meta-cognitive-ralph-impl` (commit `10eb077`)
- Merged docs bundle `docs/features/ralph-loop/` (commit `5a0642a`)
- Both merges conflict-free
- Full suite: **956 passed / 4 skipped on main**
- LEDGER entry 52 pushed
- Issue #24 closed via API (feature branch snapshot superseded by main integration)

### Follow-ups (ledgered, not blocking)

- G13 gate-gaming signal (M3 partial)
- R5 Engine.run dispatch integration
- A12/A24/A29 model-capability classes from run-3
- Atom contract-validation acceptance criteria
