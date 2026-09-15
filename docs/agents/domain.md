# Domain docs — <user>/ace-engine

Layout: **single-context**.

- `CONTEXT.md` (repo root) — domain glossary and ubiquitous language
  (engine, pipeline, judge, subject, run, cell, scored_state, …).
- `docs/adr/` — architectural decision records:
  - `0001-results-archive-per-run-id.md`
  - `0002-judge-steered-retry-and-escalation.md`
  - `0004-ralph-gates-consume-llm-judge.md` (narrows 0002: Ralph-gate context)
- `docs/features/` — feature bundles (docs + research for one feature):
  - `ralph-loop/` — ace-meta-cognitive-ralph-loop (start at its `README.md`;
    includes the ADR-0003 workflow-over-states draft in
    `research-s2-redesign.md` §8)

Consumer rules (for skills that read domain docs):

1. Read `CONTEXT.md` before generating or reviewing code that touches
   the engine domain; use its terms verbatim.
2. Before proposing architecture changes, check `docs/adr/` for an
   existing decision; if one covers the change, follow it or write a
   superseding ADR (next number, links back).
3. There is exactly one context — do not create per-module CONTEXT
   files. If the engine grows distinct sub-domains, re-evaluate via a
   new ADR.

## docs/features/

- `observability/` — session logs, tracing, autopsy (EPIC + PRD + 8 tickets, Gitea #25-#33)
- `skills-personas/` — skill lifecycle, effectiveness telemetry, persona system (EPIC + PRD + 8 tickets, Gitea #34-#42)
