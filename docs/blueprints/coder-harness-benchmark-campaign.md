# Blueprint: Coder Harness Benchmark Campaign

> **Parent:** `orchestration-layer-design.md` → Routing Engine → Model Selection
> **Sibling:** `campaign-state-machine-v2.md` | `campaign-runbook.md`
> **Source:** `coder-harness/` (benchmark platform)

## Purpose

This blueprint defines how the Coder Harness benchmark platform integrates with DSH's orchestration layer to provide **data-driven model routing decisions** for the dual-GPU Triton deployment. The engine-under-test is **ACE** (Autonomous Coding Engine, PRD→generate→validate→test→commit, zero human intervention) — its active epic is [`EPIC-ENGINE-UNIFICATION.md`](../../coder-harness/EPIC-ENGINE-UNIFICATION.md) and its full pipeline is documented in [`coder-harness/AGENTS.md §16`](../../coder-harness/AGENTS.md).

## Integration Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  DSH ORCHESTRATION LAYER                     │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ routing.yaml │  │ fleet health │  │  capacity    │      │
│  │ (model → role│  │ (provider    │  │  planning    │      │
│  │  mapping)    │  │  status)     │  │  (GPU fit)   │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                  │               │
│         ▼                 ▼                  ▼               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │            BENCHMARK RESULTS FEED                    │    │
│  │  report.py output → routing.yaml updates             │    │
│  │  watchdog.py → fleet health signals                  │    │
│  │  gpu_fit.py → capacity matrix                        │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ reads results
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                  CODER HARNESS PLATFORM                      │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ orchestrate  │  │   report     │  │ deploy-state │      │
│  │ .py          │  │   .py        │  │ -machine.py  │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                  │               │
│         ▼                 ▼                  ▼               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              SQLite: benchmark-results.db             │    │
│  │  model_configs | benchmark_runs | judge_scores       │    │
│  │  gpu_fit_matrix | latency_profiles                   │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ SSH + API
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    TRITON MACHINE                            │
│  <LAN_IP> | RTX 3090 (24GB) + RTX 3070 (8GB)          │
│  6 model configs via Docker Compose profiles                 │
│  BeeLlama.cpp (KVarN + DFlash speculative decoding)        │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow: Benchmark → Routing Decision

### Step 1: Run Benchmark Campaign
```bash
cd coder-harness
python3 orchestrate.py --phase 1    # Run all configs × tasks
python3 report.py --format json     # Generate comparison
```

### Step 2: Extract Recommendations
The report produces:
```json
{
  "recommendation": {
    "orchestrator": "config-h",
    "orchestrator_rationale": "Muse-Glimmer-30B scores 8.1/10 on orchestration tasks with 128K context",
    "coder": "config-i",
    "coder_rationale": "Qwen3.6-35B-A3B at 188 tok/s with 7.2/10 coding quality"
  }
}
```

### Step 3: Update routing.yaml
The orchestration layer's routing rules are updated:
```yaml
routes:
  - match:
      capability: "orchestration"
      role: "orchestrator"
    select:
      provider: "beellama-host"
      model: "Muse-Glimmer-30B-UD-Q3_K_XL"
      port: 8080
      context: 128000
      notes: "Best compromise: 58 tok/s, 8.1/10 orchestration score"

  - match:
      capability: "code-generation"
      role: "coder"
    select:
      provider: "beellama-host"
      model: "Qwen3.6-35B-A3B-UD-IQ4_XS"
      port: 8080
      context: 128000
      notes: "Speed king: 188 tok/s, 7.2/10 coding quality"
```

### Step 4: Model Swap via State Machine
When the orchestration layer needs a different model:
```bash
python3 deploy-state-machine.py swap config-h  # Swap to orchestrator model
# OR
python3 deploy-state-machine.py swap config-i  # Swap to coder model
```

## Model Config Reference

| Config ID | 3090 Model | Speed | VRAM | Context | Best For |
|---|---|---|---|---|---|
| `config-h` | Muse-Glimmer-30B + DFlash2 | 58 tok/s | 15.1 GB | 128K | Orchestration, planning |
| `config-i` | Qwen3.6-35B-A3B + DFlash | 188 tok/s | 20.7 GB | 128K | Coding, speed |
| `config-g-laguna` | Laguna-XS-2.1 | 181 tok/s | 21 GB | 8K | Fast coding (short ctx) |
| `config-g-glm` | GLM-4.7-Flash 23B-A3B | TBD | 17.5 GB | 32K | Reasoning, analysis |
| `config-h-coder` | Qwen3-Coder-30B + cpu-moe | TBD | 12 GB | 128K | Dedicated coding |
| `config-h-qwen35` | Qwen3.5-9B-MTP + DFlash | 153 tok/s | 7.4 GB | 140K | Baseline, fallback |

## Agent Integration Patterns

### Pattern 1: Benchmark-Driven Routing
```
Agent needs to route a task
  → Query benchmark-results.db for best config for task category
  → If current model != best config → trigger model swap
  → Route task to swapped model
```

### Pattern 2: Capacity-Aware Dispatch
```
Agent receives a task requiring N tokens context
  → Query gpu_fit_matrix for configs that fit at N tokens
  → Filter by speed/quality requirements
  → Select optimal config from filtered set
```

### Pattern 3: Health-Monitored Execution
```
Agent dispatches a long benchmark
  → watchdog.py monitors GPU health every 30s
  → If temp > 83°C or GPU disappears → abort + checkpoint
  → Agent resumes from checkpoint after GPU recovers
```

### Pattern 4: A/B Testing via State Machine
```
Agent wants to compare two configs
  → deploy-state-machine.py swap config-A
  → Run task, record result
  → deploy-state-machine.py swap config-B
  → Run task, record result
  → Compare results from SQLite
```

## Relevant Files

| File | Location | Purpose |
|---|---|---|
| `coder-harness/AGENTS.md` | dsh-hub/coder-harness/ | Master reference doc |
| `coder-harness/orchestrate.py` | dsh-hub/coder-harness/ | Main benchmark orchestrator |
| `coder-harness/report.py` | dsh-hub/coder-harness/ | Results → recommendations |
| `coder-harness/models/manifest.json` | dsh-hub/coder-harness/ | Model config registry |
| `tickets/deploy/deploy-state-machine.py` | dsh-hub/coder-harness/ | Model swap state machine |
| `docs/blueprints/campaign-state-machine-v2.md` | dsh-hub/ | Campaign execution model |
| `orchestration-layer-design.md` | dsh-hub/ | Routing engine design |
| `workflows/fmd-ralph-workflow.js` | dsh-hub/ | FMD workflow script |
