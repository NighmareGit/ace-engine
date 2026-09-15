# S7: Ticket Slice — RL1 implementation tickets

**Status:** COMPLETE (all 10 tickets implemented, verified, and integrated)

Generated 2026-09-09. All tickets created on Gitea with label `ready-for-agent`.
Parent PRD: Gitea issue #13 (http://<LAN_IP>:3000/<user>/ace-engine/issues/13).

## Ticket table

| Issue # | Title | Depends on | Stage |
|---------|-------|------------|-------|
| #14 | RL1-1: Tracer bullet — report_types + config + minimal round_driver + R7 prompt-audit test | none | (a) core library |
| #15 | RL1-2: Mechanical gate enforcement — gates.py (R1, R4, ADR-0004) | #14 | (a) core library |
| #16 | RL1-3: Round-driver revert + cumulative budget clamp (M1, M2, R3) | #14, #15 | (a) core library |
| #17 | RL1-4: run_ralph action route + CLI seam (F4-d synchronous-but-resumable) | #14, #16 | (b) action wrapper |
| #18 | RL1-5: ralph_runs schema + resume_ralph + status CLI (M4, M7) | #14, #16 | (c) resume + ledger |
| #19 | RL1-6: RalphJsonlHandler + ralph_report.json (M5, M6) | #14, #16 | (c) telemetry + report |
| #20 | RL1-7: Divergent ideation + similarity.py + G12 collapse detection | #14, #15 | (d) ideation |
| #21 | RL1-8: Pattern registry — pattern_gate + ralph_patterns + approval CLI (R2, R6) | #14, #18 | (e) pattern registry |
| #22 | RL1-9: pattern_query + pattern_export projection (R2, R6) | #21 | (e) pattern registry |
| #23 | RL1-10: G13 gate-gaming detection + workspace denillist + R7 denylist test (M3, M8) | #14, #16, #19, #21 | (e) projection + finalization |

## Dependency chain (linearized)

```
#14 (tracer bullet)
  ├── #15 (gates)
  │     └── #20 (ideation + similarity + G12)
  ├── #16 (revert + budget)
  │     ├── #17 (action route + CLI)
  │     ├── #18 (resume + status)
  │     │     └── #21 (pattern registry)
  │     │           ├── #22 (pattern query + export)
  │     │           └── #23 (G13 + denillist)
  │     └── #19 (JSONL handler + report)
  │           └── #23 (G13 + denillist)
  └── #23 (G13 + denillist, also depends on #16/#19/#21)
```

## M1–M8 must-fix mapping

| Must-fix | Ticket | Module |
|----------|--------|--------|
| M1 revert mechanism (git revert) | #16 | round_driver |
| M2 cumulative budget clamp | #16 | round_driver + budget |
| M3 G13 gate-gaming + max_patterns_per_run | #23 | gate_gaming + pattern_store |
| M4 status CLI | #18 | cli.py |
| M5 JSONL handler | #19 | ralph_jsonl |
| M6 ralph_report.json | #19 | round_driver + RalphResult |
| M7 ralph_runs schema | #18 | ralph_runs |
| M8 workspace denillist | #23 | round_driver + R7 test |

## S5 grill resolution mapping

| Resolution | Ticket(s) |
|------------|-----------|
| #1 Ideation rubric (JSON scoring, one-retry, mechanical novelty) | #20 |
| #2 Revert safety (git revert, revert_round emits ralph_round_failed) | #16 |
| #3 Pattern approval (async CLI, auto-approve kill switch) | #21 |
| #4 Resume semantics (failed/cancelled only, BEGIN IMMEDIATE, dirty stash) | #18 |
| #5 Pattern search (LIKE AND, no FTS5, score DESC) | #22 |
| #6 Report sanitization (schema + maxLength + sandbox) | #14 (R7 test) |
| #7 Model routing (ideation 9B :8082, judge 35B :8080) | #15, #20 |
| #8 G12 collapse (TF-IDF ≥ 0.75, 2 consecutive, forced-diversity) | #20 |
| QB Judge-gate boundary (ADR-0004) | #15 |

## Slicing deviations from the stage plan and why

1. **Stage (c) split into three tickets (#18, #19, partial #23).** The epic groups "resume + round ledger" as one stage, but resume (#18), JSONL/report (#19), and G13 finalization (#23) touch distinct modules (ralph_runs.py, ralph_jsonl.py, gate_gaming.py) with different test surfaces. Splitting keeps each ticket ≤2 modules and lets #19 (telemetry) proceed in parallel with #18 (resume) once #16 lands.

2. **Stage (e) split into three tickets (#21, #22, #23).** Pattern registry, query/export, and G13/denillist are separable: #21 owns the table + approval gate, #22 owns read/query + export, #23 owns the cross-cutting G13 signal + denillist. #22 is purely read-only over #21's table, so it can be implemented independently once the schema exists.

3. **G13 + denillist placed last (#23) rather than with pattern_gate.** G13 requires the full round ledger (from #16), the telemetry chain (from #19), and the pattern cap (from #21). It is the natural integration/verification ticket that closes the loop on M3 and M8 after all data-producing modules exist.

4. **similarity.py (#20) extracted as a shared primitive in engine/intent/.** The epic explicitly calls for \`engine/intent/similarity.py\` to be reused by both the IIL router and G12. This ticket owns that extraction; the router itself is not modified (its existing TfidfVectorizer usage is the provenance, not the target).

5. **R7 prompt-audit test split across #14 and #23.** The base R7 test (three allowed inputs) lands in the tracer bullet (#14) to prove the round-boundary contract immediately. The denillist extension (M8) lands in #23 because it depends on \`RalphConfig.workspace_denylist\` being wired into the round driver, which happens alongside G13 finalization.

## Implementation completion

All tickets RL1-1 through RL1-10 were implemented as part of the S10 integration:

| Ticket | Status | Commit |
|--------|--------|--------|
| #14 | COMPLETE | 10eb077 (part of ace-meta-cognitive-ralph-impl) |
| #15 | COMPLETE | 10eb077 |
| #16 | COMPLETE | 10eb077 |
| #17 | COMPLETE | 10eb077 |
| #18 | COMPLETE | 10eb077 |
| #19 | COMPLETE | 10eb077 |
| #20 | COMPLETE | 10eb077 |
| #21 | COMPLETE | 10eb077 |
| #22 | COMPLETE | 10eb077 |
| #23 | COMPLETE | 10eb077 |

**Integration commit:** `10eb077`
**Suite status:** 956 passed / 4 skipped on main
**Issue #24:** Closed via API (feature branch snapshot superseded by main integration)
