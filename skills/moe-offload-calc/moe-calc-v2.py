#!/usr/bin/env python3
"""
MoE Offload Calculator — CLI tool for visualizing CPU/GPU layer splits.

Reads GGUF files, extracts tensor structure, calculates optimal offloading
strategies for any MoE model on any GPU configuration.

Usage:
    python3 moe-calculator.py <model.gguf> --gpu 3070 --context 16384
    python3 moe-calculator.py <model.gguf> --gpu 3090 --context 128000 --cache q4_0
    python3 moe-calculator.py --list-gpus
    python3 moe-calculator.py --list-models /data/models/

Examples:
    # GLM-4.7 on 3070 at 16K
    python3 moe-calculator.py /data/models/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S.gguf --gpu 3070 --context 16384

    # Qwen3.6-35B on 3090 at 128K
    python3 moe-calculator.py /data/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf --gpu 3090 --context 131072

    # Compare all GPUs for a model
    python3 moe-calculator.py /data/models/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S.gguf --compare

    # Scan directory for MoE models
    python3 moe-calculator.py --scan /data/models/
"""

import argparse
import json
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# GPU DATABASE
# ══════════════════════════════════════════════════════════════════════════════

GPUS = {
    "3060-12":  {"name": "RTX 3060 12GB",  "vram_mb": 12288, "arch": "ampere", "pcie": "4.0 x16"},
    "3060-ti":  {"name": "RTX 3060 Ti 8GB", "vram_mb": 8192,  "arch": "ampere", "pcie": "4.0 x16"},
    "3070":     {"name": "RTX 3070 8GB",    "vram_mb": 8192,  "arch": "ampere", "pcie": "4.0 x16"},
    "3070-ti":  {"name": "RTX 3070 Ti 8GB", "vram_mb": 8192,  "arch": "ampere", "pcie": "4.0 x16"},
    "3080":     {"name": "RTX 3080 10GB",   "vram_mb": 10240, "arch": "ampere", "pcie": "4.0 x16"},
    "3080-ti":  {"name": "RTX 3080 Ti 12GB","vram_mb": 12288, "arch": "ampere", "pcie": "4.0 x16"},
    "3090":     {"name": "RTX 3090 24GB",   "vram_mb": 24576, "arch": "ampere", "pcie": "4.0 x16"},
    "3090-ti":  {"name": "RTX 3090 Ti 24GB","vram_mb": 24576, "arch": "ampere", "pcie": "4.0 x16"},
    "4060":     {"name": "RTX 4060 8GB",    "vram_mb": 8192,  "arch": "ada",    "pcie": "4.0 x8"},
    "4060-ti":  {"name": "RTX 4060 Ti 8GB", "vram_mb": 8192,  "arch": "ada",    "pcie": "4.0 x8"},
    "4070":     {"name": "RTX 4070 12GB",   "vram_mb": 12288, "arch": "ada",    "pcie": "4.0 x16"},
    "4070-ti":  {"name": "RTX 4070 Ti 12GB","vram_mb": 12288, "arch": "ada",    "pcie": "4.0 x16"},
    "4070-ti-s": {"name": "RTX 4070 Ti Super 16GB", "vram_mb": 16384, "arch": "ada", "pcie": "4.0 x16"},
    "4080":     {"name": "RTX 4080 16GB",   "vram_mb": 16384, "arch": "ada",    "pcie": "4.0 x16"},
    "4080-s":   {"name": "RTX 4080 Super 16GB", "vram_mb": 16384, "arch": "ada", "pcie": "4.0 x16"},
    "4090":     {"name": "RTX 4090 24GB",   "vram_mb": 24576, "arch": "ada",    "pcie": "4.0 x16"},
    "5070":     {"name": "RTX 5070 12GB",   "vram_mb": 12288, "arch": "blackwell", "pcie": "5.0 x16"},
    "5070-ti":  {"name": "RTX 5070 Ti 16GB","vram_mb": 16384, "arch": "blackwell", "pcie": "5.0 x16"},
    "5080":     {"name": "RTX 5080 16GB",   "vram_mb": 16384, "arch": "blackwell", "pcie": "5.0 x16"},
    "5090":     {"name": "RTX 5090 32GB",   "vram_mb": 32768, "arch": "blackwell", "pcie": "5.0 x16"},
    "a100-40":  {"name": "A100 40GB",       "vram_mb": 40960, "arch": "ampere", "pcie": "4.0 x16"},
    "a100-80":  {"name": "A100 80GB",       "vram_mb": 81920, "arch": "ampere", "pcie": "4.0 x16"},
    "h100":     {"name": "H100 80GB",       "vram_mb": 81920, "arch": "hopper", "pcie": "5.0 x16"},
}

# ══════════════════════════════════════════════════════════════════════════════
# GGUF READER (minimal — reads tensor info without full gguf dependency)
# ══════════════════════════════════════════════════════════════════════════════

def read_gguf_tensors(path: str, use_gguf_lib: bool = False) -> list[tuple[str, int]]:
    """Read tensor names and sizes from a GGUF file. Returns [(name, nbytes), ...].
    Uses fast header-only parsing — does NOT read model weights."""
    if use_gguf_lib:
        try:
            import gguf
            reader = gguf.GGUFReader(path)
            return [(t.name, t.data.nbytes) for t in reader.tensors]
        except ImportError:
            pass

    # Fast: manual GGUF header parsing (reads only metadata, not weights)
    tensors = []
    with open(path, "rb") as f:
        # GGUF magic
        magic = f.read(4)
        if magic != b"GGUF":
            raise ValueError(f"Not a GGUF file: {path}")

        version = struct.unpack("<I", f.read(4))[0]
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]

        # Skip KV metadata — use seek for large strings to avoid MemoryError
        for _ in range(min(n_kv, 1000)):  # safety limit
            # key (string)
            key_len = struct.unpack("<Q", f.read(8))[0]
            if key_len > 1_000_000:  # 1MB safety — skip remaining KV
                break
            f.seek(key_len, 1)
            # value type + data
            val_type = struct.unpack("<I", f.read(4))[0]
            _skip_gguf_value(f, val_type)

        # Read tensor info
        for _ in range(n_tensors):
            # name
            name_len = struct.unpack("<Q", f.read(8))[0]
            name = f.read(name_len).decode("utf-8", errors="replace")
            # n_dims
            n_dims = struct.unpack("<Q", f.read(8))[0]
            # dims
            dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(n_dims)]
            # type
            dtype = struct.unpack("<I", f.read(4))[0]
            # offset
            offset = struct.unpack("<Q", f.read(8))[0]

            # Calculate byte size
            dtype_sizes = {0: 4, 1: 2, 2: 10, 3: 2, 5: 1, 6: 2, 7: 4, 8: 8, 9: 1, 10: 2}
            elem_size = dtype_sizes.get(dtype, 4)
            n_elements = 1
            for d in dims:
                n_elements *= d

            tensors.append((name, n_elements * elem_size))

        return tensors


def _skip_gguf_value(f, val_type):
    """Skip a GGUF metadata value without loading it into memory.
    GGUFValueType: 0=UINT8, 1=INT8, 2=UINT16, 3=INT16, 4=UINT32, 5=INT32,
    6=FLOAT32, 7=BOOL, 8=STRING, 9=ARRAY
    """
    if val_type == 8:  # STRING
        str_len = struct.unpack("<Q", f.read(8))[0]
        f.seek(str_len, 1)
    elif val_type == 9:  # ARRAY
        arr_type = struct.unpack("<I", f.read(4))[0]
        arr_len = struct.unpack("<Q", f.read(8))[0]
        for _ in range(arr_len):
            _skip_gguf_value(f, arr_type)
    else:  # numeric: 0-7
        type_sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1}
        f.read(type_sizes.get(val_type, 4))


# ══════════════════════════════════════════════════════════════════════════════
# MODEL ANALYZER
# ══════════════════════════════════════════════════════════════════════════════

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
    ffn_single: int = 0  # dense FFN (non-MoE layers)
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


@dataclass
class ModelInfo:
    """Complete model tensor analysis."""
    path: str
    name: str
    total_size_bytes: int
    layers: dict[int, LayerInfo]
    total_layers: int
    moe_layers: list[int]
    dense_layers: list[int]
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


def analyze_model(path: str, use_gguf_lib: bool = False) -> ModelInfo:
    """Analyze a GGUF file and extract MoE structure."""
    tensors = read_gguf_tensors(path, use_gguf_lib=False)  # manual parser is faster
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

    # Identify MoE vs dense layers
    for layer, info in sorted(layers.items()):
        if info.expert_total > 0:
            moe_layers.append(layer)
            info.is_dense = False
        else:
            dense_layers.append(layer)
            info.is_dense = True

    # Try to detect number of experts from tensor sizes
    if moe_layers:
        # Expert tensors have shape [n_experts, hidden_dim, intermediate_dim]
        # The first MoE layer's expert tensor size / known patterns can hint at count
        sample = layers[moe_layers[0]]
        if sample.expert_exps > 0:
            # rough heuristic: expert_exps / (hidden * intermediate) ≈ n_experts
            # We'll try common values
            for n in [4, 8, 16, 32, 64, 128, 256, 512]:
                # Check if the expert tensor size is divisible by n
                if sample.expert_exps % n == 0 and sample.expert_exps // n > 0:
                    pass  # too many candidates, skip heuristic

    total_size = sum(size for _, size in tensors)

    return ModelInfo(
        path=path,
        name=name,
        total_size_bytes=total_size,
        layers=layers,
        total_layers=len(layers),
        moe_layers=moe_layers,
        dense_layers=dense_layers,
    )


# ══════════════════════════════════════════════════════════════════════════════
# OFFLOAD CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OffloadResult:
    """Result of offload calculation."""
    gpu_name: str
    gpu_vram_mb: int
    context: int
    cache_type: str
    n_gpu_moe_layers: int
    n_cpu_moe_layers: int
    gpu_total_mb: float
    gpu_attn_mb: float
    gpu_expert_mb: float
    gpu_router_mb: float
    gpu_other_mb: float
    gpu_kv_mb: float
    gpu_headroom_mb: float
    cpu_expert_mb: float
    ram_needed_mb: float
    fits: bool
    model: ModelInfo


def calculate_offload(
    model: ModelInfo,
    gpu_key: str,
    context: int = 16384,
    cache_type: str = "q8_0",
    n_cpu_layers: Optional[int] = None,
) -> OffloadResult:
    """Calculate optimal GPU/CPU layer split."""
    gpu = GPUS[gpu_key]
    vram = gpu["vram_mb"]

    # KV cache calculation
    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    # Estimate KV bytes per token per layer
    # For deepseek2 (MLA): ~1280 * 2 bytes
    # For qwen/moe: ~hidden_size * 2 bytes
    # Use a heuristic based on attention tensor size
    sample_attn = 0
    for l in model.layers.values():
        if l.attn > 0:
            sample_attn = l.attn
            break
    # Rough: attn tensor / (n_heads * head_dim * 2) ≈ hidden_size
    # We'll use a conservative estimate
    kv_per_token_per_layer = 2048 * 2  # 4KB per token per layer (conservative)

    kv_total = (context * kv_per_token_per_layer * model.total_layers) / 1e6
    kv_total += 200  # overhead for CUDA context, model metadata

    # Base weights (always on GPU)
    base_on_gpu = model.attn_total_mb + model.router_total_mb + \
                  sum(l.ffn_norm + l.ffn_single + l.other for l in model.layers.values()) / 1e6

    # Expert weights (can be split)
    expert_per_layer = []
    for i in sorted(model.moe_layers):
        l = model.layers[i]
        expert_per_layer.append(l.expert_total / 1e6)

    if expert_per_layer:
        avg_expert_per_layer = sum(expert_per_layer) / len(expert_per_layer)
    else:
        avg_expert_per_layer = 0

    # Calculate how many layers fit
    vram_for_experts = vram - kv_total - base_on_gpu - 200  # 200MB safety margin

    if vram_for_experts <= 0:
        n_gpu_moe = 0
    elif n_cpu_layers is not None:
        n_gpu_moe = max(0, len(model.moe_layers) - n_cpu_layers)
    else:
        n_gpu_moe = int(vram_for_experts / avg_expert_per_layer) if avg_expert_per_layer > 0 else 0
        n_gpu_moe = max(0, min(n_gpu_moe, len(model.moe_layers)))

    n_cpu_moe = len(model.moe_layers) - n_gpu_moe

    # Calculate actual GPU usage
    gpu_expert = sum(expert_per_layer[:n_gpu_moe])
    cpu_expert = sum(expert_per_layer[n_gpu_moe:])

    gpu_total = base_on_gpu + gpu_expert + kv_total
    headroom = vram - gpu_total

    return OffloadResult(
        gpu_name=gpu["name"],
        gpu_vram_mb=vram,
        context=context,
        cache_type=cache_type,
        n_gpu_moe_layers=n_gpu_moe,
        n_cpu_moe_layers=n_cpu_moe,
        gpu_total_mb=gpu_total,
        gpu_attn_mb=model.attn_total_mb,
        gpu_expert_mb=gpu_expert,
        gpu_router_mb=model.router_total_mb,
        gpu_other_mb=base_on_gpu - model.attn_total_mb - model.router_total_mb,
        gpu_kv_mb=kv_total,
        gpu_headroom_mb=headroom,
        cpu_expert_mb=cpu_expert,
        ram_needed_mb=cpu_expert + 500,
        fits=headroom > 0,
        model=model,
    )


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

def print_model_info(model: ModelInfo):
    """Print model tensor analysis."""
    print()
    print(f"  MODEL: {model.name}")
    print(f"  Path: {model.path}")
    print(f"  Total size: {model.total_size_bytes/1e9:.2f} GB ({model.total_size_bytes:,} bytes)")
    print(f"  Layers: {model.total_layers} ({len(model.moe_layers)} MoE, {len(model.dense_layers)} dense)")
    print(f"  Attention: {model.attn_total_mb:.1f} MB")
    print(f"  Expert FFN: {model.expert_total_mb:.1f} MB")
    print(f"  Router: {model.router_total_mb:.1f} MB")
    if model.moe_layers:
        avg_expert = model.expert_total_mb / len(model.moe_layers)
        print(f"  Avg expert/layer: {avg_expert:.1f} MB")


def print_breakdown(result: OffloadResult, verbose: bool = True):
    """Print full VRAM breakdown and layer chart."""
    model = result.model
    vram = result.gpu_vram_mb

    print()
    print("=" * 100)
    print(f"  MoE OFFLOAD — {result.gpu_name} ({vram} MB)")
    print(f"  Model: {model.name} | Context: {result.context:,} | Cache: {result.cache_type}")
    print("=" * 100)
    print()

    # Summary
    total_moe = len(model.moe_layers)
    gpu_moe = result.n_gpu_moe_layers
    cpu_moe = result.n_cpu_moe_layers
    print(f"  Layers: {model.total_layers} total ({len(model.dense_layers)} dense + {total_moe} MoE)")
    print(f"  GPU MoE layers: {gpu_moe}/{total_moe} | CPU MoE layers: {cpu_moe}/{total_moe}")
    if model.moe_layers:
        first_gpu = model.moe_layers[0] if gpu_moe > 0 else -1
        last_gpu = model.moe_layers[gpu_moe - 1] if gpu_moe > 0 else -1
        first_cpu = model.moe_layers[gpu_moe] if cpu_moe > 0 else -1
        last_cpu = model.moe_layers[-1] if cpu_moe > 0 else -1
        print(f"  GPU expert range: L{first_gpu:02d}-L{last_gpu:02d}")
        print(f"  CPU expert range: L{first_cpu:02d}-L{last_cpu:02d}")
    print()

    # VRAM breakdown
    print(f"  VRAM BREAKDOWN ({vram} MB total):")
    print(f"  {'─' * 65}")
    bar_w = 40

    items = [
        ("Attention", result.gpu_attn_mb),
        ("Router/Gating", result.gpu_router_mb),
        ("Dense FFN/Other", result.gpu_other_mb),
        ("Expert Weights", result.gpu_expert_mb),
        ("KV Cache", result.gpu_kv_mb),
    ]

    for name, mb in items:
        pct = mb / vram * 100 if vram > 0 else 0
        bar_len = max(0, int(pct / 100 * bar_w))
        bar = "█" * bar_len + "░" * (bar_w - bar_len)
        marker = " ⚠️" if pct > 80 else ""
        print(f"  {name:18s} {mb:8.1f} MB ({pct:5.1f}%) {bar}{marker}")

    print(f"  {'─' * 65}")
    print(f"  {'TOTAL':18s} {result.gpu_total_mb:8.1f} MB ({result.gpu_total_mb/vram*100:.1f}%)")
    headroom = result.gpu_headroom_mb
    status = "✅ FITS" if result.fits else "❌ OOM"
    print(f"  {'HEADROOM':18s} {headroom:8.1f} MB ({headroom/vram*100:.1f}%) {status}")
    print(f"  {'CPU EXPERTS':18s} {result.cpu_expert_mb:8.1f} MB ({result.cpu_expert_mb/1024:.1f} GB)")
    print(f"  {'RAM NEEDED':18s} {result.ram_needed_mb:8.1f} MB ({result.ram_needed_mb/1024:.1f} GB)")
    print()

    if not verbose:
        return

    # Layer-by-layer chart
    gpu_layer_set = set(model.moe_layers[:gpu_moe])
    cpu_layer_set = set(model.moe_layers[gpu_moe:])

    print(f"  LAYER PLACEMENT CHART:")
    print(f"  {'─' * 95}")
    print(f"  {'Lyr':>4s} {'Attn':>7s} {'Expert':>7s} {'Router':>7s} {'Total':>7s} {'Side':>4s}  {'Action'}")
    print(f"  {'─' * 95}")

    for i in sorted(model.layers.keys()):
        l = model.layers[i]
        attn = l.attn / 1e6
        expert = l.expert_total / 1e6
        router = (l.router_gate + l.router_probs) / 1e6
        total = l.total / 1e6

        if l.is_dense:
            side = "GPU"
            action = "attn + FFN (dense)"
            mem_bar = "▓" * min(int(total / 5), 30)
        elif i in gpu_layer_set:
            side = "GPU"
            action = f"attn + router + experts"
            mem_bar = "█" * min(int(expert / 5), 30)
        else:
            side = "CPU"
            action = "attn(GPU) + experts(RAM)"
            mem_bar = "░" * min(int(expert / 5), 30)

        print(f"  L{i:02d}  {attn:6.1f} {expert:6.1f} {router:6.1f} {total:6.1f}MB  {side}  {action}")

    print(f"  {'─' * 95}")
    print()

    # GPU vs CPU summary
    print(f"  GPU vs CPU SUMMARY:")
    print(f"  {'─' * 55}")
    print(f"  {'Component':30s} {'GPU':>12s} {'CPU':>12s}")
    print(f"  {'─' * 55}")

    cpu_attn = 0  # attention always on GPU
    cpu_router = sum(model.layers[i].router_gate + model.layers[i].router_probs
                     for i in cpu_layer_set) / 1e6
    gpu_router = sum(model.layers[i].router_gate + model.layers[i].router_probs
                     for i in gpu_layer_set) / 1e6

    print(f"  {'Attention':30s} {model.attn_total_mb:10.1f}MB {'(GPU)':>12s}")
    print(f"  {'Router/Gating':30s} {gpu_router:10.1f}MB {cpu_router:10.1f}MB")
    print(f"  {'Expert FFN weights':30s} {result.gpu_expert_mb:10.1f}MB {result.cpu_expert_mb:10.1f}MB")
    print(f"  {'KV Cache':30s} {result.gpu_kv_mb:10.1f}MB {'(GPU)':>12s}")
    print(f"  {'─' * 55}")
    print(f"  {'TOTAL':30s} {result.gpu_total_mb:10.1f}MB {result.cpu_expert_mb:10.1f}MB")
    print()


def print_commands(result: OffloadResult):
    """Generate llama.cpp/BeeLlama commands."""
    model = result.model
    n_cpu = result.n_cpu_moe_layers
    first_cpu = model.moe_layers[result.n_gpu_moe_layers] if n_cpu > 0 else -1
    last_cpu = model.moe_layers[-1] if n_cpu > 0 else -1

    print(f"  LLAMA.CPP COMMANDS:")
    print(f"  {'─' * 70}")
    print()

    if n_cpu > 0:
        # Option 1: --n-cpu-moe
        print(f"  Option 1: --n-cpu-moe (simple)")
        print(f"    --n-cpu-moe {n_cpu}")
        print(f"    Offloads layers L{first_cpu:02d}-L{last_cpu:02d} experts to CPU")
        print()

        # Option 2: -ot
        print(f"  Option 2: --override-tensor (granular)")
        ot_parts = []
        for pattern in ["ffn_*_exps", "ffn_*_shexp", "ffn_gate_inp", "exp_probs_b"]:
            if first_cpu == last_cpu:
                ot_parts.append(f"blk.{first_cpu}.{pattern}=CPU")
            else:
                ot_parts.append(f"blk.{first_cpu}-{last_cpu}.{pattern}=CPU")
        print(f"    -ot '{','.join(ot_parts)}'")
        print()

    # Full docker command
    print(f"  FULL DOCKER COMMAND:")
    print(f"    docker run -d --name {model.name[:20]}-offload \\")
    print(f"      --network host --ipc host --gpus device=<GPU_INDEX> \\")
    print(f"      -v /data/models/:/models-data/:ro \\")
    print(f"      beellama-kvarn:latest \\")
    print(f"      -m /models-data/{Path(model.path).name} \\")
    print(f"      --port <PORT> --host 0.0.0.0 \\")
    print(f"      -ngl 999 -c {result.context} -t 20 \\")
    print(f"      --flash-attn on \\")
    print(f"      --cache-type-k {result.cache_type} --cache-type-v {result.cache_type} \\")
    if n_cpu > 0:
        print(f"      --n-cpu-moe {n_cpu} \\")
    print(f"      --load-mode none \\")
    print(f"      -fit off")
    print()


def print_comparison(model: ModelInfo, context: int = 16384, cache: str = "q8_0"):
    """Print comparison table across GPUs."""
    print()
    print("=" * 110)
    print(f"  GPU COMPARISON — {model.name} | Context: {context:,} | Cache: {cache}")
    print("=" * 110)
    print()
    fmt = "{:<12s} {:>8s} {:>6s} {:>7s} {:>7s} {:>9s} {:>9s} {:>9s} {:>8s} {:>6s}"
    print(fmt.format("GPU", "VRAM", "Cache", "GPU_L", "CPU_L", "GPU_MB", "KV_MB", "CPU_MB", "Headroom", "Status"))
    print("─" * 110)

    for gpu_key in ["3060-12", "3070", "3080", "3090", "4060-ti", "4070", "4070-ti-s", "4080", "4090", "5070", "5070-ti", "5090", "a100-40", "h100"]:
        gpu = GPUS[gpu_key]
        for c in [cache]:
            r = calculate_offload(model, gpu_key, context, c)
            status = "✅" if r.fits else "❌"
            n_cpu = r.n_cpu_moe_layers
            cmd = f"n-cpu {n_cpu}" if n_cpu > 0 else "full GPU"
            print(fmt.format(
                gpu["name"][:12], f"{gpu['vram_mb']//1024}GB", c,
                f"{r.n_gpu_moe_layers}/{len(model.moe_layers)}",
                f"{n_cpu}/{len(model.moe_layers)}",
                f"{r.gpu_total_mb:.0f}", f"{r.gpu_kv_mb:.0f}",
                f"{r.cpu_expert_mb:.0f}", f"{r.gpu_headroom_mb:.0f}MB",
                status
            ))

    print()


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="MoE Offload Calculator — visualize CPU/GPU layer splits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s model.gguf --gpu 3070 --context 16384
  %(prog)s model.gguf --gpu 3090 --context 128000 --cache q4_0
  %(prog)s model.gguf --compare --context 65536
  %(prog)s --scan /data/models/
  %(prog)s --list-gpus
        """)
    parser.add_argument("model", nargs="?", help="Path to GGUF file")
    parser.add_argument("--gpu", default="3070", choices=list(GPUS.keys()), help="Target GPU (default: 3070)")
    parser.add_argument("--context", "-c", type=int, default=16384, help="Context size in tokens (default: 16384)")
    parser.add_argument("--cache", choices=["f16", "q8_0", "q4_0"], default="q8_0", help="KV cache type (default: q8_0)")
    parser.add_argument("--n-cpu-layers", type=int, help="Force N MoE layers to CPU")
    parser.add_argument("--compare", action="store_true", help="Compare across all GPUs")
    parser.add_argument("--compare-contexts", action="store_true", help="Compare across context sizes")
    parser.add_argument("--compact", action="store_true", help="Compact output (no layer chart)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--list-gpus", action="store_true", help="List available GPUs")
    parser.add_argument("--scan", help="Scan directory for MoE models")
    parser.add_argument("--save", help="Save result to file")
    args = parser.parse_args()

    if args.list_gpus:
        print("\n  Available GPUs:")
        print(f"  {'Key':12s} {'Name':25s} {'VRAM':>6s} {'Arch':10s}")
        print("  " + "─" * 60)
        for key, gpu in sorted(GPUS.items()):
            print(f"  {key:12s} {gpu['name']:25s} {gpu['vram_mb']//1024:4d}GB  {gpu['arch']:10s}")
        print()
        return

    if args.scan:
        print(f"\n  Scanning {args.scan} for MoE models...")
        models = []
        for p in sorted(Path(args.scan).glob("*.gguf")):
            try:
                info = analyze_model(str(p), use_gguf_lib=True)
                if info.moe_layers:
                    models.append(info)
                    expert_mb = info.expert_total_mb
                    print(f"  {p.name:50s} {info.total_mb/1024:.1f}GB  {len(info.moe_layers):2d} MoE layers  {expert_mb:7.1f}MB experts  ({info.arch})")
            except Exception as e:
                pass
        print(f"\n  Found {len(models)} MoE models")
        if models:
            print(f"\n  Run full analysis on a model:")
            print(f"    python3 moe-calculator.py <model.gguf> --gpu 3070 --context 16384")
        return

    if not args.model:
        parser.print_help()
        return

    if not os.path.exists(args.model):
        print(f"  Error: File not found: {args.model}")
        sys.exit(1)

    # Analyze model
    model = analyze_model(args.model)
    print_model_info(model)

    if not model.moe_layers:
        print("\n  This model has no MoE layers (not a Mixture of Experts model).")
        print("  All layers are dense — no CPU offload possible.")
        return

    if args.json:
        r = calculate_offload(model, args.gpu, args.context, args.cache, args.n_cpu_layers)
        output = {
            "model": {"name": model.name, "path": model.path, "total_mb": model.total_mb,
                      "layers": model.total_layers, "moe_layers": len(model.moe_layers)},
            "gpu": args.gpu,
            "context": args.context,
            "cache": args.cache,
            "n_gpu_moe": r.n_gpu_moe_layers,
            "n_cpu_moe": r.n_cpu_moe_layers,
            "gpu_total_mb": r.gpu_total_mb,
            "gpu_headroom_mb": r.gpu_headroom_mb,
            "cpu_expert_mb": r.cpu_expert_mb,
            "fits": r.fits,
        }
        print(json.dumps(output, indent=2))
        return

    if args.compare:
        print_comparison(model, args.context, args.cache)
        return

    if args.compare_contexts:
        print(f"\n  CONTEXT SIZE COMPARISON — {model.name} on {args.gpu}")
        print(f"  {'─' * 80}")
        fmt = "{:>8s} {:>6s} {:>7s} {:>7s} {:>9s} {:>9s} {:>8s} {:>6s}"
        print(fmt.format("Context", "Cache", "GPU_L", "CPU_L", "GPU_MB", "CPU_MB", "Headroom", "Status"))
        print(f"  {'─' * 80}")
        for ctx in [4096, 8192, 16384, 32768, 65536, 131072]:
            for cache in ["q8_0", "q4_0"]:
                r = calculate_offload(model, args.gpu, ctx, cache)
                status = "✅" if r.fits else "❌"
                print(fmt.format(
                    f"{ctx//1024}K", cache,
                    f"{r.n_gpu_moe_layers}/{len(model.moe_layers)}",
                    f"{r.n_cpu_moe_layers}/{len(model.moe_layers)}",
                    f"{r.gpu_total_mb:.0f}", f"{r.cpu_expert_mb:.0f}",
                    f"{r.gpu_headroom_mb:.0f}MB", status
                ))
        print()
        return

    # Default: full breakdown
    r = calculate_offload(model, args.gpu, args.context, args.cache, args.n_cpu_layers)
    print_breakdown(r, verbose=not args.compact)
    print_commands(r)

    if args.save:
        with open(args.save, "w") as f:
            f.write(f"MoE Offload: {model.name} on {args.gpu}\n")
            f.write(f"Context: {args.context:,} | Cache: {args.cache}\n")
            f.write(f"GPU layers: {r.n_gpu_moe_layers} | CPU layers: {r.n_cpu_moe_layers}\n")
            f.write(f"Fits: {r.fits}\n")
        print(f"  Saved to {args.save}")


if __name__ == "__main__":
    main()
