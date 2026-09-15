# Coder Harness — AGENTS.md

> Master reference for the Coder Harness Benchmark Platform: a self-scoring, crash-resilient system for evaluating local LLM models on a dual-GPU Triton machine, with an autonomous coding engine (ACE) that executes PRDs end-to-end.

## 1. Project Overview

Coder Harness benchmarks local LLM inference across model, GPU, and context-size combinations on a dual-GPU Triton machine (RTX 3090 24GB + RTX 3070 8GB). It sends standardized tasks to BeeLlama endpoints, scores responses with an automated judge model, tracks everything in SQLite, and generates comparison reports with scatter plots. The system is crash-resilient: a checkpoint manager saves progress every N runs and resumes from the last good state, while a GPU watchdog polls temperatures and aborts on hardware failure.

**The Autonomous Coding Engine (ACE)** parses PRDs into tasks, generates code via BeeLlama inference, runs quality gates, tests, and commits to Gitea — all end-to-end with zero human intervention.

> **New to ACE?** Read [ACE-RUNBOOK.md](ACE-RUNBOOK.md) first — it routes every recipe to the authoritative doc that owns the detail. Note: the engine service authenticates with `Authorization: Token <secret>` (not `Bearer`), and the live CLI is `python3 cli.py ace ...` (`harness.py` is legacy).

**The Config Capability Benchmark Suite (CCBS)** is a two-layer system for discovering, benchmarking, and ranking model configs on the dual-GPU Triton machine. It scans model directories, classifies models by role (orchestrator/worker), suggests optimal setups, generates viable configurations, runs benchmarks, and produces comparison reports. CCBS is deployed and running on Triton at `/home/<user>/ccbs/`.

**Location:** `/home/<user>/projects/ace-engine/`
**Parent project:** `dsh-hub` — see `orchestration-layer-design.md` and `docs/blueprints/` for the broader DSH campaign and orchestration model.

### Key Architecture Decision: Docker on Triton

The engine runs inside a Docker container on Triton with `network_mode: host`, eliminating all SSH overhead:
- `localhost:8080` → BeeLlama 3090 (direct HTTP)
- `localhost:8082` → BeeLlama 3070 (direct HTTP)
- `localhost:3000` → Gitea (direct HTTP)
- Volume mount for project files

See `docs/docker-architecture.md` and `transport.py` for details.

---

## 2. Quick Start

```bash
cd /home/<user>/projects/ace-engine

# 1. Run preflight health checks
python3 preflight.py

# 2. Dry-run the full pipeline (no inference, no SSH)
python3 orchestrate.py --phase 1 --dry-run

# 3. Run a single config against all tasks
python3 orchestrate.py --config 3090-qwen35-9b

# 4. Run the methodology pilot (5 tasks × 3 configs × 3 reps = 45 runs)
python3 pilot.py

# 5. Generate the comparison report
python3 report.py --format md
```

---

## 3. Architecture

### Module Dependency Graph

```
orchestrate.py  (CLI entry — sequences the pipeline)
├── preflight.py        (7-point health check)
│   └── transport.py    (HTTP/Local/SSH transport layer)
├── gpu_fit.py          (GPU fit matrix probe)
│   └── transport.py
├── runner.py           (inference execution + SQLite logging)
│   └── transport.py
├── judge.py            (automated scoring via judge model)
│   └── transport.py
├── watchdog.py         (background GPU temperature monitor)
│   └── transport.py
├── checkpoint.py       (crash recovery — save/resume progress)
└── report.py           (comparison tables + scatter plots)
    └── (matplotlib, sqlite3)

pilot.py                (methodology validation — standalone CLI)
├── runner.py
├── judge.py
└── transport.py

engine_service.py       (HTTP API service on port 3082 — FastAPI)
├── transport.py        (delegates to local execution)
├── code_generator.py
├── test_runner.py
├── git_workflow.py
└── quality_gates.py

engine_client.py        (HTTP client with retry logic — mirrors LocalTransport API)
└── transport.py        (used by HTTPTransport)

legacy/harness.py       (LEGACY master CLI — deprecated; live CLI is `python3 engine/cli.py ace ...`)
├── engine_service.py   (for `engine start`)
├── engine_client.py    (for `engine status`)
└── transport.py        (for all remote operations)

# Live CLI (replaces harness.py for engine/bench commands):
#   python3 engine/cli.py ace run|parse|status|resume|cancel
#   python3 engine/cli.py bench run|pilot|report
```

### Three-Tier Architecture

The system uses a three-tier approach for engine control:

```
Tier 1: Fix SSH (baseline)          Tier 2: HTTP API Service           Tier 3: Native Triton
├── sshpass install                 ├── engine_service.py (:3082)       ├── DSH runs on Triton
├── SSH key authentication          ├── FastAPI endpoints               ├── No remote comms needed
├── ControlMaster multiplexing      ├── /health, /inference, /exec     ├── Direct localhost access
└── Works everywhere today          └── /write-files, /run-tests       └── Optimal performance
                                    ├── /git-commit, /gpu-status
                                    └── /stream (SSE)

get_transport() priority:
  1. HTTPTransport  → engine_service.py on port 3082 (preferred)
  2. LocalTransport → direct subprocess/HTTP (Docker on Triton)
  3. RemoteTransport → SSH tunneling (nightmare → Triton fallback)
```

### Data Flow

```
corpus.json ──→ runner.py ──→ SQLite (benchmark_runs)
manifest.json ──→ runner.py ──→ SQLite (model_configs)
                              ↓
                        judge.py ──→ SQLite (judge_scores)
                              ↓
                        report.py ──→ reports/benchmark-report.md
                                   ──→ reports/scatter-*.png
```

### Pipeline Sequence

```
preflight → GPU fit → pilot (optional) → full run → scoring → report
     │                    │                   │          │
     │                    │                   │          └─ report.py
     │                    │                   └─ judge.py (per run)
     │                    └─ pilot.py (45 runs, go/no-go)
     └─ preflight.py (7 checks)
```

---

## 4. Module Reference

### `ssh_utils.py` — SSH + BeeLlama + nvidia-smi (261 lines)

The transport layer. All remote operations go through this module.

**Class:** `SSHClient`
- `connect(host, user, pw, key_path)` → `bool` — SSH connectivity test (3 attempts with exponential backoff)
- `run(cmd, timeout)` → `(stdout, stderr, exit_code)` — execute remote command
- `curl_beellama(port, messages, max_tokens, temperature, ...)` → `dict` — chat completion via BeeLlama `/v1/chat/completions`
- `get_nvidia_smi(gpu_index)` → `list[dict]` or `dict` — GPU memory, temperature, utilization
- `check_beellama_health(port)` → `bool` — `/health` endpoint check
- `get_model_list(port)` → `list[str]` — `/v1/models` endpoint

**Constants:** `TRITON_HOST=<LAN_IP>`, `TRITON_USER=<user>`, `PORT_3090=8080`, `PORT_3070=8082`

**SSH pattern:** SSH keys are configured on nightmare → Triton. Two modes:
- **Key-based (preferred):** `ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> {cmd}`
- **Password fallback:** `sshpass -p {pw} ssh -o StrictHostKeyChecking=no <user>@<LAN_IP> {cmd}` (requires `sshpass` installed)

The `SSHClient.connect(key_path=...)` method selects the mode. Without `key_path`, it falls back to `sshpass`.

**Beellama API format:**
```bash
curl -s -X POST http://localhost:{port}/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"..."}],"max_tokens":512,"temperature":0.3}'
```

### `preflight.py` — Pre-flight Health Check (174 lines)

**CLI:** `python3 preflight.py`
**Returns:** `dict[str, {status: pass|warn|fail, detail: str}]`

**7 checks:**
1. `ssh_connectivity` — SSH to Triton
2. `gpu_availability` — nvidia-smi (≥2 GPUs)
3. `beellama_3090_health` — `/health` on port 8080
4. `beellama_3070_health` — `/health` on port 8082
5. `disk_space` — ≥10GB on `/home/<user>/`
6. `system_load` — load average < 4.0
7. `stress_test` — 3 rapid API calls succeed

### `runner.py` — Benchmark Runner (501 lines)

**CLI:** `python3 runner.py --config <id> [--task <id>] [--dry-run] [--summary]`
**Class:** `BenchmarkRunner`

Key methods:
- `run_single(model_config_id, task_id, turn_index, repetition, dry_run)` → `run_id | plan_dict`
- `run_batch(model_config_id, task_ids, dry_run)` → `list[run_id]` — runs all tasks × 3 reps
- `run_config_matrix(config_ids, task_ids, dry_run)` → `dict[config_id → list[run_id]]`
- `get_completed_runs(model_config_id, task_id)` → `list[run_id]`
- `should_skip(model_config_id, task_id, ...)` → `bool`

**Default sampling:** temperature=0.3, top_p=0.95, top_k=40, max_tokens=2048

**DB init:** Idempotent — syncs `manifest.json` → `model_configs`, `corpus.json` → `tasks` on every instantiation.

### `judge.py` — Automated Scorer (417 lines)

**CLI:** `python3 judge.py --run-id <id> | --batch | --summary [--dry-run]`
**Class:** `JudgeScorer`

**Judge model:** Qwen3.5-9B on port 8082 (3070 GPU)

**5 scoring dimensions** (0–10 scale):
| Dimension | Method | Notes |
|-----------|--------|-------|
| completeness | Judge prompt | Checklist-based |
| correctness | Code execution | Automated test runs for coder tasks |
| quality | Judge prompt | Code quality assessment |
| intelligence | Judge prompt | Algorithm/data-structure selection |
| role_fit | Judge prompt | Task-role appropriateness |

**Process:** strips model identity (blinding), executes code tests for coder tasks, sends to judge model, parses JSON response.

**Rubric:** `rubric_anchors.md` — 7 dimension-specific anchor tables (0–10 scale)

### `gpu_fit.py` — GPU Fit Matrix Probe (524 lines)

**CLI:** `python3 gpu_fit.py [--config <id>] [--sizes 4096,8192,...] [--dry-run] [--show]`
**Class:** `GPUFitProbe`

For each (config × context_size) pair:
1. SSH → rewrite `.env` on Triton
2. Restart docker-compose
3. Poll `/health` until ready (180s timeout)
4. Send probe inference ("Say hello in one word.")
5. Read nvidia-smi for VRAM usage
6. Persist to `gpu_fit_matrix` table

**Default context sizes:** 4096, 8192, 32768, 65536, 131072

### `watchdog.py` — GPU Watchdog (158 lines)

**Class:** `GPUWatchdog`
- `start()` — background thread polls nvidia-smi every 30s
- `stop()` — stops polling
- `poll_once()` → `dict[gpu_index → status]`
- `is_aborted()` → `bool`
- `get_status_summary()` → `str`

**Abort triggers:** GPU disappears from nvidia-smi, or temperature > 83°C, or nvidia-smi query failure.

### `checkpoint.py` — Crash Recovery (162 lines)

**Class:** `CheckpointManager`
- `save_checkpoint()` — saves completed run IDs + per-config progress to `checkpoints` table
- `load_checkpoint()` → `dict | None`
- `resume_from_checkpoint()` → `{completed_run_ids, config_progress, should_resume}`
- `mark_run_complete(run_id)` — auto-checkpoints every N runs (default: 5)

### `orchestrate.py` — Main CLI Entry (274 lines)

**CLI:** `python3 orchestrate.py --phase <0|1> [--config <id>] [--resume] [--dry-run] [--db <path>] [--sizes <csv>]`
**Class:** `BenchmarkOrchestrator`

**Phases:**
- **Phase 0:** GPU fit matrix (all configs × context sizes)
- **Phase 1:** Full evaluation (all configs × all tasks + scoring)

**Features:** signal handlers (SIGINT/SIGTERM → checkpoint + exit), resume from checkpoint, GPU watchdog integration.

### `pilot.py` — Methodology Pilot (516 lines)

**CLI:** `python3 pilot.py [--dry-run] [--report]`
**Class:** `MethodologyPilot`

**Design:** 3 configs × 5 tasks × 3 reps = 45 runs

**Configs:** `3090-qwen36-35b`, `3070-qwen35-9b`, `3090-qwen35-9b`
**Tasks:** T01 (easy/orch), T05 (hard/orch), T14 (easy/coder), T18 (hard/coder), T22 (medium/multi-turn)

**Metrics:** within-model variance (std dev), between-model effect sizes (Cohen's d), judge consistency (avg std)

**Verdict thresholds:** variance σ < 2.0, separation d > 0.3, judge std < 1.5

### `report.py` — Benchmark Reporter (591 lines)

**CLI:** `python3 report.py [--db <path>] [--format md|json]`
**Class:** `BenchmarkReporter`

**Outputs:**
- `reports/benchmark-report.md` — per-role comparison tables, speed summary, pairwise matrix, recommendations
- `reports/benchmark-report.json` — machine-readable equivalent
- `reports/scatter-*.png` — speed vs quality scatter plots (requires matplotlib)

---

## 5. Configuration Guide

### Model Configurations

> **Authoritative source:** `beellama-kvarn-deploy/CONFIG-INDEX.md` — this table is a snapshot and may lag the running system.

Defined in `models/manifest.json` (6 configs):

| Config ID | GPU | Model | VRAM | Context | Speculative | KVarN |
|-----------|-----|-------|------|---------|-------------|-------|
| `3090-qwen36-35b` | 3090 | Qwen3.6-35B-A3B IQ4_XS | 18.8 GB | 128K | DFlash | kvarn5 |
| `3090-muse-glimmer` | 3090 | Muse-Glimmer-30B Q3_K_XL | 15.9 GB | 32K | DFlash2 | — |
| `3090-laguna-xs` | 3090 | Laguna-XS-2.1 APEX-i-quality | 19.1 GB | 8K | none | — |
| `3090-qwen35-9b` | 3090 | Qwen3.5-9B-MTP Q4_K_M | 8.0 GB | 140K | DFlash | kvarn5 |
| `3070-qwen35-9b` | 3070 | Qwen3.5-9B-MTP Q4_K_M | 6.9 GB | 8K | DFlash | kvarn2 |
| `3070-qwen35-4b` | 3070 | Qwen3.5-4B Q5_K_M | 4.0 GB | 32K | — | kvarn2 |

### Docker Compose Profiles

Defined in `tickets/deploy/docker-compose.yml` (6 profiles + shared 3070 service):

| Profile | 3090 Service | Model | Speed | Context |
|---------|-------------|-------|-------|---------|
| `config-h` | `llama-3090-config-h` | Muse-Glimmer-30B + DFlash2 | 58 tok/s | 128K |
| `config-i` | `llama-3090-config-i` | Qwen3.6-35B-A3B + DFlash | 197 tok/s | 128K |
| `config-g-laguna` | `llama-3090-config-g-laguna` | Laguna-XS-2.1 | 181 tok/s | 8K |
| `config-g-glm` | `llama-3090-config-g-glm` | GLM-4.7-Flash 23B-A3B | TBD | 32K |
| `config-h-coder` | `llama-3090-config-h-coder` | Qwen3-Coder-30B + cpu-moe | TBD | 128K |
| `config-h-qwen35` | `llama-3090-config-h-qwen35` | Qwen3.5-9B-MTP + DFlash | 153 tok/s | 140K |

**Shared 3070 service:** `llama-3070` — Qwen3.5-9B-MTP Q4_K_M + DFlash-MTP, port 8082, always active.

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
**To switch models:** Use the state machine or manually swap docker profiles, then run Python benchmarks against the now-running model.

### .env Files

Located at `tickets/deploy/configs/`. One per profile (e.g., `config-i.env`). Contains `MODEL_PATH`, `CONTEXT_SIZE`, `DRAFT_MODEL` variables.

### VRAM Budget Reference

**RTX 3090 (24 GB):**

| Config | Weights | KV Cache | Spec Draft | Total | Headroom |
|--------|---------|----------|------------|-------|----------|
| config-h (Muse-Glimmer) | 9.8 GB | 2.5 GB | 2.8 GB | 15.1 GB | 8.9 GB ✅ |
| config-i (Qwen3.6-35B) | 17.0 GB | 1.8 GB | 0.2 GB | 19.0 GB | 5.0 GB ✅ |
| config-g-laguna | 15.0 GB | 6.0 GB | 0 | 21.0 GB | 3.0 GB ⚠️ |
| config-g-glm | 12.5 GB | 5.0 GB | 0 | 17.5 GB | 6.5 GB ✅ |
| config-h-coder | 5.5 GB | 6.5 GB | 0 | 12.0 GB | 12.0 GB ✅ |
| config-h-qwen35 | 5.5 GB | 1.0 GB | 0.9 GB | 7.4 GB | 16.6 GB ✅ |

**RTX 3070 (8 GB):**

| Config | Total | Headroom |
|--------|-------|----------|
| Qwen3.5-9B + DFlash-MTP | 6.7 GB | 1.3 GB ✅ |

### KVarN Compatibility

Works on Qwen models. **NOT compatible** with GLM-4.7 or Laguna-XS (forced to f16 cache).

---

## 6. Running Benchmarks

### Step-by-step Pipeline

```bash
cd /home/<user>/projects/ace-engine

# Step 1: Preflight — verify SSH, GPUs, beellama, disk, load, stress
python3 preflight.py

# Step 2: GPU fit — which configs fit at which context sizes
python3 gpu_fit.py --sizes 4096,8192,32768,65536,131072
# Or dry-run first:
python3 gpu_fit.py --dry-run

# Step 3: Pilot — validate methodology (45 runs, ~25 min)
python3 pilot.py --dry-run   # preview plan
python3 pilot.py             # execute
# Review reports/pilot-report.md for go/no-go verdict

# Step 4: Full benchmark — all configs × all tasks × 3 reps
python3 orchestrate.py --phase 1
# Resume after crash:
python3 orchestrate.py --phase 1 --resume
# Single config:
python3 orchestrate.py --config 3090-qwen35-9b

# Step 5: Report — comparison tables + scatter plots
python3 report.py --format md    # → reports/benchmark-report.md
python3 report.py --format json  # → reports/benchmark-report.json
```

### Corpus: 23 Tasks

| IDs | Role | Count | Categories |
|-----|------|-------|------------|
| T01–T10 | orchestrator | 10 | ticket-understanding, multi-step-planning, verification, ambiguity-handling, prioritization, delegation-design, scope-management, risk-identification, status-assessment, rollback-planning |
| T11–T20 | coder | 10 | implementation, refactoring, debugging, code-generation, optimization, test-writing, api-design, data-structure-selection, error-handling, performance-tuning |
| T21–T23 | multi-turn | 3 | iterative-refinement (3 turns), code-review-feedback-loop (3 turns), design-evolution (4 turns) |

**Automated checks exist for:** T11, T13, T14, T15, T18, T20 (coder tasks with `automated_check` in corpus.json).

---

## 7. Triton Deployment

### Machine

| Property | Value |
|----------|-------|
| Host | `<LAN_IP>` |
| User | `<user>` |
| GPU 0 | RTX 3090 24 GB |
| GPU 1 | RTX 3070 8 GB |
| Docker | 29.6.1 |
| BeeLlama ports | 8080 (3090), 8082 (3070) |

### Deploy Workflow

```bash
# Transfer deployment files to Triton
scp -r tickets/deploy/ <user>@<LAN_IP>:~/dockers/beellama-benchmark/

# Or use the deploy script:
./tickets/deploy/scripts/deploy-to-host.sh --smoke-test
```

### Docker Compose Commands (on Triton)

```bash
cd ~/dockers/beellama-benchmark

# Start a config
docker compose --profile config-i up -d

# Health check
curl -sf http://localhost:8080/health  # 3090
curl -sf http://localhost:8082/health  # 3070

# Stop
docker compose --profile config-i down
```

### State Machine

`tickets/deploy/deploy-state-machine.py` (1185 lines) manages model swap lifecycle:

```
IDLE → STOPPING → LOADING → HEALTH_CHECK → READY → BENCHMARKING → SWAPPING → ERROR
```

```bash
python3 deploy-state-machine.py swap config-i       # Swap to config-i
python3 deploy-state-machine.py bench config-h      # Swap + benchmark
python3 deploy-state-machine.py status              # Current state
python3 deploy-state-machine.py history             # Swap history
python3 deploy-state-machine.py rollback            # Last known-good
```

### Benchmark Scripts (on Triton)

Located at `tickets/deploy/scripts/`:

| Script | Purpose |
|--------|---------|
| `bench-throughput.sh` | Throughput measurement (tok/s) |
| `bench-quality.sh` | Coding quality tasks |
| `bench-context.sh` | Context window stress test |
| `bench-all-configs.sh` | Full benchmark across all configs |
| `bench-response-quality.sh` | Response quality evaluation |

---

## 8. API Reference

### BeeLlama Chat Completions

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

**Response fields:** `choices[0].message.content`, `choices[0].message.reasoning_content`, `timings.predicted_per_second`, `timings.prompt_per_second`, `timings.predicted_ms`, `timings.prompt_ms`, `usage.total_tokens`

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

### SSH Connection Pattern

```bash
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> {cmd}
```

### SQLite Schema (8 tables)

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

#### Table: `model_configs`

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

#### Table: `tasks`

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

#### Table: `benchmark_runs`

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

#### Table: `judge_scores`

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

#### Table: `latency_profiles`

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

#### Table: `gpu_fit_matrix`

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

#### Table: `schema_version`

```sql
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

#### Table: `checkpoints` (runtime-only, created by `checkpoint.py`)

Not defined in `schema.sql`. Created at runtime by the `CheckpointManager` class to persist completed run IDs and per-config progress for crash recovery.

**Query examples:**
```sql
-- Get average throughput per config
SELECT model_config_id, AVG(predicted_per_second) AS avg_tps
FROM benchmark_runs WHERE status='complete'
GROUP BY model_config_id ORDER BY avg_tps DESC;

-- Get best config per coder role
SELECT br.model_config_id, AVG(js.overall) AS avg_score
FROM benchmark_runs br
JOIN judge_scores js ON br.id = js.run_id
WHERE br.status='complete' 
  AND br.task_id IN (SELECT id FROM tasks WHERE role='coder')
GROUP BY br.model_config_id ORDER BY avg_score DESC;

-- Get GPU fit data
SELECT model_config_id, gpu, context_size, fits, vram_used_mb
FROM gpu_fit_matrix ORDER BY model_config_id, context_size;
```

---

## 9. Agent Integration

### Tool Mappings for DSH Agents

| DSH Agent Need | Coder Harness Tool | Call Pattern |
|----------------|-------------------|--------------|
| Check if Triton is healthy | `preflight.py` | `python3 preflight.py` → parse JSON output |
| Run single benchmark task | `runner.py` | `python3 runner.py --config <id> --task <id>` |
| Score a completed run | `judge.py` | `python3 judge.py --run-id <id>` |
| Get GPU fit data | `gpu_fit.py` | `python3 gpu_fit.py --show` |
| Generate report | `report.py` | `python3 report.py --format json` → parse JSON |
| Check model availability | `ssh_utils.py` | `SSHClient.get_model_list(port)` |

### Structured Outputs

All modules emit structured data suitable for agent consumption:

- **preflight:** `dict[str, {status, detail}]` — pass/fail per check
- **runner dry-run:** `list[{config, task, port, prompt_preview, params}]`
- **judge scores:** `list[{run_id, scores, correctness, execution_result}]`
- **report JSON:** `reports/benchmark-report.json` — full structured report
- **pilot verdict:** `dict{verdict, variance_ok, separation_ok, judge_ok, ...}`

### Database Access

Agents can query `benchmark-results.db` directly:

```python
import sqlite3
conn = sqlite3.connect('benchmark-results.db')
conn.row_factory = sqlite3.Row

# Get all scored runs
rows = conn.execute("""
    SELECT br.*, js.overall, js.quality, js.intelligence
    FROM benchmark_runs br
    JOIN judge_scores js ON br.id = js.run_id
    WHERE br.status = 'complete'
""").fetchall()
```

### Parent Project Integration

- **Orchestration layer:** `orchestration-layer-design.md` — DSH middle layer for routing, fleet health, and configuration
- **Campaign blueprints:** `docs/blueprints/` — campaign state machine, runbook, token analysis, FMD model mapping
- **Campaign state machine:** `docs/blueprints/campaign-state-machine-v2.md` — formal 13-state model

### Agent Execution Rails

Worktree isolation, preflight gates, and endpoint contracts for agent-driven
engine runs. See `docs/agents/agent-rails.md` (full contract) and
`scripts/agent-worktree.sh` (worktree bootstrap).

- **Worktree:** `scripts/agent-worktree.sh <branch>` creates an isolated
  `../ace-engine-<suffix>` worktree. Work ONLY there; push when done.
- **Preflight:** Never run the engine without `deploy/host/preflight.py`
  PASS. A 200 with the wrong model name is an explicit FAIL.
- **Endpoints:** subject <LAN_IP>:8082 (Qwen3.5-9B), judge <LAN_IP>:8080
  (Qwen3.6-35B), Gitea <LAN_IP>:3000. Override via TRITON_*_URL env vars.
- **Invalid run:** kill process, annotate log (never delete), investigate.
- **Commits:** one logical change per commit; suite green before commit.

---

## 10. Troubleshooting

### SSH Connection Fails

```bash
# Test SSH directly
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> echo OK

# If key-based auth fails, check key permissions
chmod 600 ~/.ssh/id_rsa
```

### Container Won't Start

```bash
# On Triton:
docker compose --profile config-i logs
ss -tlnp | grep 8080   # Check port is free
nvidia-smi              # Check GPU memory
docker ps -a | grep beellama
```

### Health Check Timeout

- Model loading takes 60–120s for large models (35B+)
- Check `docker compose --profile config-X logs` for load errors
- Verify model file exists: `ls -lh /data/models/MODEL-NAME.gguf`

### OOM on 3090

- Lower `CONTEXT_SIZE` in .env
- Use more aggressive KVarN (kvarn4 instead of kvarn5)
- Switch to a smaller model
- Use `--cpu-moe` flag for MoE models (config-h-coder)

### KVarN / Speculative Draft Errors

- KVarN works on Qwen models only. GLM-4.7 and Laguna-XS are forced to f16.
- Laguna-XS crashes with DFlash draft (SIGSEGV) — use `speculative_type: none`

### Benchmark Interrupted (SIGINT/SIGTERM)

```bash
# Resume from last checkpoint:
python3 orchestrate.py --phase 1 --resume
```

### Checkpoint Recovery

The `checkpoint` table in SQLite tracks completed runs. On resume:
1. `running` status runs → marked `incomplete`
2. Completed runs are skipped
3. Benchmark continues from where it left off

---

## 11. File Inventory

### Core Platform (Python)

| File | Lines | Purpose |
|------|-------|---------|
| `orchestrate.py` | 274 | Main CLI entry — sequences full pipeline |
| `runner.py` | 501 | Inference execution + SQLite logging |
| `judge.py` | 417 | Automated scoring via judge model (3070 port 8082) |
| `gpu_fit.py` | 524 | GPU fit matrix probe (configs × context sizes) |
| `pilot.py` | 516 | Methodology pilot (5 tasks × 3 configs × 3 reps) |
| `report.py` | 591 | Comparison tables + scatter plots |
| `preflight.py` | 174 | 7-point health check |
| `watchdog.py` | 158 | Background GPU temperature monitor |
| `checkpoint.py` | 162 | Crash recovery — save/resume progress |
| `ssh_utils.py` | 261 | SSH + BeeLlama + nvidia-smi transport layer |

### Autonomous Coding Engine (Python)

| File | Lines | Purpose |
|------|-------|---------|
| `harness.py` | 893 | **LEGACY** master CLI (`legacy/harness.py`, deprecated) — live CLI is `python3 engine/cli.py ace|bench ...` |
| `task_queue.py` | 1538 | DAG task orchestration with dependency ordering |
| `code_generator.py` | 1411 | BeeLlama code generation with retry + local detection |
| `context_manager.py` | 505 | Project file tracking and context building |
| `quality_gates.py` | 607 | AST validation, imports, style, security |
| `test_runner.py` | 904 | Pytest/unittest execution and parsing |
| `git_workflow.py` | 414 | Branch/commit/push/PR workflow |
| `prd_parser.py` | 396 | PRD → module-grouped task decomposition |
| `prompt_templates.py` | 726 | 7 task-type templates with auto-detection |
| `file_ops.py` | 693 | File operations via SSH/local |
| `demo_e2e.py` | 709 | Full pipeline demo (--dry-run verified) |

### Transport & Engine Service

| File | Lines | Purpose |
|------|-------|---------|
| `transport.py` | ~550 | Three-transport abstraction (HTTP/Local/SSH) |
| `engine_service.py` | 1265 | FastAPI HTTP server on port 3082 |
| `engine_client.py` | ~150 | HTTP client with retry logic, mirrors LocalTransport API |
| `Dockerfile.engine` | 41 | Engine container (Python 3.12-slim, no GPU) |
| `docker-compose.engine.yml` | 49 | Engine compose with host networking |
| `.dockerignore` | — | Build context cleanup |
| `scripts/deploy-engine.sh` | — | Build + deploy + smoke test |
| `docs/dsh-docker-audit-report.md` | — | DSH Docker audit: gap analysis, build plan, readiness |

#### Engine Service Endpoints (`engine_service.py`)

FastAPI HTTP server running on port 3082. Exposes the following endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check — returns `{"status": "ok"}` |
| `/inference` | POST | Send inference request to BeeLlama (messages, max_tokens, temperature) |
| `/write-files` | POST | Write files to project directory on Triton |
| `/run-tests` | POST | Execute test suite for a project |
| `/git-commit` | POST | Create git commit with specified files |
| `/exec` | POST | Execute arbitrary shell command |
| `/gpu-status` | GET | Query nvidia-smi for GPU stats |
| `/stream` | GET | SSE stream for real-time events |

**Systemd unit:** `engine-service.service` — auto-restart on failure, starts on boot.

```bash
# Start/stop/status (legacy harness.py — still works, routes to engine_service.py)
python3 harness.py engine start [--port 3082] [--token <secret>]
python3 harness.py engine stop
python3 harness.py engine status

# Or via systemd on Triton
sudo systemctl start engine-service
sudo systemctl status engine-service
```

> The `harness.py engine *` commands above route through `legacy/harness.py` (deprecated). The live equivalent is `python3 engine/cli.py ace ...`; for raw service management use `python3 engine_service.py` directly or systemd.

#### Docker Status (audited 2026-09-03)

**Engine Docker** (`coder-engine:latest`): ✅ Built, 134 MB, Python 3.12-slim, works with `transport.py` LocalTransport. Can run `harness.py` commands, talk to BeeLlama on localhost:8080/8082, and push to Gitea on localhost:3000.

**DSH Docker**: ❌ Does not exist. DSH is built and working natively on Triton (Node.js 22, pnpm, all packages compiled) but has never been containerized. There is no `Dockerfile` in the deepseek-harness repository.

**Gap:** The coder-engine Docker is Python-only. It cannot run DSH agent loops, the `meta-cognitive-ralph-loop` skill, or any Node.js-based DSH functionality. To integrate DSH with the engine, a new hybrid container (Node.js + Python) or two-container setup is needed. See `docs/dsh-docker-audit-report.md` for full analysis.

### Telemetry & Streaming

| File | Lines | Purpose |
|------|-------|---------|
| `telemetry_collector.py` | 448 | Active GPU/event collection |
| `telemetry_dashboard.py` | 1446 | Self-contained HTML dashboard |
| `telemetry_schema.py` | ~200 | Extended schema (6 new tables) |
| `telemetry_models.py` | 572 | Typed dataclasses for telemetry |
| `telemetry_config.py` | 550 | Centralized configuration |
| `schema_unified.py` | 138 | Canonical SQLite schema |
| `streaming_server.py` | 42 | Engine-generated SSE server (FastAPI) |

### Remote Control & Infrastructure

| File | Lines | Purpose |
|------|-------|---------|
| `remote_control.py` | 1088 | Unified Triton controller |
| `work_engine.py` | 1440 | Full pipeline with Docker sandboxes |
| `work_engine_simple.py` | 731 | Simplified: no Docker, direct SSH+curl |
| `sandbox_manager.py` | 588 | Docker sandbox lifecycle |
| `gitea_utils.py` | 663 | Gitea API client via SSH tunnel |
| `remote_control.py` | 1088 | Unified SSH controller |

### Data Files

| File | Lines | Purpose |
|------|-------|---------|
| `schema.sql` | 138 | SQLite DDL: 7 tables |
| `corpus.json` | 305 | 23 benchmark tasks (T01–T23) |
| `corpus-real.json` | 217 | 15 real-project tasks (RT01–RT15) |
| `rubric_anchors.md` | 113 | Scoring anchors for 7 dimensions |
| `models/manifest.json` | 88 | 6 model configurations |

### PRDs & Specs

| File | Lines | Purpose |
|------|-------|---------|
| `prd-telemetry-engine.md` | 225 | Telemetry engine PRD (54 user stories) |
| `spec-telemetry-engine.md` | 303 | Telemetry engine technical spec |
| `user-stories-telemetry-engine.md` | 77 | 54 user stories across 10 categories |
| `prd-live-streaming.md` | 350 | Live streaming PRD (15 user stories) |
| `spec-live-streaming.md` | 200 | Streaming technical spec |
| `inner-streaming-design.md` | 560 | DSH SSE Bridge Plugin design |
| `prd-streaming-mvp.md` | 57 | Streaming MVP (minimal, 3 tasks) |

### Documentation

| File | Lines | Purpose |
|------|-------|---------|
| `AGENTS.md` | 1300+ | This file — master reference |
| `docs/docker-architecture.md` | 612 | Docker containerization architecture |
| `docs/API-REFERENCE.md` | 689 | All CLI commands documented |
| `docs/remote-control.md` | 653 | Remote control usage guide |
| `docs/workflow-state-machine.md` | 478 | State machine documentation |
| `ENGINE-AUDIT.md` | 278 | Honest engine audit |
| `README.md` | 466 | Auto-generated project README |
| `CONTRIBUTING.md` | 273 | Contribution guidelines |

### Deployment (Triton)

| File | Lines | Purpose |
|------|-------|---------|
| `tickets/deploy/docker-compose.yml` | 300 | 6 profiles + shared 3070 service |
| `tickets/deploy/deploy-state-machine.py` | 1185 | Model swap lifecycle state machine |
| `tickets/deploy/README.md` | 148 | Deployment documentation |
| `tickets/deploy/scripts/bench-throughput.sh` | 168 | Throughput measurement |
| `tickets/deploy/scripts/bench-quality.sh` | 155 | Coding quality tasks |
| `tickets/deploy/scripts/bench-context.sh` | 221 | Context stress test |
| `tickets/deploy/scripts/bench-all-configs.sh` | 216 | Full benchmark runner |
| `tickets/deploy/scripts/bench-response-quality.sh` | 158 | Response quality evaluation |
| `tickets/deploy/scripts/deploy-to-host.sh` | 204 | Transfer + smoke test |
| `tickets/deploy/configs/*.env` | 41 each | Per-profile environment files (6 files) |
| `tickets/deploy/configs/benchmark-manifest.json` | 148 | Task×Config matrix |

### Tests

| File | Lines | Purpose |
|------|-------|---------|
| `test_integration.py` | 1107 | 62 integration tests across 14 classes |
| `test_prompts.py` | 674 | 69 prompt engineering tests |

### Reports (Generated)

| File | Purpose |
|------|---------|
| `reports/benchmark-report.md` | Markdown comparison report |
| `reports/benchmark-report.json` | Machine-readable report |
| `reports/scatter-*.png` | Speed vs quality scatter plots |
| `reports/pilot-report.md` | Pilot methodology report |
| `reports/telemetry-dashboard.html` | Self-contained HTML telemetry dashboard |

---

## 12. Remote Control

> **Legacy section:** This section documents the `harness.py`-based remote control layer, which lives in `legacy/harness.py` (deprecated). The live CLI is `python3 engine/cli.py ace|bench ...`. Commands below still work but route through the legacy module — prefer `engine/cli.py` for new work.

The remote control layer provides a unified interface for all Triton operations — model management, benchmarks, sandboxes, projects, and telemetry — exposed through two entry points: `harness.py` (thin CLI router) and `remote_control.py` (full-featured controller).

### `harness.py` — Single Entry Point (730 lines)

**CLI:** `python3 harness.py <command> [subcommand] [options]`

A thin router that delegates every subcommand to the existing module that owns the logic. No duplication — just a dispatcher with lazy imports.

**Global flags:**
| Flag | Description |
|------|-------------|
| `--pretty` | Pretty-print JSON output |
| `--verbose`, `-v` | Enable debug logging |
| `--version` | Show version |

**Commands:**

| Command | Delegates to | Description |
|---------|-------------|-------------|
| `status` | `remote_control.py` | Full system status (model, GPU, sandboxes) |
| `health` | `preflight.py` | Quick 7-point preflight health check |
| `setup` | *(built-in)* | Print first-time setup guide |
| `bench throughput -c <id>` | `remote_control.py` | Throughput benchmark (tok/s) |
| `bench quality -c <id>` | `remote_control.py` | Quality benchmark (scoring) |
| `bench all --configs <csv>` | `remote_control.py` | Benchmarks across multiple configs |
| `bench orchestrate --phase <0\|1>` | `orchestrate.py` | Full orchestrator pipeline |
| `bench pilot` | `pilot.py` | Methodology pilot (45 runs) |
| `run -p <project> -t <task>` | `work_engine.py` | Execute a coding task (full pipeline) |
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
| `engine start [--port] [--token]` | `engine_service.py` | Start engine HTTP service on port 3082 |
| `engine stop` | `engine_service.py` | Stop the engine HTTP service |
| `engine status` | `engine_client.py` | Check engine service health and uptime |

**Examples:**
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

### `remote_control.py` — Unified Controller (1088 lines)

**CLI:** `python3 remote_control.py <group> <action> [options]`
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
- `model_list()` → `list[dict]` — All known model configs
- `bench_throughput(config_id, reps, dry_run)` → `dict` — Run throughput benchmark on Triton
- `bench_quality(config_id, tasks, dry_run)` → `dict` — Run quality benchmark
- `bench_all(config_ids, dry_run)` → `list[dict]` — Benchmarks across multiple configs
- `sandbox_create(project, name, dry_run)` → `dict` — Create Docker sandbox on Triton
- `sandbox_exec(name, command, timeout)` → `dict` — Execute command in sandbox
- `sandbox_collect(name, remote_path, local_path)` → `dict` — Copy files from sandbox to nightmare
- `sandbox_destroy(name, dry_run)` → `dict` — Destroy a sandbox
- `project_status()` → `list[dict]` — List projects on Triton
- `project_sync(local_path, remote_path, dry_run)` → `dict` — Sync local project to Triton via rsync
- `telemetry_dashboard()` → `str` — Generate HTML dashboard
- `telemetry_export()` → `dict` — Export SQLite benchmark database to JSON
- `system_health()` → `dict` — Full health check (SSH, GPU, BeeLlama, disk, Docker, load)
- `system_gpu()` → `dict` — Detailed GPU status with process info
- `system_logs(lines)` → `dict` — Recent container logs from Triton

**SSH transport:** Uses key-based SSH (no sshpass, no paramiko). All commands run via `ssh -o StrictHostKeyChecking=no <user>@<LAN_IP> {cmd}`.

**Model swap strategy:**
1. Try `deploy-state-machine.py swap <profile>` first (state machine)
2. Fall back to direct `docker compose --profile <profile> up -d`
3. Poll `/health` until ready (max 180s timeout)

### `work_engine.py` — Full Pipeline Orchestration (1477 lines)

**CLI:** `python3 work_engine.py <command> [options]`
**Class:** `WorkEngine`

Orchestrates the complete remote work pipeline by connecting all existing tools: SSH → model swap → Docker sandbox → inference → Gitea push → telemetry.

**Commands:**

| Command | Description |
|---------|-------------|
| `run --project <p> --task <t>` | Execute a coding task (full 7-step pipeline) |
| `bench --project <p> --tasks <types>` | Run benchmarks inside a sandbox |
| `sessions [--status <s>]` | List work sessions |
| `collect --name <session>` | Collect results from a completed session |
| `cleanup` | Destroy all harness sandboxes |
| `status` | System status: model, GPU, sandboxes, telemetry, Gitea |

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

**CLI:** `python3 sandbox_manager.py <command> [options]`
**Class:** `SandboxManager`

Manages ephemeral Docker containers on Triton for coding work. Each sandbox is a `ubuntu:24.04` container with project code volume-mounted.

**Commands:**

| Command | Description |
|---------|-------------|
| `create --project <p> [--name <n>]` | Create a new sandbox |
| `list` | List active harness sandboxes |
| `exec --name <n> --command <cmd>` | Execute command in sandbox |
| `collect --name <n> --from <path> --to <path>` | Copy files from sandbox to local |
| `destroy --name <n>` | Destroy a sandbox |
| `destroy-all` | Destroy all harness sandboxes |
| `report` | Generate telemetry summary report |

**Sandbox naming:** All containers are prefixed with `harness-` (e.g., `harness-test-01`). The manager strips/prefixes automatically.

**Container setup:**
- Base image: `ubuntu:24.04`
- Volume mounts: `~/projects/<project>` → `/workspace` (read-only), `~/models` → `/models` (read-only)
- Auto-installs: python3, pip, git, curl, jq

---

## 13. Gitea Integration

Coder Harness integrates with a Gitea instance running on Triton (`http://localhost:3000`) for source code management. All Gitea API calls are tunnelled through SSH since Gitea only listens on localhost inside the Triton host.

### `gitea_utils.py` — Gitea API Client (663 lines)

**CLI:** `python3 gitea_utils.py <command> [options]`
**Class:** `GiteaClient`

**Authentication:** Personal access token (`<GITEA_TOKEN>`) passed via `Authorization: token <token>` header.

**Commands:**

| Command | Description |
|---------|-------------|
| `list-repos` | List all repositories for the authenticated user |
| `get-repo <name>` | Get details for a repository |
| `create-repo <name> [-d desc]` | Create a new repository |
| `delete-repo <name>` | Delete a repository |
| `list-branches <repo>` | List branches in a repo |
| `create-branch <repo> <branch> [--from <src>]` | Create a new branch |
| `delete-branch <repo> <branch>` | Delete a branch |
| `get-file <repo> <path> [--branch <b>]` | Get file content (base64-decoded) |
| `create-file <repo> <path> <content> -m <msg>` | Create or update a file |
| `list-files <repo> [--path <p>]` | List files in a directory |
| `list-commits <repo> [--branch <b>] [--limit N]` | List recent commits |
| `get-commit <repo> <sha>` | Get commit details |
| `clone <repo> <target_dir>` | Clone a repo on Triton |
| `push <local_dir> <repo> [--branch <b>]` | Push local changes to Gitea |
| `search <query> [--repo <r>]` | Search code across repos |

**Key methods (GiteaClient class):**
- `list_repos()` → `list` — All repos for authenticated user
- `create_repo(name, description, auto_init)` → `dict` — Create new repo
- `delete_repo(name)` → `bool` — Delete repo
- `list_branches(repo)` → `list` — List branches
- `create_branch(repo, branch, from_branch)` → `dict` — Create branch (via git over SSH)
- `get_file(repo, path, branch)` → `dict` — Get file content (auto base64-decode)
- `create_file(repo, path, content, message, branch)` → `dict` — Create/update file (auto SHA detection for updates)
- `list_files(repo, path, branch)` → `list` — List directory contents
- `clone(repo, target_dir)` → `bool` — Clone with token-authenticated HTTP URL
- `push(local_dir, repo, branch)` → `bool` — Push to token-authenticated remote
- `search_code(query, repo)` → `list` — Search code

**Architecture:** Every API call is tunnelled through SSH:
```
nightmare → SSH → Triton → curl localhost:3000/api/v1/...
```

### `projects.yaml` — Project Registry

Defines which projects exist on Triton and how to work with them. Used by the work engine to locate projects, select entry points, and run tests.

**Projects registered:**

| Project | Language | Gitea Repo | Benchmark Configs |
|---------|----------|------------|-------------------|
| `coder-harness` | python | `<user>/coder-harness` | `3090-qwen36-35b`, `3090-qwen35-9b` |
| `pulsar-harness` | rust | `<user>/pulsar-harness` | `3090-qwen36-35b` |
| `dsh-workspace` | typescript | `<user>/dsh-workspace` | `3090-qwen36-35b`, `3090-muse-glimmer` |
| `deepseek-harness` | typescript | — | `3090-qwen36-35b` |

**Default settings:**
- `model_config`: `3090-qwen36-35b`
- `max_tokens`: 2048
- `temperature`: 0.3
- `sandbox_prefix`: `harness`
- `cleanup_after_hours`: 24

### Repos on Triton

All project repos live at `/home/<user>/projects/<name>` on Triton. The work engine volume-mounts these into Docker sandboxes at `/workspace`. Gitea serves as the remote, with clone/push using token-authenticated HTTP URLs.

---

## 14. Transport Layer

The transport layer (`transport.py`, ~550 lines) abstracts local vs remote execution with a **three-transport architecture**: HTTPTransport (primary), LocalTransport (fallback), and RemoteTransport (SSH fallback).

### `transport.py` — Three-Transport Abstraction

**Detection:** Checks for `/.dockerenv` or `CONTAINER=docker` env var, and `ENGINE_SERVICE_URL` / `TRANSPORT_MODE` env vars to determine execution environment.

**Classes:**
- `HTTPTransport` — **Primary.** Delegates all operations to `engine_service.py` via HTTP. Uses `engine_client.py` with retry logic. Connects to `ENGINE_SERVICE_URL` (default: `http://<LAN_IP>:3082`).
- `LocalTransport` — **Fallback.** Used inside Docker on Triton. Direct HTTP calls, local subprocess.
- `RemoteTransport` — **SSH fallback.** Used on nightmare when HTTP service is unavailable. SSH tunneling to Triton.

**Factory:** `get_transport()` → auto-detects and returns appropriate transport:
1. **HTTPTransport** → if `engine_service.py` is reachable on port 3082 (preferred)
2. **LocalTransport** → if running inside Docker on Triton (direct subprocess)
3. **RemoteTransport** → SSH tunneling (nightmare → Triton fallback)

**Override:** Set `TRANSPORT_MODE` env var to force a specific mode:
- `http` → always use HTTPTransport
- `local` → always use LocalTransport
- `ssh` → always use RemoteTransport
- `auto` → auto-detect (default)

**Key methods (all transports):**
- `check_beellama_health(port)` → `bool` — Health check via HTTP
- `curl_beellama(port, messages, max_tokens, ...)` → `dict` — Chat completion
- `get_model_list(port)` → `list[str]` — Available models
- `run_command(cmd, timeout, cwd)` → `(stdout, stderr, rc)` — Execute command
- `run_git(repo_path, *args)` → `(stdout, stderr, rc)` — Git operations
- `docker_exec(container, cmd)` → `(stdout, stderr, rc)` — Docker exec
- `nvidia_smi(gpu_index)` → `dict` — GPU stats
- `write_file(path, content)` → `bool` — Write file (HTTP: POST /write-files)
- `run_tests(project, test_path)` → `dict` — Run tests (HTTP: POST /run-tests)
- `git_commit(repo_path, message, files)` → `dict` — Git commit (HTTP: POST /git-commit)

**Environment variables:**
| Variable | Default | Description |
|----------|---------|-------------|
| `ENGINE_SERVICE_URL` | `http://<LAN_IP>:3082` | Engine HTTP service endpoint |
| `TRANSPORT_MODE` | `auto` | Forced transport mode: `http`, `local`, `ssh`, `auto` |
| `TRITON_HOST` | `<LAN_IP>` | Triton SSH host (remote mode) |
| `TRITON_USER` | `<user>` | SSH user |
| `BEE_LLAMA_3090` | `http://localhost:8080` | BeeLlama 3090 endpoint |
| `BEE_LLAMA_3070` | `http://localhost:8082` | BeeLlama 3070 endpoint |
| `GITEA_URL` | `http://localhost:3000` | Gitea endpoint |
| `GITEA_TOKEN` | (empty) | Gitea API token |

### Engine Module SSH Dependencies

| Module | SSH Purpose | Transport Fix Status |
|--------|-------------|---------------------|
| `code_generator.py` | BeeLlama curl, file writes | ✅ Fixed (transport.py) |
| `test_runner.py` | Run pytest on Triton | ✅ Fixed (transport.py) |
| `git_workflow.py` | Git commit/push | ✅ Fixed (transport.py) |
| `context_manager.py` | Read project files | ✅ Fixed (transport.py) |

---

## 15. Live Streaming (PRD: prd-live-streaming.md)

A live streaming system for real-time visibility into both DSH agent activity and engine orchestration.

### Architecture

```
DSH (Node.js :3080) ──POST /events──→ Streaming Server (:3081) ──SSE──→ Browser Dashboard
                                                              │
Engine (Python) ────POST /events──→                           │
                                                     SQLite WAL (persistence)
                                                     Ring Buffer (reconnection)
```

### Key Files (PRD, not yet implemented)
- `prd-live-streaming.md` — Full PRD (15 user stories)
- `spec-live-streaming.md` — Technical spec (schema, API, transport)
- `inner-streaming-design.md` — DSH SSE Bridge Plugin design

### Planned Implementation
- `streaming_server.py` — FastAPI SSE server (port 3081)
- `streaming_client.py` — Python client for event ingestion
- `event_store.py` — SQLite WAL batched writer
- `event_schema.py` — DDL for live_events, source_sequences
- `dashboard/index.html` — Self-contained HTML dashboard

---

## 16. Autonomous Coding Engine (ACE)

The ACE parses PRDs into tasks, generates code via BeeLlama, runs quality gates, tests, and commits to Gitea — all end-to-end with zero human intervention.

### Post-Migration Layout (after Epic 4)

> **Reality check (2026-09-09):** This layout is aspirational. Today `engine/` exists (10 modules) but `bench/` does **not** — the benchmark modules (`runner.py`, `judge.py`, `gpu_fit.py`, `orchestrate.py`, `pilot.py`, `report.py`, `preflight.py`, `watchdog.py`, `checkpoint.py`) live at the **top level**, not in a `bench/` subdirectory. `legacy/harness.py` exists (deprecated); top-level `harness.py` and top-level `cli.py` do **not** exist — the live CLI is `python3 engine/cli.py ace|bench ...`.

```
engine/           — 10 modules: the unified engine package
bench/            — ASPIRATIONAL (does not exist yet; modules are at top level)
legacy/           — 24 superseded modules (deprecation warnings, sunset after 14 days)
cli.py            — ASPIRATIONAL single entry point (live CLI is engine/cli.py)
transport.py      — three-transport abstraction (unchanged)
prompt_compiler.py — prompt compilation (absorbed prompt_templates + prompt_engine)
engine_service.py — FastAPI HTTP service (unchanged)
engine_client.py  — HTTP client (unchanged)
```

### Engine Modules (`engine/`)

| Module | Purpose |
|--------|---------|
| `__init__.py` | `EngineConfig` dataclass, `VERSION`, constants |
| `engine.py` | `Engine` facade — `run()`, `step()`, `status()`, `cancel()` |
| `pipeline.py` | 12-state state machine (`State` enum, `Pipeline` class) |
| `context.py` | `build_context()` — project file tree, imports, framework detection |
| `generator.py` | `generate_code()` — prompt compilation + BeeLlama inference |
| `validator.py` | `validate()` — 4-stage validation (AST, syntax, imports, execution) |
| `tester.py` | `run_tests()` — pytest execution via transport |
| `committer.py` | `commit_code()` — branch, write, commit, push via transport |
| `state.py` | SQLite persistence — `init_db()`, `checkpoint_run()`, `load_run()` |
| `events.py` | `emit_event()` — optional callback + streaming bridge |

### Benchmark Modules (`bench/`)

| Module | Purpose |
|--------|---------|
| `runner.py` | `BenchmarkRunner` — inference execution + SQLite logging |
| `judge.py` | `JudgeScorer` — automated 5-dimension scoring |
| `pilot.py` | `MethodologyPilot` — methodology validation (45 runs) |
| `report.py` | `BenchmarkReporter` — comparison tables + scatter plots |
| `gpu_fit.py` | `GPUFitProbe` — GPU fit matrix across configs × context sizes |
| `orchestrate.py` | `BenchmarkOrchestrator` — full pipeline CLI |
| `preflight.py` | 7-point pre-flight health check |
| `watchdog.py` | `GPUWatchdog` — background temperature monitor |
| `checkpoint.py` | `CheckpointManager` — crash recovery |

### CLI

```bash
# Engine commands
python3 cli.py ace run --prd <file> --project <path> [--config] [--dry-run] [--judge]
python3 cli.py ace parse <prd>
python3 cli.py ace status [--run <id>]
python3 cli.py ace resume --run <id>
python3 cli.py ace cancel

# Benchmark commands
python3 cli.py bench run --config <id> [--task <id>]
python3 cli.py bench pilot
python3 cli.py bench report --format md|json

# Streaming commands
python3 cli.py stream start [--port 3081]
python3 cli.py stream stop
python3 cli.py stream status

# System commands
python3 cli.py status
python3 cli.py health
```

**`--judge` flag:** Enables judge scoring by setting `EngineConfig(judge=True)`. When active, after each task the engine writes a row to the `judge_verdicts` table in `engine.db` with task_id, model, score, and reasoning. The table is created lazily on first write and lives only in the engine's local SQLite DB (not `benchmark-results.db`).

### Data Flow

```
PRD → engine/engine.py → engine/pipeline.py (state machine)
                              ↓
                    engine/context.py (build context)
                              ↓
                    engine/generator.py (prompt_compiler + BeeLlama)
                              ↓
                    engine/validator.py (4-stage validation)
                              ↓
                    engine/tester.py (pytest via transport)
                              ↓
                    engine/committer.py (git + Gitea via transport)
                              ↓
                    engine/state.py (SQLite checkpoint)
```

### Legacy Modules (`legacy/`)

24 superseded modules moved here with deprecation warnings. Will be deleted after the sunset window (14 days from first import). See `MIGRATION.md` for the full old-to-new mapping.

### Key Engine Fixes Applied

- `9098bd0` — Fixed ContextManager, CodeGenerator, TestRunner constructor interfaces
- `090e182` — Fixed quality gates false positive on empty generation
- `42fe8a5` — Added local execution detection (port-based)
- `090e182` — Fixed PRD parser to group by module (50% task reduction)

---

## 17. Telemetry

The telemetry system tracks all work sessions, GPU stats, benchmark results, and system events in a local SQLite database. It provides both a collector (active data gathering) and a dashboard (visual reporting).

### `telemetry_collector.py` — Active Collection (448 lines)

**CLI:** `python3 telemetry_collector.py <command> [options]`
**Class:** `TelemetryCollector`

Actively monitors the telemetry database, collects GPU stats from Triton via SSH, records benchmark results, and generates periodic summary reports.

**Commands:**

| Command | Description |
|---------|-------------|
| `start --interval <sec>` | Start continuous collection loop (default: 60s) |
| `collect` | Run a single collection cycle |
| `events --limit <n>` | Show recent telemetry events (default: 20) |
| `summary` | Show a telemetry summary |
| `export --output <path>` | Export all telemetry data to JSON |

**Key methods:**
- `collect_gpu_stats()` → `list[dict]` — Query nvidia-smi on Triton via SSH
- `record_gpu_snapshots(gpus)` — Persist GPU snapshots to `gpu_snapshots` table
- `record_event(event_type, data, session_id)` — Record a telemetry event
- `record_benchmark(config_id, benchmark_type, tps, tokens, elapsed, vram)` — Record benchmark result
- `collect()` → `dict` — Run a single collection cycle (GPU + event count)
- `recent_events(limit)` → `list[dict]` — Most recent events
- `generate_summary()` → `dict` — Full summary (sessions, events, GPU, benchmarks, config usage)
- `export_json(output_path)` — Export all tables to JSON file
- `run_loop(interval)` — Continuous collection loop (Ctrl+C to stop)

**Telemetry SQLite schema (additional tables beyond benchmark-results.db):**

```sql
-- GPU snapshots (periodic nvidia-smi readings)
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

-- Benchmark results (from remote_control.py and work_engine.py)
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

**Telemetry DB:** `~/coder-harness-telemetry.db` (shared with `work_engine.py` and `sandbox_manager.py`)

### `telemetry_dashboard.py` — HTML Dashboard (1446 lines)

**CLI:** `python3 telemetry_dashboard.py [--output <path>] [--open]`
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

**Features:**
- Auto-refreshes every 60 seconds (`<meta http-equiv="refresh" content="60">`)
- Dark theme with monospace font
- Inline SVG charts (no external dependencies)
- Responsive layout

**Output:** `reports/telemetry-dashboard.html` — single self-contained HTML file

### Telemetry Data Flow

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

---

## 18. Engine Control Layer

The Engine Control Layer provides a unified HTTP API for all engine operations, enabling remote control without SSH. It consists of three components: `engine_service.py` (server), `engine_client.py` (client), and the updated `transport.py` with HTTPTransport.

### `engine_service.py` — HTTP API Service (1265 lines)

FastAPI HTTP server running on port 3082. Deployed as a systemd service (`engine-service.service`) on Triton with auto-restart on failure.

**CLI:** `python3 engine_service.py [--port 3082] [--token <secret>]`

**Endpoints:**

| Endpoint | Method | Request Body | Response | Description |
|----------|--------|-------------|----------|-------------|
| `/health` | GET | — | `{"status": "ok", "uptime": ...}` | Health check |
| `/inference` | POST | `{messages, max_tokens, temperature, port}` | BeeLlama response dict | Proxy inference to BeeLlama |
| `/write-files` | POST | `{path, content, mode}` | `{"ok": true}` | Write files to project dir |
| `/run-tests` | POST | `{project, test_path, timeout}` | `{stdout, stderr, rc}` | Run pytest/unittest |
| `/git-commit` | POST | `{repo_path, message, files}` | `{stdout, stderr, rc}` | Git commit |
| `/exec` | POST | `{command, timeout, cwd}` | `{stdout, stderr, rc}` | Execute shell command |
| `/gpu-status` | GET | — | `{gpus: [...]}` | Query nvidia-smi |
| `/stream` | GET | — | SSE stream | Real-time event stream |

**Authentication:** Optional token via `--token` flag or `ENGINE_TOKEN` env var. Must be sent as `Authorization: Token <secret>` (scheme literal `Token`, **not** `Bearer` — `engine_service.py` rejects `Bearer` with 401). All endpoints except `/health` and `/gpu-snapshot` require it.

**Systemd unit (`engine-service.service`):**
```ini
[Unit]
Description=Coder Harness Engine Service
After=network.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/projects/coder-harness
ExecStart=/usr/bin/python3 engine_service.py --port 3082
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### `engine_client.py` — HTTP Client (~150 lines)

HTTP client library with retry logic. Mirrors the `LocalTransport` API so callers don't need to know which transport is active.

**Class:** `EngineClient`
- `__init__(base_url, token, timeout, max_retries)`
- `health()` → `dict` — GET /health
- `inference(messages, max_tokens, temperature, port)` → `dict` — POST /inference
- `write_files(path, content, mode)` → `dict` — POST /write-files
- `run_tests(project, test_path, timeout)` → `dict` — POST /run-tests
- `git_commit(repo_path, message, files)` → `dict` — POST /git-commit
- `exec_command(command, timeout, cwd)` → `dict` — POST /exec
- `gpu_status()` → `dict` — GET /gpu-status

**Retry logic:** Exponential backoff (1s, 2s, 4s) on connection errors. Max 3 retries by default. Returns `None` on final failure (caller decides fallback).

### `transport.py` — Updated with HTTPTransport

The transport layer now has three implementations:

```python
class HTTPTransport:
    """Delegates all operations to engine_service.py via HTTP."""
    def __init__(self, base_url=ENGINE_SERVICE_URL, token=None):
        self.client = EngineClient(base_url, token)

class LocalTransport:
    """Direct subprocess/HTTP calls when running on Triton."""
    # ... existing implementation

class RemoteTransport:
    """SSH tunneling from nightmare to Triton."""
    # ... existing implementation
```

**Auto-detection priority:**
1. `TRANSPORT_MODE=http` → always HTTPTransport
2. `TRANSPORT_MODE=local` → always LocalTransport
3. `TRANSPORT_MODE=ssh` → always RemoteTransport
4. `TRANSPORT_MODE=auto` (default):
   - Try HTTPTransport (health check port 3082)
   - Fallback to LocalTransport (check /.dockerenv)
   - Fallback to RemoteTransport (SSH)

### Three-Tier Architecture (Detailed)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        nightmare (dev machine)                       │
│                                                                      │
│  DSH Agent ──→ harness.py ──→ transport.py ──→ get_transport()      │
│                                              │                       │
│                        ┌─────────────────────┤                       │
│                        ▼                     ▼                       │
│                  HTTPTransport          RemoteTransport              │
│                  (Tier 2)               (Tier 1)                     │
│                   │                         │                        │
│                   │ HTTP                    │ SSH                    │
│                   ▼                         ▼                        │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                     Triton (<LAN_IP>)                       │ │
│  │                                                                 │ │
│  │  engine_service.py (:3082)    ←── HTTP from nightmare           │ │
│  │  ├── /inference → BeeLlama (:8080/:8082)                       │ │
│  │  ├── /write-files → /home/<user>/projects/                     │ │
│  │  ├── /run-tests → pytest                                        │ │
│  │  ├── /git-commit → git                                          │ │
│  │  ├── /exec → subprocess                                         │ │
│  │  └── /gpu-status → nvidia-smi                                   │ │
│  │                                                                 │ │
│  │  OR (Tier 3 - Native):                                          │ │
│  │  DSH runs directly on Triton → no remote comms needed          │ │
│  │  LocalTransport → direct subprocess + HTTP localhost            │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### CLI Commands

```bash
# Start/stop the engine service (legacy harness.py — still works, routes to engine_service.py)
python3 harness.py engine start [--port 3082] [--token <secret>]
python3 harness.py engine stop
python3 harness.py engine status

# Force transport mode (legacy harness.py)
TRANSPORT_MODE=http python3 harness.py status   # Use HTTP transport
TRANSPORT_MODE=ssh python3 harness.py status    # Use SSH transport
TRANSPORT_MODE=auto python3 harness.py status   # Auto-detect (default)
```

> The `harness.py engine *` commands above route through `legacy/harness.py` (deprecated). For raw service management use `python3 engine_service.py` directly or systemd; the live CLI is `python3 engine/cli.py ace ...`.

---

## 19. Config Capability Benchmark Suite (CCBS)

**Location:** `/home/<user>/projects/ace-engine/tickets/ccbs/`
**Deployed on:** Triton at `/home/<user>/ccbs/`
**Status:** ✅ Deployed and tested (73 models scanned, 712 configs generated)

### Overview

CCBS is a two-layer system for discovering, benchmarking, and ranking model configs on the dual-GPU Triton machine (RTX 3090 24GB + RTX 3070 8GB). It automates the entire model configuration lifecycle:

1. **Scans** GGUF model directories to discover available models
2. **Classifies** each model by role (orchestrator/worker/flexible) based on size, architecture, and features
3. **Suggests** optimal orchestrator+worker pairs with scores and explanations
4. **Generates** all viable configurations (model × GPU × cache × spec × context)
5. **Benchmarks** each config through: load → warmup → tasks → score → swap
6. **Learns** from benchmark results to improve role classifications over time
7. **Reports** rankings, Pareto frontiers, and recommendations in Markdown

### Architecture

```
Layer 1 (JS/Python, Agent-side)     Layer 2 (Python, Triton-side)
┌─────────────────────┐           ┌──────────────────────────────┐
│ ccbs-suggest.js     │           │ ccbs-state-machine.py (FSM)  │
│  - classifyModel()  │           │  14 states, 25 events        │
│  - pairScore()      │──bridge──▶│  checkpoint/resume           │
│  - suggestSetups()  │           ├──────────────────────────────┤
│                     │           │ ccbs-runner.py               │
│ ccbs-model-scanner.js          │  CLI + orchestration         │
│  - parseGgufHeader()│           │  --full, --config, --sweep  │
│  - scanModelPool()  │           ├──────────────────────────────┤
│                     │           │ ccbs-config-gen.py           │
│ ccbs-run.js         │           │  Cartesian product → filter  │
│  - validateSetup()  │           │  → rank by expected value    │
│  - generateDockerEnv│           ├──────────────────────────────┤
│  - invokeBench()    │           │ ccbs-judge.py                │
└─────────────────────┘           │  5-dim scoring + hallucination│
                                  ├──────────────────────────────┤
                                  │ ccbs-report.py               │
                                  │  Rich tables + Markdown      │
                                  └──────────────────────────────┘
```

### Key Commands

```bash
# SSH to Triton
ssh <user>@<LAN_IP>

# Navigate to CCBS
cd ~/ccbs

# Scan model directories
node ccbs-model-scanner.js /home/<user>/models/ /data/models/

# Get smart suggestions (top 5 setups)
node ccbs-suggest.js --suggest 5

# Generate all viable configurations
python3 ccbs-config-gen.py --scan --output ccbs-queue.json

# Run benchmarks (dry run first)
python3 ccbs-runner.py --full --dry-run

# Run actual benchmarks
python3 ccbs-runner.py --full

# Generate comparison report
python3 ccbs-report.py --output reports/ccbs-final-report.md
```

### Model Pool Status (as of 2026-09-04)

- **Models Scanned:** 73
- **Top Orchestrator:** Qwen3 Next 80B A3B Thinking
- **Top Worker:** Qwen3.5-27B-DFlash
- **Configurations Generated:** 712 viable configs
- **BeeLlama Status:** ✅ Running (ports 8080, 8082)

### Integration with Coder Harness

CCBS integrates with the broader Coder Harness system:

- **Model Discovery:** CCBS scans model directories and provides recommendations for which configs to benchmark
- **Configuration Generation:** CCBS generates viable configurations that can be used by `orchestrate.py`
- **Benchmark Results:** CCBS results feed into the telemetry system and can be queried alongside Coder Harness results
- **Role Classification:** CCBS classifies models by role, which can inform model selection for coding tasks

### Files

| File | Purpose |
|------|---------|
| `ccbs-model-scanner.js` | JS GGUF scanner (canonical — parses headers, scans model directories) |
| `ccbs-suggest.js` | Role classification and setup suggestions |
| `ccbs-config-gen.py` | Configuration generation (Cartesian product → filter → rank) |
| `ccbs-runner.py` | Benchmark runner with state machine (accepts transport via constructor) |
| `ccbs-judge.py` | 5-dimension scoring with hallucination detection |
| `ccbs-report.py` | Rich tables + Markdown report generation |
| `ccbs-constants.json` | Shared constants (KV cache, VRAM overhead, arch types) — single source of truth |
| `migrate_ccbs_schema.py` | SQLite schema migration (8 new tables) |
| `ccbs-corpus.json` | 14 benchmark tasks (L1-L5 + sweeps) |

## Agent skills

### Issue tracker

Issues live in Gitea (`<user>/ace-engine`), driven via the `tea` CLI or
the Gitea REST API (`GITEA_TOKEN`). External PRs are NOT a triage
surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles (`needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`) plus `bug`/`enhancement` and
`P1`/`P2`/`P3`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
