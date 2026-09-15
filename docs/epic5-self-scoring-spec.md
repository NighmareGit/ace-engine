# Epic-5 Self-Scoring & Benchmark Integration — Design Spec

Status: design-gate approved for implementation
Branch: `epic5-self-scoring` · Created: 2026-09-09
Source: `design_drafts/EPICS.md` §Epic 5 (ACs 5.1–5.3), issue 11,
issue-02 takeaways, handoff evidence (T08 truncation, REAP variance).

## 0. Non-negotiables carried in

- Rule 6: **no writes ever to `benchmark-results.db`**. Tripwire found: top-level
  `judge.py:12` defaults `DB_PATH` to `benchmark-results.db` — the engine-side
  adapter must pass the engine.db path explicitly AND guard against the default.
- Judge = :8080 (Qwen3.6-35B), subject = :8082. Judge never the model under test.
  Endpoints pinned explicitly, never defaulted (deployed-copy divergence lesson).
- Retry discipline: judge calls get max ONE retry, transient failures only.
  A bad score is final data, never a reroll trigger.
- Simulation-first: full scoring path must work with mock transport / dry-run
  before any live traffic.

## 1. Two judges, two tables (reconciling spec vs existing code)

EPICS.md names an `engine_scores` table; the repo already has `judge_verdicts`
(run-level, lazily created). Resolution — keep both, distinct roles:

| | `judge_verdicts` (existing) | `engine_scores` (new, AC5.1) |
|---|---|---|
| Level | per run | per task |
| Scorer | `engine/judge.py` — deterministic offline gate (success/tasks/commit/tokens/error_free) | NEW `engine/llm_judge.py` — LLM 5-dim scorer (completeness, correctness, quality, intelligence, role_fit) via judge model |
| Writes | run_id, overall_pass, score, dimensions JSON | run_id, task_id, judge_model, 5 dims, overall (generated mean), reasoning, tokens, scored_at |
| Purpose | pipeline pass/fail gate | quality telemetry for CCBS ranking |

`engine.db` only. AC5.1's `SELECT * FROM engine_scores` verification is satisfied.

## 2. `engine/llm_judge.py` — LLM 5-dimension scorer

- `score_task(run_id, task_result, code, ctx, config, transport) -> dict`
- Prompt: reuse top-level `judge.py`'s `JUDGE_PROMPT_TEMPLATE` + rubric anchor
  extraction (import, don't duplicate). Blinding: strip model identity from code.
- Judge call: `transport.curl_beellama(port=8080, max_tokens=1024, temperature=0.0)`
  — explicit port, one retry on transient (timeout/5xx/JSON-parse), then a final
  zero-score verdict with `error` recorded. Never silent-fallback to another model.
- Output parsing: JSON with 5 integer dims 0–10; malformed JSON after retry ⇒
  all dims 0 + `"parse_error"` reasoning (final data, not reroll trigger).
- Config gate: refuses to run when `config.subject_port == config.judge_port`
  (judge must never be the model under test).

## 3. Scoring config (handoff evidence applied)

`EngineConfig` extensions:

```python
judge_mode: str = "off"            # off | deterministic | llm | full (both)
judge_port: int = 8080             # pinned, explicit
subject_port: int = 8082
max_tokens_by_role: dict = field(default_factory=lambda: {
    "orchestrator": 4096,   # T08 evidence: 2048 truncated long-form answers
    "multi-turn": 4096,
    "coder": 2048,
})
judge_temperature: float = 0.0
```

`generator.generate_code` consumes role-aware max_tokens (task.role from PRD parse;
default "coder" when unknown). `--judge` CLI flag maps to `judge_mode="full"`.

## 4. Telemetry completeness (AC5.3)

`TaskResult`/`RunResult` gain: `prompt_tokens`, `completion_tokens`,
`thinking_tokens`, `tokens_per_sec` (all 0.0/0 when the transport response lacks
timings — BeeLlama provides `usage.total_tokens` + `timings.*`; populate when
present, never fabricate). `wall_clock_s` = existing `time_s`/`total_time_s`
aliased in the report.

`RunReportSchema` (contract, test-enforced): a run report dict MUST contain
run_id, config, prd_path, success, wall_clock_s, total_tokens,
prompt_tokens, completion_tokens, thinking_tokens, tokens_per_sec,
per_task: [{task_id, state, attempts, commit_sha, time_s, tokens,
scores{5 dims}, overall}], judge_verdict (deterministic), error_message.

## 5. `Engine.run_matrix` (AC5.2) + variance (REAP lesson)

```python
Engine.run_matrix(configs: list[str], prds: list[str], repetitions: int = 1,
                  dry_run: bool = False) -> dict
```

- Executes configs × prds × repetitions via `self.run(...)` (model_config swept
  through EngineConfig). Returns JSON-serializable dict:
  `{matrix: [RunReport...], summary: {per (config, prd): {scores_mean,
  scores_std, pass_rate, tokens_mean, wall_clock_mean}}, schema_version: 1}`
- **Variance is a first-class output** — `scores_std` per cell; single-run scores
  on unstable models are flagged (`repetitions < 3 ⇒ "variance": "insufficient"`),
  never presented as a reliable mean.
- JSON-serializability is itself unit-tested (AC5.2 verification).

## 6. RunSummary (AC5.4 story)

`run_matrix` summary + per-run `RunReport` together satisfy the "one artifact"
requirement; `Engine.run()` additionally emits `run_summary` via `emit_event`.

## 7. Implementation order (TDD)

1. `engine_scores` DDL in `state.py` + `save_engine_scores()` (test: lazy create,
   Rule-6 isolation — assert zero writes to benchmark-results.db)
2. `engine/llm_judge.py` scorer with mock transport (tests: parse ok, parse
   fail ⇒ zeros+error, one-retry cap, port-mismatch refusal)
3. Role-aware max_tokens through generator (test: orchestrator gets 4096)
4. Telemetry fields plumbed from transport responses (test: present, numeric)
5. `run_matrix` + variance + schema validation tests (all offline/mock)
6. Simulation-first dry-run on Triton, then ONE live judged run (tiny PRD,
   1 task) → variance report
7. `ace-0.1a` gate discussion

## 8. Out of scope (this branch)

- CCBS-side consumption code (they get the run_matrix contract)
- Top-level `judge.py` benchmark-lane refactor (default-DB tripwire is *guarded
  against* here; fixing the module default is a separate hygiene commit)
- Epic-6 atom replay/backtesting
