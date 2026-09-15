# Issues: Transport Layer Integration

## Design Analysis

### Current State
4 modules have SSH dependencies that need to be replaced with the `transport.py` abstraction:

| Module | SSH Pattern | # Call Sites | Complexity |
|--------|-------------|-------------|------------|
| `code_generator.py` | `self._ssh()` / `self._ssh_stdin()` | 8 | Medium — has `_is_local()` already |
| `test_runner.py` | `self._ssh()` via `_run_ssh()` | 8 | Medium — standalone functions |
| `git_workflow.py` | `self._ssh_exec()` | 12 | Low — single method, all git ops |
| `context_manager.py` | `os.walk()` / `open()` | 4 | Low — already local, no SSH |

### Seam Location
The seam is at `transport.py:get_transport()` → returns `LocalTransport` or `RemoteTransport`. All 4 modules should accept a `transport` parameter in their constructor and use it instead of SSH.

### Interface Contract
```python
transport = get_transport()  # Auto-detects local vs remote
# Then use:
transport.curl_beellama(port, messages, max_tokens)
transport.run_command(cmd, timeout, cwd)
transport.run_git(repo_path, *args)
transport.check_beellama_health(port)
```

## Issue 1: Integrate transport.py into code_generator.py

### What to build
Replace all `self._ssh()` and `self._ssh_stdin()` calls in `CodeGenerator` with `transport.run_command()`. Remove the `_is_local()` port-check hack. Accept `transport` as constructor parameter.

### Acceptance criteria
- [ ] `CodeGenerator.__init__` accepts optional `transport` parameter
- [ ] If no transport provided, calls `get_transport()` automatically
- [ ] `_ssh()` method delegates to `transport.run_command()`
- [ ] `_ssh_stdin()` method delegates to `transport.run_command()` with input
- [ ] BeeLlama curl calls use `transport.curl_beellama()` instead of raw SSH curl
- [ ] File write operations use `transport.run_command()` with `cat > file`
- [ ] `TRITON_HOST` and `TRITON_USER` constants removed (transport handles this)
- [ ] All 62 existing tests still pass
- [ ] New test: verify LocalTransport is used when `/.dockerenv` exists

### Blocked by
None — can start immediately.

## Issue 2: Integrate transport.py into test_runner.py

### What to build
Replace `self._ssh()` (which calls `_run_ssh()`) with `transport.run_command()`. Remove standalone `_ssh_cmd()` and `_run_ssh()` functions.

### Acceptance criteria
- [ ] `TestRunner.__init__` accepts optional `transport` parameter
- [ ] `_ssh()` method delegates to `transport.run_command()`
- [ ] Standalone `_ssh_cmd()` and `_run_ssh()` functions removed
- [ ] `TRITON_HOST` and `TRITON_USER` constants removed
- [ ] All 62 existing tests still pass
- [ ] New test: verify `run()` method works with both transports

### Blocked by
None — can start immediately.

## Issue 3: Integrate transport.py into git_workflow.py

### What to build
Replace `self._ssh_exec()` with `transport.run_git()` for git operations and `transport.run_command()` for non-git commands.

### Acceptance criteria
- [ ] `GitWorkflow.__init__` accepts optional `transport` parameter
- [ ] `_ssh_exec()` delegates to `transport.run_command()`
- [ ] Git-specific operations use `transport.run_git()` where appropriate
- [ ] All 62 existing tests still pass
- [ ] New test: verify git commit works with LocalTransport

### Blocked by
None — can start immediately.

## Issue 4: Integrate transport.py into context_manager.py

### What to build
ContextManager already uses `os.walk()` and `open()` for local file access. Verify it works correctly in both local (Docker) and remote (nightmare) contexts. The project path is volume-mounted in Docker, so `os.walk()` works in both cases.

### Acceptance criteria
- [ ] `ContextManager.__init__` accepts optional `transport` parameter
- [ ] File discovery uses `transport.run_command()` for remote, `os.walk()` for local
- [ ] File reading uses `transport.run_command()` for remote, `open()` for local
- [ ] All 62 existing tests still pass
- [ ] New test: verify context building works in Docker environment

### Blocked by
None — can start immediately.

## Issue 5: Run streaming PRD inside Docker container

### What to build
Execute the streaming MVP PRD (`prd-streaming-mvp.md`) inside the `coder-engine` Docker container on Triton. This validates the full pipeline: PRD parsing → code generation → quality gates → git commit.

### Acceptance criteria
- [ ] Engine container can reach BeeLlama (verified: `beellama_8080: True`)
- [ ] Engine container can reach Gitea (verified: `localhost:3000`)
- [ ] PRD is parsed into tasks (3 tasks from prd-streaming-mvp.md)
- [ ] Code is generated for streaming_server.py, streaming_client.py, dashboard/
- [ ] Quality gates pass on generated code
- [ ] Generated code is committed to Gitea
- [ ] At least 1 generated file is syntactically valid Python

### Blocked by
Issues 1-4 (transport integration) should complete first, but can run in parallel since the Docker container already has `transport.py` working for `code_generator.py`.

## Issue 6: End-to-end validation

### What to build
Verify the complete pipeline works: build Docker → deploy → run PRD → generated code works.

### Acceptance criteria
- [ ] `docker build -f Dockerfile.engine -t coder-engine:latest` succeeds
- [ ] `docker compose -f docker-compose.engine.yml --profile engine up -d` starts
- [ ] Engine container health check passes
- [ ] `docker exec coder-engine python3 harness.py ace run ...` completes
- [ ] Generated `streaming_server.py` starts and responds to HTTP requests
- [ ] Generated `dashboard/index.html` loads in browser

### Blocked by
Issues 1-5.

---

## Issue 7: Create DSH hybrid Docker image (Node.js + Python)

### What to build
Build a `dsh-engine:latest` Docker image that includes both DSH (Node.js 22) and the coder-harness engine (Python 3.12). This enables DSH to run the engine as a subprocess, use the `meta-cognitive-ralph-loop` skill, and access all DSH functionality inside the container.

### Current state (audited 2026-09-03)
- DSH is fully built natively on Triton (Node.js 22, pnpm, all packages compiled)
- No `Dockerfile` exists in the deepseek-harness repository
- `coder-engine:latest` is Python-only (134 MB), cannot run DSH
- DSH node_modules = 857 MB, source = 242 MB (needs multi-stage build)

### Acceptance criteria
- [ ] `Dockerfile.dsh` created in deepseek-harness repo (multi-stage: build → runtime)
- [ ] `docker build -f Dockerfile.dsh -t dsh-engine:latest` succeeds on Triton
- [ ] `docker run --rm --network host dsh-engine:latest node --version` returns v22.x
- [ ] `docker run --rm --network host dsh-engine:latest python3 --version` returns 3.12.x
- [ ] `docker run --rm --network host dsh-engine:latest node apps/cli/lib/bin.js --version` works
- [ ] `docker run --rm --network host dsh-engine:latest python3 harness.py status` works
- [ ] Image size < 800 MB (multi-stage excludes dev deps, tests, source)
- [ ] BeeLlama reachable from container: `curl -sf http://localhost:8080/health`

### Implementation approach
Multi-stage build:
1. **Stage 1 (builder):** `node:22-slim` + pnpm → `pnpm install` + `pnpm run build`
2. **Stage 2 (python-builder):** `python:3.12-slim` → copy coder-harness source
3. **Stage 3 (runtime):** `node:22-slim` → copy built DSH + node_modules from stage 1, copy Python + engine from stage 2
4. Bundle skills into `$DSH_BUNDLED_SKILL_DIR`
5. Entry point: custom script that starts DSH or engine based on args

### Blocked by
None — can start immediately.

## Issue 8: Install meta-cognitive-ralph-loop skill on Triton

### What to build
Install the `meta-cognitive-ralph-loop` skill into DSH's skill filesystem on Triton so DSH can load and use it.

### Current state
- The skill exists in DSH's available_skills catalog (loaded from nightmare's DSH)
- It is NOT installed in any skill directory on Triton
- DSH skill-filesystem loads from: `<project>/.dsh/skills/`, `<project>/.agents/skills/`, `~/.dsh/skills/`, `~/.agents/skills/`, `$DSH_BUNDLED_SKILL_DIR`

### Acceptance criteria
- [ ] `meta-cognitive-ralph-loop/SKILL.md` exists in one of the DSH skill roots on Triton
- [ ] `dsh skill list` (or equivalent) shows `meta-cognitive-ralph-loop` as available
- [ ] The skill is also bundled into the `dsh-engine:latest` Docker image

### Blocked by
Issue 7 (Docker image) for the Docker-bundled path. Can install natively on Triton immediately.

## Issue 9: Wire DSH to engine inside Docker

### What to build
Configure DSH inside the hybrid container to invoke the coder-harness engine as a tool. DSH should be able to:
1. Call `python3 harness.py ace run --project <path> <prd.md>` as a subprocess
2. Stream engine output back through DSH's event system
3. Use the `meta-cognitive-ralph-loop` skill to orchestrate autonomous development

### Acceptance criteria
- [ ] DSH agent preset configured with engine tool mapping
- [ ] `harness.py` is on PATH inside the container (or absolute path works)
- [ ] DSH can execute a simple engine command and get structured output
- [ ] Engine events flow to DSH's session telemetry

### Blocked by
Issues 7-8.
