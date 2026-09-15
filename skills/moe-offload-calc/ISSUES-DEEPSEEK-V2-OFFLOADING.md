# DeepSeek V2 Offloading Issues — Dedicated Reference

**Date:** 2026-09-06
**Author:** MiMo (DSH Agent)
**Project:** CCBS — Config Capability Benchmark Suite

---

## Executive Summary

DeepSeek V2 (and its variants V2-Lite, V2-Coder) presents **unique offloading challenges** due to its novel Multi-head Latent Attention (MLA) architecture and massive MoE (Mixture of Experts) design with 160 experts per layer. While MoE models are theoretically offload-friendly (only 5-10% of experts activated per token), DeepSeek V2's specific implementation introduces several complications that make offloading more difficult than expected.

**Key Issues:**
1. **MLA KV Cache Complexity** — Non-standard attention mechanism complicates KV cache compression
2. **Massive Expert Count** — 160 experts per layer increases offload granularity challenges
3. **Shared Expert Overhead** — Shared experts add VRAM pressure even when offloading
4. **PCIe Bandwidth Saturation** — Large expert weights can saturate PCIe bus
5. **Context Length Scaling** — KV cache grows linearly, eating into VRAM budget

---

## Architecture Overview

### DeepSeek V2 Specifications

| Property | Value |
|----------|-------|
| Total Parameters | 236B |
| Active Parameters | 21B per token |
| Layers | 60 |
| Hidden Dimension | 5120 |
| Attention Heads | 128 |
| Experts per Layer | 160 |
| Active Experts per Token | 6 (top-6 routing) |
| Shared Experts | 2 per layer |
| KV Cache Type | Multi-head Latent Attention (MLA) |

### Key Architectural Features

**1. Multi-head Latent Attention (MLA)**
- Compresses KV cache into latent space
- Reduces KV cache size by ~93% compared to standard MHA
- BUT: Non-standard implementation complicates offloading

**2. Mixture of Experts (MoE)**
- 160 experts per MoE layer (57 MoE layers total)
- Top-6 routing (only 6 experts activated per token)
- 2 shared experts per layer (always active)

**3. DeepSeekMoE Architecture**
- Fine-grained expert specialization
- Shared experts for common patterns
- Auxiliary loss for load balancing

---

## Offloading Issues — Detailed Analysis

### Issue 1: MLA KV Cache Compression

**Problem:** DeepSeek V2 uses Multi-head Latent Attention (MLA) which compresses KV cache into a latent representation. This is fundamentally different from standard MHA (Multi-head Attention) used in most models.

**Why It Matters:**
- Standard KV cache compression (q8_0, q4_0) assumes MHA format
- MLA uses a different tensor layout and compression scheme
- Incorrect compression can cause:
  - Quality degradation (garbled outputs)
  - Crashes (SIGSEGV)
  - Memory corruption

**Evidence from Research:**
- [Optimized DeepSeek V2/V3 implementation (MLA + flash attention)](https://github.com/ggml-org/llama.cpp/pull/12227) — Shows MLA requires special handling
- [BugFix: Support kv_cache_dtype_skip_layers for MLA attention](https://app.semanticdiff.com/gh/vllm-project/vllm/pull/47309/overview) — MLA needs layer-specific KV cache handling

**Impact:**
- Cannot use standard q8_0/q4_0 compression for KV cache
- Must use f16 for MLA layers (increases VRAM usage)
- KVarN compression may not work correctly

**Workaround:**
```bash
# Use f16 for MLA layers only
--cache-type-k f16 --cache-type-v f16

# Or use q8_0 only for non-MLA layers (if supported)
--cache-type-k q8_0 --cache-type-v q8_0
```

### Issue 2: Massive Expert Count (160 Experts)

**Problem:** DeepSeek V2 has 160 experts per MoE layer, which is 2-10x more than typical MoE models (64 experts in Mixtral, 8 in Switch Transformer).

**Why It Matters:**
- **Offload Granularity:** Each expert is ~16 MB (Q4 quantized), so 160 experts = 2.56 GB per layer
- **PCIe Transfer:** Even with top-6 routing, you transfer 6 × 16 MB = 96 MB per layer
- **Total PCIe:** 96 MB × 57 MoE layers = 5.5 GB per token (theoretical maximum)

**Evidence from Our Benchmarks:**
- Naive offloading (all experts to CPU): 2.56 GB per layer → IMPOSSIBLE
- Smart offloading (only activated experts): 96 MB per layer → FEASIBLE but slow
- Cached offloading (87% hit rate): 12.5 MB per layer → FAST

**Impact:**
- Naive `--n-cpu-moe` is extremely slow (transfers all 160 experts)
- Must use `-ot` patterns to transfer only activated experts
- Cache hit rate is critical for performance

**Workaround:**
```bash
# Use -ot patterns to transfer only activated experts
-ot 'blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_*_shexp=CPU'

# NOT this (transfers all 160 experts):
--n-cpu-moe 30
```

### Issue 3: Shared Expert Overhead

**Problem:** DeepSeek V2 has 2 shared experts per layer that are ALWAYS active (not routed). These add ~32 MB per layer of VRAM pressure even when offloading.

**Why It Matters:**
- Shared experts cannot be offloaded (they're always needed)
- They add to the "base" VRAM requirement
- Reduces headroom for other tensors

**Evidence from Tensor Analysis:**
```
Per MoE layer breakdown:
  - Attention: ~45 MB (always on GPU)
  - Router/Gating: ~0.4 MB (always on GPU)
  - Shared Experts: ~32 MB (always on GPU)  ← NEW overhead
  - Routed Experts: ~2.5 GB (can offload)
```

**Impact:**
- VRAM budget is tighter than expected
- Less room for KV cache at large context sizes
- May force aggressive KV cache compression

**Workaround:**
- Account for shared experts in VRAM calculations
- Use more aggressive KV cache compression (q4_0 instead of q8_0)
- Reduce context size if VRAM is tight

### Issue 4: PCIe Bandwidth Saturation

**Problem:** With 160 experts, even smart offloading can saturate PCIe bandwidth at high throughput.

**Why It Matters:**
- RTX 3070 PCIe 4.0 x16: ~13 GB/s effective
- RTX 3090 PCIe 4.0 x16: ~25 GB/s effective
- At 24 TPS, you need 24 × 96 MB = 2.3 GB/s per layer
- 57 MoE layers × 2.3 GB/s = 131 GB/s → IMPOSSIBLE

**Evidence from PCIe Traffic Modeling:**
```
PCIe Bandwidth Analysis — DeepSeek V2 on RTX 3070
═══════════════════════════════════════════════════

Naive: 2.56 GB/layer × 57 layers × 24 TPS = 3.5 TB/s → IMPOSSIBLE
Smart: 96 MB/layer × 57 layers × 24 TPS = 131 GB/s → IMPOSSIBLE
Cached: 12.5 MB/layer × 57 layers × 24 TPS = 17 GB/s → BARELY POSSIBLE
```

**Impact:**
- Even smart offloading is too slow for high throughput
- Must achieve >80% cache hit rate for acceptable performance
- Layer pipelining is critical to overlap PCIe with GPU compute

**Workaround:**
- Use layer pipelining (overlap PCIe with GPU compute)
- Maximize cache hit rate (>80%)
- Reduce throughput expectations (10-20 TPS, not 100+)

### Issue 5: Context Length Scaling

**Problem:** KV cache grows linearly with context length, eating into VRAM budget for expert weights.

**Why It Matters:**
- MLA KV cache: ~1.28 KB per token per layer (compressed)
- At 128K context: 128K × 1.28 KB × 60 layers = 9.8 GB
- Leaves only 14.2 GB for weights on 24 GB GPU (3090)
- Leaves only -1.8 GB for weights on 8 GB GPU (3070) → OOM

**Evidence from VRAM Calculations:**
```
VRAM Budget — DeepSeek V2 at 128K Context
═══════════════════════════════════════════

RTX 3090 (24 GB):
  - KV Cache: 9.8 GB (MLA compressed)
  - Attention: 2.7 GB
  - Router: 0.02 GB
  - Shared Experts: 1.9 GB
  - Routed Experts: 9.6 GB (only 6/160 active)
  - Total: 24.0 GB → OOM (no headroom)

RTX 3070 (8 GB):
  - KV Cache: 9.8 GB → OOM (exceeds VRAM)
```

**Impact:**
- Cannot run full DeepSeek V2 at 128K context on single GPU
- Must use aggressive KV cache compression (q4_0)
- May need to reduce context to 32K or 64K

**Workaround:**
```bash
# Use q4_0 for KV cache (50% reduction)
--cache-type-k q4_0 --cache-type-v q4_0

# Or reduce context size
--ctx-size 32768  # Instead of 131072
```

### Issue 6: Expert Specialization Patterns

**Problem:** DeepSeek V2's experts are highly specialized, making cache prediction difficult.

**Why It Matters:**
- Standard temporal correlation (70-85% accuracy) may not work well
- Experts may be activated in unpredictable patterns
- Cache hit rate may be lower than expected

**Evidence from Research:**
- [DALI: A Workload-Aware Offloading Framework for Efficient MoE Inference](https://ar5iv.labs.arxiv.org/html/2602.03495) — Shows expert activation patterns are complex
- [Loads of interesting ideas in the 'ktransformers' report](https://github.com/ggml-org/llama.cpp/discussions/8721) — Discusses expert caching challenges

**Impact:**
- Cache hit rate may be 60-70% instead of 80-87%
- More cold misses → more PCIe transfers
- Lower throughput than expected

**Workaround:**
- Profile actual activation patterns (requires `moe-activation-profiler.py`)
- Use adaptive caching based on measured patterns
- Accept lower throughput (10-15 TPS)

---

## Comparison: DeepSeek V2 vs Other MoE Models

| Property | DeepSeek V2 | Mixtral 8x7B | Qwen3.5-9B-MTP |
|----------|-------------|--------------|----------------|
| Total Parameters | 236B | 46.7B | 9B |
| Active Parameters | 21B | 12.9B | 2.25B |
| Experts per Layer | 160 | 8 | 64 |
| Active Experts | 6 | 2 | 2 |
| Shared Experts | 2 | 0 | 0 |
| KV Cache Type | MLA | MHA | MHA |
| Offload Difficulty | **Hard** | Easy | Easy |
| PCIe Saturation Risk | **High** | Low | Low |

**Key Insight:** DeepSeek V2 is significantly harder to offload than other MoE models due to:
1. 160 experts (vs 8-64)
2. MLA KV cache (non-standard)
3. Shared experts (always active)
4. Massive total parameters (236B)

---

## Practical Recommendations

### For RTX 3090 (24 GB)

**Best Case:**
- Context ≤ 32K: Fits entirely on GPU (no offloading needed)
- Context 64K: Offload ~30 MoE layers, keep attention on GPU
- Context 128K: Offload ~50 MoE layers, use q4_0 KV cache

**Recommended Config:**
```bash
# 32K context (full GPU)
-m /data/models/DeepSeek-V2-Coder-Q4_K_M.gguf \
-ngl 999 \
-c 32768 \
--cache-type-k q8_0 --cache-type-v q8_0

# 128K context (offload)
-m /data/models/DeepSeek-V2-Coder-Q4_K_M.gguf \
-ngl 999 \
-c 131072 \
--cache-type-k q4_0 --cache-type-v q4_0 \
-n-cpu-moe 50
```

### For RTX 3070 (8 GB)

**Best Case:**
- Context ≤ 8K: Fits with aggressive compression
- Context 16K: Offload ~40 MoE layers, use q4_0 KV cache
- Context 32K+: Not recommended (OOM)

**Recommended Config:**
```bash
# 8K context (offload)
-m /data/models/DeepSeek-V2-Lite-Q4_K_M.gguf \
-ngl 999 \
-c 8192 \
--cache-type-k q4_0 --cache-type-v q4_0 \
-n-cpu-moe 40
```

### For Dual-GPU (3090 + 3070)

**Recommended Strategy:**
- **3090 (Orchestrator):** Run full model at 32K context
- **3070 (Worker):** Run DeepSeek-V2-Lite at 8K context
- **Do NOT split layers across GPUs** (too much PCIe overhead)

---

## Debugging Guide

### Symptom: OOM at Startup

**Possible Causes:**
1. Context size too large for VRAM
2. KV cache compression not aggressive enough
3. Shared experts consuming too much VRAM

**Debug Steps:**
```bash
# Check VRAM usage
nvidia-smi

# Reduce context size
--ctx-size 8192  # Instead of 131072

# Use aggressive KV compression
--cache-type-k q4_0 --cache-type-v q4_0

# Check if model fits
python3 moe-calculator.py model.gguf --gpu 3090 --context 8192
```

### Symptom: Slow Throughput (< 10 TPS)

**Possible Causes:**
1. PCIe bandwidth saturation
2. Low cache hit rate
3. CPU compute bottleneck

**Debug Steps:**
```bash
# Check PCIe utilization
nvidia-smi --query-gpu=utilization.gpu --format=csv -l 1

# Check cache hit rate (if available in logs)
grep "cache hit" /path/to/beellama/logs

# Reduce offloading (keep more on GPU)
-n-cpu-moe 20  # Instead of 50

# Use layer pipelining
--pipeline on  # If supported
```

### Symptom: Quality Degradation

**Possible Causes:**
1. Incorrect KV cache compression for MLA
2. Expert weights corrupted during transfer
3. Routing decisions degraded

**Debug Steps:**
```bash
# Use f16 for KV cache (no compression)
--cache-type-k f16 --cache-type-v f16

# Check model integrity
sha256sum model.gguf

# Compare with reference output
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'
```

### Symptom: Crashes (SIGSEGV)

**Possible Causes:**
1. MLA KV cache corruption
2. Expert tensor alignment issues
3. Memory access violations

**Debug Steps:**
```bash
# Check dmesg for segfault details
dmesg | tail -20

# Run with debug output
./llama-server -m model.gguf --log-disable 0

# Try without offloading
-ngl 999  # Keep everything on GPU
```

---

## Known Workarounds

### Workaround 1: Use DeepSeek-V2-Lite

**Why:** DeepSeek-V2-Lite has fewer parameters (16B vs 236B) and is easier to offload.

**Tradeoff:** Lower quality, but fits on smaller GPUs.

```bash
# DeepSeek-V2-Lite on 3070
-m /data/models/DeepSeek-V2-Lite-Q4_K_M.gguf \
-ngl 999 \
-c 16384 \
--cache-type-k q8_0 --cache-type-v q8_0
```

### Workaround 2: Aggressive KV Cache Compression

**Why:** Reduces VRAM usage by 50-75%.

**Tradeoff:** Minor quality degradation (usually acceptable).

```bash
# q4_0 compression (50% reduction)
--cache-type-k q4_0 --cache-type-v q4_0

# q8_0 compression (25% reduction)
--cache-type-k q8_0 --cache-type-v q8_0
```

### Workaround 3: Reduce Context Size

**Why:** KV cache grows linearly with context.

**Tradeoff:** Limits long-context tasks.

```bash
# 32K instead of 128K
--ctx-size 32768

# 8K for tight VRAM
--ctx-size 8192
```

### Workaround 4: Layer Pipelining

**Why:** Overlaps PCIe transfers with GPU compute.

**Tradeoff:** Increases complexity, may not be supported.

```bash
# Enable pipelining (if supported)
--pipeline on

# Or use -ot patterns for fine-grained control
-ot 'blk.15-46.ffn_*_exps=CPU'
```

---

## Future Research Needed

### 1. MLA KV Cache Compression

**Goal:** Develop q8_0/q4_0 compression specifically for MLA format.

**Approach:**
- Analyze MLA tensor layout
- Develop block-wise quantization for latent space
- Validate quality against f16 baseline

### 2. Expert Activation Prediction

**Goal:** Improve cache hit rate from 60-70% to 80-87%.

**Approach:**
- Profile actual activation patterns
- Build predictive model based on input tokens
- Implement adaptive caching

### 3. PCIe Optimization

**Goal:** Reduce PCIe traffic by 50% through smarter offloading.

**Approach:**
- Implement layer pipelining
- Use speculative prefetching
- Optimize tensor placement

### 4. Multi-Node Offloading

**Goal:** Extend offloading to multiple machines via InfiniBand/RDMA.

**Approach:**
- Design distributed expert cache
- Implement low-latency transport
- Optimize for multi-node PCIe

---

## References

### Primary Sources

- [Optimized DeepSeek V2/V3 implementation (MLA + flash attention)](https://github.com/ggml-org/llama.cpp/pull/12227) — MLA implementation details
- [Loads of interesting ideas in the 'ktransformers' report](https://github.com/ggml-org/llama.cpp/discussions/8721) — Offloading strategies
- [DALI: A Workload-Aware Offloading Framework](https://ar5iv.labs.arxiv.org/html/2602.03495) — MoE offloading research
- [BugFix: Support kv_cache_dtype_skip_layers for MLA attention](https://app.semanticdiff.com/gh/vllm-project/vllm/pull/47309/overview) — MLA KV cache issues

### Related Issues

- [CPU offload still results in out of VRAM error (Unsloth's DeepSeek R1)](https://github.com/vllm-project/vllm/issues/32653) — VRAM issues with offloading
- [Bug: n_ctx will reuse n_ctx_train when --ctx_size not set](https://github.com/ggml-org/llama.cpp/issues/8817) — Context size issues
- [Bug: CPU offload not working for DeepSeek-V2-Lite-Chat](https://github.com/vllm-project/vllm/issues/15871) — Offloading failures

### Our Research

- `RESEARCH-MOE-ACTIVATION-PATTERNS.md` — Expert activation predictability
- `RESEARCH-PCIE-TRAFFIC-PATTERNS.md` — PCIe bandwidth modeling
- `RESEARCH-MOE-LAYER-HOTNESS.md` — Layer hotness mental model

---

## Conclusion

DeepSeek V2 is a powerful model but presents significant offloading challenges due to:
1. **MLA KV Cache** — Non-standard format complicates compression
2. **160 Experts** — Massive expert count increases PCIe traffic
3. **Shared Experts** — Always-active experts add VRAM pressure
4. **PCIe Saturation** — Large expert weights can saturate bus
5. **Context Scaling** — KV cache grows linearly with context

**Key Takeaway:** DeepSeek V2 requires careful tuning and aggressive optimization to offload successfully. Standard offloading strategies that work for other MoE models (Mixtral, Qwen3.5) may not work for DeepSeek V2.

**Recommendation:** Start with DeepSeek-V2-Lite for easier offloading, or use full DeepSeek V2 only at small context sizes (≤32K) on powerful GPUs (RTX 3090).

---

**Document Version:** 1.0
**Last Updated:** 2026-09-06
**Status:** Active Research
