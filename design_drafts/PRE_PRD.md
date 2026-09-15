# PRE_PRD.md: Engine Unification

## Epic 1: Engine Kernel — Acceptance Criteria & Verification Gates

### AC1.1: Engine package exists and is importable
- **Gate:** `python3 -c "from engine import Engine, EngineConfig"` succeeds
- **Edge cases:** Import from different working directories, import order dependencies

### AC1.2: CLI is a single entry point
- **Gate:** `python3 cli.py --help` prints usage with ace/bench/stream/status groups
- **Edge cases:** Missing arguments, invalid subcommands, --version flag

### AC1.3: State machine handles all 12 transitions
- **Gate:** Unit test `test_engine_pipeline.py` enumerates every valid transition
- **Edge cases:** Invalid transitions raise ValueError, terminal states have no outgoing transitions

### AC1.4: Dry-run produces task plan
- **Gate:** `python3 cli.py ace run --prd test-prd.md --project /path --dry-run` returns JSON with task list
- **Edge cases:** PRD with no tasks, PRD with circular dependencies, missing PRD file

### AC1.5: Context builder reads project files
- **Gate:** Unit test verifies build_context returns Context with file_tree, imports, framework
- **Edge cases:** Empty project, project with no Python files, deeply nested directories

### AC1.6: Generator calls BeeLlama via transport
- **Gate:** Unit test with MockTransport verifies generate_code calls curl_beellama
- **Edge cases:** Transport timeout, empty response, response with no code blocks

### AC1.7: Validator catches all 4 error types
- **Gate:** Unit test provides code failing each stage, verifies correct stage catches it
- **Edge cases:** Code that passes AST but fails syntax, code with valid syntax but missing imports

### AC1.8: Test runner executes pytest
- **Gate:** Unit test with MockTransport verifies run_tests calls pytest
- **Edge cases:** No tests in project, pytest not installed, test timeout

### AC1.9: Committer creates branch and commits
- **Gate:** Unit test with MockTransport verifies commit_code creates branch and commits
- **Edge cases:** Dirty working tree, push failure, branch already exists

### AC1.10: Engine.run() executes full pipeline
- **Gate:** Integration test with MockTransport runs full pipeline, verifies RunResult
- **Edge cases:** Task failure mid-pipeline, all tasks fail, zero tasks in PRD

### AC1.11: E2E test with real BeeLlama
- **Gate:** `python3 -m pytest tests/e2e/ -v` passes with real BeeLlama
- **Edge cases:** BeeLlama unavailable (skip test), generated code doesn't compile, tests fail

### AC1.12: Streaming opt-in
- **Gate:** Unit test verifies engine.run() works with on_event=None
- **Edge cases:** on_event raises exception (catch and log, don't crash engine)

### AC1.13: Transport single detection
- **Gate:** Unit test verifies transport.get_transport() called once in Engine.__init__
- **Edge cases:** Transport detection fails (fallback to SSH), transport changes mid-run

---

## Epic 2: Pipeline Hardening — Acceptance Criteria & Verification Gates

### AC2.1: Checkpoint/resume works
- **Gate:** Integration test creates pipeline, checkpoints, creates new Pipeline.resume(), verifies state restored
- **Edge cases:** Corrupt SQLite, missing run_id, partial checkpoint

### AC2.2: Timeouts enforced
- **Gate:** Unit test sets timeout=1s, provides slow MockTransport, verifies timeout error
- **Edge cases:** Timeout during checkpoint (partial state saved), timeout during commit

### AC2.3: SIGINT graceful shutdown
- **Gate:** Integration test sends SIGINT during pipeline, verifies checkpoint saved and exit code 0
- **Edge cases:** SIGINT during checkpoint itself, SIGINT during terminal state

### AC2.4: JSON output format
- **Gate:** Unit test runs every CLI command, verifies stdout is valid JSON with "ok" field
- **Edge cases:** Command error produces {"ok": false, "error": "..."}, --pretty formats output

### AC2.5: Lazy imports
- **Gate:** Unit test times `python3 cli.py --help` and verifies < 1 second
- **Edge cases:** Missing optional dependency, import error in lazy-loaded module

---

## Epic 3: E2E Validation — Acceptance Criteria & Verification Gates

### AC3.1: MockTransport enables all unit tests
- **Gate:** All unit tests pass without any running server
- **Edge cases:** MockTransport returns unexpected format, MockTransport timing

### AC3.2: Unit test coverage
- **Gate:** `python3 -m pytest tests/unit/ -v` — 15-20 tests all pass
- **Edge cases:** Test isolation (no shared state between tests), test ordering

### AC3.3: Integration test coverage
- **Gate:** `python3 -m pytest tests/integration/ -v` — 2-3 tests all pass
- **Edge cases:** Mock transport state between tests, pipeline state leakage

### AC3.4: E2E test produces report
- **Gate:** E2E test produces JSON report with run_id, total_time_s, tasks, states_visited
- **Edge cases:** Partial completion (some tasks fail), timeout during E2E

### AC3.5: E2E test cleans up
- **Gate:** E2E test deletes test branch after completion
- **Edge cases:** Cleanup failure (test branch left behind), cleanup during failure

---

## Epic 4: Consolidation & Migration — Acceptance Criteria & Verification Gates

### AC4.1: Top-level directory clean
- **Gate:** `ls *.py | wc -l` ≤ 15
- **Edge cases:** New files added to top level, files missed in migration

### AC4.2: Legacy modules importable with warnings
- **Gate:** `python3 -c "from legacy.work_engine import WorkEngine"` succeeds with DeprecationWarning
- **Edge cases:** Legacy module has import errors, circular imports in legacy

### AC4.3: Benchmark platform independent
- **Gate:** `python3 -m bench.orchestrate --dry-run` works
- **Edge cases:** bench/ imports from top-level transport.py (should work)

### AC4.4: Documentation accurate
- **Gate:** AGENTS.md describes engine/ architecture, README.md reflects new structure
- **Edge cases:** Documentation drift, stale references to old modules

### AC4.5: All tests pass after migration
- **Gate:** `python3 -m pytest tests/ -v` — all non-legacy tests pass
- **Edge cases:** Tests reference old module paths, missing test fixtures

---

> **⚠️ Phase 2 — Blocked on Phase 1 completion (Epics 1–4).** See `BACKTEST-DESIGN.md` for the authoritative design. Tickets T60–T66.

## Epic 6: Backtesting (Phase 2) — Acceptance Criteria & Verification Gates

### AC6.1: Deterministic atom replay (NFR-B1)
- **Gate:** 100 identical replays of the same atom (same recorded input, `TRANSPORT_MODE=mock`) produce byte-identical `output_hash` — CI loop asserts `len(set(output_hashes)) == 1`
- **Edge cases:** Atom with gate_hash, atom with zero-duration transitions, replay with missing transcript fixture

### AC6.2: Mutation detection ≥ 95% (NFR-B3)
- **Gate:** The mutation harness applies the full sabotage corpus (syntax error, missing import, disallowed import, runtime error, empty code) to the 4-stage validator; the validator catches ≥ 95%. T64 report shows sensitivity score ≥ 0.95.
- **Edge cases:** Mutation that passes all 4 stages (broken gate), mutation that causes validator crash, mutation that only triggers on real transport

### AC6.3: Bisect ≤ 2 min / 50 atoms (NFR-B4)
- **Gate:** `forensics.py bisect --run <id>` walks a 50-atom trace and reports the first divergent atom within 120 seconds wall-clock on CI hardware. T65 test asserts `bisect_time_s ≤ 120`.
- **Edge cases:** Trace with no divergence (bisect returns "no divergence"), trace with first atom divergent, trace with multiple divergences (bisect must find the first)

### NFR Coverage (all NFRs from BACKTEST-DESIGN.md §6)

| NFR | Requirement | Verified by |
|-----|-------------|-------------|
| NFR-B1 | Atom replay deterministic (100 replays → identical hash) | AC6.1 (CI loop) |
| NFR-B2 | Ticket-level replay < 30 s, zero network | T66 acceptance (PRD-level timing) |
| NFR-B3 | Mutation harness detects ≥ 95% of sabotage corpus | AC6.2 (T64 report) |
| NFR-B4 | Bisect ≤ 2 min on 50-atom trace | AC6.3 (T65 test) |
| NFR-B5 | Traces in engine.db only (Rule 6) | T61 DDL + schema check in each ticket's Do NOT |
| NFR-B6 | Golden trace recording works on real GPU once | T60 record convention + T63 RECORD=1 mode |
