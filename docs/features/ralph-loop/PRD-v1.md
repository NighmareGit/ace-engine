# PRD: ACE Meta-Cognitive Ralph Loop (v1)

Source epic: `EPIC.md` (authoritative for
design detail; S5 grill resolutions + S6 red-team amendments supersede older text).
ADRs: 0003 (workflow over states), 0004 (Ralph gates consume LLM Judge).
Glossary: `CONTEXT.md` (Ralph round/run, RoundReport, Ralph gate, Pattern,
Pattern projection, Ideation, Ideation collapse).

## Problem Statement

The ACE engine executes one task at a time through a convergent pipeline. It
cannot iteratively attack an objective that needs many coordinated rounds of
ideation, implementation, and adversarial verification — and it has no mechanism
to learn across rounds: every run starts from zero, the skill registry is empty
(G9), and failures repeat. The installed `meta-cognitive-ralph-loop` skill
proves the loop design but is instruction-only: 5 of its 6 loop invariants are
LLM self-report conventions, its orchestrator grades its own gates, its
iteration counter lives in a context window, and its pattern memory is an
unvalidated cross-project SQLite poisoning vector.

## Solution

A first-class engine workflow: the Ralph loop as `engine/workflows/ralph/` —
a round-driver library exposed via a synchronous-but-resumable `run_ralph`
action. Each Ralph round dispatches one ACE run through the existing pipeline,
then the round driver enforces mechanical post-commit gates (deterministic
validator, real tests, model-separated judge per ADR-0004) and reverts on gate
failure. The only data crossing the round boundary is a schema-validated,
size-bounded RoundReport plus the pinned workspace — the fresh-agent rule.
Learned patterns live in a machine-queryable `ralph_patterns` table in
engine.db behind a mechanical approval gate; approved patterns project one-way
into markdown skill-docs (seeding G9). Budgets are cumulative across rounds and
harness-enforced (T8). Ideation is bounded and mechanical (novelty computed,
never LLM-judged); ideation collapse is detected by TF-IDF plan similarity
(G12) and answered with forced diversity, not failure.

## User Stories

1. As an operator, I want to start a Ralph run with a single objective string, so that the engine autonomously iterates toward it without me sequencing tasks.
2. As an operator, I want the action to be resumable by `ralph_run_id` after a crash, so that hours-long runs survive session death.
3. As an operator, I want `ralph status` to show run state, round states, and the resume command, so that I can operate without reading the database.
4. As an operator, I want a stop-file to cancel a Ralph run cleanly, so that cancellation reuses the engine's existing CANCELLED path.
5. As an operator, I want a `ralph_report.json` per run, so that autopsies need one file instead of N round files.
6. As an operator, I want a per-round JSONL event stream, so that I can tail a running Ralph execution.
7. As the engine, I want round N+1's context built from only the RoundReport, the pinned workspace, and the objective, so that no unbounded conversation state leaks across rounds.
8. As the engine, I want Review and RedTeam gate verdicts computed from the LLM Judge's scores by round-driver code, so that no gate outcome is ever LLM self-reported.
9. As the engine, I want a failed Review/RedTeam gate to revert the round's commits, so that gate-failed code never becomes the base of the next round.
10. As the engine, I want per-round budget clamped to remaining cumulative budget, so that a single round cannot exhaust the whole run's cap.
11. As the engine, I want ideation candidates bounded (default 5, max 8) and scored in one LLM call, so that divergent ideation cannot become a budget hole.
12. As the engine, I want novelty computed mechanically against prior selected plans, so that ideation diversity is not another LLM self-report.
13. As the engine, I want ideation collapse (G12) detected by plan-text similarity, so that a stuck-in-a-local-optimum loop triggers forced diversity instead of burning rounds.
14. As the engine, I want a round ledger (`ralph_rounds`), so that churn (G11) and collapse (G12) signals are computable from data.
15. As a reviewer, I want every gate outcome backed by validator, tester, or judge evidence in the RoundReport, so that audits never rely on the orchestrator's honesty.
16. As a pattern consumer outside the engine, I want to query `ralph_patterns` programmatically (approved rows only by default), so that external agents and tools can reuse learned lessons without filesystem scraping.
17. As the owner, I want patterns to enter `proposed` and reach `approved` only through an explicit CLI approval, so that no LLM-written lesson auto-propagates (ADR-0002).
18. As the owner, I want approved patterns projected one-way into markdown skill-docs, so that the skill registry (G9) gets seeded and stays human-auditable.
19. As the owner, I want gate-gaming detection (G13: review pass rate vs objective completion divergence), so that a loop optimizing the judge's rubric instead of the objective is surfaced.
20. As the owner, I want `max_patterns_per_run` and a pattern-similarity hint in the approve CLI, so that pattern flooding and near-duplicate lessons are contained.
21. As a test runner, I want a prompt-audit test for R7, so that the fresh-agent rule is mechanically verified, not documented.
22. As a test runner, I want a query-interface test proving `pattern_query` returns only approved rows, so that the approval gate is regression-proof.
23. As a developer, I want `resume_ralph` to reject terminal runs and dirty worktrees safely, so that resume never lands on a reverted SHA or destroys operator work.

## Implementation Decisions

- **Workflow over states (ADR-0003):** no new State enum entries, no
  VALID_TRANSITIONS changes, no parallel pipeline. Ralph is a cross-run
  workflow; each round is one engine run.
- **Surface (F4-d):** synchronous-but-resumable `run_ralph` action; the round
  driver is a library core; the action and CLI are thin seams over it.
- **Module map:** `engine/workflows/ralph/{round_driver, report_types, gates,
  ideation, pattern_gate, config}.py`; plus `pattern_query.py` and
  `engine/intent/similarity.py` (shared `plan_similarity`, reused by the IIL
  router and G12).
- **RoundReport v1:** typed dataclass, JSON ≤64KB, schema-validated with
  maxLength truncation; fields per S2 §3. The only structured round-boundary
  channel.
- **Gates (ADR-0004):** Dev→validator, Test→tester (deterministic);
  Review/RedTeam→LLM Judge scores consumed by round-driver threshold logic
  ("Ralph gate"); RedTeam oracle only via explicit escalation config.
- **Revert (S6/M1):** `git revert` of the round's commits; `push
  --force-with-lease` only as documented fallback. Aborted round → existing
  FAILED state; `revert_round()` emits `ralph_round_failed`.
- **Budget (S6/M2):** per-round caps = `min(config.cap, remaining_cumulative)`;
  cumulative accounting in `ralph_rounds`; `max_ralph_rounds` default 5.
- **Resume (S5/S6):** resumable from failed/cancelled only; budget-exhausted
  resume hard-stops; `BEGIN IMMEDIATE` lock → `RalphRunBusyError`; stash →
  `DirtyWorktreeError`; `ralph status` CLI.
- **Patterns:** `ralph_patterns` table (status proposed|approved|rejected|
  expired, TTL via `expires_at`); async CLI approval (`approve-pattern`/
  `reject-pattern`); LIKE-based AND keyword search, `score DESC` ranking,
  namespace-scoped, approved-only default; `max_patterns_per_run` cap;
  markdown projection one-way from SQL.
- **Tables (additive-only):** `ralph_runs` (owned by round_driver; lock
  target), `ralph_rounds`, `ralph_patterns`.
- **Telemetry:** `RalphJsonlHandler` → `ralph_run_<id>_rounds.jsonl`; events
  `ralph_round_failed`, `ralph_ideation_collapse`, G11 churn, G13 divergence;
  `ralph_report.json` per run (Archive-compatible).

## Testing Decisions

- Test external behavior at the highest seams: `run_ralph()` / `resume_ralph()`
  (RalphResult), `gates.evaluate_gate_sequence()` (GateVerdict),
  `pattern_query.query_patterns()` (rows), CLI surface.
- Prompt-audit tests: R7 (round prompt contains only the three allowed inputs;
  no denillisted workspace paths), R1 (no LLM self-reported gate outcome in the
  gate path).
- Prior art: engine/orchestrator tests for budget/stop/checkpoint patterns;
  laneb protocol tests for parse-and-reject discipline; adapter tests for
  handler wiring.
- Live gating follows engine convention: unit/mock in CI; a Triton live smoke
  is a separate stage after unit green.

## Out of Scope

- Async action semantics / polling API (future enhancement).
- Merges to main from within the loop; engine self-modification (ADR-0002).
- Ralph loop driving ACE's own development (dogfooding is post-v1).
- External orchestrator harness, lane-B primitives, new state lane (rejected,
  see epic non-goals).
- Pattern TTL sweeper UI and dashboard surface (ledgered, post-v1).

## Further Notes

- Success metric: "Ralph pass rate" ≥60% (objective achieved within
  `max_ralph_rounds`) on the first real workload, N4-style — measured at S8
  dogfood, outside this PRD's implementation scope.
- The S6 red-team's accepted add-ons: Archive integration via
  `ralph_report.json`, G11 wiring. Deferred add-ons are ledgered.
- Design provenance: S1 analysis → S2 redesign → S3 fireplace → S4 epic →
  S5 grill → S6 red-team, all committed under `.scratch/ralph-loop/research/`.
