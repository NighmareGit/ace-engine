# PRE-EPIC: ace-meta-cognitive-ralph-loop

Status: PRE-EPIC (design only — no implementation authorized by this document)
Date: 2026 (ledger era 43+)
Owner goal: give the ACE engine a native meta-cognitive Ralph loop — the skill
`meta-cognitive-ralph-loop` (installed in the skunkworks toolbox) becomes an
engine-level workflow: fresh-agent iterative execution with divergent ideation
and adversarial testing, driven through ACE's deterministic state machine.

---

## 1. Why (motivation)

- ACE today has: T4 re-plan brain (bounded), lane B+ (code-as-intent),
  escalation ladder (9B→35B→oracle), TGD signals, skill-as-docs registry
  (currently empty → G9).
- The `meta-cognitive-ralph-loop` skill provides what ACE lacks: **fresh-agent
  rounds with no conversation carry-over**, bounded structured hand-off between
  rounds, divergent ideation before convergence, adversarial verification.
- Mapping: a Ralph round ≈ ACE "run"; the shared workspace ≈ engine.db +
  workspace; the bounded round report ≈ checkpoint/replan artifact. The skill's
  loop should become a **first-class engine workflow**, not an external script
  poking at ACE.

## 2. The state machine (pre-epic workflow)

Phases S0–S10. Each phase has an artifact + exit gate. S0–S4 are design
phases (human-in-the-loop at gates); S5–S10 are execution phases.

```
S0 INTAKE ──► S1 ANALYZE ──► S2 REDESIGN ──► S3 FIREPLACE ──► S4 EPIC
                                                                 │
                                                  grill-with-docs│(S5)
                                                                 ▼
                                              S6 REFINE ──► S7 TO-PRD/TO-TICKETS
                                                                 │
                                              tickets ready ─────┘
                                                                 ▼
                                    S8 ACE-EXECUTE (feed tickets to ACE engine)
                                                                 ▼
                                    S9 REVIEW (code-review on ACE outputs)
                                              │ all green
                                              ▼
                                    S10 INTEGRATE (codebase-design, one-shot
                                         merge into ace main repo, push = rollback point)
```

### S0 INTAKE
- Pin the skill source: locate `meta-cognitive-ralph-loop` skill files
  (toolbox install), copy canonical text into
  `.scratch/ralph-loop/research/skill-source.md`.
- Exit gate: skill source verbatim + checksum recorded.

### S1 ANALYZE (code-review of the skill)
- Run `code-review` + `brooks-review` lens over the skill definition:
  what does the loop actually guarantee (fresh context per round, report
  boundary, workspace-as-memory)? Where does it assume a human? Where does it
  trust the workspace (which ACE treats as untrusted-model output)?
- Exit gate: analysis doc listing (a) loop invariants, (b) implicit
  human-only steps, (c) security/reliability gaps vs ACE's threat model
  (LLM output is untrusted → sandbox, budget caps apply).

### S2 REDESIGN (codebase-design for ACE usage)
- Skill: `codebase-design`. Decide module placement: propose
  `engine/workflows/ralph/` (round driver, report schema, ideation protocol)
  vs. expressing Ralph as ACE task-graph / lane-B harness. Key question: is
  Ralph a **new state lane** (parallel to GENERATE) or a **workflow over
  existing states** (run-loop calling the existing pipeline N times)?
- Exit gate: design doc with module boundaries, interfaces, seams; ADR draft
  for the "workflow vs lane" decision.

### S3 FIREPLACE (how does ACE *issue* a ralph run?)
- Skill: `fireplace`. Frames to explore:
  1. Task verb: `run_ralph(objective)` as an ACE action in
     `routes/ace_actions.json` (loop as an invocable action).
  2. Loop-over-runs: outer orchestrator starts N ACE runs; each run is one
     Ralph round; round report = structured hand-off (uses existing
     checkpoint/persistence).
  3. Lane-B extension: `run()` primitives gain a `ralph_round()` primitive
     (StopIteration halt + report injection, depth ≤2, budget-capped).
  4. External harness (status quo): keep the loop outside the engine.
- Uses outputs of S1+S2 as grounding.
- Exit gate: ≥4 framed options with trade-offs; pick one (owner sign-off).

### S4 EPIC CREATION
- Synthesize S1–S3 into the epic `ace-meta-cognitive-ralph-loop`
  (`docs/epics/` or Gitea epic issue): features, non-features, NFRs
  (budget caps per round, sandbox enforcement for round code, stop/timeout
  semantics reuse T5), success criteria.
- Exit gate: epic doc committed.

### S5 GRILL (grill-with-docs)
- Run `grill-with-docs` on the epic → produces question set + ADRs/glossary.
- A **planner-type subagent (high reasoning effort)** answers the questions
  against the codebase; owner resolves conflicts.
- Exit gate: question/answer log; epic updated or questions marked wontfix.

### S6 REFINE (adversarial top-up)
- Red-team pass over the refined epic: what features on top? blind spots?
  pipeline gaps? (e.g., round-report poisoning, budget exhaustion mid-round,
  fresh-agent with zero memory vs engine.db trust, infinite divergent loops.)
- Exit gate: blind-spot list triaged into epic features / ledgered knowns.

### S7 TO-PRD → TO-TICKETS
- `to-prd` publishes the PRD (Gitea issue, ready-for-agent) → `to-issues`
  slices tracer-bullet tickets, dependency-ordered.
- Exit gate: scoped ticket set; **owner GO** before S8.

### S8 ACE-EXECUTE (the dogfood)
- Feed tickets to the ACE engine as its next real workload (RLM ticket schema
  v1 → adapter, as proven on PRD #8). N4-style pass-rate tracked per ticket.
- Exit gate: ACE run report; failures either re-scoped (feedback into S7) or
  escalated per ladder.

### S9 REVIEW
- `code-review` on ACE's committed outputs (run/ branches). Gate: all green
  or targeted fix round (loop S8→S9 max 2 iterations).
- Exit gate: review verdict per ticket.

### S10 INTEGRATE
- **Pre-commit a rollback point on ace main repo (commit + push) BEFORE any
  integration.** Then one-shot integration designed with `codebase-design`
  (single cohesive change, not ticket-by-ticket), full test suite, push.
- Exit gate: main branch green + tests pass; ledger entry.

---

## 3. Open questions (pre-epic — to be resolved in S2/S3/S5)

1. **Workflow vs lane**: is Ralph a run-loop over the existing pipeline, or a
   new engine state lane? (Lean: run-loop — reuses VALID_TRANSITIONS, T5, T8
   budget caps, checkpointing for free.)
2. **Round memory boundary**: what exactly crosses the round boundary? (Lean:
   same contract as T4 re-plan report — typed, validated, small.)
3. **Fresh-agent execution substrate**: ACE spawns fresh agent contexts
   itself (lane-B style) vs delegating to the outer DSH ralph tool.
4. **Divergent ideation under budget**: ideation rounds burn LLM calls — how
   do T8 caps and depth ≤2 interact with a loop that is *designed* to
   iterate? Need a `max_ralph_rounds` cap like the DSH ceiling.
5. **Adversarial testing in-pipeline**: judge (35B) as the adversarial tester,
   or dedicated oracle escalation (P6)?
6. **G9 synergy**: the Ralph loop's skill-as-docs usage could seed the skill
   registry, killing the systemic G9 signal — fold in or separate epic?

## 4. Explicit non-goals (for now)

- No merges to main without S10's rollback-point commit.
- No Ralph loop for ACE's own development in this epic (dogfooding is S8
  output, not S10 input).
- No change to ADR-0002 (LLMs edit task definitions, never engine code) —
  the Ralph loop drives ACE; humans + S10 integrate.

## 5. Suggested additions on top (owner's "thoughts?" prompt)

- **Round ledger**: persist each Ralph round as a typed row (like
  `laneb_events`) → replayable, TGD-signalable (new signals G11: round churn,
  G12: ideation-collapse = same solution repeated N rounds).
- **Self-improvement guardrail**: the skill advertises "self-improving" — in
  ACE terms that must mean *task definition* improvements only (ADR-0002),
  never engine code edits. Make it a mechanical gate, not a convention.
- **Resume semantics**: reuse task-state/checkpointing so a killed round N
  resumes at N with the N-1 report — matches the existing CANCELLED path.
- **Metrics**: reuse N4 framing — define "Ralph pass rate" (objective achieved
  within M rounds) as the epic's headline success criterion.
