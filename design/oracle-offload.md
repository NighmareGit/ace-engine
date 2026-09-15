# Oracle Offload Design — P6 Capstone

**Status:** SPEC-LEVEL (no oracle model download/deployment this wave)
**Author:** Wave-6 oracle builder
**Date:** 2026-09-10
**Hardware budget:** RTX 3090 24GB, with the 35B judge resident (10-12GB headroom).

## 1. Problem

The P6 oracle tier needs an 180B-class Mixture-of-Experts (MoE) model that can
run on the Triton host's RTX 3090 (24GB) **while the 35B judge stays resident**.
The judge occupies ~12-14GB VRAM (Qwen3.6-35B-Q4_K_M ≈ 20GB at full offload,
but runs at ~12-14GB with the engine's working context), leaving **10-12GB
headroom** for the oracle's in-VRAM portion. The oracle's remaining experts
offload to host RAM (31GB) and optionally SSD via mmap.

Key constraint: the oracle is **latency-tolerant** (pre-digestion work inverts
the big-model feasibility math — we have minutes, not milliseconds), so PCIe
expert streaming is acceptable.

**Environment facts (verified, `spikes-w0-results.md`):**
- RTX 3090: 24GB VRAM (device 0)
- RTX 3070: 8GB VRAM (device 1) — runs the 9B subject, NOT the oracle
- Triton host: i5-13600K (20 threads), 31GB RAM
- 35B judge: resident on 3090, ~12-14GB VRAM in use during oracle digest

## 2. VRAM Budget

```
3090 total:                 24 GB
  − 35B judge (resident):  ~12-14 GB
  − CUDA context + misc:   ~0.5-1 GB
  = oracle VRAM budget:     ~10-11 GB  (conservative: plan for 10 GB)
```

Host RAM headroom: 31GB total, ~4.7GB free with both GPU servers running
(S3 measurement). The oracle's CPU-offloaded experts live here — ~15-25GB
for the cold experts, feasible but tight. SSD (mmap) backs the overflow.

## 3. Candidate Models

Three candidates analyzed with explicit size/active-expert math.

### Candidate A — Qwen3-235B-A22B (recommended)

- **Architecture:** 235B total params, ~22B active per token (MoE, 128 experts,
  8 active). Dense embeddings + attention are the always-resident cost.
- **Source:** [Qwen3-Instruct docs](https://inference.readthedocs.io/en/latest/models/builtin/llm/qwen3-instruct.html),
  [Reddit discussion](https://www.reddit.com/r/LocalLLaMA/comments/1kb97ys/ubergarmqwen3235ba22bgguf_over_140_toks_pp_and_10/)
- **Expert math** (from [Spheron blog](https://www.spheron.network/blog/dynamic-expert-quantization-moe-offloading-gpu-cloud/)):
  - 128 experts × ~1.56B params each ≈ 200B expert params
  - Dense (embedding + attention): ~35B params
- **Q4_K_M quant:** ~130GB full model (too large for VRAM, but we offload)
- **Q3_K_M quant (oracle working quant):** ~100GB full
  - Dense portion Q3: ~20GB → but only attention KV + embeddings resident
  - Per-expert Q3: ~0.78GB; 8 active experts in VRAM: ~6.2GB
  - **In-VRAM budget: ~6-8GB (active experts) + ~2GB (attention/KV cache) ≈ 8-10GB** ✓ fits
- **Why recommended:** Best capability/VRAM ratio. Apache-2.0 license. Active
  expert count (8) is small enough that the in-VRAM working set fits the
  10GB budget at Q3. The 22B-active footprint per token is the right size for
  PRD pre-digestion quality.

### Candidate B — DeepSeek-V3 (671B-A37B)

- **Architecture:** 671B total, ~37B active per token (MoE, 256 experts).
- **Source:** [unsloth/DeepSeek-V3-GGUF](https://huggingface.co/unsloth/DeepSeek-V3-GGUF)
- **Q2_K_XS quant:** ~130GB full (from HF card)
  - Per-expert Q2: ~0.4GB; 8 active experts: ~3.2GB
  - Dense portion: ~50B params Q2 ≈ 12.5GB
  - **In-VRAM: ~3.2GB (active) + ~3GB (attention/KV) ≈ 6-7GB** ✓ fits
- **Why not first choice:** 37B active per token is overkill for PRD digestion
  and the Q2 quant degrades quality. License is MIT (fine), but the model is
  much larger to store (~130GB SSD). Slower expert-swap latency due to 256
  experts. Reserve as the "full oracle" if Candidate A proves insufficient.

### Candidate C — Qwen3-30B-A3B (mild case, NOT 180B-class)

- **Architecture:** 30B total, ~3B active per token (MoE).
- **Q4_K_M:** ~17GB full → **fully resident in VRAM** with zero offload.
- **Why listed:** The spec names this as the "mild case" that fits fully in
  VRAM. It is NOT 180B-class but is the cheapest oracle — useful as a canary
  model and as a fallback if the 180B candidates prove infeasible on the
  owner's hardware. No expert offload needed; just `-ngl 999`.

### VRAM Math Summary

| Candidate       | Total | Active | Quant  | In-VRAM (est.) | Fits 10GB? |
|-----------------|-------|--------|--------|----------------|------------|
| Qwen3-235B-A22B | 235B  | 22B    | Q3_K_M | ~8-10 GB       | ✓ (tight)  |
| DeepSeek-V3     | 671B  | 37B    | Q2_K_XS| ~6-7 GB        | ✓          |
| Qwen3-30B-A3B   | 30B   | 3B     | Q4_K_M | ~17 GB (full)  | ✓ (no off) |

## 4. llama-server Flag Recommendations

For the recommended candidate (Qwen3-235B-A22B at Q3_K_M):

```bash
llama-server \
  --model oracle-qwen3-235b-q3_k_m.gguf \
  --port 8086 \
  --n-gpu-layers 999 \          # offload all layers to GPU; experts stream
  --ctx-size 32768 \            # oracle context budget (latency-tolerant)
  --n-cpu-moe 0 \               # route ALL MoE experts through GPU scheduler
                                  # (see compute-market guide [1])
  --mmap \                      # use mmap for weight offload to RAM/SSD
  --cache-type-k q5_0 \         # KV cache quant to fit the context budget
  --cache-type-v q5_0 \
  --threads 16 \                # i5-13600K: use 16 of 20 threads
  --temp 0.2 \
  --reasoning-format none       # oracle emits structured JSON, no reasoning
```

**Expert offload patterns (`-ot`):** llama.cpp supports `-ot` to pin specific
expert index ranges to GPU. For the oracle, we want the 8 hottest experts
(active per token) in VRAM and the rest on CPU/SSD:

```bash
  -ot "ffn.*.expert_0-7=CPU" \   # example pattern: first 8 experts on CPU
                                  # (tune based on actual expert routing)
```

The exact `-ot` pattern must be tuned empirically during the canary (§6).
See [llama.cpp issue #20757](https://github.com/ggml-org/llama.cpp/issues/20757)
for the two-tier GPU+RAM expert cache design that motivates this approach.

**Key flags explained:**
- `--n-gpu-layers 999`: offload all dense layers; MoE experts are scheduled
  separately via `--n-cpu-moe` / `-ot`.
- `--mmap`: weights memory-mapped from SSD → only active pages in RAM. Critical
  for fitting the ~100GB Q3 model in 31GB host RAM.
- `--cache-type-k q5_0`: quantize KV cache to fit 32K context in the VRAM budget.
- `--n-cpu-moe 0`: the [compute-market guide](https://www.compute-market.com/blog/run-large-moe-models-small-gpu-2026)
  notes that if a 35B-A3B fits on the card, `--n-cpu-moe 0` routes all MoE
  through the GPU scheduler for optimal expert placement.

## 5. Deployment Plan (Triton)

### Container layout

```
host (i5-13600K, 31GB RAM, RTX 3090 + RTX 3070)
├── beellama-3070   (compose: beellama-kvarn)  :8082  — 9B subject
├── beellama-3090   (compose: beellama-kvarn)  :8080  — 35B judge
└── oracle-moe      (NEW compose service)      :8086  — oracle (this doc)
```

- **Image:** extend `ace-sandbox:latest` (already has llama-server 0.4.4-dev).
  The oracle container needs the CUDA-linked binary + the GGUF on a volume.
- **Port:** 8086 (convention; NOT running yet — `OracleConfig.port` default).
- **GPU:** RTX 3090 (device 0), shared with the 35B judge. The judge stays
  resident; the oracle's in-VRAM working set fits the 10GB headroom.
- **Volume:** `/home/<user>/models/oracle/` holds the GGUF (~100GB for Q3).

### Startup order

1. beellama-3090 (35B judge) — must be resident BEFORE the oracle starts, so
   the oracle's VRAM probe sees the true headroom.
2. oracle-moe — starts, probes VRAM, places experts accordingly.
3. beellama-3070 (9B subject) — independent.

### Failure isolation (oracle down ≠ engine down)

- The oracle container is **independent** of the engine's hot path. If it
  crashes or is stopped:
  - `digest_prd` catches the transport error → `OracleUnavailable` → falls
    back to the 35B judge (O2 path). No engine crash.
  - `escalate_task_to_oracle` returns `degraded=True` → engine falls through
    to the existing FAILED path. No engine crash.
  - `should_escalate_to_oracle` returns `(False, "oracle disabled")` when
    unreachable → no escalation attempted.
- The engine's `_maybe_replan` → oracle hook is wrapped in a broad
  `except Exception` (engine.py) so any oracle bug is logged, not propagated.
- Health: the engine's `check_health` does NOT probe the oracle (only the
  transport's primary endpoint). Oracle health is checked lazily on first
  escalation attempt.

## 6. Canary Protocol

Before the oracle is trusted for production escalations:

1. **Deploy** the oracle container on Triton with Candidate A (Qwen3-235B-A22B
   Q3_K_M). No model download this wave — this is the W3 deployment step.
2. **First 10 escalations:** compare oracle digest quality vs. the 35B re-plan
   baseline. Metrics:
   - Atom count (expect 2-8 atoms per task)
   - Validator pass rate (expect ≥80% on first try, ≥95% with retry)
   - Coverage (100% — every original task mapped)
   - Latency (expect 30-120s per digest — latency-tolerant)
3. **Quality gate:** oracle digest must produce ≥1 valid atom ticket that the
   existing pipeline can execute. If the oracle's atoms are worse than the 35B
   re-plan's output, the canary fails → stay on 35B-only.
4. **VRAM monitor:** during the canary, log peak VRAM on the 3090. If the
  oracle evicts the 35B judge (OOM), reduce `--n-gpu-layers` or switch to
  Candidate B (DeepSeek-V3 Q2_K_XS, smaller in-VRAM footprint).

## 7. Recommendation

**Candidate A (Qwen3-235B-A22B at Q3_K_M)** is the recommended oracle:

- 22B active per token — the right capability for PRD pre-digestion.
- ~8-10GB in-VRAM working set — fits the 10GB headroom budget (tight but
  feasible with Q3 quant + KV quant).
- Apache-2.0 license — no legal blocker.
- Expert offload via `--n-cpu-moe 0` + `--mmap` + `-ot` patterns.

**Fallback chain if Candidate A is infeasible on the owner's hardware:**
1. Candidate C (Qwen3-30B-A3B, fully resident, no offload) — the "mild case".
2. Candidate B (DeepSeek-V3 Q2_K_XS) — smaller in-VRAM, lower quality.

No oracle model is downloaded or deployed in this wave (W3 deployment step,
gated on owner hardware). This doc is spec-level only.

## Sources

- [Run 100B+ MoE Models on a 16GB GPU (2026 Hardware Guide)](https://www.compute-market.com/blog/run-large-moe-models-small-gpu-2026) — `--n-cpu-moe` flag, 35B-A3B on 24GB
- [Dynamic Expert Quantization on GPU Cloud (2026)](https://www.spheron.network/blog/dynamic-expert-quantization-moe-offloading-gpu-cloud/) — Qwen3-235B-A22B expert math (128 experts, ~1.56B params each)
- [unsloth/DeepSeek-V3-GGUF (Hugging Face)](https://huggingface.co/unsloth/DeepSeek-V3-GGUF) — Q2_K_XS quant, multi-file GGUF
- [llama.cpp two-tier GPU+RAM expert cache (issue #20757)](https://github.com/ggml-org/llama.cpp/issues/20757) — persistent VRAM expert slot cache design
- [Qwen3-Instruct docs](https://inference.readthedocs.io/en/latest/models/builtin/llm/qwen3-instruct.html) — Qwen3 model family
- [ubergarm/Qwen3-235B-A22B-GGUF (Reddit)](https://www.reddit.com/r/LocalLLaMA/comments/1kb97ys/ubergarmqwen3235ba22bgguf_over_140_toks_pp_and_10/) — community GGUF benchmarks
