# ACE Watch — Gap Fix + Split State Machine

**Date:** 2026-09-04  
**Parent:** `ace-watch-state-machine.md` (original 3-track build)  
**Branch:** `meta-loop-self-improve`  
**Workdir:** `/home/<user>/projects/ace-engine/`

---

## Scope

Fix the 10 known gaps from the handoff document and split the 1444-line
`streaming_server.py` into maintainable modules.

---

## Dependency Graph (Full)

```
S.1 [SPLIT_CORE] ──→ S.2 [SPLIT_ROUTES] ──→ S.3 [THIN_MAIN] ──┬──→ S.4 [LIFESPAN]
                                                              ├──→ S.5 [PERSISTENT_DB]
                                                              └──→ S.6 [AUTH_READS]
                                                                       │
S.7 [SPLIT_TESTS] ←──────────────────────────────────────────────────┘

G.1 [TESTS_DSH] ──────────────────────────────────────────→ G.7 [WS_ADAPTER]
G.2 [TESTS_GPU] ────────────────────────→ G.5 [GPU_WIRE]
G.3 [TASK_ID]  (standalone)
G.4 [DEMO_MODE] (standalone)
G.6 [SYSTEMD]  (standalone)

Integration:
S.7 ─┬─→ I.1 [INTEGRATION] ──→ I.2 [DEPLOY] ──→ I.3 [HANDOFF]
G.1 ─┤
G.2 ─┤
G.3 ─┤
G.4 ─┤
G.5 ─┤
G.6 ─┘
G.7 ─┘ (optional — nice to have)
```

---

## Track S: Split + Refactor `streaming_server.py`

### S.1 — SPLIT_CORE

**Goal:** Extract core infrastructure into `streaming_core.py`

Extract these classes/functions from `streaming_server.py`:
- `generate_ulid()` + `_C32` constant
- `RingBuffer` class (full)
- `SSEClientManager` class (full)
- `StreamingServer` class (full — but using per-event DB connection for now)
- `_format_sse()` function
- `_periodic_cleanup()` function

**Output file:** `streaming_core.py`  
**Imports:** `from streaming_core import ...` in refactored `streaming_server.py`  
**Constraints:**
- Preserve all existing method signatures exactly (no behavioral change)
- `StreamingServer.__init__` stays identical
- `StreamingServer._connect_db()` stays as-is (S.5 will fix per-event later)

**Acceptance:** `streaming_core.py` passes `python3 -c "import streaming_core; print('ok')"`

### S.2 — SPLIT_ROUTES

**Goal:** Extract all route handlers and dashboard config into `streaming_routes.py`

Extract from `streaming_server.py`:
- `_load_dashboard_config()` / `_save_dashboard_config()` + `_DASHBOARD_CONFIG_PATH`
- `create_app()` function (all route definitions)

**Output file:** `streaming_routes.py`  
**Imports:** `from streaming_routes import create_app`  
**Constraints:**
- `create_app()` signature stays: `(db_path, ring_buffer_size, auth_token) -> FastAPI`
- All route handlers move as-is
- Dashboard config helpers move with the routes that use them

**Acceptance:** `streaming_routes.py` imports cleanly, `create_app` returns a `FastAPI` instance.

### S.3 — THIN_MAIN

**Goal:** Reduce `streaming_server.py` to CLI entry point + imports

After S.1 and S.2, `streaming_server.py` should contain only:
- Module docstring
- `import argparse`, `import uvicorn`, `import os`, `import signal`, `import logging`
- `parse_args()` — unchanged
- `main()` — unchanged (it already calls `create_app(...)`)
- `if __name__ == "__main__": main()` — unchanged

All other code now lives in `streaming_core.py` and `streaming_routes.py`.

**Acceptance:** `python3 streaming_server.py --help` works; file under 100 lines.

### S.4 — LIFESPAN (Gap #5)

**Goal:** Migrate `@app.on_event("startup")` and `@app.on_event("shutdown")` → `lifespan` context manager

In `streaming_routes.py` (where `create_app` lives after S.2):

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await server.init_db()
    app.state._cleanup_task = asyncio.create_task(_periodic_cleanup(server))
    yield
    task = getattr(app.state, "_cleanup_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    for cid in list(server.sse_manager._clients.keys()):
        server.sse_manager.disconnect(cid)
```

Replace `@app.on_event` decorators with `app = FastAPI(..., lifespan=lifespan)`.

**Acceptance:** Server starts, serves events, shuts down cleanly. No deprecation warnings in logs.

### S.5 — PERSISTENT_DB (Gap #4)

**Goal:** Replace per-event `sqlite3.connect()` with a persistent connection

In `streaming_core.py` → `StreamingServer`:

1. Add `self._db_conn: Optional[sqlite3.Connection] = None` to `__init__`
2. Add `self._db_lock = asyncio.Lock()` (already exists)
3. In `init_db()`: create `self._conn = self._connect_db()` once, keep open
4. In `ingest_event()`: use `self._conn` instead of opening/closing per event
5. In `query_events()`: use `self._conn` in the `_query()` closure
6. In `_periodic_cleanup()`: use `server._conn`
7. In shutdown lifespan (S.4): close `self._conn`

**Key consideration:** SQLite + asyncio — use `await loop.run_in_executor(None, ...)` for writes, or keep the lock pattern. Since `_db_lock` already serializes writes, a single connection is safe.

**Acceptance:** Events persist; no "database is locked" errors under load test; connection closes cleanly on shutdown.

### S.6 — AUTH_READS (Gap #3)

**Goal:** Add bearer-token auth to `GET /stream` and `GET /api/snapshot`

In `streaming_routes.py`, add auth check to:
- `get_stream()` — call `server.check_auth(request)`, return 401 if invalid
- `get_snapshot()` — call `server.check_auth(request)`, return 401 if invalid

Only enforce when `auth_token` is configured (backward compatible).

**Acceptance:** `curl http://localhost:3081/stream` → 401 when token is set; `curl -H "Authorization: Bearer <token>" http://localhost:3081/stream` → 200.

### S.7 — SPLIT_TESTS

**Goal:** Tests for the refactored split modules

Add to `test_streaming.py` or new `test_streaming_split.py`:
- Test `streaming_core.RingBuffer` directly (append, replay_since, get_all)
- Test `streaming_core.SSEClientManager` directly (connect, disconnect, broadcast, client_count)
- Test `streaming_core._format_sse()` output format
- Test `streaming_core.generate_ulid()` format (26 chars, Crockford base32)
- Test `streaming_routes.create_app()` returns configured FastAPI with all routes
- Test lifespan startup/shutdown lifecycle

**Acceptance:** All new tests pass; original 91 tests still pass.

---

## Track G: Gap Fixes

### G.1 — TESTS_DSH (Gap #2 partial)

**Goal:** Unit tests for `dsh_adapter.py` projection logic

Test file: `test_dsh_adapter.py` (new)

Test cases:
- `_truncate_value()` — string under limit unchanged, string over limit truncated with "…[truncated]"
- `_truncate_data()` — nested dict/list truncation, non-string passthrough
- `_DSH_EVENT_MAP` — all entries map DSH type → stream type
- `DSHAdapter._project_event()` — known type returns envelope with correct source/type; unknown type returns None
- `DSHAdapter._project_event()` — data fields exclude routing keys (type, seq, session_id, sessionId)
- `DSHAdapter._project_event()` — preserves dsh_seq when present
- `DSHAdapter._generate_mock_event()` — returns valid raw event dict with type and seq
- `_safe_json_get()` — returns None on unreachable URL (mock with urllib.error)
- `_safe_json_get()` — returns parsed JSON on success (mock response)

**Acceptance:** 10+ tests, all pass. `python3 -m pytest test_dsh_adapter.py -v`

### G.2 — TESTS_GPU (Gap #2 partial)

**Goal:** Unit tests for `gpu_telemetry.py` parsing

Test file: `test_gpu_telemetry.py` (new)

Test cases:
- `_parse_nvidia_smi()` — valid 2-GPU CSV output → correct dict with gpu_3090/gpu_3070 keys
- `_parse_nvidia_smi()` — single GPU output → single entry
- `_parse_nvidia_smi()` — empty string → empty dict
- `_parse_nvidia_smi()` — malformed line (too few fields) → skipped with warning
- `_parse_nvidia_smi()` — non-numeric temp → skipped with warning
- `_parse_nvidia_smi()` — custom gpu_map override works
- `_NVIDIA_SMI_FIELDS` — contains expected fields (index, name, temperature.gpu, utilization.gpu, memory.used, memory.total)
- `GPUTelemetryPoller.__init__` — defaults set correctly, interval floored at 0.5
- `GPUTelemetryPoller.start()` / `stop()` — thread lifecycle (start twice is no-op)
- `poll_once()` — with mocked `_query_nvidia_smi` returning valid CSV, calls `client.emit_event`

**Acceptance:** 10+ tests, all pass. `python3 -m pytest test_gpu_telemetry.py -v`

### G.3 — TASK_ID (Gap #6)

**Goal:** Thread `task_id` through `quality_gates.py` emit calls

Current state: `quality_gates.py` imports `emit_event_fire_and_forget` and calls it, but doesn't pass `task_id` in the data payload.

Fix:
1. In `check_all()` — accept optional `task_id` parameter
2. In `_emit_quality_result()` (or wherever emit is called) — include `task_id` in the data dict
3. Update callers in `task_queue.py` to pass `task_id` when invoking quality gates

Files to modify:
- `quality_gates.py` — add `task_id` parameter to `check_all()`, include in emit data
- `task_queue.py` — pass `task_id=task.id` or similar when calling quality gates

**Acceptance:** Quality gate events in the stream include `task_id` field. Existing tests pass.

### G.4 — DEMO_MODE (Gap #7)

**Goal:** Add demo mode toggle button to dashboard

In `dashboard/index.html`:
1. Add a "Demo Mode" toggle button in the header
2. When active, generate synthetic events (similar to dsh_adapter --mock)
3. Event types to simulate: `engine.task.start`, `engine.task.complete`, `engine.inference.start`, `engine.inference.complete`, `gpu.snapshot`, `system.annotation`
4. Toggle via `?demo=1` query param OR the button (button takes precedence)
5. Show visual indicator when demo mode is active (e.g., "DEMO" badge in header)

**Acceptance:** Toggle button visible; clicking it starts/stop synthetic events; events appear in both panes; URL param `?demo=1` auto-enables.

### G.5 — GPU_WIRE (Gap #8)

**Goal:** Wire GPU telemetry events into the engine service stream

Currently `gpu_telemetry.py` runs as a separate process. Make the engine service optionally emit GPU snapshots.

In `engine_service.py`:
1. Add `--with-gpu` flag to start a background GPU poller
2. Use `gpu_telemetry.GPUTelemetryPoller` directly (in-process, no subprocess)
3. Poller emits to the streaming server via `StreamingClient`
4. Add `/gpu-snapshot` endpoint that returns latest GPU stats on-demand

In `engine_service.py`, add route:
- `GET /gpu-status` — already exists, but ensure it includes streaming event emission

**Files to modify:**
- `engine_service.py` — add `--with-gpu` flag, instantiate poller, wire events
- Optionally: `task_queue.py` — emit GPU snapshot before each task.run

**Acceptance:** Engine service with `--with-gpu` emits `gpu.snapshot` events; `/gpu-status` endpoint returns current stats.

### G.6 — SYSTEMD (Gap #9)

**Goal:** Create systemd unit for streaming server

File: `tickets/deploy/streaming-server.service` (new)

```ini
[Unit]
Description=ACE Watch Streaming Server (SSE + Dashboard)
After=network.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/projects/coder-harness
ExecStart=/usr/bin/python3 streaming_server.py --port 3081 --host 0.0.0.0 --token ${STREAMING_TOKEN}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/home/<user>/projects/coder-harness/.env.streaming

[Install]
WantedBy=multi-user.target
```

Also create:
- `.env.streaming` template with `STREAMING_TOKEN=change-me`
- Deployment instructions in a comment header

**Acceptance:** Unit file is syntactically valid (`systemd-analyze verify` if available, or manual review).

### G.7 — WS_ADAPTER (Gap #10) [OPTIONAL]

**Goal:** DSH adapter WebSocket variant for lower latency

Create `dsh_adapter_ws.py` — extends `DSHAdapter` with:
- WebSocket connection to DSH (if DSH supports WS)
- Falls back to HTTP polling if WS unavailable
- Same projection logic, different transport

**Note:** Only implement if DSH has a WebSocket endpoint. Otherwise, document the gap as "blocked on DSH WS support."

**Acceptance:** New file exists with WS adapter class; falls back to HTTP when WS unavailable.

---

## Wave Plan for Sub-Agent Dispatch

### Wave 1 (Fully Parallel — No Cross-Dependencies)

| Agent | Track | Task | Est. Files |
|-------|-------|------|------------|
| **A-S1** | S | SPLIT_CORE: Create `streaming_core.py` | 1 new |
| **A-G1** | G | TESTS_DSH: Create `test_dsh_adapter.py` | 1 new |
| **A-G2** | G | TESTS_GPU: Create `test_gpu_telemetry.py` | 1 new |
| **A-G3** | G | TASK_ID: Fix quality_gates.py threading | 2 modified |
| **A-G4** | G | DEMO_MODE: Dashboard toggle button | 1 modified |
| **A-G6** | G | SYSTEMD: Create streaming-server.service | 1 new |

### Wave 2 (After S.1)

| Agent | Track | Task | Est. Files |
|-------|-------|------|------------|
| **A-S2** | S | SPLIT_ROUTES: Create `streaming_routes.py` | 1 new |

### Wave 3 (After S.2)

| Agent | Track | Task | Est. Files |
|-------|-------|------|------------|
| **A-S3** | S | THIN_MAIN: Reduce `streaming_server.py` to CLI | 1 modified |

### Wave 4 (After S.3 — Parallel)

| Agent | Track | Task | Est. Files |
|-------|-------|------|------------|
| **A-S4** | S | LIFESPAN: Migrate on_event → lifespan | 1 modified (routes) |
| **A-S5** | S | PERSISTENT_DB: Connection pool in StreamingServer | 1 modified (core) |
| **A-S6** | S | AUTH_READS: Token auth on GET /stream + /api/snapshot | 1 modified (routes) |

### Wave 5 (After Wave 4 + G.2 — Parallel)

| Agent | Track | Task | Est. Files |
|-------|-------|------|------------|
| **A-S7** | S | SPLIT_TESTS: Tests for split modules | 1 new |
| **A-G5** | G | GPU_WIRE: Wire GPU into engine service | 1 modified |
| **A-G7** | G | WS_ADAPTER: DSH WebSocket variant (optional) | 1 new |

### Wave 6 — Integration

| Agent | Task |
|-------|------|
| **A-I1** | Run all tests, verify 91+ pass + new tests pass |
| **A-I2** | Deploy to Triton, verify live dashboard |
| **A-I3** | Update handoff document |

---

## Total Impact

| Metric | Before | After |
|--------|--------|-------|
| `streaming_server.py` lines | 1444 | ~80 |
| `streaming_core.py` lines | — | ~500 |
| `streaming_routes.py` lines | — | ~850 |
| Test count | 91 | 120+ |
| Known gaps open | 10 | 0-1 (G.7 optional) |

---

## Risk Register

| Risk | Mitigation |
|------|-----------|
| Split breaks existing tests | S.7 validates; integration gate runs all 91 first |
| Persistent DB connection + asyncio race | Keep `_db_lock` for all writes; single-thread executor |
| Auth on /stream breaks open dashboard | Only enforce when `auth_token` is set |
| Demo mode event loop conflicts | Use `setTimeout`/`setInterval` in browser, not SSE reconnection |
| GPU wire requires nvidia-smi on same host | Make `--with-gpu` flag opt-in; skip gracefully |

---

## File Map After Completion

```
coder-harness/
├── streaming_server.py          # ~80 lines — CLI entry only
├── streaming_core.py            # ~500 lines — RingBuffer, SSEClientManager, StreamingServer, ULID
├── streaming_routes.py          # ~850 lines — create_app, all routes, config helpers
├── streaming_client.py          # unchanged
├── event_schema.py              # unchanged
├── dsh_adapter.py               # unchanged (G.7 adds dsh_adapter_ws.py)
├── dsh_adapter_ws.py            # NEW (optional, G.7)
├── gpu_telemetry.py             # unchanged
├── engine_service.py            # modified (G.5 — --with-gpu flag)
├── quality_gates.py             # modified (G.3 — task_id threading)
├── task_queue.py                # modified (G.3 — pass task_id)
├── dashboard/index.html         # modified (G.4 — demo toggle)
├── test_streaming.py            # unchanged
├── test_streaming_integration.py # unchanged
├── test_dsh_adapter.py          # NEW (G.1)
├── test_gpu_telemetry.py        # NEW (G.2)
├── test_streaming_split.py      # NEW (S.7)
├── tickets/deploy/
│   ├── engine-service.service   # existing
│   └── streaming-server.service # NEW (G.6)
└── .env.streaming               # NEW template (G.6)
```
