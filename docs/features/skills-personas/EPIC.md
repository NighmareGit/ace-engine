# EPIC: Skills & Personas — Lifecycle, Effectiveness Telemetry, Persona System

Source: WAVE0-DESIGN.md (D4/D5), XRAY-AND-GRILL.md (Q6/Q7/Q8), capability
inventory (Phase A below), multi-frame ideation (Phase B). Supersedes no
prior epic — this is a new vertical layered ABOVE wave-0.

## Motivation

Wave-0 wires skills into generation and seeds two docs, but the skill system
is a *fire-and-forget* injection: a `SkillStore` loads markdown from a
directory, matches by `skill_id`, and appends the body to the prompt
(`engine/intent/skills.py:145-150`). There is no lifecycle around that doc —
no authoring gate, no validation, no measurement of whether the skill
actually changed the outcome, no way to retire a skill that correlates with
failures. The two seed skills (`multi-module-implementation`,
`file-targeting-discipline`) and the two pre-existing house-procedure skills
(`extraction-playbook`, `runbook-variance-gate`) are the *entire* registry.
G9 (skill-lookup miss) is a persistent systemic signal (ledger 43).

The persona system is equally thin. Worker prompts are assembled by
`prompt_compiler.py` from per-`task_type` templates (7 types: implement,
refactor, fix, test, config, migrate, document) with a single
`_SYSTEM_PROMPT_OVERRIDES` line per type (`prompt_compiler.py:509-535`). The
wave-0 worker additions (D4: DAG position, siblings, exact file targets) are
5 ad-hoc lines appended to `IMPLEMENT_TEMPLATE` — there is no composable
persona layer, no per-failure-class adaptation, no systematization. The
re-plan brain's diagnostic persona (D4: TARGETING | MISSING-SIBLING | LOGIC |
MODEL-LIMIT | UNRECOVERABLE classification) is a string patch prepended to
`replan_protocol.py`'s system prompt, not a structured layer.

The blind spots are structural:

1. **No skill lifecycle.** A skill doc is a markdown file an agent can drop
   into `engine/intent/skills/`. There is no validation that the skill is
   internally consistent, non-conflicting with existing skills, or free of
   prompt-injection patterns. A bad skill poisons *every* future prompt it
   matches — the same lesson as the ralph pattern library's ADR-0002 gate
   (`EPIC-ace-meta-cognitive-ralph-loop.md` R2/M3).
2. **No effectiveness telemetry.** `SkillStore.get()` writes a `skill_lookups`
   miss row (`skills.py:141-143`) but there is no link between a skill that
   *was* injected and the outcome of that generation. We cannot answer "did
   tasks carrying skill X pass more often than tasks without it."
3. **No skill matching beyond `skill_id`.** `context_for_prompt(skill_id)`
   is a literal key match. There is no metadata-driven matching (by task type,
   by failure class, by DAG shape), no ranking, no "which skill is relevant
   to this task" decision.
4. **No persona composition.** The worker persona is a flat template + one
   override line. There is no layering (base -> task-type -> failure-class ->
   skill), no per-failure-class adaptation for the re-plan brain beyond the
   wave-0 diagnostic string.
5. **No learning-loop bridge.** The ralph-loop epic projects approved
   patterns to markdown skill-docs as a one-way export (`EPIC-ace-meta-cognitive-ralph-loop.md`
   R6, stage (e)). The ingestion/validation side of that bridge is unspecified.

## Hard Requirements (testable)

- **R1.** A `skill_effectiveness` table in `engine.db` records every skill
   injection event: `skill_id`, `run_id`, `task_id`, `attempt`, `stage`,
   `was_injected` (bool), and a denormalized `outcome` (pass/fail/replan)
   populated post-validation.
- **R2.** A `skill_validation` gate runs at `SkillStore._load_all()` time
   (`skills.py:64-73`): schema completeness, body length bounds (100-8192 chars),
   no code-fence smuggling, no prompt-injection patterns. Failing skills are
   skipped with a warning.
- **R3.** Skill matching is metadata-driven via `applies_to` / `failure_class`
   front-matter. `SkillStore.match_for_task(task_type, failure_class)` returns
   matching skills ranked by specificity. Literal `skill_id` match preserved
   as fallback.
- **R4.** A persona composition layer in `prompt_compiler.py`: system prompt
   = SYSTEM_BASE + task-type line + failure-class fragment + skill context.
- **R5.** A `PersonaStore` loads persona fragments from `engine/intent/personas/`
   (one markdown file per failure-class, <=1000 chars).
- **R6.** A `pattern_to_skill()` ingestion pipeline: approved `ralph_patterns`
   rows -> validated skill docs in `engine/intent/skills/`.
- **R7.** A kill-switch query flags skills whose injection-correlated pass rate
   is >=20% absolute below baseline (min 10 samples).
- **R8.** Effectiveness telemetry depends on OBS-01 (`session_logs`) and
   OBS-03 (`generation_telemetry`) for outcome linking. Does NOT re-specify
   capture.

## Module Map

Prefer extending existing modules over new ones:

| Module | Role | Change |
|--------|------|--------|
| `engine/intent/skills.py` | SkillStore — loading, matching, validation | Deepen: add `match_for_task()`, validation gate, effectiveness recording |
| `engine/intent/personas.py` | **NEW** — PersonaStore mirroring SkillStore | New file (R5) |
| `engine/intent/skill_effectiveness.py` | **NEW** — recording + kill-switch analytics | New file (R1, R7, R8) |
| `prompt_compiler.py` | Prompt assembly — persona composition layer | Additive: failure-class layer + skill context injection |
| `engine/generator.py` | Generation chokepoint — skill injection | Additive: call `match_for_task` + record effectiveness |
| `engine/orchestrator/replan_brain.py` | Re-plan brain — pass failure_class to compiler | Additive: include diagnostic in compile context |
| `engine/intent/skills/` | Skill docs directory | + seeded persona-system skills |
| `engine/intent/personas/` | **NEW** — persona fragment docs | New directory |
| `engine/orchestrator/patch.py` | Re-plan patch actions (wave-0) | No change |
| `engine/orchestrator/replan_protocol.py` | Re-plan system prompt (wave-0) | No change |

## Deliverables

1. `engine/intent/skills.py` — deepened with validation + matching + effectiveness
2. `engine/intent/personas.py` — new PersonaStore
3. `engine/intent/skill_effectiveness.py` — new recording + kill-switch
4. `prompt_compiler.py` — persona composition layer
5. `engine/generator.py` — skill matching + effectiveness recording
6. `engine/orchestrator/replan_brain.py` — pass failure_class to compiler
7. `engine/intent/skills/` — + persona-system seed skill
8. `engine/intent/personas/` — seed fragments (targeting, missing-sibling, logic, model-limit)
9. `engine/intent/pattern_to_skill.py` — ralph->skill ingestion
10. `tests/test_skills.py` — validation, matching, effectiveness
11. `tests/test_personas.py` — PersonaStore + composition
12. `docs/features/skills-personas/PRD-v1.md`
13. `docs/features/skills-personas/TICKETS.md`

## Metrics

- **Skill coverage:** >=80% of implement/fix tasks match >=1 skill post-seeding.
- **Validation rejection:** 0% for existing seed skills (they must pass).
- **Effectiveness signal:** 1:1 skill_effectiveness-to-session_logs for injected tasks.
- **Kill-switch precision:** >=20% absolute pass-rate drop, min 10 samples.
- **Persona composition:** 100% correct failure-class fragment by construction.

## Non-Goals

- **No LLM-drafted skills.** v1 = human-authored or ralph-mined + approved.
- **No embedding/TF-IDF skill retrieval.** Metadata matching only.
- **No changes to wave-0 code.** Builds ABOVE, doesn't contradict.
- **No changes to ralph pattern store.** Owns only ingestion side.
- **No changes to OBS-01/03.** Depends on them, doesn't re-specify.
- **No prompt bloat budget enforcement.** Future ledger item.

## Explicit Dependencies

| Dep | Why | Ticket |
|-----|-----|--------|
| OBS-01 (#26) `session_logs` | Outcome linking | R8 |
| OBS-03 (#28) `generation_telemetry` | Normalization | R8 |
| R6 ralph pattern projection | Ingestion source | R6 |
| Wave-0 D5 skills wiring | Prerequisite (in flight) | — |

## Traceability

| Phase A Gap | Requirement | Ticket |
|-------------|-------------|--------|
| No skill validation | R2 | SP-01 |
| No effectiveness telemetry | R1, R7, R8 | SP-02 |
| No metadata matching | R3 | SP-03 |
| No persona composition | R4, R5 | SP-04 |
| No ralph->skill ingestion | R6 | SP-05 |
| No persona fragments | R5 | SP-06 |
| Empty registry (G9 systemic) | R3, R6 | SP-07 |
| No kill-switch | R7 | folded into SP-02 |

## Phase A — Current-State Inventory

### Skills today

| What | Where | State |
|------|-------|-------|
| `SkillStore` — load/match/context_for_prompt | `engine/intent/skills.py:50-158` | Exists; literal `skill_id` match only |
| Front-matter parser | `skills.py:75-130` | Exists |
| `DEFAULT_SKILLS_DIR` | `skills.py:28` | `engine/intent/skills/` |
| `skill_lookups` table (G9 telemetry) | `skills.py:165-204` | Exists; misses only |
| 4 skill docs | `engine/intent/skills/` | 2 wave-0 seeds + 2 pre-existing |
| Wave-0 wiring into generator | `engine/generator.py` (D5) | In flight on main |
| Skill validation gate | — | **MISSING** (R2) |
| `match_for_task()` metadata matching | — | **MISSING** (R3) |
| `skill_effectiveness` table | — | **MISSING** (R1) |
| Effectiveness kill-switch | — | **MISSING** (R7) |

### Personas today

| What | Where | State |
|------|-------|-------|
| `SYSTEM_BASE` | `prompt_compiler.py:177-201` | Exists; generic |
| 7 per-task-type templates | `prompt_compiler.py:203-495` | Exists |
| `_SYSTEM_PROMPT_OVERRIDES` | `prompt_compiler.py:509-535` | Exists; flat |
| Wave-0 worker additions (D4) | `prompt_compiler.py` | In flight |
| Wave-0 re-plan diagnostic (D4) | `replan_protocol.py` | In flight |
| `PersonaStore` | — | **MISSING** (R5) |
| Persona fragment docs | — | **MISSING** (R5) |
| Failure-class persona layer | — | **MISSING** (R4) |
| Composition layers | — | **MISSING** (R4) |

### Gap table

| Capability | Exists? | Gap severity |
|------------|---------|--------------|
| Skill loading | Yes | — |
| Skill injection | Yes (wave-0) | — |
| Skill validation | No | HIGH |
| Metadata matching | No | MEDIUM |
| Effectiveness measurement | No | HIGH |
| Skill kill-switch | No | MEDIUM |
| Persona fragments | No | MEDIUM |
| Persona composition | No | MEDIUM |
| Ralph->skill ingestion | No | LOW |

## Phase B — Ideation Summary

### F1 — Authoring frame
Skills dropped into a directory, no gate. Validation gate (R2) at load time.
HIGH value, LOW cost. v1.

### F2 — Matching frame
Literal skill_id match. Add applies_to/failure_class front-matter +
match_for_task(). HIGH value, LOW cost. v1.

### F3 — Effectiveness frame
skill_lookups records misses only. Add skill_effectiveness table + kill-switch.
HIGH value, MEDIUM cost. v1 but gated on OBS-01.

### F4 — Persona frame
Flat template + one override line. Add PersonaStore + composition layer.
MEDIUM value, LOW cost. v1.

### F5 — Learning-loop frame
Ralph exports to skill-docs. Add pattern_to_skill() ingestion with validation
+ dedup. MEDIUM value, LOW cost. Depends on ralph stage (e).

### F6 — Red-team
- Skill staleness: R2 + R7 mitigate.
- Prompt bloat: max_skills_per_task cap + Rails.max_context_tokens.
- Conflicting skills: R2 + R7.
- Overfitting: R7.
- Telemetry feedback loops: advisory-only principle (skills are context).
