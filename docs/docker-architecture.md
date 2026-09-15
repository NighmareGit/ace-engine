# Docker Architecture for Coder-Harness Engine

> **Author:** MiMo (meta-cognitive Ralph loop)
> **Date:** 2025-09-03
> **Status:** Design proposal — ready for review

---

## 1. Problem

The coder-harness engine currently runs on a separate machine (nightmare) and SSHs into Triton for every operation: BeeLlama inference, file I/O, git commits, nvidia-smi queries, and Docker sandbox management. This creates several problems:

1. **SSH overhead:** Every API call incurs SSH connection latency (~50-100ms per command)
2. **Fragile transport:** SSH failures cascade — a dropped connection kills the entire pipeline
3. **SSH-to-self bugs:** When the engine runs directly on Triton (proven to work for code generation), `git_workflow.py` and `test_runner.py` still try to SSH to `<LAN_IP>`, causing connection loops
4. **Deployment friction:** Requires SSH key management, sshpass, and network connectivity between two machines
5. **No reproducibility:** Engine environment depends on whatever Python version and packages are installed on nightmare

The goal is to containerize the engine alongside BeeLlama on Triton, eliminating SSH entirely and creating a self-contained, reproducible deployment.

---

## 2. Approaches Evaluated

### Approach A: Simple Container with `network_mode: host`

```
Triton
├── Docker: beellama-kvarn      (network_mode: host, :8080)
├── Docker: beellama-3070        (network_mode: host, :8082)
├── Docker: gitea                (network_mode: host, :3000)
└── Docker: coder-engine         (network_mode: host)  ← NEW
    └── Volume: ~/projects → /workspace
```

| Aspect | Assessment |
|--------|-----------|
| **Pros** | Mirrors BeeLlama's proven pattern; engine reaches all services via `localhost:PORT`; no Docker networking complexity; simplest to implement |
| **Cons** | No network isolation between containers; engine has full host network access; can't bind conflicting ports |
| **Risk** | Low — `network_mode: host` is battle-tested on this Triton |
| **Image size** | Small — Python 3.12-slim + stdlib only (no CUDA) |

### Approach B: Sidecar per BeeLlama Container

Each BeeLlama container gets a companion engine sidecar sharing its network namespace.

| Aspect | Assessment |
|--------|-----------|
| **Pros** | Tight coupling between engine and its inference backend |
| **Cons** | Engine needs to talk to BOTH BeeLlama instances (8080 + 8082) and Gitea; sidecar pattern doesn't fit multi-service communication; duplicates engine per GPU |
| **Risk** | High — architectural mismatch |

### Approach C: Multi-Stage Dockerfile (builder + runtime)

```
FROM python:3.12-slim AS builder
# Install build deps, compile anything needed
FROM python:3.12-slim AS runtime
COPY --from=builder /app /app
```

| Aspect | Assessment |
|--------|-----------|
| **Pros** | Smaller final image; cleaner separation |
| **Cons** | Engine has zero compiled dependencies (pure stdlib + matplotlib); multi-stage adds complexity with no size benefit |
| **Risk** | Low but unnecessary |

### Approach D: Compose Profile Addition

Add `coder-engine` as a new service in the existing `tickets/deploy/docker-compose.yml`, sharing the same file as BeeLlama.

```yaml
services:
  # ... existing beellama services ...
  
  coder-engine:
    build:
      context: ../..
      dockerfile: Dockerfile.engine
    network_mode: host
    volumes:
      - /home/<user>/projects:/workspace:rw
      - /home/<user>/coder-harness-telemetry.db:/app/telemetry.db
    environment:
      - TRITON_HOST=localhost
      - TRITON_USER=<user>
      - GITEA_URL=http://localhost:3000
      - BEE_LLAMA_3090=http://localhost:8080
      - BEE_LLAMA_3070=http://localhost:8082
    profiles: ["engine"]
```

| Aspect | Assessment |
|--------|-----------|
| **Pros** | Unified deployment; single `docker compose --profile engine up -d`; shares .env with BeeLlama; lifecycle managed alongside BeeLlama |
| **Cons** | Coupled to BeeLlama compose file; changing BeeLlama config risks breaking engine; compose file grows |
| **Risk** | Low — additive change to existing compose |
| **Verdict** | ✅ **Best approach** — combines simplicity of A with operational discipline of D |

### Approach E: Engine on Host, Docker for Sandboxes Only

Keep the engine Python process running directly on Triton's host, but use Docker only for ephemeral code-gen sandboxes.

| Aspect | Assessment |
|--------|-----------|
| **Pros** | No container overhead for the engine itself; direct file system access |
| **Cons** | No reproducibility; depends on host Python; host environment pollution; harder to version-control the runtime |
| **Risk** | Medium — loses containerization benefits |

### Approach F: Dedicated Docker Network with Aliases

Create a custom bridge network so containers communicate by service name.

```yaml
networks:
  coder-net:
    driver: bridge

services:
  beellama-3090:
    networks: [coder-net]
    # ...
  coder-engine:
    networks: [coder-net]
    # reaches beellama at http://beellama-3090:8080
```

| Aspect | Assessment |
|--------|-----------|
| **Pros** | Clean DNS-based service discovery; proper isolation |
| **Cons** | **Breaks BeeLlama's `network_mode: host` pattern** — BeeLlama must listen on host ports for external access; bridge network adds NAT overhead; port mapping becomes complex |
| **Risk** | High — conflicts with existing BeeLlama architecture |

### Approach G: Hybrid — Engine Container + Host-mode Sandboxes

Engine runs in a container with `network_mode: host`. When it needs to spawn code-gen sandboxes, it launches them as sibling containers via Docker socket mount.

| Aspect | Assessment |
|--------|-----------|
| **Pros** | Engine is containerized; sandboxes are still ephemeral Docker containers; Docker socket gives full control |
| **Cons** | Requires Docker socket mount (`/var/run/docker.sock`); security surface increases |
| **Risk** | Medium — docker socket mount is powerful but well-understood |

---

## 3. Selected Approach: D + G Hybrid

**Primary:** Approach D (compose profile addition) with elements of G (Docker socket for sandbox management).

### Rationale

1. **Proven pattern:** `network_mode: host` is exactly what BeeLlama uses — it works on Triton today
2. **Zero SSH:** Engine runs on Triton, reaches BeeLlama/Gitea via `localhost` — no SSH transport needed
3. **Unified lifecycle:** `docker compose --profile engine up -d` starts everything; `docker compose down` stops everything
4. **No GPU needed:** Engine is pure Python — no CUDA base image, no GPU reservation
5. **Volume mount for code:** `~/projects` mounted read-write — engine edits code directly, no file transfer
6. **Reproducible:** Python 3.12-slim base image with pinned dependencies
7. **Hot-reload ready:** Source mounted as volume; `watchdog` or `entr` can trigger restarts on file change

### Architecture Diagram

```
Triton (<LAN_IP>, 20 cores, 32GB RAM, 2× GPU)
│
├── Docker: beellama-kvarn     ← network_mode: host, :8080, GPU 0
├── Docker: beellama-3070      ← network_mode: host, :8082, GPU 1
├── Docker: gitea              ← network_mode: host, :3000
│
└── Docker: coder-engine       ← network_mode: host, NO GPU
    ├── Volume: /home/<user>/projects → /workspace (rw)
    ├── Volume: /home/<user>/coder-harness-telemetry.db → /app/telemetry.db
    ├── Volume: /var/run/docker.sock → /var/run/docker.sock (for sandbox mgmt)
    ├── Env: BEE_LLAMA_3090=http://localhost:8080
    ├── Env: BEE_LLAMA_3070=http://localhost:8082
    ├── Env: GITEA_URL=http://localhost:3000
    └── Env: GITEA_TOKEN=<GITEA_TOKEN>

Network flow (all localhost, no SSH):
  coder-engine ──HTTP──→ localhost:8080  (BeeLlama 3090)
  coder-engine ──HTTP──→ localhost:8082  (BeeLlama 3070)
  coder-engine ──HTTP──→ localhost:3000  (Gitea API)
  coder-engine ──Docker──→ /var/run/docker.sock (sandbox creation)
  coder-engine ──RW──→ /workspace (project files)
```

---

## 4. Dockerfile

```dockerfile
# coder-harness engine — lightweight Python container
# No GPU needed; pure inference client + file operations + git

FROM python:3.12-slim AS runtime

# System deps: git (for workflow), curl (for healthchecks), openssh-client (optional fallback)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy engine source (will be overlaid by volume mount in dev)
COPY --chown=root:root coder-harness/ /app/

# Install optional Python deps (matplotlib for reports)
RUN pip install --no-cache-dir matplotlib

# Default environment — overridden by docker-compose
ENV TRITON_HOST=localhost
ENV TRITON_USER=<user>
ENV BEE_LLAMA_3090=http://localhost:8080
ENV BEE_LLAMA_3070=http://localhost:8082
ENV GITEA_URL=http://localhost:3000
ENV GITEA_TOKEN=""
ENV PYTHONUNBUFFERED=1

# Health check: verify Python is alive
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Entrypoint: default to the harness CLI
ENTRYPOINT ["python3", "harness.py"]
CMD ["status"]
```

### Image Size Estimate

| Layer | Size |
|-------|------|
| `python:3.12-slim` | ~150 MB |
| `git`, `curl`, `openssh-client` | ~30 MB |
| `matplotlib` | ~40 MB |
| Engine source (Python files) | ~1 MB |
| **Total** | **~220 MB** |

Compare: BeeLlama image is ~5 GB (CUDA runtime + build tools). The engine image is **23× smaller**.

---

## 5. DSH Containerization Gap (audited 2026-09-03)

The current `coder-engine` Docker is **Python-only** — it cannot run DSH agent loops, the `meta-cognitive-ralph-loop` skill, or any Node.js-based DSH functionality. DSH is fully built and working natively on Triton (Node.js 22, pnpm, all packages compiled, CLI binary at `apps/cli/lib/bin.js`) but has never been containerized. There is no `Dockerfile` in the deepseek-harness repository.

### What Exists on Triton

| Component | Status | Location |
|-----------|--------|----------|
| DSH source + build | ✅ Built | `/home/<user>/projects/deepseek-harness/` |
| Node.js 22 + pnpm 10 | ✅ Installed | System-wide |
| DSH CLI binary | ✅ Built | `apps/cli/lib/bin.js` |
| DSH Web frontend | ✅ Built | `apps/web/dist/` |
| DSH skill-filesystem | ✅ Built | `packages/skill/skill-filesystem/lib/` |
| DSH tool-ralph | ✅ Built | `packages/workflow/tool-ralph/lib/` |
| meta-cognitive-ralph-loop skill | ❌ Not installed | Not in any skill directory |
| `dsh` CLI on PATH | ❌ Not configured | `which dsh` returns nothing |

### Recommended: Hybrid Container (`dsh-engine:latest`)

```
┌─────────────────────────────────────────────┐
│  dsh-engine:latest (hybrid container)       │
│                                             │
│  Node.js 22 + Python 3.12                   │
│  DSH CLI + all packages + skills            │
│  Coder harness engine (Python)              │
│  meta-cognitive-ralph-loop skill            │
│  All 38+ skills bundled                     │
│                                             │
│  network_mode: host                         │
│  → localhost:8080 BeeLlama 3090             │
│  → localhost:8082 BeeLlama 3070             │
│  → localhost:3000 Gitea                     │
└─────────────────────────────────────────────┘
```

### Implementation Steps

1. **Create `Dockerfile.dsh`** — Multi-stage Node.js build (source → build → runtime)
2. **Install meta-cognitive-ralph-loop** — Extract SKILL.md, install to `~/.dsh/skills/`
3. **Bundle skills** — Copy all skills into `$DSH_BUNDLED_SKILL_DIR` in image
4. **Create `docker-compose.dsh.yml`** — Host networking, volume mounts, env vars
5. **Build and verify** — `docker build`, `dsh skill list`, BeeLlama health check
6. **Wire engine ↔ DSH** — DSH calls `harness.py` as a tool via subprocess

Full audit: `docs/dsh-docker-audit-report.md`

---

## 6. docker-compose Additions

Add to `tickets/deploy/docker-compose.yml`:

```yaml
services:

  # ==========================================================================
  #  Coder Engine — Autonomous coding engine
  #  No GPU needed; talks to BeeLlama via localhost
  # ==========================================================================
  coder-engine:
    container_name: coder-engine
    build:
      context: ../..           # parent of coder-harness/
      dockerfile: coder-harness/Dockerfile.engine
    image: coder-engine:latest
    network_mode: host
    restart: unless-stopped
    # NO GPU reservation — pure Python
    volumes:
      # Project code — read-write for file operations
      - /home/<user>/projects:/workspace:rw
      # Telemetry database — persistent across restarts
      - /home/<user>/coder-harness-telemetry.db:/app/telemetry.db
      # Docker socket — for sandbox lifecycle management
      - /var/run/docker.sock:/var/run/docker.sock
      # Git config — for commits
      - /home/<user>/.gitconfig:/root/.gitconfig:ro
      # SSH keys (optional fallback, read-only)
      - /home/<user>/.ssh:/root/.ssh:ro
    environment:
      - TRITON_HOST=localhost
      - TRITON_USER=<user>
      - BEE_LLAMA_3090=http://localhost:8080
      - BEE_LLAMA_3070=http://localhost:8082
      - GITEA_URL=http://localhost:3000
      - GITEA_TOKEN=<GITEA_TOKEN>
      - PYTHONUNBUFFERED=1
      - WORKSPACE=/workspace
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
    profiles:
      - engine
```

### Usage

```bash
cd ~/dockers/beellama-benchmark

# Start BeeLlama (as before)
docker compose --profile config-h up -d

# Start engine (new)
docker compose --profile engine up -d

# Or start both at once
docker compose --profile config-h --profile engine up -d

# View engine logs
docker compose --profile engine logs -f coder-engine

# Enter engine container
docker exec -it coder-engine bash

# Run a benchmark from inside the container
docker exec coder-engine python3 harness.py bench throughput -c 3090-qwen36-35b

# Stop engine
docker compose --profile engine down
```

---

## 6. Volume Mounts

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `/home/<user>/projects` | `/workspace` | rw | Project code — engine reads/writes files directly |
| `/home/<user>/coder-harness-telemetry.db` | `/app/telemetry.db` | rw | Persistent telemetry database |
| `/var/run/docker.sock` | `/var/run/docker.sock` | rw | Docker API access for sandbox creation/destruction |
| `/home/<user>/.gitconfig` | `/root/.gitconfig` | ro | Git identity for commits |
| `/home/<user>/.ssh` | `/root/.ssh` | ro | SSH keys (fallback for nightmare connectivity) |
| `/home/<user>/models` | `/models` | ro | Model files (for gpu_fit.py probe) |

### Important: Telemetry DB Path

The engine's `schema_unified.py` currently hardcodes `DB_PATH = os.path.expanduser("~/coder-harness-telemetry.db")`. Inside the container, `~` resolves to `/root`. We need to:

1. **Option A:** Set `HOME=/app` in the container env, so `~/coder-harness-telemetry.db` → `/app/coder-harness-telemetry.db`
2. **Option B:** Mount the host DB to `/root/coder-harness-telemetry.db`
3. **Option C:** Make `DB_PATH` configurable via env var (cleanest)

**Recommended: Option C** — add `DB_PATH` env var support:

```python
# schema_unified.py
DB_PATH = os.environ.get(
    "CODER_HARNESS_DB",
    os.path.expanduser("~/coder-harness-telemetry.db")
)
```

Then mount: `-v /home/<user>/coder-harness-telemetry.db:/data/telemetry.db`
And set: `CODER_HARNESS_DB=/data/telemetry.db`

---

## 7. Networking

### Current: SSH-Tunneled (nightmare → Triton)

```
nightmare ──SSH──→ <LAN_IP>
                    ├── sshpass → curl localhost:8080 (BeeLlama)
                    ├── sshpass → curl localhost:3000 (Gitea)
                    ├── sshpass → nvidia-smi
                    ├── sshpass → docker compose ...
                    └── sshpass → git commit/push
```

### Proposed: Direct localhost (engine on Triton)

```
coder-engine container (network_mode: host)
├── HTTP → localhost:8080 (BeeLlama 3090)
├── HTTP → localhost:8082 (BeeLlama 3070)
├── HTTP → localhost:3000 (Gitea API)
├── Docker socket → /var/run/docker.sock (sandbox mgmt)
├── nvidia-smi → direct exec (for GPU monitoring)
└── git → direct exec (for commits/pushes)
```

### What Changes in the Code

The engine needs a **transport abstraction layer** that detects whether it's running locally or remotely:

```python
# transport.py — NEW FILE

import os
import subprocess
import json
import urllib.request

RUNNING_IN_DOCKER = os.path.exists("/.dockerenv")
TRITON_HOST = os.environ.get("TRITON_HOST", "localhost")
BEE_LLAMA_3090 = os.environ.get("BEE_LLAMA_3090", "http://localhost:8080")
BEE_LLAMA_3070 = os.environ.get("BEE_LLAMA_3070", "http://localhost:8082")
GITEA_URL = os.environ.get("GITEA_URL", "http://localhost:3000")

def curl_beellama(port, messages, max_tokens=512, temperature=0.3, **kwargs):
    """Send inference request. Uses direct HTTP when local, SSH when remote."""
    url = BEE_LLAMA_3090 if port == 8080 else BEE_LLAMA_3070
    
    if RUNNING_IN_DOCKER:
        # Direct HTTP — no SSH
        return _direct_http(url, messages, max_tokens, temperature, **kwargs)
    else:
        # SSH tunnel — current behavior
        return _ssh_curl(port, messages, max_tokens, temperature, **kwargs)

def run_command(cmd, timeout=30):
    """Execute a command. Local when in Docker, SSH when remote."""
    if RUNNING_IN_DOCKER:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode
    else:
        return _ssh_run(cmd, timeout)
```

### Modules That Need Transport Refactoring

| Module | Current SSH Pattern | Local Alternative |
|--------|-------------------|-------------------|
| `ssh_utils.py` | `sshpass ssh <user>@host curl ...` | `urllib.request.urlopen(...)` |
| `git_workflow.py` | `ssh user@host "cd path && git ..."` | `subprocess.run(["git", ...])` locally |
| `test_runner.py` | `ssh user@host "python3 -m pytest ..."` | `subprocess.run(["python3", ...])` locally |
| `gitea_utils.py` | `ssh user@host "curl localhost:3000/..."` | `urllib.request.urlopen("http://localhost:3000/...")` |
| `sandbox_manager.py` | `ssh user@host "docker ..."` | `subprocess.run(["docker", ...])` locally |
| `file_ops.py` | `ssh user@host "cat/ls/mkdir ..."` | Direct file I/O |
| `preflight.py` | `SSHClient.connect()` | Check `localhost:8080/health` directly |
| `remote_control.py` | `subprocess.run(["ssh", ...])` | Direct subprocess calls |

---

## 8. Development Workflow

### Hot-Reload Development

The engine source is volume-mounted, so code changes are instant:

```bash
# On Triton — start with live reload
docker compose --profile engine up -d

# Edit code on host
vim ~/projects/coder-harness/code_generator.py

# Changes are immediately visible inside the container
docker exec coder-engine python3 harness.py status
```

For automatic restarts on code change:

```bash
# Install watchdog inside container (already in Dockerfile)
docker exec coder-engine pip install watchdog

# Run with file watching
docker exec -d coder-engine \
    watchmedo auto-restart --directory=/app --pattern="*.py" \
    -- python3 harness.py status
```

### Iterative Development Cycle

```bash
# 1. Edit code on Triton host
vim ~/projects/coder-harness/ssh_utils.py

# 2. Test inside container
docker exec coder-engine python3 -c "from ssh_utils import SSHClient; print('OK')"

# 3. Run full integration test
docker exec coder-engine python3 test_integration.py

# 4. Run a real benchmark
docker exec coder-engine python3 harness.py bench throughput -c 3090-qwen35-9b

# 5. Check logs
docker compose --profile engine logs --tail=50 coder-engine
```

### Testing the SSH-to-Local Transition

```bash
# Verify the engine can reach BeeLlama directly
docker exec coder-engine curl -s http://localhost:8080/health
docker exec coder-engine curl -s http://localhost:8082/health

# Verify the engine can reach Gitea directly
docker exec coder-engine curl -s http://localhost:3000/api/v1/repos/search \
    -H "Authorization: token <GITEA_TOKEN>"

# Verify file operations
docker exec coder-engine ls -la /workspace/coder-harness/

# Verify git operations
docker exec coder-engine git -C /workspace/coder-harness status

# Verify Docker socket access
docker exec coder-engine docker ps
```

---

## 9. Migration Steps

### Phase 1: Prepare (no breaking changes)

1. **Add `DB_PATH` env var support** to `schema_unified.py`
2. **Create `transport.py`** with the local/remote detection logic
3. **Create `Dockerfile.engine`** in `coder-harness/`
4. **Test the Dockerfile builds** on Triton

### Phase 2: Refactor Modules (one at a time)

5. **Refactor `gitea_utils.py`** — replace SSH tunnel with direct HTTP calls
6. **Refactor `git_workflow.py`** — replace SSH git commands with local subprocess calls
7. **Refactor `test_runner.py`** — replace SSH pytest runs with local subprocess calls
8. **Refactor `sandbox_manager.py`** — replace SSH docker commands with local subprocess calls
9. **Refactor `file_ops.py`** — replace SSH file operations with direct I/O
10. **Refactor `preflight.py`** — add direct HTTP health checks alongside SSH
11. **Refactor `remote_control.py`** — add local execution path

Each module should detect `RUNNING_IN_DOCKER` and use the appropriate transport. SSH transport remains as a fallback for nightmare → Triton use.

### Phase 3: Deploy

12. **Add `coder-engine` service** to `docker-compose.yml`
13. **Build the image** on Triton: `docker compose --profile engine build`
14. **Start the engine**: `docker compose --profile engine up -d`
15. **Verify connectivity**: health checks against BeeLlama and Gitea
16. **Run a smoke test**: `docker exec coder-engine python3 harness.py health`

### Phase 4: Validate

17. **Run the full benchmark pipeline** from inside the container
18. **Compare results** with SSH-based runs (should be identical, faster)
19. **Verify telemetry** persists across container restarts
20. **Test crash recovery** — kill container, restart, resume from checkpoint

### Rollback Plan

If anything breaks, simply stop the engine container and continue using the SSH-based workflow from nightmare:

```bash
docker compose --profile engine down
# SSH-based workflow from nightmare continues working unchanged
```

---

## 10. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Docker socket mount = root access | Engine container runs as root inside Docker (standard pattern); restrict to `docker` group on host |
| Gitea token in environment | Use Docker secrets or `.env` file (already gitignored); never commit tokens |
| No network isolation | Acceptable because all services are on localhost and not exposed to external network |
| SSH keys in volume mount | Read-only mount; keys only used as fallback for nightmare connectivity |

---

## 11. Resource Budget

| Resource | BeeLlama (3090) | BeeLlama (3070) | Gitea | Engine | Total |
|----------|----------------|----------------|-------|--------|-------|
| CPU cores | 8 | 4 | 1 | 2 | 15 / 20 ✅ |
| RAM | 12 GB | 4 GB | 512 MB | 1 GB | 17.5 / 32 GB ✅ |
| GPU | RTX 3090 | RTX 3070 | — | — | 2 / 2 ✅ |
| Disk | — | — | — | 220 MB image | Negligible |

The engine consumes ~1 GB RAM and 2 CPU cores — minimal compared to BeeLlama's GPU-bound workload.

---

## 12. Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `coder-harness/Dockerfile.engine` | Container image definition |
| `coder-harness/transport.py` | Local/remote transport abstraction |

### Modified Files

| File | Change |
|------|--------|
| `coder-harness/schema_unified.py` | Add `CODER_HARNESS_DB` env var support |
| `coder-harness/tickets/deploy/docker-compose.yml` | Add `coder-engine` service |
| `coder-harness/gitea_utils.py` | Add direct HTTP path (no SSH) |
| `coder-harness/git_workflow.py` | Add local subprocess path (no SSH) |
| `coder-harness/test_runner.py` | Add local subprocess path (no SSH) |
| `coder-harness/sandbox_manager.py` | Add local Docker API path (no SSH) |
| `coder-harness/file_ops.py` | Add direct file I/O path (no SSH) |
| `coder-harness/preflight.py` | Add direct HTTP health checks |
| `coder-harness/remote_control.py` | Add local execution path |

---

## 13. Success Criteria

- [ ] `docker compose --profile engine up -d` starts the engine on Triton
- [ ] Engine reaches BeeLlama on `localhost:8080` and `localhost:8082` without SSH
- [ ] Engine reaches Gitea on `localhost:3000` without SSH
- [ ] Engine can create/destroy Docker sandboxes via socket mount
- [ ] Engine can read/write project files via volume mount
- [ ] `python3 harness.py health` passes all 7 checks from inside the container
- [ ] A full benchmark run completes faster than the SSH-based equivalent
- [ ] Telemetry database persists across container restarts
- [ ] Image size is under 300 MB

---

## 14. Engine Control Layer Architecture

The Engine Control Layer introduces a three-tier approach for engine control, enabling remote operations without SSH by providing an HTTP API service on port 3082.

### Three-Tier Architecture

```
Tier 1: Fix SSH (baseline)          Tier 2: HTTP API Service           Tier 3: Native Triton
├── sshpass install                 ├── engine_service.py (:3082)       ├── DSH runs on Triton
├── SSH key authentication          ├── FastAPI endpoints               ├── No remote comms needed
├── ControlMaster multiplexing      ├── /health, /inference, /exec     ├── Direct localhost access
└── Works everywhere today          └── /write-files, /run-tests       └── Optimal performance
                                    ├── /git-commit, /gpu-status
                                    └── /stream (SSE)
```

**Transport priority:**
```
get_transport() → auto-detect:
  1. HTTPTransport  → engine_service.py on port 3082 (preferred)
  2. LocalTransport → direct subprocess/HTTP (Docker on Triton)
  3. RemoteTransport → SSH tunneling (nightmare → Triton fallback)
```

### Components

| Component | File | Lines | Description |
|-----------|------|-------|-------------|
| HTTP API Service | `engine_service.py` | ~333 | FastAPI server on port 3082 with 8 endpoints |
| HTTP Client | `engine_client.py` | ~150 | Retry-capable client mirroring LocalTransport API |
| Three-Transport | `transport.py` | ~550 | HTTPTransport (primary), LocalTransport, RemoteTransport |
| Systemd Unit | `engine-service.service` | — | Auto-restart on failure, starts on boot |

### Engine Service Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check — returns `{"status": "ok"}` |
| `/inference` | POST | Proxy inference request to BeeLlama |
| `/write-files` | POST | Write files to project directory on Triton |
| `/run-tests` | POST | Execute test suite for a project |
| `/git-commit` | POST | Create git commit with specified files |
| `/exec` | POST | Execute arbitrary shell command |
| `/gpu-status` | GET | Query nvidia-smi for GPU stats |
| `/stream` | GET | SSE stream for real-time events |

### Network Flow

```
nightmare (dev machine)
│
├── DSH Agent ──→ harness.py ──→ transport.py ──→ get_transport()
│                                              │
│                        ┌─────────────────────┤
│                        ▼                     ▼
│                  HTTPTransport          RemoteTransport
│                  (Tier 2)               (Tier 1)
│                   │                         │
│                   │ HTTP                    │ SSH
│                   ▼                         ▼
│
└── Triton (<LAN_IP>)
    │
    ├── engine_service.py (:3082)    ←── HTTP from nightmare
    │   ├── /inference → BeeLlama (:8080/:8082)
    │   ├── /write-files → /home/<user>/projects/
    │   ├── /run-tests → pytest
    │   ├── /git-commit → git
    │   ├── /exec → subprocess
    │   └── /gpu-status → nvidia-smi
    │
    └── OR (Tier 3 - Native):
        DSH runs directly on Triton → no remote comms needed
        LocalTransport → direct subprocess + HTTP localhost
```

### Systemd Deployment

```bash
# Install service
sudo cp engine-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable engine-service

# Start/stop/status
sudo systemctl start engine-service
sudo systemctl stop engine-service
sudo systemctl status engine-service

# Or via harness.py CLI
python3 harness.py engine start [--port 3082] [--token <secret>]
python3 harness.py engine stop
python3 harness.py engine status
```

### Transport Selection

Set `TRANSPORT_MODE` env var to force a specific transport:
- `http` → always use HTTPTransport (engine_service.py)
- `local` → always use LocalTransport (Docker on Triton)
- `ssh` → always use RemoteTransport (SSH tunneling)
- `auto` → auto-detect (default)

```bash
# Force HTTP transport
TRANSPORT_MODE=http python3 harness.py status

# Force SSH transport
TRANSPORT_MODE=ssh python3 harness.py status

# Auto-detect (default)
python3 harness.py status
```
