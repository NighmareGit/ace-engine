# PRD: Research Task Type (v1)

Source epic: `EPIC.md` (authoritative). Amendments: `GRILL-S4.md`
(7 deltas + owner answers supersede DESIGN-S2 where they differ).
Machine PRD: `.scratch/research-task-type/PRD-A-set/PRD-R01.json`.
Gitea issue #43.

## Problem Statement

The ACE engine gates code atoms with deterministic validators and tests,
but has no way to execute or verify research work. An exploration task —
e.g. the beellama tiered-memory port research — produces prose today,
with nothing distinguishing a rigorous verdict from confident mush. The
S9 lesson generalizes: any gate that checks artifact *shape* rather than
*substance* passes hollow work.

## Solution

Add a `research` task type behind a `TaskHandler` strategy dispatch: the
atom emits a cited `verdict.md` (`[claim-N]`/`[src-N]` markers) and a
deterministically post-processed `verdict.evidence.json` sidecar. The
evidence gate is two-layer: a staged deterministic validator (7 checks
over the claim→source graph, including source-graph cycles, spans,
density, verdict position, answer-the-question) plus an LLM entailment
judge (5 dimensions, threshold 6.0). file:// sources are content-verified
in v1. Everything else (session logs, budgets, autopsy, re-plan, ralph
rounds) is reused unchanged.

## User Stories

1. As an operator, I want to hand the engine a research question from a
   PRD and get back a committed, evidence-gated verdict document, so
   that exploration work is as accountable as code work.
2. As an operator, I want every claim in a verdict traceable to a
   file on disk with a matching span, so that I can audit any claim in
   seconds.
3. As the engine, I want research atoms to reuse the same state machine,
   budgets, session logs and re-plan machinery, so that the new task
   type costs no new operational surface.
4. As a reviewer, I want the deterministic floor to reject hollow
   verdicts before the LLM judge sees them, so that judge tokens are
   spent only on structurally sound work.
5. As an epic owner, I want a multi-round research epic (like beellama)
   to map onto ralph rounds, each round committing one verified verdict.

## Success Metrics

- Evidence oracle rejects ≥90% of the 10-case bad-verdict corpus;
  the valid case passes (zero false positives).
- Beellama dogfood: R01–R08 verdicts committed, each ≥6.0, each with
  content-verified file:// sources where applicable.
- Zero behavior change on code atoms (full suite green; one
  `task_type` branch in engine.py).

## Out of Scope

HTTP source accessibility (v2), G13 divergence signals, research
autopsy section, multi-round aggregation module, any code-atom change.
