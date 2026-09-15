# ADR 0001 — Per-run-id results archive in a dedicated Gitea repo

**Status:** proposed
**Decided:** 2026-09-09
**Linked design:** `docs/epic5-hardening-design.md` §1 (Results store wiring)

## Context

Live-run failures (50 % run-failure rate, see
`research/epic5-first-run-failures-2026-09-09.md`) are hard to diagnose because
engine state is trapped in a single SQLite file on Triton (`engine.db`) with no
per-run snapshot and no push-button retrieval. `engine_task_results` is empty at
runtime (`save_task_result` is never called), so the only forensic trace is
ephemeral retry prompts. When a run finishes — successfully or not — there is no
artifact a human or the *CCBS* ranking layer can pull without SSHing into Triton
and hand-copying a live DB file that the engine may still be writing to.

A dedicated Gitea repo `<user>/ace-results` already exists for run-results
archives. The question is **what** goes in it, **when**, and **how it is kept from
bloating**.

## Decision

Store **both** a snapshot of `engine.db` and a JSON `report.json` per `run_id`
under `<user>/ace-results/archive/<run_id>/`, committed automatically at the end
of `Engine.run()` and re-exposed as an `ace archive <run_id>` CLI command. Control
bloat by retaining `report.json` forever (small, diffable) and capping retained
`.db` snapshots per project (default latest 30, prunable via `ace archive prune`).

The canonical runtime engine DB moves to
`ENGINE_DB_PATH=/home/<user>/ace-results/engine.db` (Rule 6: never
`benchmark-results.db`).

## Consequences

**Positive**
- Every run — success or failure — produces a retrievable, versioned artifact.
  Autopsies (`fetch_run` / `ace archive pull`) no longer need live Triton access.
- Dual artifacts separate concerns: `report.json` is the queryable/diffable
  durable record; the `.db` snapshot is a faithful replay of full engine state
  for the few cases that need it.
- Automatic archival at end-of-`run()` means no caller can forget to archive;
  the CLI re-exposure covers re-archives and ad-hoc forensics.

**Negative / trade-offs**
- SQLite in a Git repo does not diff or merge; unbounded `.db` commits bloat the
  clone. The retention cap + prune command mitigate this but add a maintenance
  surface (`archive_keep` config, prune cadence) the owner must tune (deferred:
  confirm `archive_keep` default).
- Two artifact formats means two readers must stay in sync (the JSON schema is
  the `RunReport` contract, already unit-tested — this constrains drift).
- Pushing to Gitea on every run adds latency and a failure mode to the run path;
  archival must be best-effort and never fail the run (fire-and-forget with a
  warning on push failure).

## Alternatives considered

1. **JSON-only archive** (no `.db` snapshot). Simplest, no bloat. Rejected: loses
   the ability to replay full engine state (e.g. to reconstruct a corrupted
   mid-run checkpoint). Kept `.db` under a cap as the lower-frequency forensic
   tier.
2. **Per-run-id separate repos** (one Gitea repo per run). Rejected: explodes
   repo count, makes cross-run queries (CCBS ranking across runs) expensive, and
   fights Gitea's permission model.
3. **Archive only on failure**. Rejected: successful runs are the baseline for
   variance reporting (REAP lesson); needing them later and not having them
   recreates the original diagnosability gap.
4. **Push raw state to the existing `engine.db` without versioning**. Rejected:
   exactly the status quo problem — no per-run retrieval, no history.

## Consequences of not doing this

Without a per-run archive, every failure analysis requires live Triton access and
a manual DB copy, the variance signal from successful runs is unrecorded, and the
CCBS ranking layer has no stable input. The 50 %-failure-rate root-cause work
that produced this design would not have been possible at pace.
