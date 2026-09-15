# Observability Tickets — Slice Map

PRD: Gitea issue [#25](http://<LAN_IP>:3000/<user>/ace-engine/issues/25)
Epic: `docs/features/observability/EPIC.md`

## Issue Numbers

| Ticket | Title | Gitea # |
|--------|-------|---------|
| OBS-01 | session_logs table + SessionLogRecorder (the capture chokepoint) | #26 |
| OBS-02 | ace autopsy CLI — read-only failure reconstruction | #27 |
| OBS-03 | generation_telemetry table — per-attempt token/latency profiles | #28 |
| OBS-04 | engine_events table — queryable event bus | #29 |
| OBS-05 | TGD G4 upgrade — real prompt_tokens instead of code-blob proxy | #30 |
| OBS-06 | privacy + performance guardrails for session logs | #31 |
| OBS-07 | cost/quality analytics queries — per-model aggregation views | #32 |
| OBS-08 | live-tail event stream — tail a running run's events | #33 |

## Dependency Chain

```
OBS-01 (session_logs + SessionLogRecorder)  ← foundation, no deps
  ├── OBS-02 (ace autopsy CLI)              ← needs session_logs table
  ├── OBS-03 (generation_telemetry)         ← shares capture chokepoint
  ├── OBS-05 (TGD G4 upgrade)               ← needs session_logs.prompt_tokens
  └── OBS-06 (privacy + performance)        ← needs SessionLogRecorder

OBS-04 (engine_events)                      ← independent, no deps
  └── OBS-08 (live-tail)                    ← needs engine_events table

OBS-07 (cost/quality analytics)             ← needs OBS-01 + OBS-03
```

Visual:

```
Wave 1 (parallel):
  OBS-01  OBS-04

Wave 2 (after OBS-01):
  OBS-02  OBS-03  OBS-05  OBS-06

Wave 3 (after OBS-01 + OBS-03):
  OBS-07

Wave 3 (after OBS-04):
  OBS-08
```

## Implementation Order

1. **OBS-01** — Must be first. All other tickets depend on session_logs.
2. **OBS-04** — Can be done in parallel with OBS-01. Independent.
3. **OBS-02, OBS-03, OBS-05, OBS-06** — After OBS-01 lands.
4. **OBS-07** — After OBS-01 + OBS-03.
5. **OBS-08** — After OBS-04.
