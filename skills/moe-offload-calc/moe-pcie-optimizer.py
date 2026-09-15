#!/usr/bin/env python3
"""
MoE PCIe Traffic Optimizer — Model, estimate, and score PCIe traffic patterns
for CPU-offloaded Mixture of Experts inference.

Models the PCIe data path between CPU RAM and GPU VRAM for MoE expert tensors,
estimates latency for different offload strategies, and scores them on a
multi-dimensional cost function (latency, VRAM headroom, RAM pressure, routing overhead).

Supports:
  - Full GPU (baseline)
  - --n-cpu-moe (bulk expert offload)
  - -ot patterns (selective tensor offload)
  - Hybrid strategies (partial offload with fine-grained tensor control)
  - Naive, Smart, Pipelined strategies (3-tier expert caching)
  - Mermaid diagram output for all visualizations
  - ASCII art diagrams for PCIe traffic and data flow
  - JSON output for tool composition with moe-calculator.py and moe-ot-visualizer.py
  - Activation profile input for cache hit modeling
  - Demo mode with synthetic data (no GGUF needed)

Usage:
    python3 moe-pcie-optimizer.py model.gguf --gpu 3070 --context 16384
    python3 moe-pcie-optimizer.py model.gguf --gpu 3070 --context 65536 --strategy hybrid
    python3 moe-pcie-optimizer.py --demo --gpu 3070 --context 16384 --mermaid
    python3 moe-pcie-optimizer.py --demo --gpu 3070 --compare-all --json
    python3 moe-pcie-optimizer.py model.gguf --gpu 3070 --profile heatmap.json
    python3 moe-pcie-optimizer.py model.gguf --gpu 3070 --diagram
"""

import argparse
import json
import math
import os
import re
import struct
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS — PCIe bandwidth model (per-lane, from Triton + dsh-hub)
# ══════════════════════════════════════════════════════════════════════════════

# PCIe bandwidth per lane (GB/s, bidirectional, theoretical peak)
PCIE_BW = {
    "3.0": 1.969,   # PCIe 3.0: ~2 GB/s per lane
    "4.0": 3.938,   # PCIe 4.0: ~4 GB/s per lane
    "5.0": 7.877,   # PCIe 5.0: ~8 GB/s per lane
}

# DDR5 bandwidth reference (GB/s, theoretical peak)
DDR5_BW = 51.2     # DDR5-6400 dual channel

# CPU-GPU copy latency overhead (microseconds, fixed cost per transfer)
PCIE_COPY_OVERHEAD_US = 5.0

# Realistic effective H2D bandwidth per GPU (bytes/sec) — overrides from Triton
# When a GPU key is present here, it takes precedence over the per-lane calculation.
PCIE_BANDWIDTH_OVERRIDE = {
    "3070": 13e9,    # PCIe 3.0 x16 ~13 GB/s (effective)
    "3090": 25e9,    # PCIe 4.0 x16 ~25 GB/s
    "4090": 25e9,    # PCIe 4.0 x16 ~25 GB/s
    "5090": 50e9,    # PCIe 5.0 x16 ~50 GB/s
}

# Activation tensor size per layer per token (bytes)
ACTIVATION_PER_LAYER = 8192  # ~8 KB (hidden_dim * 2 for fp16)

# Expert activation ratio (top-8 out of 64 = 12.5%)
EXPERT_ACTIVATION_RATIO = 8 / 64

# Cache hit rates (from research)
CACHE_HIT_RATES = {
    "hot_gpu": 0.87,    # 87% hit for hot experts in GPU
    "warm_cpu": 0.67,   # 67% hit for warm experts in CPU RAM
    "cold_disk": 0.0,   # 0% hit for cold experts on disk
}

# PCIe latency tiers
PCIE_LATENCY = {
    "gpu_hit_ms": 0.0,       # Expert already in GPU VRAM — no transfer
    "cpu_hit_ms": 0.2,       # Expert in CPU RAM — fast DDR read
    "disk_hit_ms": 10.0,     # Expert on NVMe SSD
    "cold_miss_ms": 100.0,   # Expert on HDD / swap
}

# KV cache bytes per token per layer (conservative estimate)
KV_BYTES_PER_TOKEN_PER_LAYER = 2048 * 2  # 4 KB per token per layer (fp16)

# KV cache overhead (CUDA context, model metadata)
KV_CACHE_OVERHEAD_MB = 500

# Baseline throughput for full-GPU reference (tokens/sec)
BASELINE_TPS_FULL_GPU = 24.0

# Expert size heuristic (MB per expert per layer, typical for Q4 quantized MoE)
EXPERT_SIZE_MB_HEURISTIC = 10.0


# ══════════════════════════════════════════════════════════════════════════════
# GPU DATABASE — merged: dsh-hub's richer entries + Triton's bandwidth overrides
# ══════════════════════════════════════════════════════════════════════════════

GPUS = {
    "3060-12":  {"name": "RTX 3060 12GB",  "vram_mb": 12288, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3060-ti":  {"name": "RTX 3060 Ti 8GB", "vram_mb": 8192,  "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3070":     {"name": "RTX 3070 8GB",    "vram_mb": 8192,  "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3070-ti":  {"name": "RTX 3070 Ti 8GB", "vram_mb": 8192,  "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3080":     {"name": "RTX 3080 10GB",   "vram_mb": 10240, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3080-ti":  {"name": "RTX 3080 Ti 12GB","vram_mb": 12288, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3090":     {"name": "RTX 3090 24GB",   "vram_mb": 24576, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "3090-ti":  {"name": "RTX 3090 Ti 24GB","vram_mb": 24576, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "4060":     {"name": "RTX 4060 8GB",    "vram_mb": 8192,  "arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 8},
    "4060-ti":  {"name": "RTX 4060 Ti 8GB", "vram_mb": 8192,  "arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 8},
    "4070":     {"name": "RTX 4070 12GB",   "vram_mb": 12288, "arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 16},
    "4070-ti":  {"name": "RTX 4070 Ti 12GB","vram_mb": 12288, "arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 16},
    "4070-ti-s":{"name": "RTX 4070 Ti Super 16GB","vram_mb": 16384,"arch":"ada",  "pcie_gen": "4.0", "pcie_lanes": 16},
    "4080":     {"name": "RTX 4080 16GB",   "vram_mb": 16384, "arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 16},
    "4080-s":   {"name": "RTX 4080 Super 16GB","vram_mb":16384,"arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 16},
    "4090":     {"name": "RTX 4090 24GB",   "vram_mb": 24576, "arch": "ada",    "pcie_gen": "4.0", "pcie_lanes": 16},
    "5070":     {"name": "RTX 5070 12GB",   "vram_mb": 12288, "arch": "blackwell","pcie_gen":"5.0", "pcie_lanes": 16},
    "5070-ti":  {"name": "RTX 5070 Ti 16GB","vram_mb": 16384, "arch": "blackwell","pcie_gen":"5.0", "pcie_lanes": 16},
    "5080":     {"name": "RTX 5080 16GB",   "vram_mb": 16384, "arch": "blackwell","pcie_gen":"5.0", "pcie_lanes": 16},
    "5090":     {"name": "RTX 5090 32GB",   "vram_mb": 32768, "arch": "blackwell","pcie_gen":"5.0", "pcie_lanes": 16},
    "a100-40":  {"name": "A100 40GB",       "vram_mb": 40960, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "a100-80":  {"name": "A100 80GB",       "vram_mb": 81920, "arch": "ampere", "pcie_gen": "4.0", "pcie_lanes": 16},
    "h100":     {"name": "H100 80GB",       "vram_mb": 81920, "arch": "hopper", "pcie_gen": "5.0", "pcie_lanes": 16},
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

class StrategyType(Enum):
    FULL_GPU = "full_gpu"
    N_CPU_MOE = "n_cpu_moe"
    OT_PATTERN = "ot_pattern"
    HYBRID = "hybrid"
    NAIVE = "naive"
    SMART = "smart"
    PIPELINED = "pipelined"


@dataclass
class LayerInfo:
    """Per-layer tensor breakdown."""
    layer: int
    is_dense: bool = False
    attn: int = 0
    expert_exps: int = 0
    expert_shexp: int = 0
    router_gate: int = 0
    router_probs: int = 0
    ffn_single: int = 0
    ffn_norm: int = 0
    other: int = 0

    @property
    def expert_total(self) -> int:
        return self.expert_exps + self.expert_shexp

    @property
    def total(self) -> int:
        return (self.attn + self.expert_exps + self.expert_shexp +
                self.router_gate + self.router_probs + self.ffn_single +
                self.ffn_norm + self.other)

    @property
    def total_mb(self) -> float:
        return self.total / 1e6

    @property
    def expert_total_mb(self) -> float:
        return self.expert_total / 1e6


@dataclass
class ModelInfo:
    """Complete model tensor analysis."""
    path: str
    name: str
    total_size_bytes: int
    layers: dict
    total_layers: int
    moe_layers: list
    dense_layers: list
    n_experts: Optional[int] = None
    n_active: Optional[int] = None
    arch: str = "unknown"

    @property
    def total_mb(self) -> float:
        return self.total_size_bytes / 1e6

    @property
    def attn_total_mb(self) -> float:
        return sum(l.attn for l in self.layers.values()) / 1e6

    @property
    def expert_total_mb(self) -> float:
        return sum(l.expert_total for l in self.layers.values()) / 1e6

    @property
    def router_total_mb(self) -> float:
        return sum(l.router_gate + l.router_probs for l in self.layers.values()) / 1e6

    @property
    def expert_per_layer_mb(self) -> float:
        if not self.moe_layers:
            return 0
        return self.expert_total_mb / len(self.moe_layers)


@dataclass
class GPUInfo:
    """GPU hardware specifications (Triton-style)."""
    key: str
    name: str
    vram_mb: int
    pcie_gen: int
    pcie_width: int
    arch: str
    bandwidth_bytes: int

    @property
    def bandwidth_gbps(self) -> float:
        return self.bandwidth_bytes / 1e9

    @property
    def vram_gb(self) -> float:
        return self.vram_mb / 1024


@dataclass
class PcieTransfer:
    """A single PCIe transfer event."""
    layer: int
    tensor_name: str
    size_bytes: int
    direction: str  # "cpu_to_gpu" or "gpu_to_cpu"

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1e6


@dataclass
class StrategyResult:
    """Result of evaluating an offload strategy."""
    strategy_type: StrategyType
    name: str
    description: str
    gpu_layers: list       # layers with expert weights on GPU
    cpu_layers: list       # layers with expert weights on CPU
    transfers: list = field(default_factory=list)  # list of PcieTransfer per token
    gpu_vram_used_mb: float = 0.0
    gpu_headroom_mb: float = 0.0
    cpu_ram_needed_mb: float = 0.0
    pcie_bytes_per_token: int = 0
    pcie_bandwidth_gb_s: float = 0.0
    latency_per_token_ms: float = 0.0
    routing_overhead_ms: float = 0.0
    total_inference_overhead_ms: float = 0.0
    fits_vram: bool = True
    score: float = 0.0           # composite score (0-100, higher is better)
    score_breakdown: dict = field(default_factory=dict)  # component scores
    # Triton-style extended fields
    hot_experts_on_gpu: int = 0
    warm_experts_on_cpu: int = 0
    cold_experts_on_disk: int = 0
    activation_transfer_kb: float = 0.0
    weight_fetch_kb: float = 0.0
    total_pcie_kb_per_token: float = 0.0
    pcie_bandwidth_utilization_pct: float = 0.0
    expert_fetch_latency_ms: float = 0.0
    cpu_compute_latency_ms: float = 0.0
    gpu_compute_latency_ms: float = 0.0
    pipeline_overlap_ms: float = 0.0
    estimated_tokens_per_second: float = 0.0
    expert_coverage_pct: float = 0.0
    cache_hit_rate: float = 0.0
    rating: str = ""
    llama_command: str = ""
    mermaid: str = ""
    ascii_art: str = ""


@dataclass
class OptimizationResult:
    """The answer to 'what should I do?' (Triton-style)."""
    recommended: StrategyResult
    alternatives: list
    context: int
    cache_type: str
    model: ModelInfo
    gpu: GPUInfo

    def to_dict(self) -> dict:
        return {
            "model": self.model.name,
            "gpu": self.gpu.name,
            "context": self.context,
            "cache_type": self.cache_type,
            "recommended": _strategy_to_dict(self.recommended),
            "alternatives": [_strategy_to_dict(a) for a in self.alternatives],
        }


def _strategy_to_dict(r: StrategyResult) -> dict:
    """Serialize a StrategyResult to dict (handles both styles)."""
    return {
        "type": r.strategy_type.value,
        "name": r.name,
        "description": r.description,
        "gpu_layers": len(r.gpu_layers),
        "cpu_layers": len(r.cpu_layers),
        "gpu_vram_used_mb": round(r.gpu_vram_used_mb, 1),
        "gpu_headroom_mb": round(r.gpu_headroom_mb, 1),
        "cpu_ram_needed_mb": round(r.cpu_ram_needed_mb, 1),
        "pcie_bytes_per_token": r.pcie_bytes_per_token,
        "latency_per_token_ms": round(r.latency_per_token_ms, 3),
        "routing_overhead_ms": round(r.routing_overhead_ms, 3),
        "total_overhead_ms": round(r.total_inference_overhead_ms, 3),
        "fits_vram": r.fits_vram,
        "score": round(r.score, 1),
        "score_breakdown": r.score_breakdown,
        # Triton-style fields
        "hot_experts_on_gpu": r.hot_experts_on_gpu,
        "warm_experts_on_cpu": r.warm_experts_on_cpu,
        "cold_experts_on_disk": r.cold_experts_on_disk,
        "activation_transfer_kb": round(r.activation_transfer_kb, 2),
        "weight_fetch_kb": round(r.weight_fetch_kb, 2),
        "total_pcie_kb_per_token": round(r.total_pcie_kb_per_token, 2),
        "pcie_bandwidth_utilization_pct": round(r.pcie_bandwidth_utilization_pct, 2),
        "expert_fetch_latency_ms": round(r.expert_fetch_latency_ms, 4),
        "gpu_compute_latency_ms": round(r.gpu_compute_latency_ms, 4),
        "estimated_tokens_per_second": round(r.estimated_tokens_per_second, 2),
        "expert_coverage_pct": round(r.expert_coverage_pct, 1),
        "cache_hit_rate": round(r.cache_hit_rate, 3),
        "rating": r.rating,
        "llama_command": r.llama_command,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GGUF READER — Triton's version with manual fallback, adapted to LayerInfo
# ══════════════════════════════════════════════════════════════════════════════

def read_gguf_tensors(path: str) -> list:
    """Read tensor names and sizes from a GGUF file."""
    try:
        import gguf
        reader = gguf.GGUFReader(path)
        return [(t.name, t.n_bytes) for t in reader.tensors]
    except ImportError:
        return _read_gguf_manual(path)


def _read_gguf_manual(path: str) -> list[tuple[str, int]]:
    """Fallback: manual GGUF header parsing (no gguf dependency)."""
    tensors = []
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            raise ValueError(f"Not a GGUF file: {path}")
        f.read(4)  # version
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        f.read(8)  # metadata_kv_count

        # Skip metadata
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            key_len = struct.unpack("<Q", f.read(8))[0]
            f.seek(key_len, 1)
            val_type = struct.unpack("<I", f.read(4))[0]
            _skip_gguf_value(f, val_type)

        # Read tensor info
        for _ in range(n_tensors):
            name_len = struct.unpack("<Q", f.read(8))[0]
            name = f.read(name_len).decode("utf-8").rstrip("\x00")
            n_dims = struct.unpack("<I", f.read(4))[0]
            dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(n_dims)]
            dtype = struct.unpack("<I", f.read(4))[0]
            offset = struct.unpack("<Q", f.read(8))[0]
            nbytes = math.prod(dims)
            dtype_sizes = {0: 4, 1: 2, 2: 1, 10: 1, 11: 2}
            if dtype in dtype_sizes:
                nbytes *= dtype_sizes[dtype]
            else:
                nbytes = max(64, nbytes)
            tensors.append((name, nbytes))

    return tensors


def _skip_gguf_value(f, val_type):
    """Skip a GGUF metadata value."""
    if val_type == 8:  # STRING
        str_len = struct.unpack("<Q", f.read(8))[0]
        f.seek(str_len, 1)
    elif val_type == 9:  # ARRAY
        arr_type = struct.unpack("<I", f.read(4))[0]
        arr_len = struct.unpack("<Q", f.read(8))[0]
        for _ in range(arr_len):
            _skip_gguf_value(f, arr_type)
    else:
        type_sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1}
        f.read(type_sizes.get(val_type, 4))


# ══════════════════════════════════════════════════════════════════════════════
# MODEL ANALYZER — dsh-hub's LayerInfo-based analysis
# ══════════════════════════════════════════════════════════════════════════════

def analyze_model(path: str) -> ModelInfo:
    """Analyze a GGUF file and extract MoE structure."""
    tensors = read_gguf_tensors(path)
    name = Path(path).stem
    layers = {}
    moe_layers = []
    dense_layers = []

    for tname, size in tensors:
        parts = tname.split(".")
        if parts[0] != "blk":
            continue
        layer = int(parts[1])
        if layer not in layers:
            layers[layer] = LayerInfo(layer=layer)
        l = layers[layer]

        if "attn" in tname:
            l.attn += size
        elif "ffn_gate_up_exps" in tname or "ffn_gate_exps" in tname:
            l.expert_exps += size
        elif "ffn_down_exps" in tname:
            l.expert_exps += size
        elif "ffn_up_exps" in tname:
            l.expert_exps += size
        elif "ffn_gate_shexp" in tname or "ffn_down_shexp" in tname or "ffn_up_shexp" in tname:
            l.expert_shexp += size
        elif "ffn_gate_inp" in tname:
            l.router_gate += size
        elif "exp_probs" in tname:
            l.router_probs += size
        elif "ffn_gate" in tname and "exps" not in tname and "shexp" not in tname and "inp" not in tname:
            l.ffn_single += size
        elif "ffn_down" in tname and "exps" not in tname and "shexp" not in tname:
            l.ffn_single += size
        elif "ffn_up" in tname and "exps" not in tname and "shexp" not in tname:
            l.ffn_single += size
        elif "ffn_norm" in tname:
            l.ffn_norm += size
        else:
            l.other += size

    for layer, info in sorted(layers.items()):
        if info.expert_total > 0:
            moe_layers.append(layer)
            info.is_dense = False
        else:
            dense_layers.append(layer)
            info.is_dense = True

    total_size = sum(size for _, size in tensors)

    # Detect architecture
    arch = "unknown"
    if moe_layers and len(layers) > 10:
        if 0 in dense_layers:
            arch = "deepseek2"
        else:
            arch = "qwen3moe"

    return ModelInfo(
        path=path, name=name, total_size_bytes=total_size,
        layers=layers, total_layers=len(layers),
        moe_layers=moe_layers, dense_layers=dense_layers,
        arch=arch,
    )


# ══════════════════════════════════════════════════════════════════════════════
# DEMO DATA — dsh-hub's synthetic model
# ══════════════════════════════════════════════════════════════════════════════

def generate_demo_model() -> ModelInfo:
    """Generate synthetic MoE model data (GLM-4.7-like)."""
    layers = {}
    moe_layers = []
    dense_layers = []

    for layer in range(47):
        l = LayerInfo(layer=layer)
        l.attn = 15_135_744
        l.ffn_norm = 8_192

        if layer == 0:
            l.ffn_single = 11_796_480
            l.is_dense = True
            dense_layers.append(layer)
        else:
            if layer < 5:
                l.expert_exps = 273_678_336
                l.expert_shexp = 6_488_064
            else:
                l.expert_exps = 254_803_968
                l.expert_shexp = 6_905_856
            l.router_gate = 393_408
            l.is_dense = False
            moe_layers.append(layer)

        layers[layer] = l

    total_size = sum(l.total for l in layers.values())
    return ModelInfo(
        path="/demo/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S.gguf",
        name="GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S",
        total_size_bytes=total_size,
        layers=layers, total_layers=47,
        moe_layers=moe_layers, dense_layers=dense_layers,
        n_experts=64, n_active=8,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PCIe TRAFFIC MODEL — merged: dsh-hub's per-lane + Triton's PcieTrafficModel
# ══════════════════════════════════════════════════════════════════════════════

def get_pcie_bandwidth(gpu_key: str) -> float:
    """Get effective PCIe bandwidth in GB/s for a GPU.

    Priority:
    1. Per-GPU override from PCIE_BANDWIDTH_OVERRIDE (Triton)
    2. Per-lane calculation: PCIE_BW[gen] * lanes * 0.80 (dsh-hub)
    """
    if gpu_key in PCIE_BANDWIDTH_OVERRIDE:
        return PCIE_BANDWIDTH_OVERRIDE[gpu_key] / 1e9
    gpu = GPUS.get(gpu_key)
    if not gpu:
        return 25.0  # safe default
    gen = gpu["pcie_gen"]
    lanes = gpu["pcie_lanes"]
    per_lane = PCIE_BW.get(gen, 3.938)
    theoretical = per_lane * lanes
    return theoretical * 0.80


def calculate_pcie_latency(size_bytes: int, gpu_key: str) -> float:
    """Calculate PCIe transfer latency in milliseconds for a given transfer size."""
    bw_gbs = get_pcie_bandwidth(gpu_key)
    if bw_gbs <= 0:
        return float('inf')
    transfer_time_s = (size_bytes / 1e9) / bw_gbs
    overhead_s = PCIE_COPY_OVERHEAD_US / 1e6
    return (transfer_time_s + overhead_s) * 1000


def estimate_active_expert_bytes_per_token(model: ModelInfo, n_active: int = 8) -> int:
    """Estimate bytes transferred per token for active experts on CPU."""
    if not model.moe_layers:
        return 0
    sample = model.layers[model.moe_layers[0]]
    if sample.expert_total == 0:
        return 0
    n_experts = 64
    if model.n_experts:
        n_experts = model.n_experts
    bytes_per_expert = sample.expert_total // n_experts
    return bytes_per_expert * n_active


class PcieTrafficModel:
    """Models PCIe data transfer for MoE inference (Triton-style).

    Core equation:
        transfer_time_ms = (data_kb / 1024) / bandwidth_gbps * 1000
    """

    def __init__(self, gpu_key: str):
        self.gpu_key = gpu_key
        self.bandwidth_gbps = get_pcie_bandwidth(gpu_key)
        self.bandwidth_bytes = self.bandwidth_gbps * 1e9

    def activation_transfer_kb(self, n_cpu_layers: int) -> float:
        """KB of activation tensors transferred per token for n offloaded layers."""
        return n_cpu_layers * ACTIVATION_PER_LAYER / 1024

    def weight_fetch_kb(self, expert_size_mb: float, n_misses: int) -> float:
        """KB of expert weights fetched on cold cache misses."""
        return n_misses * expert_size_mb * 1024

    def transfer_time_ms(self, data_kb: float) -> float:
        """PCIe transfer time in ms for a given data volume in KB."""
        data_bytes = data_kb * 1024
        return (data_bytes / self.bandwidth_bytes) * 1000

    def bandwidth_utilization(self, data_kb: float, tok_per_sec: float) -> float:
        """Percentage of PCIe bandwidth used at given throughput."""
        data_bytes_per_sec = data_kb * 1024 * tok_per_sec
        return (data_bytes_per_sec / self.bandwidth_bytes) * 100


# ══════════════════════════════════════════════════════════════════════════════
# EXPERT CACHE SIMULATOR — Triton's 3-tier caching model
# ══════════════════════════════════════════════════════════════════════════════

class ExpertCacheSimulator:
    """Simulates 3-tier expert caching: GPU VRAM -> CPU RAM -> Disk.

    Hit rate model:
        - Uniform: gpu_slots / n_total
        - With profile: hot experts pinned in GPU, higher hit rate
    """

    def __init__(self, n_experts: int, n_active: int, expert_size_mb: float):
        self.n_experts = n_experts
        self.n_active = n_active
        self.expert_size_mb = expert_size_mb

    def gpu_cache_slots(self, vram_for_experts_mb: float) -> int:
        """How many experts fit in GPU VRAM cache."""
        if self.expert_size_mb <= 0:
            return 0
        return int(vram_for_experts_mb / self.expert_size_mb)

    def simulate(
        self,
        gpu_cache_slots: int,
        activation_profile: Optional[dict] = None,
        token_count: int = 1000,
    ) -> dict:
        """Simulate cache hit rates. Returns dict with hit rates and distribution."""
        if activation_profile is not None and "hot_experts" in activation_profile:
            hot_count = min(len(activation_profile["hot_experts"]), gpu_cache_slots)
            hot_coverage = activation_profile.get("avg_top8_coverage", 0.94)

            warmup = min(1.0, token_count / 50)
            hit_rate = min(hot_coverage, gpu_cache_slots / self.n_active) * warmup

            hot_on_gpu = hot_count
            warm_on_cpu = min(self.n_experts - hot_count, gpu_cache_slots * 2)
            cold_on_disk = self.n_experts - hot_on_gpu - warm_on_cpu
        else:
            hit_rate = min(1.0, gpu_cache_slots / self.n_experts) if self.n_experts > 0 else 0
            hot_on_gpu = gpu_cache_slots
            warm_on_cpu = min(self.n_experts - gpu_cache_slots, gpu_cache_slots * 3)
            cold_on_disk = max(0, self.n_experts - hot_on_gpu - warm_on_cpu)

        miss_rate = 1.0 - hit_rate
        cpu_miss_frac = 0.8
        disk_miss_frac = 0.2
        avg_miss_ms = (
            cpu_miss_frac * PCIE_LATENCY["cpu_hit_ms"] +
            disk_miss_frac * PCIE_LATENCY["disk_hit_ms"]
        )
        effective_ms = hit_rate * PCIE_LATENCY["gpu_hit_ms"] + miss_rate * avg_miss_ms

        return {
            "hit_rate": hit_rate,
            "miss_rate": miss_rate,
            "hot_on_gpu": hot_on_gpu,
            "warm_on_cpu": warm_on_cpu,
            "cold_on_disk": cold_on_disk,
            "effective_fetch_ms": effective_ms,
            "cold_miss_rate": miss_rate * disk_miss_frac,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE OVERLAPPER — Triton's layer-level pipelining model
# ══════════════════════════════════════════════════════════════════════════════

class PipelineOverlapper:
    """Models layer-level pipelining: overlap PCIe transfers with GPU compute.

    Key insight: While GPU computes layer N, PCIe transfers data for layer N+1.
    """

    @staticmethod
    def compute_overlap(
        n_offloaded_layers: int,
        gpu_ms: float,
        pcie_ms: float,
    ) -> dict:
        """Model pipeline overlap for offloaded layers.

        Without pipelining: total = n * (gpu + pcie)
        With pipelining: total = gpu + (n-1) * max(gpu, pcie) + pcie
        """
        if n_offloaded_layers <= 0:
            return {"effective_ms": 0, "overlap_ms": 0, "bottleneck": "none"}

        if n_offloaded_layers == 1:
            effective = gpu_ms + pcie_ms
        else:
            effective = (
                gpu_ms +
                (n_offloaded_layers - 1) * max(gpu_ms, pcie_ms) +
                pcie_ms
            )

        overlap_per_layer = min(gpu_ms, pcie_ms)
        total_overlap = overlap_per_layer * max(0, n_offloaded_layers - 1)

        naive_total = n_offloaded_layers * (gpu_ms + pcie_ms)

        if gpu_ms > pcie_ms * 1.1:
            bottleneck = "gpu"
        elif pcie_ms > gpu_ms * 1.1:
            bottleneck = "pcie"
        else:
            bottleneck = "balanced"

        return {
            "effective_ms": effective,
            "overlap_ms": total_overlap,
            "naive_ms": naive_total,
            "savings_pct": ((naive_total - effective) / naive_total * 100) if naive_total > 0 else 0,
            "bottleneck": bottleneck,
        }


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY EVALUATORS — all strategies from both versions
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_full_gpu(model: ModelInfo, gpu_key: str, context: int, cache: str) -> StrategyResult:
    """Evaluate full GPU strategy (no offloading)."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    gpu_total = base_mb + model.expert_total_mb + kv_mb
    headroom = vram - gpu_total

    return StrategyResult(
        strategy_type=StrategyType.FULL_GPU,
        name="Full GPU",
        description=f"All {model.total_layers} layers on GPU, no CPU offload",
        gpu_layers=list(model.moe_layers),
        cpu_layers=[],
        transfers=[],
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=500,
        pcie_bytes_per_token=0,
        pcie_bandwidth_gb_s=get_pcie_bandwidth(gpu_key),
        latency_per_token_ms=0.0,
        routing_overhead_ms=0.0,
        total_inference_overhead_ms=0.0,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
    )


def evaluate_n_cpu_moe(model: ModelInfo, gpu_key: str, context: int, cache: str,
                       n_cpu_layers: int) -> StrategyResult:
    """Evaluate --n-cpu-moe strategy."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    n_gpu_moe = max(0, len(model.moe_layers) - n_cpu_layers)
    gpu_moe_layers = model.moe_layers[:n_gpu_moe]
    cpu_moe_layers = model.moe_layers[n_gpu_moe:]

    gpu_expert_mb = sum(model.layers[l].expert_total for l in gpu_moe_layers) / 1e6
    cpu_expert_mb = sum(model.layers[l].expert_total for l in cpu_moe_layers) / 1e6

    gpu_total = base_mb + gpu_expert_mb + kv_mb
    headroom = vram - gpu_total

    transfers = []
    for l in cpu_moe_layers:
        layer_info = model.layers[l]
        if layer_info.expert_exps > 0:
            transfers.append(PcieTransfer(l, "expert_exps", layer_info.expert_exps, "cpu_to_gpu"))
        if layer_info.expert_shexp > 0:
            transfers.append(PcieTransfer(l, "expert_shexp", layer_info.expert_shexp, "cpu_to_gpu"))
        if layer_info.router_gate > 0:
            transfers.append(PcieTransfer(l, "router_gate", layer_info.router_gate, "cpu_to_gpu"))
        if layer_info.router_probs > 0:
            transfers.append(PcieTransfer(l, "router_probs", layer_info.router_probs, "cpu_to_gpu"))

    total_bytes = sum(t.size_bytes for t in transfers)
    latency_ms = calculate_pcie_latency(total_bytes, gpu_key)
    routing_overhead = len(cpu_moe_layers) * 0.05

    return StrategyResult(
        strategy_type=StrategyType.N_CPU_MOE,
        name=f"n-cpu-moe ({n_cpu_layers} layers)",
        description=f"Offload {n_cpu_layers}/{len(model.moe_layers)} MoE layers to CPU",
        gpu_layers=gpu_moe_layers,
        cpu_layers=cpu_moe_layers,
        transfers=transfers,
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=cpu_expert_mb + 500,
        pcie_bytes_per_token=total_bytes,
        pcie_bandwidth_gb_s=get_pcie_bandwidth(gpu_key),
        latency_per_token_ms=latency_ms,
        routing_overhead_ms=routing_overhead,
        total_inference_overhead_ms=latency_ms + routing_overhead,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
    )


def evaluate_ot_pattern(model: ModelInfo, gpu_key: str, context: int, cache: str,
                        ot_str: str) -> StrategyResult:
    """Evaluate -ot pattern strategy."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    rules = _parse_ot_string(ot_str)

    gpu_moe_layers = []
    cpu_moe_layers = []
    transfers = []
    gpu_expert_mb = 0.0
    cpu_expert_mb = 0.0

    for layer in model.moe_layers:
        l = model.layers[layer]
        layer_on_gpu = True

        for rule in rules:
            if layer >= rule["start"] and layer <= rule["end"]:
                if rule["pattern"] in ("ffn_*_exps", "ffn_gate_up_exps", "ffn_down_exps", "ffn_up_exps"):
                    layer_on_gpu = False
                    break

        if layer_on_gpu:
            gpu_moe_layers.append(layer)
            gpu_expert_mb += l.expert_total / 1e6
        else:
            cpu_moe_layers.append(layer)
            cpu_expert_mb += l.expert_total / 1e6
            if l.expert_exps > 0:
                transfers.append(PcieTransfer(layer, "expert_exps", l.expert_exps, "cpu_to_gpu"))
            if l.expert_shexp > 0:
                transfers.append(PcieTransfer(layer, "expert_shexp", l.expert_shexp, "cpu_to_gpu"))

    gpu_total = base_mb + gpu_expert_mb + kv_mb
    headroom = vram - gpu_total

    n_active = model.n_active or 8
    n_experts = model.n_experts or 64
    active_fraction = n_active / n_experts

    active_bytes = int(sum(t.size_bytes for t in transfers) * active_fraction)
    bw = get_pcie_bandwidth(gpu_key)
    latency_ms = calculate_pcie_latency(active_bytes, gpu_key)
    routing_overhead = len(cpu_moe_layers) * 0.01

    return StrategyResult(
        strategy_type=StrategyType.OT_PATTERN,
        name="ot-pattern",
        description=f"-ot pattern: {ot_str[:60]}...",
        gpu_layers=gpu_moe_layers,
        cpu_layers=cpu_moe_layers,
        transfers=transfers,
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=cpu_expert_mb + 500,
        pcie_bytes_per_token=active_bytes,
        pcie_bandwidth_gb_s=bw,
        latency_per_token_ms=latency_ms,
        routing_overhead_ms=routing_overhead,
        total_inference_overhead_ms=latency_ms + routing_overhead,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
    )


def evaluate_hybrid(model: ModelInfo, gpu_key: str, context: int, cache: str,
                    gpu_expert_cap_mb: float = 0.0) -> StrategyResult:
    """Evaluate hybrid strategy: keep some expert layers on GPU, offload rest with -ot."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    vram_for_experts = vram - kv_mb - base_mb - 200

    if gpu_expert_cap_mb > 0:
        target_gpu_expert = gpu_expert_cap_mb
    else:
        avg_expert = model.expert_total_mb / len(model.moe_layers) if model.moe_layers else 0
        n_gpu = int(vram_for_experts / avg_expert) if avg_expert > 0 else 0
        n_gpu = max(0, min(n_gpu, len(model.moe_layers)))
        target_gpu_expert = sum(model.layers[l].expert_total for l in model.moe_layers[:n_gpu]) / 1e6

    gpu_moe_layers = []
    cpu_moe_layers = []
    transfers = []
    gpu_expert_mb = 0.0
    cpu_expert_mb = 0.0

    for layer in model.moe_layers:
        l = model.layers[layer]
        if gpu_expert_mb + l.expert_total_mb <= target_gpu_expert:
            gpu_moe_layers.append(layer)
            gpu_expert_mb += l.expert_total_mb
        else:
            cpu_moe_layers.append(layer)
            cpu_expert_mb += l.expert_total_mb
            if l.expert_exps > 0:
                transfers.append(PcieTransfer(layer, "expert_exps", l.expert_exps, "cpu_to_gpu"))
            if l.expert_shexp > 0:
                transfers.append(PcieTransfer(layer, "expert_shexp", l.expert_shexp, "cpu_to_gpu"))

    gpu_total = base_mb + gpu_expert_mb + kv_mb
    headroom = vram - gpu_total

    n_active = model.n_active or 8
    n_experts = model.n_experts or 64
    active_fraction = n_active / n_experts
    active_bytes = int(sum(t.size_bytes for t in transfers) * active_fraction)

    bw = get_pcie_bandwidth(gpu_key)
    latency_ms = calculate_pcie_latency(active_bytes, gpu_key)
    routing_overhead = len(cpu_moe_layers) * 0.01

    return StrategyResult(
        strategy_type=StrategyType.HYBRID,
        name=f"hybrid ({len(gpu_moe_layers)} GPU + {len(cpu_moe_layers)} CPU)",
        description=f"Auto-optimal: {len(gpu_moe_layers)} layers on GPU, {len(cpu_moe_layers)} on CPU with selective fetch",
        gpu_layers=gpu_moe_layers,
        cpu_layers=cpu_moe_layers,
        transfers=transfers,
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=cpu_expert_mb + 500,
        pcie_bytes_per_token=active_bytes,
        pcie_bandwidth_gb_s=bw,
        latency_per_token_ms=latency_ms,
        routing_overhead_ms=routing_overhead,
        total_inference_overhead_ms=latency_ms + routing_overhead,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
    )


# ══════════════════════════════════════════════════════════════════════════════
# TRITON-STYLE STRATEGY EVALUATORS (naive, smart, pipelined)
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_naive(model: ModelInfo, gpu_key: str, context: int, cache: str,
                   activation_profile: Optional[dict] = None) -> StrategyResult:
    """Evaluate naive strategy: all experts on CPU, compute on CPU."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    gpu_total = base_mb + kv_mb
    headroom = vram - gpu_total

    n_cpu_moe = len(model.moe_layers)
    cpu_expert_mb = model.expert_total_mb

    traffic = PcieTrafficModel(gpu_key)
    cache_sim = ExpertCacheSimulator(
        model.n_experts or 64, model.n_active or 8, model.expert_per_layer_mb
    )

    activation_kb = traffic.activation_transfer_kb(n_cpu_moe)
    total_pcie_kb = activation_kb * 2  # round-trip
    pcie_transfer_ms = traffic.transfer_time_ms(total_pcie_kb)

    gpu_compute_ms = model.total_layers * 0.5
    cpu_compute_ms = n_cpu_moe * (model.n_active or 8) * 5.0 * 0.01

    total_ms = gpu_compute_ms + cpu_compute_ms + pcie_transfer_ms
    tps = 1000.0 / total_ms if total_ms > 0 else 0

    return StrategyResult(
        strategy_type=StrategyType.NAIVE,
        name="naive",
        description="All experts on CPU, compute on CPU (--cpu-moe style)",
        gpu_layers=[],
        cpu_layers=list(model.moe_layers),
        transfers=[],
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=cpu_expert_mb + 500,
        pcie_bytes_per_token=int(total_pcie_kb * 1024),
        pcie_bandwidth_gb_s=get_pcie_bandwidth(gpu_key),
        latency_per_token_ms=pcie_transfer_ms,
        routing_overhead_ms=0.0,
        total_inference_overhead_ms=total_ms,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
        activation_transfer_kb=activation_kb,
        total_pcie_kb_per_token=total_pcie_kb,
        gpu_compute_latency_ms=gpu_compute_ms,
        cpu_compute_latency_ms=cpu_compute_ms,
        estimated_tokens_per_second=tps,
        llama_command=f"./llama-server -m {model.name}.gguf --n-cpu-moe {n_cpu_moe} --ctx-size {context} --cache-type-k {cache} --cache-type-v {cache}",
    )


def evaluate_smart(model: ModelInfo, gpu_key: str, context: int, cache: str,
                   activation_profile: Optional[dict] = None) -> StrategyResult:
    """Evaluate smart strategy: 3-tier caching — hot experts in GPU, warm in CPU RAM, cold on disk."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    vram_for_experts = vram - kv_mb - base_mb - 200

    traffic = PcieTrafficModel(gpu_key)
    cache_sim = ExpertCacheSimulator(
        model.n_experts or 64, model.n_active or 8, model.expert_per_layer_mb
    )

    avg_per_layer = model.expert_per_layer_mb + (model.router_total_mb / len(model.moe_layers) if model.moe_layers else 0)
    n_gpu_moe = int(vram_for_experts / avg_per_layer) if avg_per_layer > 0 else 0
    n_gpu_moe = max(0, min(n_gpu_moe, len(model.moe_layers)))
    n_cpu_moe = len(model.moe_layers) - n_gpu_moe

    gpu_moe_layers = model.moe_layers[:n_gpu_moe]
    cpu_moe_layers = model.moe_layers[n_gpu_moe:]

    gpu_expert_mb = sum(model.layers[l].expert_total for l in gpu_moe_layers) / 1e6
    cpu_expert_mb = sum(model.layers[l].expert_total for l in cpu_moe_layers) / 1e6

    gpu_total = base_mb + gpu_expert_mb + kv_mb
    headroom = vram - gpu_total

    gpu_slots = cache_sim.gpu_cache_slots(vram_for_experts)
    cache_result = cache_sim.simulate(gpu_slots, activation_profile)
    hit_rate = cache_result["hit_rate"]

    activation_kb = traffic.activation_transfer_kb(n_cpu_moe)
    cold_misses = cache_result["miss_rate"] * (model.n_active or 8)
    weight_fetch_kb = traffic.weight_fetch_kb(model.expert_per_layer_mb, cold_misses)
    total_pcie_kb = activation_kb + weight_fetch_kb

    pcie_transfer_ms = traffic.transfer_time_ms(total_pcie_kb) if n_cpu_moe > 0 else 0
    gpu_compute_ms = model.total_layers * 0.5 + n_gpu_moe * 0.3
    cpu_compute_ms = n_cpu_moe * (model.n_active or 8) * 5.0 * (1 - hit_rate) * 0.01

    total_ms = gpu_compute_ms + pcie_transfer_ms + cpu_compute_ms
    tps = 1000.0 / total_ms if total_ms > 0 else 0
    bw_util = traffic.bandwidth_utilization(total_pcie_kb, tps) if total_pcie_kb > 0 else 0

    hot_experts = cache_result["hot_on_gpu"]
    coverage = (hot_experts / (model.n_active or 8) * 100) if (model.n_active or 8) > 0 else 0

    return StrategyResult(
        strategy_type=StrategyType.SMART,
        name="smart",
        description="3-tier caching — hot experts in GPU, warm in CPU RAM, cold on disk",
        gpu_layers=gpu_moe_layers,
        cpu_layers=cpu_moe_layers,
        transfers=[],
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=cpu_expert_mb + 500,
        pcie_bytes_per_token=int(total_pcie_kb * 1024),
        pcie_bandwidth_gb_s=get_pcie_bandwidth(gpu_key),
        latency_per_token_ms=pcie_transfer_ms,
        routing_overhead_ms=0.0,
        total_inference_overhead_ms=total_ms,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
        hot_experts_on_gpu=cache_result["hot_on_gpu"],
        warm_experts_on_cpu=cache_result["warm_on_cpu"],
        cold_experts_on_disk=cache_result["cold_on_disk"],
        activation_transfer_kb=activation_kb,
        weight_fetch_kb=weight_fetch_kb,
        total_pcie_kb_per_token=total_pcie_kb,
        pcie_bandwidth_utilization_pct=bw_util,
        expert_fetch_latency_ms=cache_result["effective_fetch_ms"],
        gpu_compute_latency_ms=gpu_compute_ms,
        cpu_compute_latency_ms=cpu_compute_ms,
        estimated_tokens_per_second=tps,
        expert_coverage_pct=coverage,
        cache_hit_rate=hit_rate,
        llama_command=f"./llama-server -m {model.name}.gguf --n-cpu-moe {n_cpu_moe} --ctx-size {context} --cache-type-k {cache} --cache-type-v {cache}",
    )


def evaluate_pipelined(model: ModelInfo, gpu_key: str, context: int, cache: str,
                       activation_profile: Optional[dict] = None) -> StrategyResult:
    """Evaluate pipelined strategy: smart caching + overlap PCIe transfers with GPU compute."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache, 1.0)
    kv_mb = (context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB

    base_mb = model.attn_total_mb + model.router_total_mb + \
              sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    vram_for_experts = vram - kv_mb - base_mb - 200

    traffic = PcieTrafficModel(gpu_key)
    cache_sim = ExpertCacheSimulator(
        model.n_experts or 64, model.n_active or 8, model.expert_per_layer_mb
    )
    overlapper = PipelineOverlapper()

    avg_per_layer = model.expert_per_layer_mb + (model.router_total_mb / len(model.moe_layers) if model.moe_layers else 0)
    n_gpu_moe = int(vram_for_experts / avg_per_layer) if avg_per_layer > 0 else 0
    n_gpu_moe = max(0, min(n_gpu_moe, len(model.moe_layers)))
    n_cpu_moe = len(model.moe_layers) - n_gpu_moe

    gpu_moe_layers = model.moe_layers[:n_gpu_moe]
    cpu_moe_layers = model.moe_layers[n_gpu_moe:]

    gpu_expert_mb = sum(model.layers[l].expert_total for l in gpu_moe_layers) / 1e6
    cpu_expert_mb = sum(model.layers[l].expert_total for l in cpu_moe_layers) / 1e6

    gpu_total = base_mb + gpu_expert_mb + kv_mb
    headroom = vram - gpu_total

    gpu_slots = cache_sim.gpu_cache_slots(vram_for_experts)
    cache_result = cache_sim.simulate(gpu_slots, activation_profile)
    hit_rate = cache_result["hit_rate"]

    activation_kb = traffic.activation_transfer_kb(n_cpu_moe)
    cold_misses = cache_result["miss_rate"] * (model.n_active or 8)
    weight_fetch_kb = traffic.weight_fetch_kb(model.expert_per_layer_mb, cold_misses)
    total_pcie_kb = activation_kb + weight_fetch_kb

    pcie_transfer_ms = traffic.transfer_time_ms(total_pcie_kb) if n_cpu_moe > 0 else 0
    gpu_compute_ms = model.total_layers * 0.5 + n_gpu_moe * 0.3
    cpu_compute_ms = n_cpu_moe * (model.n_active or 8) * 5.0 * (1 - hit_rate) * 0.01

    overlap_result = overlapper.compute_overlap(n_cpu_moe, gpu_compute_ms, pcie_transfer_ms)
    overlap_ms = overlap_result["overlap_ms"]
    effective_pcie_ms = overlap_result["effective_ms"]

    total_ms = gpu_compute_ms + effective_pcie_ms + cpu_compute_ms
    tps = 1000.0 / total_ms if total_ms > 0 else 0
    bw_util = traffic.bandwidth_utilization(total_pcie_kb, tps) if total_pcie_kb > 0 else 0

    hot_experts = cache_result["hot_on_gpu"]
    coverage = (hot_experts / (model.n_active or 8) * 100) if (model.n_active or 8) > 0 else 0

    return StrategyResult(
        strategy_type=StrategyType.PIPELINED,
        name="pipelined",
        description="Smart caching + overlap PCIe transfers with GPU compute",
        gpu_layers=gpu_moe_layers,
        cpu_layers=cpu_moe_layers,
        transfers=[],
        gpu_vram_used_mb=gpu_total,
        gpu_headroom_mb=headroom,
        cpu_ram_needed_mb=cpu_expert_mb + 500,
        pcie_bytes_per_token=int(total_pcie_kb * 1024),
        pcie_bandwidth_gb_s=get_pcie_bandwidth(gpu_key),
        latency_per_token_ms=effective_pcie_ms,
        routing_overhead_ms=0.0,
        total_inference_overhead_ms=total_ms,
        fits_vram=headroom > 0,
        score=0.0,
        score_breakdown={},
        hot_experts_on_gpu=cache_result["hot_on_gpu"],
        warm_experts_on_cpu=cache_result["warm_on_cpu"],
        cold_experts_on_disk=cache_result["cold_on_disk"],
        activation_transfer_kb=activation_kb,
        weight_fetch_kb=weight_fetch_kb,
        total_pcie_kb_per_token=total_pcie_kb,
        pcie_bandwidth_utilization_pct=bw_util,
        expert_fetch_latency_ms=cache_result["effective_fetch_ms"],
        gpu_compute_latency_ms=gpu_compute_ms,
        cpu_compute_latency_ms=cpu_compute_ms,
        pipeline_overlap_ms=overlap_ms,
        estimated_tokens_per_second=tps,
        expert_coverage_pct=coverage,
        cache_hit_rate=hit_rate,
        llama_command=f"./llama-server -m {model.name}.gguf --n-cpu-moe {n_cpu_moe} --ctx-size {context} --cache-type-k {cache} --cache-type-v {cache}",
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCORING ENGINE — dsh-hub's 4-dimension scoring
# ══════════════════════════════════════════════════════════════════════════════

def score_strategy(result: StrategyResult, model: ModelInfo, gpu_key: str) -> StrategyResult:
    """Score a strategy on multiple dimensions (0-100, higher is better)."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    # Dimension 1: VRAM efficiency (0-25)
    if result.fits_vram:
        usage_pct = result.gpu_vram_used_mb / vram * 100
        if usage_pct > 95:
            vram_score = 20
        elif usage_pct > 80:
            vram_score = 25
        elif usage_pct > 60:
            vram_score = 18
        else:
            vram_score = 10
    else:
        vram_score = 0

    # Dimension 2: Latency (0-25)
    latency = result.total_inference_overhead_ms
    if latency <= 0:
        latency_score = 25
    elif latency <= 1.0:
        latency_score = 23
    elif latency <= 5.0:
        latency_score = 20
    elif latency <= 15.0:
        latency_score = 15
    elif latency <= 50.0:
        latency_score = 10
    else:
        latency_score = 5

    # Dimension 3: RAM pressure (0-25)
    ram_gb = result.cpu_ram_needed_mb / 1024
    if ram_gb <= 2:
        ram_score = 25
    elif ram_gb <= 4:
        ram_score = 22
    elif ram_gb <= 8:
        ram_score = 18
    elif ram_gb <= 16:
        ram_score = 12
    else:
        ram_score = 5

    # Dimension 4: Routing efficiency (0-25)
    routing_overhead = result.routing_overhead_ms
    if routing_overhead <= 0.1:
        routing_score = 25
    elif routing_overhead <= 0.5:
        routing_score = 22
    elif routing_overhead <= 2.0:
        routing_score = 18
    elif routing_overhead <= 5.0:
        routing_score = 12
    else:
        routing_score = 5

    total = vram_score + latency_score + ram_score + routing_score

    if not result.fits_vram:
        total = min(total, 30)

    result.score = total
    result.score_breakdown = {
        "vram_efficiency": vram_score,
        "latency": latency_score,
        "ram_pressure": ram_score,
        "routing_efficiency": routing_score,
    }

    # Also compute Triton-style rating
    if total >= 85:
        result.rating = "★★★★★"
    elif total >= 70:
        result.rating = "★★★★☆"
    elif total >= 55:
        result.rating = "★★★☆☆"
    elif total >= 40:
        result.rating = "★★☆☆☆"
    else:
        result.rating = "★☆☆☆☆"

    return result


# ══════════════════════════════════════════════════════════════════════════════
# OT PATTERN PARSER — from dsh-hub
# ══════════════════════════════════════════════════════════════════════════════

def _parse_ot_string(ot_str: str) -> list:
    """Parse -ot string into structured rules."""
    rules = []
    for part in ot_str.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        tensor_part, device = part.rsplit("=", 1)
        device = device.strip().upper()
        m = re.match(r"blk\.(\d+)(?:-(\d+))?\.(.+)", tensor_part)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
            pattern = m.group(3)
            rules.append({"start": start, "end": end, "pattern": pattern, "device": device})
    return rules


# ══════════════════════════════════════════════════════════════════════════════
# MERMAID DIAGRAM GENERATORS — from dsh-hub
# ══════════════════════════════════════════════════════════════════════════════

def generate_mermaid_pcie_flow(result: StrategyResult, model: ModelInfo, gpu_key: str) -> str:
    """Generate Mermaid diagram showing PCIe data flow."""
    gpu = GPUS[gpu_key]
    bw = result.pcie_bandwidth_gb_s

    lines = ["```mermaid"]
    lines.append("graph LR")
    lines.append("    subgraph CPU[\"CPU / RAM\"]")
    lines.append("        RAM[\"DDR5 RAM\"]")

    if result.cpu_layers:
        lines.append("        EXP[\"Expert Weights<br/>\" + str(round(result.cpu_ram_needed_mb - 500)) + \" MB\"]")
        lines.append("        RAM --> EXP")
    else:
        lines.append("        RAM[\"DDR5 RAM<br/>(minimal usage)\"]")

    lines.append("    end")
    lines.append("")
    lines.append("    subgraph PCIe[\"PCIe Bus\"]")
    lines.append(f"        BW[\"{bw:.1f} GB/s effective\"]")
    if result.cpu_layers:
        lines.append(f"        TR[\"{result.pcie_bytes_per_token / 1e6:.1f} MB/token\"]")
        lines.append("        BW --> TR")
    else:
        lines.append("        BW[\"No PCIe transfer\"]")
    lines.append("    end")
    lines.append("")
    lines.append(f"    subgraph GPU[\"GPU — {gpu['name']}\"]")

    if result.gpu_layers:
        lines.append("        VRAM[\"VRAM\"]")
        lines.append(f"        GW[\"GPU Expert Weights<br/>{result.gpu_vram_used_mb:.0f} MB\"]")
        lines.append("        KV[\"KV Cache\"]")
        lines.append("        ROUTE[\"Router/Gating\"]")
        lines.append("        VRAM --> GW")
        lines.append("        VRAM --> KV")
        lines.append("        VRAM --> ROUTE")
    else:
        lines.append("        VRAM[\"VRAM<br/>(no model weights)\"]")

    lines.append("    end")
    lines.append("")

    if result.cpu_layers and result.gpu_layers:
        lines.append("    EXP -->|\"PCIe x16\"| GW")
    elif result.cpu_layers:
        lines.append("    EXP -->|\"PCIe x16\"| VRAM")

    lines.append("```")
    return "\n".join(lines)


def generate_mermaid_strategy_comparison(results: list) -> str:
    """Generate Mermaid bar chart comparing strategies."""
    lines = ["```mermaid"]
    lines.append("xychart-beta")
    lines.append('    title "Strategy Comparison — PCIe Latency per Token"')
    lines.append('    x-axis [' + ", ".join(f'"{r.name[:20]}"' for r in results) + ']')
    lines.append('    y-axis "Latency (ms)" 0 --> ' + str(max(r.total_inference_overhead_ms * 1.2 for r in results) or 10))
    lines.append('    bar [' + ", ".join(f'{r.total_inference_overhead_ms:.2f}' for r in results) + ']')
    lines.append("```")
    return "\n".join(lines)


def generate_mermaid_vram_chart(results: list, gpu_vram: int) -> str:
    """Generate Mermaid chart showing VRAM usage across strategies."""
    lines = ["```mermaid"]
    lines.append("xychart-beta")
    lines.append('    title "VRAM Usage by Strategy"')
    lines.append('    x-axis [' + ", ".join(f'"{r.name[:20]}"' for r in results) + ']')
    lines.append(f'    y-axis "MB" 0 --> {gpu_vram}')
    lines.append('    bar [' + ", ".join(f'{r.gpu_vram_used_mb:.0f}' for r in results) + ']')
    lines.append('    line [' + ", ".join(f'{gpu_vram}' for _ in results) + ']')
    lines.append("```")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# ASCII ART GENERATORS — from both versions
# ══════════════════════════════════════════════════════════════════════════════

def generate_ascii_pcie_diagram(result: StrategyResult, model: ModelInfo, gpu_key: str) -> str:
    """Generate ASCII art diagram of PCIe traffic flow (dsh-hub style)."""
    gpu = GPUS[gpu_key]
    bw = result.pcie_bandwidth_gb_s

    lines = []
    lines.append("")
    lines.append("  ╔══════════════════════════════════════════════════════════════════════════════╗")
    lines.append(f"  ║  PCIe DATA FLOW — {result.name}")
    lines.append(f"  ║  GPU: {gpu['name']} | PCIe {gpu['pcie_gen']} x{gpu['pcie_lanes']} | BW: {bw:.1f} GB/s")
    lines.append("  ╠══════════════════════════════════════════════════════════════════════════════╣")
    lines.append("  ║")
    lines.append("  ║  ┌─────────────────────┐        ┌──────────────────┐        ┌───────────────────────────┐")
    lines.append("  ║  │      CPU / RAM      │◄──────►│     PCIe Bus     │◄──────►│          GPU              │")
    lines.append("  ║  │                     │        │                  │        │                           │")

    if result.cpu_layers:
        cpu_mb = result.cpu_ram_needed_mb - 500
        lines.append(f"  ║  │  Expert Weights     │        │  {result.pcie_bytes_per_token/1e6:6.1f} MB/token │        │  VRAM: {result.gpu_vram_used_mb:.0f}/{gpu['vram_mb']} MB")
        lines.append(f"  ║  │  {cpu_mb:6.1f} MB          │        │  {bw:4.1f} GB/s        │        │  Headroom: {result.gpu_headroom_mb:+.0f} MB")
    else:
        lines.append(f"  ║  │  (minimal)          │        │  No transfer     │        │  VRAM: {result.gpu_vram_used_mb:.0f}/{gpu['vram_mb']} MB")
        lines.append(f"  ║  │                     │        │                  │        │  Headroom: {result.gpu_headroom_mb:+.0f} MB")

    lines.append("  ║  └─────────────────────┘        └──────────────────┘        └───────────────────────────┘")
    lines.append("  ║")

    if result.transfers:
        lines.append("  ║  TRANSFER BREAKDOWN (per token):")
        lines.append("  ║  ┌──────────┬────────────────────┬──────────┬──────────────┐")
        lines.append("  ║  │  Layer   │  Tensor            │  Size    │  Latency     │")
        lines.append("  ║  ├──────────┼────────────────────┼──────────┼──────────────┤")
        for t in result.transfers[:10]:
            lat = calculate_pcie_latency(t.size_bytes, gpu_key)
            lines.append(f"  ║  │  L{t.layer:02d}     │  {t.tensor_name:18s} │  {t.size_mb:5.1f}MB │  {lat:6.2f} ms    │")
        if len(result.transfers) > 10:
            lines.append(f"  ║  │  ...     │  ({len(result.transfers)-10} more)          │          │              │")
        lines.append("  ║  └──────────┴────────────────────┴──────────┴──────────────┘")
    else:
        lines.append("  ║  No PCIe transfers needed (full GPU)")

    lines.append("  ║")
    lines.append("  ╚══════════════════════════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def generate_ascii_layer_map(result: StrategyResult, model: ModelInfo) -> str:
    """Generate ASCII art showing layer placement (dsh-hub style)."""
    lines = []
    lines.append("")
    lines.append("  LAYER PLACEMENT MAP:")
    lines.append("  " + "─" * 80)

    gpu_set = set(result.gpu_layers)
    cpu_set = set(result.cpu_layers)

    for layer in sorted(model.layers.keys()):
        l = model.layers[layer]
        marker = ""
        if l.is_dense:
            marker = "▓▓▓ GPU (dense) ▓▓▓"
        elif layer in gpu_set:
            marker = "████ GPU expert ████"
        elif layer in cpu_set:
            marker = "░░░░ CPU expert ░░░░"
        else:
            marker = "    (no expert)    "

        expert_mb = l.expert_total_mb
        bar_len = min(int(expert_mb / 10), 40) if expert_mb > 0 else 0

        if layer in gpu_set:
            bar = "█" * bar_len
        elif layer in cpu_set:
            bar = "░" * bar_len
        else:
            bar = "·" * 5

        lines.append(f"  L{layer:02d}  {marker}  {expert_mb:6.1f}MB  {bar}")

    lines.append("  " + "─" * 80)
    lines.append(f"  Legend: █ = GPU  ░ = CPU  ▓ = Dense  Total layers: {model.total_layers}")
    return "\n".join(lines)


def print_data_flow_diagram():
    """Print the MoE inference data flow diagram (Triton style)."""
    print()
    print("┌─────────────────────────────────────────────────────────────────┐")
    print("│                    MoE Inference Data Flow                       │")
    print("├─────────────────────────────────────────────────────────────────┤")
    print("│                                                                   │")
    print("│  Token Input                                                      │")
    print("│      │                                                            │")
    print("│      ▼                                                            │")
    print("│  ┌──────────────┐                                                │")
    print("│  │ Embedding    │  GPU (always)                                  │")
    print("│  └──────┬───────┘                                                │")
    print("│         │                                                         │")
    print("│         ▼                                                         │")
    print("│  ┌──────────────┐     ┌──────────────┐                           │")
    print("│  │ Attention    │────▶│ KV Cache     │  GPU (always)             │")
    print("│  │ Layer N      │◀────│              │                           │")
    print("│  └──────┬───────┘     └──────────────┘                           │")
    print("│         │                                                         │")
    print("│         ▼                                                         │")
    print("│  ┌──────────────┐                                                │")
    print("│  │ Router/Gate  │  GPU (fast, small tensors)                     │")
    print("│  │ Select top-K │                                                │")
    print("│  └──────┬───────┘                                                │")
    print("│         │                                                         │")
    print("│         ▼                                                         │")
    print("│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │")
    print("│  │ Expert FFN   │◀───▶│ PCIe Bus     │◀───▶│ Expert Cache │     │")
    print("│  │ (8 active)   │     │ ~25 GB/s     │     │ GPU/CPU/Disk │     │")
    print("│  └──────┬───────┘     └──────────────┘     └──────────────┘     │")
    print("│         │                                                         │")
    print("│         ▼                                                         │")
    print("│  ┌──────────────┐                                                │")
    print("│  │ LayerNorm    │  GPU (always)                                  │")
    print("│  └──────┬───────┘                                                │")
    print("│         │                                                         │")
    print("│         ▼                                                         │")
    print("│  Token Output                                                     │")
    print("└─────────────────────────────────────────────────────────────────┘")


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY — merged report styles
# ══════════════════════════════════════════════════════════════════════════════

def print_strategy_report(result: StrategyResult, model: ModelInfo, gpu_key: str,
                          verbose: bool = True):
    """Print full strategy report (dsh-hub style)."""
    gpu = GPUS[gpu_key]

    print()
    print("=" * 100)
    print(f"  MoE PCIe OPTIMIZER — {result.name}")
    print(f"  GPU: {gpu['name']} ({gpu['vram_mb']} MB) | PCIe {gpu['pcie_gen']} x{gpu['pcie_lanes']}")
    print(f"  Model: {model.name} | Layers: {model.total_layers} ({len(model.moe_layers)} MoE)")
    print("=" * 100)

    # Summary
    print()
    print(f"  STRATEGY: {result.description}")
    print(f"  GPU expert layers: {len(result.gpu_layers)} | CPU expert layers: {len(result.cpu_layers)}")
    if result.gpu_layers:
        print(f"  GPU expert range: L{result.gpu_layers[0]:02d}-L{result.gpu_layers[-1]:02d}")
    if result.cpu_layers:
        print(f"  CPU expert range: L{result.cpu_layers[0]:02d}-L{result.cpu_layers[-1]:02d}")
    print()

    # VRAM breakdown
    print(f"  VRAM USAGE:")
    print(f"  {'─' * 70}")
    print(f"  {'Component':25s} {'MB':>8s} {'% of VRAM':>10s}")
    print(f"  {'─' * 70}")
    print(f"  {'GPU Expert Weights':25s} {result.gpu_vram_used_mb - result.gpu_headroom_mb - 200:8.1f}")
    print(f"  {'Headroom':25s} {result.gpu_headroom_mb:8.1f}")
    print(f"  {'Total VRAM':25s} {gpu['vram_mb']:8d}")
    fits = "✅ FITS" if result.fits_vram else "❌ OOM"
    print(f"  Status: {fits}")
    print()

    # PCIe traffic
    print(f"  PCIe TRAFFIC MODEL:")
    print(f"  {'─' * 70}")
    print(f"  {'Bandwidth':25s} {result.pcie_bandwidth_gb_s:8.1f} GB/s")
    print(f"  {'Data per token':25s} {result.pcie_bytes_per_token / 1e6:8.1f} MB")
    print(f"  {'Transfer latency':25s} {result.latency_per_token_ms:8.2f} ms")
    print(f"  {'Routing overhead':25s} {result.routing_overhead_ms:8.2f} ms")
    print(f"  {'Total overhead':25s} {result.total_inference_overhead_ms:8.2f} ms")
    if result.total_inference_overhead_ms > 0:
        print(f"  {'Throughput impact':25s} ~{1000 / result.total_inference_overhead_ms:.0f} tokens/sec PCIe-bound")
    else:
        print(f"  {'Throughput impact':25s} No PCIe bottleneck")
    print()

    # RAM
    print(f"  RAM REQUIREMENT:")
    print(f"  {'─' * 70}")
    print(f"  {'CPU expert weights':25s} {result.cpu_ram_needed_mb - 500:8.1f} MB")
    print(f"  {'OS/system overhead':25s} {'500':>8s} MB")
    print(f"  {'Total RAM needed':25s} {result.cpu_ram_needed_mb:8.1f} MB ({result.cpu_ram_needed_mb/1024:.1f} GB)")
    print()

    # Score
    print(f"  STRATEGY SCORE: {result.score:.0f}/100 {result.rating}")
    print(f"  {'─' * 70}")
    for dim, val in result.score_breakdown.items():
        bar = "█" * (val // 2) + "░" * ((25 - val) // 2)
        print(f"  {dim:25s} {val:2d}/25 {bar}")
    print()

    # Triton-style extended info
    if result.cache_hit_rate > 0 or result.estimated_tokens_per_second > 0:
        print(f"  CACHE / THROUGHPUT (Triton model):")
        print(f"  {'─' * 70}")
        if result.cache_hit_rate > 0:
            print(f"  {'Cache hit rate':25s} {result.cache_hit_rate*100:8.1f}%")
        if result.expert_coverage_pct > 0:
            print(f"  {'Expert coverage':25s} {result.expert_coverage_pct:8.1f}%")
        if result.estimated_tokens_per_second > 0:
            print(f"  {'Est. throughput':25s} {result.estimated_tokens_per_second:8.1f} tok/s")
        if result.total_pcie_kb_per_token > 0:
            print(f"  {'PCIe per token':25s} {result.total_pcie_kb_per_token:8.1f} KB")
        if result.hot_experts_on_gpu > 0:
            print(f"  {'Hot experts on GPU':25s} {result.hot_experts_on_gpu:8d}")
        if result.warm_experts_on_cpu > 0:
            print(f"  {'Warm experts on CPU':25s} {result.warm_experts_on_cpu:8d}")
        print()

    if verbose:
        # Layer chart
        gpu_set = set(result.gpu_layers)
        cpu_set = set(result.cpu_layers)

        print(f"  LAYER CHART:")
        print(f"  {'─' * 95}")
        print(f"  {'Lyr':>4s} {'Attn':>7s} {'Expert':>7s} {'Router':>7s} {'Total':>7s} {'Side':>4s}")
        print(f"  {'─' * 95}")

        for i in sorted(model.layers.keys()):
            l = model.layers[i]
            attn = l.attn / 1e6
            expert = l.expert_total / 1e6
            router = (l.router_gate + l.router_probs) / 1e6
            total = l.total / 1e6

            if l.is_dense:
                side = "GPU"
            elif i in gpu_set:
                side = "GPU"
            elif i in cpu_set:
                side = "CPU"
            else:
                side = "—"

            print(f"  L{i:02d}  {attn:6.1f} {expert:6.1f} {router:6.1f} {total:6.1f}MB  {side}")

        print(f"  {'─' * 95}")
        print()

    # Command
    if result.llama_command:
        print(f"  Ready-to-run command:")
        print(f"  {result.llama_command}")
        print()


def print_comparison_table(results: list, gpu_key: str, model: ModelInfo):
    """Print comparison table across strategies (dsh-hub style)."""
    gpu = GPUS[gpu_key]

    print()
    print("=" * 120)
    print(f"  STRATEGY COMPARISON — {model.name} on {gpu['name']}")
    print("=" * 120)
    print()
    fmt = "{:<25s} {:>8s} {:>7s} {:>7s} {:>8s} {:>8s} {:>7s} {:>5s}"
    print(fmt.format("Strategy", "VRAM", "GPU_L", "CPU_L", "PCIe MB", "Lat(ms)", "Score", "Fits"))
    print("─" * 120)

    for r in sorted(results, key=lambda x: x.score, reverse=True):
        fits = "✅" if r.fits_vram else "❌"
        print(fmt.format(
            r.name[:25],
            f"{r.gpu_vram_used_mb:.0f}",
            f"{len(r.gpu_layers)}",
            f"{len(r.cpu_layers)}",
            f"{r.pcie_bytes_per_token/1e6:.1f}",
            f"{r.total_inference_overhead_ms:.2f}",
            f"{r.score:.0f}",
            fits,
        ))

    print()
    best = max(results, key=lambda x: x.score)
    print(f"  🏆 RECOMMENDED: {best.name} (score: {best.score:.0f}/100) {best.rating}")
    if best.cpu_layers:
        print(f"     CPU layers: L{best.cpu_layers[0]:02d}-L{best.cpu_layers[-1]:02d}")
        print(f"     PCIe overhead: {best.total_inference_overhead_ms:.2f} ms/token")
    else:
        print(f"     Full GPU — no PCIe overhead")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# CLI — merged flags from both versions
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="MoE PCIe Traffic Optimizer — model, estimate, and score PCIe traffic patterns",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s model.gguf --gpu 3070 --context 16384
  %(prog)s --demo --gpu 3070 --context 65536 --strategy hybrid
  %(prog)s --demo --gpu 3070 --compare-all --mermaid
  %(prog)s --demo --gpu 3070 --compare-all --json
  %(prog)s --demo --gpu 3090 --ot "blk.15-46.ffn_*_exps=CPU"
  %(prog)s model.gguf --gpu 3070 --profile heatmap.json --n-experts 64 --n-active 8
  %(prog)s model.gguf --gpu 3070 --diagram
  %(prog)s --demo --gpu 3070 --compare-all --ascii
        """)
    parser.add_argument("model", nargs="?", help="Path to GGUF file")
    parser.add_argument("--gpu", default="3070", choices=list(GPUS.keys()), help="Target GPU")
    parser.add_argument("--context", "-c", type=int, default=16384, help="Context size (tokens)")
    parser.add_argument("--cache", choices=["f16", "q8_0", "q4_0"], default="q8_0", help="KV cache type")
    parser.add_argument("--strategy", choices=["full_gpu", "n_cpu_moe", "ot_pattern", "hybrid",
                                             "naive", "smart", "pipelined", "all"],
                        default="all", help="Strategy to evaluate")
    parser.add_argument("--n-cpu-layers", type=int, help="Number of MoE layers to offload (for n_cpu_moe)")
    parser.add_argument("--ot", help="-ot pattern string")
    parser.add_argument("--compare-all", action="store_true", help="Compare all strategies")
    parser.add_argument("--compare", action="store_true", help="Compare strategies (alias for --compare-all)")
    parser.add_argument("--mermaid", action="store_true", help="Output Mermaid diagrams")
    parser.add_argument("--ascii", action="store_true", help="Output ASCII art diagrams")
    parser.add_argument("--diagram", action="store_true", help="Show data flow diagram")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--demo", action="store_true", help="Use synthetic demo data")
    parser.add_argument("--list-gpus", action="store_true", help="List available GPUs with PCIe info")
    parser.add_argument("--profile", help="Activation profile JSON file (for cache hit modeling)")
    parser.add_argument("--n-experts", type=int, help="Number of experts (auto-detected if omitted)")
    parser.add_argument("--n-active", type=int, help="Active experts per token (auto-detected if omitted)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()

    if args.list_gpus:
        print()
        print(f"  {'Key':12s} {'Name':25s} {'VRAM':>6s} {'Arch':10s} {'PCIe':>8s} {'BW GB/s':>8s}")
        print("  " + "─" * 75)
        for key, gpu in sorted(GPUS.items()):
            bw = get_pcie_bandwidth(key)
            print(f"  {key:12s} {gpu['name']:25s} {gpu['vram_mb']//1024:4d}GB  {gpu['arch']:10s} {gpu['pcie_gen']} x{gpu['pcie_lanes']:<3d} {bw:7.1f}")
        print()
        return

    if args.diagram:
        print_data_flow_diagram()
        return

    # Load model
    if args.demo:
        model = generate_demo_model()
    elif args.model:
        if not os.path.exists(args.model):
            print(f"  Error: file not found: {args.model}", file=sys.stderr)
            sys.exit(1)
        model = analyze_model(args.model)
    else:
        print("  Error: provide a GGUF file path or use --demo", file=sys.stderr)
        sys.exit(1)

    # Override expert counts if specified (Triton feature)
    if args.n_experts is not None:
        model.n_experts = args.n_experts
    if args.n_active is not None:
        model.n_active = args.n_active

    # Parse activation profile (Triton feature)
    activation_profile = None
    if args.profile:
        if not os.path.exists(args.profile):
            print(f"Error: Profile file not found: {args.profile}", file=sys.stderr)
            sys.exit(1)
        with open(args.profile) as f:
            activation_profile = json.load(f)

    if not args.quiet:
        print()
        print(f"  MODEL: {model.name}")
        print(f"  Layers: {model.total_layers} ({len(model.moe_layers)} MoE, {len(model.dense_layers)} dense)")
        print(f"  Total: {model.total_mb/1024:.2f} GB | Experts: {model.expert_total_mb:.1f} MB | Router: {model.router_total_mb:.1f} MB")
        if model.n_experts:
            print(f"  Experts: {model.n_experts} total, {model.n_active} active per token")

    # Evaluate strategies
    results = []

    if args.compare_all or args.compare or args.strategy == "all":
        # Full GPU baseline
        r = evaluate_full_gpu(model, args.gpu, args.context, args.cache)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

        # Auto-calculate optimal n-cpu-moe
        gpu = GPUS[args.gpu]
        kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
        kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(args.cache, 1.0)
        kv_mb = (args.context * kv_per_token * model.total_layers) / 1e6 + KV_CACHE_OVERHEAD_MB
        base_mb = model.attn_total_mb + model.router_total_mb + \
                  sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6
        vram_for_experts = gpu["vram_mb"] - kv_mb - base_mb - 200

        if model.moe_layers:
            avg_expert = model.expert_total_mb / len(model.moe_layers)
            n_gpu_auto = int(vram_for_experts / avg_expert) if avg_expert > 0 else 0
            n_gpu_auto = max(0, min(n_gpu_auto, len(model.moe_layers)))
            n_cpu_auto = len(model.moe_layers) - n_gpu_auto

            if n_cpu_auto > 0:
                r = evaluate_n_cpu_moe(model, args.gpu, args.context, args.cache, n_cpu_auto)
                r = score_strategy(r, model, args.gpu)
                results.append(r)

                r = evaluate_hybrid(model, args.gpu, args.context, args.cache)
                r = score_strategy(r, model, args.gpu)
                results.append(r)

        # Triton-style strategies (if activation_profile provided or always include)
        for strat_name in ("naive", "smart", "pipelined"):
            if strat_name == "naive":
                r = evaluate_naive(model, args.gpu, args.context, args.cache, activation_profile)
            elif strat_name == "smart":
                r = evaluate_smart(model, args.gpu, args.context, args.cache, activation_profile)
            else:
                r = evaluate_pipelined(model, args.gpu, args.context, args.cache, activation_profile)
            r = score_strategy(r, model, args.gpu)
            results.append(r)

    elif args.strategy == "n_cpu_moe":
        n_cpu = args.n_cpu_layers or (len(model.moe_layers) // 2)
        r = evaluate_n_cpu_moe(model, args.gpu, args.context, args.cache, n_cpu)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    elif args.strategy == "ot_pattern":
        if not args.ot:
            print("  Error: --ot pattern required for ot_pattern strategy", file=sys.stderr)
            sys.exit(1)
        r = evaluate_ot_pattern(model, args.gpu, args.context, args.cache, args.ot)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    elif args.strategy == "hybrid":
        r = evaluate_hybrid(model, args.gpu, args.context, args.cache)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    elif args.strategy == "full_gpu":
        r = evaluate_full_gpu(model, args.gpu, args.context, args.cache)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    elif args.strategy == "naive":
        r = evaluate_naive(model, args.gpu, args.context, args.cache, activation_profile)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    elif args.strategy == "smart":
        r = evaluate_smart(model, args.gpu, args.context, args.cache, activation_profile)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    elif args.strategy == "pipelined":
        r = evaluate_pipelined(model, args.gpu, args.context, args.cache, activation_profile)
        r = score_strategy(r, model, args.gpu)
        results.append(r)

    # JSON output (dsh-hub style, enriched with Triton fields)
    if args.json:
        output = {
            "model": {
                "name": model.name,
                "path": model.path,
                "total_mb": model.total_mb,
                "layers": model.total_layers,
                "moe_layers": len(model.moe_layers),
                "expert_total_mb": model.expert_total_mb,
                "router_total_mb": model.router_total_mb,
                "n_experts": model.n_experts,
                "n_active": model.n_active,
            },
            "gpu": args.gpu,
            "gpu_info": GPUS[args.gpu],
            "context": args.context,
            "cache": args.cache,
            "pcie_bandwidth_gb_s": get_pcie_bandwidth(args.gpu),
            "strategies": [],
        }
        for r in results:
            strat = _strategy_to_dict(r)
            output["strategies"].append(strat)

        best = max(results, key=lambda x: x.score)
        output["recommended"] = best.name
        output["recommended_score"] = best.score

        print(json.dumps(output, indent=2))
        return

    # Display results
    if args.compare_all or args.compare or len(results) > 1:
        print_comparison_table(results, args.gpu, model)

    for r in results:
        if not args.quiet:
            print_strategy_report(r, model, args.gpu,
                                  verbose=not (args.compare_all and len(results) > 1))

        if args.mermaid:
            print("  MERMAID — PCIe Data Flow:")
            print(generate_mermaid_pcie_flow(r, model, args.gpu))
            print()

        if args.ascii:
            print(generate_ascii_pcie_diagram(r, model, args.gpu))
            print(generate_ascii_layer_map(r, model))

    # Comparison Mermaid
    if args.mermaid and len(results) > 1:
        print("  MERMAID — Strategy Comparison:")
        print(generate_mermaid_strategy_comparison(results))
        print()
        print("  MERMAID — VRAM Usage:")
        gpu = GPUS[args.gpu]
        print(generate_mermaid_vram_chart(results, gpu["vram_mb"]))
        print()


if __name__ == "__main__":
    main()
