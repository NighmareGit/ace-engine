# Deployed-copy divergence — Triton `~/projects/coder-harness-frozen-20260909`

Audited 2026-09-09 against `epic5-self-scoring` @ `bbab9fe` (repo HEAD `7604745`).
The deployed checkout was frozen and renamed (was `~/projects/coder-harness`); it
sits at stale commit `5c92fba` with uncommitted deployed-state drift (issue 08).
Do NOT clean or "fix" it in place — this note is the record.

## Findings

| Item | Status vs our HEAD |
|------|--------------------|
| Root-level engine scripts (`engine_service.py`, `transport.py`, adapters, streaming_*) | Stale copies; `transport.py` fixes (shlex quoting, ENGINE_TOKEN) already merged upstream |
| `tests/engine/`, `tests/e2e/`, `tools/` (root copies) | Older; tracked tree is newer (e2e gained `_require_functional` assertions) |
| `engine_service.py` default port | **Deployed copy defaults to 8082** and predates the Token-not-bearer auth fix. Our HEAD: 8080 default, Token scheme. Implication: Epic-5 judge/model wiring must pin ports explicitly (env `BEE_LLAMA_3090`/`BEE_LLAMA_3070`), never rely on the default. |
| `beellama-kvarn-deploy/*`, `Dockerfile.engine` (modified) | Deployed-state drift — issue 08, intentionally left uncommitted |
| `engine.db`, `engine.db.bak`, `benchmark-results.db` (root) | Live runtime state. Backed up to `research/harvest/host-db-backup-20260909/` (6 early engine runs: 3 FAILED, 2 DONE, 1 NEXT; root `benchmark-results.db` was a 0-byte empty artifact). |
| `.env.streaming` | Contains STREAMING_TOKEN — stays on disk, never committed (Rule 13) |
| `.engine_state`, `engine_run_*.json`, `corpus.json`, `index.html`, `test-prd.md`, `e2e-final-result.json` | Ephemeral runtime artifacts — gitignored on this branch |

## DB hygiene (this branch)

`engine.db`, `engine.db.bak`, `benchmark-results.db` (incl. the `tickets/ccbs/`
copy) are untracked as of this branch: live DB state must never ride git (merge
traps on binary blobs; Rule 6 hygiene). Old snapshots remain in git history.
`research.sqlite` stays tracked (intentional ledger sync).
