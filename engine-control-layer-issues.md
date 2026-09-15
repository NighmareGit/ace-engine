# Engine Control Layer — Issue Tracker

> Vertical-slice decomposition of `engine-control-layer-tasks.json` (10 tasks → 6 issues).
> Each issue is independently demoable end-to-end.

---

## Issue #1 — Engine Service: Core + Auth + Health

**Derived from:** T01

### What to build

Create `engine_service.py` with a FastAPI app skeleton, token authentication (`Authorization: Token <secret>` — NOT Bearer; via `ENGINE_TOKEN` env var), Pydantic request/response models for all 8 endpoints, consistent error response format, structured JSON request logging, and a `GET /health` endpoint. Include a SIGTERM handler that sets a shutdown flag and allows in-flight requests to drain. CLI args: `--port` (default 3082), `--token` (default from `ENGINE_TOKEN` env). Port-conflict detection on startup.

**End-to-end behavior:** Start the service → hit `/health` → get `{status, uptime, model, gpu_temp}` within 5s → hit any endpoint without a token → get 401 → send a bad token → get 403 → send SIGTERM → service drains in-flight requests → exits cleanly.

### Acceptance criteria
- [ ] FastAPI app starts on `0.0.0.0:3082` with CORS enabled
- [ ] Token auth (`Authorization: Token <secret>` — NOT Bearer) rejects requests without valid `Authorization` header
- [ ] `GET /health` returns `{status, uptime, model, gpu_temp}` within 5s
- [ ] Pydantic models defined for all endpoint request/response types
- [ ] Consistent error response: `{error: str, detail: str, status_code: int}`
- [ ] SIGTERM triggers graceful shutdown (finish in-flight, then exit)
- [ ] Structured JSON logging for every request (method, path, status, duration)
- [ ] Port conflict returns clear error message on startup

### Blocked by
None — can start immediately

---

## Issue #2 — Engine Service: Inference + File Ops + Tests + Git

**Derived from:** T02, T03, T04

### What to build

Implement four operational endpoints on the engine service: `POST /inference` (proxy chat completions to BeeLlama with 300s timeout), `POST /write-files` (atomic writes with path traversal protection), `POST /run-tests` (pytest subprocess with 120s timeout), and `POST /git-commit` (git add + commit via subprocess). Update the service state to `PROCESSING` during inference, back to `READY` on completion.

**End-to-end behavior:** Send a chat completion request → BeeLlama responds with content + reasoning + timings → write files to a project → run pytest against the project → commit results to git → all within one authenticated session, with structured error handling and state transitions.

### Acceptance criteria
- [ ] `POST /inference` proxies to correct BeeLlama port (8080 or 8082) based on `port` param
- [ ] 300s timeout allows long-running inference (30–60s thinking)
- [ ] Inference response includes `content`, `reasoning_content`, `timings`, `usage`
- [ ] 503 returned when BeeLlama is unreachable
- [ ] Service state transitions: `READY → PROCESSING → READY` during inference
- [ ] `POST /write-files` writes all files to specified `project_path` via atomic temp+rename
- [ ] Path traversal (`../`) rejected with 400 error
- [ ] Partial failures collected: `written` list has successes, `errors` list has failures
- [ ] `POST /run-tests` executes pytest in `project_path` with pattern filter
- [ ] Test results parsed: `passed`/`failed` counts + per-test status
- [ ] 120s timeout kills long-running test suites gracefully
- [ ] `POST /git-commit` stages specified files and commits with message, returns `sha`

### Blocked by
Issue #1 (needs the service core, auth, and models)

---

## Issue #3 — Engine Service: Exec, GPU Status, and SSE Streaming

**Derived from:** T05

### What to build

Implement `POST /exec` (run shell commands with configurable timeout and dangerous command blocking), `GET /gpu-status` (query nvidia-smi for per-GPU metrics), and `GET /stream` (SSE endpoint with channel filtering and 30s heartbeat keepalive). The exec endpoint warns on and blocks dangerous commands (`rm -rf /`, `mkfs`, etc.) unless an `--allow-exec` flag is set at service startup. The SSE endpoint streams periodic engine and GPU events.

**End-to-end behavior:** Execute a safe shell command → get stdout/stderr/rc → try a dangerous command → get blocked → poll GPU status → get per-GPU temp/VRAM/utilization → connect to SSE stream → receive heartbeats every 30s → receive filtered channel events.

### Acceptance criteria
- [ ] `POST /exec` runs shell command with configurable timeout
- [ ] Dangerous commands blocked or warn-logged (`rm -rf`, `mkfs`, etc.)
- [ ] `GET /gpu-status` returns per-GPU metrics from nvidia-smi
- [ ] `GET /stream` establishes SSE connection with correct `Content-Type`
- [ ] SSE heartbeat sent every 30s to prevent proxy/client timeout
- [ ] Channel filtering: only events for requested channels are sent
- [ ] exec timeout returns partial stdout/stderr + timeout error

### Blocked by
Issue #1 (needs the service core, auth, and models)

---

## Issue #4 — Engine HTTP Client

**Derived from:** T06

### What to build

Create `engine_client.py` with an `EngineClient` class that mirrors the `LocalTransport`/`RemoteTransport` API. Uses `urllib.request` (stdlib) for zero dependencies. Features: connection reuse, configurable `base_url` and `token`, per-endpoint request timeout, retry with exponential backoff (3 attempts, 1s/2s/4s), and structured error handling via `EngineClientError`. Methods: `inference()`, `write_files()`, `run_tests()`, `git_commit()`, `exec_command()`, `health()`, `gpu_status()`, `stream()`. All methods return parsed JSON dicts.

**End-to-end behavior:** Instantiate `EngineClient` with base URL and token → call any endpoint → get parsed JSON response → kill the engine service mid-request → client retries 3 times with backoff → on 3rd failure raises `EngineClientError` with status code and response body → call `stream()` → get an iterator of SSE events.

### Acceptance criteria
- [ ] `EngineClient` class with all 8 endpoint methods
- [ ] Zero external dependencies (stdlib urllib only)
- [ ] Token auth via `Authorization: Token <secret>` header (NOT Bearer)
- [ ] Retry with exponential backoff: 3 attempts, delays 1s/2s/4s
- [ ] Connection reuse via urllib connection pooling
- [ ] Per-endpoint timeout configuration
- [ ] `EngineClientError` exception class with `status_code` and response body
- [ ] Methods return parsed JSON dicts matching API response schemas
- [ ] `stream()` returns an iterator of SSE events

### Blocked by
Issue #2, Issue #3 (needs all endpoints implemented so the client methods have something to call)

---

## Issue #5 — HTTPTransport + CLI Lifecycle

**Derived from:** T07, T08

### What to build

Add `HTTPTransport` class to `transport.py` that wraps `EngineClient` and mirrors the `LocalTransport`/`RemoteTransport` API. Update `get_transport()` with new priority: HTTP → Local → SSH. Include transport fallback: if HTTP fails with `ConnectionError`, log warning and fall back to SSH. Cache transport selection with `TRANSPORT_MODE` env var override.

Add `engine` subcommand group to `harness.py`: `engine start [--port] [--token]`, `engine stop`, `engine status`. Implement the full service lifecycle state machine: `STOPPED → STARTING → READY → PROCESSING → DEGRADED → FAILED`, persisted in a `.state` file with consecutive error tracking (5+ → DEGRADED, 10+ → FAILED).

**End-to-end behavior:** Start the engine via `harness.py engine start` → PID file written → status shows `READY` → existing `code_generator`, `test_runner`, `git_workflow` work transparently via HTTPTransport → if engine dies, transport falls back to SSH → `harness.py engine stop` sends SIGTERM → status shows `STOPPED`. Restart engine → state machine recovers from `.state` file.

### Acceptance criteria
- [ ] `HTTPTransport` class implements same API surface as `LocalTransport`/`RemoteTransport`
- [ ] `get_transport()` tries HTTP first (probe `localhost:3082/health`)
- [ ] Fallback chain: HTTP → Local → SSH
- [ ] `TRANSPORT_MODE` env var can force: `http`, `local`, `ssh`, or `auto`
- [ ] `HTTPTransport` falls back to SSH on `ConnectionError`
- [ ] Transport selection logged at startup
- [ ] Existing code (`code_generator`, `test_runner`, `git_workflow`) works without changes
- [ ] `HTTPTransport` handles 503 from engine service gracefully
- [ ] `harness.py engine start` launches service as background process, writes PID file
- [ ] `harness.py engine stop` sends SIGTERM gracefully
- [ ] `harness.py engine status` shows service health + uptime + state
- [ ] State machine: `STOPPED → STARTING → READY → PROCESSING → DEGRADED → FAILED`
- [ ] State persisted in `.state` file for crash recovery
- [ ] Consecutive error count tracked; 5+ errors → DEGRADED, 10+ → FAILED
- [ ] Engine start fails gracefully if port is already in use

### Blocked by
Issue #1 (needs the engine service running to probe), Issue #4 (needs `EngineClient`)

---

## Issue #6 — Deployment + Tests

**Derived from:** T09, T10

### What to build

Create `engine_service.unit` systemd unit file with `Type=simple`, `Restart=always`, `RestartSec=5`, environment from `ENGINE_TOKEN` env and `EnvironmentFile=-/etc/engine-token`. Fix SSH fallback in `transport.py`: remove hardcoded password, use SSH key auth only, add `ControlMaster=auto` for connection pooling, increase SSH timeout to 300s for inference, add `ServerAliveInterval=30` keepalive.

Create `test_engine_service.py` with unit tests for all 8 endpoints, a full integration pipeline test (inference → write-files → run-tests → git-commit), a resilience test (kill service mid-inference → verify retry + SSH fallback), transport selection tests with mocked environments, auth validation, and a concurrent request test (5 parallel inferences, no drops).

**End-to-end behavior:** Deploy the engine via systemd → service auto-restarts on failure → SSH connections are pooled and alive → run the full test suite → all 8 endpoints validated → integration pipeline completes end-to-end → kill the service during a request → client retries and falls back to SSH → 5 concurrent requests complete without errors.

### Acceptance criteria
- [ ] systemd unit file starts `engine_service.py` with correct env
- [ ] `Restart=always` with `RestartSec=5` for auto-recovery
- [ ] `EnvironmentFile` supports token from `/etc/engine-token`
- [ ] SSH uses key auth only (no sshpass dependency)
- [ ] SSH `ControlMaster=auto` for connection pooling
- [ ] SSH timeout increased to 300s for inference
- [ ] SSH keepalive: `ServerAliveInterval=30`, `ServerAliveCountMax=10`
- [ ] No hardcoded passwords in any file
- [ ] All 8 endpoints have request/response validation tests
- [ ] Integration test completes full write → test → commit pipeline
- [ ] Resilience test: service kill → client retry → SSH fallback works
- [ ] Transport selection returns HTTP when service is up, SSH when service is down
- [ ] Auth: missing token → 401, wrong token → 403, valid token → 200
- [ ] 5 concurrent requests complete without drops or errors
- [ ] All tests runnable with: `python3 -m pytest test_engine_service.py`

### Blocked by
Issue #1, Issue #4, Issue #5 (needs the full service, client, and transport layer working)

---

## Dependency Summary

```
Issue #1 (Core)
  ├─→ Issue #2 (Inference + Files + Tests + Git)
  ├─→ Issue #3 (Exec + GPU + SSE)
  │     └─→ Issue #4 (Client)  ← also needs #2
  │           └─→ Issue #5 (Transport + CLI)  ← also needs #1
  │                 └─→ Issue #6 (Deploy + Tests)
  └─→ Issue #3 (parallel with #2)
```

**Parallelizable:** Issue #2 and Issue #3 can be built in parallel after Issue #1.
