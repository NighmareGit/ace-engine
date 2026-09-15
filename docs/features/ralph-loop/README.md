# Feature: ace-meta-cognitive-ralph-loop

The Ralph loop as a first-class ACE engine workflow: fresh-agent rounds with
divergent ideation, mechanical post-commit gates, and a machine-queryable
pattern store — built on the `meta-cognitive-ralph-loop` skill design.

- Gitea epic tracker: PRD issue **#13**, tickets **#14–#23** (RL1-1…RL1-10)
- ADRs: `docs/adr/0003` (draft, workflow over states — full text in
  `research-s2-redesign.md` §8), `docs/adr/0004-ralph-gates-consume-llm-judge.md`
- Glossary terms: `CONTEXT.md` (Ralph round/run, RoundReport, Ralph gate,
  Pattern, Pattern projection, Ideation, Ideation collapse)

## Document map

| Doc | What it is |
|-----|-----------|
| `PRE-EPIC.md` | Original idea + S0–S10 workflow state machine (design authorization) |
| `EPIC.md` | The epic: R1–R7 hard requirements, module map, deliverables; **S5 Grill resolutions** and **S6 Red-team amendments** sections are authoritative |
| `PRD-v1.md` | Published PRD (Gitea #13): problem/solution, 23 user stories, implementation + testing decisions |
| `TICKETS.md` | Ticket slice map: #14–#23, dependency chain, M1–M8 mapping |
| `research-skill-source.md` | S0: pinned `meta-cognitive-ralph-loop` skill source (canonical tree, sha256 manifest) |
| `research-s1-analysis.md` | S1: skill analysis — invariants, harness-trust steps, gaps vs ACE threat model |
| `research-s2-redesign.md` | S2: redesign — workflow-vs-lane decision, module map, RoundReport contract, gates, ADR-0003 draft (+ owner pattern-store amendment) |
| `research-s3-fireplace.md` | S3: issue-surface decision (F4-d synchronous-but-resumable action) |
| `research-s6-redteam.md` | S6: red-team findings F1–F14, M1–M8 must-fixes, add-on features |

(S5 grill decisions are recorded inside `EPIC.md` and ADR-0004/CONTEXT.md;
the S7 slice produced the Gitea tickets directly.)

## Status

**Implementation complete (S0–S10).** 
- Design complete (S0–S7) → engine dogfood ×3 runs (N4 10%→0% honest→63.3%/90% with 35B ladder)
- S9 rejected engine-generated output → direct implementation
- S10: rollback tag `pre-ralph-integration` → merged to main — full suite **956 passed / 4 skipped**
- Issue #24 closed via API (feature branch snapshot superseded by main integration)

Tickets #14–#23 implemented and verified: round_driver, gates, ideation, pattern_gate, pattern_query, config, report_types, CLI, ace_actions.json route, similarity, and all tests.
