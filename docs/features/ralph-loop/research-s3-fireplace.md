# S3 FIREPLACE: How Does ACE *Issue* a Ralph Run?

**Phase:** S3 FIREPLACE (multi-perspective brainstorming)  
**Date:** 2026-09-04  
**Inputs:** PRE-EPIC-ace-meta-cognitive-ralph-loop.md, skill-analysis-s1.md, redesign-s2.md  
**S2 position to challenge:** "Prototype Option 2 (loop-over-runs), migrate to Option 1 (action route)."

**Owner directive amendment (applied to F3/F4 + S4 hand-off):** S2 §6's decision to replace the pattern library's SQLite store with markdown skill-docs has been partially overruled. The canonical pattern store is now a `ralph_patterns` TABLE in engine.db (additive-only, WAL, typed columns: id, project namespace, pattern type, body, keywords, score, status `proposed|approved|rejected|expired`, source `ralph_run_id`/`round_id`, created/expires timestamps). Rationale: external consumers (other agents, projects, dashboards, future tooling) must query learned patterns *programmatically* — the skill's `query.py` exists for exactly this, and markdown would force a rework later. The ADR-0002 approval gate is unchanged and *medium-independent*: rows move `proposed→approved` via the mechanical gate; prompt injection reads `approved` rows only. The contamination vector was unvalidated content, not the storage medium. Markdown skill-docs become an **export/projection** of approved patterns (still seeds the G9 skill registry), never the source of truth. TTL/expiry and integrity become SQL (`status` transitions, `expires_at` column). Implication for S3: evaluate issue-surface options partly on "can this surface expose the pattern store to external consumers cleanly," and include in S4's hand-off the requirement that the epic defines the `ralph_patterns` schema + query interface as a first-class deliverable.

---

## Frames

### F1 — Operator Frame: Who/What Starts a Ralph Run?

The question is *which entity* is authorized to begin a Ralph execution and through what channel.

| Option | Description | Trade-offs |
|--------|-------------|------------|
| **F1-a: Human via CLI** | `python3 cli.py ralph --objective "..."` — a new subcommand on the existing CLI that constructs a RalphConfig and calls `run_ralph()`. | ✅ Lowest friction; CLI already exists (`cli.py`). ✅ Operator gets stdout/stderr. ❌ Human must be present for the full duration (hours-long run). ❌ No programmatic hook for automation. |
| **F1-b: Remote-control API** | A new method on `RemoteControl` (`remote_control.py` is 1088 lines, all SSH-tunneled). `ralph_run(objective)` tunnels through SSH to Triton. | ✅ Reuses the existing remote-control surface (status, bench, sandbox all go through here). ✅ Operator can start from `nightmare` and let Triton run. ❌ Adds a long-lived SSH session; a dropped connection mid-run orphans the process unless it's backgrounded. |
| **F1-c: CI / scheduled trigger** | A Gitea Actions workflow (or cron) that invokes the Ralph loop on a schedule or on PR events. | ✅ Enables "nightly self-improvement" runs. ❌ No human in the loop to approve pattern-library proposals (ADR-0002 violation if patterns auto-persist). ❌ CI timeout budgets may not accommodate hours-long runs. |
| **F1-d: Another ACE run (recursive)** | An ACE run whose task is "improve the engine via Ralph" — the run's Lane-B or action dispatch invokes `run_ralph()`. | ✅ The Ralph loop can be used to improve ACE itself (dogfood). ❌ Recursive budget: a Ralph run contains N ACE runs. T8 caps must be nested, not flat. ❌ The pre-epic (§4) explicitly says "No Ralph loop for ACE's own development in this epic." → **ruled out for v1**. |
| **F1-e: DSH harness (status quo)** | The outer DSH `ralph` tool orchestrates ACE runs via CLI/API, exactly as the skill does today. | ✅ Zero engine changes. ❌ Pre-epic (line 17-19) explicitly rejects this: "first-class engine workflow, not an external script poking at ACE." |

**Frame insight:** The operator is *not* the hard part. Any of F1-a/b/c can be a thin adapter over the same entry function. The real question is *where that entry function lives* (F2/F3). F1-d is ruled out by the pre-epic's non-goals. F1-e is the status quo the epic exists to eliminate.

---

### F2 — Lifecycle Frame: Long-Running Async Workflow vs. N Short Runs

A Ralph run may take hours (5 rounds × 30 min/round = 2.5h). How does the engine model this duration?

| Option | Description | Trade-offs |
|--------|-------------|------------|
| **F2-a: Single long-running process** | `run_ralph()` is a blocking call that runs all rounds in one Python process (S2's `round_driver.py` outer loop). | ✅ One `run_id`, one session row, one budget tally. ✅ Simplest mental model. ❌ The process must survive hours; a SIGKILL, OOM, or network blip kills the entire run. ❌ T8 wall-clock cap (`max_wall_clock_s`) is checked per-round but the *process* has no checkpoint boundary — a mid-round kill loses the round's partial work. |
| **F2-b: N independent runs, outer orchestrator stitches** (S2 Option 2) | Each Ralph round = one `Engine.run()`. An outer orchestrator (CLI script, DSH tool) reads the RoundReport, constructs the next PRD, and starts the next run. | ✅ Each round is independently checkpointed (T6), budgeted (T8), and stoppable (T5). ✅ A crashed round doesn't kill the others — resume is "start round N+1." ❌ No single `run_id` for the whole Ralph execution — debugging requires correlating N run IDs via the `ralph_rounds` table. ❌ The orchestrator is *outside* the engine; it must reimplement budget carry-across (S2 §7 proposes a `ralph_rounds` table for this). |
| **F2-c: Async action with polling** (S2 Option 1 variant) | `run_ralph()` is dispatched as an async action. The engine returns a `ralph_run_id` immediately; the operator polls `status(ralph_run_id)` for progress. | ✅ Non-blocking; the operator/CI can disconnect and re-poll. ✅ Single `ralph_run_id` for the whole execution. ❌ Requires a new "async action" semantic in the action dispatch layer — today's actions are synchronous (run_tests, commit, grep all block until done). ❌ The polling surface must be built (new endpoint or CLI flag). |
| **F2-d: Trigger.py integration** | Ralph rounds are triggered by the existing T2 trigger mechanism (`trigger.py`). A failed round produces an error signature; the trigger fires the next round. | ✅ Reuses the existing escalation machinery. ❌ T2 triggers are designed for *re-plan within a task*, not *new round of a workflow*. The trigger's notion of "budget" is `max_retries_generate`, not `max_ralph_rounds`. ❌ Conflating two abstraction layers (task retry vs. round iteration). |

**Frame insight:** F2-b (N independent runs) is the only option that reuses *all* existing lifecycle machinery (T5 stop, T6 checkpoint, T8 budget) without modification. F2-c (async action) is the end-state target but requires new async semantics. The key realization: **the lifecycle frame is where S2's Option 2 earns its keep** — not because it's elegant, but because it inherits T5/T6/T8 for free.

---

### F3 — Trust Frame: Which Surface Enforces the Fresh-Agent Rule and Budget Mechanically?

S1 gap analysis established that the skill's invariants are convention-only. ACE must enforce mechanically. The issue surface determines *where the enforcement lives*.

| Option | Fresh-agent enforcement | Budget enforcement | Trade-offs |
|--------|------------------------|-------------------|------------|
| **F3-a: Action route (Option 1)** | The action dispatch path constructs each round's prompt from ONLY the RoundReport + pinned workspace + objective. The engine *is* the harness; it controls prompt construction. | T8 counters live in `orchestrator_session` (session.py). The action route calls `Engine.run()` which calls `sess.create_session()` — budget is automatic. | ✅ Both rules enforced by engine code, not convention. ❌ Requires the action dispatch path to handle a multi-round workflow (today's actions are single-shot). |
| **F3-b: Loop-over-runs (Option 2)** | The outer orchestrator constructs the next round's PRD. The engine sees only a PRD — it cannot verify that the PRD contains *only* the RoundReport and not leaked context. | The orchestrator must sum T8 counters across runs (S2 §7 proposes this). The engine's per-run budget is automatic; cross-run carry-across is the orchestrator's responsibility. | ✅ Engine is unchanged. ❌ Fresh-agent rule is a convention in the orchestrator, not a mechanical guarantee. A buggy orchestrator leaks context. |
| **F3-c: Lane-B primitive (Option 3)** | Lane-B's `execute()` already enforces depth ≤2 and sandbox-only execution (laneb.py line 30-31). A `ralph_round()` primitive would inherit these gates. | Lane-B's `_bump_budget()` (laneb.py line 472-490) already integrates with T8 via the injected `budget_increment` closure. | ✅ Strongest mechanical enforcement (sandbox + depth + budget all binding). ❌ Lane-B is code-as-intent with sandbox-only execution — it cannot run the full ACE pipeline (generate → validate → test → commit). Ralph needs the full pipeline. → **Ruled out** (S2's rejection stands). |
| **F3-d: External harness (Option 4)** | No enforcement. The DSH ralph tool constructs prompts; the engine cannot verify freshness. | The harness must track budget itself (the skill's ITERATION_LIMIT problem, S1 gap #3). | ❌ Rejected by pre-epic. |

**Frame insight:** The fresh-agent rule is the harder invariant. Only F3-a (action route) and F3-c (Lane-B) can enforce it mechanically, and F3-c is ruled out by capability mismatch. **This is the strongest argument for F3-a as the end-state.** But F3-b can approximate it with a *verifiable contract*: the RoundReport schema (S2 §3) is a typed dataclass with max-length bounds; the orchestrator can validate it before constructing the next PRD. It's "convention + validation" rather than "mechanical," which is the same trust level as ACE's existing PRD parsing (prd_parser.py is deterministic but trusts the PRD *content*).

**Pattern-store dimension (owner amendment):** The fresh-agent rule is not the only trust question — *who can read and write the pattern store* matters equally. Under the revised storage contract, `ralph_patterns` lives in engine.db and is queryable by any engine surface. This changes the trust calculus:

| Option | Pattern-store exposure to external consumers | Trust implication |
|--------|---------------------------------------------|-------------------|
| **F3-a: Action route** | The action handler writes patterns to `ralph_patterns` via `pattern_gate.persist_pattern()`. External consumers query engine.db directly (same as `orchestrator_session`, `engine_runs`). The approval gate is engine-enforced: only `approved` rows are ever read by the prompt-injection path. | ✅ Canonical store is *inside* the engine's trust boundary. The ADR-0002 gate is mechanical (SQL `status` transition), not convention. External consumers get programmatic access without bypassing the gate. |
| **F3-b: Loop-over-runs** | The outer orchestrator writes patterns. If the orchestrator is external (DSH tool), it must either (1) write to engine.db over a trust boundary it doesn't own, or (2) maintain a *separate* pattern store and sync — reintroducing the very fragmentation the owner overruled. | ❌ Either the orchestrator bypasses the engine's trust boundary, or there are two sources of truth. Both violate the "canonical store in engine.db" contract. |
| **F3-d: External harness** | Patterns live outside engine.db (status quo the owner overruled). | ❌ Rejected. |

**Revised F3 insight:** The owner's amendment *strengthens* the case for the action route. With `ralph_patterns` as a first-class engine.db table, the issue surface must (a) write patterns through the engine's mechanical approval gate, and (b) expose them to external consumers via the engine's existing DB-query interfaces. Only the action route does both without a trust-boundary violation. The loop-over-runs approach forces the orchestrator to either own the DB write (violating engine locality) or maintain a parallel store (violating single-source-of-trust). The contamination vector the owner identified was *unvalidated content*, not the storage medium — and the action route's mechanical `proposed→approved` transition is the enforcement.

---

### F4 — Evolution Frame: Prototype-to-Target Path

Which option has the cheapest migration to the others? Is there an option that is BOTH prototype and end-state?

| Option | Migration cost to others | Prototype + end-state? |
|--------|--------------------------|------------------------|
| **F4-a: Option 2 (loop-over-runs) → Option 1 (action route)** | Medium. The RoundReport schema, gate sequence, and ideation module are *identical* under both. The migration is: (1) move the outer loop from the orchestrator into `round_driver.py`, (2) add `run_ralph()` to the action route table. The `round_driver` core is the same; only the *entry seam* changes. | No. Option 2 leaves the loop outside the engine; Option 1 brings it inside. They share internals but have different seam placement. |
| **F4-b: Option 1 thin-wrapper over Option 2 internals** | Low. If Option 2 is built first with a clean `round_driver.py` module, Option 1 is just: add `run_ralph` to `ace_actions.json`, wire a new action handler that calls `round_driver.execute()`. The action becomes a thin adapter over the same core. | **Yes.** This is the "both" option: build the loop-over-runs core (round_driver, gates, ideation, report_types) as a library module, then expose it via the action route as a thin wrapper. The prototype *is* the end-state core; the action route is just the entry seam. |
| **F4-c: Start with Option 1 directly** | Zero migration (it's already the target). | Yes, but higher initial cost — must build the async action semantic *and* the round driver simultaneously. |
| **F4-d: Hybrid — action route that is synchronous-but-resumable** | Low. The action route blocks (synchronous) but the round driver checkpoints after each round (via T6). On crash, the operator re-issues the same action with a `resume=ralph_run_id` parameter. | **Yes.** This is F4-b refined: the action is synchronous (simple), but resumable (robust). The operator experience is "run once, if it crashes, run again with --resume." No async polling needed. |

**Frame insight:** The decision is *not* "Option 1 vs Option 2." It's "which seam first." If the `round_driver` core is built as a library with a clean seam, both options are the same code with different entry points. **F4-d (synchronous-but-resumable action) is the option that is both prototype and end-state**: it has the simplicity of a blocking call (prototype-friendly) and the crash-resilience of N independent runs (end-state quality).

**Pattern-store migration dimension (owner amendment):** The `ralph_patterns` table is a *shared dependency* of every issue surface — whoever writes a pattern must do so through the engine's approval gate. This reframes the migration question: it's not just "which seam first" but "which seam keeps the pattern store inside the engine's trust boundary from day one."

| Option | Migration cost (revised) | Pattern-store implication |
|--------|--------------------------|---------------------------|
| **F4-a: Option 2 → Option 1** | Medium → **Low.** The `ralph_patterns` table is engine.db-resident regardless of seam. Migration is purely the entry seam; the pattern store doesn't move. | ✅ No pattern-store rework at migration time. |
| **F4-b: Option 1 thin-wrapper over Option 2 core** | Low. Same as before, but now the pattern store is *inside* the engine, so the "core" includes `pattern_gate.py` writing to `ralph_patterns` via a DB interface. | ✅ Pattern store is engine-native from the start. |
| **F4-d: Synchronous-but-resumable action** | Low. The action handler calls `round_driver.execute()`, which calls `pattern_gate.persist_pattern()`, which writes to `ralph_patterns`. All inside the engine. | ✅ **Best.** The pattern store is never outside the engine's trust boundary, so there is zero migration cost for patterns. |

**Revised F4 insight:** The owner's amendment makes the "prototype vs. end-state" question *easier*, not harder. Because `ralph_patterns` is engine.db-resident and seam-agnostic, it is a stable dependency that all surfaces share. The migration cost is *only* the entry seam — and the action seam is the cheapest because it keeps pattern writes inside the engine. F4-d remains the recommended path, now with the additional advantage that the pattern store is correct-by-construction from the first prototype.

---

### F5 — Failure Frame: What Happens When a Round Crashes Mid-Run?

| Option | Crash behavior | Orphaned rounds | Budget correlation |
|--------|---------------|-----------------|-------------------|
| **F5-a: Single long-running process (F2-a)** | The entire Ralph run dies. The `ralph_rounds` table has a row for the crashed round with state="running." On resume, the orchestrator finds the last completed round and restarts from N+1. | The crashed round's ACE run (if mid-`Engine.run()`) may have left a partial commit. T6 `reconcile_git_state()` handles orphan commits on resume. | All rounds share one `run_id` and one T8 session. Budget is trivially correlated. A crashed round *consumed* budget that is lost (the LLM calls happened but produced no RoundReport). |
| **F5-b: N independent runs (F2-b)** | Only the crashed round dies. The orchestrator detects the failure (non-zero exit code, missing RoundReport) and can retry or abort. | Each round is one ACE run with its own `run_id`. Orphaned rounds are individual `engine_runs` rows with state=FAILED. The `ralph_rounds` table correlates them via `ralph_run_id`. | Each round has its own T8 session. The orchestrator sums `llm_calls` across `ralph_rounds` rows. A crashed round's budget is *in* its session row (T8 counters are persisted atomically per-call, session.py line 145-163) — the tally survives the crash. |
| **F5-c: Async action with polling (F2-c)** | The action process dies. The `ralph_run_id` persists in a new `ralph_runs` table. On resume, the operator re-attaches to the same `ralph_run_id`. | Same as F5-b but with a single top-level `ralph_run_id` for the whole execution. | Same as F5-b. The action process maintains a cumulative budget check between rounds. |
| **F5-d: External harness (F2-e)** | The harness dies. ACE has no knowledge of the Ralph run. The harness must implement its own crash recovery. | ACE sees N independent runs with no grouping. The harness must correlate them externally. | The harness must sum ACE's per-run budget via telemetry queries. | 

**Frame insight:** F5-b (N independent runs) has the *best* crash behavior: each round is isolated, budget is persisted per-round, and the `ralph_rounds` table provides correlation. F5-a (single process) has the *worst* crash behavior. This is S2's strongest argument for Option 2, and it holds up under scrutiny. **But** F5-c (async action) achieves the same isolation *with* a single `ralph_run_id` — it's strictly better. The cost is the async action semantic.

---

### F6 — Observability Frame: How Does the Operator Understand What Happened?

(S2 underweighted this. A Ralph run is a *workflow*, not a task. The operator needs workflow-level visibility.)

| Option | Observability | Trade-offs |
|--------|--------------|------------|
| **F6-a: Action route** | The action emits events via `emit_event()` (engine.py line 14). Ralph-specific events (`ralph_round_start`, `ralph_round_complete`, `ralph_ideation_collapse`) can be projected to the escalation JSONL (D26 handler, engine.py line 81-82). The operator reads one JSONL file for the whole Ralph run. | ✅ Reuses existing event infrastructure. ✅ Single `ralph_run_id` = single JSONL file. ❌ Requires new event types in the events schema. |
| **F6-b: Loop-over-runs** | Each round emits its own events under its own `run_id`. The operator must grep across N JSONL files to reconstruct the Ralph run timeline. | ❌ Fragmented observability. ❌ The `ralph_rounds` table provides correlation but not a unified timeline. |
| **F6-c: Round ledger (pre-epic §5 suggestion)** | A new `ralph_rounds` table (S2 §7) persists each round as a typed row. A `ralph_status(ralph_run_id)` query returns the full timeline. | ✅ Structured, queryable, replayable. ✅ Enables G11 (round churn) and G12 (ideation collapse) signals. ❌ New table = new schema migration (but additive-only, so safe per state.py line 162-166). |

**Frame insight:** F6-c (round ledger) is *orthogonal* to the issue surface — it's needed regardless of Option 1 or 2. But F6-a (action route) gives better *event* observability because the single `ralph_run_id` means a single JSONL file. The round ledger + action route together give the best of both: structured table for queries, JSONL for human-readable timeline.

---

## Cross-Pollination

### The round_driver core is identical under all surfaces

The key insight from F4: `round_driver.py`, `gates.py`, `ideation.py`, `report_types.py`, and `pattern_gate.py` are *the same code* whether the entry point is an action route, a CLI command, or an outer orchestrator. The only difference is the **entry seam**:

| Entry seam | How it calls round_driver |
|------------|--------------------------|
| Action route | `run_ralph` action → `round_driver.execute(objective, config)` |
| CLI | `cli.py ralph` → `round_driver.execute(objective, config)` |
| Outer orchestrator | `orchestrator.start_round()` → `round_driver.execute(objective, config)` |

This reframes the decision: **the issue surface is a seam choice, not an architectural choice.** The architecture (round_driver + gates + ideation + report) is stable.

### Hybrid: Synchronous-but-resumable action (F4-d)

The recommended hybrid:
1. `run_ralph` is added to `ace_actions.json` (Option 1's seam).
2. The action handler calls `round_driver.execute()` *synchronously* (blocks until done).
3. After each round, the round driver persists a `ralph_rounds` row with the `workspace_sha` and cumulative budget (Option 2's checkpointing).
4. On crash, the operator re-issues `run_ralph` with `resume=ralph_run_id`. The round driver reads the `ralph_rounds` table, finds the last completed round, and continues from N+1.

This is **Option 1's seam + Option 2's crash resilience + no new async semantic**. The action is synchronous (simple), but the round driver checkpoints (resumable).

### Pattern-store exposure as a seam filter (owner amendment)

The owner's amendment adds a clean evaluation filter: **which issue surfaces let external consumers query `ralph_patterns` without bypassing the approval gate?**

- **Action route (recommended):** Patterns are written by engine code (`pattern_gate.py` → `ralph_patterns` table) and read by external consumers via `pattern_query.py` or direct engine.db query. The approval gate (`proposed→approved`) is an engine-enforced SQL transition. External consumers *cannot* read `proposed` rows because the query interface filters `status='approved'` by default. The trust boundary is the engine.db file itself — same boundary as `orchestrator_session`, `engine_runs`, etc. Clean.
- **Loop-over-runs:** The outer orchestrator must write patterns. If it's external, it either writes directly to engine.db (violating engine locality — now *its* table is being written by *their* code) or maintains a separate store and syncs (two sources of truth). Neither is clean. The owner's amendment *closes* this door: the canonical store is engine.db-resident, so the writer must be engine-internal.
- **External harness:** Patterns live outside engine.db. Rejected by pre-epic and now doubly rejected by the owner's amendment.

**Conclusion:** The pattern-store amendment reinforces the action route recommendation. It does not change the recommendation, but it *does* remove the loop-over-runs option's last plausible advantage (simplicity of being "outside the engine"). With `ralph_patterns` inside engine.db, "outside the engine" means "outside the trust boundary," which is a non-starter.

### Trigger.py integration (F2-d revisited)

The T2 trigger (`trigger.py`) fires a re-plan when a task fails repeatedly. Ralph rounds are *not* task failures — they are *iterations*. The trigger's budget semantics (`max_retries_generate`) don't map to `max_ralph_rounds`. **Trigger.py is the wrong integration point.** The Ralph loop is a *workflow*, not a *retry*. This confirms S2's "workflow over states" decision (Option A).

---

## Scoring

Scoring: Impact × Feasibility × Alignment (1–5 each). Total = product.

| Option | Impact | Feasibility | Alignment | Total | Notes |
|--------|--------|-------------|-----------|-------|-------|
| **Option 1: Action route (direct)** | 5 — first-class, single run_id, best observability | 2 — requires async action semantic + new event types | 5 — perfect ADR-0002/0003 fit | **50** | End-state target, high initial cost |
| **Option 2: Loop-over-runs** | 3 → **2** — fragmented run_ids, weaker fresh-agent enforcement, **and now pattern-store trust-boundary violation (owner amendment)** | 5 — zero engine hot-path changes | 4 → **2** — good ADR-0003 fit, but budget carry-across is external **and ralph_patterns must be written by external orchestrator** | **20 → 60** → **20** | Owner amendment *lowers* alignment: external orchestrator cannot cleanly write engine.db-resident patterns |
| **Option 1→2 migration (S2 rec)** | 4 — validates contract first | 4 — two-phase build | 4 → **2** — sequential risk **+ pattern-store boundary violation during Option 2 phase** | **64 → 32** | Owner amendment makes the "Option 2 first" phase a trust-boundary liability |
| **Option 1 thin-wrapper over Option 2 core (F4-b)** | 5 — first-class + resilient | 4 — build core first, then thin seam | 5 — best of both | **100** | **Recommended** |
| **Synchronous-but-resumable action (F4-d)** | 5 — first-class + resilient + simple | 4 — needs ralph_rounds table + resume param | 5 — reuses T5/T6/T8, **pattern-store is engine-native from day one** | **100** | **Recommended (refined)** |
| **Option 3: Lane-B primitive** | 2 — too narrow for full pipeline | 1 — cannot run generate→validate→test→commit | 3 — strong gates but wrong capability | **6** | Ruled out (S2 correct) |
| **Option 4: External harness** | 1 — status quo | 5 — zero engine changes | 1 — rejected by pre-epic | **5** | Ruled out |

---

## Convergence

### Decision

**Build the `round_driver` core as a library module (shared by all surfaces), expose it via a synchronous-but-resumable `run_ralph` action in `ace_actions.json`, and checkpoint after each round via the `ralph_rounds` table.**

### Rationale (≤5 bullets)

1. **The round_driver core is identical under all surfaces** (F4 cross-pollination). The decision is "which seam first," not "which architecture." Building the core as a library means no rework when adding the action route.
2. **Synchronous-but-resumable gives Option 2's crash resilience with Option 1's single-run_id simplicity** (F4-d). No new async semantic needed — the action blocks, but T6 checkpointing after each round makes it resumable via `resume=ralph_run_id`.
3. **The action route is the only surface that mechanically enforces the fresh-agent rule** (F3-a). Loop-over-runs relies on orchestrator convention; the action route controls prompt construction in engine code. This is S1 gap #1 (gate enforcement) applied to the issue surface.
4. **The `ralph_rounds` table is needed regardless** (F6-c). It provides round correlation, budget carry-across, G11/G12 signals, and resume state. It's additive-only (state.py line 162-166), so zero migration risk.
5. **Lane-B and external harness are correctly ruled out** (S2's rejections stand). Lane-B is too narrow (F3-c); external harness is rejected by the pre-epic (F1-e).

### Where I agree with S2 §9

- **"Option 1 (ACE action route) is the end-state target."** ✅ Agreed. The action route is the only surface that mechanically enforces the fresh-agent rule and gives single-run_id observability.
- **"Option 2 (loop-over-runs) is the pragmatic first step."** ⚠️ Partially agreed. The *core* should be built first (round_driver, gates, ideation, report_types), but the *seam* should be the action route from day one — not a separate loop-over-runs orchestrator that must later be migrated. The migration cost is in the seam, not the core.

### Where I disagree with S2 §9

- **S2 frames the decision as "Option 2 now, Option 1 later."** I disagree. The decision is "build the core once, expose via action route immediately." The action route is a thin wrapper over the same `round_driver.execute()` that a loop-over-runs orchestrator would call. There is no "migration" — there is one core with two seams, and the action seam is the primary one.
- **S2 underweights the observability frame (F6).** The round ledger + single JSONL file (action route) is a strict improvement over N fragmented JSONL files (loop-over-runs). This pushes toward the action route *earlier*, not later.
- **S2's Option 2 has a hidden cost: the outer orchestrator must reimplement budget carry-across.** S2 §7 proposes a `ralph_rounds` table for this — but that table is *exactly* what the action route needs too. Building the table for Option 2 and then "migrating" to Option 1 means building it once anyway. Build it once, under the action route.
- **S2 §6's pattern-storage decision (markdown skill-docs as canonical store) is partially overruled by the owner.** The canonical store is now `ralph_patterns` in engine.db. This *strengthens* the action route recommendation: with the pattern store engine.db-resident, the issue surface must write patterns through the engine's trust boundary. The loop-over-runs approach forces an external orchestrator to either write engine.db directly (violating locality) or maintain a parallel store (violating single-source-of-trust). The owner's amendment thus *removes* Option 2's last plausible advantage.

### What this commits S4's epic to

- `engine/workflows/ralph/` package with `round_driver.py`, `gates.py`, `ideation.py`, `report_types.py`, `pattern_gate.py`, `config.py` (S2's module map stands).
- `run_ralph` action in `routes/ace_actions.json` with `objective`, `max_rounds`, `resume_id` parameters.
- `ralph_rounds` and `ralph_runs` tables in engine.db (additive-only).
- Resume semantics: `run_ralph(objective, resume_id=ralph_run_id)` continues from the last completed round.
- New event types: `ralph_round_start`, `ralph_round_complete`, `ralph_round_failed`, `ralph_complete`.
- No new async action semantic (synchronous-but-resumable via T6 checkpointing).

---

## Hand-off to S4

S4's epic must encode these decisions:

1. **Issue surface:** `run_ralph` action in `routes/ace_actions.json`, dispatched via the existing action handler. Synchronous (blocks), but resumable via `resume=ralph_run_id` parameter.
2. **Prototype stages:** (a) Build `round_driver` core + `gates.py` + `report_types.py` as library modules with a CLI seam (`cli.py ralph --objective`). (b) Add `run_ralph` to `ace_actions.json` as a thin wrapper over the same core. (c) Add `resume` parameter + `ralph_rounds` table for crash resilience. (d) Add `ideation.py` (divergent ideation). (e) Add `pattern_gate.py` (ADR-0002 pattern registry) — writing to the `ralph_patterns` table (see item 8 below).
3. **Acceptance criteria implications:**
   - A Ralph run that crashes at round 3 resumes at round 3 (not round 1) — verified by `resume_ralph()` test.
   - The fresh-agent rule is mechanically enforced: round N+1's prompt contains ONLY the RoundReport + pinned workspace + objective — verified by a prompt-audit test.
   - T8 budget is enforced across rounds: cumulative `llm_calls` across all rounds in a `ralph_run_id` cannot exceed `EngineConfig.max_llm_calls` — verified by a budget-exhaustion test.
   - The `ralph_rounds` table correlates all rounds under one `ralph_run_id` — verified by a query test.
   - Pattern proposals require approval before injection (ADR-0002) — verified by a pattern-gate test.
4. **ADR-0003 update:** The ADR draft in S2 §8 should be updated to specify the *seam* (action route) in addition to the *architecture* (workflow over states).
5. **Non-goals:** No recursive Ralph-for-ACE (F1-d ruled out by pre-epic §4). No Lane-B primitive (F3-c ruled out). No external harness (F1-e ruled out by pre-epic).
6. **Budget integration:** The `ralph_rounds` table schema from S2 §7 (lines 496-507) is adopted with one addition: a `ralph_run_id` column is the top-level grouping key (one Ralph execution = one `ralph_run_id` = N rounds). The action route creates this ID at dispatch time.
7. **Event schema:** New Ralph-specific event types must be added to the events schema before S8 (ACE-execute). The D26 escalation JSONL handler (engine.py line 81-82) already projects events to per-run JSONL files — Ralph gets this for free once events are emitted.
8. **`ralph_patterns` schema + query interface (first-class deliverable, owner amendment):** The epic MUST define the `ralph_patterns` table as a first-class deliverable, not an afterthought. Canonical store in engine.db (additive-only, WAL). Proposed schema:
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
   **Query interface:** A `pattern_query.py` module (replaces the skill's `query.py`) exposes `query_patterns(project_namespace, keywords, status='approved', limit=10) -> list[PatternRow]` for external consumers (other agents, dashboards, future tooling). This is the programmatic access point the owner requires — markdown export is a *projection* of approved rows, never the query path.
   **Approval gate (medium-independent, ADR-0002):** `pattern_gate.persist_pattern()` writes `status='proposed'`. A separate `pattern_gate.approve_pattern(pattern_id, approval_token)` transitions `proposed→approved` — this is the mechanical gate. Prompt injection reads `status='approved'` rows only. The gate is the same mechanical transition regardless of whether the canonical store is SQL or markdown; the owner's amendment changes the *medium*, not the *gate*.
   **Markdown export (G9 seed):** A `pattern_export.py` module projects `status='approved'` rows to markdown skill-docs in the skill registry (`engine/intent/skills/`). This is a one-way export, regenerated on approval, never read back as source of truth.
