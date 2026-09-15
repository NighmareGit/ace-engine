# S6 RED-TEAM: Adversarial Pass over the Ralph Loop Epic

**Phase:** S6 REFINE (red-team)
**Date:** 2026-09-09
**Input epic:** `EPIC.md` (S5-grill-resolved)
**Scope:** Analysis only. No code changes.

---

## Findings

### F1 — Round-boundary leak: workspace files are an uncontrolled channel (Medium)

**What.** R7 enforces that the *LLM prompt* for round N+1 contains only RoundReport + pinned workspace + objective. The prompt-audit test (R7 testable criterion) inspects the *constructed prompt string*. But the round also hands the next round a full git checkout at `workspace_sha` — which includes every file the previous round wrote: `.scratch/`, `run/<run_id>/` escalation JSONL, `engine_run_*_escalations.jsonl`, `__pycache__/`, `.pytest_cache/`, IDE files, etc. The next round's LLM can `read` or `glob` those files. The fresh-agent rule is enforced on the prompt, not on the workspace contents.

**Evidence.**
- Epic §R7: "Round N+1's LLM prompt is built from ONLY the RoundReport … + pinned workspace at `workspace_sha` + the original objective." The workspace is the *entire* git tree, not a curated subset.
- `engine/engine.py:80-82` — the escalation JSONL handler writes `engine_run_<run_id>_escalations.jsonl` into `project_path/run/<run_id>/`. That file is committed by the committer (it lives in the project dir) and will be in `workspace_sha`.
- `engine/archive.py:76-78` — `report.json` is written under the project's run dir. Same leakage path.
- S5 grill resolution #6 explicitly accepted cross-round prompt injection *through the RoundReport* ("both LLMs share the engine trust level"). But the workspace-files channel is a *different* surface: it carries the previous round's full escalation trace, judge reasoning, error context — exactly the "conversation carry-over" the fresh-agent rule exists to prevent.

**Verdict: feature or leak?** It is a *controlled feature* that is currently *uncontrolled*. The workspace is legitimately the "pinned state" the next round builds on (the epic's intent). But the epic does not define what the round driver must *strip* from the workspace before handing it on. Without a denylist, the next round sees the prior round's escalation JSONL, judge reasoning, and any `.scratch/` artifacts — a soft channel that bypasses the R7 prompt-audit test.

**Concrete epic change required.** Add to the epic (Scope or R7 clause): "The round driver maintains a workspace denylist. Before computing `workspace_sha`, the driver `git rm -rf`s or `git checkout`s clean: `run/<run_id>/`, `.scratch/`, `__pycache__/`, `.pytest_cache/`, and any `engine_run_*_escalations.jsonl`. The denylist is a `RalphConfig` field (default: the list above). The R7 prompt-audit test is extended to assert that a glob of the pinned workspace at `workspace_sha` contains no denylisted paths." Alternatively, document this as an explicit accepted risk with a rationale — but it must be a *decision*, not an oversight.

---

### F2 — RoundReport is the only *structured* channel, but the workspace is an unaudited *unstructured* channel — and the epic's threat model is silent on the gap (Medium)

**What.** F1's denillist mitigates file-based leakage, but there is a deeper structural issue: the epic defines RoundReport as "the ONLY structured data that crosses the round boundary" (redesign-s2.md §3, line 138), yet the workspace checkout is *also* data crossing the boundary, and it is not structured, not schema-validated, and not size-bounded. The 64KB RoundReport bound (epic §RoundReport schema) is meaningless if the workspace carries 50MB of prior-round artifacts.

**Evidence.**
- redesign-s2.md §3 line 138: "The round report is the ONLY structured data that crosses the round boundary."
- Epic §RoundReport schema: "Total JSON ≤64KB serialized" — bounds the report, not the workspace.
- `engine/archive.py:84-86` — the archive snapshots `engine.db` (potentially large) but this is post-run, not per-round. However, the *project* workspace can accumulate test artifacts, generated files, etc.

**Concrete epic change required.** Add a workspace-size budget to `RalphConfig` (e.g., `max_workspace_size_mb` or `max_workspace_file_count`). The round driver checks workspace size before computing `workspace_sha`; if exceeded, it fails the round with a clear error rather than silently passing a bloated context to the next round. This is the workspace analog of the 64KB report bound.

---

### F3 — Gate gaming via approved-pattern injection is a real, novel attack surface (High)

**What.** The S5 grill resolution #6 accepted cross-round prompt injection through the RoundReport ("both LLMs share the engine trust level; no content-level sanitization"). But there is a *second* injection path the grill did not examine: the `ralph_patterns` table. Approved patterns are injected into the subject model's prompt (this is the entire point of the pattern store — redesign-s2.md §2 `pattern_gate.py`). A subject model that learns what the 35B judge rewards (via the patterns that *got approved*) can craft future code that satisfies the judge's rubric without meeting the objective. Worse: the subject model can *propose patterns* (via `propose_pattern_update`) that, once approved, steer *future rounds and future runs* toward judge-gaming strategies.

**Attack chain:**
1. Round N's subject model generates code that happens to score well on the 35B judge (e.g., by including keywords the judge's rubric rewards: "error handling", "type hints", "docstrings").
2. Round N's `pattern_gate.py` extracts a pattern proposal like "Including comprehensive error handling and type hints improves Review gate scores."
3. The owner approves it (the approval gate is a human judgment call on a plausible-sounding pattern — ADR-0002's "necessary but not sufficient" caveat, epic §Risks).
4. Round N+1's subject model receives the approved pattern as injected context.
5. Round N+1's subject model writes code that maximizes the pattern (error handling, type hints) at the expense of the actual objective — gaming the Review gate.
6. Over multiple rounds, the pattern store accumulates judge-gaming patterns that converge on "what the 35B judge rewards" rather than "what achieves the objective."

**Evidence.**
- Epic §R2: "only `approved` rows are readable by the query interface" — approved patterns *are* injected into prompts.
- Epic §pattern_gate.py interface: `propose_pattern_update(round_report, skill_store)` — the subject model proposes patterns based on what it observed in the round.
- Epic §Risks "Pattern-library semantic poisoning" (LOW-MEDIUM): "Even with the approval gate, a human could approve a bad pattern." The epic frames this as a *quality* problem. It is actually a *gate-gaming* problem: the subject model can systematically propose patterns that game the judge, and the human approver sees only the pattern text, not its systemic effect on gate outcomes.
- `engine/llm_judge.py:63-89` — the judge prompt is deterministic and stable (same rubric every time). A subject model that learns the rubric (via approved patterns that describe what scores well) can optimize for it.
- S5 grill resolution #3: patterns stay `proposed` and require human approval. But the human approver has no tooling to see "this pattern, combined with existing approved patterns, creates a gate-gaming surface."

**Why the grill missed it.** The grill treated pattern approval as an ADR-0002 compliance question (is the gate mechanical?) rather than an *adversarial dynamics* question (what happens when the subject model systematically proposes patterns that game the judge?). ADR-0002's threat model is "unvalidated content auto-propagating" — it does not address "validated content that systematically biases gate outcomes."

**Concrete epic change required.** Add to the epic:
1. A `pattern_proposal_rubric` that scores proposals on *objective relevance*, not just plausibility. The approval CLI (`cli.py ralph approve-pattern`) should show the proposal's similarity to existing approved patterns (TF-IDF cosine, reusing `engine/intent/similarity.py`) so the owner can see convergence.
2. A `max_patterns_per_run` cap (currently unbounded — the schema allows unlimited `proposed` rows per run).
3. A gate-gaming detection signal: if the Review/RedTeam gate pass rate increases while objective-completion rate (from RoundReport `state`) decreases across rounds, emit a `ralph_gate_gaming_suspected` event. This is the G12-analog for gate integrity.
4. Elevate "Pattern-library semantic poisoning" from LOW-MEDIUM to HIGH in the risk table, with the gate-gaming attack chain as the rationale.

---

### F4 — Budget: a single round can consume the entire cumulative cap (Medium)

**What.** Epic §Budget behavior: "Round driver sums `llm_calls` + `total_tokens` across all prior rounds in the `ralph_run_id`; checks against `EngineConfig.max_llm_calls` / `max_total_tokens` before each round." The check is *between* rounds. Within a round, the only bound is the per-run T8 cap (`EngineConfig.max_llm_calls` for that ACE run's `run_id`). If the cumulative cap is, say, 1000 calls, and rounds 1-4 each use 200, the cumulative is 800. Round 5 is allowed to start (800 < 1000) but can consume up to its *per-run* cap of 1000 calls — blowing the cumulative total to 1800. The epic's R3 testable criterion says "no single round can exceed per-run caps" but does not say "no single round can exceed the *remaining* cumulative budget."

**Evidence.**
- Epic §R3: "A budget-exhaustion test confirms the loop terminates when cumulative caps are hit, and no single round can exceed per-run caps." — The second clause is the *per-run* cap, not the *cumulative* cap.
- `engine/orchestrator/budget.py:29-33` — `check_budget` compares counters against caps. The counters are per-`run_id`. The round driver sums across `ralph_rounds` rows for the cumulative check, but the *in-round* budget enforcement uses the per-run session counters.
- Epic §Risks "Mid-round budget exhaustion" (MEDIUM): "A round that exhausts T8 budget mid-execution … leaves no RoundReport." This describes the *symptom* (mid-round exhaustion) but not the *structural* issue (a round can exceed the cumulative cap because the cumulative check is only between rounds).

**Is it acceptable?** For a first version, *conditionally yes* — if per-run caps are set tight enough that one round cannot blow past the cumulative cap. But the epic does not state this constraint, and the R3 testable criterion is weaker than it should be. An operator who sets `max_llm_calls=1000` (per-run) and `max_ralph_rounds=5` expecting a 5000-call cumulative budget will instead get a budget that can be exhausted in *two* rounds (each consuming up to 1000, the cumulative check only fires *between* rounds).

**Concrete epic change required.** Add to the epic: "The round driver computes `remaining_cumulative = max_llm_calls - sum(prior rounds)` before each round. The per-round T8 cap for the ACE run dispatched in that round is set to `min(config.max_llm_calls, remaining_cumulative)`. This ensures no single round can exceed the cumulative cap." Update R3 testable criterion accordingly.

---

### F5 — Ideation tokens: inside or outside the round budget? (Low-Medium)

**What.** Epic §Risks "Ideation budget blowup" (LOW): "Generating 8 candidates × 2048 tokens = 16K tokens/round on ideation alone. Mitigation: hard cap of 16K tokens per round for ideation; counts against T8." The epic says ideation "counts against T8" but does not specify *which* T8 counters. Ideation happens *before* the ACE run is dispatched (it's part of the round driver's outer loop, not inside `Engine.run()`). The ACE run's T8 counters are created by `sess.create_session(run_id, …)` inside `Engine.run()` (engine/engine.py:111). Ideation tokens are consumed *before* that session exists.

**Evidence.**
- redesign-s2.md §5 line 437-442: ideation calls `budget.increment_and_check(run_id, tokens=candidate.tokens)` — but the `run_id` here is the *ACE run's* run_id, which does not exist yet at ideation time (the round driver hasn't dispatched the run).
- Epic §Budget behavior: "Round driver sums `llm_calls` + `total_tokens` across all prior rounds in the `ralph_run_id`" — this implies the round driver maintains its *own* cumulative counters, not the per-run T8 session counters.
- `engine/orchestrator/session.py:145-163` — `increment_llm_calls` operates on `orchestrator_session` rows keyed by `run_id`. If the round driver calls this before `create_session`, it will create a session row with no task IDs and no cap configuration — the caps default to 0 (unbounded), so `check_budget` will never fire.

**Concrete epic change required.** The epic must specify: (a) the round driver maintains its *own* cumulative token/call counters in the `ralph_rounds` table (not the per-run `orchestrator_session`); (b) ideation tokens are added to the round driver's cumulative counters, not the ACE run's T8 session; (c) the round driver checks its *own* cumulative counters before dispatching ideation and before dispatching the ACE run. Add a `ralph_cumulative_tokens` / `ralph_cumulative_llm_calls` field to the round driver's in-memory state, persisted per-round in `ralph_rounds`.

---

### F6 — Resume: synchronous-but-resumable is not operator-usable without a status surface (Medium)

**What.** Epic §F4-d: "On crash, the operator re-issues `run_ralph` with `resume=ralph_run_id`." The action is synchronous — it blocks until done. If the DSH session dies mid-action (SIGKILL, OOM, network drop), the operator has no way to discover *which* `ralph_run_id` was in progress, *which* round it was on, or *what the last workspace_sha was* without manually querying engine.db. The `ralph_rounds` table has the data, but there is no CLI or API surface to read it.

**Evidence.**
- Epic §Scope: `ralph_rounds` table has `ralph_run_id`, `round_id`, `state`, `workspace_sha`. The data is there.
- Epic §Deliverables Stage (c): "`resume=ralph_run_id` parameter: continues from last completed round." — describes the *mechanism*, not the *operator experience*.
- `engine/engine.py:977-1016` — `Engine.status(run_id)` exists for ACE runs but returns `RunStatus` for a single run, not a Ralph run. There is no `RalphStatus` query.
- S5 grill resolution #4: "Resumable from `failed`/`cancelled` only (`completed` is terminal). … Concurrency: `BEGIN IMMEDIATE` on the `ralph_runs` row → `RalphRunBusyError`." — This handles concurrency but not *discoverability*.

**Concrete epic change required.** Add to the epic a `cli.py ralph status <ralph_run_id>` command that queries `ralph_rounds` and returns: current round, last completed round, cumulative budget, last workspace_sha, and a `resumable` boolean. This is the operator's crash-recovery UI. Without it, resume is a theoretical capability that requires an engineer to hand-query SQLite.

---

### F7 — Stop-file naming/cleanup across ralph runs (Low)

**What.** `engine/orchestrator/stop.py:27`: the stop-file path is `/tmp/.ace-stop-{run_id}`. For a Ralph run, each round dispatches a *different* ACE run with a *different* `run_id`. The round driver checks `stop.check_stop_file(run_id)` — but which `run_id`? The Ralph run's `ralph_run_id` or the current round's ACE `run_id`? If the operator creates `/tmp/.ace-stop-run-1234567890` to stop round 2, the round driver (checking the Ralph run's ID) won't see it. If the round driver checks the ACE run's ID, a stop-file from round 1 (never cleaned up) will immediately cancel round 2.

**Evidence.**
- `engine/orchestrator/stop.py:27`: `_stop_file_path(run_id)` uses the `run_id` passed to it.
- Epic §Budget/stop/resume: "Round driver checks `stop.check_stop_file(run_id)` before each round and between gate evaluations." — ambiguous which `run_id`.
- `engine/orchestrator/stop.py:50-56`: `clear_stop_file` removes both per-run and global. But the per-run path is derived from a single `run_id`.

**Concrete epic change required.** Specify in the epic: the round driver checks the *global* stop-file (`/tmp/.ace-stop`) for operator-initiated cancellation, and a *Ralph-specific* stop-file (`/tmp/.ace-stop-ralph-{ralph_run_id}`) for Ralph-run cancellation. Per-round ACE run IDs are not exposed to the operator for stop-file purposes. Add `ACE_STOP_RALPH_FILE` env var override (parallel to `ACE_STOP_FILE`).

---

### F8 — Events wiring: ralph_* events are declared but the escalation JSONL handler doesn't project them (Medium)

**What.** Epic §In-scope engine changes: "Ralph event types: `ralph_round_start`, `ralph_round_complete`, `ralph_round_failed`, `ralph_complete`." These are declared as in-scope. But `engine/adapters/escalation_jsonl.py:30-44` has a hardcoded `_TIER_TRANSITIONS` dict that only recognizes `replan_exhausted`, `budget_abort`, and `oracle_escalation`. The new Ralph events will be *emitted* (via `emit_event`) but *not projected* to the escalation JSONL — they silently vanish in the handler (the `transition is not None` guard on line 63 filters them out). This is correct behavior for *escalation* JSONL (Ralph events aren't escalations), but it means there is *no* JSONL projection for Ralph events at all. The operator gets a single JSONL file for the ACE run's escalations, but nothing for the Ralph run's round lifecycle.

**Evidence.**
- `engine/adapters/escalation_jsonl.py:30-44`: `_TIER_TRANSITIONS` does not include any `ralph_*` events.
- `engine/engine.py:80-82`: the `EscalationJsonlHandler` is wired in `Engine.run()` with `run_dir = project_path/run/<run_id>`. For a Ralph run, each round has a *different* `run_id`, so each round gets a *different* JSONL file. There is no Ralph-run-level JSONL.
- Epic §F6-a (fireplace): "The action emits events via `emit_event()`. Ralph-specific events … can be projected to the escalation JSONL." — This is *not implemented* by the current handler and requires a new projection path.

**Concrete epic change required.** Add to the epic: a `RalphJsonlHandler` (analogous to `EscalationJsonlHandler`) that projects `ralph_*` events to `ralph_run_<ralph_run_id>_rounds.jsonl` in the project's `run/` directory. The handler is instantiated by the round driver (not by `Engine.run()`), so it persists across rounds. This gives the operator a single timeline file for the entire Ralph run.

---

### F9 — report.json for ralph runs: not in scope, but autopsies need it (Medium)

**What.** `engine/orchestrator/report_builder.py` builds a `RunReport` and `engine/orchestrator/report_persistence.py` persists it as `report.json` in the archive. This happens inside `Engine.run()` (engine/engine.py:201-226). For a Ralph run, each *round* produces its own `report.json` (one per ACE run). There is no *Ralph-run-level* report that aggregates all rounds. The Archive (`archive.py`) snapshots `engine.db` which contains `ralph_rounds` rows, but the operator has no single `ralph_report.json` showing the full Ralph execution (rounds, ideation summaries, pattern proposals, gate verdicts, cumulative budget).

**Evidence.**
- `engine/archive.py:76-78`: `report.json` is written per `run_id`. For Ralph, each round's ACE run has a different `run_id`.
- Epic §Deliverables: stages (a)-(e) do not include a Ralph-run-level report.
- CONTEXT.md §Archive: "The archive is the source of truth for autopsies." — but autopsies of a Ralph run currently require correlating N `report.json` files + N JSONL files + the `ralph_rounds` table.

**Concrete epic change required.** Add to the epic: a `RalphResult.to_report_dict()` method that produces a Ralph-run-level report (objective, rounds summary, cumulative budget, pattern proposals, terminal state, final workspace_sha). The round driver writes this to `project_path/run/<ralph_run_id>/ralph_report.json` on completion. This is the Ralph analog of `report.json` and the primary input for Ralph autopsies.

---

### F10 — TGD signal wiring for G11/G12: G12 is not a TGD signal, and G11 is not wired into Ralph (Low-Medium)

**What.** The epic mentions G12 (ideation collapse) as a wired signal (S5 grill resolution #8, epic §Metrics). G11 (external reference failure) is a TGD signal in `toolgap_detector.py:621-661`. Neither is wired into the Ralph workflow:
- G12 is computed by the round driver (TF-IDF similarity of selected plans) but is not emitted as a TGD finding — it's a Ralph-internal signal.
- G11 (sandbox egress failures) is detected by `scan_run()` but only as a post-hoc scan. A Ralph round that generates code requiring egress will fail the Dev/Test gate, but the round driver won't know *why* (no G11 signal fed back into the round's context for the next round).

**Evidence.**
- `engine/orchestrator/toolgap_detector.py:621-661`: G11 scans `engine_task_results.error_message` for URL/package patterns. This runs post-run via `scan_run(run_id)` (engine/engine.py:232-238). The round driver does not consume G11 findings.
- S5 grill resolution #8: G12 is "TF-IDF cosine similarity over selected plan text" computed by the round driver. It is not a TGD signal — it has no `Finding` object, no `GapReport`, no persistence.
- Epic §Metrics: "G12 ideation-collapse detection: Wired." — but wired only as a round-driver-internal event, not as a TGD finding.

**Concrete epic change required.** Add to the epic:
1. G12 findings should be persisted to a `ralph_findings` table (or emitted as `ralph_ideation_collapse` events that the TGD can pick up in its next `scan_history()`).
2. The round driver should call `scan_run(round_run_id)` after each ACE run and consume G11 findings: if G11 fires, inject a "sandbox has no egress" note into the next round's RoundReport so the subject model doesn't retry the same failing approach.

---

### F11 — Escalation JSONL interplay: Ralph post-commit gate failures are not escalations (Low)

**What.** The escalation JSONL handler (`escalation_jsonl.py`) projects `replan_exhausted`, `budget_abort`, and `oracle_escalation` events. Ralph introduces a *new* terminal condition: post-commit gate failure → `git reset`. This is neither an escalation nor a pipeline event the handler recognizes. If the round driver emits a `ralph_round_failed` event with a `reason="redteam_gate_failed"`, the escalation JSONL handler will silently drop it (correct — it's not an escalation), but *no other handler picks it up either*. The operator has no record of *why* a round was reverted.

**Evidence.**
- `engine/adapters/escalation_jsonl.py:63-64`: `transition = _TIER_TRANSITIONS.get(event_type)` — returns `None` for `ralph_round_failed`, so no JSONL record is written.
- Epic §F6-a: "Ralph-specific events … can be projected to the escalation JSONL." — This is the fireplace's *recommendation*, not the epic's *commitment*. The epic's in-scope changes list "Ralph event types" but does not specify a projection handler.

**Concrete epic change required.** This is addressed by F8 (RalphJsonlHandler). Add a note to the epic that `ralph_round_failed` events carrying a `reason` field are projected to the Ralph JSONL, not the escalation JSONL.

---

### F12 — `git reset --hard` revert is destructive and the epic's safety argument is incomplete (High)

**What.** S5 grill resolution #2: "Round driver uses `git reset --hard <pre-round-sha>`." The epic claims `reconcile_git_state()` is "provably a no-op post-reset (it only pushes orphan local commits, never pulls/force-pushes)." This is *almost* correct but misses a critical case: if the round's ACE run pushed to the remote *before* the post-commit gate fails (which is the normal path — `commit_code` pushes on success, engine.py:849-855), the remote branch now contains the bad commit. `git reset --hard <pre-round-sha>` reverts the *local* branch, but the *remote* still has the bad commit. The next round's ACE run will `git push` to a remote that is *ahead* of local, causing a non-fast-forward rejection.

**Evidence.**
- `engine/engine.py:849-855`: `commit_code` is called with `cancel_check=_stopped`. On success, the commit is pushed to remote (via `commit_code` → committer).
- `engine/orchestrator/checkpoint.py:76-78`: `reconcile_git_state` pushes orphan commits. After a `git reset --hard`, the local branch is behind the remote (the remote has the bad commit). `reconcile_git_state` will see `local_sha != remote_sha` and attempt to push — but the push will be a non-fast-forward rejection because local diverged from remote.
- S5 grill resolution #2: "Remote feature branch retains the bad commit (acceptable; ephemeral)." — This is *not* acceptable if the next round's push fails because of it.

**Concrete epic change required.** Add to the epic: after `git reset --hard <pre-round-sha>`, the round driver must also force-push the reverted local state to the remote (`git push --force-with-lease origin <branch>`), OR the round driver must use `git revert <bad_sha>` (which creates a new commit that undoes the bad one, preserving remote history). The `git reset --hard` approach requires force-push; the `git revert` approach does not. The epic must specify which, and the force-push safety implications must be documented (force-push to a shared branch is a destructive operation). This is a **must-fix before S7**.

---

### F13 — `ralph_runs` table: schema is declared but no module owns it (Low)

**What.** Epic §In-scope engine changes lists a `ralph_runs` table ("Top-level grouping: one Ralph execution = one `ralph_run_id`"). The `ralph_patterns` schema has a `FOREIGN KEY (source_ralph_run_id) REFERENCES ralph_runs(ralph_run_id)`. But the epic's module list does not include a `ralph_runs` owner — `round_driver.py` is the implied creator, but the table schema is not specified (only `ralph_rounds` and `ralph_patterns` have full schemas). Without a schema, the FK constraint in `ralph_patterns` will fail at creation time.

**Evidence.**
- Epic §In-scope: "`ralph_runs` table | `engine.db` (new table) | Top-level grouping: one Ralph execution = one `ralph_run_id`" — no schema.
- Epic §`ralph_patterns` schema: `FOREIGN KEY (source_ralph_run_id) REFERENCES ralph_runs(ralph_run_id)` — requires `ralph_runs(ralph_run_id)` to exist.
- Epic §`ralph_rounds` schema: has `ralph_run_id TEXT NOT NULL` but no FK to `ralph_runs` (it should).

**Concrete epic change required.** Add a `ralph_runs` schema to the epic:
```sql
CREATE TABLE IF NOT EXISTS ralph_runs (
    ralph_run_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    project_path TEXT NOT NULL,
    config_json TEXT,                            -- serialized RalphConfig
    state TEXT NOT NULL DEFAULT 'running',       -- running | completed | failed | cancelled
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    final_workspace_sha TEXT
);
```
Add FK from `ralph_rounds.ralph_run_id` to `ralph_runs.ralph_run_id`.

---

### F14 — Concurrency guard uses `BEGIN IMMEDIATE` but SQLite WAL + Python sqlite3 module behavior is subtle (Low)

**What.** S5 grill resolution #4: "Concurrency: `BEGIN IMMEDIATE` on the `ralph_runs` row → `RalphRunBusyError` for a second invoker." The Python `sqlite3` module does not expose `BEGIN IMMEDIATE` directly — you set `isolation_level=None` and execute `BEGIN IMMEDIATE` as a raw SQL statement, or use `conn.execute("BEGIN IMMEDIATE")` with `isolation_level=""`. The existing codebase (`engine/state.py`) uses `busy_timeout` (WAL mode) but does not use `BEGIN IMMEDIATE` anywhere. The round driver will need a new DB access pattern.

**Evidence.**
- `engine/state.py`: uses `_get_conn()` which sets `busy_timeout=5000` and `journal_mode=WAL`. No `BEGIN IMMEDIATE` usage in the codebase.
- S5 grill resolution #4: assumes `BEGIN IMMEDIATE` is available. It is, but the connection must be in autocommit mode (`isolation_level=None`), which conflicts with the existing `_get_conn()` helper that uses the default isolation level.

**Concrete epic change required.** Add to the epic: the round driver uses a dedicated `BEGIN IMMEDIATE` transaction on the `ralph_runs` row for concurrency guarding, with a helper in `engine/state.py` (e.g., `_get_immediate_conn()`) that returns a connection with `isolation_level=None`. Document that this is a *new* DB access pattern, not a reuse of `_get_conn()`.

---

## Add-on features

| # | Feature | Value | Cost | Recommendation |
|---|---------|-------|------|----------------|
| 1 | **Ralph run autopsy reusing the Archive** — extend `archive.py` to snapshot `ralph_rounds` + `ralph_patterns` into a `ralph_archive/<ralph_run_id>/` dir with a `ralph_report.json`. Enables post-hoc forensic reads of Ralph runs without hand-querying SQLite. | HIGH — operators need this from day one; without it, debugging a 5-round Ralph run requires correlating N report.json files + N JSONL files. | LOW — the Archive already exists; this is a new archive *type*, not a new system. | **In epic** (as part of F9). |
| 2 | **G11 round-churn signal** — wire TGD G11 (external reference failures) into the RoundReport so the next round knows "the sandbox has no egress, don't retry." | MEDIUM — prevents repeated egress failures across rounds, saves budget. | LOW — `scan_run()` already exists; the round driver just needs to call it and consume findings. | **In epic** (as part of F10). |
| 3 | **Pattern TTL sweeper** — a `cli.py ralph sweep-patterns` command that transitions `approved` patterns to `expired` when their `expires_at` is past. Without this, patterns accumulate indefinitely. | LOW-MEDIUM — prevents pattern-store bloat; the `expires_at` column exists but nothing reads it. | LOW — a single SQL `UPDATE … WHERE expires_at < datetime('now')`. | **Ledger** (post-v1). |
| 4 | **Ralph dashboard surface** — a `cli.py ralph list` / `cli.py ralph show <ralph_run_id>` that renders the round ledger as a table (round, plan summary, gate verdict, budget, workspace_sha). | MEDIUM — operator UX for long-running Ralph runs. | MEDIUM — requires a new CLI module + table rendering. | **Ledger** (post-v1; F6 `status` is the minimal version). |
| 5 | **Ralph-for-ACE dogfooding (meta)** — use the Ralph loop to implement Ralph tickets (the pre-epic's F1-d, ruled out for v1). The pre-epic explicitly non-goals this. | HIGH long-term (compounds ACE's own development) but the pre-epic ruled it out for good reason (recursive budget, trust boundary). | HIGH — requires nested budget caps, recursive sandbox, and a solution to "who approves patterns for the Ralph-improving Ralph run." | **Explicitly out of scope** (pre-epic non-goal). Do not add to ledger for this epic. |

**Recommend top 2 for the epic:** #1 (Ralph autopsy/Archive integration) and #2 (G11 round-churn signal). Both are low-cost, high-value, and close gaps the epic's current stages omit. #3 and #4 go to the ledger. #5 is correctly non-goaled.

---

## Verdict

### Is the epic ready for S7 (to-prd/to-tickets)?

**No — not without the mandatory amendments below.** The epic's *architecture* (ADR-0003, F4-d, module map) is sound and the S5 grill resolutions are well-reasoned. But the *operational* surface (resume UX, budget semantics, git-revert safety, event projection, report aggregation) has gaps that implementation will hit in the first sprint. The epic is a strong S5.5, not a clean S7.

### Mandatory epic amendments (must-fix before S7)

| # | Finding | Amendment |
|---|---------|-----------|
| M1 | **F12 — git reset --hard is destructive** | Specify `git revert` (not `git reset --hard`) as the revert mechanism, OR specify `git push --force-with-lease` after reset. Document the force-push safety implication. This is the highest-severity finding. |
| M2 | **F4 — single round can exceed cumulative cap** | Add `remaining_cumulative = cap - sum(prior rounds)` logic; set per-round T8 cap to `min(config.cap, remaining_cumulative)`. Update R3 testable criterion. |
| M3 | **F3 — gate gaming via approved patterns** | Elevate pattern poisoning risk to HIGH. Add `max_patterns_per_run` cap. Add gate-gaming detection signal (Review pass rate vs. objective completion rate divergence). Add pattern-proposal similarity check to approval CLI. |
| M4 | **F6 — no operator-visible resume surface** | Add `cli.py ralph status <ralph_run_id>` command to the deliverables. |
| M5 | **F8/F11 — no Ralph event projection** | Add `RalphJsonlHandler` to the deliverables. Specify the JSONL path (`ralph_run_<id>_rounds.jsonl`). |
| M6 | **F9 — no Ralph-run-level report** | Add `RalphResult.to_report_dict()` and `ralph_report.json` to the deliverables. |
| M7 | **F13 — `ralph_runs` schema missing** | Add full `ralph_runs` schema with FK from `ralph_rounds`. |
| M8 | **F1 — workspace denillist** | Add workspace denillist to `RalphConfig` and extend R7 prompt-audit test to assert no denillisted paths in `workspace_sha`. |

### Optional amendments (ledger / post-v1)

| # | Finding | Amendment |
|---|---------|-----------|
| O1 | **F2 — workspace size budget** | Add `max_workspace_size_mb` to `RalphConfig`. |
| O2 | **F5 — ideation token accounting** | Specify round-driver-owned cumulative counters (distinct from per-run T8 session). |
| O3 | **F7 — stop-file naming** | Specify global + Ralph-specific stop-file paths. |
| O4 | **F10 — G12 as TGD signal** | Persist G12 findings to `ralph_findings` table; wire G11 into RoundReport. |
| O5 | **F14 — `BEGIN IMMEDIATE` DB access pattern** | Add `_get_immediate_conn()` helper to `engine/state.py`. |
| O6 | Add-on #3 (pattern TTL sweeper) | Ledger. |
| O7 | Add-on #4 (Ralph dashboard) | Ledger. |
