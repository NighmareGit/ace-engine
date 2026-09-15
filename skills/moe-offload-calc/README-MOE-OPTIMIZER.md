# MoE Offload Optimization System

## Overview

A comprehensive six-tool system for optimizing Mixture of Experts model offloading on multi-GPU systems. The system analyzes GGUF model files, calculates optimal GPU/CPU layer splits, models PCIe traffic patterns, simulates 3-tier expert caching, and recommends the best offload strategy for maximum inference throughput.

## Tools

| Tool | Lines | Purpose |
|------|-------|---------|
| `moe-calculator.py` | 1007 | VRAM budget, layer splits, -ot visualization, GPU comparison, hotness indicators |
| `moe-ot-visualizer.py` | 978 | Dedicated -ot pattern analysis and comparison with hotness column |
| `moe-pcie-optimizer.py` | 1951 | PCIe traffic model, 8-strategy scoring, 3-tier caching, pipeline overlap, Mermaid/ASCII diagrams |
| `moe-offload-calculator.py` | 414 | GLM-4.7-specific multi-scenario calculator (3070/3090 × 8K-128K) |
| `moe-calc-v2.py` | 745 | Alternate calculator implementation |

**Total:** ~5,095 lines of Python

## Quick Start

```bash
# Analyze a model on your GPU
python3 moe-calculator.py model.gguf --gpu 3070 --context 16384

# Compare offload strategies (8 strategies)
python3 moe-pcie-optimizer.py model.gguf --gpu 3070 --compare-all

# Visualize -ot pattern impact
python3 moe-ot-visualizer.py model.gguf --gpu 3070 --ot "blk.15-46.ffn_*_exps=CPU"

# Show data flow diagram
python3 moe-pcie-optimizer.py model.gguf --gpu 3070 --diagram

# Demo mode (no GGUF needed)
python3 moe-pcie-optimizer.py --demo --gpu 3070 --compare-all

# Scan for MoE models
python3 moe-calculator.py --scan /data/models/

# GLM-4.7 multi-scenario analysis
python3 moe-offload-calculator.py
```

## Key Features

### 1. VRAM Budget Calculator (`moe-calculator.py`)
- Reads GGUF files via `gguf` library (with manual fallback)
- Extracts tensor structure: attention, expert FFN, router, norms
- Calculates VRAM budget with KV cache estimation
- Generates `--n-cpu-moe` and `-ot` commands
- Compares across 22 GPU models and multiple context sizes
- **Hot/cold layer classification** with 🔥/❄️/🧊 indicators
- **Inlined -ot visualization**: parse patterns, apply rules, visualize VRAM impact

### 2. -ot Pattern Visualizer (`moe-ot-visualizer.py`)
- Parses `-ot` pattern strings with wildcard support
- Shows before/after VRAM impact
- Generates ASCII charts of tensor placement
- Compares two patterns side-by-side
- Calculates PCIe traffic estimates
- **Hotness column** in layer charts

### 3. PCIe Traffic Optimizer (`moe-pcie-optimizer.py`)
- Models 8 offload strategies: full_gpu, n_cpu_moe, ot_pattern, hybrid, naive, smart, pipelined, cached
- **Per-lane PCIe bandwidth model**: PCIe 3.0/4.0/5.0 with per-GPU overrides
- **3-tier expert caching**: GPU VRAM (87% hit) → CPU RAM (67% hit) → NVMe SSD
- **Pipeline overlap**: Overlaps PCIe with GPU compute for throughput improvement
- **Activation profile support**: Load real expert activation patterns from JSON
- Estimates throughput for each strategy
- Generates Mermaid diagrams and ASCII art for documentation
- Scores strategies on 0-100 composite scale (4 dimensions)
- **Demo mode**: Synthetic data for testing without GGUF files

### 4. GLM-4.7 Multi-Scenario Calculator (`moe-offload-calculator.py`)
- Hardcoded for GLM-4.7-Flash-REAP-23B-A3B on RTX 3070/3090
- Auto-runs all 7 scenarios (8K/16K/32K/64K context × q8_0/q4_0 cache)
- Generates comparison tables + full Docker commands with `--n-cpu-moe` and `-ot`

### 5. Alternate Calculator (`moe-calc-v2.py`)
- Same core functionality with alternate implementation
- Supports `--list-gpus`, `--scan`, and standard flags

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MoE Offload Optimization System               │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────┐│
│  │  moe-calculator  │   │  moe-ot-         │   │  moe-pcie    ││
│  │  (VRAM & splits) │   │  visualizer      │   │  -optimizer  ││
│  │                  │   │  (-ot patterns)  │   │  (PCIe &     ││
│  │  - GGUF reader   │   │                  │   │   strategies) ││
│  │  - VRAM budget   │   │  - Pattern parse │   │              ││
│  │  - Layer split   │   │  - Before/after  │   │  - Traffic    ││
│  │  - GPU compare   │   │  - ASCII charts  │   │    model     ││
│  │  - Model scan    │   │  - Comparison    │   │  - Caching   ││
│  │  - -ot viz       │   │  - Hotness col   │   │  - Pipeline  ││
│  │  - Hotness       │   │                  │   │  - Scoring   ││
│  └────────┬─────────┘   └────────┬─────────┘   └──────┬───────┘│
│           │                      │                     │        │
│           └──────────────────────┴─────────────────────┘        │
│                             │                                    │
│                     ┌───────▼────────┐                          │
│                     │  Shared JSON   │                          │
│                     │  Schemas       │                          │
│                     └────────────────┘                          │
│                                                                   │
│  ┌──────────────────┐   ┌──────────────────┐                    │
│  │  moe-offload-    │   │  moe-calc-v2     │                    │
│  │  calculator      │   │  (alternate)     │                    │
│  │  (GLM-4.7 multi) │   │                  │                    │
│  └──────────────────┘   └──────────────────┘                    │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    MoE Inference Data Flow                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Token Input                                                      │
│      │                                                            │
│      ▼                                                            │
│  ┌──────────────┐                                                │
│  │ Embedding    │  GPU (always)                                  │
│  └──────┬───────┘                                                │
│         │                                                         │
│         ▼                                                         │
│  ┌──────────────┐     ┌──────────────┐                           │
│  │ Attention    │────▶│ KV Cache     │  GPU (always)             │
│  │ Layer N      │◀────│              │                           │
│  └──────┬───────┘     └──────────────┘                           │
│         │                                                         │
│         ▼                                                         │
│  ┌──────────────┐                                                │
│  │ Router/Gate  │  GPU (fast, small tensors)                     │
│  │ Select top-K │                                                │
│  └──────┬───────┘                                                │
│         │                                                         │
│         ▼                                                         │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │ Expert FFN   │◀───▶│ PCIe Bus     │◀───▶│ Expert Cache │     │
│  │ (8 active)   │     │ ~25 GB/s     │     │ GPU/CPU/Disk │     │
│  └──────┬───────┘     └──────────────┘     └──────────────┘     │
│         │                                                         │
│         ▼                                                         │
│  ┌──────────────┐                                                │
│  │ LayerNorm    │  GPU (always)                                  │
│  └──────┬───────┘                                                │
│         │                                                         │
│         ▼                                                         │
│  Token Output                                                     │
└─────────────────────────────────────────────────────────────────┘
```

## What Traverses PCIe

```
PCIe Transfer Analysis (per token, per layer)
═══════════════════════════════════════════════

With --cpu-moe (all experts on CPU):
  GPU → CPU:  Activation tensor    ~8 KB
  CPU → GPU:  Expert output       ~8 KB
  Total:      ~16 KB per layer
  Latency:    ~0.001 ms at 25 GB/s

With smart caching (hot experts on GPU):
  Cache hit:  0 KB (expert already in GPU)
  Cache miss: ~33 MB (8/64 experts × 262 MB)
  Average:    ~4.4 MB per layer (87% hit rate)
  Latency:    ~0.18 ms at 25 GB/s

With naive on-demand (load only active):
  GPU → CPU:  Request (tiny)
  CPU → GPU:  8 expert weights × 33 MB = ~264 MB
  Total:      ~264 MB per layer
  Latency:    ~10.6 ms at 25 GB/s  ← TOO SLOW
```

## Strategy Comparison

```
Strategy Comparison — GLM-4.7 on RTX 3070 (16K context)
═══════════════════════════════════════════════════════

                     Naive    Smart    Pipelined  Hybrid
                     ─────    ─────    ─────────  ──────
GPU VRAM Used       4.4 GB   7.8 GB   7.8 GB     8.6 GB
CPU RAM Needed      12.6 GB  9.2 GB   9.2 GB     8.4 GB
PCIe per token      1.6 GB   1.6 GB   1.6 GB     1.6 GB
Cache hit rate      20%      20%      20%        20%
Score               51/100   30/100   30/100     25/100
```

## Research Foundation

The system is built on research findings:

1. **PCIe Bandwidth**: RTX 3090 ~25 GB/s, RTX 3070 ~13 GB/s (realistic H2D)
2. **Expert Activation**: Predictable via temporal correlation (70-85% accuracy)
3. **On-Demand Loading**: Too slow (~5.5 ms per layer at PCIe 4.0)
4. **Expert Caching**: Achieves 67-87% hit rate with temporal correlation
5. **Layer Pipelining**: 1.5-2.5x throughput improvement by overlapping PCIe with GPU compute

## Understanding Layer Hotness — The Mental Model

**The key insight:** MoE models are inherently offload-friendly because only 5-10% of expert weights are activated per token, unlike dense models where 100% of weights are needed.

### Layer Classification

```
DENSE LAYERS (Always Hot 🔥):
  - Attention: Q/K/V projections, output projection
  - Dense FFN: Standard feed-forward networks
  - Embedding/Output: Token embedding, output norm
  → 100% weights accessed per token → NEVER offload

MoE LAYERS (Hot/Cold Mix ❄️🔥):
  - Router/Gating: Tiny (~0.1 MB), selects experts → MUST stay on GPU
  - Expert FFN: Large, but only 2-4 activated per token → CAN offload
  → 5-10% weights accessed per token → SAFE to offload
```

### Why MoE Offloading Works

```
NAIVE OFFLOAD (all experts to CPU):
  Per layer: 160 experts × 16 MB = 2.56 GB transferred
  Per token: 2.56 GB × 60 layers = 153.6 GB ← IMPOSSIBLE

SMART OFFLOAD (only activated experts):
  Per layer: 6 experts × 16 MB = 96 MB transferred
  Per token: 96 MB × 60 layers = 5.76 GB ← FEASIBLE at 13 GB/s
  Latency: 5.76 GB / 13 GB/s = 443 ms per token

WITH CACHING (hot experts on GPU):
  Cache hit (87%): 0 KB transfer (expert already in GPU)
  Cache miss (13%): 96 MB × 13% = 12.5 MB per layer
  Per token: 12.5 MB × 60 layers = 750 MB
  Latency: 750 MB / 13 GB/s = 57.7 ms per token ← MUCH BETTER
```

### Interpreting Visualizations

When you see:
```
Layer 15: GPU 45.2 MB | CPU 320.5 MB | Hot 12% | Cold 88%
```
- **45.2 MB on GPU**: Attention + router + norms (must stay)
- **320.5 MB on CPU**: Expert weights (safe to offload)
- **12% hot**: Frequently activated experts (cached on GPU)
- **88% cold**: Rarely activated experts (stay on CPU)

### Key Metrics

| Metric | Good | Fair | Poor |
|--------|------|------|------|
| Activation Ratio | < 5% | 5-15% | > 15% |
| PCIe Utilization | < 30% | 30-70% | > 70% |
| Cache Hit Rate | > 80% | 60-80% | < 60% |

For deeper understanding, see [RESEARCH-MOE-LAYER-HOTNESS.md](RESEARCH-MOE-LAYER-HOTNESS.md).

## Files

```
coder-harness/skills/moe-offload-calc/
├── moe-calculator.py           # VRAM & layer split calculator (1007 lines)
├── moe-ot-visualizer.py        # -ot pattern visualizer (978 lines)
├── moe-pcie-optimizer.py       # PCIe traffic optimizer (1951 lines)
├── moe-offload-calculator.py   # GLM-4.7 multi-scenario calculator (414 lines)
├── moe-calc-v2.py              # Alternate calculator (745 lines)
├── README-MOE-OPTIMIZER.md     # This file
├── SKILL.md                    # Skill documentation
├── PACKAGE-README.md           # Package summary
├── ISSUES-DEEPSEEK-V2-OFFLOADING.md  # Known issues
├── RESEARCH-MOE-LAYER-HOTNESS.md     # Layer hotness mental model
└── (research docs in tickets/ccbs/)
    ├── DESIGN-MOE-OPTIMIZER.md
    ├── DESIGN-MOE-PCIE-OPTIMIZER.md
    ├── RESEARCH-MOE-ACTIVATION-PATTERNS.md
    └── RESEARCH-PCIE-TRAFFIC-PATTERNS.md
```

## Dependencies

- Python 3.10+
- `gguf` library (optional — falls back to manual GGUF parsing)

## Deployment

All tools are deployed to Triton at `/home/<user>/ccbs/`:

```bash
# Deploy to Triton
scp moe-calculator.py moe-ot-visualizer.py moe-pcie-optimizer.py moe-offload-calculator.py moe-calc-v2.py <user>@<LAN_IP>:/home/<user>/ccbs/

# Test on Triton
ssh <user>@<LAN_IP> "python3 /home/<user>/ccbs/moe-pcie-optimizer.py --demo --gpu 3070 --compare-all"
```
