# MoE Offload Calculator & PCIe Optimizer — Skill Package

**Package Location:** `/home/<user>/projects/ace-engine/skills/moe-offload-calc/`

**Also deployed to:** `/home/<user>/ccbs/` on Triton

**Date Packaged:** 2026-09-07

## Package Contents

| File | Lines | Purpose |
|------|-------|---------|
| `SKILL.md` | 254 | Skill documentation with usage guide |
| `moe-calculator.py` | 1007 | VRAM budget, layer splits, -ot visualization, hotness indicators |
| `moe-ot-visualizer.py` | 978 | Dedicated -ot pattern analysis and comparison |
| `moe-pcie-optimizer.py` | 1951 | PCIe traffic model, 8-strategy scoring, caching, pipeline, Mermaid/ASCII |
| `moe-offload-calculator.py` | 414 | GLM-4.7-specific multi-scenario calculator |
| `moe-calc-v2.py` | 745 | Alternate calculator implementation |
| `README-MOE-OPTIMIZER.md` | 290 | Complete system documentation with hotness guide |
| `RESEARCH-MOE-LAYER-HOTNESS.md` | 587 | Research document on layer hotness mental model |
| `ISSUES-DEEPSEEK-V2-OFFLOADING.md` | — | Known issues with DeepSeek-V2 offloading |

**Total:** ~5,095 lines of Python + documentation

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
- Generates comparison tables + full Docker commands

### 5. Alternate Calculator (`moe-calc-v2.py`)
- Same core functionality with alternate implementation

## Key Insight: Layer Hotness

The system is built on the fundamental insight that **MoE models are inherently offload-friendly**:

```
DENSE LAYERS (🔥 HOT):
  - Attention, Dense FFN, Embedding
  - 100% weights accessed per token
  - NEVER offload

MoE LAYERS (❄️ COLD):
  - Router/Gating (tiny, must stay on GPU)
  - Expert FFN (large, only 5-10% activated)
  - SAFE to offload
```

**PCIe Traffic Reality:**
- **Naive**: 160 experts × 16 MB = 2.56 GB per layer → IMPOSSIBLE
- **Smart**: 6 experts × 16 MB = 96 MB per layer → FEASIBLE
- **Cached**: 12.5 MB per layer (87% hit rate) → FAST

## Usage Examples

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
```

## Integration Notes

### For Coder Harness
- Original location: `coder-harness/skills/moe-offload-calc/`
- Deployed to Triton at `/home/<user>/ccbs/`
- Integrated with CCBS benchmark system

### Dependencies
- Python 3.10+
- `gguf` library (optional — falls back to manual GGUF parsing)

## Research Foundation

The system is built on research findings documented in:
- `RESEARCH-MOE-ACTIVATION-PATTERNS.md` — Expert activation predictability
- `RESEARCH-PCIE-TRAFFIC-PATTERNS.md` — PCIe bandwidth modeling
- `RESEARCH-MOE-LAYER-HOTNESS.md` — Layer hotness mental model

Key findings:
1. **PCIe Bandwidth**: RTX 3090 ~25 GB/s, RTX 3070 ~13 GB/s (realistic H2D)
2. **Expert Activation**: Predictable via temporal correlation (70-85% accuracy)
3. **On-Demand Loading**: Too slow (~5.5 ms per layer at PCIe 4.0)
4. **Expert Caching**: Achieves 67-87% hit rate with temporal correlation
5. **Layer Pipelining**: 1.5-2.5x throughput improvement by overlapping PCIe with GPU compute

## Deployment

### Local Development
```bash
cd /home/<user>/projects/ace-engine/skills/moe-offload-calc
python3 moe-calculator.py --demo --gpu 3070
```

### Triton Deployment
```bash
scp moe-calculator.py moe-ot-visualizer.py moe-pcie-optimizer.py moe-offload-calculator.py moe-calc-v2.py <user>@<LAN_IP>:/home/<user>/ccbs/
```

### Verification
```bash
# Test on Triton
ssh <user>@<LAN_IP> "python3 /home/<user>/ccbs/moe-pcie-optimizer.py --demo --gpu 3070 --compare-all"
```

## Version History

- **v1.1 (2026-09-07)**: Merged Triton + dsh-hub versions into unified superset
  - Added 8-strategy scoring (naive, smart, pipelined, cached)
  - Added per-lane PCIe bandwidth model with 3-tier caching
  - Added pipeline overlap modeling
  - Added activation profile support (`--profile`)
  - Added `--demo` mode to PCIe optimizer
  - Added Mermaid + ASCII diagram output
  - Added GLM-4.7 multi-scenario calculator
  - Added alternate calculator (v2)
- **v1.0 (2026-09-06)**: Initial release with hot/cold layer classification
  - Added 🔥/❄️/🧊 indicators to all tools
  - Created RESEARCH-MOE-LAYER-HOTNESS.md
  - Updated documentation with mental model
  - Packaged for skunkworks-toolbox

## License

Internal use — part of the DSH (DeepSeek Harness) project ecosystem.
