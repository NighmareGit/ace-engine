# Remote Control System

Complete guide for the coder-harness remote control infrastructure.

---

## Architecture

```
nightmare (DSH)                    Triton (<LAN_IP>)
├─ harness.py ──────SSH────────── ├─ BeeLlama (port 8080/8082)
├─ remote_control.py ─SSH──────── ├─ Gitea (port 3000)
├─ work_engine.py ───SSH───────── ├─ Docker sandboxes
├─ sandbox_manager.py ─SSH─────── └─ ACP server (stdio)
├─ gitea_utils.py ───SSH─────────
└─ telemetry_collector.py ─SSH────
```

All local Python modules connect to the remote Triton host over SSH. The remote host runs the inference engine (BeeLlama), the code forge (Gitea), isolated Docker sandboxes for code execution, and an ACP server that coordinates work via stdio.

### SSH Transport

All SSH connections use key-based auth (preferred) with password fallback:

```bash
# Key-based (preferred):
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> {cmd}

# Password fallback (requires sshpass):
sshpass -p {pw} ssh -o StrictHostKeyChecking=no <user>@<LAN_IP> {cmd}
```

**Constants:** `TRITON_HOST=<LAN_IP>`, `TRITON_USER=<user>`, `PORT_3090=8080`, `PORT_3070=8082`

---

## Quick Reference

All commands go through `harness.py` — a thin CLI router that delegates every subcommand to the owning module. No logic duplication, just dispatch with lazy imports.

### Global Flags

| Flag | Description |
|------|-------------|
| `--pretty` | Pretty-print JSON output |
| `--verbose`, `-v` | Enable debug logging |
| `--version` | Show version |

### Full Command Reference

| Command | Delegates To | Description |
|---------|-------------|-------------|
| `status` | `remote_control.py` | Full system status (model, GPU, sandboxes) |
| `health` | `preflight.py` | Quick 7-point preflight health check |
| `setup` | *(built-in)* | Print first-time setup guide |
| `bench throughput -c <id>` | `remote_control.py` | Throughput benchmark (tok/s) |
| `bench quality -c <id>` | `remote_control.py` | Quality benchmark (scoring) |
| `bench all --configs <csv>` | `remote_control.py` | Benchmarks across multiple configs |
| `bench orchestrate --phase <0\|1>` | `orchestrate.py` | Full orchestrator pipeline |
| `bench pilot` | `pilot.py` | Methodology pilot (45 runs) |
| `run -p <project> -t <task>` | `work_engine.py` | Execute a coding task (full 7-step pipeline) |
| `sessions` | `work_engine.py` | List work sessions |
| `collect --name <session>` | `work_engine.py` | Collect results from a session |
| `model status` | `remote_control.py` | Current model + GPU status |
| `model swap <config>` | `remote_control.py` | Swap to a different model config |
| `model list` | `remote_control.py` | List all available model configs |
| `sandbox create -p <project>` | `remote_control.py` | Create a Docker sandbox |
| `sandbox list` | `sandbox_manager.py` | List active harness sandboxes |
| `sandbox destroy -n <name>` | `remote_control.py` | Destroy a sandbox |
| `sandbox cleanup` | `sandbox_manager.py` | Destroy all harness sandboxes |
| `gitea repos` | `gitea_utils.py` | List all repositories |
| `gitea branches <repo>` | `gitea_utils.py` | List branches in a repo |
| `gitea files <repo>` | `gitea_utils.py` | List files in a repo path |
| `dashboard` | `telemetry_dashboard.py` | Generate HTML telemetry dashboard |
| `telemetry export` | `remote_control.py` | Export telemetry to JSON |
| `report --format md\|json` | `report.py` | Generate benchmark report |

### Examples

```bash
python3 harness.py status                          # System health
python3 harness.py health                          # Quick 7-point check
python3 harness.py bench throughput -c 3090-qwen36-35b
python3 harness.py run -p coder-harness -t "Fix bug X" -c 3090-qwen36-35b
python3 harness.py model swap config-i
python3 harness.py sandbox create -p coder-harness -n test-01
python3 harness.py gitea repos
python3 harness.py dashboard
python3 harness.py report --format json
```

---

## Module Deep Dive

### `remote_control.py` — Unified Controller (1088 lines)

**Class:** `RemoteControl`

The full-featured controller for the Triton remote coding engine. Combines SSH, Docker, BeeLlama API, and Gitea operations into one interface.

**Command groups:**

| Group | Actions | Description |
|-------|---------|-------------|
| `model` | `status`, `list`, `swap <config>` | Model management |
| `bench` | `throughput`, `quality`, `all` | Benchmark operations |
| `sandbox` | `create`, `exec`, `collect`, `destroy` | Sandbox management |
| `project` | `status`, `sync` | Project operations on Triton |
| `telemetry` | `dashboard`, `export` | Telemetry operations |
| `system` | `health`, `gpu`, `logs` | System health and diagnostics |

**Key methods:**
- `model_status()` → `dict` — Current loaded model, GPU stats, BeeLlama health, active containers
- `model_swap(config_id, dry_run)` → `dict` — Swap model via state machine or direct docker compose
- `bench_throughput(config_id, reps, dry_run)` → `dict` — Run throughput benchmark on Triton
- `sandbox_create(project, name, dry_run)` → `dict` — Create Docker sandbox on Triton
- `sandbox_exec(name, command, timeout)` → `dict` — Execute command in sandbox
- `sandbox_collect(name, remote_path, local_path)` → `dict` — Copy files from sandbox to nightmare
- `system_health()` → `dict` — Full health check (SSH, GPU, BeeLlama, disk, Docker, load)
- `system_gpu()` → `dict` — Detailed GPU status with process info

**Model swap strategy:**
1. Try `deploy-state-machine.py swap <profile>` first (state machine)
2. Fall back to direct `docker compose --profile <profile> up -d`
3. Poll `/health` until ready (max 180s timeout)

### `work_engine.py` — Full Pipeline Orchestration (1477 lines)

**Class:** `WorkEngine`

Orchestrates the complete remote work pipeline by connecting all existing tools: SSH → model swap → Docker sandbox → inference → Gitea push → telemetry.

**Pipeline steps (run command):**
1. **Swap model** — Ensure the correct BeeLlama model is loaded
2. **Create sandbox** — Docker container on Triton with project mounted
3. **Prepare project** — Verify project code is accessible in sandbox
4. **Run inference** — Call BeeLlama API via curl on Triton
5. **Collect results** — Write results JSON to project directory
6. **Push to Gitea** — Commit results to a feature branch
7. **Cleanup** — Destroy the Docker sandbox

**Telemetry DB:** `~/coder-harness-telemetry.db` — tracks `work_sessions` and `work_events` tables with session lifecycle, inference metrics, and event timeline.

### `sandbox_manager.py` — Docker Sandbox Lifecycle (621 lines)

**Class:** `SandboxManager`

Manages ephemeral Docker containers on Triton for coding work. Each sandbox is a `ubuntu:24.04` container with project code volume-mounted.

| Command | Description |
|---------|-------------|
| `create --project <p> [--name <n>]` | Create a new sandbox |
| `list` | List active harness sandboxes |
| `exec --name <n> --command <cmd>` | Execute command in sandbox |
| `collect --name <n> --from <path> --to <path>` | Copy files from sandbox to local |
| `destroy --name <n>` | Destroy a sandbox |
| `destroy-all` | Destroy all harness sandboxes |
| `report` | Generate telemetry summary report |

**Sandbox naming:** All containers are prefixed with `harness-` (e.g., `harness-test-01`).

**Container setup:** Ubuntu 24.04 base image, volumes: `~/projects/<project>` → `/workspace` (read-only), `~/models` → `/models` (read-only). Auto-installs: python3, pip, git, curl, jq.

### `gitea_utils.py` — Gitea API Client (663 lines)

**Class:** `GiteaClient`

All Gitea API calls are tunnelled through SSH since Gitea only listens on localhost inside Triton:

```
nightmare → SSH → Triton → curl localhost:3000/api/v1/...
```

| Command | Description |
|---------|-------------|
| `list-repos` | List all repositories |
| `get-repo <name>` | Get details for a repository |
| `create-repo <name> [-d desc]` | Create a new repository |
| `list-branches <repo>` | List branches |
| `create-branch <repo> <branch>` | Create a new branch |
| `get-file <repo> <path>` | Get file content (base64-decoded) |
| `create-file <repo> <path> <content> -m <msg>` | Create or update a file |
| `list-files <repo> [--path <p>]` | List files in a directory |
| `clone <repo> <target_dir>` | Clone a repo on Triton |
| `push <local_dir> <repo>` | Push local changes to Gitea |
| `search <query>` | Search code across repos |

### `telemetry_collector.py` — Active Collection (448 lines)

**Class:** `TelemetryCollector`

| Command | Description |
|---------|-------------|
| `start --interval <sec>` | Start continuous collection loop (default: 60s) |
| `collect` | Run a single collection cycle |
| `events --limit <n>` | Show recent telemetry events |
| `summary` | Show a telemetry summary |
| `export --output <path>` | Export all telemetry data to JSON |

**Telemetry SQLite schema (additional tables beyond benchmark-results.db):**

```sql
CREATE TABLE gpu_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gpu_index INTEGER NOT NULL,
    name TEXT,
    memory_used_mb REAL,
    memory_total_mb REAL,
    temperature_c REAL,
    utilization_pct REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);

CREATE TABLE benchmark_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT NOT NULL,
    benchmark_type TEXT NOT NULL,
    tokens_per_sec REAL,
    tokens_generated INTEGER,
    elapsed_seconds REAL,
    vram_used_mb REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);
```

### `telemetry_dashboard.py` — HTML Dashboard (1446 lines)

**Class:** `TelemetryDashboard`

Generates a self-contained HTML dashboard from SQLite telemetry data. Reads from two databases:
- `~/coder-harness-telemetry.db` — work sessions + events
- `~/projects/dsh-hub/coder-harness/benchmark-results.db` — benchmarks, configs, GPU fit

**Dashboard sections:**
1. **System Overview** — GPU configs, VRAM usage bar charts, last model swap
2. **Benchmark Results** — Per-config summary table, speed bar charts, GPU fit matrix
3. **Work Sessions** — Session table with status badges, timeline view
4. **Event Log** — Recent telemetry events with timestamps
5. **Recommendations** — Best configs for orchestrator/coder roles, fastest overall

**Output:** `reports/telemetry-dashboard.html` — single self-contained HTML file with auto-refresh every 60 seconds, dark theme, inline SVG charts.

---

## Config ↔ Profile Mapping

The manifest config IDs (used by Python modules) and Docker profile names (used by compose) are different identifiers for the same configs:

| Manifest Config ID | Docker Profile | 3090 Model | Speed | Port |
|---|---|---|---|---|
| `3090-qwen36-35b` | `config-i` | Qwen3.6-35B-A3B IQ4_XS | 188 tok/s | 8080 |
| `3090-muse-glimmer` | `config-h` | Muse-Glimmer-30B Q3_K_XL | 55 tok/s | 8080 |
| `3090-laguna-xs` | `config-g-laguna` | Laguna-XS-2.1 APEX-i-quality | — | 8080 |
| `3090-qwen35-9b` | `config-h-qwen35` | Qwen3.5-9B-MTP Q4_K_M | 171 tok/s | 8080 |
| `3070-qwen35-9b` | (shared 3070) | Qwen3.5-9B-MTP Q4_K_M | 65 tok/s | 8082 |
| `3070-qwen35-4b` | (no profile) | Qwen3.5-4B Q5_K_M | — | 8082 |

**Python CLI uses manifest IDs:** `python3 orchestrate.py --config 3090-qwen36-35b`
**Docker CLI uses profile names:** `docker compose --profile config-i up -d`

### VRAM Budget

**RTX 3090 (24 GB):**

| Config | Weights | KV Cache | Spec Draft | Total | Headroom |
|--------|---------|----------|------------|-------|----------|
| config-i (Qwen3.6-35B) | 17.0 GB | 1.8 GB | 0.2 GB | 19.0 GB | 5.0 GB ✅ |
| config-h (Muse-Glimmer) | 9.8 GB | 2.5 GB | 2.8 GB | 15.1 GB | 8.9 GB ✅ |
| config-g-laguna | 15.0 GB | 6.0 GB | 0 | 21.0 GB | 3.0 GB ⚠️ |
| config-h-qwen35 | 5.5 GB | 1.0 GB | 0.9 GB | 7.4 GB | 16.6 GB ✅ |

**RTX 3070 (8 GB):**

| Config | Total | Headroom |
|--------|-------|----------|
| Qwen3.5-9B + DFlash-MTP | 6.7 GB | 1.3 GB ✅ |

---

## Work Engine Pipeline — Detailed

The work engine (`work_engine.py`) chains all existing tools into a 7-step pipeline:

```
Step 1: Swap Model
  │  ├── Try deploy-state-machine.py swap <profile> (state machine)
  │  ├── Fallback: docker compose --profile <profile> up -d
  │  └── Poll /health every 3s (max 180s)
  ▼
Step 2: Create Sandbox
  │  ├── docker run -d --name harness-<name> ubuntu:24.04 sleep infinity
  │  ├── Volume mount: ~/projects/<project> → /workspace
  │  └── Install deps: python3, git, curl, jq
  ▼
Step 3: Prepare Project
  │  ├── Verify project exists at ~/projects/<project>
  │  ├── Check current git branch
  │  └── Verify files accessible inside container
  ▼
Step 4: Run Inference
  │  ├── Write JSON payload to temp file on Triton (avoids shell-escaping)
  │  ├── curl -s -X POST http://localhost:<port>/v1/chat/completions -d @payload.json
  │  ├── Parse response (content, reasoning_content, timings, usage)
  │  └── Record inference metrics to telemetry DB
  ▼
Step 5: Collect Results
  │  ├── Write results JSON to project directory on Triton
  │  └── Record event in telemetry DB
  ▼
Step 6: Push to Gitea (only if inference succeeded)
  │  ├── git checkout -b results/<name>
  │  ├── git add results-<name>.json
  │  ├── git commit -m "results: <name> [timestamp]"
  │  ├── git push origin results/<name>
  │  └── git checkout <original-branch>
  ▼
Step 7: Cleanup (always runs)
  └── docker rm -f harness-<name>
```

**Error handling:** Each step records events in the telemetry DB. If any step fails, the pipeline records the failure and continues to cleanup. Step 7 (cleanup) always runs, even on failure.

### Telemetry Events Generated

| Event | When |
|-------|------|
| `model_swap_start` | Before model swap begins |
| `model_swap_done` | After model swap completes |
| `model_swap_timeout` | If swap exceeds 180s |
| `model_swap_skip` | Config uses shared 3070 (no swap needed) |
| `sandbox_create_start` | Before sandbox creation |
| `sandbox_created` | After container created |
| `sandbox_create_fail` | If creation fails |
| `project_ready` | After project verification |
| `inference_start` | Before inference call |
| `inference_done` | After inference completes |
| `inference_fail` | If inference fails |
| `results_collected` | After results written |
| `push_start` | Before Gitea push |
| `push_done` | After Gitea push |
| `sandbox_destroyed` | After container cleanup |
| `pipeline_done` | Pipeline completion |

---

## Sandbox Lifecycle

```
Creating
  │  ├── docker run -d --name harness-<name> ubuntu:24.04 sleep infinity
  │  ├── Volume mount: ~/projects/<project> → /workspace:ro
  │  ├── Volume mount: ~/models → /models:ro
  │  ├── Install: python3, pip, git, curl, jq
  │  └── Record session in telemetry DB (status: 'created')
  ▼
Running
  │  ├── exec: docker exec harness-<name> <command>
  │  ├── collect: docker cp → /tmp/staging → SCP to nightmare
  │  └── Status tracked in work_sessions table (status: 'running')
  ▼
Destroying
  │  ├── docker rm -f harness-<name>
  │  ├── Update session status to 'destroyed'
  │  └── Record sandbox_destroyed event
  ▼
Done
```

**Naming:** All containers use `harness-` prefix (e.g., `harness-test-01`). The manager auto-strips/prefixes.

**Volume mounts:**

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `~/projects/<project>` | `/workspace` | read-only | Project source code |
| `~/models` | `/models` | read-only | Model files |

**File collection (2-step transfer):**
1. `docker cp harness-<name>:/path /tmp/harness-<name>-collect-<pid>` — container to Triton
2. `scp -r <user>@<LAN_IP>:/tmp/staging ./local-path` — Triton to nightmare
3. `rm -rf /tmp/staging` — cleanup

---

## Gitea Integration — Detailed

All Gitea API calls are tunnelled through SSH:

```
nightmare                    Triton
┌──────────┐                ┌──────────────────────────────┐
│gitea_utils│──SSH──→       │ curl -sf -X GET              │
│   .py     │               │   http://localhost:3000       │
│           │               │   /api/v1/repos/search       │
│           │               │   -H "Authorization: token"  │
└──────────┘                └──────────────────────────────┘
```

### API Methods

| Method | HTTP | Endpoint | Description |
|--------|------|----------|-------------|
| `list_repos()` | GET | `/repos/search?limit=50` | List all repos |
| `get_repo(name)` | GET | `/repos/{user}/{name}` | Get repo details |
| `create_repo(name, desc)` | POST | `/user/repos` | Create new repo |
| `delete_repo(name)` | DELETE | `/repos/{user}/{name}` | Delete repo |
| `list_branches(repo)` | GET | `/repos/{user}/{repo}/branches` | List branches |
| `create_branch(repo, branch)` | *(git over SSH)* | — | Create branch |
| `delete_branch(repo, branch)` | DELETE | `/repos/{user}/{repo}/branches/{branch}` | Delete branch |
| `get_file(repo, path)` | GET | `/repos/{user}/{repo}/contents/{path}` | Get file (base64 decoded) |
| `create_file(repo, path, content)` | POST | `/repos/{user}/{repo}/contents/{path}` | Create/update file |
| `list_files(repo, path)` | GET | `/repos/{user}/{repo}/contents/{path}` | List directory |
| `list_commits(repo, branch)` | GET | `/repos/{user}/{repo}/commits?sha={branch}` | List commits |
| `clone(repo, target_dir)` | *(git clone)* | — | Clone with token auth |
| `push(local_dir, repo)` | *(git push)* | — | Push to token remote |
| `search_code(query)` | GET | `/codesearch?q={query}` | Search code |

**Authentication:** Token `<GITEA_TOKEN>` in `Authorization: token` header. For clone/push, embedded in URL: `http://<user>:<token>@localhost:3000/{user}/{repo}.git`

**File create/update:** `create_file()` auto-detects existing files (GET for SHA), then POSTs with SHA for update or without for create. Content is base64-encoded.

---

## Telemetry Data Flow

```
Triton (SSH) ──→ telemetry_collector.py ──→ gpu_snapshots table
                                               ↑
work_engine.py ──→ work_sessions table ────────┤
                  work_events table ───────────┤
                                               ↓
sandbox_manager.py ──→ work_sessions table ──→ telemetry_dashboard.py ──→ HTML
                      work_events table
                                              ↓
remote_control.py ──→ benchmark-results.db ──→ telemetry_dashboard.py
                                               ↓
                                         reports/telemetry-dashboard.html
```

### Telemetry DB Tables

| Table | Source | Purpose |
|-------|--------|---------|
| `work_sessions` | work_engine.py, sandbox_manager.py | Session lifecycle tracking |
| `work_events` | work_engine.py, sandbox_manager.py, telemetry_collector.py | Event log |
| `gpu_snapshots` | telemetry_collector.py | Periodic nvidia-smi readings |
| `benchmark_results` | telemetry_collector.py, remote_control.py | Benchmark metrics |

**DB location:** `~/coder-harness-telemetry.db` (shared across all modules)

---

## Workflows

### 1. Run a Coding Task

```bash
# 1. Check health
python3 harness.py health

# 2. Execute task
python3 harness.py run -p coder-harness -t "Fix bug X" -c 3090-qwen36-35b --name fix-01

# 3. Check session
python3 harness.py sessions --status completed

# 4. Collect results
python3 harness.py collect --name fix-01 --to ./results/

# 5. Generate dashboard
python3 harness.py dashboard
```

### 2. Benchmark Multiple Configs

```bash
python3 harness.py bench all --configs 3090-qwen36-35b,3090-qwen35-9b,3070-qwen35-9b
python3 harness.py report --format md
cat reports/benchmark-report.md
```

### 3. Model Swap and Test

```bash
python3 harness.py model swap config-h
python3 harness.py model status --pretty
python3 harness.py bench throughput -c 3090-muse-glimmer --reps 1
python3 harness.py model swap config-i
```

### 4. Sandbox Development Cycle

```bash
python3 harness.py sandbox create -p coder-harness -n dev-01
python3 remote_control.py sandbox exec --name harness-dev-01 --command "ls /workspace/"
python3 remote_control.py sandbox collect --name harness-dev-01 --from /workspace/results --to ./dev-results/
python3 harness.py sandbox destroy -n dev-01
```

### 5. Gitea Code Review

```bash
python3 harness.py gitea repos
python3 harness.py gitea files coder-harness --path src/
python3 gitea_utils.py list-commits coder-harness --limit 5
python3 gitea_utils.py search "SSHClient" --repo coder-harness
```

### 6. Full Telemetry Monitoring

```bash
python3 telemetry_collector.py start --interval 60 &  # Start collector
python3 harness.py run -p coder-harness -t "Task 1"   # Do work
python3 telemetry_collector.py summary                 # Check stats
python3 harness.py dashboard                           # Generate dashboard
python3 telemetry_collector.py export --output telemetry-export.json
```

---

## BeeLlama API Reference

### Chat Completions

```bash
POST http://localhost:{port}/v1/chat/completions
Content-Type: application/json

{
  "messages": [{"role": "user", "content": "..."}],
  "max_tokens": 512,
  "temperature": 0.3,
  "top_p": 0.95,
  "top_k": 40
}
```

**Response fields:** `choices[0].message.content`, `choices[0].message.reasoning_content`, `timings.predicted_per_second`, `timings.prompt_per_second`, `usage.total_tokens`

### Health Check

```bash
GET http://localhost:{port}/health
# → {"status": "ok"}
```

### Model List

```bash
GET http://localhost:{port}/v1/models
# → {"data": [{"id": "model-name", ...}]}
```

---

## Troubleshooting

### SSH connection refused

```bash
# Verify Triton is reachable
ping <LAN_IP>

# Check SSH key and host
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> echo OK

# If key-based auth fails, check key permissions
chmod 600 ~/.ssh/id_rsa
```

### Model swap hangs

The swap can stall if a sandbox is mid-generation. Kill active sandboxes first:

```bash
harness.py sandbox cleanup
harness.py model swap config-i
```

### Benchmark returns 0 tok/s

Likely the model container is not running. Check:

```bash
ssh <user>@<LAN_IP> "docker ps | grep beellama"
```

If no container is running, restart with the desired config:

```bash
harness.py model swap config-i
```

### Container won't start

```bash
# On Triton:
docker compose --profile config-i logs
ss -tlnp | grep 8080   # Check port is free
nvidia-smi              # Check GPU memory
```

### OOM on 3090

- Lower `CONTEXT_SIZE` in `.env`
- Use more aggressive KVarN (kvarn4 instead of kvarn5)
- Switch to a smaller model
- Use `--cpu-moe` flag for MoE models (config-h-coder)

### Gitea unreachable

```bash
# Gitea only listens on localhost inside Triton — must use SSH tunnel or curl via SSH
ssh <user>@<LAN_IP> "curl -sf http://localhost:3000/api/v1/repos/search"

# If the service is down, restart it:
ssh <user>@<LAN_IP> "docker restart gitea"
```

### Sandbox creation fails

Check disk space and Docker health on Triton:

```bash
ssh <user>@<LAN_IP> "df -h /home/<user> && docker info"
```

Remove stale sandboxes if disk is full:

```bash
harness.py sandbox cleanup
```

### Telemetry dashboard shows no data

Ensure the telemetry collector is running and both databases exist:

```bash
ls -la ~/coder-harness-telemetry.db
ls -la ~/projects/dsh-hub/coder-harness/benchmark-results.db
harness.py dashboard
```

### Benchmark interrupted (SIGINT/SIGTERM)

Resume from last checkpoint:

```bash
python3 orchestrate.py --phase 1 --resume
```

The `checkpoint` table in SQLite tracks completed runs. On resume, `running` status runs are marked `incomplete` and the pipeline continues from where it left off.
