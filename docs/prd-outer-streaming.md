# Outer Streaming: Coder-Harness Engine

## Problem

The coder-harness engine (`harness.py ace run`) orchestrates an autonomous coding pipeline:

```
PRD parsing → task decomposition → context building → code generation (BeeLlama on Triton) → quality gates → testing → git commit
```

Currently, all progress information flows through two channels:

1. **`print()` statements** — Human-readable but not machine-parseable. ~50 print calls across `task_queue.py`, `code_generator.py`, and `harness.py` output progress to stdout. These are interleaved, unstructured, and lost if stdout is not captured.

2. **In-memory `execution_log`** — A `List[dict]` in `TaskQueue` that records 7 event types (`prd_load`, `tasks_loaded`, `task_start`, `task_complete`, `task_skipped`, `execute_all_complete`). This log is saved to `task_queue_state.json` on completion but provides no real-time visibility.

**The gap:** The DSH Web GUI at `http://127.0.0.1:3080` has no way to observe pipeline progress in real-time. An agent watching the engine must either parse stdout (fragile) or wait for the entire pipeline to complete (no incremental feedback). Post-hoc analysis requires manually loading JSON state files.

**What we need:** A streaming solution that emits structured events as the pipeline executes, supporting both real-time observation (WebSocket/SSE to the GUI) and post-hoc replay (persisted event log). Must work with the existing Python codebase without major rewrites, must not block the engine pipeline, and must capture all key events: task lifecycle, inference progress, quality results, and git operations.

---

## Solution

### Architecture: Three-Layer Event Streaming

```
┌─────────────────────────────────────────────────────────────┐
│                    Coder-Harness Engine                      │
│                                                              │
│  TaskQueue._emit("task.start", {task_id, title})            │
│       │                                                      │
│       ▼                                                      │
│  ┌──────────────────┐                                        │
│  │   EventEmitter    │  ← in-process, synchronous fan-out   │
│  │   (task_queue.py) │                                        │
│  └──────┬───────────┘                                        │
│         │                                                    │
│    ┌────┴────┬────────────┬──────────────┐                   │
│    ▼         ▼            ▼              ▼                   │
│  NDJSON   SQLite       Ring          SSE                    │
│  stdout   work_events   Buffer       Server                 │
│  pipe     table         (last 500)   (optional)             │
│    │         │            │              │                   │
│    ▼         ▼            ▼              ▼                   │
│  pipe to   crash-safe   /status       /events               │
│  agent     replay       endpoint      SSE endpoint          │
└─────────────────────────────────────────────────────────────┘
```

**Layer 1 — EventEmitter (in-process):** A lightweight Python class that accepts typed events with structured payloads. Replaces the existing `_log_event()` method. Fans out to multiple subscribers synchronously in the caller's thread. Zero external dependencies.

**Layer 2 — Output Subscribers:** Pluggable sinks that receive events from the emitter:
- `NDJSONSubscriber` — Writes newline-delimited JSON to stdout. Pipe-friendly, works immediately without a server.
- `SQLiteSubscriber` — Writes to the `work_events` table in `~/coder-harness-telemetry.db`. Crash-safe, queryable, supports post-hoc replay.
- `RingBufferSubscriber` — Keeps the last N events in memory for fast status queries (no DB hit needed).

**Layer 3 — SSE Endpoint (optional):** A lightweight aiohttp server that broadcasts events to connected WebSocket/SSE clients. Supports `?since=<id>` for replay from history. Only started when `--stream` flag is used.

### Why This Design

| Constraint | How We Address It |
|---|---|
| Must work with existing Python codebase | EventEmitter wraps `_log_event()` — minimal changes to calling code |
| Must not block the engine pipeline | Subscribers run synchronously but are non-blocking (SQLite writes are fast, NDJSON is just `print()`, ring buffer is in-memory) |
| Must support real-time streaming | SSE endpoint broadcasts to connected GUI clients |
| Must support post-hoc replay | SQLite `work_events` table stores all events with timestamps |
| Must capture task lifecycle, inference, quality, git | Comprehensive event type taxonomy (see below) |
| Must work with existing telemetry infrastructure | SQLite subscriber writes to the same DB as `TelemetryCollector` |

---

## Events

### Event Envelope

Every event follows a consistent envelope:

```json
{
  "id": "evt_20250115_103000_001",
  "type": "task.start",
  "timestamp": "2025-01-15T10:30:00.123Z",
  "pipeline_id": "ace_prd_abc123",
  "task_id": "T01",
  "severity": "info",
  "data": { }
}
```

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique event ID (monotonic, timestamp-based) |
| `type` | string | Event type (dot-namespaced) |
| `timestamp` | ISO 8601 | UTC timestamp with millisecond precision |
| `pipeline_id` | string | Pipeline run identifier (derived from PRD path + timestamp) |
| `task_id` | string? | Task identifier (null for pipeline-level events) |
| `severity` | enum | `info`, `warn`, `error` |
| `data` | object | Event-specific payload (see below) |

### Event Types

#### Pipeline Lifecycle

| Event | When | Payload |
|---|---|---|
| `pipeline.start` | Pipeline begins | `{prd_path, prd_title, task_count, project_path, config}` |
| `pipeline.complete` | Pipeline finishes | `{total, completed, failed, skipped, duration_s, summary}` |
| `pipeline.error` | Pipeline-level error | `{error, traceback}` |

#### Task Lifecycle

| Event | When | Payload |
|---|---|---|
| `task.queued` | Task added to queue | `{task_id, title, category, complexity, dependencies, files_to_create, files_to_modify}` |
| `task.start` | Task execution begins | `{task_id, title}` |
| `task.step.start` | Pipeline step begins | `{task_id, step: "context"|"generate"|"quality"|"test"|"commit", step_index: 1-5}` |
| `task.step.complete` | Pipeline step ends | `{task_id, step, step_index, status: "ok"|"error"|"skip", duration_s, result_summary}` |
| `task.complete` | Task finishes successfully | `{task_id, status: "complete", duration_s, steps_summary}` |
| `task.fail` | Task fails | `{task_id, status: "failed", error, duration_s, failed_step}` |
| `task.skip` | Task skipped | `{task_id, reason: "dependency_failed"}` |
| `task.retry` | Task retrying | `{task_id, attempt, max_retries, previous_error}` |

#### Inference (Code Generation)

| Event | When | Payload |
|---|---|---|
| `inference.start` | BeeLlama request sent | `{task_id, attempt, port, max_tokens, temperature, prompt_length}` |
| `inference.complete` | Response received | `{task_id, attempt, tokens_generated, thinking_tokens, predicted_per_second, predicted_ms, prompt_ms, files_extracted}` |
| `inference.error` | Request failed | `{task_id, attempt, error, ssh_exit_code}` |

#### Quality Gates

| Event | When | Payload |
|---|---|---|
| `quality.start` | Quality check begins | `{task_id, files_to_check}` |
| `quality.result` | Check result | `{task_id, overall: "pass"|"fail"|"skip", checks: [{name, status, detail}], reason?}` |

#### Testing

| Event | When | Payload |
|---|---|---|
| `test.start` | Test run begins | `{task_id, project_path, test_pattern}` |
| `test.result` | Test results | `{task_id, passed, failed, total, status: "pass"|"fail"}` |

#### Git Operations

| Event | When | Payload |
|---|---|---|
| `git.commit` | Commit created | `{task_id, commit_sha, message, files_changed}` |
| `git.push` | Changes pushed | `{task_id, remote, branch, commit_sha}` |

#### System

| Event | When | Payload |
|---|---|---|
| `system.health` | Health check | `{checks: [{name, status, detail}]}` |
| `system.gpu` | GPU stats snapshot | `{gpus: [{index, name, memory_used_mb, temperature_c, utilization_pct}]}` |
| `system.model_swap` | Model swap initiated | `{from_config, to_config, status}` |

---

## Interface

### API Contract: EventEmitter

```python
class EventEmitter:
    """In-process event emitter for pipeline events."""

    def __init__(self):
        self._subscribers: List[Callable[[dict], None]] = []
        self._event_count: int = 0
        self._pipeline_id: str = ""

    def set_pipeline(self, prd_path: str) -> None:
        """Set the pipeline ID based on PRD path + timestamp."""

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """Add a subscriber. Called synchronously for each event."""

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        """Remove a subscriber."""

    def emit(self, event_type: str, data: dict = None,
             task_id: str = None, severity: str = "info") -> dict:
        """Emit an event. Returns the event envelope."""

    def get_recent(self, n: int = 50) -> List[dict]:
        """Get the last N events from the ring buffer."""
```

### API Contract: Subscribers

```python
class NDJSONSubscriber:
    """Writes events as newline-delimited JSON to a file object."""
    def __init__(self, file=sys.stdout): ...

class SQLiteSubscriber:
    """Writes events to the work_events table."""
    def __init__(self, db_path: str = "~/coder-harness-telemetry.db"): ...

class RingBufferSubscriber:
    """Keeps last N events in memory for fast status queries."""
    def __init__(self, max_size: int = 500): ...
    def get_recent(self, n: int = 50) -> List[dict]: ...
```

### API Contract: SSE Server (Optional)

```
GET /events          → SSE stream of real-time events
GET /events?since=X  → Replay events after ID X
GET /status          → Current pipeline state (from ring buffer)
GET /health          → Server health check
```

### CLI Interface

```bash
# Default: NDJSON to stdout (pipe-friendly)
python3 harness.py ace run prd.md --project /path/to/proj
# → emits NDJSON lines to stdout

# With SQLite persistence
python3 harness.py ace run prd.md --project /path/to/proj --stream sqlite

# With SSE server for GUI
python3 harness.py ace run prd.md --project /path/to/proj --stream sse --port 8899

# All output modes
python3 harness.py ace run prd.md --project /path/to/proj --stream all

# Post-hoc replay
python3 harness.py ace stream replay --since "2025-01-15T10:00:00Z"
python3 harness.py ace stream status  # show current pipeline state
```

### Example NDJSON Output

```json
{"id":"evt_001","type":"pipeline.start","timestamp":"2025-01-15T10:30:00.000Z","pipeline_id":"ace_prd_abc","data":{"prd_path":"/tmp/prd.md","task_count":5}}
{"id":"evt_002","type":"task.queued","timestamp":"2025-01-15T10:30:00.001Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"title":"Add login form","category":"logic"}}
{"id":"evt_003","type":"task.start","timestamp":"2025-01-15T10:30:00.002Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"title":"Add login form"}}
{"id":"evt_004","type":"task.step.start","timestamp":"2025-01-15T10:30:00.003Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"step":"context","step_index":1}}
{"id":"evt_005","type":"task.step.complete","timestamp":"2025-01-15T10:30:01.500Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"step":"context","step_index":1,"status":"ok","duration_s":1.5,"result_summary":"3 files"}}
{"id":"evt_006","type":"task.step.start","timestamp":"2025-01-15T10:30:01.501Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"step":"generate","step_index":2}}
{"id":"evt_007","type":"inference.start","timestamp":"2025-01-15T10:30:01.502Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"attempt":1,"port":8080,"prompt_length":2048}}
{"id":"evt_008","type":"inference.complete","timestamp":"2025-01-15T10:30:15.200Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"attempt":1,"tokens_generated":512,"predicted_per_second":35.2,"files_extracted":2}}
{"id":"evt_009","type":"task.step.complete","timestamp":"2025-01-15T10:30:15.201Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"step":"generate","step_index":2,"status":"ok","duration_s":13.7}}
{"id":"evt_010","type":"task.complete","timestamp":"2025-01-15T10:30:30.000Z","pipeline_id":"ace_prd_abc","task_id":"T01","data":{"status":"complete","duration_s":30.0}}
{"id":"evt_011","type":"pipeline.complete","timestamp":"2025-01-15T10:35:00.000Z","pipeline_id":"ace_prd_abc","data":{"total":5,"completed":4,"failed":1,"skipped":0,"duration_s":300}}
```

---

## Implementation Decisions

### 1. EventEmitter over raw callbacks

**Choice:** Dedicated `EventEmitter` class with `subscribe()`/`emit()` pattern.

**Rationale:** Raw callbacks (`on_event = callback`) work for single consumers but break down when multiple subscribers need the same events (stdout + SQLite + ring buffer). The emitter handles fan-out, event envelope construction, and ID generation once, so callers just call `emit("task.start", {...})`.

### 2. NDJSON over raw JSON or binary protocols

**Choice:** Newline-delimited JSON (one JSON object per line).

**Rationale:**
- **vs. raw JSON array:** NDJSON is streamable — you can `tail -f` the output, parse line-by-line, and don't need to wait for the closing `]`.
- **vs. binary (msgpack/protobuf):** JSON is human-readable, works with standard tools (`jq`, `grep`), and the event volume (~100 events per task) doesn't justify binary overhead.
- **vs. logfmt:** JSON is more structured and easier to parse programmatically.

### 3. SQLite for persistence (not a new database)

**Choice:** Write events to the existing `work_events` table in `~/coder-harness-telemetry.db`.

**Rationale:** The telemetry infrastructure already has SQLite with `work_events`, `gpu_snapshots`, and `benchmark_results` tables. Adding a `stream_events` table (or using `work_events` directly) avoids introducing a new database. SQLite is crash-safe (WAL mode), queryable, and the file can be shipped/exported.

**Schema addition:**
```sql
CREATE TABLE IF NOT EXISTS stream_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    pipeline_id TEXT,
    task_id TEXT,
    severity TEXT DEFAULT 'info',
    data TEXT,  -- JSON payload
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stream_events_pipeline ON stream_events(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_stream_events_task ON stream_events(task_id);
CREATE INDEX IF NOT EXISTS idx_stream_events_type ON stream_events(event_type);
```

### 4. SSE over WebSocket for real-time streaming

**Choice:** Server-Sent Events (SSE) endpoint, not WebSocket.

**Rationale:**
- **Uni-directional is sufficient:** Events flow from engine → GUI. The GUI doesn't need to send commands back over the same channel (use HTTP POST for control).
- **Simpler protocol:** SSE uses plain HTTP, works with `EventSource` in browsers, auto-reconnects, and doesn't require a WebSocket library.
- **Existing infrastructure:** The DSH Web GUI already uses HTTP APIs. SSE adds one new endpoint without changing the transport model.
- **Fallback:** NDJSON over stdout works without any server, so SSE is purely additive.

### 5. Ring buffer for fast status queries

**Choice:** Keep last 500 events in a bounded deque in memory.

**Rationale:** The `/status` endpoint needs to answer "what's happening right now?" without hitting SQLite. A ring buffer of 500 events (~5 task executions) is enough to show current task, recent completions, and any errors. Bounded memory means no leak risk.

### 6. Minimal changes to existing code

**Choice:** Replace `_log_event()` calls with `_emit()` calls, add step-level events in pipeline methods.

**Rationale:** The existing `_log_event()` method is called 7 times. Each call site becomes an `_emit()` call with the same data. New events (step-level, inference, quality, git) are added by inserting `_emit()` calls at the start/end of each `_step_*` method — typically 2 lines per step. Total diff: ~80 lines of additions, ~20 lines of replacements.

### 7. Synchronous fan-out (no threads)

**Choice:** Subscribers are called synchronously in the emitter's thread.

**Rationale:** The engine is already synchronous (sequential task execution). Adding threads for event delivery would introduce complexity (thread safety, ordering guarantees) without benefit — the SQLite write takes <1ms, NDJSON is just `print()`, and the ring buffer is in-memory. If a subscriber needs to be async (e.g., SSE broadcast), it can buffer events internally and use its own thread.

### 8. Pipeline ID for correlation

**Choice:** Generate a unique `pipeline_id` per `ace run` invocation (PRD path + timestamp hash).

**Rationale:** Multiple pipeline runs may overlap (if the user starts a second run while the first is still going). The `pipeline_id` lets the GUI and post-hoc tools filter events to a specific run.

---

## File Changes Summary

| File | Change | Lines |
|---|---|---|
| `event_emitter.py` | **New** — EventEmitter class + subscribers | ~120 |
| `task_queue.py` | Replace `_log_event()` with `_emit()`, add step events | ~40 modified |
| `code_generator.py` | Add `inference.start`/`inference.complete` events | ~15 added |
| `harness.py` | Add `--stream` flag, wire up subscribers | ~25 added |
| `stream_server.py` | **New** — SSE endpoint (optional) | ~80 |
| `docs/prd-outer-streaming.md` | This document | — |

**Total new code:** ~240 lines across 2 new files.
**Total modifications:** ~80 lines across 3 existing files.
