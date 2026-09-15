# PRD: Skills & Personas v1 — Skill Lifecycle, Effectiveness Telemetry, Persona System

Source epic: `docs/features/skills-personas/EPIC.md` (authoritative for
motivation, requirements traceability, module map). ADRs: 0002 (LLMs edit
task definitions, never code — skills are advisory context, not executable),
0004 (Ralph gates consume LLM Judge — skill effectiveness telemetry is
pipeline-context-dependent). Glossary: `CONTEXT.md` (skill, persona,
failure_class, task_type, injection, effectiveness).

## Problem Statement

The ACE engine's skill system is a fire-and-forget injection: `SkillStore`
loads markdown docs from `engine/intent/skills/`, matches by literal
`skill_id`, and appends the body to the generation prompt
(`engine/intent/skills.py:145-150`). There is no lifecycle around that doc —
no validation gate, no measurement of whether the skill changed the outcome,
no way to retire a skill that correlates with failures. The entire registry
is four docs (two wave-0 seeds + two pre-existing house procedures). G9
(skill-lookup miss) is a persistent systemic signal (ledger 43).

The persona system is equally thin. Worker prompts are assembled from 7
per-`task_type` templates with a single override line each
(`prompt_compiler.py:509-535`). Wave-0's worker additions (DAG position,
siblings, exact file targets) are 5 ad-hoc lines appended to
`IMPLEMENT_TEMPLATE` — there is no composable persona layer, no
per-failure-class adaptation.

The ralph-loop epic (`EPIC-ace-meta-cognitive-ralph-loop.md` R6) projects
approved patterns to markdown skill-docs as a one-way export, but the
ingestion/validation side of that bridge is unspecified.

Specific gaps:

1. **No skill validation.** A skill doc containing a code fence, a
   prompt-injection pattern, or contradictory advice loads silently and
   poisons every future prompt it matches.
2. **No effectiveness telemetry.** `skill_lookups` records misses only
   (`skills.py:141-143`). There is no link between a skill that *was*
   injected and the outcome of that generation.
3. **No metadata matching.** `context_for_prompt(skill_id)` is a literal
   key match — no task-type routing, no failure-class routing.
4. **No persona composition.** Flat template + one override line. No
   failure-class layer, no stacking.
5. **No ralph->skill ingestion.** The export bridge's validation/dedup side
   is unspecified.

## Solution

Deepen `engine/intent/skills.py` with a load-time validation gate (R2) and
metadata-driven matching (`applies_to` / `failure_class` front-matter, R3).
Add a `skill_effectiveness` table (R1) that links every injection event to
its outcome via `session_logs` (OBS-01), with a kill-switch query (R7) that
flags skills correlating with failures. Add a `PersonaStore` (R5) mirroring
`SkillStore` and a composition layer in `prompt_compiler.py` (R4) that
stacks base + task-type + failure-class + skill. Add a
`pattern_to_skill()` ingestion pipeline (R6) for the ralph->skill export
bridge.

## User Stories

1. As an operator, I want every skill doc validated at load time (no code
   fences, no prompt-injection patterns, body within bounds), so that a bad
   skill cannot silently poison prompts.
2. As an operator, I want skills matched to tasks by metadata (task type,
   failure class), so that the right skill reaches the right task without
   manual skill_id wiring.
3. As an operator, I want a `skill_effectiveness` table linking each
   injection to its outcome, so that I can measure whether a skill actually
   helps.
4. As an operator, I want a kill-switch query that flags skills whose
   injection correlates with failures, so that harmful skills can be retired.
5. As the re-plan brain, I want the worker prompt to include a
   failure-class-specific persona fragment (e.g. targeting discipline for
   TARGETING errors), so that the worker adapts to the diagnosed failure.
6. As an operator, I want persona fragments loaded from composable markdown
   files (one per failure-class), so that persona evolution doesn't require
   prompt_compiler edits.
7. As the ralph loop, I want approved patterns ingested into the skill
   registry with validation + dedup, so that learned knowledge compounds
   without manual copy-paste.
8. As a test runner, I want `test_skills.py` to verify the validation gate
   rejects code-fence skills and accepts valid ones, so that the gate is
   regression-proof.
9. As a test runner, I want `test_personas.py` to verify the PersonaStore
   loads fragments and the composition layer stacks them correctly, so that
   persona assembly is regression-proof.
10. As the engine, I want skill effectiveness recording to be additive — no
    changes to the generation path's core logic — so that telemetry never
    breaks generation.
11. As the owner, I want the skill registry to grow beyond the 2 seed docs
    without manual gatekeeping, so that the G9 systemic signal resolves.
12. As an operator, I want the effectiveness kill-switch to have a minimum
    sample threshold (10 injections), so that noisy small-sample skills are
    not falsely flagged.

## Implementation Decisions

- **Skill validation gate (R2):** Run at `SkillStore._load_all()` time
  (`skills.py:64-73`). Checks: schema completeness (skill_id, title, purpose
  present), body length 100-8192 chars, no code fences, no prompt-injection
  regex patterns. Failing skills are skipped with a warning.
- **Metadata matching (R3):** Front-matter gains `applies_to: [task_type]`
  and `failure_class: [TARGETING | MISSING-SIBLING | LOGIC | MODEL-LIMIT]`.
  `match_for_task(task_type, failure_class)` filters by these fields,
  ranked by specificity. `max_skills_per_task` cap (default 3) prevents bloat.
- **Effectiveness telemetry (R1, R8):** New `skill_effectiveness` table in
  `engine.db`. Recording at injection point in `engine/generator.py`.
  Outcome populated post-validation by joining to `session_logs` (OBS-01).
- **Kill-switch (R7):** SQL query over `skill_effectiveness` computes
  per-skill pass rate. Flag threshold: 20% absolute gap, min 10 injections.
- **Persona composition (R4):** `prompt_compiler.py` `compile()` gains a
  `failure_class` context key. System prompt stacked: SYSTEM_BASE +
  task-type override + failure-class fragment + skill context.
- **PersonaStore (R5):** New `engine/intent/personas.py` mirroring
  `SkillStore` — loads markdown from `engine/intent/personas/`, each <=
  1000 chars, one per failure-class.
- **Ralph->skill ingestion (R6):** `pattern_to_skill()` reads approved
  `ralph_patterns` rows, validates against R2 gate, dedups by
  (skill_id, body hash), writes to `engine/intent/skills/`.

## Testing Decisions

- **What makes a good test:** Test external behavior (validation gate
  rejects bad skills, match_for_task returns right skills, effectiveness
  links injection to outcome), not implementation details.
- **Modules tested:** `engine/intent/skills.py`, `engine/intent/personas.py`,
  `engine/intent/skill_effectiveness.py`, `prompt_compiler.py`.
- **Prior art:** `tests/test_skills.py`, `test_session_log.py`.

## Out of Scope

- **LLM-drafted skills.** v1 = human-authored or ralph-mined + approved.
- **Embedding/TF-IDF skill retrieval.** Metadata matching only.
- **Real-time dashboard.** Kill-switch is a query, not GUI.
- **Changes to wave-0 code.** Owned by wave-0 agent.
- **Changes to OBS-01/03.** Owned by observability epic.
- **Changes to ralph pattern store.** Owned by ralph epic.

## Further Notes

- **Dependency on OBS-01/03:** SP-02 blocked on OBS-01. SP-01, SP-03, SP-04,
  SP-06 independent.
- **Dependency on wave-0 D5:** SP-03 deepens skills.py — coordinate merge.
- **Dependency on ralph stage (e):** SP-05 spec-only until ralph_patterns exists.
