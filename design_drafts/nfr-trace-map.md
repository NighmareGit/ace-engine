# NFR Trace Map: Engine Unification

Each NFR cites (i) verification gate(s) and (ii) design decision(s) that satisfy it.

| # | NFR | SLO | Verification Gate | Design Decision |
|---|-----|-----|-------------------|-----------------|
| NFR1 | E2E completion time | <5 min for trivial PRD (≤3 tasks) | Integration test: `test_prd_to_commit.py` asserts `result.total_time_s < 300` | Timeout enforcement per state (300s inference, 180s test, 60s commit) |
| NFR2 | CLI size | <600 lines | Lint rule: `wc -l cli.py` in CI | Monolithic CLI with lazy imports (PRD-02 D1) |
| NFR3 | State checkpoint latency | <100ms per transition | Unit test: `test_pipeline.py` asserts checkpoint < 100ms | SQLite WAL mode, atomic writes (PRD-01 D7) |
| NFR4 | State machine correctness | All 12 states reachable, no invalid transitions | Unit test: `test_pipeline.py` enumerates all valid transitions | Transition whitelist in pipeline.py (PRD-02 D4) |
| NFR5 | Retry intelligence | Each retry adds error context | Unit test: `test_engine_generator.py` verifies ErrorContext escalation | Retry with escalation strategy (PRD-01 D6) |
| NFR6 | Streaming opt-in | Engine works without streaming | Unit test: `test_engine_events.py` verifies engine.run() with streaming_url=None | EventBridge silently drops events (PRD-01 D4) |
| NFR7 | Transport detection | Single detection at startup | Unit test: `test_engine_engine.py` verifies transport called once in __init__ | Transport cached in Engine.__init__ (PRD-01 D2) |
| NFR8 | Validator coverage | 4 stages catch syntax, imports, execution | Unit test: `test_engine_validator.py` verifies each stage catches its error type | Chain of Responsibility pattern (PRD-04 D4) |
| NFR9 | Legacy compatibility | Old modules importable for 2 weeks | Import test: `python3 -c "from legacy.work_engine import WorkEngine"` | Move to legacy/, not delete (AD5) |
| NFR10 | Test pyramid | 30+ unit, 3-5 integration, 1 E2E | CI: `python3 -m pytest tests/` counts pass | Test strategy (PRD-04 D1) |
| NFR11 | Dry-run mode | Preview task plan without execution | CLI test: `python3 cli.py ace run --prd X --project Y --dry-run` produces task list | Dry-run stops at QUEUED (PRD-01 D8) |
| NFR12 | Signal handling | SIGINT checkpoints and exits gracefully | Integration test: `test_engine_pipeline.py` simulates SIGINT | Signal handler in cli.py (PRD-02 D6) |
| NFR13 | Benchmark separation | bench/ independently runnable | Import test: `python3 -m bench.orchestrate --dry-run` | Move to bench/ directory (AD9) |
| NFR14 | Engine standalone | Engine works without Gitea for dry-run | Unit test: `test_engine_engine.py` with mock transport, dry_run=True | Transport abstraction (PRD-01 D2) |
| NFR15 | Progress visibility | Status shows 0-100% progress | CLI test: `python3 cli.py ace status --run <id>` includes progress_pct | Pipeline.get_progress() method |
