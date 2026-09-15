# VALIDATION REPORT: Architecture & Blueprint Simulation Pass
> Date: 2026-09 (validation round after blueprint v2)
> Method: 4 parallel agents — code-vs-contract diff, implementation simulation, red-team, goal-conformance — plus direct code spot-checks by the orchestrator.

---

## Verdict

The architecture (state machine, module separation, 2-pattern constraint, checkpoint/resume) is **structurally sound**. The first blueprint draft was **not implementable end-to-end**: simulation proved Goals 2–4 (full run, timing, SIGINT-resume) break at concrete, fixable points. All findings below have been patched into the artifacts.

**Meta-lesson (recorded in decision log):** three consecutive document-only audits passed the design; the first simulation pass and first code-anchored checks found 10+ real defects within minutes. Document-only audits systematically over-pass. Every future review gate MUST include (a) a code-anchored API check and (b) a traced execution simulation.

---

## Findings → Fixes Applied

| # | Sev | Finding | Source | Fix (artifact) |
|---|-----|---------|--------|----------------|
| V1 | CRIT | `check_execution` exec'd LLM code with full `__builtins__` — arbitrary code execution on host | Red-team + Sim B1 | Restricted namespace `{"__builtins__": {}}` + 5s `_with_timeout`; Docker sandbox remains opt-in |
| V2 | CRIT | Path traversal: `task.module` from PRD regex unsanitized (`../../.ssh/authorized_keys`) | Red-team | Sanitize in parse_prd (strip `..`, leading `/`), plus traversal guard in file extraction |
| B2 | CRIT | **Generated code never written to disk before pytest** — TEST phase tested nothing; 0 tests passed vacuously | Sim B2 | New pipeline step 7f: stage files to disk after VALIDATE, before TEST; rollback on failure |
| V3 | CRIT | HTTPTransport sends no auth token; engine_service requires one → 401 on every call. Real codebase defect inherited by design | Red-team | Phase 1 prerequisite: fix `transport.py` to send `Authorization: Token {ENGINE_TOKEN}` |
| B3 | HIGH | Project dir not bootstrapped (ace-demo doesn't exist) → context build crashes | Sim B3 | `bootstrap_project()`: mkdir + git init + user config, idempotent |
| B4 | HIGH | Git push has no auth/remote setup — commit always fails | Sim B4 | Bootstrap wires Gitea token remote; `setup_git_remote` documented |
| B5 | HIGH | Resume loses `current_task_idx` + `current_task` → resume crashes | Sim B5 | Checkpoint serializes FULL Task objects + index; resume reconstructs both |
| V4 | HIGH | DB path CWD-relative → silent second database | Red-team | `REPO_ROOT = Path(__file__).parent.parent`; absolute `engine.db` path |
| B9 | MED | Parser extracts User Stories as tasks for the real test-prd.md | Sim B9 + orchestrator | Boilerplate-section filter before ALL strategies; falls back to original if filter empties |
| B6 | MED | Multi-file extraction names files `task_T01_2.py` instead of `app.py` | Sim B6 | Filename recovery from `# file.py` label lines; strip label; suffix fallback |
| B12 | LOW | Non-python fences dropped (requirements.txt, Dockerfile) | Sim B12 | Regex now captures all fence types |
| B7 | MED | `check_imports` was a stub that always passed | Sim B7 | Concrete implementation: ast walk vs `sys.stdlib_module_names` + project imports + allowlist |
| B8 | MED | Permanent auth failures consumed all 5 retries | Sim B8 | Permanent-error detection (401/403/permission) → fail fast, no retry |
| B10 | MED | No SIGINT handler in new Engine | Sim B10 | `signal.signal(SIGINT → self.cancel())` in `Engine.__init__` |
| F7 | LOW | `tempfile.mktemp` race | Red-team | `mkstemp` + fdopen |
| F8 | MED | pytest not installed on Triton (handoff pending job dropped) | Conformance + Red-team | Epic 1 prerequisite checklist |
| R6 | HIGH | Handoff Rule 6 VIOLATED: engine tables merged into benchmark-results.db | Conformance | Separate `engine.db` everywhere (blueprint, migration, architecture, contracts) |
| M1 | HIGH | Migration breaks retained modules importing moved modules (e.g. engine_service→test_runner) | Red-team | Migration Step 5 rewritten: import audit per retained module, per-batch import check |
| M3 | MED | Shell scripts reference old top-level paths | Red-team | Migration step added to update bench scripts to `python3 -m bench.<mod>` |
| C12 | MED | ARCHITECTURE said "separate DB" while impl used one file | Conformance | Aligned to Rule 6 |
| Scope | MED | 15 chaos mitigations push Phase 1 from 4–6h toward 8h | Conformance | C3 (DB backup), C4 (lock), C9 (sidecar) triaged to Phase 3 (Hardening) |

---

## Goals Status After Fixes

| Goal | Before | After |
|------|--------|-------|
| 1. Dry-run produces task plan | Works but misleading modules | Works with corrected deliverable extraction |
| 2. Full run: generate→validate→test→commit | BROKE at 5 points | All breaks patched; reachable in simulation |
| 3. E2E < 5 min | Unevaluatable (crashed first) | ~2–3 min plausible for trivial PRD; 300s×N worst case |
| 4. SIGINT → checkpoint → resume | Resume crashed | Full state reconstruction (tasks, index, retries) |

## Probes to Run Before Implementation (from red-team)

1. exec-escape probe → must FAIL to modify host after fix (empty `__builtins__`)
2. Path-traversal probe → `parse_prd` must reject `..` modules (sanitizer added)
3. HTTPTransport auth probe → 401 today; Phase 1 fixes; re-probe must pass
4. write_file probe → transport lacks method (confirmed by grep); Phase 1 adds it or uses run_command+base64 (blueprint specifies both)
5. Import probe → `python3 -c "from engine import Engine"` after each migration batch

## Open Items (not blocking design, tracked for implementation)

- ~~Code-vs-contract exhaustive API diff~~ — **complete**; all 11 corrections (C1–C11) applied:
  - C1 (killer): BeeLlama response is FLAT (`total_tokens`, `thinking_tokens`) — nested `usage` parsing would record 0 tokens. Fixed.
  - C2 (killer): confirmed NO `write_file` on any transport — `write_project_file()` helper (base64 via `run_command`, works on all 3 transports) is the single write path. Fixed.
  - C3 (bug): `docker_exec` arg order misused in sandbox cleanup → `run_command("docker rm -f ...")`. Fixed.
  - C4: `prd_result` now carries `files_to_create`/`files_to_modify` lists + `prd_title` + `acceptance_criteria` so ManifestGenerator populates context_refs. Fixed.
  - C5/C6: `parse_prd()` now tries the existing rich `PRDParser` FIRST (module grouping, specs, deps); regex is fallback only. Mapping layer converts PRDParser dicts → `Task` (module = primary file, full `files` list preserved, category→task_type mapping). Fixed.
  - C7: absolute DB path (already fixed via V4).
  - C8: `engine.db` tracks its own `schema_version` (Rule 6: never bump benchmark DB's). Fixed.
  - C9: `emit_event` now bridges to `streaming_client.emit_event_fire_and_forget` when `STREAMING_URL` is set; callback failures can never crash the engine. Fixed.
  - C10: documented dedicated `/run-tests` endpoint as Phase 2 upgrade path (transport doesn't wrap it yet). Noted.
  - C11: import path verified — `from transport import get_transport` works when running from repo root (documented in MIGRATION troubleshooting). Noted.
- `check_syntax` stage is near-redundant with `check_ast`; kept for AD8's 4-stage contract, consider `py_compile`-based differentiation in implementation
- CORS wildcard on :3082 documented as LAN-only constraint

---

## Post-validation mission-integrity pass

- Self-scoring & benchmark-integration contract: `contracts/self-scoring-benchmark.yaml` (Phase-2 stub — defines judge integration, quality_scores table in engine.db, and benchmark run API contract; honors Rule 6)

---

## Mission-integrity pass

> Date: 2026-09 (final integrity gate before campaign-ready)
> Method: Cross-reference verification of the Mission Trace Table against all design artifacts; consistency sweep across design_drafts/; red-team of citations.

### What was added

1. **Mission anchor** — ARCHITECTURE.md §0 (lines 3–28): AGENTS.md project mission (line 3) and ACE definition (line 9) quoted verbatim, with Goal-Trace Table mapping 10 mission clauses to design sections and acceptance gates.
2. **Goal-trace table** — ARCHITECTURE.md §0 Goal-Trace: 10-row table mapping each AGENTS.md mission clause to design sections (§5 Execution Pipeline, §6 Module Map, §8 NFR, §9 Infrastructure, §11 Chaos) and acceptance gates from PRE_PRD.md.
3. **Epic 5 stub** — EPICS.md (lines 137–158): "Phase 2 stub" for self-scoring via judge + CCBS benchmark drivability, with 5 user stories and 3 acceptance criteria (AC5.1–AC5.3). Clearly marked as blocked on Phase 1 completion (Epics 1–4).
4. **Self-scoring contract** — `contracts/self-scoring-benchmark.yaml` (510 lines): Phase-2 stub defining (A) Judge Integration Contract (quality_scores table in engine.db, 5 scoring dimensions, score_task/score_run APIs), (B) Benchmark Run API Contract (run_benchmark, run_matrix for CCBS, BenchmarkReport dataclass), (C) Rule 6 compliance summary, (D) Integration points with existing modules.
5. **Parent-layer anchoring** — ARCHITECTURE.md §0 opens with AGENTS.md line 3 (project mission) and line 9 (ACE definition) quoted verbatim. EPICS.md Mission section (line 5) anchors the epic chain to the ACE mission.

### Red-team findings and resolutions

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| MI-1 | ARCHITECTURE.md goal-trace line citations for §5/§6/§8/§9/§11 are systematically ~20 lines off from actual file content | Low | Acknowledged: citations were written against pre-Mission-Anchor draft. Content is present; line numbers need updating. **NOT a content gap** — all cited sections exist with the described content. |
| MI-2 | EPIC-ENGINE-UNIFICATION.md §Module Interface Contracts `parse_prd()` cited at line 173 (target-state tree) not in Engine contract block | Low | `parse_prd()` is defined in IMPLEMENTATION_BLUEPRINT.md (lines 173–323), not in the Module Interface Contracts section of EPIC-ENGINE-UNIFICATION.md. The Engine.run() contract (line 326) calls `prd_parser.parse()` internally. Functionally correct; citation location is imprecise. |
| MI-3 | `contracts/engine-package.yaml` (v1) uses old class-based patterns (Builder, Strategy, Repository, Observer) inconsistent with v2 plain functions | Medium | v1 is a historical artifact from the Pragmatic Review v1 iteration. v2 (`engine-package-v2.yaml`) is the authoritative contract. **Recommend: deprecate v1 or delete to prevent confusion.** No action needed for campaign readiness since v2 is the implementation target. |
| MI-4 | ARCHITECTURE.md §1 "Overview" uses "one unified `engine/` package" but doesn't name the 12-state machine or one-CLI explicitly | Low | §4 "State Machine (12 States)" and principle 5 "One CLI, one engine, one entry point" cover both decisions. §1 is a summary paragraph. No content gap. |

### Goal-trace status (final)

| # | Mission Clause | Anchored at Mission Layer | Anchored at Epic Layer | Anchored at Design Layer | Anchored at Contract Layer | Phase |
|---|---|---|---|---|---|---|
| 1 | PRD → task decomposition | AGENTS.md §16 | EPICS.md US2, US14 | ARCHITECTURE.md §5 PARSE | engine-package-v2.yaml | Phase 1 |
| 2 | BeeLlama code generation | AGENTS.md §8, §14, §16 | EPICS.md US3–4 | ARCHITECTURE.md §5 GENERATE, §6 generator | engine-package-v2.yaml | Phase 1 |
| 3 | Quality gates (validation) | AGENTS.md §16 | EPICS.md US5 | ARCHITECTURE.md §5 VALIDATE, §8 NFR8 | engine-package-v2.yaml | Phase 1 |
| 4 | Self-repair / test loop | AGENTS.md §16 | EPICS.md US6–7 | ARCHITECTURE.md §5 Retry, §8 NFR5 | engine-package-v2.yaml | Phase 1 |
| 5 | Gitea commit | AGENTS.md §13, §16 | EPICS.md US8 | ARCHITECTURE.md §5 COMMIT, §6 committer | engine-package-v2.yaml | Phase 1 |
| 6 | Zero human intervention | AGENTS.md line 3, 9 | EPICS.md US1 | ARCHITECTURE.md §1, §5, principle 5 | engine-package-v2.yaml | Phase 1 |
| 7 | Crash-resilient checkpoint / resume | AGENTS.md §3, §4, §10 | EPICS.md Epic 2 US1–3 | ARCHITECTURE.md §7, §8 NFR3, §11 C3–C5 | engine-package-v2.yaml | Phase 1 |
| 8 | Self-scoring via judge | AGENTS.md §4, §6 | **EPICS.md Epic 5 (Phase 2 stub)** | — | self-scoring-benchmark.yaml (Phase 2) | **Phase 2** |
| 9 | Benchmark drivability for CCBS | AGENTS.md §5, §6, §8 | **EPICS.md Epic 5 (Phase 2 stub)** | — | self-scoring-benchmark.yaml (Phase 2) | **Phase 2** |
| 10 | Separate SQLite DBs (Rule 6) | AGENTS.md §8 | EPICS.md Mission | ARCHITECTURE.md §9, §11 C12 | engine-package-v2.yaml + self-scoring-benchmark.yaml | Phase 1 |

**Summary:** 8 of 10 mission clauses are fully anchored across all four layers (mission → epic → design → contract) and are scoped for Phase 1 implementation. 2 clauses (self-scoring via judge, CCBS benchmark drivability) are correctly flagged as Phase 2 gaps with Epic 5 stubs and a self-scoring contract. No contradictions found between documents on scope, state machine definition, Rule 6 compliance, or Epic 5 Phase 2 status.

### Consistency sweep results

| Check | Result |
|-------|--------|
| engine.db is only engine DB mentioned anywhere for engine data | ✅ PASS — ARCHITECTURE.md §9, MIGRATION_BLUEPRINT §Database Changes, blueprint state.py, self-scoring-benchmark.yaml all specify engine.db for engine data, benchmark-results.db untouched |
| 12-state machine stated identically everywhere | ✅ PASS — ARCHITECTURE.md §4, EPIC-ENGINE-UNIFICATION.md Pipeline State Machine, EPICS.md, blueprint pipeline.py, contract v2 all define the same 12 states (IDLE, PARSING, QUEUED, CONTEXT, GENERATE, VALIDATE, TEST, COMMIT, NEXT, DONE, FAILED, CANCELLED) with identical transition tables |
| One-CLI decision stated identically everywhere | ✅ PASS — ARCHITECTURE.md principle 5, EPIC-ENGINE-UNIFICATION.md §cli.py, blueprint §cli.py, EPICS.md US30 all specify single cli.py entry point |
| Epic 5 stub consistently marked Phase 2 | ✅ PASS — EPICS.md (line 137 "⚠️ Phase 2 Stub"), ARCHITECTURE.md §0 (row 8–9 "Gap"), self-scoring-benchmark.yaml (line 6 "PHASE-2 STUB") all consistent |
| No document contradicts another on scope | ✅ PASS — All documents agree: Phase 1 = Epics 1–4 (engine kernel, hardening, E2E validation, consolidation); Phase 2 = Epic 5 (self-scoring, CCBS). MIGRATION_BLUEPRINT Step 2 confirms bench/ receives judge.py. VALIDATION_REPORT.md R6 fix confirms engine.db separation. |
| Contract v1 vs v2 discrepancy | ⚠️ Minor — `contracts/engine-package.yaml` (v1) uses old class-based patterns; `contracts/engine-package-v2.yaml` (v2) uses plain functions. v2 is authoritative. v1 is a historical artifact. |

---

## Backtest & ticket-machine design pass

> Date: 2026-09 (Phase 2 design propagation)
> Method: Propagation of the two new authoritative designs — BACKTEST-DESIGN.md (atom-level replay) and TICKET-STATE-MACHINE.md (ticket-ledger state machine) — into the lateral project files.

### Designs authored

| Design | Location | Key rails |
|--------|----------|-----------|
| Backtesting Design (atom replay) | `design_drafts/BACKTEST-DESIGN.md` | Determinism contract (recorded BeeLlama response, byte-identical replay), 4 replay granularities (atom → ticket → epic → PRD), 6 NFRs (NFR-B1 through NFR-B6), phase-2 tickets T60–T66 |
| Ticket-State-Machine (ticket ledger) | `design_drafts/TICKET-STATE-MACHINE.md` | 12-state ticket lifecycle, bounded fix loops, escalation gates, atom-level trace integration shared with backtesting design |

### Where the designs live

- **BACKTEST-DESIGN.md** — authoritative backtest design (§1–§7: problem, definitions, architecture, engine-side changes, debugging workflow, NFRs, blind spots plugged)
- **TICKET-STATE-MACHINE.md** — ticket-ledger state machine (§5: atom schema shared with engine atoms)
- **Campaign orchestrator** — `campaigns/engine-unification/orchestrator/` (campaign-side tooling for both designs)
- **Engine contracts** — `contracts/engine-package-v2.yaml` (engine module definitions)

### What was propagated

| File | Addition | NFRs referenced |
|------|----------|-----------------|
| `EPICS.md` | Epic 6 (Phase 2): Backtesting & Atom Replay — goal, 4 user stories, 3 acceptance criteria (AC6.1–AC6.3) | NFR-B1, NFR-B3, NFR-B4 |
| `ARCHITECTURE.md` | `engine/trace.py` (AtomRecorder) added to module map as Phase-2 module; determinism contract subsection added after module map | — |
| `PRE_PRD.md` | AC6.1 (100 replays identical hash), AC6.2 (mutation detection ≥95%), AC6.3 (bisect ≤2min/50 atoms) | NFR-B1, NFR-B3, NFR-B4 |
| `compacted_state_0.json` | Decision-log entry: ticket-ledger + backtest design authored | — |
| `orchestration-layer-design.md` | ACE section extended: ticket-ledger state machine + atom-replayable runs | — |

### Key design rails preserved across all propagations

1. **Determinism contract** — mock transport replays recorded BeeLlama responses byte-for-byte; no LLM re-generation during replay
2. **Rule 6 compliance** — all traces in `engine.db` only; never `benchmark-results.db`
3. **Phase 2 scope** — blocked on Epics 1–4; no implementation effort until Phase 1 complete
4. **Shared atom model** — campaign atoms (ticket-machine gates) and engine atoms (pipeline transitions) use the same replay/forensics tooling
