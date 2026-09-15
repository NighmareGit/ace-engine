# Routing Recommendation — Benchmark-Driven Model Assignment
**Generated:** 2026-09-03 from throughput benchmarks across 6 configs

## Summary

| Role | Recommended Config | Speed | VRAM | Context | Why |
|------|-------------------|-------|------|---------|-----|
| **Orchestrator** | config-h (Muse-Glimmer-30B) | 55 tok/s | 16.3 GB | 128K | Agentic by design, thinking model, 128K context for large tickets |
| **Coder** | config-i (Qwen3.6-35B-A3B) | 188 tok/s | 20.7 GB | 128K | Fastest MoE, excellent code gen, 128K context |
| **Reasoning** | config-g-glm (GLM-4.7-Flash) | 133 tok/s | 14.5 GB | 32K | Best reasoning model, good for analysis/planning |
| **Fast Coder** | config-g-laguna (Laguna-XS) | 158 tok/s | 19.1 GB | 8K | Purpose-built coder, fast but limited context |
| **Baseline** | config-h-qwen35 (Qwen3.5-9B) | 171 tok/s | 10.3 GB | 140K | Proven stable, massive headroom, fallback |
| **Deep Coder** | config-h-coder (Qwen3-Coder-30B) | 33 tok/s | 5.3 GB | 128K | Dedicated coder, CPU offload, slow but deep |
| **Helper/Judge** | 3070 (Qwen3.5-9B) | 65 tok/s | 6.8 GB | 8K | Always-on helper for parallel tasks and scoring |

## Routing Config

Full routing rules: `config/routing-beellama.yaml`

Key decisions:
- Orchestrator with large context (>32K) → Muse-Glimmer (128K ctx, agentic)
- Orchestrator with short context (≤32K) → GLM-4.7 (faster reasoning)
- Coder for code generation → Qwen3.6-35B (188 tok/s, fastest)
- Coder for deep algorithms → Qwen3-Coder-30B (dedicated coder)
- Coder for short tasks (≤8K) → Laguna-XS (158 tok/s, purpose-built)
- Speed-critical tasks → Qwen3.6-35B (188 tok/s)
- Reasoning tasks → GLM-4.7 (best reasoning)
- Context-heavy (>32K) → Qwen3.6-35B or Qwen3.5-9B (both 128K+)
- Default → Qwen3.6-35B (fastest with large context)

## Model Swap

Switch configs on Triton:
```bash
# Via state machine
python3 deploy-state-machine.py swap config-h  # for orchestrator
python3 deploy-state-machine.py swap config-i  # for coder

# Via docker compose directly
ssh <user>@<LAN_IP> 'cd ~/dockers/beellama-benchmark && docker compose --profile config-i up -d'
```

Load time: ~60-120s for large models (35B+).

## Benchmark Data

| Config | Avg tok/s | Best tok/s | VRAM | Context | Roles |
|--------|-----------|------------|------|---------|-------|
| config-i | 188.5 | 204.1 | 20.7 GB | 128K | coder |
| config-h-qwen35 | 171.3 | 183.8 | 10.3 GB | 140K | coder, orchestrator |
| config-g-laguna | 158.4 | 160.9 | 19.1 GB | 8K | coder |
| config-g-glm | 133.4 | 134.8 | 14.5 GB | 32K | orchestrator |
| config-h | 55.2 | 58.0 | 16.3 GB | 128K | orchestrator |
| config-h-coder | 32.8 | 33.2 | 5.3 GB | 128K | coder |
