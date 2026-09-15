# Specification: Live Streaming for Autonomous Coding Engine

## 1. Streaming Server (`streaming_server.py`)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/events` | Ingest single event |
| `POST` | `/events/batch` | Ingest batch of events |
| `GET` | `/stream` | SSE event stream |
| `GET` | `/events/recent` | Last N events (initial state) |
| `GET` | `/api/snapshot` | Full dashboard state |
| `GET` | `/api/gpu` | Current GPU status |
| `GET` | `/health` | Server health check |
| `GET` | `/` | Serve dashboard HTML |

### SSE Stream Protocol

```
GET /stream?channels=dsh,engine&since=<event-id>
Accept: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

Wire format per event:
```
id: <event-ulid>
event: <channel>:<type>
data: {"id":"...","seq":42,"time":...,"source":"dsh","type":"tool/call","data":{...}}

```

Heartbeat (every 30s):
```
: ping

```

### Ring Buffer

- Per-channel `collections.deque(maxlen=500)`
- FIFO eviction when full
- Used for `Last-Event-ID` replay on reconnection

## 2. Event Envelope

```python
@dataclass
class EventEnvelope:
    id: str                    # ULID (sortable, globally unique)
    seq: int                   # Monotonic stream sequence
    time: int                  # Server wall-clock (epoch ms)
    source_time: int           # Producer timestamp (epoch ms)
    source: str                # "dsh" | "engine" | "system"
    source_id: str             # Session/run identifier
    type: str                  # Namespaced event type
    correlation_id: str | None # Cross-source linking
    data: dict                 # Payload
    schema_version: int = 1
```

## 3. Event Schema (`event_schema.py`)

### Tables

```sql
CREATE TABLE IF NOT EXISTS live_events (
    id TEXT PRIMARY KEY,              -- ULID
    seq INTEGER NOT NULL UNIQUE,      -- Monotonic sequence
    time INTEGER NOT NULL,            -- Server timestamp (epoch ms)
    source_time INTEGER,              -- Producer timestamp
    source TEXT NOT NULL,             -- "dsh" | "engine" | "system"
    source_id TEXT,                   -- Session identifier
    type TEXT NOT NULL,               -- Event type
    correlation_id TEXT,              -- Cross-source link
    data TEXT NOT NULL,               -- JSON payload
    schema_version INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_sequences (
    source TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL DEFAULT 0,
    last_time INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS replay_cursors (
    client_id TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### Indexes

```sql
CREATE INDEX idx_live_events_source ON live_events(source);
CREATE INDEX idx_live_events_type ON live_events(type);
CREATE INDEX idx_live_events_seq ON live_events(seq);
CREATE INDEX idx_live_events_time ON live_events(time);
CREATE INDEX idx_live_events_source_id ON live_events(source_id);
CREATE INDEX idx_live_events_correlation ON live_events(correlation_id);
```

## 4. Event Store (`event_store.py`)

### Class: `EventStore`

```python
class EventStore:
    def __init__(self, db_path: str, flush_interval: float = 1.0, batch_size: int = 100):
        ...

    def append(self, event: EventEnvelope) -> None:
        """Add event to write buffer."""

    def flush(self) -> int:
        """Write buffered events to SQLite. Returns count written."""

    def query(self, source=None, type=None, since_seq=None, until_seq=None,
              source_id=None, limit=1000) -> list[EventEnvelope]:
        """Query events with filters."""

    def get_sequence(self, source: str) -> int:
        """Get next sequence number for source."""

    def get_recent(self, limit: int = 100) -> list[EventEnvelope]:
        """Get most recent events."""

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Delete events older than max_age. Returns count deleted."""
```

### Write Path

1. `append()` adds to in-memory buffer (list)
2. When buffer reaches `batch_size` (100) or `flush_interval` (1s) elapses:
   a. Begin SQLite transaction
   b. INSERT all buffered events
   c. UPDATE source_sequences for each source
   d. COMMIT transaction
3. Ring buffer also receives the event for SSE replay

### Sequence Generation

Monotonic per-source sequences using `source_sequences` table:
```sql
SELECT last_seq FROM source_sequences WHERE source = ?;
-- increment and update
UPDATE source_sequences SET last_seq = ?, last_time = ? WHERE source = ?;
```

## 5. SSE Adapter Pattern

```python
class EventAdapter(Protocol):
    async def connect(self) -> None: ...
    async def events(self) -> AsyncIterator[EventEnvelope]: ...
    async def disconnect(self) -> None: ...

class DSHAdapter(EventAdapter):
    """Tails DSH session JSONL logs or subscribes to DSH SSE."""
    ...

class EngineAdapter(EventAdapter):
    """Watches coder-harness pipeline events."""
    ...
```

## 6. Dashboard Server (`dashboard_server.py`)

- Serves `dashboard/index.html` at `/`
- Proxies SSE from streaming server
- Port 3081 (same as streaming server, serves both API and UI)

## 7. Client Library (`streaming_client.py`)

```python
class StreamClient:
    def __init__(self, base_url: str = "http://127.0.0.1:3081"):
        ...

    def emit(self, channel: str, event_type: str, data: dict,
             source_id: str = None, correlation_id: str = None) -> str:
        """POST event to streaming server. Returns event ID."""

    def emit_batch(self, events: list[dict]) -> list[str]:
        """POST batch of events. Returns list of event IDs."""

    async def stream(self, channels: list[str] = None,
                     since: str = None) -> AsyncIterator[EventEnvelope]:
        """Connect to SSE stream and yield events."""
```

## 8. Configuration (`streaming_config.py`)

```python
@dataclass
class StreamingConfig:
    port: int = 3081
    bind: str = "127.0.0.1"
    channels: list[str] = field(default_factory=lambda: ["dsh", "engine", "system"])
    ring_buffer_size: int = 500
    heartbeat_interval: int = 30
    flush_interval: float = 1.0
    batch_size: int = 100
    retention_hours: int = 24
    db_path: str = "~/coder-harness-telemetry.db"
    auth_token: str | None = None
    redact_sensitive: bool = True
```

## 9. Backpressure Config

```python
BACKPRESSURE_CONFIG = {
    "buffer_size": 10_000,
    "buffer_high_water": 8_000,
    "buffer_low_water": 2_000,
    "sampling": {
        "dsh.assistant.chunk": 0.05,
        "dsh.todo.write": 0.2,
        "engine.pipeline.draft_propose": 0.1,
        "engine.pipeline.cache_hit": 0.05,
        "system.heartbeat": 0.0,
    },
    "always_deliver": [
        "dsh.turn.start", "dsh.turn.end",
        "dsh.tool.call", "dsh.tool.result",
        "dsh.assistant.message",
        "engine.model.*", "engine.sandbox.*",
        "engine.inference.complete",
        "engine.quality.*",
        "engine.pipeline.stall",
        "engine.pipeline.thermal",
        "system.*",
    ],
}
```

## 10. Security

```python
REDACTION_RULES = {
    "dsh.tool.call": {
        "redact_fields": ["arguments"],
        "patterns": [
            (r'(GITEA_TOKEN[=:])\S+', r'\1[REDACTED]'),
            (r'(Bearer\s+)\S+', r'\1[REDACTED]'),
            (r'(api[_-]?key[=:])\S+', r'\1[REDACTED]'),
        ],
    },
    "engine.inference.*": {
        "truncate_fields": ["prompt"],
        "max_length": 2000,
    },
}
```
