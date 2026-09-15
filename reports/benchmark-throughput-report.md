# Benchmark Throughput Report — All Configs
**Date:** 2026-09-03
**Machine:** Triton (<LAN_IP>) — RTX 3090 (24GB) + RTX 3070 (8GB)
**Method:** 3 reps × 512 max tokens per config, BeeLlama API

---

## Speed Rankings

| Rank | Config | Model | Avg tok/s | Best tok/s | VRAM (3090) | Headroom | Context |
|------|--------|-------|-----------|------------|-------------|----------|---------|
| 🥇 1 | **config-i** | Qwen3.6-35B-A3B + DFlash | **188.5** | 204.1 | 20.7 GB | 3.5 GB ⚠️ | 128K |
| 🥈 2 | **config-h-qwen35** | Qwen3.5-9B-MTP + DFlash | **171.3** | 183.8 | 10.3 GB | 13.7 GB ✅ | 140K |
| 🥉 3 | **config-g-laguna** | Laguna-XS-2.1 APEX-i-quality | **158.4** | 160.9 | 19.1 GB | 4.9 GB ⚠️ | 8K |
| 4 | **config-g-glm** | GLM-4.7-Flash 23B-A3B | **133.4** | 134.8 | 14.5 GB | 9.5 GB ✅ | 32K |
| 5 | **config-h** | Muse-Glimmer-30B + DFlash2 | **55.2** | 58.0 | 16.3 GB | 7.7 GB ✅ | 128K |
| 6 | **config-h-coder** | Qwen3-Coder-30B + cpu-moe | **32.8** | 33.2 | 5.3 GB | 18.7 GB ✅ | 128K |

## Speed vs Intelligence Matrix

| Config | Speed | Intelligence | Best For |
|--------|-------|-------------|----------|
| config-i (Qwen3.6-35B) | ⚡⚡⚡⚡⚡ | 🧠🧠🧠🧠 | **Speed king** — fastest MoE, 128K ctx, good all-around |
| config-h-qwen35 (Qwen3.5-9B) | ⚡⚡⚡⚡ | 🧠🧠🧠 | **Baseline** — proven stable, massive headroom, 140K ctx |
| config-g-laguna (Laguna-XS) | ⚡⚡⚡⚡ | 🧠🧠🧠🧠 | **Fast coder** — purpose-built for code, tight VRAM |
| config-g-glm (GLM-4.7) | ⚡⚡⚡ | 🧠🧠🧠🧠🧠 | **Reasoning** — best for analysis, planning, orchestration |
| config-h (Muse-Glimmer) | ⚡⚡ | 🧠🧠🧠🧠 | **Orchestrator** — agentic by design, 128K ctx, thinking model |
| config-h-coder (Qwen3-Coder) | ⚡ | 🧠🧠🧠🧠🧠 | **Deep coder** — dedicated coder, CPU offload, 128K ctx |

## Recommendations

### For Orchestrator Role
**Best: config-h (Muse-Glimmer-30B)** or **config-g-glm (GLM-4.7-Flash)**
- Muse-Glimmer: Agentic by design, 128K context, thinking model, Apache 2.0
- GLM-4.7: Higher reasoning capability, 133 tok/s, 32K context limit
- Trade-off: Muse-Glimmer is slower (55 tok/s) but has 128K context for large tickets. GLM is faster but limited to 32K.

### For Coder Role
**Best: config-i (Qwen3.6-35B-A3B)** or **config-h-qwen35 (Qwen3.5-9B-MTP)**
- Qwen3.6-35B: Fastest at 188 tok/s, 128K context, excellent code generation
- Qwen3.5-9B: 171 tok/s with massive headroom, proven stable, 140K context
- Trade-off: Qwen3.6 is faster but uses 20.7 GB (tight). Qwen3.5 is safer at 10.3 GB.

### Optimal Dual-GPU Pairing
| Role | Config | Speed | VRAM |
|------|--------|-------|------|
| **Orchestrator** | config-h (Muse-Glimmer) | 55 tok/s | 16.3 GB |
| **Coder** | config-i (Qwen3.6-35B) | 188 tok/s | 20.7 GB |

This pairing gives maximum intelligence for orchestration + maximum speed for coding, with both models supporting 128K context.

## VRAM Summary

| Config | GPU Used | Total VRAM | % Utilized |
|--------|----------|------------|------------|
| config-i | 3090 | 20.7 GB / 24 GB | 86% |
| config-g-laguna | 3090 | 19.1 GB / 24 GB | 80% |
| config-h | 3090 | 16.3 GB / 24 GB | 68% |
| config-g-glm | 3090 | 14.5 GB / 24 GB | 60% |
| config-h-qwen35 | 3090 | 10.3 GB / 24 GB | 43% |
| config-h-coder | 3090 | 5.3 GB / 24 GB | 22% |
| All configs | 3070 | 6.8 GB / 8 GB | 85% |

## Files

All raw results at `~/dockers/beellama-benchmark/results/` on Triton.
This report: `reports/benchmark-throughput-report.md`
