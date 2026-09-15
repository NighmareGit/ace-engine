# Research: MoE Layer Hotness — The Mental Model

## Overview

Understanding which layers are "hot" (frequently accessed, must stay on GPU) vs "cold" (infrequently accessed, safe to offload) is critical for MoE model optimization. This document provides the mental model for interpreting layer hotness in the context of Mixture of Experts architectures.

## The Core Insight: MoE Layers are Inherently Offload-Friendly

Unlike dense models where ALL weights are needed for EVERY token, MoE models only activate a small subset of experts per token. This creates a natural opportunity for offloading:

```
DENSE MODEL (e.g., Llama-3):
  Layer N: ALL weights needed → 100% hot → NEVER offload

MoE MODEL (e.g., DeepSeek-V2):
  Layer N: 2-4 experts activated out of 60-160+ → 5-10% hot → SAFE to offload
```

## Layer Classification

### Dense Layers (Always Hot 🔥)

These layers process ALL weights for EVERY token:
- **Attention layers**: Q/K/V projections, output projection
- **Dense FFN layers**: Standard feed-forward networks (in mixed architectures)
- **Embedding/output layers**: Token embedding, output norm

**Characteristics:**
- 100% of weights accessed per token
- Cannot be offloaded without severe performance penalty
- Always stay on GPU

### MoE Layers (Hot/Cold Mix ❄️🔥)

These layers use sparse expert routing:
- **Router/Gating network**: Tiny, selects which experts to activate
- **Expert FFN weights**: Large, but only 2-4 are actually used per token

**Characteristics:**
- Only 5-10% of expert weights are accessed per token
- The routing network is tiny (~0.1 MB) and must stay on GPU
- Expert weights can be offloaded to CPU RAM
- PCIe transfers ONLY the activated experts, NOT all experts

## The Mental Model: Hotness Visualization

```
LAYER HOTNESS MAP — DeepSeek-V2 (60 layers, 160 experts, top-6)
═══════════════════════════════════════════════════════════════════

Layer 0   [Dense Attention] ─── ALL weights needed ─── GPU ONLY ─── HOT 🔥
Layer 1   [Dense Attention] ─── ALL weights needed ─── GPU ONLY ─── HOT 🔥
Layer 2   [Dense Attention] ─── ALL weights needed ─── GPU ONLY ─── HOT 🔥
Layer 3   [MoE FFN]         ─── Router on GPU ─── 6/160 experts ─── CAN OFFLOAD ❄️
Layer 4   [MoE FFN]         ─── Router on GPU ─── 6/160 experts ─── CAN OFFLOAD ❄️
...
Layer 57  [MoE FFN]         ─── Router on GPU ─── 6/160 experts ─── CAN OFFLOAD ❄️
Layer 58  [MoE FFN]         ─── Router on GPU ─── 6/160 experts ─── CAN OFFLOAD ❄️
Layer 59  [MoE FFN]         ─── Router on GPU ─── 6/160 experts ─── CAN OFFLOAD ❄️

Hotness Legend:
  🔥 HOT   = 100% weights accessed per token (dense layers)
  ❄️ COLD  = 5-10% weights accessed per token (MoE expert weights)
  🧊 FROZEN = Never accessed (idle experts in MoE layers)
```

## Why This Matters for PCIe

### The Key Insight

When you offload MoE layers to CPU:
- **Dense layers stay on GPU** (100% hot, never offloaded)
- **MoE routing stays on GPU** (tiny, fast decision-making)
- **Expert weights move to CPU** (cold, only 5-10% activated)
- **PCIe transfers ONLY activated experts** (not all experts)

### Real-World Example: DeepSeek-V2 on RTX 3070

```
PCIe Transfer Analysis — DeepSeek-V2 on RTX 3070
═══════════════════════════════════════════════════

Naive Offload (all experts to CPU):
  Per layer: 160 experts × 16 MB = 2.56 GB transferred
  Per token: 2.56 GB × 60 layers = 153.6 GB ← IMPOSSIBLE

Smart Offload (only activated experts):
  Per layer: 6 experts × 16 MB = 96 MB transferred
  Per token: 96 MB × 60 layers = 5.76 GB ← FEASIBLE at 13 GB/s
  Latency: 5.76 GB / 13 GB/s = 443 ms per token ← SLOW but works

With Caching (hot experts on GPU):
  Cache hit (87%): 0 KB transfer (expert already in GPU)
  Cache miss (13%): 96 MB × 13% = 12.5 MB per layer
  Per token: 12.5 MB × 60 layers = 750 MB
  Latency: 750 MB / 13 GB/s = 57.7 ms per token ← MUCH BETTER
```

## The Three-Tier Expert Cache

```
EXPERT CACHE HIERARCHY
═══════════════════════════════════════════════════

Tier 1: GPU VRAM (Hot) ─── 87% hit rate
  ├── Expert weights for frequently-used experts
  ├── Size: 2-4 GB (fits in VRAM headroom)
  └── Latency: 0 ms (already on GPU)

Tier 2: CPU RAM (Warm) ─── 67% hit rate
  ├── Expert weights for moderately-used experts
  ├── Size: 8-16 GB (fits in system RAM)
  └── Latency: 0.2 ms (DDR5 read)

Tier 3: NVMe SSD (Cold) ─── 0% hit rate
  ├── Expert weights for rarely-used experts
  ├── Size: 50-100 GB (fits on disk)
  └── Latency: 10 ms (NVMe read)

Tier 4: HDD/Swap (Frozen) ─── 0% hit rate
  ├── Expert weights for never-used experts
  ├── Size: unlimited
  └── Latency: 100+ ms (HDD read)
```

## Interpretation Guide for Visualizations

### When You See This:

```
Layer 15: GPU 45.2 MB | CPU 320.5 MB | Hot 12% | Cold 88%
```

**What it means:**
- Layer 15 is a MoE layer with 160 experts
- 45.2 MB stays on GPU (attention + router + norms)
- 320.5 MB offloaded to CPU (expert weights)
- Only 12% of expert weights are "hot" (frequently activated)
- 88% are "cold" (rarely activated, safe to keep on CPU)

### When You See This:

```
PCIe Transfer: 12.5 MB per token | Latency: 0.96 ms | TPS: 24.5
```

**What it means:**
- Each token requires 12.5 MB of data transfer over PCIe
- This takes 0.96 ms at current PCIe bandwidth
- Resulting throughput is 24.5 tokens per second
- **Good?** Depends on context: 24.5 TPS is usable but not fast
- **Bad?** If you need 100+ TPS, this strategy won't work

### When You See This:

```
Strategy Score: 72/100 | Rating: GOOD | VRAM: 7.2/8 GB | Headroom: 0.8 GB
```

**What it means:**
- This offload strategy scores 72 out of 100 (good, not perfect)
- VRAM usage is 7.2 GB out of 8 GB available
- Only 0.8 GB headroom (tight, but fits)
- **Warning:** If context grows, may OOM
- **Consider:** Reduce context size or use more aggressive compression

## Key Metrics to Watch

### 1. Expert Activation Ratio

```
Activation Ratio = Active Experts / Total Experts

DeepSeek-V2: 6 / 160 = 3.75%  ← EXCELLENT for offloading
GLM-4.7:     8 / 64  = 12.5%  ← GOOD for offloading
Qwen3.5-9B:  2 / 64  = 3.125% ← EXCELLENT for offloading
```

**Rule of thumb:**
- < 5%: Excellent offload candidate
- 5-15%: Good offload candidate
- > 15%: Marginal, consider keeping more on GPU

### 2. PCIe Bandwidth Utilization

```
Utilization = (Data per token × TPS) / PCIe Bandwidth

Low (< 30%):  Offload is working well, room for more
Medium (30-70%): Offload is effective, monitor for saturation
High (> 70%): PCIe bottleneck, consider caching or reducing offload
```

### 3. Cache Hit Rate

```
Hit Rate = Cache Hits / Total Expert Requests

Good (> 80%): Caching is effective, most experts stay hot
Fair (60-80%): Caching helps, but some cold misses
Poor (< 60%): Caching ineffective, consider different strategy
```

## Common Patterns and Solutions

### Pattern 1: "Layer X is 100% hot"

**Interpretation:** Layer X is a dense attention layer, not MoE.
**Solution:** Keep it on GPU, never offload.

### Pattern 2: "Layer Y is 5% hot, 95% cold"

**Interpretation:** Layer Y is MoE with excellent activation sparsity.
**Solution:** Offload to CPU with caching, expect good performance.

### Pattern 3: "PCIe utilization is 85%"

**Interpretation:** PCIe is the bottleneck.
**Solution:**
- Increase caching (more hot experts on GPU)
- Reduce context size (less KV cache pressure)
- Use faster PCIe (upgrade GPU)

### Pattern 4: "VRAM headroom is 0.2 GB"

**Interpretation:** Extremely tight, may OOM with context growth.
**Solution:**
- Reduce context size
- Use more aggressive KV cache compression (q4_0)
- Offload more layers to CPU

## Practical Guidelines

### For RTX 3070 (8 GB VRAM, 13 GB/s PCIe):

- **Best case:** Models with < 5% activation ratio, 8+ GB total weights
- **Sweet spot:** Offload 60-80% of MoE layers, keep attention on GPU
- **Avoid:** Models with > 15% activation ratio, or > 16 GB total weights

### For RTX 3090 (24 GB VRAM, 25 GB/s PCIe):

- **Best case:** Fits most models entirely on GPU
- **Sweet spot:** Offload only when context > 64K (KV cache pressure)
- **Avoid:** Offloading attention layers (always keep on GPU)

### For Multi-GPU (3090 + 3070):

- **Orchestrator (3090):** Keep all layers on GPU, no offloading
- **Worker (3070):** Offload MoE layers, keep attention on GPU
- **Benefit:** Worker gets 60-80% of full GPU performance at 30% of VRAM cost

## Conclusion

The key insight is that MoE models are **inherently offload-friendly** because:
1. Only 5-10% of expert weights are activated per token
2. The routing network is tiny and stays on GPU
3. PCIe transfers only what's needed, not all weights
4. Caching achieves 67-87% hit rate, reducing PCIe traffic further

When interpreting visualizations:
- **Hot layers** = Dense layers, must stay on GPU
- **Cold layers** = MoE expert weights, safe to offload
- **PCIe traffic** = Only activated experts, not all experts
- **Cache hit rate** = How effective the caching strategy is

This mental model explains why our tools recommend specific offload strategies and how to interpret their outputs in the context of real-world performance.
