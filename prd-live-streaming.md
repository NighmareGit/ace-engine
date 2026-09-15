# PRD: Live Streaming for Autonomous Coding Engine

## Problem Statement

The autonomous coding engine (coder-harness) drives DSH (DeepSeek Harness) agents through a multi-step pipeline: PRD parsing → task decomposition → code generation via BeeLlama on Triton → quality gates → testing → git commit. Currently, there is **zero visibility** into what's happening during execution. The engine outputs to stdout via `print()` statements, and the DSH agent produces session logs that nobody reads. When the engine runs a PRD (as it did with the telemetry PRD — 15 tasks over 23 minutes), operators have no way to:

1. See which task is currently executing and what step it's on
2. Monitor inference progress (tok/s, cache hits, draft acceptance) in real-time
3. Watch GPU temperature and VRAM during sustained inference
4. Detect stalls, errors, or quality gate failures as they happen
5. Review past executions for debugging and optimization
6. View both DSH agent activity and engine orchestration in a unified dashboard

The existing `telemetry_dashboard.py` generates a static HTML file with 60-second meta-refresh — far too slow for monitoring a system that produces 1-4 events per second and where a stall can cost minutes of GPU time.

## Solution

Build a **live streaming system** with three components:

1. **Event Streaming Server** — A standalone Python FastAPI server on port 3081 that:
   - Accepts events from two producers (DSH agent + coder-harness engine) via `POST /events`
   - Streams events to browser clients via SSE (`GET /stream`)
   - Stores events in SQLite WAL for persistence and replay
   - Handles reconnection via ring buffer + `Last-Event-ID`

2. **Event Producers** — Lightweight adapters that emit structured events:
   - **DSH Adapter**: Tails DSH session JSONL logs and/or subscribes to DSH's built-in SSE output
   - **Engine Adapter**: Hooks into the coder-harness pipeline to emit events at each stage

3. **Live Dashboard** — A self-contained HTML+JS+CSS dashboard served by the streaming server:
   - Real-time event timeline with auto-scroll
   - GPU status panels (temperature, VRAM, utilization)
   - Inference progress (tok/s sparkline, cache efficiency)
   - Quality gate results with score bars
   - Git commit history
   - Alert panel for errors, thermal warnings, stalls

## User Stories

1. As a **developer**, I want to see a live timeline of engine tasks (T01→T15) with status indicators (running/complete/failed), so that I know exactly where the pipeline is
2. As a **developer**, I want to see real-time inference metrics (tok/s, cache hit rate, draft acceptance) for each BeeLlama call, so that I can spot performance anomalies instantly
3. As a **system administrator**, I want GPU temperature and VRAM gauges updating every second, so that I can catch thermal throttling before it impacts inference
4. As a **developer**, I want to see DSH agent activity (tool calls, LLM responses, subagent spawns) in the same timeline as engine events, so that I understand the full execution flow
5. As a **developer**, I want the dashboard to auto-reconnect when the connection drops, without losing any events, so that I can leave it running all day
6. As a **developer**, I want to pause the live stream and scroll back through history, so that I can investigate what happened without stopping the engine
7. As a **system administrator**, I want alerts for thermal warnings, OOM risk, and inference failures, so that I can react to critical issues immediately
8. As a **developer**, I want to replay a past engine execution from the event log, so that I can debug issues after the fact
9. As a **developer**, I want to filter the stream by source (DSH only, engine only, or both), so that I can focus on what matters
10. As a **developer**, I want to see git commits appear in real-time as the engine pushes code to Gitea, so that I know when new code is available
11. As a **developer**, I want to see quality gate scores with dimension breakdown (completeness, correctness, quality) as each task completes, so that I can assess code quality in real-time
12. As a **system administrator**, I want to see how many viewers are connected to the stream, so that I can monitor usage
13. As a **developer**, I want the dashboard to work on any modern browser without plugins or build steps, so that I can access it from any machine on the network
14. As a **developer**, I want events to be stored in SQLite so I can run custom SQL queries against historical data, so that I can answer ad-hoc questions about past executions
15. As a **developer**, I want the streaming server to handle 100+ events per second without dropping events or lagging, so that it works even during high-throughput inference

## Implementation Decisions

### Architecture: Unified Event Stream with Source-Tagged Adapters

One streaming server merges events from both DSH and the engine into a single monotonic stream. Server-side merging is chosen over client-side merging because:
- DSH uses epoch-milliseconds + sequence numbers; the engine uses SQLite auto-increment + `datetime('now')`. Server-side reconciliation resolves clock skew.
- Single SSE connection for the client (simpler than managing two connections).
- Single sequence counter enables gap detection and replay with one logic path.

### Event Envelope (v1)

```jsonc
{
  "id": "evt_01HX7K2M...",           // ULID — globally unique, sortable
  "seq": 42,                          // Monotonic within the stream
  "time": 1735689600000,              // Server wall-clock (Unix epoch ms)
  "source_time": 1735689599500,       // Producer's local timestamp
  "source": "dsh",                    // "dsh" | "engine" | "system"
  "source_id": "session-abc-123",     // Session identifier
  "type": "tool/call",               // Namespaced event type
  "correlation_id": "corr_xyz",       // Links cross-source events
  "data": { ... },                    // Payload
  "schema_version": 1
}
```

### Transport: SSE via FastAPI (port 3081)

- **Server**: FastAPI + Uvicorn, async, `StreamingResponse` for SSE
- **Client**: Browser `EventSource` API — zero dependencies, auto-reconnect
- **Ingest**: `POST /events` (single) and `POST /events/batch` (batch)
- **Stream**: `GET /stream?channels=dsh,engine&since=<event-id>`
- **Buffer**: Ring buffer (500 events/channel) for reconnection replay
- **Heartbeat**: Every 30 seconds to keep connections alive

### Event Types

**DSH-sourced** (mapped from DSH `SessionEvent`):

| DSH Type | Stream Type | Description |
|----------|------------|-------------|
| `turn/start` | `dsh.turn.start` | Agent turn begins |
| `turn/end` | `dsh.turn.end` | Agent turn ends |
| `step/start` | `dsh.step.start` | Model call begins |
| `step/end` | `dsh.step.end` | Model call ends |
| `tool/call` | `dsh.tool.call` | Tool invocation |
| `tool/result` | `dsh.tool.result` | Tool result |
| `assistant/chunk` | `dsh.assistant.chunk` | Token streaming |
| `assistant/message` | `dsh.assistant.message` | Assembled response |
| `todo/write` | `dsh.todo.write` | Task list update |

**Engine-sourced** (mapped from coder-harness events):

| Engine Event | Stream Type | Description |
|-------------|------------|-------------|
| `pipeline.start` | `engine.pipeline.start` | Pipeline begins |
| `pipeline.complete` | `engine.pipeline.complete` | Pipeline finishes |
| `task.start` | `engine.task.start` | Task begins |
| `task.complete` | `engine.task.complete` | Task finishes |
| `task.fail` | `engine.task.fail` | Task failed |
| `inference.start` | `engine.inference.start` | BeeLlama API call begins |
| `inference.complete` | `engine.inference.complete` | BeeLlama API call done |
| `quality.result` | `engine.quality.result` | Quality gate result |
| `test.result` | `engine.test.result` | Test result |
| `git.commit` | `engine.git.commit` | Code committed |
| `gpu.snapshot` | `engine.gpu.snapshot` | GPU state update |

**System events** (from streaming server):

| Type | Description |
|------|-------------|
| `system.heartbeat` | Periodic liveness |
| `system.source.connected` | Source adapter connected |
| `system.source.disconnected` | Source adapter lost |

### Backpressure & Priority

Bounded ring buffer (10K events) with priority-based dropping:
- **P0 (never drop)**: errors, thermal warnings, quality failures, pipeline completion
- **P1 (never drop)**: turn boundaries, model swaps, sandbox lifecycle
- **P2 (never drop)**: tool calls, inference start/complete
- **P3 (sample at 10%)**: draft events, cache hits, assistant chunks
- **P4 (drop first)**: heartbeats, todo updates, request headers

### Storage: SQLite WAL + Lamport Sequences

- **`live_events`** table: append-only event stream with monotonic sequences
- **`source_sequences`** table: per-source sequence tracking for gap-free replay
- **Batch writes**: 100 events per transaction, 1-second flush interval
- **Retention**: 24 hours configurable, ~500 MB/day budget
- **Lamport clock**: ordering immune to clock skew between DSH and engine

### Security

- Bind to `127.0.0.1` by default (localhost only)
- Optional `--bind 0.0.0.0` with bearer token auth
- Event redaction: strip API keys, tokens, passwords from tool call arguments
- Truncate large code content (>4KB) in events

### Dashboard Layout

```
┌──────────────────────────────────────────────────────────────┐
│ ⚡ DSH Live Dashboard          ● Connected    🕐 14:32:05   │
├──────────────────────────────────────────────────────────────┤
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│ │ Agents: 2│ │ Tasks: 3 │ │ GPU 3090 │ │ GPU 3070 │        │
│ │ ██████░░ │ │ ████░░░░ │ │ 72°C 85% │ │ 58°C 42% │        │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘        │
├──────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────┐ ┌─────────────────────────────┐ │
│ │ 📊 Live Event Timeline  │ │ ⚡ Inference Status          │ │
│ │ 14:32:03 inference_done │ │ Config: 3090-qwen36-35b     │ │
│ │   tok/s: 197.3          │ │ Speed: 197.3 tok/s          │ │
│ │   cache: 89% hit rate   │ │ Cache: 89% hit              │ │
│ │ 14:31:58 inference_start│ │ Draft: 73% acceptance        │ │
│ │ 14:31:45 sandbox_create │ │ ┌─ tok/s History ──────┐   │ │
│ │                         │ │ │ ▁▂▃▄▅▆▇█▇▆▅▄▃▂▁     │   │ │
│ │ [Auto-scroll] [Pause]   │ │ └──────────────────────┘   │ │
│ └─────────────────────────┘ └─────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────┐ ┌─────────────────────────────┐ │
│ │ 🎯 Quality Gates        │ │ 📁 Git Activity             │ │
│ │ T15: ████████░░ 8.2/10  │ │ 14:32:01 abc1234 feat: fix │ │
│ │   completeness: 9       │ │   SSH connection timeout    │ │
│ │   correctness: 8        │ │   +12 -3 ssh_utils.py      │ │
│ └─────────────────────────┘ └─────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│ 🚨 Alerts                                                    │
│ ⚠️ 14:30:22 GPU 3090 temperature 82°C (threshold: 83°C)     │
│ ❌ 14:25:10 inference_fail: OOM on context 131072            │
└──────────────────────────────────────────────────────────────┘
```

### Files to Create/Modify

**New files:**
- `streaming_server.py` — FastAPI SSE server (~200 lines)
- `streaming_config.py` — Configuration (ports, buffer sizes, channels)
- `streaming_client.py` — Python client for DSH/engine to POST events
- `event_store.py` — Batched SQLite writer with monotonic sequences
- `event_schema.py` — DDL for live_events, source_sequences, replay_cursors
- `event_query.py` — Query API functions
- `event_replay.py` — Replay logic for reconnected clients
- `event_retention.py` — Cleanup and VACUUM
- `dashboard_server.py` — Serves the dashboard HTML
- `dashboard/index.html` — Self-contained dashboard
- `dashboard/dashboard.js` — Vanilla JS EventSource client
- `dashboard/dashboard.css` — Dark theme styles

**Modified files:**
- `task_queue.py` — Add event emission hooks at each pipeline step
- `code_generator.py` — Emit inference.start/complete events
- `quality_gates.py` — Emit quality.result events
- `harness.py` — Add `--dashboard` flag to start streaming server

### Testing Decisions

- Unit tests for event envelope validation, ring buffer, SQLite writes
- Integration test: POST event → SSE receive → verify in stream
- E2E test: Run engine with `--dashboard`, verify events appear in browser
- Load test: Simulate 100 events/sec, verify no drops or lag
- Reconnection test: Connect, receive events, disconnect, reconnect with Last-Event-ID, verify gap-free replay

## Out of Scope

1. **WebSocket transport** — SSE is sufficient for unidirectional streaming; WebSocket adds complexity without benefit
2. **Authentication/authorization** — localhost-only for v1; add later if network exposure needed
3. **Multi-session support** — Single session streaming for v1; multi-session is a future enhancement
4. **DSH core modifications** — Use DSH's existing SSE output and JSONL logs; don't modify DSH source
5. **Grafana/Prometheus integration** — SQLite-based dashboard is self-contained
6. **Mobile-optimized layout** — Desktop-first (1920×1080); mobile is a future enhancement

## Further Notes

### DSH Already Has SSE Support

DSH in headless mode already outputs events as `text/event-stream`. The DSH adapter can subscribe to this directly rather than tailing JSONL files. Check `deepseek-harness/packages/core/session/` for the SSE output implementation.

### Existing Telemetry Infrastructure

The coder-harness already has:
- `schema_unified.py` — SQLite schema with `work_events` table
- `telemetry_schema.py` — Extended schema with `pipeline_events`, `inference_traces`
- `telemetry_collector.py` — GPU snapshot collection via SSH
- `telemetry_dashboard.py` — Static HTML dashboard (kept for export)

The streaming system extends (does not replace) this infrastructure.

### Correlation ID Strategy

When a DSH tool call triggers an engine operation (e.g., DSH calls `harness.py ace run`), the streaming server can inject a `correlation_id` that links the DSH `tool/call` event with the engine `pipeline.start` event. This enables unified timeline views that show the cause-effect chain across both layers.
