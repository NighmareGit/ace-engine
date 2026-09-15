# Issue tracker — <user>/ace-engine (Gitea)

This repo tracks issues on self-hosted Gitea: `<user>/ace-engine` at
`http://<LAN_IP>:3000` (owner: `<user>`).

## How to interact

Auth: Gitea API token. On Triton it is provisioned as `GITEA_TOKEN` in
`/home/<user>/shared-configs/engine.env` (source it). Locally, `tea` is
logged in; otherwise use HTTP basic auth `<user>:<GITEA_TOKEN>`.

- List open issues:
  `tea issue ls` (repo context) or
  `curl -u <user>:<token> .../api/v1/repos/<user>/ace-engine/issues?state=open`
- Create: `tea issue create -t "<title>" -d "<body>" -L <label>`
  or `POST /api/v1/repos/<user>/ace-engine/issues` JSON
  `{"title": ..., "body": ..., "labels": [<id>]}`
- Comment/close: `POST .../issues/<index>/comments`,
  `PATCH .../issues/<index>` `{"state": "closed"}`
- Labels: `GET/POST .../repos/<user>/ace-engine/labels` — apply via the
  issue PATCH/POST `labels` field (label IDs are repo-local).

## Conventions

- External PRs are **not** a triage surface (no external contributors).
- Titles: imperative, <= 72 chars; body: symptom / acceptance / evidence.
- Every issue carries one state label (triage roles) and may carry
  `bug`/`enhancement` + `P1`/`P2`/`P3`.
- Run outputs and evidence links point to `<user>/ace-results` or the
  per-project run repos, never into this repo's tree.

## Migration note

Issues 15 (resolved), 17, 19, 20 were migrated from the parent
monorepo's local tracker (`dsh-hub/.scratch/open-loops/`, ledger:
`open-loops-ledger.md`) on 2026-09-09. Issue 15 is recorded here as a
closed reference issue; 17/19/20 are open. The parent ledger marks them
as moved and remains the tracker for non-engine (DSH-side) work.
