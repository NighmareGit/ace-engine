# ACE Watch — Implementation State Machine

## Overview

The implementation is organized into 3 parallel tracks that converge at integration:

```
Track A: enginectl CLI          Track B: Dashboard API          Track C: ace-watch Skill
(independent)                   (depends on A.1)                (depends on A + B)
```

## State Machine

### Track A: enginectl CLI

```
A.0 [INIT]
 │
 ▼
A.1 [SCAFFOLD] ──→ Create enginectl.py with CLI framework + JSON output
 │
 ▼
A.2 [STATUS] ──→ Implement `status` command (engine + stream + GPU + model)
 │
 ▼
A.3 [RUN/PARSE] ──→ Implement `run` + `parse` commands (delegate to harness.py)
 │
 ▼
A.4 [MODEL/GPU] ──→ Implement `model` + `gpu` commands
 │
 ▼
A.5 [TELEMETRY] ──→ Implement `telemetry query/export` commands
 │
 ▼
A.6 [TESTS] ──→ Unit tests for all commands
 │
 ▼
A.7 [VERIFY] ──→ Verifier gate: all tests pass, JSON output correct
 │
 ▼
A.DONE ──────────────────────────────────────┐
                                             │
                                             ▼
                                       I.1 [INTEGRATE]
```

### Track B: Dashboard API + JS Enhancement

```
B.0 [INIT]
 │
 ▼
B.1 [API ENDPOINTS] ──→ Add /api/inject, /api/config, /api/panel to streaming_server.py
 │
 ▼
B.2 [CONFIG PERSISTENCE] ──→ JSON file storage for dashboard config + panel CRUD
 │
 ▼
B.3 [JS: CONFIG LOADING] ──→ Dashboard fetches config on connect, applies state
 │
 ▼
B.4 [JS: SYSTEM EVENTS] ──→ Dashboard renders annotations, panels, highlights, focus
 │
 ▼
B.5 [JS: CUSTOM PANELS] ──→ Dashboard renders agent-created panels with markdown
 │
 ▼
B.6 [TESTS] ──→ Unit tests for API endpoints + JS event handling
 │
 ▼
B.7 [VERIFY] ──→ Verifier gate: inject event → appears in SSE stream, config persists
 │
 ▼
B.DONE ──────────────────────────────────────┐
                                             │
                                             ▼
                                       I.1 [INTEGRATE]
```

### Track C: ace-watch Skill + Docs

```
C.0 [INIT]
 │
 ▼
C.1 [SKILL SCAFFOLD] ──→ Create SKILL.md with phases + agent instructions
 │
 ▼
C.2 [TOOL REFERENCE] ──→ Write enginectl command reference doc
 │
 ▼
C.3 [DASHBOARD API REF] ──→ Write dashboard API reference doc
 │
 ▼
C.4 [AGENT GUIDE] ──→ Write agent usage guide (common workflows, error handling)
 │
 ▼
C.5 [USER INSTRUCTION MAP] ──→ Document natural language → dashboard action mapping
 │
 ▼
C.DONE ──────────────────────────────────────┐
                                             │
                                             ▼
                                       I.1 [INTEGRATE]
```

### Integration Track

```
I.1 [INTEGRATE] ──→ enginectl + dashboard API + skill all wired together
 │
 ▼
I.2 [E2E TEST] ──→ Full flow: skill pre-flight → enginectl run → dashboard shows events
 │
 ▼
I.3 [DEPLOY] ──→ Sync to Triton, start services, verify live
 │
 ▼
I.4 [CODE REVIEW] ──→ Mandatory /code-review
 │
 ▼
I.5 [COMMIT] ──→ Git commit + push
 │
 ▼
DONE ✓
```

## Dependency Graph

```
A.1 ──→ A.2 ──→ A.3 ──→ A.4 ──→ A.5 ──→ A.6 ──→ A.7 ──→ I.1
B.1 ──→ B.2 ──→ B.3 ──→ B.4 ──→ B.5 ──→ B.6 ──→ B.7 ──→ I.1
C.1 ──→ C.2 ──→ C.3 ──→ C.4 ──→ C.5 ───────────────────→ I.1
                                                    │
                                                    ▼
                                              I.2 → I.3 → I.4 → I.5
```

**Parallelism Opportunities:**
- Tracks A, B, C run in parallel (no cross-track dependencies until I.1)
- Within each track, steps are sequential (each builds on the previous)
- A.1 and B.1 can start simultaneously (different files)
- C.1 can start immediately (documentation, no code dependency)

## Wave Plan for Sub-Agent Dispatch

### Wave 1 (Parallel — No Dependencies)
- **Agent A.1**: Create `enginectl.py` scaffold with CLI framework + JSON output + `status` command
- **Agent B.1**: Add `/api/inject`, `/api/config`, `/api/panel` endpoints to `streaming_server.py`
- **Agent C.1**: Create `ace-watch` SKILL.md + skill directory structure

### Wave 2 (After Wave 1)
- **Agent A.2**: Implement `run` + `parse` commands in `enginectl.py`
- **Agent B.2**: Add config persistence (JSON file) + panel CRUD logic
- **Agent C.2**: Write enginectl tool reference documentation

### Wave 3 (After Wave 2)
- **Agent A.3**: Implement `model` + `gpu` + `telemetry` commands
- **Agent B.3**: Dashboard JS — config loading on connect + system event rendering
- **Agent C.3**: Write dashboard API reference + agent guide

### Wave 4 (After Wave 3)
- **Agent A.4**: Unit tests for all enginectl commands
- **Agent B.4**: Dashboard JS — custom panels + highlights + focus mode
- **Agent C.4**: Write user instruction mapping + troubleshooting guide

### Wave 5 (After Wave 4 — Convergence)
- **Agent I.1**: Integration — wire enginectl → dashboard API → skill
- **Verifier**: E2E test + deploy to Triton + live verification

### Wave 6 (After Wave 5)
- **Code Review**: Mandatory /code-review of all changes
- **Fix**: Address review findings
- **Commit**: Git commit + push
