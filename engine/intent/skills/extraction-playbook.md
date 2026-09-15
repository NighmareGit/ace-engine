# Skill: Technique Extraction Playbook

**Skill ID:** `extraction-playbook`
**Purpose:** House procedure for extracting a technique from an external
codebase (research steal-candidate) into ACE's own code, without copying
license-blocked content.

## When to Use

- A research brief names a concrete technique worth extracting.
- The owner approves the extraction target.
- You need to bring a pattern in from smolagents, rlm, semantic-router, etc.

## Procedure

1. **Pin the source.** Record the exact commit hash and file path of the
   source implementation. A floating reference is not acceptable — the
   extraction must be reproducible.

2. **Read, don't copy.** Study the technique's structure (the guards, the
   circuit breakers, the dispatch shape). Write the ACE version from
   understanding, not by transcribing lines. This avoids license contamination
   and forces the technique into ACE's own idioms.

3. **Name the hook.** Every extraction names the ORCH hook it feeds
   (ORCH-1 through ORCH-8). If it doesn't map to a hook, it doesn't enter
   the core — it lives in a skill or a sidecar.

4. **Write tests first (TDD).** The extracted technique must have tests that
   fail before the extraction and pass after. Target: the new code is covered
   at >=90% on the decision logic.

5. **Record the provenance.** In the module docstring, cite the source
   (paper, repo, commit) and the license. If the source is GPL or
   cc-by-nc, flag to the owner before merging — some licenses are blockers
   for the core.

6. **Verify no new runtime deps.** A technique that requires a new pip
   package must pass the "no new deps" gate (spec invariant). Pure-stdlib
   or already-present deps only.

## Anti-patterns

- Do NOT copy-paste 400 lines and change the names. That is contamination,
  not extraction.
- Do NOT extract into the orchestrator core what belongs in a skill.
  Skills compound; core changes require evidence.

## Output

A new module under `engine/orchestrator/` (or `engine/intent/`) with:
- Module docstring naming the source + license.
- Tests under `tests/engine/`.
- An ADR entry if the extraction changes a contract.
