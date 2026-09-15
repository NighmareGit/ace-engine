# Coder-Harness Integration Blueprint

> How the coder-harness benchmark platform wires into DSH's orchestration layer.

## Purpose

The coder-harness is a **standalone benchmark platform** that feeds data into DSH's
orchestration layer. It answers a single question:

> "Which local model configuration is best for the orchestrator role versus the coder role?"

Results from the harness inform the orchestration layer's routing decisions. The
orchestration layer never runs benchmarks itself — it consumes the harness's outputs.

---

## Integration Points

### 1. Model Config Registry → Routing

`models/manifest.json` maps directly to DSH's `routing.yaml` model entries.
Each config carries:

| Field | Example | Purpose |
|-------|---------|---------|
| `id` | `qwen-3090-4bit` | Stable config identifier |
| `gpu` | `rtx3090` | Target GPU |
| `model_path` | `/models/qwen2.5-32b-q4_k_m.gguf` | BeeLlama model file |
| `port` | `8081` | Local inference port |
| `context_size` | `8192` | Max context window |
| `kvarn_level` | `4` | Kvarn quantization level |
| `speculative_type` | `draft-4b` | Speculative decoding model |

The orchestration layer reads this registry to know what's available for routing.

### 2. Benchmark Results → Routing Decisions

`report.py` output feeds into the orchestration layer's `resolve()` method:

- **Per-role comparison tables** — orchestrator vs coder scores per config
- **Speed vs quality scatter** — tokens/sec vs judge scores
- **Recommendation report** — "Config X for orchestrator, Config Y for coder"

The routing engine calls `resolve(task_role)` and uses the harness's recommendation
as the primary signal.

### 3. GPU Fit Matrix → Capacity Planning

`gpu_fit.py` output tells the orchestration layer which models can run at which
context sizes, preventing OOM scenarios:

```
Config: qwen-3090-4bit  GPU: rtx3090 (24GB)
  ctx=2048  → 12.1GB used → FIT
  ctx=4096  → 15.3GB used → FIT
  ctx=8192  → 19.8GB used → FIT
  ctx=16384 → 25.1GB used → OOM
```

The orchestration layer uses this matrix to cap context sizes dynamically.

### 4. Deploy State Machine → Fleet Management

`deploy-state-machine.py` manages model swaps on Triton. The orchestration layer's
fleet health checks query this state:

```bash
python3 deploy-state-machine.py status --json
# → {"current_config": "qwen-3090-4bit", "state": "running", "health": "ok"}
```

State transitions: `idle → deploying → running → draining → idle`

The orchestration layer waits for `running` before routing tasks to a config.

### 5. SQLite Schema → Telemetry

The `benchmark_runs` table schema aligns with DSH's telemetry patterns.
Results can be queried for ongoing performance monitoring:

```sql
SELECT config_id, role, AVG(tokens_per_sec), AVG(judge_score)
FROM benchmark_runs
WHERE created_at > datetime('now', '-7 days')
GROUP BY config_id, role;
```

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    DSH ORCHESTRATION LAYER               │
│  routing.yaml ← benchmark results                       │
│  fleet health ← deploy-state-machine status              │
│  capacity planning ← gpu_fit_matrix                      │
└──────────────────────┬──────────────────────────────────┘
                       │ reads results
┌──────────────────────▼──────────────────────────────────┐
│                 CODER HARNESS PLATFORM                   │
│  schema.sql → SQLite (model_configs, benchmark_runs,    │
│              judge_scores, gpu_fit_matrix)               │
│  orchestrate.py → runs configs × tasks                  │
│  report.py → generates recommendation                   │
│  deploy-state-machine.py → manages Triton containers    │
└──────────────────────┬──────────────────────────────────┘
                       │ SSH + API
┌──────────────────────▼──────────────────────────────────┐
│                  TRITON MACHINE                          │
│  RTX 3090 (24GB) ← docker-compose profiles              │
│  RTX 3070 (8GB)  ← beellama.cpp servers                 │
│  6 model configs across 2 GPUs                          │
└─────────────────────────────────────────────────────────┘
```

---

## Agent Consumption Patterns

DSH agents interact with the harness through structured interfaces.

### Benchmark Agent

Calls `orchestrate.py` with specific configs, reads results from SQLite.

```
orchestrate.py --config qwen-3090-4bit --tasks all --json
→ reads SQLite for results
→ feeds into routing decisions
```

### Config Agent

Reads `models/manifest.json`, generates new `.env` files for untested configs.

```
read manifest.json
→ find configs not yet benchmarked
→ generate .env in tickets/deploy/configs/
→ trigger pilot run
```

### Report Agent

Calls `report.py`, formats results for human or agent consumption.

```
report.py --format json --role orchestrator
→ per-config comparison table
→ speed vs quality recommendation
→ writes to campaign artifacts
```

### Deploy Agent

Calls `deploy-state-machine.py` to swap models based on task requirements.

```
deploy-state-machine.py deploy --config qwen-3090-4bit
→ waits for state = running
→ updates routing.yaml
→ returns fleet status
```

### Health Agent

Monitors GPU health via `watchdog.py` patterns, alerts on degradation.

```
watchdog.py --check gpu-temp,utilization,memory
→ alerts on thermal throttling
→ logs degradation events
→ triggers automatic rollback if needed
```

---

## Structured Output Contracts

Each harness component produces structured output for agent consumption.

| Component | Command | Output |
|-----------|---------|--------|
| Benchmark runner | `orchestrate.py --json` | JSON with run IDs, status, timing |
| Report generator | `report.py --format json` | JSON with comparison tables, recommendations |
| Deploy state | `deploy-state-machine.py status` | JSON with state, config, health |
| GPU fit check | `gpu_fit.py --show` | JSON with fit matrix per config/GPU |

All JSON outputs use the `application/json` content type and are valid JSON 5
(Irish-style trailing commas allowed).

---

## Referenced Documents

- `docs/blueprints/campaign-state-machine-v2.md` — The 13-state campaign machine
- `docs/blueprints/campaign-runbook.md` — Step-by-step execution guide
- `orchestration-layer-design.md` — The routing engine design
- `workflows/fmd-ralph-workflow.js` — The FMD workflow script
