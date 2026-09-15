# Coder Harness Benchmark Platform — API Reference

Complete reference for every external interface, data format, and CLI command.

---

## 1. BeeLlama API

Base URL: `http://localhost:{port}/v1/`

| GPU | Port |
|-----|------|
| 3090 (primary) | 8080 |
| 3070 (helper) | 8082 |

### POST /chat/completions

OpenAI-compatible chat completion endpoint.

**Request:**

```json
{
  "model": "q",
  "messages": [{"role": "user", "content": "..."}],
  "max_tokens": 2048,
  "temperature": 0.3,
  "top_p": 0.95,
  "top_k": 40,
  "min_p": 0.05,
  "repeat_penalty": 1.1
}
```

**Response:**

```json
{
  "choices": [{
    "finish_reason": "stop",
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "...",
      "reasoning_content": "..."
    }
  }],
  "usage": {
    "completion_tokens": 512,
    "prompt_tokens": 23,
    "total_tokens": 535,
    "completion_tokens_details": {
      "reasoning_tokens": 400,
      "visible_tokens": 112
    }
  },
  "timings": {
    "predicted_per_second": 188.5,
    "draft_n": 5,
    "draft_n_accepted": 3
  }
}
```

> **Note:** Qwen3.6-35B is a thinking model — content may be in `reasoning_content` with empty `content` if `max_tokens` is too low. Use `max_tokens >= 1024` for meaningful output.

### GET /health

Returns `{"status":"ok"}` when the model is loaded and ready.

### GET /v1/models

Returns loaded model info including `n_ctx`, `n_params`, `ftype`.

---

## 2. SSH Interface

The `SSHClient` class in `ssh_utils.py` manages all remote operations. It supports two authentication modes:

### Authentication Modes

**Primary — Key-based:**
```python
# Preferred mode: SSH key authentication
# SSH keys are configured on nightmare → Triton
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i {key_path} <user>@<LAN_IP> {cmd}
```

**Fallback — Password-based (sshpass):**
```python
# Legacy fallback: uses sshpass with stored password
# Requires sshpass installed on the local machine
sshpass -p {pw} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> {cmd}
```

### SSHClient API

```python
from ssh_utils import SSHClient

client = SSHClient(host="<LAN_IP>", user="<user>", pw="password")

# Connect (3 attempts with exponential backoff)
client.connect()                        # sshpass mode (default)
client.connect(key_path="~/.ssh/id_rsa")  # key-based mode

# Execute remote command
stdout, stderr, exit_code = client.run("nvidia-smi", timeout=30)

# BeeLlama chat completion
response = client.curl_beellama(
    port=8080,
    messages=[{"role": "user", "content": "..."}],
    max_tokens=2048,
    temperature=0.3
)

# GPU info
gpu_info = client.get_nvidia_smi(gpu_index=0)

# Health check
healthy = client.check_beellama_health(port=8080)

# Model list
models = client.get_model_list(port=8080)
```

### Constants

| Constant | Value |
|----------|-------|
| `TRITON_HOST` | `<LAN_IP>` |
| `TRITON_USER` | `<user>` |
| `PORT_3090` | `8080` |
| `PORT_3070` | `8082` |

### Common Commands

```bash
# GPU status
nvidia-smi --query-gpu=name,memory.used,memory.free,temperature.gpu --format=csv,noheader

# Container status
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Health check
curl -sf http://localhost:8080/health
curl -sf http://localhost:8082/health

# Model swap
cd ~/dockers/beellama-benchmark
docker compose --profile config-i up -d
docker compose --profile config-i down
```

---

## 3. SQLite Schema

Database file: `benchmark-results.db`

### Table Summary

| Table | Purpose |
|-------|---------|
| `model_configs` | Model/GPU/quant configurations (6 rows) |
| `tasks` | Benchmark task definitions (23 rows) |
| `benchmark_runs` | Individual inference results (one per model×task×turn×rep) |
| `judge_scores` | Quality scores per run (5 dimensions + overall) |
| `latency_profiles` | Multi-bracket latency measurements |
| `gpu_fit_matrix` | Model/context GPU fit data |
| `schema_version` | Schema version tracking |
| `checkpoints` | Runtime crash recovery (created by `checkpoint.py`, not in schema.sql) |

**Indexes:** `idx_runs_model_task`, `idx_runs_status`, `idx_scores_run`

### model_configs

```sql
CREATE TABLE IF NOT EXISTS model_configs (
  id            TEXT PRIMARY KEY,
  gpu           TEXT    NOT NULL,
  model_name    TEXT    NOT NULL,
  quantization  TEXT,
  context_size  INTEGER DEFAULT 4096,
  thinking_enabled INTEGER DEFAULT 0,
  kvarn_level   TEXT,
  speculative_type TEXT,
  draft_model   TEXT,
  port          INTEGER NOT NULL,
  model_path    TEXT    NOT NULL,
  notes         TEXT
);
```

### tasks

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  role              TEXT    NOT NULL CHECK(role IN ('orchestrator','coder','multi-turn')),
  category          TEXT    NOT NULL,
  difficulty        TEXT CHECK(difficulty IN ('easy','medium','hard')),
  prompt            TEXT    NOT NULL,
  expected_behavior TEXT,
  scoring_criteria  TEXT,
  automated_check   TEXT,
  test_cases        TEXT,
  turns             INTEGER DEFAULT 1
);
```

### benchmark_runs

```sql
CREATE TABLE IF NOT EXISTS benchmark_runs (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id      TEXT NOT NULL REFERENCES model_configs(id),
  task_id              TEXT NOT NULL REFERENCES tasks(id),
  turn_index           INTEGER DEFAULT 0,
  repetition           INTEGER DEFAULT 1,
  started_at           TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at         TEXT,
  status               TEXT CHECK(status IN ('running','complete','failed','incomplete')) DEFAULT 'running',
  response_text        TEXT,
  predicted_per_second REAL,
  prompt_per_second    REAL,
  predicted_ms         REAL,
  prompt_ms            REAL,
  predicted_n          INTEGER,
  thinking_tokens      INTEGER DEFAULT 0,
  total_tokens         INTEGER,
  error_message        TEXT,
  gpu_temperature      REAL,
  beellama_commit      TEXT,
  model_sha256         TEXT,
  sampling_params      TEXT,
  session_id           TEXT,
  UNIQUE(model_config_id, task_id, turn_index, repetition)
);
```

### judge_scores

```sql
CREATE TABLE IF NOT EXISTS judge_scores (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER NOT NULL REFERENCES benchmark_runs(id),
  judge_model         TEXT    NOT NULL,
  completeness        INTEGER CHECK(completeness BETWEEN 0 AND 10),
  correctness         INTEGER CHECK(correctness BETWEEN 0 AND 10),
  quality             INTEGER CHECK(quality BETWEEN 0 AND 10),
  intelligence        INTEGER CHECK(intelligence BETWEEN 0 AND 10),
  role_fit            INTEGER CHECK(role_fit BETWEEN 0 AND 10),
  overall             REAL GENERATED ALWAYS AS (
    (completeness + correctness + quality + intelligence + role_fit) / 5.0
  ) STORED,
  judge_reasoning     TEXT,
  is_cross_validated  INTEGER DEFAULT 0,
  scored_at           TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(run_id, judge_model)
);
```

### latency_profiles

```sql
CREATE TABLE IF NOT EXISTS latency_profiles (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id      TEXT    NOT NULL REFERENCES model_configs(id),
  target_tokens        INTEGER NOT NULL,
  actual_tokens        INTEGER,
  ttft_ms              REAL,
  generation_ms        REAL,
  total_ms             REAL,
  predicted_per_second REAL,
  measured_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### gpu_fit_matrix

```sql
CREATE TABLE IF NOT EXISTS gpu_fit_matrix (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id         TEXT    NOT NULL REFERENCES model_configs(id),
  gpu                     TEXT    NOT NULL,
  context_size            INTEGER NOT NULL,
  fits                    INTEGER NOT NULL,
  vram_used_mb            REAL,
  vram_total_mb           REAL,
  inference_ok            INTEGER,
  inference_tokens_per_sec REAL,
  error_message           TEXT,
  measured_at             TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(model_config_id, gpu, context_size)
);
```

### schema_version

```sql
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### checkpoints (runtime-only, created by `checkpoint.py`)

Not defined in `schema.sql`. Created at runtime by the `CheckpointManager` class to persist completed run IDs and per-config progress for crash recovery.

---

## 4. CLI Commands

### Config ID ↔ Docker Profile Mapping

The manifest config IDs (used by Python modules) and Docker profile names (used by compose) are different identifiers for the same configs:

| Manifest Config ID | Docker Profile | 3090 Model | Port |
|---|---|---|---|
| `3090-qwen36-35b` | `config-i` | Qwen3.6-35B-A3B IQ4_XS | 8080 |
| `3090-muse-glimmer` | `config-h` | Muse-Glimmer-30B Q3_K_XL | 8080 |
| `3090-laguna-xs` | `config-g-laguna` | Laguna-XS-2.1 APEX-i-quality | 8080 |
| `3090-qwen35-9b` | `config-h-qwen35` | Qwen3.5-9B-MTP Q4_K_M | 8080 |
| `3070-qwen35-9b` | (shared 3070) | Qwen3.5-9B-MTP Q4_K_M | 8082 |
| `3070-qwen35-4b` | (no profile) | Qwen3.5-4B Q5_K_M | 8082 |

**Python CLI uses manifest IDs:** `python3 orchestrate.py --config 3090-qwen36-35b`
**Docker CLI uses profile names:** `docker compose --profile config-i up -d`

### orchestrate.py

```bash
python3 orchestrate.py                     # Run full benchmark (preflight → GPU fit → batch → report)
python3 orchestrate.py --phase 0           # GPU fit only
python3 orchestrate.py --phase 1           # Benchmark runs only
python3 orchestrate.py --config config-i   # Single config
python3 orchestrate.py --dry-run           # Print plan without executing
```

### gpu_fit.py

```bash
python3 gpu_fit.py                         # Probe all configs × context sizes
python3 gpu_fit.py --config config-i       # Single config
python3 gpu_fit.py --sizes 4096,32768     # Specific context sizes
python3 gpu_fit.py --show                  # Show stored results
python3 gpu_fit.py --dry-run               # Print probe plan
```

### pilot.py

```bash
python3 pilot.py                           # Run 5-task pilot (45 runs)
python3 pilot.py --dry-run                 # Print pilot plan without executing
python3 pilot.py --report                  # Generate pilot report after run
python3 pilot.py --db custom.db            # Use alternative database
```

### report.py

```bash
python3 report.py                          # Generate markdown report
python3 report.py --format json            # JSON output
python3 report.py --db custom.db           # Use alternative database
```

### deploy-state-machine.py (on Triton)

```bash
python3 deploy-state-machine.py swap config-i     # Swap to Config-I
python3 deploy-state-machine.py bench config-h     # Swap + benchmark
python3 deploy-state-machine.py status             # Current state + health
python3 deploy-state-machine.py abort              # Graceful shutdown
python3 deploy-state-machine.py history            # Swap history
python3 deploy-state-machine.py rollback           # Last known-good config
```

### Shell Scripts (on Triton)

```bash
./scripts/bench-throughput.sh --config config-i --port 8080 --reps 3
./scripts/bench-quality.sh --config config-i --port 8080 --tasks all
./scripts/bench-context.sh --config config-i --port 8080 --sizes "1000,4000,16000"
./scripts/bench-all-configs.sh --configs "config-i,config-h" --benchmarks "throughput,quality"
./scripts/bench-response-quality.sh --port 8080 --prompt-file scripts/prompts/coding-easy.txt
```

### harness.py — Single Entry Point

```bash
python3 harness.py status                      # Full system status (model, GPU, sandboxes)
python3 harness.py health                      # Quick 7-point preflight health check
python3 harness.py setup                       # Print first-time setup guide
python3 harness.py bench throughput -c 3090-qwen36-35b        # Throughput benchmark
python3 harness.py bench throughput -c 3090-qwen36-35b --reps 5
python3 harness.py bench quality -c 3090-qwen36-35b           # Quality benchmark
python3 harness.py bench quality -c 3090-qwen36-35b --tasks T11,T14
python3 harness.py bench all --configs 3090-qwen36-35b,3090-qwen35-9b
python3 harness.py bench orchestrate --phase 1                 # Full pipeline
python3 harness.py bench orchestrate --phase 1 --config 3090-qwen35-9b
python3 harness.py bench orchestrate --phase 0 --sizes 4096,8192  # GPU fit
python3 harness.py bench orchestrate --phase 1 --resume        # Resume from checkpoint
python3 harness.py bench pilot                 # Methodology pilot (45 runs)
python3 harness.py bench pilot --dry-run       # Preview pilot plan
python3 harness.py run -p coder-harness -t "Fix bug X" -c 3090-qwen36-35b
python3 harness.py run -p pulsar-harness -t "Optimize kernel" --branch feature-x
python3 harness.py sessions                    # List work sessions
python3 harness.py sessions --status running   # Filter by status
python3 harness.py collect --name fix-ssh-01   # Collect results
python3 harness.py collect --name fix-ssh-01 --to ./results/
python3 harness.py model status                # Current model + GPU status
python3 harness.py model swap config-i         # Swap to config-i (Docker profile name)
python3 harness.py model swap 3090-qwen36-35b  # Swap to config-i (manifest ID)
python3 harness.py model list                  # List all available configs
python3 harness.py sandbox create -p coder-harness -n test-01
python3 harness.py sandbox list                # List active sandboxes
python3 harness.py sandbox destroy -n test-01  # Destroy a sandbox
python3 harness.py sandbox cleanup             # Destroy all harness sandboxes
python3 harness.py gitea repos                 # List all Gitea repositories
python3 harness.py gitea branches coder-harness  # List branches
python3 harness.py gitea files coder-harness   # List root files
python3 harness.py gitea files coder-harness --path docs/
python3 harness.py dashboard                   # Generate HTML telemetry dashboard
python3 harness.py telemetry export            # Export telemetry to JSON
python3 harness.py report --format md          # Generate Markdown report
python3 harness.py report --format json        # Generate JSON report
```

**Global flags:** `--pretty` (pretty JSON), `--verbose`/`-v` (debug logging), `--version`

### remote_control.py — Unified Controller

```bash
# Model management
python3 remote_control.py model status --pretty           # Current model + GPU status
python3 remote_control.py model list --pretty             # List all configs
python3 remote_control.py model swap config-i             # Swap to config-i
python3 remote_control.py model swap config-i --dry-run   # Dry-run swap

# Benchmarks
python3 remote_control.py bench throughput --config 3090-qwen36-35b --reps 3
python3 remote_control.py bench quality --config 3090-qwen36-35b --tasks all
python3 remote_control.py bench all --configs 3090-qwen36-35b,3090-qwen35-9b

# Sandbox management
python3 remote_control.py sandbox create --project coder-harness --name test-01
python3 remote_control.py sandbox exec --name test-01 --command "ls -la"
python3 remote_control.py sandbox collect --name test-01 --from /workspace/results --to ./results/
python3 remote_control.py sandbox destroy --name test-01

# Project operations
python3 remote_control.py project status                  # List projects on Triton
python3 remote_control.py project sync --local ./src --remote /home/<user>/projects/myproj

# Telemetry
python3 remote_control.py telemetry dashboard              # Generate HTML dashboard
python3 remote_control.py telemetry export                 # Export SQLite to JSON

# System diagnostics
python3 remote_control.py system health --pretty           # Full health check
python3 remote_control.py system gpu --pretty              # Detailed GPU status
python3 remote_control.py system logs --lines 100          # Recent container logs
```

**Global flags:** `--host <ip>` (default: <LAN_IP>), `--user <name>` (default: <user>), `--pretty`

### work_engine.py — Full Pipeline

```bash
# Execute a coding task (full 7-step pipeline)
python3 work_engine.py run \
  --project coder-harness \
  --branch main \
  --config 3090-qwen36-35b \
  --task "Fix the SSH connection pattern in ssh_utils.py" \
  --name fix-ssh-01 \
  --max-tokens 2048

# Run benchmarks inside a sandbox
python3 work_engine.py bench --project coder-harness --config 3090-qwen36-35b --tasks throughput
python3 work_engine.py bench --project coder-harness --config 3090-qwen36-35b --tasks throughput,quality
python3 work_engine.py bench --project coder-harness --config 3090-qwen36-35b --tasks all

# List work sessions
python3 work_engine.py sessions
python3 work_engine.py sessions --status running
python3 work_engine.py sessions --status completed

# Collect results from a session
python3 work_engine.py collect --name fix-ssh-01
python3 work_engine.py collect --name fix-ssh-01 --to ./collected-results/

# Cleanup all harness sandboxes
python3 work_engine.py cleanup

# Full system status
python3 work_engine.py status --pretty
```

**Global flags:** `--host <ip>`, `--user <name>`, `--dry-run`, `--pretty`, `--verbose`/`-v`

### sandbox_manager.py — Docker Sandbox Lifecycle

```bash
# Create a sandbox
python3 sandbox_manager.py create --project coder-harness --branch main --name test-01
python3 sandbox_manager.py create --project pulsar-harness --config-id 3090-qwen36-35b

# List active sandboxes
python3 sandbox_manager.py list

# Execute a command inside a sandbox
python3 sandbox_manager.py exec --name test-01 --command "python3 --version"
python3 sandbox_manager.py exec --name test-01 --command "ls -la /workspace/" --timeout 30

# Collect files from sandbox to local machine
python3 sandbox_manager.py collect --name test-01 --from /workspace/results --to ./results/

# Destroy a sandbox
python3 sandbox_manager.py destroy --name test-01

# Destroy all harness sandboxes
python3 sandbox_manager.py destroy-all

# Generate telemetry summary report
python3 sandbox_manager.py report
```

**Global flags:** `--host <ip>`, `--user <name>`, `--dry-run`, `--verbose`/`-v`

### gitea_utils.py — Gitea API Client

```bash
# Repository operations
python3 gitea_utils.py list-repos                           # List all repos
python3 gitea_utils.py get-repo coder-harness               # Get repo details
python3 gitea_utils.py create-repo my-project -d "My project"
python3 gitea_utils.py create-repo my-project --auto-init
python3 gitea_utils.py delete-repo my-project

# Branch operations
python3 gitea_utils.py list-branches coder-harness          # List branches
python3 gitea_utils.py create-branch coder-harness feature-x
python3 gitea_utils.py create-branch coder-harness feature-x --from develop
python3 gitea_utils.py delete-branch coder-harness feature-x

# File operations
python3 gitea_utils.py get-file coder-harness README.md
python3 gitea_utils.py get-file coder-harness src/main.py --branch develop
python3 gitea_utils.py create-file coder-harness docs/guide.md "Hello World" -m "Add guide"
python3 gitea_utils.py create-file coder-harness docs/guide.md "Updated" -m "Update guide" --branch feature-x
python3 gitea_utils.py list-files coder-harness
python3 gitea_utils.py list-files coder-harness --path src/

# Commit operations
python3 gitea_utils.py list-commits coder-harness --branch main --limit 10
python3 gitea_utils.py get-commit coder-harness abc1234

# Clone and push
python3 gitea_utils.py clone coder-harness /home/<user>/projects/coder-harness
python3 gitea_utils.py push /home/<user>/projects/coder-harness coder-harness --branch main

# Search
python3 gitea_utils.py search "SSHClient" --repo coder-harness
python3 gitea_utils.py search "benchmark"
```

**Global flags:** `--host <ip>`, `--user <name>`, `--token <token>`, `--dry-run`

### telemetry_collector.py — Active Collection

```bash
# Start continuous collection (every 60 seconds)
python3 telemetry_collector.py start --interval 60

# Run a single collection cycle
python3 telemetry_collector.py collect

# Show recent events
python3 telemetry_collector.py events --limit 20

# Show telemetry summary
python3 telemetry_collector.py summary

# Export all data to JSON
python3 telemetry_collector.py export --output telemetry-export.json
```

**Global flags:** `--db <path>` (default: ~/coder-harness-telemetry.db), `--dry-run`

---

## 5. Configuration Files

### models/manifest.json

Each config entry:

```json
{
  "id": "3090-qwen36-35b",
  "gpu": "3090",
  "model_name": "Qwen3.6-35B-A3B IQ4_XS",
  "model_path": "/data/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf",
  "draft_model": "/home/<user>/models/Qwen3.6-35B-A3B-DFlash-Q4_K_M.gguf",
  "port": 8080,
  "context_size": 128000,
  "thinking_enabled": true,
  "kvarn_level": "kvarn5",
  "speculative_type": "draft-dflash",
  "expected_vram_mb": 18800,
  "notes": "Fastest MoE, 128K ctx with KVarN"
}
```

### configs/*.env

Environment variables for `docker-compose`. Key variables:

| Variable | Purpose |
|----------|---------|
| `MODEL_DIR` | Model file directory |
| `MODEL` | Model file name |
| `DRAFT_MODEL` | Draft model file (speculative decoding) |
| `PORT` | Server listen port |
| `CTX` | Context window size |
| `THREADS` | Inference thread count |
| `CACHE_K` | KVarN cache K level (`kvarn2`–`kvarn5`, `f16`) |
| `CACHE_V` | KVarN cache V level (`kvarn2`–`kvarn5`, `f16`) |
| `SPEC_TYPE` | Speculative decoding type (`draft-dflash`, `draft-mtp`, or empty) |
| `EXTRA_ARGS` | Additional flags (`--cpu-moe`, `-fit off`, `--no-mmap`) |

### configs/benchmark-manifest.json

Defines which configs to test, which tasks to run, and execution order.

---

## 6. Structured Output Contracts

### orchestrate.py JSON Output

```json
{
  "campaign": "beellama-benchmark",
  "configs_run": ["3090-qwen36-35b", "3090-muse-glimmer"],
  "total_runs": 138,
  "completed": 138,
  "failed": 0,
  "duration_seconds": 2847,
  "results_db": "benchmark-results.db"
}
```

### report.py JSON Output

```json
{
  "per_role": {
    "orchestrator": {
      "configs": {
        "3090-qwen36-35b": {"median_overall": 7.2, "iqr": 1.1},
        "3090-muse-glimmer": {"median_overall": 8.1, "iqr": 0.8}
      }
    },
    "coder": {}
  },
  "recommendation": {
    "orchestrator": "3090-muse-glimmer",
    "coder": "3090-qwen36-35b",
    "rationale": "Muse-Glimmer scores higher on orchestration tasks..."
  }
}
```

### deploy-state-machine.py Status Output

```json
{
  "state": "READY",
  "config": "config-i",
  "primary_healthy": true,
  "helper_healthy": true,
  "containers": ["beellama-benchmark-llama-3090-config-i-1", "beellama-benchmark-llama-3070-1"]
}
```
