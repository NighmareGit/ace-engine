# S1 ANALYSIS: meta-cognitive-ralph-loop skill

**Phase:** S1 ANALYZE (code-review of the skill)  
**Date:** 2026-09-04  
**Source:** `/home/<user>/projects/skunkworks-toolbox/skills/meta-cognitive-ralph-loop/` (v2.0.0)  
**ACE threat model reference:** `engine/state.py`, `engine/engine.py`, `engine/orchestrator/budget.py`, `engine/orchestrator/stop.py`, `engine/intent/sandbox.py`, `engine/intent/laneb.py`, `engine/intent/skills.py`

---

## 1. Loop invariants — what the skill actually guarantees

| # | Invariant | Mechanically enforced? | Evidence |
|---|-----------|----------------------|----------|
| 1 | **Fresh/serial dispatch** — each Developer task dispatched one at a time via `subagent` | **No — convention only.** SKILL.md Phase 3 says "Iterate through Atomic Tasks one by one" (line 154) but the JSON output contract (`"phase": "DEVELOPMENT"`, line 246) carries no harness-level serialization lock. The harness `execution_guide.md §2` (line 12-17) says "look up target_agent, inject prompt, construct payload" — but nothing prevents parallel dispatch if the orchestrator emits multiple DEVELOPMENT blocks. |
| 2 | **Mandatory gates Dev→Review→Test→RedTeam** | **No — instruction-following only.** SKILL.md line 57 states "Every iteration MUST pass Development -> Code Review -> Testing -> Red-Teaming" but the JSON contract (lines 243-254) has a `gate_status` field the orchestrator *self-reports*. No harness gate rejects a `"phase": "RED_TEAM"` that skipped REVIEW. The execution_guide §3 (line 20) says "prompt: Evaluate the sub-agent's output. Update gate_status to 'passed' or 'failed'" — the orchestrator grades its own homework. |
| 3 | **Retry caps (3) per gate** | **No — convention.** SKILL.md lines 155, 159, 163 say "Max 3 retries" but the retry_count is tracked in the orchestrator's context window (a variable, not a harness counter). execution_guide §3 line 21: "If retry_count reaches 3... inject: Max retries reached" — relies on the LLM counting correctly. No mechanical ceiling. |
| 4 | **ITERATION_LIMIT** | **No — variable the orchestrator tracks itself.** SKILL.md line 76: `CURRENT_ITERATION: 0` is a context variable. Phase 7 (line 189): "If CURRENT_ITERATION < ITERATION_LIMIT... Return to Phase 1." The LLM increments its own counter. No harness enforcement. |
| 5 | **JSON-only output contract** | **Partial — convention + parsing.** SKILL.md lines 195-268 define the JSON schema for each phase, and execution_guide §1 (line 7) says "Extract agent_roster" — but if the LLM emits prose, the harness has no mechanical rejection (no JSON-schema validator documented in the guide). The bridge scripts (query.py, persist.py) do validate JSON on *their* inputs, but the orchestrator's own output is unvalidated. |
| 6 | **Pattern-library persistence across iterations/projects** | **Yes — mechanical via SQLite.** `persist.py` (lines 61-78) writes to SQLite via `pattern_library.add_patterns_from_updates()` (pattern_library.py lines 100-143). `query.py` reads back via `get_relevant_patterns()` (pattern_library.py lines 146-175). The DB file is `pattern_library.db` next to the module (line 22). This is the *only* mechanically enforced invariant — but the *content* is LLM-written (see §3d). |

**Summary:** Of the six claimed invariants, only #6 (persistence) is mechanically enforced by code. The rest (#1-5) are instruction-following conventions that a cooperative LLM will honor but a compromised/forgetful one can silently violate. ACE's state machine (`state.py` lines 39-52, `VALID_TRANSITIONS`) mechanically enforces its gate ordering — the skill has no equivalent.

---

## 2. Implicit human-only or harness-trust steps

Every place the skill assumes a cooperative harness or human:

| # | Trust assumption | Location | Risk if violated |
|---|-----------------|----------|------------------|
| 1 | **Prompt-injection of "return to Phase 2"** | execution_guide §3 line 21: "force the Orchestrator back to Phase 2 by injecting: 'Max retries reached. Return to Phase 2...'" | This is a *text prompt* injected into the LLM's context. A compliant LLM obeys; a stubborn one ignores it. No harness-level state reset. |
| 2 | **gate_status self-reporting** | SKILL.md line 252: `"gate_status": "pending|passed|failed"` — the orchestrator reports its own gate results. execution_guide §3 line 20: "Evaluate the sub-agent's output. Update gate_status..." | The orchestrator can lie. There is no independent verification that the Reviewer/QA/Red-Team agent actually passed. Contrast with ACE: `engine/validator.py` runs AST/import/style checks *deterministically*; `engine/tester.py` runs the actual test suite. |
| 3 | **Trust in sub_agent_templates system prompts** | sub_agent_templates.json — the four templates (Developer, Reviewer, QA, Red Team) are injected verbatim. The Reviewer template (line 10) says "Check for SOLID principles, security vulnerabilities" — but this is *instruction*, not enforcement. | An LLM can ignore "Output ONLY JSON" directives. The skill has no output validator that rejects non-JSON or schema-mismatched responses. ACE's `laneb_protocol.py` *parses* the code-gen response and rejects malformed output. |
| 4 | **vector_search/persist bridge trusting LLM-written JSON** | persist.py line 62: `updates = json.loads(raw)` — then `add_patterns_from_updates()` inserts into SQLite. The *content* of `description`, `proposed_action` etc. is whatever the LLM wrote. | SQL injection is parameterized (pattern_library.py line 86-91 uses `:named` params — safe). But *semantic* poisoning is unbounded: the LLM can write "Always use eval()" as a "successful strategy" and it gets persisted. |
| 5 | **/tmp file usage** | SKILL.md line 182: "write it to a temporary file (e.g., `/tmp/pattern_updates_<CURRENT_ITERATION>.json`)" | /tmp is world-writable. A concurrent process could read, modify, or race the file. ACE uses `/tmp` for stop-files too (`stop.py` line 15) but those are simple sentinels, not structured data. |
| 6 | **ITERATION_LIMIT as a variable the orchestrator itself tracks** | SKILL.md line 76: `CURRENT_ITERATION: 0` — incremented by the LLM's own reasoning. | A forgetful LLM never increments → infinite loop. A malicious LLM sets it to ITERATION_LIMIT to exit early. ACE's `budget.py` (lines 17-47) tracks counters in SQLite via `session.increment_llm_calls()` — the engine, not the LLM, owns the tally. |
| 7 | **Harness appends sub-agent output and prompts for evaluation** | execution_guide §3 line 20: "Append the sub-agent's output to the Orchestrator's context and prompt: 'Evaluate...'" | The harness is assumed to *correctly* capture and relay output. If the sub-agent output is truncated, the evaluation is corrupted. No checksum or length validation. |
| 8 | **Red-team "Critical" severity → return to Phase 3** | execution_guide §4 line 25: "If any vulnerability has severity 'Critical' or 'High'..." | The severity label is assigned by the Red-Team LLM. It can under-report ("Low" for an SQLi) and the loop proceeds. ACE's judge (`judge.py`) uses a separate model with a rubric — still LLM-based, but at least a *different* model. |
| 9 | **Cross-project pattern library pre-loading** | execution_guide §5 line 31: "pre-load the PATTERN_LIBRARY variable with the contents of this file" | Assumes the harness reads the file and injects it. If the file is corrupted or stale, the orchestrator gets bad lessons. No versioning or integrity check on the DB. |

---

## 3. Security/reliability gaps vs ACE's threat model

ACE's threat model (from code):
- **All LLM output is untrusted** → sandboxed execution (`sandbox.py`: Docker + seccomp + no-network + read-only rootfs + cap-drop ALL, lines 20-25)
- **T8 budget caps** over all LLM calls (`budget.py`: llm_calls, total_tokens, wall_clock — checked before each task and after each call, engine.py lines 117-137)
- **T5 stop/timeout** shared CANCELLED path (`stop.py`: per-run stop-file + global kill switch)
- **Additive-only engine.db schema** (`state.py` line 162-166: `ALTER TABLE ... ADD COLUMN` — no destructive migrations)
- **ADR-0002: LLMs edit task definitions never engine code** (laneb.py line 5, orchestrator/__init__.py line 5, patch.py line 4)

### Mapping each skill mechanism:

#### (a) The skill's "self-improving" claim (pattern library) under ADR-0002

**Gap — CRITICAL.** The skill advertises "self-improving" via the pattern library (SKILL.md line 68: "META-COGNITION: You must reflect on your performance, learn from failures, and update your strategy"). The pattern library stores free-text `description` and `proposed_action` strings written by the LLM (pattern_library.py lines 74-82). These are then *injected into future agent system prompts* (execution_guide §5 line 31).

Under ADR-0002, "LLMs edit task definitions, never engine code." The pattern library is effectively **task-definition editing by an LLM** — it modifies how future agents behave. The pre-epic doc (PRE-EPIC line 158-160) explicitly flags this: "the skill advertises 'self-improving' — in ACE terms that must mean *task definition* improvements only (ADR-0002), never engine code edits. Make it a mechanical gate, not a convention."

**What breaks:** The pattern library has no validation, no human review gate, no scope limit. A single bad run can inject "Always use `eval()` on user input" as a "successful strategy" that contaminates all future runs across all projects.

**What's needed:** A mechanical gate — e.g., pattern-library entries must be validated against a schema, rate-limited, and require human approval before injection. Or: patterns are *advisory* (read-only context) not *executable* (system-prompt injection).

#### (b) Who enforces gates if the orchestrator is an LLM that can lie about gate_status?

**Gap — HIGH.** The skill's gate structure (Dev→Review→Test→RedTeam) is the *core reliability claim*. But the orchestrator self-reports `gate_status` (SKILL.md line 252). The execution_guide §3 (line 20) says the harness prompts "Evaluate the sub-agent's output. Update gate_status" — the same LLM that dispatched the agent now judges it.

**Contrast with ACE:** ACE separates concerns:
- `validator.py` — deterministic AST/import/style checks (not LLM)
- `tester.py` — runs actual test suite (not LLM)
- `judge.py` — separate LLM model (Qwen3.5-9B on 3070) with rubric
- `oracle_escalate.py` — P6 oracle for exhausted tasks

The skill has *no* deterministic gate. All four gates (Dev, Review, Test, Red-Team) are LLM judgments by the same model in the same context.

**What breaks:** A hallucinated "passed" gate lets broken code through. A lazy LLM marks everything "passed" to finish faster.

**What's needed:** At minimum, the Review and Test gates need deterministic backing (ACE's validator + tester). The Red-Team gate needs a *separate* model (ACE's judge or oracle). The `gate_status` field must be set by the harness based on *actual* test execution, not LLM self-report.

#### (c) 3-retry + phase-return loops vs ACE T8 caps — can the loop exceed budget?

**Gap — HIGH.** The skill has nested retry loops:
- Per-task: 3 retries (Dev) → return to Phase 2 (replan) → 3 more retries
- Per-gate: 3 retries (Review) / 3 retries (Test) / 3 retries (Red-Team)
- Outer loop: ITERATION_LIMIT rounds, each round = full 7-phase cycle

A single iteration can burn: (3 Dev + 3 Review + 3 QA + 1 Red-Team + 1 Meta-Cognition) = 11+ LLM calls *per atomic task*, and there can be multiple tasks per iteration. With ITERATION_LIMIT=10, that's 110+ calls *minimum* — and the phase-return loops (Dev fails 3× → replan → Dev fails 3× again) multiply this further.

**Contrast with ACE:** ACE's `budget.py` (lines 17-47) enforces hard caps on `llm_calls`, `total_tokens`, and `wall_clock_s`. The engine checks *before each task* and *after each LLM call* (engine.py lines 117-137, 497-519). Exceeding any cap triggers CANCELLED via the same path as the stop-file.

**What breaks:** The skill has *no* token/call budget. ITERATION_LIMIT only bounds rounds, not cost. A single round that hits 3-retry loops on 5 atomic tasks can burn 150+ LLM calls. The pre-epic (line 138-139) flags this: "how do T8 caps and depth ≤2 interact with a loop that is *designed* to iterate?"

**What's needed:** The skill needs a `max_llm_calls` / `max_total_tokens` cap that the *harness* (not the LLM) enforces. ACE's T8 infrastructure already exists — the Ralph loop must plug into it, not replicate it with a weaker self-tracked variable.

#### (d) The pattern_library SQLite as a poisoning vector

**Gap — MEDIUM-HIGH.** `pattern_library.py` line 22: `DEFAULT_DB_PATH = Path(__file__).resolve().parent / "pattern_library.db"` — a single SQLite file shared across *all* projects and *all* runs (execution_guide §5 line 31: "Cross-Project Reuse").

**Attack surface:**
1. **Cross-project contamination:** A poisoned pattern from project A (e.g., "Use `md5` for password hashing") becomes a "lesson" injected into project B's agent prompts.
2. **No integrity protection:** The DB has no HMAC, no signing, no audit trail of who wrote what. `add_pattern()` (line 56-97) blindly inserts.
3. **No expiration or TTL:** Patterns accumulate forever. A transient failure mode (e.g., "Tests flaky on Tuesdays → skip them") becomes permanent.
4. **Query is substring match:** `get_relevant_patterns()` (line 146-175) uses `LIKE %keyword%` — a pattern written for one context matches unrelated contexts.
5. **/tmp staging:** The JSON is written to `/tmp/pattern_updates_<n>.json` (SKILL.md line 182) before persist — a race condition or symlink attack could inject arbitrary patterns.

**Contrast with ACE:** ACE's `engine.db` is additive-only (state.py line 162-166), has backup/restore (`_backup_db()` line 77-79, `_validate_db()` line 83-90), and WAL mode. The pattern library has none of these.

**What's needed:** Per-project pattern namespaces, content validation (no code in `proposed_action`), TTL/expiry, integrity signing, and removal of /tmp staging (use pipes — persist.py already supports stdin via `-`).

---

## 4. Mapping table — skill concept → ACE concept (or new)

| Skill concept | ACE concept | Mapping notes |
|---------------|-------------|---------------|
| **Iteration** (outer loop) | **Run** (`engine_runs` table, state.py line 107) | One Ralph iteration ≈ one ACE run. Both have a run_id, start/end time, and result. |
| **Phase 0: Scoping** | **PARSING** (State.PARSING, state.py line 15) | Both parse the input (PRD / PROJECT_GOALS) into a task list. ACE's `prd_parser.py` is deterministic; skill's scoping is LLM-driven. |
| **Phase 1: Firebrainstorming** | **New** (no ACE equivalent) | ACE has no divergent-ideation phase. Closest: T4 re-plan brain (`replan_brain.py`) but that's convergence, not divergence. *New capability for ACE.* |
| **Phase 2: Decomposition** | **QUEUED → CONTEXT** (State.QUEUED, state.py line 16) | Both break work into atomic tasks. ACE's decomposition is DAG-based (`task_queue.py`); skill's is flat list. |
| **Phase 3: Developer dispatch** | **GENERATE** (State.GENERATE, state.py line 18) or **Lane-B** (`laneb.py`) | ACE's `generator.py` calls BeeLlama; Lane-B runs code-as-intent in sandbox. Skill's Developer is a free-form subagent — closer to Lane-B but without sandbox enforcement. |
| **Phase 4: Code Review** | **VALIDATE** (State.VALIDATE, state.py line 19) | ACE's `validator.py` runs deterministic AST/import/style checks. Skill's Review is LLM-only. ACE's is strictly stronger. |
| **Phase 5a: QA Testing** | **TEST** (State.TEST, state.py line 20) | ACE's `tester.py` runs the actual test suite. Skill's QA is LLM-written tests — weaker (LLM can write tests that always pass). |
| **Phase 5b: Red-Team** | **Judge** (`judge.py`) or **P6 Oracle** (`oracle_escalate.py`) | ACE's judge is a separate model (Qwen3.5-9B) with a rubric. P6 oracle is the escalation path. Skill's Red-Team is the *same* model as the orchestrator — no independence. |
| **Phase 6: Meta-Cognition** | **Skill registry** (`skills.py`, currently empty → G9 signal) | The pattern library maps to ACE's skill-as-docs registry. Both are "lessons learned" stores. ACE's is markdown (human-readable, auditable); skill's is SQLite (machine-written, opaque). |
| **Phase 7: Loop Evaluation** | **NEXT → QUEUED** (State.NEXT, state.py line 22) | Both loop back for the next task/iteration. ACE's loop is bounded by T8 budget; skill's by ITERATION_LIMIT (self-tracked). |
| **gate_status** | **State transitions** (`VALID_TRANSITIONS`, state.py lines 39-52) | ACE's transitions are mechanically enforced by the pipeline. Skill's gate_status is LLM self-report. |
| **ITERATION_LIMIT** | **max_ralph_rounds** (new config, or reuse `max_llm_calls` from T8) | ACE already has `max_llm_calls`, `max_total_tokens`, `max_wall_clock_s` in `EngineConfig`. Ralph needs its own cap — `max_ralph_rounds` — plus integration with T8. |
| **JSON output contract** | **Typed events** (`engine/events.py`) + **tables** (`engine/state.py`) | ACE emits structured events (event_type, data dict) and persists to typed SQLite tables. Skill's JSON is schema-documented but unvalidated. |
| **Pattern library (SQLite)** | **engine.db** (`state.py`) + **skill registry** (`skills.py`) | Pattern library ≈ additive tables in engine.db. Cross-project reuse ≈ skill registry (currently empty, G9 signal). |
| **/tmp pattern files** | **Stop-files** (`stop.py`) | Both use /tmp. ACE's stop-files are simple sentinels; skill's are structured JSON — higher risk. |
| **sub_agent_templates.json** | **prompt_templates.py** (ACE's 7 task-type templates) | Both are prompt templates. ACE's are code-driven with auto-detection; skill's are JSON config. |

---

## 5. Verdict — top 5 gaps that MUST become epic requirements in S2/S4

| # | Gap | Rationale |
|---|-----|-----------|
| **1** | **Gate enforcement must be mechanical, not LLM self-report.** The skill's core reliability claim (Dev→Review→Test→RedTeam) collapses if the orchestrator can lie about `gate_status`. ACE's deterministic validator + tester + separate judge model must back each gate. | Without this, the "mandatory gates" are a convention that a single forgetful/hallucinating LLM can bypass entirely. This is the single largest security/reliability gap. |
| **2** | **Pattern library must be scoped as task-definition edits under ADR-0002 with a mechanical review gate.** The "self-improving" claim is LLM-written content injected into future agent prompts — cross-project contamination with no validation, no TTL, no integrity check. | ADR-0002 is an ACE invariant. The pattern library in its current form *violates the spirit* of ADR-0002 (LLM editing behavior that persists and propagates). Must be: human-approved, per-project namespaced, or downgraded to advisory context only. |
| **3** | **Budget accounting must be harness-enforced (T8), not LLM-tracked.** ITERATION_LIMIT is a context variable the LLM increments itself. Nested retry loops (3× per gate × phase-return) can blow past any reasonable budget. ACE's `budget.py` counters must bound the Ralph loop. | The pre-epic (line 138-139) explicitly flags this. Without harness-enforced caps, a single Ralph run can exhaust the entire engine budget, starving other runs. |
| **4** | **Red-Team must use an independent model (ACE judge or P6 oracle), not the orchestrator itself.** The skill dispatches Red-Team to the same LLM that wrote the code — an attacker reviewing their own homework. | ACE already has a judge model (Qwen3.5-9B on 3070) and an oracle escalation path (P6). The Ralph loop must route Red-Team through one of these, not through the orchestrator's own context. |
| **5** | **Fresh-agent execution must use ACE's sandbox (Docker + seccomp), not bare subagent dispatch.** The skill dispatches Developer agents via `subagent` with no isolation guarantee. ACE's `sandbox.py` (Docker + no-network + read-only + cap-drop ALL) is the boundary for untrusted code. | The skill's generated code is untrusted (ACE threat model). Running it without sandboxing means a prompt-injected `rm -rf /` in a "successful strategy" pattern gets executed on the host. Lane-B already enforces Docker-only (laneb.py line 21-25); Ralph must inherit this. |

### Surprises found in reference files

1. **`persist.py` already supports stdin** (line 5, 49-54): `echo '...' | python3 -m meta_cognitive_ralph_loop.persist -`. The skill's /tmp staging (SKILL.md line 182) is unnecessary — the bridge can read from a pipe, eliminating the /tmp race condition entirely. This is a quick win for S2.

2. **`pattern_library.py` uses parameterized SQL** (line 86-91): SQL injection is *not* the risk. The risk is *semantic* poisoning — the library correctly stores whatever garbage the LLM writes. The security fix is content validation, not query sanitization.

3. **`red_team_router.py` is keyword-matching, not LLM-driven** (lines 85-107): The red-team specialization is a simple substring scan. This is actually *more* deterministic than the rest of the skill — but it means the "specialization" is just prompt selection, not model routing. In ACE terms, this maps to prompt template selection, not to the judge/oracle escalation.

4. **The skill's `CURRENT_ITERATION` is never persisted** (SKILL.md line 76): It lives only in the orchestrator's context window. If the orchestrator crashes or context-compacts, the counter resets. ACE's `checkpoint_run()` (state.py line 180-218) persists state to SQLite with backup — the Ralph loop needs equivalent checkpointing.

5. **Divergent ideation (Phase 1) has no ACE equivalent**: ACE's pipeline is purely convergent (parse → generate → validate → test). The skill's "generate 10+ diverse solutions, cross-pollinate, score" is genuinely novel for ACE. The pre-epic (line 17-19) correctly identifies this as a capability ACE lacks. S2 should decide: is this a new state, a Lane-B extension, or a workflow over existing states?

---

## Appendix: File/line reference index

| File | Key lines | What it shows |
|------|-----------|---------------|
| `skill-source.md` (SKILL.md) | 57, 76, 154-163, 182, 189, 195-268 | Gate structure, ITERATION_LIMIT, /tmp usage, JSON contract |
| `execution_guide.md` | 7-21, 25, 31 | Harness trust assumptions, gate_status self-reporting, cross-project reuse |
| `sub_agent_templates.json` | 1-22 | Four agent templates — instruction-only, no enforcement |
| `pattern_library.py` | 22, 36, 56-97, 100-143, 146-175 | SQLite schema, insert, query — parameterized but content-blind |
| `persist.py` | 49-54, 62-78 | Stdin support, JSON parse, insert |
| `query.py` | 44-57 | CLI read bridge |
| `red_team_router.py` | 85-107 | Keyword-matching specialization (deterministic) |
| `engine/state.py` | 13-25, 39-52, 107-177 | State enum, VALID_TRANSITIONS, engine_runs schema |
| `engine/engine.py` | 117-137, 497-519 | T8 budget checks, T5 stop-file integration |
| `engine/orchestrator/budget.py` | 17-47 | Hard budget caps (llm_calls, tokens, wall_clock) |
| `engine/orchestrator/stop.py` | 15, 22-41 | Stop-file mechanism (per-run + global) |
| `engine/intent/sandbox.py` | 20-25 | Docker + seccomp + no-network sandbox |
| `engine/intent/laneb.py` | 1-35 | Lane-B: code-as-intent, depth ≤2, ADR-0002 |
| `engine/intent/skills.py` | 1-30, 139 | Skill registry (empty → G9), additive telemetry |
| `PRE-EPIC-ace-meta-cognitive-ralph-loop.md` | 128-160 | Open questions, ADR-0002 non-goal, self-improvement guardrail |
