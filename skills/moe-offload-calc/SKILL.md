# MoE Offload Calculator & PCIe Optimizer

Calculate optimal GPU/CPU layer splits for Mixture of Experts models, model PCIe traffic patterns, estimate transfer latencies, and score offloading strategies. Six integrated tools for comprehensive MoE offload analysis.

## When to Use

- Determining how many MoE expert layers to keep on GPU vs offload to CPU
- Estimating VRAM usage for a given model + context size + KV cache type
- Modeling PCIe traffic latency between CPU RAM and GPU VRAM
- Comparing offload strategies: full GPU, `--n-cpu-moe`, `-ot` patterns, hybrid, naive, smart, pipelined
- Generating `--n-cpu-moe` and `-ot` (override tensor) patterns for llama.cpp/BeeLlama
- Comparing offload feasibility across GPU models (3070, 3090, 4090, etc.)
- Scanning a model directory for MoE models
- Generating Mermaid diagrams and ASCII art for documentation
- Simulating 3-tier expert caching (GPU → CPU → Disk) and pipeline overlap

## Tools

### 1. moe-calculator.py — VRAM & Layer Split Calculator

```bash
python3 <SKILL_DIR>/moe-calculator.py <model.gguf> [options]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--gpu <key>` | Target GPU | `3070` |
| `--context <N>` | Context size in tokens | `16384` |
| `--cache <type>` | KV cache type: `f16`, `q8_0`, `q4_0` | `q8_0` |
| `--n-cpu-layers <N>` | Force N MoE layers to CPU | auto |
| `--compare` | Compare across all GPUs | — |
| `--compare-contexts` | Compare across context sizes | — |
| `--compact` | Compact output (no layer chart) | — |
| `--json` | Output as JSON | — |
| `--list-gpus` | List available GPUs | — |
| `--scan <dir>` | Scan directory for MoE models | — |
| `--save <file>` | Save result to file | — |
| `--ot <pattern>` | Visualize -ot pattern effect | — |
| `--ot-compare <A>\|<B>` | Compare two -ot patterns side-by-side | — |

**Hotness indicators:** Layer chart shows 🔥 HOT (dense, 100% accessed), ❄️ COLD (MoE, offloaded), 🧊 FROZEN (idle experts).

### 2. moe-ot-visualizer.py — -ot Pattern Visualizer

```bash
python3 <SKILL_DIR>/moe-ot-visualizer.py <model.gguf> [options]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--gpu <key>` | Target GPU | `3070` |
| `--context <N>` | Context size (tokens) | `16384` |
| `--ot <pattern>` | -ot pattern string (comma-separated) | — |
| `--ot-a <pattern>` | First -ot pattern for comparison | — |
| `--ot-b <pattern>` | Second -ot pattern for comparison | — |
| `--show-all` | Show all possible -ot patterns | — |
| `--demo` | Use synthetic demo data (no GGUF needed) | — |
| `--json` | Output analysis as JSON | — |
| `--quiet`, `-q` | Minimal output | — |

Key Insight: `--n-cpu-moe` moves ALL expert tensors to CPU for offloaded layers. `-ot` patterns let you move ONLY specific tensors, keeping routing computation (gate_inp, exp_probs) on GPU for fast decision-making.

### 3. moe-pcie-optimizer.py — PCIe Traffic Model & Strategy Scorer

```bash
python3 <SKILL_DIR>/moe-pcie-optimizer.py <model.gguf> [options]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--gpu <key>` | Target GPU (includes PCIe gen/lane info) | `3070` |
| `--context <N>` | Context size (tokens) | `16384` |
| `--cache <type>` | KV cache type | `q8_0` |
| `--strategy <type>` | Strategy: `full_gpu`, `n_cpu_moe`, `ot_pattern`, `hybrid`, `naive`, `smart`, `pipelined`, `all` | `all` |
| `--n-cpu-layers <N>` | Layers to offload (for n_cpu_moe) | auto |
| `--ot <pattern>` | -ot pattern string (for ot_pattern) | — |
| `--compare-all` | Compare all strategies side-by-side | — |
| `--mermaid` | Output Mermaid diagrams | — |
| `--ascii` | Output ASCII art diagrams | — |
| `--diagram` | Output data flow diagram | — |
| `--demo` | Use synthetic demo data | — |
| `--list-gpus` | List GPUs with PCIe bandwidth info | — |
| `--profile <file>` | Activation profile JSON file | — |
| `--n-experts <N>` | Number of experts (manual override) | — |
| `--n-active <N>` | Active experts per token (manual override) | — |
| `--json` | Output as JSON (tool composition) | — |
| `--quiet`, `-q` | Minimal output | — |

### 4. moe-offload-calculator.py — GLM-4.7 Multi-Scenario Calculator

```bash
python3 <SKILL_DIR>/moe-offload-calculator.py
```

Hardcoded for GLM-4.7-Flash-REAP-23B-A3B on RTX 3070/3090. Auto-runs all 7 scenarios (8K/16K/32K/64K context × q8_0/q4_0 cache) and generates comparison tables + full Docker commands.

### 5. moe-calc-v2.py — Alternate Calculator (v2)

```bash
python3 <SKILL_DIR>/moe-calc-v2.py <model.gguf> [options]
```

Alternate implementation with the same core functionality. Supports `--list-gpus`, `--scan`, and standard GPU/context/cache flags.

## Examples

```bash
# GLM-4.7 on 3070 at 16K context — full analysis
python3 moe-calculator.py /data/models/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S.gguf --gpu 3070 --context 16384

# Compare all strategies for a model
python3 moe-pcie-optimizer.py /data/models/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S.gguf --gpu 3070 --compare-all

# Compare strategies with Mermaid diagrams
python3 moe-pcie-optimizer.py --demo --gpu 3070 --compare-all --mermaid

# Visualize -ot pattern impact
python3 moe-ot-visualizer.py --demo --gpu 3070 --ot "blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_*_shexp=CPU"

# JSON output for tool composition
python3 moe-pcie-optimizer.py --demo --gpu 3090 --compare-all --json

# List GPUs with PCIe bandwidth
python3 moe-pcie-optimizer.py --list-gpus

# Scan for MoE models
python3 moe-calculator.py --scan /data/models/

# GLM-4.7 multi-scenario analysis
python3 moe-offload-calculator.py
```

## Strategy Types

### Full GPU
All layers on GPU. Fastest (no PCIe overhead) but requires enough VRAM for the entire model.

### --n-cpu-moe N
Keeps the first N MoE layers' expert weights on GPU, offloads the rest to CPU/RAM. Attention, routing, and KV cache always stay on GPU. Simple to configure but transfers ALL expert weights per token.

### -ot Pattern (Override Tensor)
Fine-grained tensor buffer placement. Moves only specific tensors to CPU:
```
-ot 'blk.L15-L46.ffn_*_exps=CPU,blk.L15-L46.ffn_*_shexp=CPU'
```
Keeps routing computation on GPU for faster decision-making. Only fetches active experts (top-k) via PCIe.

### Hybrid
Auto-calculates the optimal GPU/CPU split based on VRAM budget. Combines the best of n-cpu-moe and -ot: keeps as many expert layers on GPU as possible, offloads the rest with selective routing.

### Naive
Loads all expert weights from CPU per token. Simple but transfers 160+ experts × 16 MB = 2.56 GB/layer — too slow for production.

### Smart
Loads only the activated experts (top-k) per token. Transfers 6-8 experts × 16 MB = ~96 MB/layer — feasible.

### Pipelined
Overlaps PCIe transfers with GPU compute. Achieves 1.5-2.5x throughput improvement over smart offload by prefetching experts for the next layer while computing the current one.

## PCIe Traffic Model

The optimizer models the actual data path between CPU RAM and GPU VRAM:

- **PCIe bandwidth**: Per-lane calculation (PCIe 3.0: ~2 GB/s/lane, 4.0: ~4 GB/s/lane, 5.0: ~8 GB/s/lane) with per-GPU overrides
- **Transfer latency**: Time to move expert tensors per token (includes fixed overhead)
- **Active expert fetch**: With -ot patterns, only top-k experts are fetched (e.g., 8/64 = 12.5%)
- **Routing overhead**: Routing on GPU (~10us/layer) vs CPU (~50us/layer)
- **3-tier caching**: GPU VRAM (87% hit, 0 ms) → CPU RAM (67% hit, 0.2 ms) → NVMe SSD (0% hit, 10 ms)
- **Pipeline overlap**: Overlaps PCIe with GPU compute for throughput improvement

## Strategy Scoring

Each strategy is scored on 4 dimensions (0-25 each, total 0-100):

| Dimension | What it measures |
|-----------|-----------------|
| VRAM efficiency | How well VRAM is utilized (sweet spot: 80-95%) |
| Latency | PCIe transfer overhead per token |
| RAM pressure | CPU RAM requirement for offloaded weights |
| Routing efficiency | Whether routing stays on GPU (fast) or moves to CPU |

**OOM penalty**: Strategies that don't fit VRAM are capped at 30/100.

## Output Formats

### Mermaid Diagrams
PCIe data flow, strategy comparison bar charts, VRAM usage charts — ready for documentation.

### ASCII Art
Layer placement maps, PCIe transfer breakdowns — for terminal and quick visualization.

### JSON
Full structured output including model info, GPU info, all strategy results with scores — for tool composition with moe-calculator.py and moe-ot-visualizer.py.

## Available GPUs

3060-12, 3060-ti, 3070, 3070-ti, 3080, 3080-ti, 3090, 3090-ti, 4060, 4060-ti, 4070, 4070-ti, 4070-ti-s, 4080, 4080-s, 4090, 5070, 5070-ti, 5080, 5090, a100-40, a100-80, h100

PCIe bandwidth varies by GPU:
- PCIe 4.0 x16: ~50 GB/s effective (30-series, 40-series, A100)
- PCIe 4.0 x8: ~25 GB/s (4060, 4060 Ti)
- PCIe 5.0 x16: ~100 GB/s (50-series, H100)

## Understanding Layer Hotness

**Key insight:** MoE models are inherently offload-friendly because only 5-10% of expert weights are activated per token.

```
DENSE LAYERS (Always Hot 🔥):
  - Attention, Dense FFN, Embedding → 100% weights accessed → NEVER offload

MoE LAYERS (Hot/Cold Mix ❄️🔥):
  - Router/Gating → Tiny, MUST stay on GPU
  - Expert FFN → Large, only 2-4 activated → CAN offload
```

### PCIe Traffic Reality

```
NAIVE: 160 experts × 16 MB = 2.56 GB per layer → IMPOSSIBLE
SMART: 6 experts × 16 MB = 96 MB per layer → FEASIBLE
CACHED: 12.5 MB per layer (87% hit rate) → FAST
```

### Interpreting Output

- **Hot %**: Percentage of expert weights frequently activated (< 5% = excellent offload candidate)
- **PCIe utilization**: < 30% = good, > 70% = bottleneck
- **Cache hit rate**: > 80% = effective caching

For the full mental model, see [RESEARCH-MOE-LAYER-HOTNESS.md](../tickets/ccbs/RESEARCH-MOE-LAYER-HOTNESS.md).

## Performance Impact

- **Full GPU**: Fastest (100% GPU compute, 0 PCIe overhead)
- **-ot pattern**: 3-10x slower than full GPU (only active experts fetched)
- **Hybrid**: Between -ot and n-cpu-moe (auto-optimized split)
- **Full n-cpu-moe**: Limited by DDR5 bandwidth (~50 GB/s vs GPU's 900+ GB/s)
- **Key insight**: MoE only activates 8 out of 64-512 experts per token, so the CPU bottleneck is less severe than it sounds

## Tool Composition

All three main tools output JSON when `--json` is used. Combine them in scripts:

```python
import subprocess, json

# Step 1: Calculate optimal layer split
calc = subprocess.run(["python3", "moe-calculator.py", model, "--gpu", "3070", "--json"],
                      capture_output=True, text=True)
config = json.loads(calc.stdout)

# Step 2: Optimize PCIe traffic
opt = subprocess.run(["python3", "moe-pcie-optimizer.py", model, "--gpu", "3070",
                      "--strategy", "hybrid", "--json"], capture_output=True, text=True)
strategy = json.loads(opt.stdout)

# Step 3: Visualize -ot patterns
vis = subprocess.run(["python3", "moe-ot-visualizer.py", model, "--gpu", "3070",
                      "--ot", strategy["recommended_ot"], "--json"], capture_output=True, text=True)
visualization = json.loads(vis.stdout)
```

## Dependencies

- Python 3.10+
- `gguf` library (optional — falls back to manual GGUF parsing if not installed)
