# DSH Docker Audit Report

**Date:** 2026-09-03  
**Auditor:** MiMo (automated)  
**Target:** Install `/meta-cognitive-ralph-loop` + skills package into DSH Docker on Triton

---

## 1. Current State — What Exists

### 1.1 DSH on Triton (native, NOT in Docker)

| Component | Status | Location |
|-----------|--------|----------|
| DSH source | ✅ Built | `/home/<user>/projects/deepseek-harness/` |
| Node.js | ✅ v22.23.2 | `/usr/bin/node` |
| pnpm | ✅ v10.34.5 | `/usr/bin/pnpm` |
| DSH CLI binary | ✅ Built | `apps/cli/lib/bin.js` |
| DSH Web frontend | ✅ Built | `apps/web/dist/` |
| DSH node_modules | ✅ Installed (857 MB) | `node_modules/` |
| DSH skill-filesystem | ✅ Built | `packages/skill/skill-filesystem/lib/` |
| DSH tool-ralph | ✅ Built | `packages/workflow/tool-ralph/lib/` |
| DSH settings | ✅ Configured | `~/.dsh/settings.yaml` |
| DSH ACP agent profile | ✅ Configured | `~/.dsh/profiles/acp-agent/` |
| `dsh` CLI command | ❌ NOT on PATH | `which dsh` returns nothing |

**Key finding:** DSH is fully built and working on Triton natively, but there is **no Dockerfile** and **no Docker image** for DSH itself.

### 1.2 Coder Engine Docker (exists, minimal)

| Component | Status | Details |
|-----------|--------|---------|
| `Dockerfile.engine` | ✅ Exists | Python 3.12-slim, 155 MB, no GPU |
| `docker-compose.engine.yml` | ✅ Exists | host networking, volume mounts |
| `coder-engine:latest` | ✅ Built | 8 layers, 134 MB |
| Container | ⚠️ Exited | Last ran 2 hours ago |

**The engine Docker is a pure Python container** — it has no DSH, no Node.js, no skills, no ralph loop. It can only run `harness.py` commands via BeeLlama API calls.

### 1.3 BeeLlama + Gitea (running, healthy)

| Service | Port | Status |
|---------|------|--------|
| BeeLlama 3090 (config-i) | 8080 | ✅ `{"status":"ok"}` |
| BeeLlama 3070 | 8082 | ✅ `{"status":"ok"}` |
| Gitea | 3000 | ✅ v1.27.3 |

### 1.4 Skills on Triton

| Skill Source | Count | Location |
|-------------|-------|----------|
| DSH `.agents/skills/` | 11 | `deepseek-harness/.agents/skills/` |
| DSH cordis preset skills | 2 | `apps/cli/config/agent-presets/cordis/skills/` |
| Grok installed skills | 38 | `~/.grok/installed-plugins/skills-bce86e95/skills/` |
| **meta-cognitive-ralph-loop** | ❌ NOT FOUND | Not in any skill directory on Triton |

**Critical finding:** The `meta-cognitive-ralph-loop` skill does not exist on Triton. It's listed in this session's available_skills catalog (loaded from nightmare's DSH), but it was never installed on the Triton machine.

---

## 2. Gap Analysis — What's Missing

### 2.1 Missing: DSH Docker Image

DSH has **no Dockerfile anywhere** in its repository. To run DSH inside Docker on Triton, we need to create:

1. **`Dockerfile.dsh`** — Multi-stage build:
   - Stage 1: Node.js 22 + pnpm → build DSH from source
   - Stage 2: Node.js 22 runtime → copy built artifacts + node_modules
   - Include: CLI, web frontend, all packages, skill-filesystem, tool-ralph
   - Exclude: source TypeScript, tests, build scripts (keep image small)

2. **`docker-compose.dsh.yml`** — DSH service definition:
   - `network_mode: host` (access BeeLlama on localhost:8080/8082, Gitea on localhost:3000)
   - Volume mounts: `~/.dsh/` (settings), `~/projects/` (workspace), Docker socket
   - Environment: API keys, DSH config paths
   - Entry point: `dsh` CLI or `node apps/cli/lib/bin.ts`

### 2.2 Missing: meta-cognitive-ralph-loop Skill

This skill needs to be:
1. **Sourced** — It exists in the DSH skill catalog (this session has it). We need the actual `SKILL.md` file content.
2. **Installed** on Triton in one of:
   - `~/.dsh/skills/meta-cognitive-ralph-loop/SKILL.md` (user-level)
   - `/home/<user>/projects/deepseek-harness/.agents/skills/meta-cognitive-ralph-loop/SKILL.md` (project-level)
   - Bundled into the Docker image at `$DSH_BUNDLED_SKILL_DIR`

### 2.3 Missing: Skills Package Integration

The DSH skill-filesystem provider loads skills from these roots (in priority order):
1. `<project>/.dsh/skills/` (rank 100)
2. `<project>/.agents/skills/` (rank 200)
3. Custom dirs (rank 300)
4. `~/.dsh/skills/` (rank 400)
5. `~/.agents/skills/` (rank 500)
6. `$DSH_BUNDLED_SKILL_DIR` (bundled rank)

For a Docker build, the cleanest approach is **bundled skills** — copy the skill files into the image and set `DSH_BUNDLED_SKILL_DIR`.

### 2.4 Missing: Engine ↔ DSH Integration

Currently the coder-engine Docker and DSH are completely separate:
- Engine Docker: Python only, runs `harness.py`, talks to BeeLlama via HTTP
- DSH (native): Node.js, runs agent loops, has skills, talks to LLMs via API

The streaming PRD envisions DSH watching the engine, but there's no bridge. Options:
1. **DSH runs the engine** — DSH Docker includes both Node.js (DSH) and Python (engine), DSH calls `harness.py` as a tool
2. **Engine emits events, DSH consumes** — Engine POSTs to streaming server, DSH subscribes via SSE
3. **Both in one container** — Hybrid image with Node.js + Python + all skills

---

## 3. Recommended Architecture

### Option A: Single Hybrid Container (recommended)

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

**Pros:** Single container, DSH can directly invoke engine, no SSH, no network bridging
**Cons:** Larger image (~500 MB), two runtimes

### Option B: Two Containers, Shared Network

```
┌──────────────────┐    ┌──────────────────┐
│  dsh:latest      │    │  coder-engine:   │
│  Node.js + skills│    │  latest          │
│  DSH CLI         │    │  Python only     │
│  port 3080 (web) │    │  harness.py      │
└────────┬─────────┘    └────────┬─────────┘
         │   network_mode: host  │
         └───────────────────────┘
              ↓ localhost
         BeeLlama (8080/8082)
         Gitea (3000)
```

**Pros:** Clean separation, smaller images, independent updates
**Cons:** Two containers to manage, DSH can't directly call engine subprocess

---

## 4. Implementation Plan

### Phase 1: Source the meta-cognitive-ralph-loop Skill

1. Extract the `SKILL.md` content from this session's available_skills catalog
2. Create the skill directory on Triton: `~/.dsh/skills/meta-cognitive-ralph-loop/SKILL.md`
3. Verify DSH can load it: `dsh skill list | grep meta-cognitive`

### Phase 2: Create DSH Dockerfile

1. Write `Dockerfile.dsh` in `/home/<user>/projects/deepseek-harness/`:
   - Base: `node:22-slim`
   - Install pnpm, copy workspace, build
   - Copy built artifacts to runtime stage
   - Bundle skills into `$DSH_BUNDLED_SKILL_DIR`
   - Set entry point to `dsh` CLI

2. Write `docker-compose.dsh.yml`:
   - `network_mode: host`
   - Volume mounts for settings, projects, Docker socket
   - Environment variables for API keys

### Phase 3: Build and Verify

1. `docker build -f Dockerfile.dsh -t dsh:latest .` on Triton
2. Verify: `docker run --rm --network host dsh:latest dsh --version`
3. Verify: `docker run --rm --network host dsh:latest dsh skill list`
4. Verify: `docker run --rm --network host dsh:latest curl -sf http://localhost:8080/health`

### Phase 4: Update Coder Engine Docker

1. Update `Dockerfile.engine` to include DSH skill integration
2. Or merge into hybrid container (Option A)
3. Update `docker-compose.engine.yml` with DSH volumes and env

### Phase 5: Update Documentation

1. Update `AGENTS.md` with Docker architecture
2. Update `docs/docker-architecture.md` with DSH container
3. Update `issues-transport-integration.md` with streaming readiness

---

## 5. Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| DSH build fails in Docker | High | Test build locally first, use multi-stage to isolate |
| Skills don't load in container | Medium | Verify `DSH_BUNDLED_SKILL_DIR` env, test with `dsh skill list` |
| Node.js + Python hybrid too large | Low | Multi-stage build, exclude dev deps, ~500 MB acceptable |
| BeeLlama unreachable from container | Low | `network_mode: host` eliminates this |
| Gitea token exposure in image | Medium | Use env vars, not baked-in values |

---

## 6. Readiness Verdict

| Component | Ready? | Blocker |
|-----------|--------|---------|
| DSH source on Triton | ✅ Yes | — |
| DSH built on Triton | ✅ Yes | — |
| DSH Dockerfile | ❌ No | Needs creation |
| DSH Docker image | ❌ No | Needs build |
| meta-cognitive-ralph-loop skill | ❌ No | Not installed on Triton |
| Skills package in Docker | ❌ No | Needs bundling |
| Engine ↔ DSH bridge | ❌ No | Needs integration |
| Streaming server | ❌ No | Only 42-line stub |

**Bottom line:** DSH is fully built and working natively on Triton, but has never been containerized. The `meta-cognitive-ralph-loop` skill exists in the DSH ecosystem but was never installed on Triton. To "put the engine in a docker and run the docker on host" with full DSH capabilities, we need to:

1. Create a DSH Dockerfile (new)
2. Install the ralph-loop skill (new)
3. Build the Docker image (new)
4. Wire DSH to the engine (new)

**Estimated effort:** 2-3 hours for a working hybrid container, 4-6 hours for a polished two-container setup.
