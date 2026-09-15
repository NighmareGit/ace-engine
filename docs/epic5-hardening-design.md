# Epic-5 ACE Engine Hardening — Design

**Date:** 2026-09-09
**Source failures:** `research/epic5-first-run-failures-2026-09-09.md` (50 % live-run failure rate)
**Scope:** Harden the ACE engine against the three live-run failure modes + five cross-cutting gaps found in the root-cause analysis. Design only — no engine code modified.
**Style:** TDD (51 engine tests green); every interface below has a named test.

---

## 0. Verified ground truth (primary sources)

Before deciding, the report's claims were checked against the code. Findings that shape every decision below:

| # | Claim | Verified | Key location |
|---|-------|----------|--------------|
| V1 | `save_task_result` never called → `engine_task_results` empty | ✅ | `state.py:204` def; zero callers; cols `validation_result`/`test_result` exist (`state.py:118-119`) |
| V2 | Stage detail discarded on exhaustion | ✅ | `engine.py:282-283` records only a count |
| V3 | `generator.py:47` reads only `content`, no reasoning-exhaustion branch | ✅ | no `finish_reason` / `reasoning_content` handling |
| V4 | Inverted URL ternary + `ace-demo` default | ✅ | `committer.py:46-53`, default at `:49` |
| V5 | PERMANENT/retryable classification computed but ignored | ✅ | `committer.py:62-69` vs `engine.py:336-355` |
| V6 | Dead imports: `MAX_TOTAL_ATTEMPTS_PER_TASK`, `_recheck_transport`, `_with_timeout`, `TimeoutError`, `_checkpoint_lock` | ✅ | `engine.py:13` imported, grep shows no use |
| V7 | `state.py` has its own `DB_PATH`/`BACKUP_PATH` that ignore `ENGINE_DB_PATH`/`ENGINE_DB_BACKUP` | ✅ | `state.py:55-56` vs `paths.py:11-12` (the known wart) |
| V8 | **`emit_event` bridge is dead** — signature is `emit_event(on_event, event_type, data)` (3 args, `events.py:6`) but all 3 call sites call `emit_event("type", {...})` (2 args) | ✅ | `engine.py:149,298,366` — escalation path cannot fire today |
| V9 | Judge scored validation-failed code 8.2/10 | ✅ | report E.3; `engine.py:290-300` scores failed tasks that have files |

---

## 1. Results store wiring  [decided]

### Decision
- **Standard runtime DB path** = `ENGINE_DB_PATH=/home/<user>/ace-results/engine.db`. This is the canonical engine state location (Rule 6: never `benchmark-results.db`).
- **`state.py` must honor the same env overrides** as `paths.py`. Fix the wart: `state.py.DB_PATH`/`BACKUP_PATH` currently hardcode the repo-root path and ignore `ENGINE_DB_PATH`/`ENGINE_DB_BACKUP`. They must fall back identically: `os.environ.get("ENGINE_DB_PATH") or <repo-root>/engine.db`.
- **Archive repo** = the dedicated Gitea repo `<user>/ace-results` (already exists). Per run_id it receives a snapshot of `engine.db` **and** a JSON export.
- **Snapshot placement in lifecycle:** a single primitive `archive_run(run_id, project)` is called **automatically at the end of `Engine.run()`** (after DONE or FAILED), **and** re-exposed as a CLI command `ace archive <run_id>` for re-archives and ad-hoc autopsies. `run_matrix` calls `run()` per cell, so every cell is archived automatically.
- **What gets committed** to `<user>/ace-results/archive/<run_id>/`:
  - `engine.db` — faithful full-state snapshot (the engine DB as-of run end).
  - `report.json` — the JSON export (RunReport + per-task stages + scores + judge reasoning). Small, diffable, queryable without SQLite.
- **Bloat control:** `report.json` is retained forever (small). `engine.db` snapshots are retained under a **configurable cap** (default: latest 30 per project); older `.db` snapshots are pruned by `ace archive prune`. JSON is the durable artifact; `.db` is a convenience replay.
- **Autopsy pull:** `ace archive pull <run_id>` (and the programmatic `fetch_run(run_id)`) reads `archive/<run_id>/report.json` from the local clone of `<user>/ace-results` (falling back to the Gitea API if not cloned). Autopsies never need the `.db` unless replaying state machine transitions.

### Rationale
Single primitive + automatic call + CLI re-exposure means archiving can never be "forgotten" by a new caller, yet remains re-runnable for forensics. Dual artifacts (`.db` + JSON) give both fidelity and queryability; the cap prevents the `.db` snapshots from bloating the Gitea repo indefinitely (SQLite does not diff/merge in git).

### Interface sketch
```python
def archive_run(run_id: str, project: str,
                results_repo: str = "/home/<user>/ace-results",
                db_path: str | None = None) -> str:
    """Snapshot engine.db + write report.json into results_repo/archive/<run_id>/,
    commit, push. Returns the archive directory path. Idempotent per run_id."""

def fetch_run(run_id: str,
              results_repo: str = "/home/<user>/ace-results") -> dict:
    """Load archive/<run_id>/report.json. Falls back to Gitea API if not present locally."""

def prune_archives(project: str, keep: int = 30,
                   results_repo: str = "/home/<user>/ace-results") -> int:
    """Remove engine.db snapshots beyond the per-project retention cap. Returns count removed."""
```
`EngineConfig` gains `archive_results: bool = True` (opt-out for pure-local dry runs) and `archive_keep: int = 30`.

### Test plan
- T1.1 `state.py` honors `ENGINE_DB_PATH` (set env, assert `_get_conn` opens that path).
- T1.2 `archive_run` writes `archive/<run_id>/{engine.db,report.json}` and is idempotent.
- T1.3 `archive_run` called automatically at end of `Engine.run()` (spy).
- T1.4 `prune_archives` retains exactly `keep` newest `.db` per project, removes older.
- T1.5 `fetch_run` reads local JSON; falls back to Gitea API mock when absent.
- T1.6 Rule-6 tripwire: no write to `benchmark-results.db` during archive.

### Effort: M

---

## 2. Validator stage telemetry  [decided]

### Decision
- **Wire `save_task_result` into `_run_task`** — it exists, the columns exist, the call is missing.
- **Call it on every failed validation attempt** (not just the terminal one), so the per-attempt progression is captured (attempt 1 failed at `imports`, attempt 2 at `execution`, …). Also call it at terminal state (test result, commit sha).
- **No schema redesign.** `engine_task_results.validation_result` (TEXT) already exists — store `json.dumps(result.stages)` there. `test_result` stores the test-failure JSON. This is purely additive population of existing columns.
- **Surface the failing stage in `TaskResult.error_message`** on exhaustion: include `<stage> on <file>: <error>` from the last failed attempt, not just the attempt count. Add a `validation_stages: list[dict] | None` field to `TaskResult` (carried into the JSON export) so consumers see how far each attempt got.
- **Log** the failing stage at WARNING at the exhaustion point (`engine.py:282`).

### Rationale
The schema already supports it; the only bug is the missing call. Per-attempt capture (bounded by `max_retries_generate`, default 3) is cheap and turns "validation failed after 3 attempts" into a forensic trace. Adding `validation_stages` to `TaskResult` costs one field and makes the detail flow into `report.json` for free.

### Interface sketch
```python
# engine.py _run_task — inside the generate retry loop, after each validate():
save_task_result(pipeline.run_id, task.id, {
    "state": State.VALIDATE.value,
    "generated_code": "\n".join(code.files.values()),
    "validation_result": json.dumps(result.stages),   # existing column
    "attempts": attempt + 1,
    "error_message": _last_stage_error(result.stages),
})

# On exhaustion, TaskResult gains:
@dataclass
class TaskResult:
    ...
    validation_stages: list[dict] | None = None   # NEW — last failed attempt's stages
```
`_last_stage_error(stages)` returns `"imports on api/health.py: Import 'foo' not found…"` for the first failing stage.

### Test plan
- T2.1 After a failed validation, `engine_task_results` has a row with `validation_result` parseable as `[{stage,file,passed,error}]`.
- T2.2 Each failed attempt appends a row (3 failed attempts → 3 rows, ascending `attempts`).
- T2.3 Terminal `TaskResult.error_message` contains the stage name + file + error string.
- T2.4 `TaskResult.validation_stages` equals the last attempt's `result.stages`.
- T2.5 WARNING log emitted at exhaustion with the failing stage name.

### Effort: S

---

## 3. Reasoning-budget strategy  [decided]

### Decision
- **Adaptive with a static floor.** The role-aware static budget (`max_tokens_by_role`, currently 4096 for all roles) is the floor. On detecting **reasoning exhaustion**, escalate `max_tokens` ×2 (capped at 16384) for **exactly one** retry.
- **Exhaustion signature:** `content` is empty/whitespace **AND** `finish_reason == "length"` **AND** (`reasoning_content` non-empty **OR** `thinking_tokens` > 0). The transport returns all of these; `generator.py` currently reads only `content`.
- **Generation side:** detection + one escalate-retry live in the generate path. This is a *generation* retry (the pipeline's own concern), distinct from the scoring one-retry discipline.
- **Judge side:** the existing `_MAX_ATTEMPTS = 2` (one retry) **doubles as the escalation**. First judge call at `_MAX_TOKENS_JUDGE = 4096`; on reasoning exhaustion, retry once at 8192 (or 16384). If still exhausted → final data (zeros + `error`), never a reroll. This **fits** the one-retry discipline rather than violating it.
- **Explicit `finish_reason` handling:** `generator.py` must read `response.get("finish_reason")` and propagate it on `GeneratedCode` (new field `finish_reason: str`). Empty-content-without-exhaustion (genuine empty answer) is surfaced as a distinct `reasoning_exhaustion=False` empty-output case, not misreported as a validation failure.

### Rationale
Static 4096 handles the *known* case (9B-MTP spends ~650 reasoning tokens; clean stop at 4096). Adaptive escalation handles the harder/unknown case without re-tuning. Capping retries at one and folding the judge's escalation into its existing single retry respects the runbook's one-retry discipline on scoring. Reading `finish_reason` explicitly is the precondition for all of this and closes the truncation gap that currently misreports empty LLM output as "validation failed."

### Interface sketch
```python
@dataclass
class GeneratedCode:
    ...
    finish_reason: str = ""          # NEW — from response.get("finish_reason")

def _is_reasoning_exhaustion(response: dict) -> bool:
    """Empty content + finish_reason=length + (reasoning_content or thinking_tokens)."""
    content = response.get("content", "") or ""
    return (not content.strip()
            and response.get("finish_reason") == "length"
            and bool(response.get("reasoning_content")
                     or response.get("thinking_tokens", 0)))

# In _run_task generate loop: after a generation that exhausts, allow ONE escalate
# with min(max_tokens*2, 16384); if it exhausts again, surface distinct error.
```

### Test plan
- T3.1 `_is_reasoning_exhaustion` true/false over a table of synthetic responses.
- T3.2 `GeneratedCode.finish_reason` populated from transport response.
- T3.3 On exhaustion, generation retries once with doubled `max_tokens`; a second exhaustion surfaces `error_message` containing `"reasoning exhausted"` (not the generic validation message).
- T3.4 Escalation retry count ≤ 1 even with `max_retries_generate > 1`.
- T3.5 Judge: first call 4096, exhaustion → one retry at higher budget; still exhausted → zeros + error, no reroll.
- T3.6 Non-exhaustion empty content (finish_reason=stop, no reasoning) is NOT treated as exhaustion.

### Effort: M

---

## 4. Commit path hardening  [decided]

### Decision
- **Fix the inverted ternary** (`committer.py:50-51`) by replacing the whole URL block with a single correct form: always embed the token when present, never when absent. One line, no branching on token presence for the host part.
- **Replace the `ace-demo` default** by deriving the repo name from the project basename (`os.path.basename(project_path)`), overridable by `GITEA_REPO`. Fail fast with a clear error if it resolves to empty. (The live test repo `epic5-live-test` equals the project basename, so derivation removes the mismatch.)
- **Pre-flight repo existence:** fail fast by default with a clear error naming the repo and how to create it. Opt-in autocreate via `GITEA_AUTOCREATE=1` (uses the existing Gitea API `create_repo`). Autocreate is **off by default** — silently creating repos can mask config errors and litter Gitea.
- **Honor the PERMANENT/retryable classification** (`committer.py:62-69`): a retryable push failure re-enters the commit/generate loop (bounded by the existing commit retry counter); a PERMANENT failure fails the task immediately with the classification in `error_message`. Today `_run_task` ignores the classification entirely (`engine.py:336-355`).

### Rationale
The ternary is a pure logic bug; one correct expression removes both the inversion and the https-drop hazard. Deriving the repo name from the project basename matches the actual deployment convention and eliminates the `ace-demo` mismatch that caused the PERMANENT auth failure. Honoring the classification turns a computed-but-ignored signal into correct retry behavior. Autocreate-off-by-default is the safe posture for a write-path that pushes to a remote.

### Interface sketch
```python
def _build_origin_url(project_path: str) -> str:
    gitea_url = os.environ.get("GITEA_URL", "http://localhost:3000")
    gitea_user = os.environ.get("GITEA_USER", "<user>")
    gitea_token = os.environ.get("GITEA_TOKEN", "")
    repo_name = os.environ.get("GITEA_REPO") or os.path.basename(project_path)
    if not repo_name:
        raise ValueError("Cannot determine Gitea repo name: set GITEA_REPO or use a non-empty project path.")
    host = gitea_url.split("://", 1)[-1]
    auth = f"{gitea_user}:{gitea_token}@" if gitea_token else ""
    return f"http://{auth}{host}/{gitea_user}/{repo_name}.git"

def _ensure_repo_exists(repo_name: str, autocreate: bool) -> None:
    """Fail fast if repo missing, unless autocreate (GITEA_AUTOCREATE=1)."""
```
`CommitResult` gains `permanent: bool` so `_run_task` can branch on it.

### Test plan
- T4.1 `_build_origin_url` embeds token iff present; never embeds empty token; table-driven over {token×https×repo}.
- T4.2 Default repo name == `os.path.basename(project_path)`; `GITEA_REPO` overrides.
- T4.3 Missing repo + autocreate off → clear `ValueError`/fail-fast error naming the repo.
- T4.4 Missing repo + autocreate on → `create_repo` called (mock Gitea API).
- T4.5 Retryable push failure re-enters commit loop; PERMANENT fails task immediately; `error_message` carries the classification.
- T4.6 Commit retry bounded by existing commit retry counter (no infinite loop).

### Effort: M

---

## 5. Judge-steered retry + escalation policy  [decided]

This is the central design decision. Two sub-questions:

### 5a. Should the LLM judge's reasoning feed the regeneration loop?  [decided]

**No — not in the hot generate→validate loop.**

- The deterministic validator already yields precise, actionable stage errors (and with Topic 2 those now flow into the retry prompt via `ErrorContext`). The gate that drives retries stays deterministic, fast, and free.
- Inserting an LLM judge call per retry would multiply latency and cost by retries-per-task, couple generation speed to judge availability, and blur the deterministic-gate vs-LLM-telemetry separation.
- **Instead:** the LLM judge's reasoning is captured and **attached to the escalation event** (see 5b) as evidence for the orchestrator/autopsy. The deterministic gate and the LLM judge stay distinct: deterministic = gate; LLM = quality telemetry + escalation evidence.

### 5b. Escalation event contract  [decided]

When retries exhaust (validation, test, or commit), emit a **`task_escalation`** event over the `emit_event` bridge. The orchestrator subscribes via `Engine(on_event=…)` and decides (pause / run / human review). Fire-and-forget — escalation never blocks or crashes the pipeline.

**First, fix the dead bridge** (V8): `emit_event(on_event, event_type, data)` requires the callback as arg 0. All three current call sites pass 2 args. The corrected form is `emit_event(self.on_event, "type", data)`. This fix is in scope and is a prerequisite for escalation to work at all.

**Event payload JSON:**
```json
{
  "event_type": "task_escalation",
  "run_id": "run-1788818036",
  "task_id": "T01",
  "stage": "validation",
  "attempts": 3,
  "last_errors": ["Import 'foo' not found in stdlib or project"],
  "validation_stages": [
    {"stage": "empty", "file": "api/health.py", "passed": true, "error": null},
    {"stage": "ast",   "file": "api/health.py", "passed": true, "error": null},
    {"stage": "syntax","file": "api/health.py", "passed": true, "error": null},
    {"stage": "imports","file": "api/health.py","passed": false,
     "error": "Import 'foo' not found in stdlib or project"}
  ],
  "scores": {"overall": 8.2, "completeness": 9, "error": null},
  "judge_reasoning": "The code is well-structured but imports a missing dep...",
  "commit_sha": null,
  "ts": "2026-09-09T12:34:56"
}
```

**Escalation triggers:** validation exhaustion, test exhaustion, commit PERMANENT failure. A retryable commit failure that is retried is *not* an escalation; only the terminal failure is.

**Cross-stage cap:** wire the currently-dead `MAX_TOTAL_ATTEMPTS_PER_TASK` (`pipeline.py:139`) into `_run_task` as a real cap — when generate+test+commit attempts across a task ≥ the cap, escalate and fail the task. This closes the COMMIT→QUEUED infinite-loop risk it was meant to prevent. Also call the currently-dead `_recheck_transport` once on first transport failure (cheap recovery from a transient blip).

### Rationale
Keeping the LLM judge out of the hot loop preserves the fast deterministic gate; attaching its reasoning to the escalation event gives the orchestrator exactly the rich signal it needs (scores + judge reasoning + full stage trace) at exactly the right moment (terminal failure). Fixing the `emit_event` calling convention is non-negotiable — without it no escalation can ever fire. Wiring the dead cap and transport-recheck turns two dead safety-nets into real ones.

### Interface sketch
```python
# events.py — corrected call form (and the bridge stays fire-and-forget)
emit_event(self.on_event, "task_escalation", { ... payload ... })

# engine.py _run_task — terminal failure path:
if pipeline.total_attempts(task.id) >= MAX_TOTAL_ATTEMPTS_PER_TASK:
    pipeline.transition(State.FAILED)
    _emit_escalation(pipeline, task, stage="...", ...)
    return task_result

# New helper:
def _emit_escalation(pipeline, task, stage, last_errors, validation_stages,
                     scores=None, judge_reasoning=None):
    emit_event(pipeline.engine.on_event, "task_escalation", { ... })
```
`Pipeline.total_attempts(task_id)` sums generate+test+commit counters (new 3-line method).

### Test plan
- T5.1 `emit_event(on_event, "task_escalation", payload)` invokes `on_event` with exactly (event_type, data); does not raise when `on_event` is None.
- T5.2 On validation exhaustion, `task_escalation` emitted with `stage="validation"`, full `validation_stages`, and `last_errors`.
- T5.3 On commit PERMANENT failure, `task_escalation` emitted with `stage="commit"`.
- T5.4 Retryable commit failure is retried (no escalation); only terminal failure escalates.
- T5.5 `task_escalation` payload includes `scores` + `judge_reasoning` when the LLM judge ran.
- T5.6 Cross-stage cap: task fails + escalates when total attempts ≥ `MAX_TOTAL_ATTEMPTS_PER_TASK`.
- T5.7 `_recheck_transport` called once on first transport failure (mock transport).
- T5.8 Escalation never blocks the pipeline (emit wrapped so exceptions are swallowed).

### Effort: L

---

## 6. Scoring invalid code  [decided]

### Decision
- **Score always, flag `scored_state`.** The LLM judge scores every task that has generated code (as today), **and** each `engine_scores` row records the state the code was in when scored: `scored_state ∈ {validation_failed, test_failed, committed}`.
- **CCBS consumes** the score **plus** `scored_state` **plus** the deterministic `judge_verdicts.overall_pass`. This preserves all signal (we never throw away data) and lets CCBS filter or weight — e.g. only rank `scored_state == "committed"`, or down-weight `validation_failed` scores.
- The deterministic gate (validator) remains what blocks commit; the LLM score is quality telemetry regardless of gate outcome. The 8.2/10-on-invalid-code case becomes a *flagged data point*, not a silent contradiction.

### Rationale
Dropping scores on invalid code would discard information (the judge/validator disagreement is itself a useful signal about judge calibration). Flagging `scored_state` is the minimal, non-destructive way to keep the score interpretable. CCBS gets everything it needs to decide its own consumption policy; the engine's job is to emit the flag, not to censor.

### Interface sketch
```python
# llm_judge.score_task returns (and saves) an extra field:
result["scored_state"] = "validation_failed" | "test_failed" | "committed"

# engine_scores gains one column (additive, no migration of existing data):
#   scored_state TEXT
# save_engine_scores(...) gains scored_state kwarg.
```
`TaskResult.scores` (already a dict) carries `scored_state` into `report.json` automatically.

### Test plan
- T6.1 Score on a validation-failed task → `engine_scores.scored_state == "validation_failed"`.
- T6.2 Score on a committed task → `scored_state == "committed"`.
- T6.3 `report.json` per-task `scores` includes `scored_state`.
- T6.4 Deterministic gate still blocks commit independent of LLM score (a 10/10 on invalid code is still not committed).

### Effort: S

---

## 7. Implementation order

| Phase | Topics | Dependency | Effort | Goal |
|-------|-------|------------|--------|------|
| **P0** | 2 (stage telemetry) + fix `state.py` env override (1 partial) | — | S | Forensic trace in DB; unblock archive path |
| **P1** | 3 (reasoning budget) | P0 telemetry (to validate the new error path) | M | Close truncation gap; stop misreporting empty output |
| **P2** | 4 (commit hardening) | — | M | Eliminate wrong-repo push + honor classification |
| **P3** | 5b `emit_event` fix + escalation contract | P0 (stages in payload), P2 (commit escalation) | L | Working escalation bridge + cross-stage cap |
| **P4** | 1 (archive wiring) | P0 state.py fix, P3 events | M | Per-run archive + prune + pull |
| **P5** | 6 (scored_state flag) | P0 | S | CCBS-consumable scoring |

P0 and P2 are independent and can run in parallel. P3 depends on P0 (needs stage detail in the payload) and P2 (commit escalation). P4 depends on the state.py fix (P0) and the event bridge (P3). P5 is independent but small, so it ships alongside P0.

Within each phase, TDD order: write the failing test against the interface sketch, then wire the code, then green.

---

## 8. Deferred to user — confirmed

These decisions were confirmed by the human owner during implementation; the answers below supersede the proposals.

1. **Archive retention cap** (Topic 1) — **Confirmed `archive_keep=30` `.db` snapshots/project, JSON exports forever.** The engine retains `report.json` indefinitely (small, diffable) and prunes `engine.db` snapshots to the newest 30 per project via `EngineConfig.archive_keep` (default 30) / `ace archive prune`.
2. **Gitea autocreate default** (Topic 4) — **Confirmed OFF by default.** `GITEA_AUTOCREATE=1` enables opt-in autocreate (used only for matrix lanes). A missing repo fails fast with a clear error naming the repo and how to create it.
3. **Judge escalation budget** (Topic 3) — **Confirmed ceiling 32768 (owner override).** Long contexts are fine on the 3090 judge. Subject-side escalation remains ×2 per the design (cap 16384). The judge's existing single retry doubles as the escalation, doubling its budget toward the 32768 ceiling.
4. **CCBS consumption policy** (Topic 6) — **Confirmed: consume all scores, engine emits `scored_state`, no engine-side filtering.** The engine flags each `engine_scores` row with `scored_state ∈ {validation_failed, test_failed, committed}` and lets CCBS filter/weight. The three states are emitted on every scored row (`validation_failed` on validation/reasoning exhaustion, `test_failed` on commit failure, `committed` on success).
5. **Orchestrator escalation action** (Topic 5) — **Confirmed: emit-only.** The engine emits `task_escalation` events over the fixed `emit_event` bridge; what the orchestrator does with them is outside engine scope. Escalation never blocks or crashes the pipeline.

---

## 9. Decision register

| # | Topic | Status |
|---|-------|--------|
| 1 | Results store wiring | [decided] |
| 2 | Validator stage telemetry | [decided] |
| 3 | Reasoning-budget strategy | [decided] |
| 4 | Commit path hardening | [decided] |
| 5a | LLM judge in regeneration loop | [decided — no] |
| 5b | Escalation event contract | [decided] |
| 6 | Scoring invalid code | [decided] |
| — | `state.py` env-override wart | [decided — fix to match paths.py] |
| — | `emit_event` calling convention (V8) | [decided — fix; prerequisite for 5b] |
| — | `MAX_TOTAL_ATTEMPTS_PER_TASK` / `_recheck_transport` | [decided — wire the dead safety-nets] |
