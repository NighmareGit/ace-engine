# PRD: ACE Watch — Agent-Controllable Engine with Live Dashboard

## Problem Statement

Running a PRD through the coder-harness engine requires manual orchestration: start the streaming server, start the GPU poller, set environment variables, launch the engine, and open the dashboard in a browser. The operator has no way to control what the dashboard shows from within a session. The engine's full feature set (model swap, telemetry query, task management) is only accessible via scattered CLI commands that agents don't know about.

**We need:**
1. A unified toolchain (`enginectl`) that gives session agents full control over every engine feature
2. A dashboard API that lets agents inject events, create custom panels, and configure the display based on user instructions
3. A skill (`ace-watch`) that auto-orchestrates the full stack — pre-flight, run, observe, adapt — so the operator just says "run this PRD" and watches it happen
4. Reference documentation so any session agent can use the engine tools and dashboard API without prior knowledge

## Solution

### Component 1: `enginectl` — Unified Engine Control CLI

A single Python entry point (`enginectl.py`) that wraps all engine operations into structured JSON-output commands. Every command returns machine-parseable JSON so agents can inspect results and make decisions.

**Command Groups:**

| Group | Commands | Description |
|-------|----------|-------------|
| `run` | `run <prd> --project <p> --config <c>` | Execute a PRD end-to-end with streaming |
| `parse` | `parse <prd>` | Parse PRD into task list (JSON) |
| `status` | `status` | Engine + streaming + GPU + model status |
| `tasks` | `tasks list`, `tasks cancel <id>` | Task execution management |
| `model` | `model list`, `model swap <c>`, `model status` | Model lifecycle |
| `gpu` | `gpu` | Real-time GPU status |
| `telemetry` | `telemetry query`, `telemetry export` | Query streaming events |
| `stream` | `stream start`, `stream stop`, `stream status` | Streaming server lifecycle |
| `dashboard` | `dashboard` | Print dashboard URL + config hints |

**Key Design Decisions:**
- All output is JSON (agents parse it, humans don't read raw CLI)
- `run` command sets `STREAMING_SERVER_URL` and `STREAMING_TOKEN` automatically
- `run` command returns immediately with a `run_id`; progress is via `tasks list --run <id>`
- Every command has a `--json` flag (default true) and optional `--pretty` for human debugging
- Commands that talk to Triton use the existing transport layer (HTTP → engine_service.py)

### Component 2: Dashboard API — Agent-Controllable Display

Add REST endpoints to `streaming_server.py` that let agents modify what the dashboard shows:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /api/inject` | POST | Inject custom events into the stream (annotations, panels, commands) |
| `POST /api/config` | POST | Configure dashboard display (highlight tasks, focus source, custom panels) |
| `GET /api/config` | GET | Get current dashboard configuration |
| `POST /api/panel` | POST | Add/update a custom panel on the dashboard |
| `DELETE /api/panel/<id>` | DELETE | Remove a custom panel |

**Inject Event Types:**

| Type | Data Fields | Dashboard Effect |
|------|-------------|-----------------|
| `system.annotation` | `text`, `style` (info/warning/error/success) | Colored banner above the event timeline |
| `system.panel` | `title`, `content`, `position` (top/bottom/left/right) | Custom panel with markdown content |
| `system.highlight` | `task_ids[]`, `color` | Highlight specific task rows in the engine stream |
| `system.focus` | `source` (dsh/engine/both) | Dim one pane, emphasize the other |
| `system.command` | `command`, `status` (pending/running/done/failed), `result` | Command execution status in the header |

**Dashboard Config Schema:**

```json
{
  "highlight_tasks": ["T03", "T07"],
  "focus_source": "engine",
  "auto_scroll": true,
  "custom_panels": [
    {
      "id": "panel-1",
      "title": "User Notes",
      "content": "Comparing config-i vs config-h...",
      "position": "bottom",
      "created_by": "agent"
    }
  ],
  "annotations": [
    {
      "text": "T03 extraction failed — needs HTML template fix",
      "style": "warning",
      "time": 1788491028421
    }
  ]
}
```

**Dashboard JS Changes:**
- On SSE connect, fetch `GET /api/config` and apply initial state
- Listen for `system.annotation`, `system.panel`, `system.highlight`, `system.focus`, `system.command` events
- Render annotations as colored banners above the timeline
- Render custom panels in the specified position
- Highlight task rows with the specified color
- Show command status in the header bar

### Component 3: `ace-watch` Skill

A DSH skill that orchestrates the full stack. Invocable via `/ace-watch <prd> --project <path> --config <id>`.

**Skill Phases:**

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ PRE-FLIGHT│────▶│  DEPLOY  │────▶│   RUN    │────▶│ OBSERVE  │────▶│ COMPLETE │
│          │     │          │     │          │     │          │     │          │
│ Check    │     │ Start    │     │ Execute  │     │ Poll     │     │ Report   │
│ services │     │ services │     │ PRD via  │     │ progress │     │ results  │
│ + GPU    │     │ + inject │     │ enginectl│     │ + adapt  │     │ + clean  │
│ health   │     │ welcome  │     │          │     │ dashboard│     │ up       │
└──────────┘     └──────────┘     └──────────┘     └──────────┘     └──────────┘
     │                │                │                │                │
     ▼                ▼                ▼                ▼                ▼
  JSON status     Dashboard URL    run_id +         Live updates    Final report
  to session      to operator      streaming ON     to session      + dashboard
                                                                     keeps running
```

**Phase Details:**

1. **PRE-FLIGHT** — Check streaming server, GPU poller, engine service, BeeLlama health. Auto-start anything not running. Report status to session.

2. **DEPLOY** — Inject `system.annotation` ("Session started: running {prd_title}") and `system.panel` ("Engine Info: config={config}, project={project}") into dashboard. Print dashboard URL to operator.

3. **RUN** — Call `enginectl run <prd> --project <p> --config <c>`. Receive `run_id`. Begin polling.

4. **OBSERVE** — Poll `enginectl tasks list --run <id>` every 10s. On task completion, inject `system.annotation` ("T03 complete: 197 tok/s, 8.2/10 quality"). If user asks to highlight something, inject `system.highlight`. If user asks for a notes panel, inject `system.panel`.

5. **COMPLETE** — Report final results (tasks completed, total time, avg tok/s). Ask operator if dashboard should stay running or shut down.

**User Instruction Translation:**

The skill teaches the agent to map natural language to dashboard actions:

| User Says | Agent Does |
|-----------|-----------|
| "Run this PRD" | Full ace-watch flow |
| "Show me GPU temps" | Inject panel with GPU telemetry query results |
| "Highlight T03" | Inject `system.highlight` with task_ids=["T03"] |
| "Add a notes panel" | Inject `system.panel` with user's content |
| "Switch to config-h" | `enginectl model swap config-h`, inject annotation |
| "What's the cache hit rate?" | Query telemetry, respond in chat + optional panel |
| "Focus on the DSH stream" | Inject `system.focus` with source="dsh" |
| "Stop the dashboard" | `enginectl stream stop` |

### Component 4: Reference Documentation

A new file `docs/ace-watch-reference.md` covering:

1. **Architecture Overview** — How streaming server, engine, dashboard, and enginectl relate
2. **Tool Reference** — Every `enginectl` command with examples and JSON output schemas
3. **Dashboard API Reference** — Every endpoint with request/response examples
4. **Event Schema** — All event types the dashboard understands (engine, DSH, system injection)
5. **Agent Guide** — How to use the tools from a session, common workflows, error handling
6. **Operator Guide** — How to start the stack manually, dashboard URL, browser tips
7. **Troubleshooting** — Common issues and fixes (SSE disconnect, no data, auth errors)

## User Stories

1. As an **operator**, I want to say `/ace-watch my-prd.md --project my-project` and see the dashboard light up with live task progress, so I can watch the engine work without manual setup
2. As an **operator**, I want to tell the agent "highlight T03" and see it highlighted on the dashboard, so I can focus on what matters
3. As an **operator**, I want to tell the agent "add a notes panel with my observations" and see it appear on the dashboard, so I can annotate the run in real-time
4. As an **agent**, I want a single `enginectl status` command that tells me everything about the engine, streaming, GPU, and model state, so I can make informed decisions
5. As an **agent**, I want to inject custom events into the dashboard via `POST /api/inject`, so I can translate user instructions into visual changes
6. As an **agent**, I want structured JSON output from every command, so I can parse results without regex
7. As an **operator**, I want the dashboard to show command execution status when I use the command bar, so I know what happened
8. As an **agent**, I want to query historical telemetry data via `enginectl telemetry query`, so I can answer questions like "what was the avg tok/s on the last run?"
9. As an **operator**, I want the engine to automatically stream events to the dashboard when I run a PRD, so I don't need to configure env vars
10. As an **agent**, I want the `ace-watch` skill to handle all pre-flight checks and service management, so I can focus on the user's actual request
11. As an **operator**, I want to see custom panels the agent creates based on my questions ("show me the GPU history"), so I get visual answers
12. As an **agent**, I want to create/update/remove dashboard panels via the API, so I can dynamically adjust what the operator sees

## Implementation Decisions

### enginectl as a Thin Wrapper

`enginectl.py` is a thin CLI that delegates to existing modules:
- `run` → `harness.py ace run` (with env vars set)
- `parse` → `harness.py ace parse`
- `status` → health check + `streaming_client.py` health + `gpu_telemetry.py` status
- `tasks` → `task_queue.py` task state query
- `model` → `remote_control.py` model commands
- `gpu` → `nvidia-smi` via transport
- `telemetry` → SQLite query on `streaming-events.db`
- `stream` → `harness.py stream` commands

### Dashboard API as Streaming Server Extension

The `/api/inject`, `/api/config`, `/api/panel` endpoints are added to `streaming_server.py` alongside existing endpoints. They:
- Store config in a JSON file (`dashboard-config.json`) that the dashboard fetches on connect
- Emit injected events through the existing ring buffer + SSE pipeline
- Are protected by the same auth token as POST /events

### Dashboard JS as Progressive Enhancement

The existing `dashboard/index.html` is enhanced (not replaced):
- On connect: fetch `GET /api/config` and apply state
- On `system.*` events: render annotations, panels, highlights
- Custom panels use `<div class="custom-panel">` with markdown rendering (simple: bold, italic, code, links)
- Highlighted tasks get a colored left border
- Focus mode dims one pane via CSS opacity

### Skill File Structure

```
~/.agents/skills/ace-watch/
├── SKILL.md              # Skill definition + instructions
├── references/
│   ├── tool-reference.md # enginectl command reference
│   ├── dashboard-api.md  # Dashboard API reference
│   └── event-schema.md   # All event types
```

## Out of Scope

1. **WebSocket transport** — SSE is sufficient for observation; control continues via REST
2. **Multi-session dashboard** — Single session per dashboard instance for v1
3. **Persistent custom panels** — Panels live for the session duration; not saved across restarts
4. **Agent-initiated model swaps from dashboard** — Model swap is via enginectl, not dashboard UI
5. **Real-time LLM output streaming** — The dashboard shows inference metrics, not raw token streams

## Testing

- Unit tests for `enginectl` command parsing and JSON output
- Unit tests for dashboard API endpoints (inject, config, panel CRUD)
- Integration test: inject event → verify in SSE stream
- Integration test: config change → verify dashboard applies it
- E2E test: full ace-watch flow with mock engine
