# Engine Observability — Session Logs, Tracing, Autopsy

**Status: IMPLEMENTED** — all 8 tickets (OBS-01 through OBS-08) complete on
`feature/engine-observability`.

**Integration complete:** Session logs captured at `generator.py:82-95`, autopsy CLI
working, telemetry pipeline verified. See LEDGER entry 46 for full audit trail.

Feature home for the engine observability epic. Capture every LLM call,
reconstruct failures with one command, and make the event bus queryable.

## Document Map

| File | Purpose |
|------|---------|
| `EPIC.md` | Feature epic: motivation from run-1 x-ray, 10 hard requirements (R1-R10), module map, deliverables, metrics, non-goals |
| `PRD-v1.md` | Product requirements: problem/solution, 12 user stories, 8 implementation decisions, full schema, testing decisions, out of scope |
| `TICKETS.md` | Ticket slice map: 8 OBS tickets (#26-#33), dependency chain, implementation order |

## Gitea

- PRD issue: [#25](http://<LAN_IP>:3000/<user>/ace-engine/issues/25)
- Tickets: [#26](http://<LAN_IP>:3000/<user>/ace-engine/issues/26) – [#33](http://<LAN_IP>:3000/<user>/ace-engine/issues/33)
- Label: `ready-for-agent`

## Quick Start (post-implementation)

```bash
# After a run, reconstruct what happened:
ace autopsy run-1788948210           # full run summary
ace autopsy run-1788948210 T02       # deep-dive on T02
ace autopsy run-1788948210 T02 --full # include full prompt/response payloads

# Tail a running run's events:
ace tail run-1788948210

# Query session logs directly:
sqlite3 engine.db "SELECT * FROM session_logs WHERE run_id='run-1788948210';"
```

## Architecture

```
engine/generator.py ──→ SessionLogRecorder ──→ session_logs (engine.db)
                              │                    + sidecar JSONL
                              └──→ generation_telemetry (engine.db)

engine/events.py ──→ engine_events (engine.db) [parallel write]

engine/autopsy.py ←── reads session_logs + engine_events + existing tables
engine/tail.py    ←── polls engine_events
```
