# Coder Harness — Remote Coding Engine for Triton

> Benchmark, evaluate, and run coding tasks on a dual-GPU Triton machine (RTX 3090 + RTX 3070) via BeeLlama.cpp inference.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![SQLite](https://img.shields.io/badge/database-sqlite-green.svg)](https://www.sqlite.org/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](#license)

## What is this?

Coder Harness is a **self-scoring, crash-resilient benchmark platform** for evaluating local
LLM inference across model, GPU, and context-size combinations.  It sends standardized coding
and orchestration tasks to [BeeLlama.cpp](https://github.com/windknown/beellama) endpoints
over SSH, scores responses with an automated judge model, tracks everything in SQLite, and
generates comparison reports with scatter plots.

**Key capabilities:**

- **6 model configurations** across 2 GPUs (RTX 3090 24 GB + RTX 3070 8 GB)
- **23 benchmark tasks** spanning orchestrator, coder, and multi-turn roles
- **Automated scoring** on 5 quality dimensions (completeness, correctness, quality, intelligence, role-fit)
- **GPU fit matrix** — probe which configs fit at which context sizes
- **Crash recovery** — checkpoint manager saves progress and resumes from the last good state
- **GPU watchdog** — background thread polls temperatures and aborts on hardware failure
- **Telemetry dashboard** — live monitoring of inference sessions


## Quick Start

```bash
cd /home/<user>/projects/ace-engine

# 1. Preflight — verify SSH, GPUs, beellama, disk, load, stress
python3 preflight.py

# 2. Run the full benchmark pipeline (GPU fit → full run → scoring → report)
python3 orchestrate.py --phase 1

# 3. Run a single model config
python3 orchestrate.py --config 3090-qwen36-35b

# 4. Generate comparison report
python3 report.py --format md

# 5. Auto-generate this README
python3 generate_readme.py
```

> Operational runbook: [ACE-RUNBOOK.md](ACE-RUNBOOK.md) — start here to run a PRD through the ACE engine, swap model configs, or debug.

## Architecture

```
nightmare (DSH Host)
  └─ orchestrate.py ──→ SSH ──→ Triton (<LAN_IP>)
                                   ├─ BeeLlama 3090 (port 8080)
                                   ├─ BeeLlama 3070 (port 8082)
                                   ├─ Gitea (port 3000)
                                   └─ Docker sandboxes
```

### Module Dependency Graph

```
orchestrate.py       CLI entry — sequences the full pipeline
├── preflight.py     7-point health check
│   └── ssh_utils.py SSH + beellama + nvidia-smi
├── gpu_fit.py       GPU fit matrix probe
│   └── ssh_utils.py
├── runner.py        Inference execution + SQLite logging
│   └── ssh_utils.py
├── judge.py         Automated scoring via judge model
│   └── ssh_utils.py
├── watchdog.py      Background GPU temperature monitor
│   └── ssh_utils.py
├── checkpoint.py    Crash recovery — save/resume progress
└── report.py        Comparison tables + scatter plots
                        └── (matplotlib, sqlite3)

pilot.py             Methodology validation — standalone CLI
├── runner.py
├── judge.py
└── ssh_utils.py

remote_control.py    Remote session orchestration
sandbox_manager.py   Docker sandbox lifecycle
work_engine.py       Task routing and execution
gitea_utils.py       Gitea API integration
telemetry_dashboard.py  Live inference monitoring
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


## Available Commands

### CLI Reference

| Module | Command | Purpose |
|--------|---------|---------|
| `preflight.py` | `python3 preflight.py` | Run 7-point preflight health check |
| `orchestrate.py` | `python3 orchestrate.py --phase <0|1>` | Full benchmark pipeline (GPU fit or full run) |
| `orchestrate.py` | `python3 orchestrate.py --config <id>` | Run a single model config |
| `orchestrate.py` | `python3 orchestrate.py --resume` | Resume from last checkpoint |
| `orchestrate.py` | `python3 orchestrate.py --dry-run` | Dry-run (no inference, no SSH) |
| `runner.py` | `python3 runner.py --config <id> [--task <id>]` | Run benchmark tasks |
| `judge.py` | `python3 judge.py --run-id <id>` | Score a single completed run |
| `judge.py` | `python3 judge.py --batch` | Score all unscored runs |
| `judge.py` | `python3 judge.py --summary` | Print scoring summary |
| `gpu_fit.py` | `python3 gpu_fit.py [--config <id>]` | Probe GPU fit matrix |
| `gpu_fit.py` | `python3 gpu_fit.py --show` | Display fit results |
| `pilot.py` | `python3 pilot.py` | Run methodology pilot (45 runs) |
| `pilot.py` | `python3 pilot.py --dry-run` | Preview pilot plan |
| `report.py` | `python3 report.py --format md` | Generate Markdown report |
| `report.py` | `python3 report.py --format json` | Generate JSON report |
| `remote_control.py` | `python3 remote_control.py` | Remote session orchestration |
| `sandbox_manager.py` | `python3 sandbox_manager.py` | Docker sandbox lifecycle |
| `work_engine.py` | `python3 work_engine.py` | Task routing and execution |
| `gitea_utils.py` | `python3 gitea_utils.py` | Gitea API integration |
| `telemetry_dashboard.py` | `python3 telemetry_dashboard.py` | Live telemetry dashboard |

### Common Flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview actions without executing inference or SSH |
| `--resume` | Resume from last saved checkpoint |
| `--config <id>` | Target a specific model configuration |
| `--task <id>` | Target a specific benchmark task |
| `--db <path>` | Custom SQLite database path |
| `--sizes <csv>` | Comma-separated context sizes for GPU fit probe |
| `--format md|json` | Output format for reports |


## Retrieval & Memory

The ACE engine extends research atoms with three additive features, each behind
a dispatch branch or feature flag so existing code-atom runs are unaffected.

### 1. Edit-ops atom kind (`task_type="edit"`)

A new atom kind dispatched when a PRD has `category: edit`.  Edits use
Aider-style SEARCH/REPLACE fences (`<<<<<<< SEARCH` / `=======` /
`>>>>>>> REPLACE`).  The pure `apply_blocks()` seam enforces exact-match
apply — multi-match fails loud with the match count (never silent
first-match).  Post-apply `ast.parse` validation + commit-gate read-back
(Gitea #44) ensure correctness.  No feature flag: the branch only activates
for PRDs with `category: edit` (zero blast radius on existing runs).

→ [DESIGN-G1](docs/features/retrieval-memory/DESIGN-G1.md)

### 2. Retrieval lane (repo-map → embeddings)

Injected into the research handler's CONTEXT stage, gated behind
`EngineConfig.enable_retrieval` (default `False`).  `RepoMapBuilder` walks
the workspace with tree-sitter (Python + C + C++), extracts top-level
function/class signatures, and renders a compact map (≤3k tokens).  Retrieved
chunks are injected into the prompt wrapped in
`--- UNTRUSTED RETRIEVAL CONTENT ---` markers (prompt-injection defense) with
per-atom-type token budgets (`RETRIEVAL_BUDGETS`).  Embedding-based retrieval
(Phase 0 conditional) ships only when `repo_map_recall@10 < 0.85`.

```python
config = EngineConfig(enable_retrieval=True)
```

→ [DESIGN-G2](docs/features/retrieval-memory/DESIGN-G2.md)

### 3. Memory lane (poisoning-gated recall)

Cross-run contradiction detection via a `CONFLICTS_WITH` edge primitive.
Research claims are submitted to `memory_claims` (pending queue); the
poisoning gate (`ace memory approve/reject`) runs a contradiction pre-check
before human approval.  Approved claims are recalled into ralph ideation
novelty signals, gated behind `EngineConfig.enable_memory_recall` (default
`False`).  One-way contract: gate writes (`approve_for_recall`), recall reads
(`status='approved'` only).

```bash
ace memory list              # list pending claims with contradiction counts
ace memory approve <claim_id> # approve (with contradiction pre-check)
ace memory reject <claim_id> # reject
```

→ [DESIGN-G3](docs/features/retrieval-memory/DESIGN-G3.md) ·
→ [DESIGN-G4 (integration)](docs/features/retrieval-memory/DESIGN-G4.md)


## Model Configurations

Auto-generated from [`models/manifest.json`](models/manifest.json).

| Config ID | GPU | Model | VRAM | Context | Speculative | KVarN | Thinking |
|-----------|-----|-------|------|---------|-------------|-------|----------|
| `3090-qwen36-35b` | 3090 | Qwen3.6-35B-A3B IQ4_XS | 18.8 GB | 128,000 | draft-dflash | kvarn5 | ✅ |
| `3090-muse-glimmer` | 3090 | Muse-Glimmer-30B Q3_K_XL | 15.9 GB | 32,000 | draft-dflash2 | — | — |
| `3090-laguna-xs` | 3090 | Laguna-XS-2.1 APEX-i-quality | 19.1 GB | 8,000 | none | — | — |
| `3090-qwen35-9b` | 3090 | Qwen3.5-9B-MTP Q4_K_M | 8.0 GB | 140,000 | draft-dflash | kvarn5 | ✅ |
| `3070-qwen35-9b` | 3070 | Qwen3.5-9B-MTP Q4_K_M | 6.9 GB | 8,000 | draft-mtp | kvarn2 | ✅ |
| `3070-qwen35-4b` | 3070 | Qwen3.5-4B Q5_K_M | 4.0 GB | 32,000 | none | kvarn2 | ✅ |

### Configuration Notes

| Config ID | Notes |
|-----------|-------|
| `3090-qwen36-35b` | Fastest MoE, 128K ctx with KVarN |
| `3090-muse-glimmer` | Agentic by design, Apache 2.0 |
| `3090-laguna-xs` | Purpose-built coder, no KVarN, no DFlash (SIGSEGV) |
| `3090-qwen35-9b` | Baseline, massive headroom, 140K ctx |
| `3070-qwen35-9b` | Budget coder, 8GB VRAM ceiling |
| `3070-qwen35-4b` | Ultra-budget, massive headroom |


## Project Registry

Auto-generated from [`projects.yaml`](projects.yaml).

| Project | Description | Language | Gitea Repo | Benchmarks |
|---------|-------------|----------|------------|------------|
| `coder-harness` | Coder Harness Benchmark Platform | python | <user>/coder-harness | `3090-qwen36-35b`, `3090-qwen35-9b` |
| `pulsar-harness` | Pulsar CUDA Inference Engine | rust | <user>/pulsar-harness | `3090-qwen36-35b` |
| `dsh-workspace` | DSH Campaign Results Workspace | typescript | <user>/dsh-workspace | `3090-qwen36-35b`, `3090-muse-glimmer` |
| `deepseek-harness` | DeepSeek Harness Plugin System | typescript | — | `3090-qwen36-35b` |

### Default Settings

| Setting | Value |
|---------|-------|
| `model_config` | `3090-qwen36-35b` |
| `max_tokens` | `2048` |
| `temperature` | `0.3` |
| `sandbox_prefix` | `harness` |
| `cleanup_after_hours` | `24` |


## Database Schema

Auto-generated from [`schema.sql`](schema.sql).  
SQLite database: `benchmark-results.db`

**7 tables** in the schema:

| Table | Purpose |
|-------|---------|
| `model_configs` | Model configurations: every testable model/GPU/quant combo |
| `tasks` | Benchmark task definitions |
| `benchmark_runs` | Individual inference results — one row per (model, task, turn, repetition) |
| `judge_scores` | Judge / quality scoring results |
| `latency_profiles` | Multi-bracket latency measurements |
| `gpu_fit_matrix` | Model/context GPU fit data |
| `schema_version` | Schema version tracking |

### Full DDL

```sql
-- Coder Harness Benchmark Platform — SQLite Schema
-- Enable foreign key enforcement before any DDL
PRAGMA foreign_keys = ON;

-- =============================================================================
-- Model configurations: every testable model/GPU/quant combo
-- =============================================================================
CREATE TABLE IF NOT EXISTS model_configs (
  id            TEXT PRIMARY KEY,                     -- e.g. '3090-qwen36-35b'
  gpu           TEXT    NOT NULL,                     -- e.g. '3090', '3070'
  model_name    TEXT    NOT NULL,                     -- e.g. 'Qwen3.6-35B-A3B IQ4_XS'
  quantization  TEXT,                                 -- e.g. 'IQ4_XS'
  context_size  INTEGER DEFAULT 4096,
  thinking_enabled INTEGER DEFAULT 0,                 -- 0 = off, 1 = on
  kvarn_level   TEXT,                                 -- e.g. 'kvarn5', 'f16', NULL
  speculative_type TEXT,                              -- e.g. 'draft-dflash', 'draft-mtp', 'none'
  draft_model   TEXT,                                 -- e.g. 'Qwen3.5-9B-DFlash.gguf', NULL
  port          INTEGER NOT NULL,
  model_path    TEXT    NOT NULL,                     -- full path on Triton
  notes         TEXT
);

-- =============================================================================
-- Benchmark task definitions
-- =============================================================================
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,                 -- e.g. 'T01' through 'T23'
  role              TEXT    NOT NULL CHECK(role IN ('orchestrator','coder','multi-turn')),
  category          TEXT    NOT NULL,
  difficulty        TEXT CHECK(difficulty IN ('easy','medium','hard')),
  prompt            TEXT    NOT NULL,
  expected_behavior TEXT,
  scoring_criteria  TEXT,
  automated_check   TEXT,                             -- optional shell command for coder tasks
  test_cases        TEXT,                             -- JSON array of test case descriptions
  turns             INTEGER DEFAULT 1                 -- for multi-turn tasks
);

-- =============================================================================
-- Individual inference results — one row per (model, task, turn, repetition)
-- =============================================================================
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

-- =============================================================================
-- Judge / quality scoring results
-- =============================================================================
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

-- =============================================================================
-- Multi-bracket latency measurements
-- =============================================================================
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

-- =============================================================================
-- Model/context GPU fit data
-- =============================================================================
CREATE TABLE IF NOT EXISTS gpu_fit_matrix (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id         TEXT    NOT NULL REFERENCES model_configs(id),
  gpu                     TEXT    NOT NULL,
  context_size            INTEGER NOT NULL,
  fits                    INTEGER NOT NULL,          -- 1 = fits, 0 = OOM
  vram_used_mb            REAL,
  vram_total_mb           REAL,
  inference_ok            INTEGER,
  inference_tokens_per_sec REAL,
  error_message           TEXT,
  measured_at             TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(model_config_id, gpu, context_size)
);

-- =============================================================================
-- Schema version tracking
-- =============================================================================
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Seed initial version
INSERT INTO schema_version (version) VALUES (1);

-- =============================================================================
-- Indexes for common query patterns
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_runs_model_task   ON benchmark_runs(model_config_id, task_id);
CREATE INDEX IF NOT EXISTS idx_runs_status       ON benchmark_runs(status);
CREATE INDEX IF NOT EXISTS idx_scores_run        ON judge_scores(run_id);
```

### Indexes

| Index | Columns |
|-------|---------|
| `idx_runs_model_task` | `model_config_id, task_id` |
| `idx_runs_status` | `status` |
| `idx_scores_run` | `run_id` |


## File Inventory

Post-migration layout (after Epic 4). See [`MIGRATION.md`](MIGRATION.md) for the full old-to-new mapping.

### `engine/` — 10 modules (Unified Engine Package)

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

### `bench/` — 9 modules (Benchmark Platform)

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

### `legacy/` — 24 superseded modules

Moved with deprecation warnings. Deleted after 14-day sunset window. See [`MIGRATION.md`](MIGRATION.md) for full mapping.

### Top-Level Retained Files

| Module | Purpose |
|--------|---------|
| `cli.py` | Unified CLI entry point (new) |
| `transport.py` | Three-transport abstraction (HTTP/Local/SSH) |
| `prompt_compiler.py` | Prompt compilation (absorbed prompt_templates + prompt_engine) |
| `engine_service.py` | FastAPI HTTP service on port 3082 |
| `engine_client.py` | HTTP client with retry logic |
| `ssh_utils.py` | SSH + BeeLlama + nvidia-smi utilities |
| `event_schema.py` | Canonical SQLite schema |
| `streaming_server.py` | SSE server (FastAPI) |
| `streaming_routes.py` | Streaming routes |
| `streaming_client.py` | Streaming client |
| `streaming_core.py` | Streaming core |
| `dsh_adapter.py` | DSH adapter |
| `dsh_adapter_ws.py` | DSH WebSocket adapter |
| `gpu_telemetry.py` | GPU telemetry collection |
| `generate_readme.py` | README generator |
| `validate_e2e.py` | E2E validation |


## Benchmark Results

*No benchmark report generated yet.  Run `python3 report.py --format md` to generate one.*

**Database:** `benchmark-results.db` (122,880 bytes)

| Metric | Value |
|--------|-------|
| Model configs | 6 |
| Tasks | 23 |
| Benchmark runs | 0 |
| Judge scores | 0 |

### Example Queries

```sql
-- Average throughput per config
SELECT model_config_id, AVG(predicted_per_second) AS avg_tps
FROM benchmark_runs WHERE status='complete'
GROUP BY model_config_id ORDER BY avg_tps DESC;

-- Best config for coder tasks
SELECT br.model_config_id, AVG(js.overall) AS avg_score
FROM benchmark_runs br
JOIN judge_scores js ON br.id = js.run_id
WHERE br.status='complete'
  AND br.task_id IN (SELECT id FROM tasks WHERE role='coder')
GROUP BY br.model_config_id ORDER BY avg_score DESC;
```


## Deployment

For Triton deployment instructions, see [`tickets/deploy/README.md`](tickets/deploy/README.md).

### Triton Machine

| Property | Value |
|----------|-------|
| Host | `<LAN_IP>` |
| User | `<user>` |
| GPU 0 | RTX 3090 24 GB |
| GPU 1 | RTX 3070 8 GB |
| Docker | 29.6.1 |
| BeeLlama 3090 | port 8080 |
| BeeLlama 3070 | port 8082 |

### Benchmark Scripts (on Triton)

| Script | Purpose |
|--------|---------|
| [`bench-all-configs.sh`](tickets/deploy/scripts/bench-all-configs.sh) | (see script) |
| [`bench-context.sh`](tickets/deploy/scripts/bench-context.sh) | (see script) |
| [`bench-quality.sh`](tickets/deploy/scripts/bench-quality.sh) | (see script) |
| [`bench-response-quality.sh`](tickets/deploy/scripts/bench-response-quality.sh) | (see script) |
| [`bench-throughput.sh`](tickets/deploy/scripts/bench-throughput.sh) | (see script) |

---

*Auto-generated by [`generate_readme.py`](generate_readme.py) from the coder-harness codebase.*
