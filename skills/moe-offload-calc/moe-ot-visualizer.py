#!/usr/bin/env python3
"""
MoE -ot (Override Tensor) Pattern Visualizer

Visualizes the impact of -ot patterns on layer placement, showing which
tensors move between GPU and CPU, and the VRAM savings from selective
expert offloading.

Key Insight:
  --n-cpu-moe moves ALL expert tensors to CPU for offloaded layers.
  -ot patterns let you move ONLY specific tensors, keeping routing
  computation (gate_inp, exp_probs) on GPU for fast decision-making.

Usage:
    python3 moe-ot-visualizer.py model.gguf --gpu 3070 --context 16384 \\
        --ot "blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_gate_inp=CPU"

    python3 moe-ot-visualizer.py model.gguf --gpu 3070 \\
        --ot-a "blk.15-46.ffn_*_exps=CPU" \\
        --ot-b "blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_gate_inp=CPU"

    python3 moe-ot-visualizer.py --demo --gpu 3070 --context 16384 \\
        --ot "blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_*_shexp=CPU,blk.15-46.ffn_gate_inp=CPU,blk.15-46.exp_probs_b=CPU"
"""

import argparse
import fnmatch
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# GPU DATABASE
# ══════════════════════════════════════════════════════════════════════════════

GPUS = {
    "3060-12":  {"name": "RTX 3060 12GB",  "vram_mb": 12288},
    "3070":     {"name": "RTX 3070 8GB",    "vram_mb": 8192},
    "3080":     {"name": "RTX 3080 10GB",   "vram_mb": 10240},
    "3080-ti":  {"name": "RTX 3080 Ti 12GB","vram_mb": 12288},
    "3090":     {"name": "RTX 3090 24GB",   "vram_mb": 24576},
    "4060":     {"name": "RTX 4060 8GB",    "vram_mb": 8192},
    "4070":     {"name": "RTX 4070 12GB",   "vram_mb": 12288},
    "4070-ti-s":{"name": "RTX 4070 Ti Super 16GB", "vram_mb": 16384},
    "4080":     {"name": "RTX 4080 16GB",   "vram_mb": 16384},
    "4090":     {"name": "RTX 4090 24GB",   "vram_mb": 24576},
    "5070":     {"name": "RTX 5070 12GB",   "vram_mb": 12288},
    "5070-ti":  {"name": "RTX 5070 Ti 16GB","vram_mb": 16384},
    "5080":     {"name": "RTX 5080 16GB",   "vram_mb": 16384},
    "5090":     {"name": "RTX 5090 32GB",   "vram_mb": 32768},
    "a100-40":  {"name": "A100 40GB",       "vram_mb": 40960},
    "a100-80":  {"name": "A100 80GB",       "vram_mb": 81920},
    "h100":     {"name": "H100 80GB",       "vram_mb": 81920},
}

# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TensorInfo:
    """A single tensor in the GGUF file."""
    name: str
    size_bytes: int
    layer: int  # -1 for non-layer tensors (token_embd, output_norm, etc.)

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1e6

    @property
    def layer_str(self) -> str:
        return f"L{self.layer:02d}" if self.layer >= 0 else "meta"

    def matches_pattern(self, pattern: str) -> bool:
        """Check if this tensor matches a -ot pattern (with wildcards)."""
        return fnmatch.fnmatch(self.name, pattern)


@dataclass
class OtPattern:
    """A parsed -ot pattern like 'blk.15-46.ffn_*_exps=CPU'."""
    raw: str
    tensor_pattern: str  # e.g. "blk.15-46.ffn_*_exps"
    device: str          # "CPU" or "GPU"
    layer_start: int     # -1 if no layer range
    layer_end: int       # -1 if no layer range

    def matches_tensor(self, tensor: TensorInfo) -> bool:
        """Check if this pattern applies to a given tensor."""
        if tensor.layer < 0:
            return False
        if self.layer_start >= 0 and tensor.layer < self.layer_start:
            return False
        if self.layer_end >= 0 and tensor.layer > self.layer_end:
            return False
        # Build a fnmatch-compatible pattern by replacing the layer range with a wildcard
        # e.g. "blk.15-46.ffn_*_exps" → "blk.*.ffn_*_exps"
        fn_pattern = re.sub(r"blk\.\d+(?:-\d+)\.", "blk.*.", self.tensor_pattern)
        # Strip .weight suffix from both pattern and tensor name for matching
        # (GGUF tensors always end in .weight, but users write patterns without it)
        pat_no_suffix = fn_pattern.removesuffix(".weight")
        name_no_suffix = tensor.name.removesuffix(".weight")
        return fnmatch.fnmatch(name_no_suffix, pat_no_suffix)


@dataclass
class LayerPlacement:
    """Per-layer placement summary."""
    layer: int
    is_moe: bool
    tensors: list  # list of (TensorInfo, device) tuples

    @property
    def gpu_tensors(self) -> list:
        return [(t, d) for t, d in self.tensors if d == "GPU"]

    @property
    def cpu_tensors(self) -> list:
        return [(t, d) for t, d in self.tensors if d == "CPU"]

    @property
    def gpu_total_mb(self) -> float:
        return sum(t.size_mb for t, _ in self.gpu_tensors)

    @property
    def cpu_total_mb(self) -> float:
        return sum(t.size_mb for t, _ in self.cpu_tensors)

    @property
    def total_mb(self) -> float:
        return self.gpu_total_mb + self.cpu_total_mb


@dataclass
class OtAnalysis:
    """Complete analysis of -ot impact."""
    tensors: list  # list of TensorInfo
    patterns: list  # list of OtPattern
    placements: dict  # layer -> LayerPlacement
    gpu_vram_mb: int
    context: int
    gpu_total_mb: float
    cpu_total_mb: float
    headroom_mb: float
    fits: bool
    moved_tensors: list  # tensors moved by -ot patterns
    moved_total_mb: float


# ══════════════════════════════════════════════════════════════════════════════
# GGUF PARSER
# ══════════════════════════════════════════════════════════════════════════════

def read_gguf_tensors(path: str) -> list[TensorInfo]:
    """Read tensor names and sizes from a GGUF file."""
    import gguf
    reader = gguf.GGUFReader(path)
    tensors = []
    for t in reader.tensors:
        layer = _extract_layer(t.name)
        tensors.append(TensorInfo(name=t.name, size_bytes=t.n_bytes, layer=layer))
    return tensors


def _extract_layer(name: str) -> int:
    """Extract layer number from tensor name, or -1 for non-layer tensors."""
    m = re.match(r"blk\.(\d+)\.", name)
    return int(m.group(1)) if m else -1


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_ot_string(ot_str: str) -> list[OtPattern]:
    """Parse a comma-separated -ot string into OtPattern objects.

    Supported formats:
        blk.15-46.ffn_*_exps=CPU
        blk.15.ffn_gate_inp=GPU
        token_embd=CPU
        output_norm=CPU
    """
    patterns = []
    for part in ot_str.split(","):
        part = part.strip()
        if not part:
            continue

        # Split on last '=' to get device
        eq_idx = part.rfind("=")
        if eq_idx < 0:
            print(f"  ⚠ Warning: skipping malformed pattern (no '='): {part}", file=sys.stderr)
            continue

        tensor_pattern = part[:eq_idx].strip()
        device = part[eq_idx + 1:].strip().upper()

        if device not in ("CPU", "GPU"):
            print(f"  ⚠ Warning: unknown device '{device}' in pattern: {part}", file=sys.stderr)
            continue

        # Extract layer range from pattern
        layer_start, layer_end = _extract_layer_range(tensor_pattern)

        # Normalize pattern for fnmatch: replace layer range with wildcard
        # e.g. "blk.15-46.ffn_*_exps" → fnmatch each tensor's name
        patterns.append(OtPattern(
            raw=part,
            tensor_pattern=tensor_pattern,
            device=device,
            layer_start=layer_start,
            layer_end=layer_end,
        ))

    return patterns


def _extract_layer_range(pattern: str) -> tuple[int, int]:
    """Extract layer range from pattern like 'blk.15-46.ffn_*_exps'."""
    m = re.match(r"blk\.(\d+)(?:-(\d+))?\.", pattern)
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        return start, end
    return -1, -1


# ══════════════════════════════════════════════════════════════════════════════
# DEMO DATA (synthetic MoE model for when no GGUF file is available)
# ══════════════════════════════════════════════════════════════════════════════

def generate_demo_tensors(model_name: str = "GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S") -> list[TensorInfo]:
    """Generate synthetic tensor data for a GLM-4.7-like MoE model."""
    tensors = []

    # Token embedding + output norm
    tensors.append(TensorInfo(name="token_embd.weight", size_bytes=512_000_000, layer=-1))
    tensors.append(TensorInfo(name="output_norm.weight", size_bytes=51_200, layer=-1))

    total_layers = 47  # L0-L46
    moe_start = 1      # L0 is dense, L1-L46 are MoE

    for layer in range(total_layers):
        prefix = f"blk.{layer}"

        # Attention (always same size)
        tensors.append(TensorInfo(name=f"{prefix}.attn_q.weight", size_bytes=3_774_873, layer=layer))
        tensors.append(TensorInfo(name=f"{prefix}.attn_k.weight", size_bytes=3_774_873, layer=layer))
        tensors.append(TensorInfo(name=f"{prefix}.attn_v.weight", size_bytes=3_774_873, layer=layer))
        tensors.append(TensorInfo(name=f"{prefix}.attn_output.weight", size_bytes=3_774_873, layer=layer))
        tensors.append(TensorInfo(name=f"{prefix}.attn_norm.weight", size_bytes=25_600, layer=layer))
        tensors.append(TensorInfo(name=f"{prefix}.attn_norm.bias", size_bytes=25_600, layer=layer))

        # Layer norm
        tensors.append(TensorInfo(name=f"{prefix}.ffn_norm.weight", size_bytes=8_192, layer=layer))

        if layer < moe_start:
            # Dense layer: single FFN
            tensors.append(TensorInfo(name=f"{prefix}.ffn_gate.weight", size_bytes=11_796_480, layer=layer))
            tensors.append(TensorInfo(name=f"{prefix}.ffn_up.weight", size_bytes=11_796_480, layer=layer))
            tensors.append(TensorInfo(name=f"{prefix}.ffn_down.weight", size_bytes=11_796_480, layer=layer))
        else:
            # MoE layer: 64 experts, top-8 routing
            # Expert gate_up combined weights
            n_experts = 64
            # GLM-4.7: expert_exps = 254803968 bytes (layers 5-46) or 273678336 (layers 1-4)
            expert_size = 254_803_968 if layer >= 5 else 273_678_336
            shexp_size = 6_905_856 if layer >= 5 else 6_488_064

            # gate_up_exps: combined gate+up for all experts
            tensors.append(TensorInfo(name=f"{prefix}.ffn_gate_up_exps.weight", size_bytes=expert_size, layer=layer))
            # down_exps: separate down projection
            tensors.append(TensorInfo(name=f"{prefix}.ffn_down_exps.weight", size_bytes=expert_size // 2, layer=layer))
            # Shared expert weights
            tensors.append(TensorInfo(name=f"{prefix}.ffn_gate_shexp.weight", size_bytes=shexp_size // 2, layer=layer))
            tensors.append(TensorInfo(name=f"{prefix}.ffn_down_shexp.weight", size_bytes=shexp_size // 2, layer=layer))

            # Router / gating
            tensors.append(TensorInfo(name=f"{prefix}.ffn_gate_inp.weight", size_bytes=393_408, layer=layer))
            # Expert selection probabilities (bias/offsets)
            tensors.append(TensorInfo(name=f"{prefix}.exp_probs_b.weight", size_bytes=256, layer=layer))

    return tensors


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def analyze_ot_impact(
    tensors: list[TensorInfo],
    patterns: list[OtPattern],
    gpu: str = "3070",
    context: int = 16384,
) -> OtAnalysis:
    """Analyze the impact of -ot patterns on tensor placement."""

    vram = GPUS.get(gpu, GPUS["3070"])["vram_mb"]

    # KV cache estimate: ~2.5KB per token per layer for deepseek2 MLA
    kv_per_token_per_layer = 1280 * 2  # 2560 bytes
    total_layers = max((t.layer for t in tensors if t.layer >= 0), default=0) + 1
    kv_cache_mb = (context * kv_per_token_per_layer * total_layers) / 1e6
    kv_cache_mb += 300  # CUDA context overhead

    # Determine default device for each tensor
    # Default: everything on GPU
    placements = {}  # layer -> LayerPlacement
    moved_tensors = []
    moved_total_mb = 0.0

    # Group tensors by layer
    by_layer = {}
    for t in tensors:
        by_layer.setdefault(t.layer, []).append(t)

    for layer_idx in sorted(by_layer.keys()):
        layer_tensors = by_layer[layer_idx]
        layer_entries = []  # (TensorInfo, device)

        for tensor in layer_tensors:
            device = "GPU"  # default

            # Check all patterns (last match wins)
            for pattern in patterns:
                if pattern.matches_tensor(tensor):
                    device = pattern.device

            layer_entries.append((tensor, device))

            # Track moved tensors
            if device == "CPU":
                moved_tensors.append((tensor, pattern))
                moved_total_mb += tensor.size_mb

        placements[layer_idx] = LayerPlacement(
            layer=layer_idx,
            is_moe=layer_idx >= 1 and layer_idx < total_layers,
            tensors=layer_entries,
        )

    # Calculate VRAM totals
    gpu_total = sum(
        sum(t.size_mb for t, d in layer.tensors if d == "GPU")
        for layer in placements.values()
    )
    gpu_total += kv_cache_mb

    headroom = vram - gpu_total
    fits = headroom >= 0

    return OtAnalysis(
        tensors=tensors,
        patterns=patterns,
        placements=placements,
        gpu_vram_mb=vram,
        context=context,
        gpu_total_mb=gpu_total,
        cpu_total_mb=sum(
            sum(t.size_mb for t, d in layer.tensors if d == "CPU")
            for layer in placements.values()
        ),
        headroom_mb=headroom,
        fits=fits,
        moved_tensors=moved_tensors,
        moved_total_mb=moved_total_mb,
    )


def analyze_baseline(tensors: list[TensorInfo], gpu: str = "3070", context: int = 16384) -> OtAnalysis:
    """Analyze with no -ot patterns (baseline: everything on GPU)."""
    return analyze_ot_impact(tensors, [], gpu, context)


# ══════════════════════════════════════════════════════════════════════════════
# ASCII VISUALIZATION
# ══════════════════════════════════════════════════════════════════════════════

def print_header(gpu: str, context: int, model_name: str = ""):
    """Print the report header."""
    vram = GPUS.get(gpu, GPUS["3070"])["vram_mb"]
    print()
    print("=" * 105)
    print(f"  MoE -OT PATTERN VISUALIZER")
    if model_name:
        print(f"  Model: {model_name}")
    print(f"  GPU: {GPUS.get(gpu, {}).get('name', gpu)} ({vram} MB) | Context: {context:,} tokens")
    print("=" * 105)


def print_pattern_summary(patterns: list[OtPattern]):
    """Print the parsed -ot patterns."""
    if not patterns:
        print("\n  No -ot patterns specified (baseline: all tensors on GPU)")
        return

    print(f"\n  -OT PATTERNS ({len(patterns)}):")
    print(f"  {'─' * 90}")
    for p in patterns:
        layer_range = ""
        if p.layer_start >= 0:
            if p.layer_start == p.layer_end:
                layer_range = f"  (layer {p.layer_start})"
            else:
                layer_range = f"  (layers {p.layer_start}-{p.layer_end})"
        print(f"  {p.raw:50s} → {p.device:3s}{layer_range}")
    print()


def print_vram_comparison(baseline: OtAnalysis, modified: OtAnalysis):
    """Print before/after VRAM comparison."""
    vram = baseline.gpu_vram_mb
    delta = modified.gpu_total_mb - baseline.gpu_total_mb

    print(f"  VRAM IMPACT:")
    print(f"  {'─' * 90}")
    print(f"  {'Component':25s} {'Baseline':>12s} {'After -ot':>12s} {'Delta':>12s}")
    print(f"  {'─' * 90}")

    # KV cache (same in both)
    kv_baseline = baseline.gpu_total_mb - sum(
        sum(t.size_mb for t, d in layer.tensors if d == "GPU")
        for layer in baseline.placements.values()
    )
    kv_modified = modified.gpu_total_mb - sum(
        sum(t.size_mb for t, d in layer.tensors if d == "GPU")
        for layer in modified.placements.values()
    )

    # GPU model weights
    gw_baseline = baseline.gpu_total_mb - kv_baseline
    gw_modified = modified.gpu_total_mb - kv_modified

    print(f"  {'GPU model weights':25s} {gw_baseline:10.1f}MB {gw_modified:10.1f}MB {gw_modified - gw_baseline:+10.1f}MB")
    print(f"  {'KV cache + overhead':25s} {kv_baseline:10.1f}MB {kv_modified:10.1f}MB {kv_modified - kv_baseline:+10.1f}MB")
    print(f"  {'─' * 90}")
    print(f"  {'GPU total':25s} {baseline.gpu_total_mb:10.1f}MB {modified.gpu_total_mb:10.1f}MB {delta:+10.1f}MB")

    # Headroom
    h_baseline = baseline.headroom_mb
    h_modified = modified.headroom_mb
    status_b = "✅" if baseline.fits else "❌ OOM"
    status_m = "✅" if modified.fits else "❌ OOM"
    print(f"  {'Headroom':25s} {h_baseline:10.1f}MB  {h_modified:10.1f}MB  {h_modified - h_baseline:+10.1f}MB")
    print(f"  {'Status':25s} {status_b:>12s} {status_m:>12s}")

    # CPU side
    print(f"  {'CPU model weights':25s} {'':>12s} {modified.cpu_total_mb:10.1f}MB")
    print()

    # Key insight
    if modified.moved_total_mb > 0:
        print(f"  💡 -ot moved {modified.moved_total_mb:.1f} MB of tensors to CPU")
        print(f"     GPU saves {abs(delta):.1f} MB VRAM (includes KV cache adjustments)")
    print()


def print_layer_chart(analysis: OtAnalysis, show_details: bool = True):
    """Print per-layer GPU/CPU placement chart with hot/cold indicators."""
    print(f"  LAYER PLACEMENT CHART:")
    print(f"  {'─' * 100}")
    print(f"  {'Lyr':>4s} {'Side':>4s} {'GPU MB':>8s} {'CPU MB':>8s} {'Total':>8s}  {'Hotness':>8s}  {'Memory Bar'}")
    print(f"  {'─' * 100}")
    print(f"  Legend: 🔥 HOT = Dense (100% accessed) | ❄️ COLD = MoE (5-10% activated) | 🧊 FROZEN = Idle experts")
    print()

    max_mb = max(p.total_mb for p in analysis.placements.values()) if analysis.placements else 1

    for layer_idx in sorted(analysis.placements.keys()):
        p = analysis.placements[layer_idx]
        gpu_mb = p.gpu_total_mb
        cpu_mb = p.cpu_total_mb
        total = p.total_mb

        # Determine primary side
        if cpu_mb > gpu_mb:
            side = "CPU"
        elif gpu_mb > 0 and cpu_mb == 0:
            side = "GPU"
        else:
            side = "GPU" if gpu_mb >= cpu_mb else "CPU"

        # Determine hotness
        if not p.is_moe:
            hotness = "🔥 HOT"  # Dense layer
        elif cpu_mb > 0:
            hotness = "❄️ COLD"  # MoE with offloaded experts
        else:
            hotness = "🔥 HOT"  # MoE but all on GPU

        # Memory bar: GPU = solid, CPU = hollow
        bar_total = int(total / max_mb * 35) if max_mb > 0 else 0
        bar_gpu = int(gpu_mb / max_mb * 35) if max_mb > 0 else 0
        bar_gpu = min(bar_gpu, bar_total)
        bar_cpu = bar_total - bar_gpu

        bar = "█" * bar_gpu + "░" * bar_cpu

        # Layer label
        label = f"L{layer_idx:02d}"
        if not p.is_moe:
            label += "(D)"  # dense marker

        print(f"  {label:>6s} {side:>4s} {gpu_mb:7.1f} {cpu_mb:7.1f} {total:7.1f}MB  {hotness:>8s}  {bar}")

    print(f"  {'─' * 100}")
    print()


def print_tensor_detail(analysis: OtAnalysis):
    """Print detailed per-tensor breakdown for layers with CPU tensors."""
    print(f"  TENSOR DETAIL (layers with CPU tensors):")
    print(f"  {'─' * 100}")

    has_cpu_layers = False
    for layer_idx in sorted(analysis.placements.keys()):
        p = analysis.placements[layer_idx]
        if not p.cpu_tensors:
            continue

        has_cpu_layers = True
        print(f"\n  L{layer_idx:02d}  [{p.gpu_total_mb:.1f}MB GPU | {p.cpu_total_mb:.1f}MB CPU]")

        for tensor, device in p.tensors:
            marker = " ← moved by -ot" if device == "CPU" else ""
            # Classify tensor type
            ttype = _classify_tensor(tensor.name)
            print(f"    └── {ttype:30s} {device:3s} ({tensor.size_mb:7.1f}MB){marker}")

    if not has_cpu_layers:
        print("  (no layers have CPU tensors)")

    print()


def _classify_tensor(name: str) -> str:
    """Classify a tensor name into a human-readable category."""
    if "attn" in name:
        return "attention"
    elif "ffn_gate_up_exps" in name:
        return "ffn_gate_up_exps (experts)"
    elif "ffn_down_exps" in name:
        return "ffn_down_exps (experts)"
    elif "ffn_gate_shexp" in name:
        return "ffn_gate_shexp (shared)"
    elif "ffn_down_shexp" in name:
        return "ffn_down_shexp (shared)"
    elif "ffn_gate_inp" in name:
        return "ffn_gate_inp (router)"
    elif "exp_probs" in name:
        return "exp_probs (routing)"
    elif "ffn_gate" in name:
        return "ffn_gate (dense)"
    elif "ffn_up" in name:
        return "ffn_up (dense)"
    elif "ffn_down" in name:
        return "ffn_down (dense)"
    elif "ffn_norm" in name:
        return "ffn_norm"
    elif "attn_norm" in name:
        return "attn_norm"
    elif "token_embd" in name:
        return "token_embedding"
    elif "output_norm" in name:
        return "output_norm"
    else:
        return name.split(".")[-1] if "." in name else name


def print_vram_bar(analysis: OtAnalysis):
    """Print a visual VRAM usage bar."""
    vram = analysis.gpu_vram_mb
    used = analysis.gpu_total_mb
    pct = (used / vram * 100) if vram > 0 else 0
    bar_w = 60
    filled = int(pct / 100 * bar_w)
    filled = min(filled, bar_w)

    bar = "█" * filled + "░" * (bar_w - filled)
    status = "✅ FITS" if analysis.fits else "❌ OOM"

    print(f"  VRAM USAGE:")
    print(f"  {'─' * 90}")
    print(f"  [{bar}] {pct:.1f}%")
    print(f"  {used:.1f} MB / {vram} MB  ({analysis.headroom_mb:+.1f} MB headroom) {status}")
    print()


def print_pci_traffic_insight(analysis: OtAnalysis):
    """Print the PCIe traffic insight for -ot patterns."""
    if not analysis.moved_tensors:
        return

    print(f"  PCIe TRAFFIC INSIGHT:")
    print(f"  {'─' * 90}")

    # Categorize moved tensors
    expert_mb = 0.0
    router_mb = 0.0
    other_mb = 0.0

    for tensor, pattern in analysis.moved_tensors:
        if "exps" in tensor.name or "shexp" in tensor.name:
            expert_mb += tensor.size_mb
        elif "gate_inp" in tensor.name or "exp_probs" in tensor.name:
            router_mb += tensor.size_mb
        else:
            other_mb += tensor.size_mb

    # Total expert weights in MoE layers
    total_expert_mb = sum(
        t.size_mb for t in analysis.tensors
        if "exps" in t.name or "shexp" in t.name
    )

    if expert_mb > 0:
        # Calculate per-token PCIe traffic
        # With -ot: GPU only fetches active experts (~8/64 = 12.5%)
        # With --n-cpu-moe: GPU fetches ALL experts from CPU for those layers
        active_pct = 8 / 64  # top-8 out of 64 experts

        saved_vs_full_offload = expert_mb * (1 - active_pct)
        print(f"  Expert weights moved to CPU:     {expert_mb:8.1f} MB")
        print(f"  Router tensors moved to CPU:     {router_mb:8.1f} MB")
        print(f"  Per-token PCIe fetch (active):   {expert_mb * active_pct:8.1f} MB")
        print(f"  vs. full --n-cpu-moe fetch:      {expert_mb:8.1f} MB")
        print(f"  PCIe traffic reduction:          {saved_vs_full_offload / expert_mb * 100:7.1f}%")

    if router_mb > 0:
        print(f"\n  ⚠️  Router tensors on CPU ({router_mb:.1f}MB) will add PCIe latency to routing.")
        print(f"     Consider keeping ffn_gate_inp and exp_probs on GPU for faster routing.")

    print()


def print_comparison_chart(baseline: OtAnalysis, modified: OtAnalysis, label_a: str, label_b: str):
    """Print side-by-side layer comparison for two -ot configurations."""
    print(f"  COMPARISON: {label_a} vs {label_b}")
    print(f"  {'─' * 105}")
    print(f"  {'Lyr':>4s}  {'A GPU':>7s} {'A CPU':>7s}  │  {'B GPU':>7s} {'B CPU':>7s}  │  {'Delta':>8s}  {'Bar'}")
    print(f"  {'─' * 105}")

    for layer_idx in sorted(set(baseline.placements.keys()) | set(modified.placements.keys())):
        pa = baseline.placements.get(layer_idx)
        pb = modified.placements.get(layer_idx)

        a_gpu = pa.gpu_total_mb if pa else 0
        a_cpu = pa.cpu_total_mb if pa else 0
        b_gpu = pb.gpu_total_mb if pb else 0
        b_cpu = pb.cpu_total_mb if pb else 0
        delta = b_gpu - a_gpu

        # Visualization: A left, B right
        max_mb = max(a_gpu + a_cpu, b_gpu + b_cpu, 1)
        a_bar_len = int((a_gpu + a_cpu) / max_mb * 15)
        b_bar_len = int((b_gpu + b_cpu) / max_mb * 15)
        a_gpu_len = int(a_gpu / max_mb * 15)
        b_gpu_len = int(b_gpu / max_mb * 15)

        a_bar = "█" * a_gpu_len + "░" * (a_bar_len - a_gpu_len)
        b_bar = "█" * b_gpu_len + "░" * (b_bar_len - b_gpu_len)

        delta_str = f"{delta:+.1f}MB" if abs(delta) > 0.05 else "="
        print(f"  L{layer_idx:02d}  {a_gpu:6.1f} {a_cpu:6.1f}  │  {b_gpu:6.1f} {b_cpu:6.1f}  │  {delta_str:>8s}  {a_bar}→{b_bar}")

    print(f"  {'─' * 105}")
    print()


def print_optimization_suggestions(analysis: OtAnalysis):
    """Print suggestions for optimizing the -ot pattern."""
    print(f"  OPTIMIZATION SUGGESTIONS:")
    print(f"  {'─' * 90}")

    suggestions = []

    # Check if router tensors are moved to CPU
    router_on_cpu = any(
        "gate_inp" in t.name or "exp_probs" in t.name
        for t, _ in analysis.moved_tensors
    )
    if router_on_cpu:
        suggestions.append(
            "⚠️  Router tensors (gate_inp, exp_probs) are on CPU.\n"
            "     This adds PCIe latency to every routing decision.\n"
            "     Consider: keep them on GPU (they're small: ~0.4MB/layer)\n"
            "     Pattern: blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_*_shexp=CPU"
        )

    # Check if expert weights are split
    expert_patterns = [p for p in analysis.patterns if "exps" in p.tensor_pattern or "shexp" in p.tensor_pattern]
    if expert_patterns:
        moved_experts = sum(t.size_mb for t, _ in analysis.moved_tensors if "exps" in t.name or "shexp" in t.name)
        if moved_experts > 0:
            active_pct = 8 / 64
            fetched = moved_experts * active_pct
            suggestions.append(
                f"✅ Expert weights ({moved_experts:.0f}MB) moved to CPU.\n"
                f"     Active fetch: {fetched:.0f}MB per token ({fetched/moved_experts*100:.0f}% of total).\n"
                f"     PCIe bandwidth: ~12GB/s (PCIe 4.0 x16).\n"
                f"     Latency: ~{fetched/12:.1f}ms per layer per token."
            )
        else:
            suggestions.append(
                "⚠️  Expert patterns specified but no expert tensors were moved.\n"
                "     Check pattern syntax and layer ranges."
            )

    # Check headroom
    if analysis.fits and analysis.headroom_mb > 500:
        suggestions.append(
            f"💡 {analysis.headroom_mb:.0f}MB headroom available.\n"
            "     You could fit more layers on GPU, or increase context size."
        )
    elif not analysis.fits:
        suggestions.append(
            f"❌ OOM: {abs(analysis.headroom_mb):.0f}MB over budget.\n"
            "     Move more layers to CPU, or reduce context size."
        )

    # Check if all layers are offloaded
    all_cpu = all(
        all(d == "CPU" for _, d in p.tensors if "exps" in _.name or "shexp" in _.name)
        for p in analysis.placements.values() if p.is_moe
    )
    if all_cpu and analysis.moved_tensors:
        suggestions.append(
            "📋 All expert weights are on CPU — equivalent to --n-cpu-moe.\n"
            "     For selective offloading, use a smaller layer range (e.g., blk.30-46)."
        )

    if not suggestions:
        suggestions.append("✅ Configuration looks optimal.")

    for i, s in enumerate(suggestions, 1):
        for line in s.split("\n"):
            print(f"  {line}")
        print()


def print_llama_command(analysis: OtAnalysis, patterns: list[OtPattern]):
    """Print the llama.cpp command with the -ot pattern."""
    if not patterns:
        return

    print(f"  LLAMA.CPP COMMAND:")
    print(f"  {'─' * 90}")
    ot_str = ",".join(p.raw for p in patterns)
    print(f"  -ot '{ot_str}'")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# SHOW-ALL MODE
# ══════════════════════════════════════════════════════════════════════════════

def print_all_patterns(tensors: list[TensorInfo], gpu: str = "3070", context: int = 16384):
    """Show all possible -ot patterns and their VRAM impact."""
    print(f"\n  ALL POSSIBLE -OT PATTERNS:")
    print(f"  {'─' * 100}")

    # Discover unique tensor name patterns per layer
    # Group by tensor base name (strip layer prefix)
    tensor_bases = {}  # base_name -> (example_tensor, size_per_layer)
    for t in tensors:
        if t.layer < 0:
            continue
        # Strip layer prefix
        base = re.sub(r"blk\.\d+\.", "blk.N.", t.name)
        if base not in tensor_bases:
            tensor_bases[base] = (t, t.size_mb)
        else:
            # Accumulate size for tensors with multiple parts
            existing = tensor_bases[base]
            tensor_bases[base] = (existing[0], existing[1] + t.size_mb)

    # Common patterns to suggest
    patterns_to_show = [
        ("Expert weights only", "ffn_*_exps", "Move only expert FFN weights to CPU"),
        ("Expert + shared", "ffn_*_exps,ffn_*_shexp", "Expert weights + shared expert"),
        ("Expert + router", "ffn_*_exps,ffn_gate_inp", "Expert weights + gating computation"),
        ("Full offload", "ffn_*_exps,ffn_*_shexp,ffn_gate_inp,exp_probs_b", "All MoE tensors to CPU"),
        ("Attention offload", "attn_*", "Move attention to CPU (unusual)"),
    ]

    # Find a sample MoE layer range
    moe_layers = sorted(set(t.layer for t in tensors if t.layer >= 1))
    if not moe_layers:
        print("  No MoE layers found in model.")
        return

    first_moe = moe_layers[0]
    last_moe = moe_layers[-1]
    sample_range = f"blk.{first_moe}-{last_moe}"

    # Calculate baseline
    baseline = analyze_baseline(tensors, gpu, context)

    fmt = "{:<30s} {:>10s} {:>10s} {:>10s}  {}"
    print(fmt.format("Pattern", "GPU MB", "CPU MB", "VRAM Δ", "Description"))
    print(f"  {'─' * 100}")

    for desc, tensor_pats, explanation in patterns_to_show:
        # Build -ot string
        ot_parts = [f"{sample_range}.{p}=CPU" for p in tensor_pats.split(",")]
        ot_str = ",".join(ot_parts)

        try:
            patterns = parse_ot_string(ot_str)
            analysis = analyze_ot_impact(tensors, patterns, gpu, context)
            delta = analysis.gpu_total_mb - baseline.gpu_total_mb
            delta_str = f"{delta:+.1f}MB"

            print(fmt.format(
                desc[:30],
                f"{analysis.gpu_total_mb:.0f}",
                f"{analysis.cpu_total_mb:.0f}",
                delta_str,
                explanation,
            ))
        except Exception as e:
            print(fmt.format(desc[:30], "ERROR", "", "", str(e)[:40]))

    print()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="MoE -ot Pattern Visualizer — see exactly what -ot patterns move",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s model.gguf --gpu 3070 --context 16384 \\
    --ot "blk.15-46.ffn_*_exps=CPU"

  %(prog)s --demo --gpu 3070 --context 16384 \\
    --ot "blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_*_shexp=CPU,blk.15-46.ffn_gate_inp=CPU,blk.15-46.exp_probs_b=CPU"

  %(prog)s model.gguf --gpu 3070 \\
    --ot-a "blk.15-46.ffn_*_exps=CPU" \\
    --ot-b "blk.15-46.ffn_*_exps=CPU,blk.15-46.ffn_gate_inp=CPU"

  %(prog)s model.gguf --gpu 3070 --show-all
        """)
    parser.add_argument("model", nargs="?", help="Path to GGUF file (omit with --demo)")
    parser.add_argument("--gpu", default="3070", choices=list(GPUS.keys()), help="Target GPU")
    parser.add_argument("--context", "-c", type=int, default=16384, help="Context size (tokens)")
    parser.add_argument("--ot", help="-ot pattern string (comma-separated)")
    parser.add_argument("--ot-a", help="First -ot pattern for comparison mode")
    parser.add_argument("--ot-b", help="Second -ot pattern for comparison mode")
    parser.add_argument("--show-all", action="store_true", help="Show all possible -ot patterns")
    parser.add_argument("--demo", action="store_true", help="Use synthetic demo data (no GGUF file needed)")
    parser.add_argument("--json", action="store_true", help="Output analysis as JSON")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()

    # Load tensors
    if args.demo:
        tensors = generate_demo_tensors()
        model_name = "GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S (demo)"
    elif args.model:
        if not os.path.exists(args.model):
            print(f"  Error: file not found: {args.model}", file=sys.stderr)
            sys.exit(1)
        tensors = read_gguf_tensors(args.model)
        model_name = Path(args.model).stem
    else:
        print("  Error: provide a GGUF file path or use --demo", file=sys.stderr)
        sys.exit(1)

    # Count MoE layers
    moe_layers = sorted(set(t.layer for t in tensors if t.layer >= 1))
    total_layers = max((t.layer for t in tensors if t.layer >= 0), default=0) + 1
    total_size_gb = sum(t.size_bytes for t in tensors) / 1e9

    if not args.quiet:
        print_header(args.gpu, args.context, model_name)
        print(f"  Tensors: {len(tensors)} | Layers: {total_layers} | MoE layers: {len(moe_layers)} | Size: {total_size_gb:.2f} GB")

    # JSON output
    if args.json:
        ot_str = args.ot or ""
        patterns = parse_ot_string(ot_str) if ot_str else []
        analysis = analyze_ot_impact(tensors, patterns, args.gpu, args.context)
        output = {
            "model": model_name,
            "gpu": args.gpu,
            "context": args.context,
            "total_tensors": len(tensors),
            "total_layers": total_layers,
            "moe_layers": len(moe_layers),
            "patterns": [p.raw for p in patterns],
            "gpu_total_mb": analysis.gpu_total_mb,
            "cpu_total_mb": analysis.cpu_total_mb,
            "headroom_mb": analysis.headroom_mb,
            "fits": analysis.fits,
            "moved_tensors": len(analysis.moved_tensors),
            "moved_total_mb": analysis.moved_total_mb,
        }
        print(json.dumps(output, indent=2))
        return

    # Show-all mode
    if args.show_all:
        print_all_patterns(tensors, args.gpu, args.context)
        return

    # Comparison mode
    if args.ot_a and args.ot_b:
        patterns_a = parse_ot_string(args.ot_a)
        patterns_b = parse_ot_string(args.ot_b)

        baseline = analyze_baseline(tensors, args.gpu, args.context)
        analysis_a = analyze_ot_impact(tensors, patterns_a, args.gpu, args.context)
        analysis_b = analyze_ot_impact(tensors, patterns_b, args.gpu, args.context)

        print(f"\n  PATTERN A: {args.ot_a}")
        print(f"  PATTERN B: {args.ot_b}")
        print()

        print_pattern_summary(patterns_a)
        print_pattern_summary(patterns_b)

        print_vram_comparison(baseline, analysis_a)
        print_vram_comparison(baseline, analysis_b)

        print_comparison_chart(analysis_a, analysis_b, "Pattern A", "Pattern B")

        print_optimization_suggestions(analysis_b)
        return

    # Single pattern mode
    patterns = parse_ot_string(args.ot) if args.ot else []
    baseline = analyze_baseline(tensors, args.gpu, args.context)
    analysis = analyze_ot_impact(tensors, patterns, args.gpu, args.context)

    print_pattern_summary(patterns)

    if patterns:
        print_vram_comparison(baseline, analysis)

    print_vram_bar(analysis)
    print_layer_chart(analysis)

    if patterns:
        print_tensor_detail(analysis)
        print_pci_traffic_insight(analysis)
        print_optimization_suggestions(analysis)
        print_llama_command(analysis, patterns)
    else:
        # Baseline info
        print(f"  BASELINE (no -ot patterns):")
        print(f"  All {len(tensors)} tensors are on GPU")
        print(f"  GPU total: {analysis.gpu_total_mb:.1f} MB / {analysis.gpu_vram_mb} MB")
        print(f"  Headroom: {analysis.headroom_mb:.1f} MB")
        if analysis.fits:
            print(f"  ✅ Model fits in VRAM")
        else:
            print(f"  ❌ OOM — use --ot to offload expert weights to CPU")
        print()
        print(f"  TIP: Try --ot 'blk.15-46.ffn_*_exps=CPU' to offload expert weights")
        print(f"       while keeping routing computation on GPU.")
        print()


if __name__ == "__main__":
    main()
