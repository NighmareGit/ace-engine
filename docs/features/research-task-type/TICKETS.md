# TICKETS — ace-research-task-type

Source: DESIGN-S2.md (module map §2, hooks §8), GRILL-S4.md deltas
(authoritative amendments), PRD-A-set/PRD-R01.json (machine form).
Grain follows the run-3 lesson: single-file atoms wherever possible;
T07 is the only multi-file atom and pairs dispatch with the facade.

| ID | Title | Module | Deps | ~ln |
|---|---|---|---|---|
| T01 | research schema — additive DDL | engine/state.py | — | 60 |
| T02 | VerdictGenerator + GeneratedVerdict | engine/research/verdict.py | T01 | 160 |
| T03 | sidecar post-processor | engine/research/sidecar.py | T02 | 120 |
| T04 | staged EvidenceValidator (7 checks) | engine/research/evidence.py | T03 | 220 |
| T05 | EvidenceScorer (LLM judge, threshold 6.0) | engine/research/scoring.py | T04 | 140 |
| T06 | file:// source verification | engine/research/sources.py | T04 | 90 |
| T07 | TaskHandler + dispatch + facade | engine/task_handler.py, engine/research/__init__.py, engine/engine.py | T02–T06 | 180 |
| T08 | ralph evidence gate + research round path | workflows/ralph/gates.py, report_types.py, round_driver.py | T07 | 110 |
| T09 | bad-verdict corpus + sensitivity test | tests/research/ | T04, T06 | 200 |
| T10 | registration hooks + dogfood readiness | prd.py, __init__.py, pipeline.py, prompts.py, README | T07 | 80 |

## Acceptance criteria (per ticket)

- **T01**: `init_db()` creates `research_verdicts` + `research_scores`
  idempotently on an existing engine.db; additive-only; full suite green.
- **T02**: marker parsing `[claim-N]`/`[src-N]` → `CitationRecord` edges;
  malformed markers skipped with warning; session-log row per LLM call
  (generator.py:82-95 capture pattern); researcher persona prompt.
- **T03**: verdict.md → schema-valid sidecar JSON deterministically;
  missing/ambiguous verdict position (supported/refuted/inconclusive +
  confidence) → hard error; zero network/LLM calls.
- **T04**: stages 0–5 mirror validator.py (short-circuit between stages,
  report-all within stage): 0 empty, 1 structure, 2 citation format,
  3 claim coverage, 4 sources (reachability, no orphans, no self-support,
  **claim→source AND source→source cycle checks**, claim density ≥2/1000),
  5 answer-the-question (TF-IDF via engine/intent/similarity.py) +
  contradiction resolutions. NO LLM imports.
- **T05**: 5 dims 0–10, overall = mean; judge transport + port guards +
  model-name separation mirror llm_judge.py; `EVIDENCE_PASS_THRESHOLD =
  6.0`; scorer never sets `passed` (ADR-0004); persists to
  research_scores; ONE retry on transient.
- **T06**: file:// sources — path exists (traversal rejected: `..`, `~`,
  absolute outside root) + evidence span substring-matches content
  (whitespace-normalized); sampled 20%, escalate to 100% on failure.
  HTTP stays v2.
- **T07**: one `task_type` branch in engine.py; CodeTaskHandler
  behavior-identical (suite green); research path
  generate→validate→score→commit with retry ladder (1 regen with failing
  checks[] as error_ctx → judge-model escalation → T4 replan signature
  `("evidence", check_name)`); budget caps + session logs unchanged.
- **T08**: `VALID_GATES += "evidence"`; research sequence =
  Evidence→Review exactly; code sequence unchanged; round driver selects
  by task type; gate JSON serializes.
- **T09**: 10 corpus cases (hollow, hallucinated citation, cyclic claims,
  self-support, silent contradiction, unresolved contradiction, bad JSON,
  zero edges, valid, valid-but-low-quality); structural cases rejected
  naming the failing check; valid passes; **mutation sensitivity ≥90%**.
- **T10**: `CATEGORY_TO_TYPE["research"]`; `task_role()` → researcher
  (max_tokens 8192); `evidence_check` retry key; prompts research branch;
  README with verdict contract; `ace research` thin CLI (single atom from
  PRD JSON, mock-transport e2e test).

## Dependency waves

- **W1**: T01, T10-registry-part (category map) — parallel
- **W2**: T02 → T03 → T04; then T05, T06 parallel
- **W3**: T07 (integration seam)
- **W4**: T08, T09, T10-remainder — parallel
