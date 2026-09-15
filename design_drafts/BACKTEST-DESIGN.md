# Backtesting Design — Atom-Level Replay for the Coding Engine

> Status: Authoritative design for engine backtesting at PRD / epic / ticket / atom granularity.
> Scope: Phase 2 (Epic 6), built on top of the unified `engine/` package. Companion: `TICKET-STATE-MACHINE.md` (the machine whose runs get backtested), `campaigns/engine-unification/orchestrator/` (campaign-side tooling).

## 1. Problem Being Solved

When the engine breaks mid-run — a gate false-positives, a retry loop thrashes, a state transition corrupts checkpoint state — today the only debugging option is rerunning the whole PRD against a live GPU. That is slow, non-deterministic (LLM output changes every run), and cannot isolate *which part* broke.

**Backtesting fixes this by making every engine run a replayable, deterministic trace of atoms.**

## 2. Definitions

- **Atom** = one state transition of the engine's 12-state machine for one task: `(run_id, task_id, from_state, to_state, input_hash, output_hash, gate_hash?, duration_ms, meta)`. The atom is the unit of debugging, replay, and comparison — the same word the ticket machine uses (§5 of TICKET-STATE-MACHINE.md), deliberately: campaign atoms and engine atoms share one replay model.
- **Trace** = the ordered atom list of one engine run (`run_id`).
- **Golden trace** = a recorded trace from a known-good run, stored with its inputs (PRD, model config, seeds, transport mode `mock`).
- **Determinism contract**: given identical `(PRD, task, atom index, recorded input)` and `TRANSPORT_MODE=mock`, an atom replayer must produce byte-identical `output_hash`. LLM nondeterminism is frozen at record time: the recorded BeeLlama response is replayed from the transcript, never re-generated.

## 3. Architecture: Recorder → Store → Replayer → Forensics

```
engine run (TRANSPORT_MODE=mock|real)
   │  (engine/trace.py instruments pipeline.py transitions)
   v
AtomRecorder ──> engine.db: atoms table (Rule 6: engine data in engine.db ONLY)
   │
   ├─> atom_replay.py --atom <id>        # replay ONE atom, compare output_hash
   ├─> atom_replay.py --run <run_id>     # replay a full trace, stop at first divergence
   ├─> ticket_replay.py --ticket <brief> # ticket-level: run engine on one golden task w/ mock transport
   ├─> prd_replay.py --prd <golden>      # PRD-level: full pipeline on a golden PRD
   └─> forensics.py bisect --run <id>    # first divergent atom (ledger bisect)
```

Four replay granularities, cheapest first — always bisect downward:

| Level | Unit | Use when | Cost |
|---|---|---|---|
| Atom | 1 transition | a specific gate/state misbehaves | ~ms, no GPU |
| Ticket | 1 task lifecycle | retries, checkpoint/resume, file staging suspect | seconds, no GPU |
| Epic | 1 dependency chain of tasks | integration between modules suspect | seconds, no GPU |
| PRD | full run | E2E regression | minutes, GPU only in `real` mode |

## 4. Engine-Side Changes (Phase 2 tickets T60–T66)

1. **`engine/trace.py` (T61)** — `AtomRecorder`: pipeline.py calls `recorder.record(from_state, to_state, inputs, outputs)` on every transition. Atom rows: `atom_id, run_id, task_id, seq, from_state, to_state, input_hash, output_hash, gate_hash, duration_ms, meta_json`. Written to `engine.db` (Rule 6).
2. **Deterministic mock transport (T63)** — `MockTransport` replays recorded BeeLlama responses from a transcript fixture (`FixturesReplayTransport`); zero network, zero GPU, byte-identical tokens. Recording mode (`RECORD=1`) captures real responses into the fixture; replay mode consumes them.
3. **Golden corpus (T60)** — `tests/golden/`: ≥3 golden PRDs (trivial, multi-task with failure+retry, multi-turn), each with recorded transcript + expected atom trace + expected gate verdicts. `test-prd.md` is golden #1.
4. **Atom replayer + forensics (T62, T65)** — replay one atom / one trace; `forensics.py bisect` walks a trace and reports the first atom whose re-computed `output_hash` differs from the recorded one, printing the atom's input/output diff.
5. **Mutation harness (T64)** — gate sensitivity testing: a corpus of known-bad patches (syntax error, missing import, disallowed import, runtime error, empty code) applied to the 4-stage validator; the validator MUST catch each. Reports a sensitivity score per gate stage. A gate that misses a mutation is a broken gate — this is how gate regressions get caught *before* they eat an engine run. Note: `__import__` escape and path traversal are caught by `check_execution` in the validator's restricted namespace — T64 verifies the validator catches them via the existing corpus mutations that exercise the same code path.
6. **PRD-level runner + report (T66)** — `prd_replay.py --prd tests/golden/test-prd.md`: full replay, JSON verdict `{run_id, atoms, replayed, diverged_at, gate_sensitivity, pass}`.

## 5. Debugging Workflow (how a break is actually debugged)

```
E2E run fails on Triton
 1. prd_replay --real-trace <run_id>      # which task/state failed? (trace is already in engine.db)
 2. forensics bisect --run <run_id>       # replay whole trace in mock mode → first divergent atom
 3. atom_replay --atom <id> --verbose     # rerun just that atom: input diff, output diff
 4. ticket_replay --task <task_id>        # widen if the atom alone doesn't reproduce
 5. Fix → re-run atom replay → ticket replay → PRD replay (never straight to full GPU run)
```

Rule: **never debug at a coarser granularity than the failure requires.** Each level up costs 10–100×.

## 6. NFRs

| ID | Requirement | Verification |
|---|---|---|
| NFR-B1 | Atom replay deterministic: 100 identical replays → identical `output_hash` | CI loop |
| NFR-B2 | Ticket-level replay completes < 30 s, zero network | CI timing |
| NFR-B3 | Mutation harness detects ≥ 95% of sabotage corpus | T64 report |
| NFR-B4 | Bisect finds first divergent atom in ≤ 2 min on a 50-atom trace | T65 test |
| NFR-B5 | Traces live in `engine.db` only (Rule 6) | schema check |
| NFR-B6 | Recording a golden trace works on real GPU once; thereafter replay needs no GPU | T66 |

## 7. Blind Spots Plugged (beyond the original ask)

1. **LLM nondeterminism** — without transcript replay, "backtest" is meaningless; mock transport replays recorded responses byte-for-byte.
2. **Gate regressions are invisible until they bite** — mutation harness makes gate sensitivity a measured number, not a hope.
3. **No downward bisect path existed** — PRD→epic→ticket→atom granularity ladder with a defined cheapest-first order.
4. **Trace storage would have drifted to benchmark-results.db** — pinned to engine.db by Rule 6 from day one.
5. **Golden corpus rot** — golden traces are regenerated on demand with `RECORD=1` and version-tagged with the engine commit; a stale golden trace is detectable by its `engine_commit` field.
6. **Campaign-side symmetry** — the ticket machine's own gates produce `gate_hash` atoms (TICKET-STATE-MACHINE.md §5), so a campaign run can be replayed/bisected with the same forensics tooling. One replay model, two consumers.
