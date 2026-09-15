# PRD: Live Streaming MVP

## Problem Statement
We need a live streaming server that shows real-time activity from the coder-harness engine. Currently there is zero visibility into what happens during execution — the engine outputs to stdout via print() and operators cannot monitor progress.

## Solution
Build a Python FastAPI SSE streaming server on port 3081 that accepts events from the engine and streams them to browser clients via Server-Sent Events.

## User Stories

1. As a developer, I want to POST events to a streaming server so that my engine activity is captured
2. As a developer, I want to view a live dashboard in my browser showing engine task progress, inference metrics, and GPU status
3. As a developer, I want the dashboard to auto-reconnect when the connection drops without losing events

## Implementation Decisions

### Module Organization

Create these files in /home/<user>/projects/coder-harness/:

- streaming_server.py — FastAPI app with SSE streaming, POST /events ingest, ring buffer, heartbeat. ~200 lines.
- streaming_client.py — Python client class to POST events from the engine pipeline. ~80 lines.
- dashboard/index.html — Self-contained HTML dashboard with dark theme, inline CSS and JS, EventSource for SSE. ~300 lines.

### Key Design

- EventEnvelope: JSON with id (ULID), seq (int), time (epoch ms), source ("engine"), type, data fields
- POST /events: accepts JSON event, returns 201 with assigned event ID
- GET /stream: SSE text/event-stream with auto-reconnect via Last-Event-ID
- Ring buffer: 500 events in memory for reconnection replay
- SQLite: live_events table for persistence (WAL mode)
- Port: 3081 (next to DSH web at 3080)

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS live_events (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    time INTEGER NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
```

## Out of Scope
- WebSocket (SSE is sufficient)
- Authentication (localhost-only)
- DSH integration (future)
- Multi-session support

## Testing
- Start server: python3 streaming_server.py
- POST event: curl -X POST localhost:3081/events -H "Content-Type: application/json" -d '{"source":"engine","type":"test","data":{"msg":"hello"}}'
- View stream: open http://localhost:3081/ in browser
