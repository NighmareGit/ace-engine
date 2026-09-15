# S2 REDESIGN: ACE Meta-Cognitive Ralph Loop

**Phase:** S2 REDESIGN (codebase-design for ACE usage)
**Date:** 2026-09-04
**Inputs:** PRE-EPIC-ace-meta-cognitive-ralph-loop.md, skill-analysis-s1.md
**Commit:** Workflow-over-states (Option A). No new State enum entries. No new VALID_TRANSITIONS.

---

## 1. The decision: workflow vs lane

### The S1 mapping's pull (and why it's incomplete)

The S1 analysis (skill-analysis-s1.md §4) maps:
- Ralph **iteration** → ACE **run** (`engine_runs` table, state.py line 107)
- Ralph **gate** → ACE **VALIDATE / TEST / JUDGE** states
- Ralph **Phase 0 Scoping** → ACE **PARSING**
- Ralph **Phase 3 Developer** → ACE **GENERATE** or Lane-B

This mapping suggests a workflow-over-states: one Ralph round ≈ one ACE run through the existing pipeline. The existing states already do what each Ralph phase needs.

**The challenge:** this mapping is *per-task*, not *per-iteration*. A Ralph iteration is not a single task — it's a full Dev→Review→Test→RedTeam cycle that may touch multiple files, retry, and diverge. If we naively map "iteration → run", we lose the ability to retry within a round (the existing run loop already retries generate/test/commit, but it does NOT natively do "generate → LLM review → fix → test → adversarial red-team → fix → commit"). The Ralph gate sequence is richer than the ACE per-task pipeline.

### Option A — Workflow over existing states (SELECTED)

**Mechanism:** a new `engine/workflows/ralph/round_driver.py` module that owns an outer loop. Each iteration of the outer loop:
1. Calls `Engine.run()` (or a new `Engine.run_single_round()` facade) with a synthetic single-task PRD.
2. The existing pipeline executes PARSING → QUEUED → CONTEXT → GENERATE → VALIDATE → TEST → COMMIT → NEXT → DONE exactly as today.
3. After COMMIT, the round driver runs the Review and Red-Team gates (mechanically, via `validator.py`, `tester.py`, `judge.py`, `oracle_escalate.py`) on the committed output.
4. If any gate fails, the round driver patches the task definition (ADR-0002 style) and re-runs — up to `max_retries_per_round`.
5. On round success, the round driver persists a typed `RoundReport`, and the next round begins with ONLY that report + pinned workspace state.

**What changes in state.py:** Nothing. The State enum and VALID_TRANSITIONS are untouched. The additive-only DB rule (state.py line 162-166) means we add new tables (`ralph_rounds`, `ralph_reports`, `ralph_patterns`) via `ALTER TABLE ... ADD COLUMN` or `CREATE TABLE IF NOT EXISTS` — exactly as `judge_verdicts` and `engine_scores` were added.

**What changes in engine.py:** A new `run_ralph(objective, max_rounds, config)` method on the `Engine` class that delegates to `round_driver.execute()`. The existing `run()` method is unchanged. The engine's SIGINT handler (engine.py line 51) already calls `self.cancel()` which checkpoints — Ralph inherits this for free.

**Leverage points reused:**
- `engine/orchestrator/budget.py` — T8 counters bound every LLM call within a round
- `engine/orchestrator/stop.py` — CANCELLED path via stop-file
- `engine/orchestrator/checkpoint.py` — T6 resume via `reconcile_git_state()`
- `engine/orchestrator/session.py` — per-run session row for budget + state
- `engine/intent/sandbox.py` — all generated code runs through Docker
- `engine/intent/skills.py` — pattern library as skill-docs
- `engine/orchestrator/oracle_escalate.py` — P6 oracle for exhausted rounds

### Option B — New state lane (REJECTED)

**Mechanism:** Add `RALPH_IDEATION`, `RALPH_DEVELOP`, `RALPH_REVIEW`, `RALPH_REDTEAM`, `RALPH_META` to the State enum; wire VALID_TRANSITIONS for the new lane; add a `RalphPipeline` subclass of `Pipeline`.

**Why rejected:**
1. **DB migration risk.** The additive-only rule (state.py line 162-166) means we cannot remove or rename states. A new lane adds 5 enum entries that must coexist forever. The existing 12-state machine is already at the complexity limit the codebase-design vocabulary calls "depth" — adding a parallel lane doubles the transition matrix.
2. **No reuse.** The new lane would need its own retry logic, budget integration, and stop-file handling — duplicating what `engine.py:_run_task()` already does. The workflow approach leverages `_run_task()` as-is.
3. **Locality violation.** The state machine in `pipeline.py` is a *per-task* machine. Ralph is a *cross-task* (cross-run) loop. Mixing the two into one state machine splits the locality of "what is the engine doing right now" across two abstraction layers.
4. **Gate enforcement.** The existing VALID_TRANSITIONS only enforce *ordering* (e.g., GENERATE before VALIDATE), not *outcome* (did validation actually pass?). The Ralph gates need outcome-based transitions ("Review passed" vs "Review failed → back to Develop"). This is a fundamentally different transition model that doesn't fit the existing `Pipeline.transition()` mechanic (pipeline.py line 25-41, which only checks legality, not pass/fail).

### Rationale summary

The S1 mapping is correct at the *phase* level (each Ralph phase maps to an existing ACE capability) but misleading at the *granularity* level (one iteration ≠ one run). The workflow approach accepts this: each Ralph round is one ACE run + a post-commit gate sequence owned by the round driver. The lane approach would force the round-level control flow into the per-task state machine, violating locality and requiring a parallel pipeline.

---

## 2. Module map

Proposed package: `engine/workflows/ralph/`

```
engine/workflows/
├── __init__.py
└── ralph/
    ├── __init__.py          # public facade: run_ralph()
    ├── round_driver.py      # outer loop: the Ralph workflow
    ├── report_types.py      # RoundReport dataclass + JSON schema
    ├── gates.py             # mechanical gate enforcement
    ├── ideation.py          # bounded divergent ideation (Phase 1)
    ├── pattern_gate.py      # ADR-0002 pattern registry with approval gate
    └── config.py            # RalphConfig dataclass
```

### `round_driver.py`

**Responsibility:** Own the outer Ralph loop. This is the workflow's seam to the rest of the engine — it is the ONLY module that knows "we are in a Ralph round." Everything below it (engine.py, pipeline.py, budget.py) runs unchanged.

**Public interface:**
```python
def run_ralph(
    objective: str,                              # the Ralph goal
    project_path: str,                           # workspace
    config: RalphConfig,                         # round budget, model selection
    on_event: Callable | None = None,            # event callback
) -> RalphResult:                                # terminal result

def resume_ralph(
    run_id: str,                                 # interrupted Ralph run
    project_path: str,
    config: RalphConfig,
) -> RalphResult:
```

**Leverage points:**
- `Engine.run()` — each round dispatches one run with a synthetic single-task PRD
- `budget.check_budget()` — consulted before each round (T8)
- `stop.check_stop_file()` — consulted before each round (T5)
- `checkpoint.reconcile_git_state()` — on resume (T6)
- `gates.evaluate_gate_sequence()` — post-commit gate enforcement
- `ideation.generate_candidate_plans()` — Phase 1 divergent ideation
- `pattern_gate.propose_pattern_update()` — Phase 6 meta-cognition

**Seam:** `round_driver.py` is the adapter boundary. It translates between the Ralph protocol (rounds, reports, gates) and the ACE protocol (runs, RunResults, state transitions). Nothing below this seam knows about Ralph.

### `report_types.py`

**Responsibility:** Define the typed round report that crosses the round boundary. Mirrors `orchestrator/replan_types.py` discipline (pure dataclasses, transport-independent, JSON-serializable).

**Public interface:**
```python
@dataclass
class RoundReport:
    round_id: int
    objective: str
    plan: str                                    # selected plan from ideation
    tasks_executed: list[TaskSummary]            # what this round did
    gate_verdict: GateVerdict                    # combined gate result
    ideation_summary: IdeationSummary            # what was considered, what was picked
    pattern_proposals: list[PatternProposal]     # meta-cognition output
    budget_consumed: BudgetSnapshot              # tokens, calls, wall-clock
    workspace_sha: str                           # git SHA after commit
    state: str                                   # "completed" | "failed" | "cancelled"
    error: str | None

    def to_dict(self) -> dict
    def to_json(self) -> str                     # bounded: max 64KB
    @staticmethod
    def from_json(raw: str) -> "RoundReport"     # validates schema + bounds
```

**Leverage points:** None (pure types). Reuses the `dataclass`/`asdict` pattern from `replan_types.py`.

**Seam:** The round report is the ONLY structured data that crosses the round boundary. The next round's context is this report + the pinned workspace (git SHA) + the objective. Nothing else from the previous round's conversation is carried.

### `gates.py`

**Responsibility:** Mechanical gate enforcement. Each gate is backed by an ACE-native deterministic or model-separated mechanism. No LLM self-reporting.

**Public interface:**
```python
def evaluate_gate_sequence(
    code: str,
    task: Task,
    project_path: str,
    transport,                                   # engine transport
    config: RalphConfig,
) -> GateVerdict:
    """Run Dev→Review→Test→RedTeam. Return combined verdict."""

def review_gate(code: str, task: Task, transport, port: int) -> GateResult:
    """LLM review via judge model (:8080). Returns pass/fail + reasoning."""

def test_gate(project_path: str, transport, timeout: int) -> GateResult:
    """Deterministic test execution via tester.py."""

def redteam_gate(code: str, task: Task, transport, config: RalphConfig) -> GateResult:
    """Adversarial review via judge model or P6 oracle."""
```

**Leverage points:**
- `engine/validator.py` — deterministic AST/import/style checks (Dev gate)
- `engine/tester.py` — actual test suite execution (Test gate)
- `engine/llm_judge.py` — separate model on :8080 for Review and RedTeam gates
- `engine/orchestrator/oracle_escalate.py` — P6 oracle for RedTeam escalation

**Seam:** `gates.py` is the adapter between the Ralph gate protocol (Dev→Review→Test→RedTeam) and ACE's existing gate infrastructure. It translates "Review failed, here's why" into a `GateResult` the round driver can act on.

### `ideation.py`

**Responsibility:** Bounded divergent ideation (Ralph Phase 1 "firebrainstorm"). Generate N candidate plans, score them, select one. Budget-capped.

**Public interface:**
```python
def generate_candidate_plans(
    objective: str,
    previous_rounds: list[RoundReport],          # prior round reports (context)
    n_candidates: int,                           # default 5, max 8
    model_port: int,                             # 8082 (9B) for speed
    max_tokens_per_candidate: int,               # default 2048
) -> IdeationResult:
    """Generate + score N candidate plans. Return scored list + selection."""
```

**Leverage points:**
- `engine/orchestrator/budget.py` — each ideation LLM call counts against T8
- `engine/orchestrator/stop.py` — cancel-check between candidates

**Seam:** Ideation is a self-contained capability. The round driver calls it, gets back a scored list, persists the selection in the RoundReport. The next round sees only the selected plan + summary of alternatives considered.

### `pattern_gate.py`

**Responsibility:** ADR-0002-compliant pattern library. Patterns live in a `ralph_patterns` TABLE in engine.db (canonical machine store); approved patterns are projected into markdown skill-docs for human consumption.

> **OWNER AMENDMENT (post-review):** The original S2 design made markdown skill-docs the primary store and rejected SQLite entirely. Owner critique: learned patterns are structured data that external consumers (other agents, projects, dashboards, future tooling) must query programmatically — markdown would force a rework when that consumer arrives. The skill's `query.py` exists precisely for this. Revised contract:
>
> - **Canonical store:** `ralph_patterns` table in engine.db (additive-only rule, WAL, backup discipline already established). Typed columns: `id`, `project` (namespace), `pattern_type`, `body`, `keywords`, `score`, `status` (`proposed|approved|rejected|expired`), `source_ralph_run_id`, `source_round_id`, `created_at`, `expires_at`.
> - **ADR-0002 gate is storage-medium-independent:** rows move `proposed → approved` only via the mechanical approval gate; prompt injection reads `approved` rows only. The contamination vector was unvalidated content auto-propagating, not SQLite per se.
> - **Markdown skill-docs are an export/projection** of approved patterns (seeds the G9-empty skill registry for human/agent reading), never the source of truth.
> - **TTL/expiry/integrity** become SQL: `status` transitions + `expires_at` column, not file hygiene.
> - **External query interface** is a first-class epic deliverable (S4): same access pattern as `engine_runs` / `laneb_events` / `skill_lookups`.

**Public interface:**
```python
def propose_pattern_update(
    round_report: RoundReport,
    skill_store: SkillStore,                     # from engine/intent/skills.py
) -> PatternProposal:
    """Extract a pattern proposal from a round report. Does NOT persist."""

def persist_pattern(
    proposal: PatternProposal,
    approval: ApprovalToken,                     # human/owner signature
    skills_dir: Path,                            # target skill registry dir
) -> str:                                         # skill_id of persisted pattern
    """Write pattern as markdown skill-doc. Requires approval."""
```

**Leverage points:**
- `engine/intent/skills.py:SkillStore` — pattern storage as markdown docs
- `engine/intent/skills.py:_write_lookup()` — G9 telemetry for pattern misses

**Seam:** `pattern_gate.py` is the adapter between the Ralph "self-improvement" protocol and ACE's skill-as-docs registry. It rejects the skill's SQLite approach entirely (see §6).

### `config.py`

**Responsibility:** Ralph-specific configuration. Extends `EngineConfig` without modifying it.

**Public interface:**
```python
@dataclass
class RalphConfig:
    max_ralph_rounds: int = 5                    # outer loop cap (answers Open Q #4)
    max_round_retries: int = 3                   # per-round gate retry cap
    ideation_candidates: int = 5                 # N for divergent ideation
    ideation_model_port: int = 8082              # 9B for ideation (speed)
    review_model_port: int = 8080                # 35B judge for review
    redteam_model_port: int = 8080               # 35B judge for red-team
    redteam_escalate_to_oracle: bool = False     # P6 oracle for red-team
    pattern_approval_required: bool = True       # ADR-0002 gate
    max_pattern_length_bytes: int = 8192         # per-pattern size cap
    round_timeout_s: int = 1800                  # per-round wall-clock cap
```

---

## 3. Round report contract (Open Question #2)

The round report is the ONLY data that crosses the round boundary. It mirrors the T4 re-plan report style (`orchestrator/replan_types.py`): pure dataclass, transport-independent, JSON-serializable, validated on construction.

### JSON schema

```json
{
  "$schema": "raft-round-report-v1",
  "type": "object",
  "required": ["round_id", "objective", "plan", "tasks_executed",
               "gate_verdict", "ideation_summary", "pattern_proposals",
               "budget_consumed", "workspace_sha", "state"],
  "properties": {
    "round_id":          { "type": "integer", "minimum": 0 },
    "objective":         { "type": "string", "maxLength": 4096 },
    "plan":              { "type": "string", "maxLength": 16384 },
    "tasks_executed": {
      "type": "array",
      "maxItems": 32,
      "items": {
        "type": "object",
        "required": ["task_id", "title", "state"],
        "properties": {
          "task_id":    { "type": "string", "pattern": "^T[0-9]+$" },
          "title":      { "type": "string", "maxLength": 256 },
          "state":      { "type": "string", "enum": ["completed", "failed", "cancelled"] },
          "commit_sha": { "type": "string", "pattern": "^[0-9a-f]{7,40}$" },
          "tokens":     { "type": "integer", "minimum": 0 }
        }
      }
    },
    "gate_verdict": {
      "type": "object",
      "required": ["overall_pass", "gates"],
      "properties": {
        "overall_pass": { "type": "boolean" },
        "gates": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["gate", "passed"],
            "properties": {
              "gate":   { "type": "string", "enum": ["dev", "review", "test", "redteam"] },
              "passed": { "type": "boolean" },
              "detail": { "type": "string", "maxLength": 4096 },
              "model":  { "type": "string" }
            }
          }
        }
      }
    },
    "ideation_summary": {
      "type": "object",
      "required": ["candidates_considered", "selected_idx"],
      "properties": {
        "candidates_considered": { "type": "integer", "minimum": 1 },
        "selected_idx":          { "type": "integer", "minimum": 0 },
        "selection_reason":      { "type": "string", "maxLength": 2048 }
      }
    },
    "pattern_proposals": {
      "type": "array",
      "maxItems": 8,
      "items": {
        "type": "object",
        "required": ["pattern_id", "title", "body"],
        "properties": {
          "pattern_id": { "type": "string", "maxLength": 128 },
          "title":      { "type": "string", "maxLength": 256 },
          "body":       { "type": "string", "maxLength": 8192 },
          "approved":   { "type": "boolean" }
        }
      }
    },
    "budget_consumed": {
      "type": "object",
      "required": ["llm_calls", "total_tokens", "wall_clock_s"],
      "properties": {
        "llm_calls":     { "type": "integer", "minimum": 0 },
        "total_tokens":  { "type": "integer", "minimum": 0 },
        "wall_clock_s":  { "type": "number", "minimum": 0 }
      }
    },
    "workspace_sha":     { "type": "string", "pattern": "^[0-9a-f]{7,40}$" },
    "state":             { "type": "string", "enum": ["completed", "failed", "cancelled"] },
    "error":             { "type": "string", "maxLength": 4096 }
  }
}
```

### Validation rules

| Field | Rule | Rationale |
|-------|------|-----------|
| `round_id` | Monotonic, gapless | Fresh-agent rule: round N+1 must reference round N |
| `objective` | ≤4096 chars | Prevents context bleed from prior rounds |
| `plan` | ≤16384 chars | Selected plan only, not all candidates |
| `tasks_executed` | ≤32 items | One round = one logical task; 32 is a safety bound |
| `pattern_proposals` | ≤8 per round, each ≤8192 chars | Prevents pattern spam; ADR-0002 scope limit |
| `workspace_sha` | Valid git SHA | Pinned workspace state for the next round |
| Total JSON | ≤64KB serialized | Size bound for the fresh-agent context |

### Fresh-agent rule

The next round's context is ONLY:
1. The `RoundReport` from the previous round (validated against the schema above)
2. The pinned workspace at `workspace_sha` (git checkout)
3. The original `objective` string

Nothing else from the previous round's LLM conversation, context window, or intermediate state is carried. This is the core reliability guarantee of the Ralph loop, and it is mechanically enforced by the round driver: the next round's LLM prompt is constructed from ONLY these three inputs.

---

## 4. Gate mapping

Each Ralph skill gate maps to an ACE-native mechanism. No LLM self-reporting.

| Ralph gate | ACE mechanism | Location | Deterministic? | Model |
|------------|---------------|----------|----------------|-------|
| **Dev** (code generated) | `validator.py:validate()` — AST/import/style/execution checks | `engine/validator.py` | Yes | None |
| **Review** (code quality) | `llm_judge.py:score_task()` via judge model on :8080 | `engine/llm_judge.py` | No (LLM) | Qwen3.6-35B (:8080) |
| **Test** (test suite) | `tester.py:run_tests()` — actual test execution | `engine/tester.py` | Yes | None |
| **RedTeam** (adversarial) | `llm_judge.py:score_task()` with red-team rubric via judge, OR `oracle_escalate.py:escalate_task_to_oracle()` for P6 | `engine/llm_judge.py` or `engine/orchestrator/oracle_escalate.py` | No (LLM) | 35B (:8080) or P6 oracle |

### Gate interface

```python
@dataclass
class GateResult:
    gate: str            # "dev" | "review" | "test" | "redteam"
    passed: bool
    detail: str          # ≤4096 chars: stage errors, judge reasoning, test failures
    model: str | None    # model identity (judge/oracle), None for deterministic
    tokens: int          # LLM tokens consumed (0 for deterministic gates)

@dataclass
class GateVerdict:
    overall_pass: bool                   # all gates passed
    gates: list[GateResult]              # per-gate results in order
    retry_recommended: bool              # True if any gate failed but is retryable
    retry_target: str | None             # which gate to retry from
```

### Gate sequence enforcement

The round driver enforces the sequence mechanically:
1. Dev gate runs first (deterministic). If it fails, the code is regenerated (up to `max_retries_generate` times, same as existing engine.py logic).
2. Review gate runs on committed code. If it fails, the round driver patches the task definition with the judge's reasoning and re-runs the round.
3. Test gate runs after Review passes. If it fails, same retry logic.
4. RedTeam gate runs last. If it fails AND `redteam_escalate_to_oracle` is True, the P6 oracle is consulted (via `oracle_escalate.py`). If the oracle is unavailable or also fails, the round is marked failed.

This is a deterministic state machine in `gates.py`, not an LLM-tracked variable. The retry count is a Python `int` in the round driver, not a context variable the LLM increments.

---

## 5. Ideation design (Open Question: divergent ideation, S1 surprise #5)

### The problem

Ralph Phase 1 ("firebrainstorming") generates 10+ diverse solutions, cross-pollinates, and scores them. ACE has no equivalent — its pipeline is purely convergent (parse → generate → validate → test). The S1 analysis (surprise #5) correctly identifies this as genuinely novel for ACE.

The risk: unbounded ideation is a budget hole. Generating 10 full plans at 2048 tokens each = 20K+ tokens before any code is written. With `max_ralph_rounds=5`, that's 100K+ tokens on ideation alone.

### The design: bounded ideation

**N candidates:** Default 5, max 8 (configurable via `RalphConfig.ideation_candidates`). The round driver enforces the max.

**Model selection:** 9B on :8082 (subject model) for ideation. The 9B is faster and cheaper for plan generation; the 35B judge is reserved for Review/RedTeam gates where quality matters more than speed. This is the same model ACE uses for code generation.

**Depth limits:**
- Each candidate plan: max 2048 tokens (configurable via `max_tokens_per_candidate`)
- Total ideation budget: max 16K tokens per round (8 candidates × 2048)
- Ideation LLM calls count against T8 budget counters (via `budget.increment_and_check()`)

**Scoring:** Each candidate is scored by the 9B model on 3 dimensions (feasibility, alignment with objective, novelty relative to prior rounds). The scoring prompt is a single LLM call with all candidates in context (not N separate calls).

**Selection:** The highest-scoring candidate is selected. The selection reason is persisted in `RoundReport.ideation_summary.selection_reason`. The other candidates are discarded — only the summary (`candidates_considered`, `selected_idx`) is persisted.

**Persistence:** The selected plan is written to the RoundReport. The full candidate set is NOT persisted (budget/space optimization). The `ideation_summary` field records how many were considered and which was selected, enabling the G12 signal (ideation collapse = same solution repeated N rounds) to be detected by the round ledger.

### Budget integration

```python
# In ideation.py:
for candidate in candidates:
    counters, stopped, reason = budget.increment_and_check(run_id, tokens=candidate.tokens)
    if stopped:
        # Ideation budget exhausted. Use whatever candidates we have.
        break
```

If the T8 budget is exhausted mid-ideation, the round driver proceeds with whatever candidates were generated (minimum 1). If zero candidates were generated, the round fails with a clear error.

---

## 6. Pattern library / skill registry (ADR-0002)

> **OWNER AMENDMENT SUPERSEDES PARTS OF THIS SECTION:** see the revised contract in the `pattern_gate.py` block (§2). Canonical store is the `ralph_patterns` engine.db table; markdown is a projection; the approval gate is unchanged. The table below's *rejections* still stand except "SQLite as primary storage" — SQLite (engine.db, with the approval gate) is now the canonical store; the original rejection targeted the *skill's* unvalidated cross-project SQLite auto-injection, which remains rejected.

### The problem (S1 gap #2)

The skill's pattern library (SQLite, LLM-written, cross-project, no validation) violates ADR-0002 in spirit: it is LLM-edited behavior that persists and propagates across all future runs. The S1 analysis (§3a) flags this as CRITICAL.

### The design: ACE-native replacement

**Patterns are markdown skill-docs in the skill registry.** The existing `engine/intent/skills.py:SkillStore` already provides:
- Markdown-based skill docs with front-matter (skill_id, title, purpose, body)
- Per-project namespacing (the `skills_dir` is configurable)
- G9 telemetry (`skill_lookups` table tracks misses)
- Human-readable, auditable, version-controlled (markdown in git)

**The approval gate:**
1. After each round, `pattern_gate.py` extracts a pattern proposal from the RoundReport (if the round produced a reusable insight).
2. The proposal is written to a staging area: `.scratch/ralph-loop/patterns/pending/<pattern_id>.md`
3. A human/owner reviews and approves (or the `pattern_approval_required` config is set to False for automated testing).
4. On approval, the pattern is moved to the skill registry: `engine/intent/skills/<pattern_id>.md`
5. The `SkillStore` picks it up on next load (or is reloaded explicitly).

**Per-project namespacing:** Patterns are stored in the project's own `skills/` directory, NOT a global SQLite DB. Cross-project reuse is possible via a shared `skills_dir` config, but it is opt-in, not automatic. This kills the cross-project contamination vector (S1 gap #d).

**Bridge adapters (optional ingest path):** The existing `query.py` and `persist.py` scripts can be used as an ingest path for migrating old SQLite patterns to markdown. `persist.py` already supports stdin (S1 surprise #1), so the pipe path is trivial:
```bash
python3 -m meta_cognitive_ralph_loop.query | python3 bridge/sqlite_to_markdown.py
```
This is NEVER a trust path — the output still goes through the approval gate.

### What is rejected

| Rejected pattern | Why |
|------------------|-----|
| Cross-project auto-injection | ADR-0002 violation; contamination vector |
| `/tmp` staging for pattern files | Race condition (S1 gap #5); use pipes or workspace-local `.scratch/` |
| SQLite as primary storage | Opaque, not auditable, not version-controlled |
| LLM-written patterns without approval | ADR-0002 violation; the "self-improving" claim must be task-definition edits only, with a mechanical review gate |
| Patterns containing executable code | Patterns are advisory context (markdown), not executable instructions. No `proposed_action` field that gets injected as a system prompt. |

---

## 7. Budget & stop integration

### max_ralph_rounds config

New field in `RalphConfig.max_ralph_rounds` (default 5). This is the outer loop cap — the maximum number of Ralph rounds (ACE runs) to execute. It is distinct from:
- `EngineConfig.max_llm_calls` — per-run LLM call cap (T8)
- `EngineConfig.max_total_tokens` — per-run token cap (T8)
- `EngineConfig.max_wall_clock_s` — per-run wall-clock cap (T8)

The round driver checks `max_ralph_rounds` before starting a new round. If the cap is reached, the loop terminates with state "completed" (objective achieved) or "failed" (objective not achieved within budget).

### T8 counter carry-across

T8 counters live in `orchestrator_session` (session.py line 30-43). Each Ralph round creates a new ACE run with its own `run_id`, so each round gets a fresh session row. The round driver maintains a separate `ralph_rounds` table that tracks cumulative budget across rounds:

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
    created_at TEXT DEFAULT (datetime('now'))
);
```

Before each round, the round driver sums `llm_calls` and `total_tokens` across all prior rounds in this `ralph_run_id` and checks against `EngineConfig.max_llm_calls` / `max_total_tokens`. If the cumulative budget is exhausted, the Ralph loop terminates with CANCELLED.

### Stop/timeout via shared CANCELLED path

The round driver checks `stop.check_stop_file(run_id)` before each round and between gate evaluations. If a stop-file is detected:
1. The current round is marked CANCELLED in `ralph_rounds`.
2. The Ralph loop terminates.
3. The `workspace_sha` of the last successful round is recorded in the RalphResult.

This reuses the exact same mechanism as `engine.py` line 117-137 (budget check) and line 146-154 (CancelledError handling).

### Resume via checkpoint.py

On resume (`resume_ralph(run_id)`):
1. Load the `ralph_rounds` table to find the last completed round.
2. Check out the workspace at the last `workspace_sha`.
3. Reconstruct the RoundReport from the last completed round.
4. Continue from round N+1.

This reuses `checkpoint.reconcile_git_state()` (checkpoint.py line 50-83) for git reconciliation and `session.get_budget_counters()` for budget state.

---

## 8. ADR draft

```markdown
# ADR 0003 — Ralph loop as a workflow over existing engine states, not a new state lane

> **OWNER AMENDMENT (post-review):** §6 pattern storage revised — canonical store is the
> `ralph_patterns` engine.db table (machine-queryable for external consumers, per owner
> directive); approved patterns project to markdown skill-docs; ADR-0002 approval gate is
> medium-independent and unchanged. See redesign-s2.md §2 `pattern_gate.py` and §6 amendment.

**Status:** proposed
**Decided:** 2026-09-04
**Linked design:** `.scratch/ralph-loop/research/redesign-s2.md`

## Context

The ACE engine's state machine (`engine/state.py`, 12 states, `VALID_TRANSITIONS`) is a *per-task* pipeline: PARSING → QUEUED → CONTEXT → GENERATE → VALIDATE → TEST → COMMIT → NEXT → DONE. The Ralph loop (from the `meta-cognitive-ralph-loop` skill) is a *cross-run* outer loop: fresh-agent rounds with divergent ideation, mechanical gates, and adversarial testing. The pre-epic (PRE-EPIC §3, Open Question #1) asks: is Ralph a new state lane or a workflow over existing states?

The S1 analysis (skill-analysis-s1.md §4) maps each Ralph phase to an existing ACE capability, suggesting workflow-over-states. But that mapping is per-task, not per-iteration — a Ralph round is richer than a single ACE task (it includes Review and RedTeam gates that ACE's per-task pipeline doesn't natively do).

## Decision

**Implement Ralph as a workflow over existing states.** A new `engine/workflows/ralph/round_driver.py` module owns the outer loop. Each Ralph round dispatches one ACE run through the existing pipeline (`Engine.run()`). Post-commit gate enforcement (Review, RedTeam) is owned by the round driver via `gates.py`, which leverages ACE's existing `validator.py`, `tester.py`, `llm_judge.py`, and `oracle_escalate.py`.

**Do NOT add new State enum entries or VALID_TRANSITIONS.** The Ralph loop is a cross-run workflow, not a per-task state machine. Mixing the two abstraction layers would violate locality and require a parallel pipeline.

## Consequences

**Positive**
- Zero changes to `engine/state.py`, `engine/pipeline.py`, or `engine/engine.py`'s `run()` method. The additive-only DB rule is respected — new tables (`ralph_rounds`, `ralph_reports`, `ralph_patterns`) are added via `CREATE TABLE IF NOT EXISTS`.
- Full reuse of T8 budget (`budget.py`), T5 stop/timeout (`stop.py`), T6 resume (`checkpoint.py`), and P6 oracle escalation (`oracle_escalate.py`).
- The existing `Engine.run()` is unchanged — Ralph is a new `run_ralph()` method that delegates to `round_driver.execute()`.
- Gate enforcement is mechanical (deterministic validator + tester + separate judge model), not LLM self-report — satisfying S1 gap #1.

**Negative / trade-offs**
- The round driver is a new abstraction layer (adapter boundary) that must be maintained. Developers must understand that "Ralph round" ≠ "ACE run" even though each round dispatches one run.
- Post-commit gates (Review, RedTeam) run AFTER the engine has already committed code. A failed RedTeam gate means the commit is reverted (via `git reset` to the pre-round SHA) before the next round. This is a new operation on the git state that the existing engine doesn't do.
- The fresh-agent rule (next round sees only RoundReport + pinned workspace) is a convention enforced by the round driver, not a harness-level guarantee. A buggy round driver could leak context.

## Alternatives considered

1. **New state lane** (add `RALPH_IDEATION`, `RALPH_DEVELOP`, `RALPH_REVIEW`, `RALPH_REDTEAM`, `RALPH_META` to State enum). Rejected: doubles the transition matrix, requires a parallel `RalphPipeline`, violates the additive-only DB rule's spirit, and forces outcome-based transitions into a state machine that only handles ordering-based transitions.
2. **External orchestrator loop** (keep the loop outside the engine, poking at ACE via CLI/API). Rejected: the pre-epic (line 17-19) explicitly states the loop should be a "first-class engine workflow, not an external script poking at ACE." This is the status quo the epic exists to eliminate.
3. **Lane-B extension** (add `ralph_round()` primitive to lane-B). Rejected: lane-B is code-as-intent with depth ≤2 and sandbox-only execution. Ralph needs full pipeline access (generate, validate, test, commit) plus post-commit gates. Lane-B is too narrow.

## Consequences of not doing this

If a new state lane were added, the state machine would grow from 12 to 17 states, the transition matrix would roughly double, and the existing `Pipeline.transition()` mechanic (which only checks ordering legality) would need to be extended to handle outcome-based transitions — a fundamental semantic change to the engine's core abstraction. If the loop were kept external, ACE would remain without native divergent-ideation and adversarial-testing capabilities, and the G9 (skill registry) gap would stay open.
```

---

## 9. Risks / open items for S3

The fireplace phase (S3) must decide **how ACE issues a Ralph run** — the issue surface. Four options with trade-offs:

### Option 1: ACE action route (`routes/ace_actions.json`)

Add `run_ralph(objective)` as a new ACE action. The operator (or an outer orchestrator) invokes it via the existing action dispatch mechanism.

**Trade-offs:**
- ✅ First-class: Ralph becomes a native ACE capability, invocable like any other action.
- ✅ Reuses the existing action dispatch, event emission, and result persistence infrastructure.
- ❌ Requires changes to the action schema and dispatch logic — a new code path in the engine's hot path.
- ❌ The action surface is designed for single-shot operations; a Ralph run is a multi-round workflow that may run for hours. The action would need async semantics.

### Option 2: Loop-over-runs (outer orchestrator starts N ACE runs)

An outer orchestrator (the DSH ralph tool, or a new `ralph_cli.py`) starts N ACE runs, each with a synthetic single-task PRD. The round report is persisted as a workspace file; the orchestrator reads it and constructs the next round's PRD.

**Trade-offs:**
- ✅ Zero changes to the engine's hot path. The engine just sees N independent runs.
- ✅ The orchestrator can be developed and tested independently of the engine.
- ❌ The "fresh-agent" guarantee is a convention, not a harness-level enforcement. A buggy orchestrator could leak context between rounds.
- ❌ No single `run_id` for the entire Ralph execution — debugging requires correlating N run IDs.
- ❌ Budget accounting across runs is the orchestrator's responsibility, not the engine's.

### Option 3: Lane-B primitive extension

Add `ralph_round()` as a new lane-B primitive. The generated code calls `ralph_round(objective)` which halts the lane-B execution, runs a full Ralph round, and resumes with the round report injected.

**Trade-offs:**
- ✅ Reuses lane-B's queued-intent mechanism (laneb.py line 14-19) for the round boundary.
- ✅ Depth ≤2 is already enforced by the lane-B executor.
- ❌ Lane-B is code-as-intent with sandbox-only execution. Ralph needs full pipeline access (generate, validate, test, commit) — lane-B is too narrow.
- ❌ The lane-B executor's single-shot-restart model (laneb.py line 203-210) doesn't accommodate multi-round workflows.

### Option 4: External harness (status quo)

Keep the Ralph loop outside the engine entirely. The DSH ralph tool (or a standalone script) orchestrates ACE runs via CLI/API.

**Trade-offs:**
- ✅ Zero engine changes. Fastest to implement.
- ❌ The pre-epic (line 17-19) explicitly rejects this: "the skill's loop should become a first-class engine workflow, not an external script poking at ACE."
- ❌ No native budget integration, no native resume, no native gate enforcement. All of these must be reimplemented in the harness.
- ❌ The G9 (skill registry) gap stays open — the pattern library remains external to ACE.

### S3 recommendation

Option 1 (ACE action route) is the end-state target, but Option 2 (loop-over-runs) is the pragmatic first step: it validates the round report contract and gate enforcement without touching the engine's hot path. S3 should prototype Option 2, then migrate to Option 1 once the contract is stable.
