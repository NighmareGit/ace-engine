#!/usr/bin/env python3
"""
MoE CPU Offload Calculator for GLM-4.7-Flash-REAP-23B-A3B on RTX 3070 (8GB)

Calculates optimal layer split between GPU and CPU given VRAM budget and context size.
Generates --n-cpu-moe and -ot patterns for llama.cpp/BeeLlama.
"""

# ══════════════════════════════════════════════════════════════════════════════
# MODEL DATA — GLM-4.7-Flash-REAP-23B-A3B Q4_K_S
# ══════════════════════════════════════════════════════════════════════════════

# Per-layer tensor sizes (bytes) from GGUF inspection
# Layer 0 is dense (no experts), layers 1-46 are MoE
LAYERS = {
    # L0: dense (gate_up is a single FFN, no experts)
    0:  {"attn": 15135744, "ffn_gate": 11796480, "ffn_norm": 8192, "expert_exps": 0, "expert_shexp": 0, "router": 0},
    # L1-L4: 64 experts
    1:  {"attn": 15135744, "ffn_gate": 0, "ffn_norm": 8192, "expert_exps": 273678336, "expert_shexp": 6488064, "router": 393408},
    2:  {"attn": 15135744, "ffn_gate": 0, "ffn_norm": 8192, "expert_exps": 273678336, "expert_shexp": 6488064, "router": 393408},
    3:  {"attn": 15135744, "ffn_gate": 0, "ffn_norm": 8192, "expert_exps": 273678336, "expert_shexp": 6488064, "router": 393408},
    4:  {"attn": 15135744, "ffn_gate": 0, "ffn_norm": 8192, "expert_exps": 273678336, "expert_shexp": 6488064, "router": 393408},
}
# L5-L46: same structure, slightly different expert sizes
for i in range(5, 47):
    LAYERS[i] = {"attn": 15135744, "ffn_gate": 0, "ffn_norm": 8192,
                 "expert_exps": 254803968, "expert_shexp": 6905856, "router": 393408}

TOTAL_LAYERS = 47  # 0-46
DENSE_LAYER = 0    # layer 0 is dense (no MoE)
MOE_START = 1      # MoE layers start at 1
EXPERTS_TOTAL = 64 # GLM-4.7 has 64 experts
EXPERTS_ACTIVE = 8 # top-8 routing

# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE CONSTRAINTS
# ══════════════════════════════════════════════════════════════════════════════

GPU_VRAM_MB = {
    "3070": 8192,
    "3090": 24576,
}

# KV cache bytes per token per layer (depends on model architecture)
# GLM-4.7 uses deepseek2 MLA: kv_lora_rank=512, qk_nope_head_dim=128
# KV cache per token per layer ≈ (kv_lora_rank + qk_nope_head_dim) * 2 bytes (fp16) per layer
# With q8_0 cache: multiply by 1.0 (same size as fp16 for q8_0)
# With q4_0 cache: multiply by 0.5
KV_BYTES_PER_TOKEN_PER_LAYER = 1280 * 2  # 2560 bytes per token per layer (fp16/q8_0)
KV_CACHE_OVERHEAD_MB = 500  # model metadata, CUDA context, etc.

# ══════════════════════════════════════════════════════════════════════════════
# CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def calculate_vram_budget(gpu="3070", context=65536, cache_type="q8_0", n_gpu_layers=None):
    """Calculate VRAM budget breakdown for a given GPU, context, and layer split."""
    vram_total = GPU_VRAM_MB[gpu]  # MB

    # KV cache per token per layer
    kv_mult = {"f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
    kv_per_token = KV_BYTES_PER_TOKEN_PER_LAYER * kv_mult.get(cache_type, 1.0)

    # KV cache total
    kv_cache_mb = (context * kv_per_token * TOTAL_LAYERS) / 1e6
    kv_cache_mb += KV_CACHE_OVERHEAD_MB

    # Model weights breakdown
    attn_total = sum(LAYERS[i]["attn"] for i in range(TOTAL_LAYERS)) / 1e6
    expert_total = sum(LAYERS[i]["expert_exps"] + LAYERS[i]["expert_shexp"]
                       for i in range(MOE_START, TOTAL_LAYERS)) / 1e6
    router_total = sum(LAYERS[i]["router"] for i in range(MOE_START, TOTAL_LAYERS)) / 1e6
    dense_ffn = LAYERS[DENSE_LAYER]["ffn_gate"] / 1e6 + LAYERS[DENSE_LAYER]["ffn_norm"] / 1e6
    other_total = sum(LAYERS[i]["ffn_norm"] for i in range(MOE_START, TOTAL_LAYERS)) / 1e6
    model_total = attn_total + expert_total + router_total + dense_ffn + other_total

    # Determine layers on GPU vs CPU
    if n_gpu_layers is not None:
        # Explicit layer count
        n_gpu_moe = min(n_gpu_layers, TOTAL_LAYERS - 1)
        n_cpu_moe = TOTAL_LAYERS - 1 - n_gpu_moe
    else:
        # Auto-calculate: find max layers that fit
        # Start with: attn + router + norm + dense_ffn always on GPU
        base_on_gpu = attn_total + router_total + other_total + dense_ffn
        vram_for_experts = vram_total - kv_cache_mb - base_on_gpu

        if vram_for_experts <= 0:
            # Can't even fit attention + KV, need to reduce context or use cpu-moe
            n_gpu_moe = 0
            n_cpu_moe = TOTAL_LAYERS - 1
        else:
            # Each MoE layer's expert weights
            avg_expert_per_layer = expert_total / (TOTAL_LAYERS - 1)
            n_gpu_moe = int(vram_for_experts / avg_expert_per_layer)
            n_gpu_moe = max(0, min(n_gpu_moe, TOTAL_LAYERS - 1))
            n_cpu_moe = TOTAL_LAYERS - 1 - n_gpu_moe

    # Calculate actual VRAM usage
    gpu_expert_mb = 0
    cpu_expert_mb = 0
    for i in range(MOE_START, TOTAL_LAYERS):
        expert_size = (LAYERS[i]["expert_exps"] + LAYERS[i]["expert_shexp"]) / 1e6
        if i < MOE_START + n_gpu_moe:
            gpu_expert_mb += expert_size
        else:
            cpu_expert_mb += expert_size

    gpu_attn = attn_total
    gpu_router = router_total
    gpu_dense = dense_ffn
    gpu_norm = other_total
    gpu_expert = gpu_expert_mb
    gpu_total = gpu_attn + gpu_router + gpu_dense + gpu_norm + gpu_expert + kv_cache_mb

    cpu_total = cpu_expert_mb

    return {
        "gpu": gpu,
        "context": context,
        "cache_type": cache_type,
        "n_gpu_moe_layers": n_gpu_moe,
        "n_cpu_moe_layers": n_cpu_moe,
        "gpu_total_mb": gpu_total,
        "gpu_attn_mb": gpu_attn,
        "gpu_router_mb": gpu_router,
        "gpu_dense_mb": gpu_dense,
        "gpu_norm_mb": gpu_norm,
        "gpu_expert_mb": gpu_expert,
        "gpu_kv_mb": kv_cache_mb,
        "gpu_headroom_mb": vram_total - gpu_total,
        "cpu_expert_mb": cpu_total,
        "ram_needed_mb": cpu_total + 500,  # 500MB for OS overhead
    }


def generate_ot_patterns(n_gpu_moe):
    """Generate -ot patterns for GPU and CPU tensor placement."""
    gpu_ranges = []
    cpu_ranges = []

    # Layer 0 is always dense (no MoE), keep on GPU
    for i in range(MOE_START, TOTAL_LAYERS):
        if i < MOE_START + n_gpu_moe:
            gpu_ranges.append(i)
        else:
            cpu_ranges.append(i)

    # Generate -ot patterns
    # GPU tensors (expert weights stay on GPU)
    gpu_ot = []
    cpu_ot = []

    if gpu_ranges:
        # For GPU layers, no override needed (default is GPU)
        # But we need to explicitly keep them on GPU if we have CPU layers
        pass  # GPU is default, no -ot needed for GPU layers

    if cpu_ranges:
        # For CPU layers, override expert tensors to CPU
        # Group contiguous ranges
        ranges = []
        start = cpu_ranges[0]
        end = cpu_ranges[0]
        for r in cpu_ranges[1:]:
            if r == end + 1:
                end = r
            else:
                ranges.append((start, end))
                start = r
                end = r
        ranges.append((start, end))

        for s, e in ranges:
            if s == e:
                cpu_ot.append(f"blk.{s}.ffn_*_exps=CPU")
                cpu_ot.append(f"blk.{s}.ffn_*_shexp=CPU")
                cpu_ot.append(f"blk.{s}.ffn_gate_inp=CPU")
                cpu_ot.append(f"blk.{s}.exp_probs_b=CPU")
            else:
                cpu_ot.append(f"blk.{s}-{e}.ffn_*_exps=CPU")
                cpu_ot.append(f"blk.{s}-{e}.ffn_*_shexp=CPU")
                cpu_ot.append(f"blk.{s}-{e}.ffn_gate_inp=CPU")
                cpu_ot.append(f"blk.{s}-{e}.exp_probs_b=CPU")

    return {
        "gpu_layers": gpu_ranges,
        "cpu_layers": cpu_ranges,
        "ot_cpu": ",".join(cpu_ot) if cpu_ot else None,
        "n_gpu_moe": len(gpu_ranges),
        "n_cpu_moe": len(cpu_ranges),
    }


def print_breakdown(result):
    """Print detailed VRAM breakdown chart."""
    print()
    print("=" * 100)
    print(f"  MoE OFFLOAD CALCULATOR — {result['gpu'].upper()} ({GPU_VRAM_MB[result['gpu']]} MB)")
    print(f"  Context: {result['context']:,} tokens | KV Cache: {result['cache_type']}")
    print("=" * 100)

    # Summary
    print()
    print(f"  LAYERS ON GPU:  {result['n_gpu_moe_layers']+1} (including L0 dense)")
    print(f"  LAYERS ON CPU:  {result['n_cpu_moe_layers']} MoE layers")
    print(f"  Experts: {EXPERTS_TOTAL} total, top-{EXPERTS_ACTIVE} active per token")
    print()

    # VRAM breakdown
    vram = GPU_VRAM_MB[result["gpu"]]
    print(f"  VRAM BREAKDOWN ({vram} MB total):")
    print(f"  {'─' * 60}")
    bar_width = 40

    items = [
        ("Attention", result["gpu_attn_mb"], "attn"),
        ("Router/Gating", result["gpu_router_mb"], "router"),
        ("Dense FFN (L0)", result["gpu_dense_mb"], "dense"),
        ("Layer Norms", result["gpu_norm_mb"], "norm"),
        ("Expert Weights", result["gpu_expert_mb"], "expert"),
        ("KV Cache", result["gpu_kv_mb"], "kv"),
    ]

    for name, mb, tag in items:
        pct = mb / vram * 100
        bar_len = int(pct / 100 * bar_width)
        bar = "█" * bar_len + "░" * (bar_width - bar_len)
        print(f"  {name:18s} {mb:8.1f} MB ({pct:5.1f}%) {bar}")

    headroom = result["gpu_headroom_mb"]
    print(f"  {'─' * 60}")
    print(f"  {'TOTAL':18s} {result['gpu_total_mb']:8.1f} MB ({result['gpu_total_mb']/vram*100:.1f}%)")
    print(f"  {'HEADROOM':18s} {headroom:8.1f} MB ({headroom/vram*100:.1f}%)")
    print(f"  {'CPU EXPERTS':18s} {result['cpu_expert_mb']:8.1f} MB ({result['cpu_expert_mb']/1024:.1f} GB)")
    print(f"  {'RAM NEEDED':18s} {result['ram_needed_mb']:8.1f} MB ({result['ram_needed_mb']/1024:.1f} GB)")
    print()

    # Layer-by-layer chart
    print(f"  LAYER-BY-LAYER PLACEMENT:")
    print(f"  {'─' * 90}")
    print(f"  {'Layer':>6s} {'Attn':>8s} {'Expert':>8s} {'Router':>7s} {'Total':>8s} {'Side':>6s} {'Running On'}")
    print(f"  {'─' * 90}")

    gpu_layers = set(range(MOE_START, MOE_START + result["n_gpu_moe_layers"]))
    gpu_cumulative = 0
    cpu_cumulative = 0

    for i in range(TOTAL_LAYERS):
        l = LAYERS[i]
        attn = l["attn"] / 1e6
        expert = (l["expert_exps"] + l["expert_shexp"]) / 1e6
        router = l["router"] / 1e6
        total = attn + expert + router + l["ffn_gate"]/1e6 + l["ffn_norm"]/1e6

        if i == DENSE_LAYER:
            side = "GPU"
            running = "attention + FFN (dense, no experts)"
            gpu_cumulative += total
        elif i in gpu_layers:
            side = "GPU"
            running = "attention + router + 8/64 experts (active)"
            gpu_cumulative += attn + router  # only active experts on GPU
        else:
            side = "CPU"
            running = "attention (GPU) + experts (CPU/RAM)"
            gpu_cumulative += attn
            cpu_cumulative += expert

        # Memory bar
        mem_bar = "█" * min(int(total / 10), 30)
        print(f"  L{i:02d}   {attn:7.1f} {expert:7.1f} {router:6.1f} {total:7.1f}MB   {side:4s}  {running}")
        if i == DENSE_LAYER:
            print(f"  {'':>6s} {'':>8s} {'':>8s} {'':>7s} {'':>8s}       {mem_bar}")

    print(f"  {'─' * 90}")
    print()

    # Summary chart
    print(f"  GPU vs CPU SUMMARY:")
    print(f"  {'─' * 60}")
    print(f"  {'Component':30s} {'GPU':>10s} {'CPU':>10s}")
    print(f"  {'─' * 60}")

    gpu_attn = sum(LAYERS[i]["attn"] for i in range(TOTAL_LAYERS)) / 1e6
    gpu_router = sum(LAYERS[i]["router"] for i in range(MOE_START, MOE_START + result["n_gpu_moe_layers"])) / 1e6
    cpu_router = sum(LAYERS[i]["router"] for i in range(MOE_START + result["n_gpu_moe_layers"], TOTAL_LAYERS)) / 1e6
    gpu_expert = result["gpu_expert_mb"]
    cpu_expert = result["cpu_expert_mb"]

    print(f"  {'Attention layers':30s} {gpu_attn:9.1f}MB {'(all on GPU)':>10s}")
    print(f"  {'Router/Gating':30s} {gpu_router:9.1f}MB {cpu_router:9.1f}MB")
    print(f"  {'Expert FFN weights':30s} {gpu_expert:9.1f}MB {cpu_expert:9.1f}MB")
    print(f"  {'KV Cache':30s} {result['gpu_kv_mb']:9.1f}MB {'(all on GPU)':>10s}")
    print(f"  {'─' * 60}")
    print(f"  {'TOTAL':30s} {result['gpu_total_mb']:9.1f}MB {cpu_expert:9.1f}MB")
    print()

    # Commands
    ot = generate_ot_patterns(result["n_gpu_moe_layers"])
    print(f"  LLAMA.CPP COMMANDS:")
    print(f"  {'─' * 60}")
    print()

    # Option 1: --n-cpu-moe
    n_cpu = result["n_cpu_moe_layers"]
    print(f"  Option 1: --n-cpu-moe (simple)")
    print(f"    --n-cpu-moe {n_cpu}")
    print(f"    Offloads ALL MoE layers' experts to CPU (layers {MOE_START + result['n_gpu_moe_layers']}-{TOTAL_LAYERS-1})")
    print(f"    Layers 0-{MOE_START + result['n_gpu_moe_layers']-1} experts stay on GPU")
    print()

    # Option 2: -ot (granular)
    print(f"  Option 2: --override-tensor (granular)")
    if ot["ot_cpu"]:
        # Wrap pattern for shell
        ot_str = ot["ot_cpu"]
        print(f"    -ot '{ot_str}'")
        print(f"    Offloads expert tensors in layers {ot['cpu_layers'][0]}-{ot['cpu_layers'][-1]} to CPU")
    else:
        print(f"    No -ot needed (all layers on GPU)")
    print()

    # Full command
    print(f"  FULL COMMAND (3070, {result['context']//1024}K context):")
    cmd_parts = [
        "docker run -d --name glm-3070-cmoe",
        "  --network host --ipc host --gpus device=1",
        "  -v /data/models/:/models-data/:ro",
        "  beellama-kvarn:latest",
        "  -m /models-data/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_S.gguf",
        "  --port 8082 --host 0.0.0.0",
        f"  -ngl 999 -c {result['context']} -t 20",
        "  --flash-attn on",
        f"  --cache-type-k {result['cache_type']} --cache-type-v {result['cache_type']}",
    ]
    if n_cpu > 0:
        cmd_parts.append(f"  --n-cpu-moe {n_cpu}")
    cmd_parts.append("  --load-mode none")
    cmd_parts.append("  -fit off")
    print("  \\")
    for p in cmd_parts:
        print(f"    {p.strip()} \\")
    print("  ; echo 'started'")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — Run all scenarios
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("GLM-4.7-Flash-REAP-23B-A3B — MoE Offload Calculator")
    print("Model: 23B total, 3B active, 64 experts top-8, 47 layers (L0 dense, L1-46 MoE)")
    print("Tensor structure: attn(15MB/layer) + expert_exps(254MB/layer) + expert_shexp(7MB/layer) + router(0.4MB/layer)")
    print(f"Total model: {sum(sum(v.values()) for v in LAYERS.values())/1e9:.2f} GB")
    print(f"  Attention: {sum(LAYERS[i]['attn'] for i in range(TOTAL_LAYERS))/1e9:.2f} GB")
    print(f"  Expert FFN: {sum(LAYERS[i]['expert_exps']+LAYERS[i]['expert_shexp'] for i in range(MOE_START, TOTAL_LAYERS))/1e9:.2f} GB")
    print(f"  Router: {sum(LAYERS[i]['router'] for i in range(MOE_START, TOTAL_LAYERS))/1e9:.2f} GB")
    print(f"  Dense FFN: {LAYERS[0]['ffn_gate']/1e9:.2f} GB")

    # ── Scenario 1: 3070, 8K context ──
    r = calculate_vram_budget(gpu="3070", context=8192, cache_type="q8_0")
    print_breakdown(r)

    # ── Scenario 2: 3070, 16K context ──
    r = calculate_vram_budget(gpu="3070", context=16384, cache_type="q8_0")
    print_breakdown(r)

    # ── Scenario 3: 3070, 32K context ──
    r = calculate_vram_budget(gpu="3070", context=32768, cache_type="q8_0")
    print_breakdown(r)

    # ── Scenario 4: 3070, 64K context (target) ──
    r = calculate_vram_budget(gpu="3070", context=65536, cache_type="q8_0")
    print_breakdown(r)

    # ── Scenario 5: 3070, 64K with q4_0 KV (aggressive) ──
    r = calculate_vram_budget(gpu="3070", context=65536, cache_type="q4_0")
    print_breakdown(r)

    # ── Scenario 6: 3090, 128K context (reference) ──
    r = calculate_vram_budget(gpu="3090", context=131072, cache_type="q8_0")
    print_breakdown(r)

    # ── Comparison table ──
    print()
    print("=" * 100)
    print("  COMPARISON TABLE — All Scenarios")
    print("=" * 100)
    print()
    fmt = "{:<12s} {:>8s} {:>6s} {:>6s} {:>9s} {:>9s} {:>9s} {:>8s} {:>10s}"
    print(fmt.format("GPU", "Context", "Cache", "GPU_L", "GPU_MB", "KV_MB", "CPU_MB", "Headroom", "Cmd"))
    print("─" * 100)

    for gpu in ["3070", "3090"]:
        for ctx in [8192, 16384, 32768, 65536, 131072]:
            for cache in ["q8_0", "q4_0"]:
                if gpu == "3090" and ctx > 131072:
                    continue
                if gpu == "3070" and ctx > 65536:
                    continue
                r = calculate_vram_budget(gpu=gpu, context=ctx, cache_type=cache)
                cmd = f"--n-cpu-moe {r['n_cpu_moe_layers']}" if r["n_cpu_moe_layers"] > 0 else "full GPU"
                fits = "OK" if r["gpu_headroom_mb"] > 0 else "OOM"
                print(fmt.format(
                    gpu, f"{ctx//1024}K", cache,
                    f"{r['n_gpu_moe_layers']+1}/{TOTAL_LAYERS}",
                    f"{r['gpu_total_mb']:.0f}",
                    f"{r['gpu_kv_mb']:.0f}",
                    f"{r['cpu_expert_mb']:.0f}",
                    f"{r['gpu_headroom_mb']:.0f}MB",
                    f"{fits} {cmd[:20]}"
                ))
