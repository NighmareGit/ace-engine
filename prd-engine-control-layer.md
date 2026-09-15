# PRD: Engine Control Layer — Non-SSH Communication

## Problem Statement

The coder-harness engine runs on Triton (<LAN_IP>) but is controlled via SSH from nightmare. This creates multiple failure modes:

1. SSH sessions timeout (60s default) killing long-running inference (model thinking takes 30-60s)
2. sshpass not installed on nightmare, breaking password-based authentication
3. Stateless execution — each SSH command starts a fresh Python process, losing all in-memory state
4. Transport auto-detection is fragile (checks /.dockerenv, fails on native Triton)
5. Bidirectional SSH is complex — streaming data back requires polling
6. Password "12345" is hardcoded in multiple files (credential leak)
7. Three parallel SSH implementations (ssh_utils.py, remote_control.py, work_engine.py) create maintenance debt

The engine, BeeLlama, Gitea, and project files all live on the same Triton machine. SSH to localhost is architecturally wrong.

## Solution

Build an HTTP API service (`engine_service.py`) that runs as a persistent process on Triton. DSH communicates via HTTP requests instead of SSH commands. Fix SSH as a fallback for the transition period. The service wraps BeeLlama inference, file operations, test execution, git workflows, and GPU monitoring behind a clean REST API.

## User Stories

1. As a **developer**, I want to send inference requests to the engine via HTTP so that long-running model calls (30-60s thinking) don't timeout
2. As a **developer**, I want the engine service to auto-start on Triton boot so I don't have to manually start it
3. As a **developer**, I want the engine to return inference results directly via HTTP response so I don't need to poll or SSH back
4. As a **developer**, I want the engine to write generated files to disk via a single HTTP call so I don't need multiple SSH commands
5. As a **developer**, I want to run tests via the engine API so test results come back in the same HTTP response
6. As a **developer**, I want to commit code to Gitea via the engine API so git operations are centralized
7. As a **developer**, I want the engine to report GPU temperature and VRAM usage via HTTP so I can monitor hardware without SSH
8. As a **developer**, I want the engine to stream live events via SSE so I can build a real-time dashboard
9. As a **developer**, I want the engine to auto-restart if it crashes so I don't need manual intervention
10. As a **developer**, I want simple token authentication (`Authorization: Token <secret>` — NOT Bearer) so unauthorized clients can't access the engine
11. As a **developer**, I want the transport layer to auto-detect whether to use HTTP, local, or SSH so existing code works without changes
12. As a **developer**, I want SSH to still work as a fallback so I can debug when the HTTP service is down
13. As an **operator**, I want health check endpoints so I can monitor service status from monitoring tools
14. As an **operator**, I want the service to log requests and errors so I can debug issues
15. As a **developer**, I want the engine to execute arbitrary shell commands via API so I can run custom scripts on Triton
16. As a **developer**, I want the engine to support concurrent requests so multiple DSH agents can use it simultaneously
17. As a **developer**, I want the service to have a clean shutdown (finish current request, then exit) so I can update without data loss

## Implementation Decisions

### Module Organization

Create these files in the project root:

- `engine_service.py` — FastAPI HTTP server: inference proxy, file ops, test runner, git ops, GPU monitoring, SSE streaming. ~300 lines.
- `engine_client.py` — Python HTTP client class for calling the engine service. Mirrors LocalTransport/RemoteTransport API. ~120 lines.

Modify these files:

- `transport.py` — Add `HTTPTransport` class. Update `get_transport()` to try HTTP first, then local, then SSH.
- `harness.py` — Add `engine start/stop/status` CLI commands.

### API Endpoints

| Method | Path | Request Body | Response | Timeout |
|--------|------|-------------|----------|---------|
| `POST` | `/inference` | `{port: int, messages: list, max_tokens: int, temperature: float}` | `{content: str, reasoning: str, timings: dict}` | 300s |
| `POST` | `/write-files` | `{project_path: str, files: [{path: str, content: str}]}` | `{written: list, errors: list, bytes_total: int}` | 60s |
| `POST` | `/run-tests` | `{project_path: str, pattern: str}` | `{status: str, passed: int, failed: int, results: list}` | 120s |
| `POST` | `/git-commit` | `{project_path: str, message: str, files: list}` | `{sha: str, message: str}` | 30s |
| `POST` | `/exec` | `{command: str, cwd: str, timeout: int}` | `{stdout: str, stderr: str, rc: int}` | varies |
| `GET` | `/health` | — | `{status: str, uptime: float, model: str, gpu_temp: list}` | 5s |
| `GET` | `/gpu-status` | — | `{gpus: [{index: int, temp: float, vram_used: float, vram_total: float, util: float}]}` | 10s |
| `GET` | `/stream` | SSE query: `?channels=engine,gpu` | `text/event-stream` | persistent |

### Authentication

Token auth via `Authorization: Token <secret>` header (NOT Bearer). Token stored in environment variable `ENGINE_TOKEN`. Default: `engine-secret-2024`. Bind to `0.0.0.0:3082`.

### Transport Integration

`transport.py` gets a new `HTTPTransport` class that mirrors the `LocalTransport` API but sends HTTP requests to the engine service. `get_transport()` auto-detection order:

1. Try HTTP (GET /health on port 3082) → `HTTPTransport`
2. Try local (probe localhost:8080) → `LocalTransport`
3. Fall back to SSH → `RemoteTransport`

### Service Management

- **Start**: `python3 engine_service.py --port 3082 --token <secret>`
- **Stop**: SIGTERM → graceful shutdown (finish current request, then exit)
- **systemd**: Unit file for auto-restart (`Restart=always`)
- **Docker**: Can run inside the existing engine Docker container with `network_mode: host`

### SSH Fallback

Keep `RemoteTransport` but fix the immediate issues:
- Install sshpass on nightmare: `sudo apt install sshpass`
- Use SSH keys instead of passwords (already configured)
- Add `ControlMaster=auto` for connection pooling
- Increase timeout to 300s for inference calls

## Testing Decisions

- **Unit tests**: API endpoint request/response validation for all 8 endpoints
- **Integration test**: POST inference → verify response → POST write-files → verify file exists on disk
- **E2E test**: Run full PRD through HTTP transport end-to-end (parse → manifest → infer → write → test → commit)
- **Stress test**: 5 concurrent inference requests, verify no drops
- **Resilience test**: Kill service mid-inference, verify client retries and falls back to SSH
- **Transport test**: Verify `get_transport()` returns correct transport in each environment

## Out of Scope

- WebSocket transport (SSE is sufficient for unidirectional streaming)
- Multi-tenant authentication (single user for now)
- GPU rental/scheduling (one machine, two GPUs)
- DSH native deployment on Triton (Tier 3 — future PRD)
- Mobile-optimized dashboard (desktop-first)
- Grafana/Prometheus integration (SQLite-based monitoring)

## Further Notes

### Three-Tier Architecture

This PRD covers **Tier 2** (HTTP Gateway). The full plan:

1. **Tier 1 (immediate)**: Fix SSH — install sshpass, use keys, increase timeouts. ~30 minutes.
2. **Tier 2 (this PRD)**: HTTP API service on Triton. Clean separation of concerns. ~4 hours.
3. **Tier 3 (future)**: DSH runs natively on Triton. No remote communication needed.

### State Machine

The engine service has two state machines:
- **Service lifecycle**: STOPPED → STARTING → READY → PROCESSING → DEGRADED → FAILED
- **Transport selection**: get_transport() → HTTP? → Local? → SSH fallback

### Grill-Me Insight

The grill-me session asked: "Are you building an API because you need one, or because it feels more modern than SSH?"

Answer: We need one because SSH fundamentally cannot handle long-running inference (30-60s thinking time exceeds SSH timeout), and the alternative (fixing SSH) only addresses symptoms. The HTTP service gives us proper timeout handling, process lifecycle management, direct port accessibility, and a foundation for streaming.

### Fireplace Insight

The fireplace identified that the engine is "five different things you SSH into separately" — not a cohesive service. The HTTP API unifies them into a single interface, which is the real architectural improvement.
