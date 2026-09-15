# EPICS.md: Engine Unification

## Mission

The epic chain in this document exists to deliver the Autonomous Coding Engine (ACE) mission: an end-to-end PRD-to-commit pipeline with zero human intervention, crash-resilient checkpointing, and quality gates — all backed by the design sections in ARCHITECTURE.md. The Phase-1 terminal gate is a successful E2E run on a real PRD, proving the engine can parse, generate, validate, test, and commit without manual intervention.

## Epic 1: Engine Kernel
**Goal:** Build the `engine/` package and `cli.py`. Prove it works with a real E2E test.
**Priority:** P0 — this is the foundation everything else depends on.
**Effort:** 4-6 hours

### Prerequisites (CONFORMANCE FIX: pending jobs from the handoff)
- [ ] **pytest installed on Triton**: `ssh <user>@<LAN_IP> "pip3 install pytest"` — the TEST phase silently fails without it (a handoff pending job previously dropped from all artifacts)
- [ ] **Gitea repo + auth verified**: `ace-demo` repo exists on Gitea, `GITEA_TOKEN` env var set, token-authenticated push tested once manually
- [ ] **`engine_service.py` running on Triton :3082** (systemd) OR SSH fallback verified — `Engine.__init__` fails fast with a diagnostic if all transports are down
- [ ] **RED-TEAM V3 — HTTPTransport auth bug (real codebase defect)**: `engine_service.py` refuses to start without a token, but `HTTPTransport._request()` sends NO Authorization header — every authenticated call gets 401. Phase 1 MUST fix `transport.py` to read `ENGINE_TOKEN` from env and send `Authorization: Token {token}`. The "preferred" transport is otherwise dead on arrival. Probe: start engine_service with a token, call `HTTPTransport().curl_beellama(...)` — RuntimeError 401 confirms the bug.
- [ ] **CORS note (RED-TEAM F9)**: `engine_service.py` allows `allow_origins=["*"]` with credentials — acceptable on an isolated LAN, but document it; do not expose :3082 beyond the local network.

### User Stories
1. As a developer, I want to run `python3 cli.py ace run --prd my-prd.md --project /path` so that the engine executes the entire PRD without further intervention
2. As a developer, I want the engine to parse my PRD into atomic, dependency-ordered tasks so that work happens in the right sequence
3. As a developer, I want the engine to build project context (file tree, imports, framework detection) for each task so that generated code matches my project's conventions
4. As a developer, I want the engine to generate code via BeeLlama inference with retry logic so that transient LLM errors don't block progress
5. As a developer, I want the engine to validate generated code through 4 stages (AST, syntax, imports, execution) so that broken code is caught before testing
6. As a developer, I want the engine to run pytest against generated code so that I know the code actually works
7. As a developer, I want the engine to automatically fix failing tests with error context so that the retry loop is intelligent, not blind
8. As a developer, I want the engine to commit passing code to Gitea with conventional commit messages so that the git history is readable
9. As a developer, I want the engine to produce a structured RunResult with timing, token usage, and per-task outcomes so that I can assess quality and cost
10. As a developer, I want the engine to emit events via an optional callback so that I can monitor progress
11. As a developer, I want the engine to work without the streaming server (opt-in) so that it's self-contained
12. As a developer, I want the engine to detect and cache the transport at startup so that there's no per-call overhead
13. As a developer, I want the engine to support `--dry-run` so that I can preview the task plan without executing
14. As a developer, I want the engine to checkpoint state after each task so that interrupted runs can be resumed
15. As a developer, I want `python3 cli.py ace status --run <id>` so that I can check the status of a running or completed engine run
16. As a developer, I want `python3 cli.py ace resume --run <id>` so that I can continue an interrupted run
17. As a developer, I want `python3 cli.py ace cancel` so that I can gracefully stop the engine (SIGINT saves state)
18. As a developer, I want the engine to log every state transition with timestamps so that I can audit what happened
19. As a developer, I want the engine to respect per-step timeouts (300s for inference, 180s for tests, 60s for git) so that hung operations don't block forever
20. As a developer, I want the engine to validate that generated code's imports exist in the project or are stdlib so that the code doesn't reference nonexistent modules
21. As a developer, I want the engine to detect the project's framework (Django, Flask, FastAPI, etc.) so that generated code follows framework conventions
22. As a developer, I want the engine to generate a summary report at the end so that I can quickly assess what was done
23. As a developer, I want the engine to support multiple model configs (`--config 3090-qwen36-35b`) so that I can use different models for different tasks
24. As a developer, I want the engine to track token usage and cost per task so that I can monitor resource consumption
25. As a developer, I want the engine to work with Python projects on Triton via the transport layer so that it doesn't require local installation
26. As a developer, I want the engine to create feature branches (not modify main) so that the main branch stays clean
27. As a developer, I want the engine to handle PRDs of varying formats (bullet lists, numbered tasks, tables) so that I don't need a rigid template
28. As a developer, I want the engine to flag ambiguous requirements in the PRD parsing stage so that I can clarify before code is generated
29. As a developer, I want the engine to retry code generation with progressively more context (error messages, stack traces, related files) so that fixes are intelligent
30. As a developer, I want a single CLI entry point replacing all 6 existing CLIs so that there's no confusion about which command to run

### Acceptance Criteria
- `python3 cli.py ace run --prd test-prd.md --project /home/<user>/projects/ace-demo --dry-run` produces a complete task plan
- `python3 cli.py ace run --prd test-prd.md --project /home/<user>/projects/ace-demo` generates real code, validates, tests, commits to Gitea
- All 12 state transitions are reachable and validated
- Engine works with streaming_url=None (no streaming dependency)
- Transport is detected once at init, cached for all operations

---

## Epic 2: Pipeline Hardening
**Goal:** Checkpoint/resume, timeouts, signal handling, dry-run mode.
**Priority:** P1 — required for production reliability.
**Effort:** 2-3 hours

### User Stories
1. As a developer, I want `python3 cli.py ace resume --run <id>` to recover from a crash by loading the last checkpoint from SQLite
2. As a developer, I want per-step timeouts enforced at the pipeline level so that hung BeeLlama inference doesn't block forever
3. As a developer, I want SIGINT to checkpoint the pipeline and exit gracefully so that no work is lost
4. As a developer, I want `--dry-run` to execute PARSING → QUEUED and stop, returning the task plan without generating code
5. As a developer, I want the pipeline to track token usage (prompt tokens, completion tokens, thinking tokens) per task so that I can monitor cost
6. As a developer, I want a `RunResult` summary at completion with total time, tokens, tasks succeeded/failed so that I can assess the run
7. As a developer, I want every CLI command to output structured JSON so that agents can parse the output
8. As a developer, I want `--pretty` flag for human-readable output and `--verbose` for debug logging
9. As a developer, I want the CLI to use lazy imports so that `cli.py --help` is fast
10. As a developer, I want progress percentage in status output (0-100%) so I can estimate completion time

### Acceptance Criteria
- `python3 cli.py ace resume --run <id>` restores pipeline state and continues
- SIGINT during inference checkpoints and exits with code 0
- `--dry-run` produces task plan without calling BeeLlama
- All CLI commands output valid JSON to stdout

---

## Epic 3: E2E Validation
**Goal:** Prove the engine works with a real PRD through real BeeLlama inference.
**Priority:** P0 — this is THE critical test.
**Effort:** 2-3 hours

### User Stories
1. As a developer, I want a `MockTransport` class that simulates BeeLlama responses so that unit tests don't require a running server
2. As a developer, I want unit tests for each engine module so that individual components are verified in isolation
3. As a developer, I want integration tests that exercise the full pipeline with mock transport so that state machine transitions are verified
4. As a developer, I want an E2E test that runs `test-prd.md` through real BeeLlama inference so that we prove the engine actually works
5. As a developer, I want the E2E test to verify that generated code passes AST validation so that we catch syntax errors
6. As a developer, I want the E2E test to run pytest against the generated code so that we catch runtime errors
7. As a developer, I want the E2E test to commit the generated code to a test branch on Gitea so that we verify the full PRD → commit path
8. As a developer, I want the E2E test to produce a structured report with timing, token usage, and quality scores so that we can track improvements
9. As a developer, I want the E2E test marked `@pytest.mark.e2e` so that it can be excluded from fast test runs
10. As a developer, I want the E2E test to clean up after itself (delete test branch) so that it doesn't leave artifacts

### Acceptance Criteria
- `python3 -m pytest tests/unit/ -v` — all unit tests pass (15-20 tests)
- `python3 -m pytest tests/integration/ -v` — all integration tests pass (2-3 tests)
- `python3 -m pytest tests/e2e/ -v` — E2E test passes with real BeeLlama
- MockTransport enables all unit tests without a running server
- E2E test produces machine-readable JSON report

---

## Epic 4: Consolidation & Migration
**Goal:** Move old modules to legacy/, update docs, clean up.
**Priority:** P2 — cleanup after engine is proven.
**Effort:** 1-2 hours

### User Stories
1. As a maintainer, I want all old modules moved to `legacy/` with deprecation warnings so that the top-level directory is clean
2. As a maintainer, I want benchmark platform moved to `bench/` so that it's clearly separated from the engine
3. As a maintainer, I want the full test suite to pass after migration so that nothing is broken
4. As a maintainer, I want `AGENTS.md` rewritten for the new architecture so that agents have accurate documentation
5. As a maintainer, I want `README.md` regenerated so that users have accurate documentation
6. As a maintainer, I want a `MIGRATION.md` document mapping old to new locations
7. As a maintainer, I want old test files moved to `tests/legacy/` so that they don't pollute the new test suite
8. As a maintainer, I want `bench/` modules independently runnable so that the benchmark platform works without the engine
9. As a maintainer, I want the top-level directory to contain fewer than 15 Python files so that the project is navigable
10. As a maintainer, I want each legacy module to emit a deprecation warning on import so that consumers know to migrate

### Acceptance Criteria
- Top-level directory has ≤15 Python files
- `python3 -c "from engine import Engine"` succeeds
- `python3 -c "from legacy.work_engine import WorkEngine"` succeeds (with deprecation warning)
- `python3 -m bench.orchestrate --dry-run` works
- All documentation reflects new architecture

---

> **⚠️ Phase 2 Stub — Out of scope for the current build.** This section captures future work identified as coverage gaps in the Mission Anchor (ARCHITECTURE.md §0, rows 8–9). No implementation effort is planned until Phase 1 (Epics 1–4) is fully complete.

## Epic 5 (Phase 2 stub): Self-Scoring & Benchmark Integration

**Goal:** Close the two mission-coverage gaps by integrating `judge.py` self-scoring into the ACE pipeline and exposing a programmatic run API that CCBS can drive as a benchmark consumer. All engine-side scoring results must persist to `engine.db` per Rule 6.

**Priority:** P3 — Phase 2, blocked on Phase 1 completion (Epics 1–4).
**Effort:** 3–5 hours (estimate, subject to revision at Phase 2 planning)

### User Stories

1. As an ACE operator, I want the engine to invoke `judge.py` after the TEST phase scores generated code on 5 dimensions (completeness, correctness, quality, intelligence, role_fit) so that self-scoring is part of the automated pipeline — not a separate manual step.
2. As an ACE operator, I want judge scores written to a `engine_scores` table in `engine.db` (Rule 6: never `benchmark-results.db`) so that engine runs are self-contained and benchmark DBs stay independent.
3. As a CCBS consumer, I want a programmatic Python API (`engine.run_matrix(configs, prds, repetitions)`) that executes N runs across a config × PRD matrix and returns structured JSON reports so that CCBS can drive the engine without CLI wrapping.
4. As a CCBS consumer, I want each run report to include token counts (prompt, completion, thinking), tokens-per-second, wall-clock time, and per-dimension judge scores so that CCBS can rank model configs on the same dimensions the benchmark platform uses.
5. As an ACE operator, I want the pipeline to emit a `RunSummary` JSON blob at completion containing total_time_s, total_tokens, per_task_scores, and an overall_quality rating so that both human operators and automated consumers can assess run quality in one artifact.

### Acceptance Criteria

- **AC5.1 — Judge integration in pipeline:** After TEST phase completes for all tasks, the engine invokes `judge.py` on each task's generated code; scores are written to `engine_scores` table in `engine.db`. *Verification:* `sqlite3 engine.db "SELECT * FROM engine_scores"` returns rows after a full pipeline run; no writes occur in `benchmark-results.db`.
- **AC5.2 — CCBS programmatic API:** `engine.run_matrix(configs=["3090-qwen36-35b"], prds=["test-prd.md"], repetitions=1)` returns a JSON-serializable dict with per-run scores, token telemetry, and timing. *Verification:* `python3 -c "from engine import Engine; print(Engine().run_matrix(...).keys())"` prints expected top-level keys.
- **AC5.3 — Telemetry completeness:** Each run report contains `prompt_tokens`, `completion_tokens`, `thinking_tokens`, `tokens_per_sec`, `wall_clock_s`, and 5 judge dimension scores. *Verification:* JSON schema validation against a defined `RunReportSchema` contract; unit test asserts all fields present and numeric.

---

> **⚠️ Phase 2 Stub — Out of scope for the current build.** This section captures atom-level replay and backtesting, designed in `BACKTEST-DESIGN.md`. Blocked on Phase 1 completion (Epics 1–4).

## Epic 6 (Phase 2): Backtesting & Atom Replay

**Goal:** Make every engine run a replayable, deterministic trace of atoms so that failures can be debugged offline without re-running against a live GPU. Fix the invisibility of gate regressions and provide a downward-bisect path from PRD → epic → ticket → atom granularity.

**Priority:** P3 — Phase 2, blocked on Phase 1 completion (Epics 1–4).
**Effort:** 3–5 hours (estimate, subject to revision at Phase 2 planning)
**Design:** `BACKTEST-DESIGN.md` (authoritative); `contracts/engine-package-v2.yaml` (engine contracts)

### User Stories

1. As an engine operator, I want every pipeline state transition recorded as a deterministic atom in `engine.db` so that any engine run can be replayed offline without a GPU (AtomRecorder via `engine/trace.py`).
2. As an engine operator, I want a mock transport that replays recorded BeeLlama responses from a transcript fixture so that atom replay is byte-identical and zero-network (FixturesReplayTransport, `TRANSPORT_MODE=mock`).
3. As an engine operator, I want a mutation harness that applies known-bad patches to the golden corpus and verifies the 4-stage validator catches each one so that gate sensitivity is a measured number, not a hope.
4. As an engine operator, I want a forensic bisect tool that walks a trace and reports the first atom whose re-computed `output_hash` diverges from the recorded one so that I can pinpoint the exact cause of a regression.

### Acceptance Criteria

- **AC6.1 — Deterministic atom replay:** 100 identical replays of the same atom (same recorded input, `TRANSPORT_MODE=mock`) produce byte-identical `output_hash`. *Verification gate (NFR-B1):* CI loop asserts `len(set(output_hashes)) == 1` across 100 replays.
- **AC6.2 — Mutation detection ≥ 95%:** The mutation harness applies the full sabotage corpus to the golden project; the 4-stage validator catches ≥ 95% of mutations. *Verification gate (NFR-B3):* T64 report shows sensitivity score ≥ 0.95.
- **AC6.3 — Bisect ≤ 2 min / 50 atoms:** `forensics.py bisect` finds the first divergent atom in a 50-atom trace within 2 minutes wall-clock on CI hardware. *Verification gate (NFR-B4):* T65 test asserts `bisect_time_s ≤ 120`.

### NFR Coverage (all NFRs from BACKTEST-DESIGN.md §6)

| NFR | Requirement | Verified by |
|-----|-------------|-------------|
| NFR-B1 | Atom replay deterministic (100 replays → identical hash) | AC6.1 (CI loop) |
| NFR-B2 | Ticket-level replay < 30 s, zero network | T66 acceptance (PRD-level timing) |
| NFR-B3 | Mutation harness detects ≥ 95% of sabotage corpus | AC6.2 (T64 report) |
| NFR-B4 | Bisect ≤ 2 min on 50-atom trace | AC6.3 (T65 test) |
| NFR-B5 | Traces in engine.db only (Rule 6) | T61 DDL + schema check in each ticket's Do NOT |
| NFR-B6 | Golden trace recording works on real GPU once | T60 record convention + T63 RECORD=1 mode |
