# Skills & Personas Tickets - Slice Map

PRD: Gitea issue [#34](http://<LAN_IP>:3000/<user>/ace-engine/issues/34)
Epic: `docs/features/skills-personas/EPIC.md`

## Issue Numbers

| Ticket | Title | Gitea # |
|--------|-------|---------|
| SP-01 | Skill validation gate - load-time rejection of malformed/malicious skills | [#35](http://<LAN_IP>:3000/<user>/ace-engine/issues/35) |
| SP-02 | Skill effectiveness telemetry - injection->outcome recording + kill-switch | [#36](http://<LAN_IP>:3000/<user>/ace-engine/issues/36) |
| SP-03 | Metadata-driven skill matching - applies_to / failure_class front-matter | [#37](http://<LAN_IP>:3000/<user>/ace-engine/issues/37) |
| SP-04 | Persona composition layer - stacked system prompt in prompt_compiler | [#38](http://<LAN_IP>:3000/<user>/ace-engine/issues/38) |
| SP-05 | Ralph->skill ingestion pipeline - pattern_to_skill() with validation + dedup | [#39](http://<LAN_IP>:3000/<user>/ace-engine/issues/39) |
| SP-06 | Persona fragment system - PersonaStore + seed failure-class fragments | [#40](http://<LAN_IP>:3000/<user>/ace-engine/issues/40) |
| SP-07 | Seed skill registry expansion - persona-system + matching discipline skills | [#41](http://<LAN_IP>:3000/<user>/ace-engine/issues/41) |
| SP-08 | Effectiveness kill-switch query - skill_flags view + minimum-sample guard | [#42](http://<LAN_IP>:3000/<user>/ace-engine/issues/42) |

## Dependency Chain

```
SP-01 (#35, validation gate)           <- foundation, no deps
  +-- SP-03 (#37, metadata matching)
  +-- SP-07 (#41, seed expansion)
  +-- SP-05 (#39, ralph->skill ingest)

SP-06 (#40, PersonaStore + fragments)  <- independent, no deps
  +-- SP-04 (#38, persona composition)

SP-02 (#36, effectiveness telemetry)   <- depends on OBS-01 (#26) + OBS-03 (#28)
  +-- SP-08 (#42, kill-switch query)
```

## Implementation Order

1. **SP-01 (#35)** - First. Validation gate foundation. No external deps.
2. **SP-06 (#40)** - Independent. PersonaStore. No external deps.
3. **SP-03 (#37)** - After SP-01. Metadata matching. No external deps.
4. **SP-07 (#41)** - After SP-01. Seed expansion. No external deps.
5. **SP-04 (#38)** - After SP-06. Composition layer. No external deps.
6. **SP-05 (#39)** - After SP-01. Spec-only until ralph stage (e).
7. **SP-02 (#36)** - After OBS-01 + OBS-03. Effectiveness telemetry.
8. **SP-08 (#42)** - After SP-02. Kill-switch query.

## External Dependencies

| Ticket | Depends on | Why |
|--------|-----------|-----|
| SP-02 (#36) | OBS-01 (#26) session_logs | Outcome linking |
| SP-02 (#36) | OBS-03 (#28) generation_telemetry | Normalization |
| SP-05 (#39) | R6 ralph ralph_patterns | Ingestion source |
| SP-03 (#37) | Wave-0 D5 | Coordinate merge |

## Tracer Bullet

Highest-leverage first slice: **SP-01 (#35) + SP-06 (#40)** in parallel.
Both independent, low-cost, unblock the rest. Effectiveness telemetry
(SP-02 -> SP-08) is highest-value but gated on OBS-01/03.
