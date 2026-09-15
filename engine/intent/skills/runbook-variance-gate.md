# Skill: Variance Gate Runbook

**Skill ID:** `runbook-variance-gate`
**Purpose:** House procedure for deciding whether a run's score variance
crosses the gate that triggers a deeper investigation.

## When to Use

Invoke this skill when:
- A completed run's score standard deviation exceeds 1.5 points (0-10 scale).
- The owner asks "why did this run vary so much?".
- You are assembling the final report and need to flag high-variance runs.

## Procedure

1. **Read the scores.** Query `engine_scores` for the run. Compute the
   standard deviation across the five dimensions (completeness, correctness,
   quality, intelligence, role_fit). If sigma <= 1.5, the gate passes — no
   action.

2. **Identify the outlier dimension.** Find the dimension whose score is
   furthest from the run mean. That is the axis the run is inconsistent on.

3. **Cross-reference the task.** Read the task definition for the task(s)
   that scored low on the outlier dimension. Check whether the task's
   acceptance criteria were ambiguous — ambiguity is the most common root
   cause of variance.

4. **Emit a finding.** If the variance is unexplained by task ambiguity,
   record a G5_JUDGE_LOW_SUBCLUSTER signal note with the evidence
   (run_id, dimension, sigma, task_id).

5. **Do NOT mutate the run.** This skill is read-only (ADR-0002). It informs
   the owner's triage; it never edits tasks or scores.

## Output Format

    Variance Gate — run <run_id>
      sigma = <value>  [PASS|FAIL]
      outlier dimension: <dim> (score <n>)
      root cause: <ambiguity | model limitation | unknown>
      action: <none | flag for owner triage>

## Notes

- This skill is documentation only — it is loaded as context into the
  intent/native prompts, not executed as code.
- The threshold (sigma > 1.5) is a house default. Flag to the owner before
  changing it.
