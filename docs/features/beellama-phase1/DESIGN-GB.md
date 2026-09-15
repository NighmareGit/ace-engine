# DESIGN-GB — Group B (cache-port) Codebase Design

> **Scope:** GB-T01..T06 — port PR #27861's GPU↔RAM LRU expert cache onto the
> rebased beellama-port base (post `preview-v0.4.7`), then attach R07 telemetry.
> **TARGET-REPO:** beellama-port · **C/C++** · **Design-only — no implementation.**
> **Author:** codebase-design pass · **Date:** 2026-09-11 · **~250 lines.**

---

## 1. Port Map: PR #27861 components → rebased tree

PR #27861 touches 8 files. The rebased base has drifted in `llama-graph.cpp`
(310 changed lines, per R02). Below: each component, its upstream attachment
point, what the drift diff changed there, and the adaptation required.

| # | Component | PR file | Drift conflict? | Adaptation |
|---|-----------|---------|-----------------|------------|
| 1 | **Cache manager** (`llama-moecache.{h,cpp}`) | new files | None | Extract verbatim (diff lines 347–826). |
| 2 | **LRU step** (`llama_moe_cache_step`) | `llama-context.cpp` | None — `decode()` unchanged by drift | Insert before `return 0` in `decode()`. |
| 3 | **build_moe_ffn hook** (lookup + device chain) | `llama-graph.cpp` | **Three nearby drift zones**: (a) `LLM_ARCH_HY_V4` added to `swiglu_clamp_exp`; (b) `n_expert_used` → `n_expert_used(il)`; (c) `build_qkv` overload split | Anchors (`ggml_build_forward_expand(gf, weights)` and `experts = build_lora_mm_id(down_exps,...)`) are **structurally stable**. Device chain is self-contained — mirrors activation inline, does NOT touch drifted `swiglu_clamp_exp`. **Port verbatim; do NOT absorb drift's `LLM_ARCH_HY_V4` or `n_expert_used_il`.** |
| 4 | **Arg/config plumbing** | `common/`, `include/llama.h` | None — drift confined to `src/` | Port verbatim. |
| 5 | **ggml obs callback** | `ggml/include/ggml.h`, `ggml/src/ggml.c`, `ggml/src/ggml-cpu/ggml-cpu.c` | None | Port verbatim. |
| 6 | **Stats surface** (`llama_moe_stream_print_stats`) | *not in PR #27861* | N/A | **New work (GB-T05).** PR only has `LLAMA_LOG_DEBUG` emit (diff line 753–758). The structured print_stats is an R07 extension designed from scratch. |

**Key drift insight:** The 310-line drift is concentrated in KV-cache attention paths
(`build_attn`, `build_attn_mha`, `build_qkv`, KVarN routing) and the MoE expert
aggregation loop. The cache hook's two insertion anchors in `build_moe_ffn` —
the `ggml_build_forward_expand(gf, weights)` line and the `experts =
build_lora_mm_id(down_exps, ...)` line — are **structurally stable**. The cache
device chain is self-contained and does not touch the drifted `swiglu_clamp_exp`
block. This means the port is low-risk: the drift does not cross the cache seam.

---

## 2. Ticket-by-ticket implementation contract

### GB-T01 — Port `llama-moecache.{h,cpp}` + CMake

**Files:** `src/llama-moecache.h`, `src/llama-moecache.cpp`, `src/CMakeLists.txt`

**Interface (header):**
```cpp
struct llama_moe_cache_layer {
    int il = -1;
    int32_t n_slots = 0;
    ggml_tensor * up_src   = nullptr;  // host-resident authoritative weights
    ggml_tensor * gate_src = nullptr;
    ggml_tensor * down_src = nullptr;
    ggml_tensor * up_c     = nullptr;  // device-resident cache [ne0,ne1,n_slots+1]
    ggml_tensor * gate_c   = nullptr;
    ggml_tensor * down_c   = nullptr;
    ggml_tensor * dev_table  = nullptr;  // I32 [1, n_expert] expert→slot
    ggml_tensor * host_table = nullptr;  // I32 [1, n_expert] CPU mirror for skip
};

void llama_moe_cache_init(const llama_model & model, int32_t n_slots, int32_t max_inserts);
const llama_moe_cache_layer * llama_moe_cache_lookup(const ggml_tensor * up_exps);
void llama_moe_cache_step();
```

**Build integration:** Add `llama-moecache.cpp` to `src/CMakeLists.txt` after
`llama-model.cpp` (matching PR line 40). No new library — it links into `libllama`.

**Compilability:** This ticket is self-contained. The new files compile once
`ggml.h` has the `ggml_moe_obs_cb_t` typedef (GB-T02 adds it, but GB-T01 can
still compile without the callback being called — the header just needs the
typedef visible). **Ordering note:** GB-T01 should be merged before GB-T02 so the
build sees the typedef; if GB-T01 lands first, add a forward `#ifndef` guard or
simply merge T01+T02 atomically. The grain law (no ticket breaks the build) holds
because the new files are additive and the CMake change is a one-liner.

### GB-T02 — Port ggml MoE observation callback

**Files:** `ggml/include/ggml.h`, `ggml/src/ggml.c`, `ggml/src/ggml-cpu/ggml-cpu.c`

**Interface additions:**
```cpp
// ggml.h (after ggml_set_abort_callback)
typedef void (*ggml_moe_obs_cb_t)(const char * tensor_name, const struct ggml_tensor * ids, void * ud);
GGML_API void              ggml_set_moe_obs_callback(ggml_moe_obs_cb_t cb, void * ud);
GGML_API ggml_moe_obs_cb_t ggml_get_moe_obs_callback(void ** ud);
```

**ggml.c:** globals `g_moe_obs_cb` / `g_moe_obs_ud` + getter/setter (static file-scope).

**ggml-cpu.c:** In `ggml_compute_forward_mul_mat_id`, after the `memset(matrix_row_counts, ...)`:
- declare `moe_tbl` / `moe_dummy` from `dst->src[3]` + `op_params[0]`
- skip cached ids in the inner loop (zero dst row + `continue`)
- invoke the observation callback after the `matrix_row_counts` loop when `strstr(src0->name, "ffn_gate_exps")`

**Compilability:** Purely additive to ggml. No existing code modified. Build green.

### GB-T03 — Port public API + CLI

**Files:** `include/llama.h`, `common/arg.cpp`, `common/common.h`, `common/common.cpp`

**Interface additions:**
```cpp
// include/llama.h — llama_context_params struct
int32_t  n_moe_cache_slots   = 0;   // cache slots per host-resident expert layer
int32_t  n_moe_cache_inserts = 2;   // max expert uploads/layer/step

// include/llama.h — new public functions
LLAMA_API void llama_moe_stream_print_stats(const struct llama_model * model);

// include/llama.h — telemetry ring-buffer API (remote-consumption seam, §4)
// Forward-declared in T03; defined in T05. No call sites until Phase 2.
LLAMA_API void  llama_moe_telemetry_enable(void);
LLAMA_API void  llama_moe_telemetry_disable(void);
LLAMA_API bool  llama_moe_telemetry_is_enabled(void);
LLAMA_API uint64_t llama_moe_telemetry_dropped(void);
LLAMA_API size_t llama_moe_telemetry_drain(struct moe_telemetry_event * out, size_t max);
LLAMA_API size_t llama_moe_telemetry_json(char * buf, size_t buf_size);

// common/common.h — common_params struct
int32_t n_moe_cache_slots   = 0;
int32_t n_moe_cache_inserts = 2;
```

**arg.cpp:** `--moe-expert-cache` (int → `n_moe_cache_slots`, env `LLAMA_ARG_MOE_EXPERT_CACHE`)
and `--moe-expert-cache-inserts` (int → `n_moe_cache_inserts`, env `LLAMA_ARG_MOE_EXPERT_CACHE_INSERTS`).

**common.cpp:** `cparams.n_moe_cache_slots = params.n_moe_cache_slots;` (and inserts)
in `common_context_params_to_llama`.

**Compilability:** Additive. The `llama_moe_stream_print_stats` and
`llama_moe_telemetry_*` declarations are forward decls with no definitions yet
— those land in GB-T05. To keep the build green at T03, declare in `llama.h`
but do NOT add call sites until T05. The linker will not complain until something
references them.

### GB-T04 — Port graph-integration (deepest seam)

**Files:** `src/llama-graph.cpp`, `src/llama-context.cpp`, `include/llama.h`

**This is the critical seam.** The 7 shared files converge here. Contract:

**llama-graph.cpp — `build_moe_ffn` (3 insertion points):**
1. After `ggml_build_forward_expand(gf, weights)`: cache lookup guard — only when
   `n_tokens == 1 && !gate_up_exps && gate_exps && down_exps && !*_b && !*_s &&
   type_op == LLM_FFN_SILU && !weight_before_ffn && loras->empty()`. Call
   `llama_moe_cache_lookup(up_exps)` → `mcache`; build `mc_slot_ids` via
   `ggml_get_rows(ctx0, mcache->dev_table, selected_experts)`.
2. After each `build_lora_mm_id` for up/gate/down: if `mcache`, set
   `tensor->src[3] = mcache->host_table`, `tensor->op_params[0] = mcache->n_slots`.
3. After `experts = build_lora_mm_id(down_exps, ...)`: if `mcache`, build device
   chain (`up_c`/`gate_c`/`down_c` via `ggml_mul_mat_id` + inline SwiGLU mirroring
   `type_op`) and `ggml_add` into `experts`.

**Activation note:** Device chain mirrors `LLM_FFN_SILU` with `swiglu_clamp_exp`
(DEEPSEEK4/DFlash clamp). **Do NOT import the drift's `LLM_ARCH_HY_V4` branch.**

**llama-context.cpp:** `#include "llama-moecache.h"`; `llama_moe_cache_init(model, params.n_moe_cache_slots, params.n_moe_cache_inserts)` after the constructing log; `llama_moe_cache_step()` before `return 0` in `decode()`.

**Compilability:** All changes guarded by `if (mcache)`; non-cache path is
byte-identical to pre-port. Build green.

### GB-T05 — R07 telemetry counters (adapted from prior art)

**Files:** `src/llama-moecache.cpp`, `src/llama-moecache.h`, `src/llama-graph.cpp`

**Do not reinvent.** Prior telemetry research exists in the phasebuffer project
and was designed explicitly for llama.cpp-class engines. GB-T05 **adapts** three
prior-art sources rather than inventing a fourth:

| Prior-art source | What GB-T05 adopts | What it defers to Phase 2 |
|-----------------|-------------------|--------------------------|
| **Lidenburg** `lidenburg-telemetry-concept.md` — the VRAM↔RAM↔SSD tiered cache telemetry for the same architecture PR #27861/#25294 implement | Per-tier hit/miss/eviction counters (`g_cache_hits`, `g_cache_misses`, `g_ram_hits`, `g_disk_misses`); per-layer `layer_stats` breakdown; `expert_event` ring for tier transitions; eviction-cause logging (`g_evictions`, `g_eviction_reloads`) | PCIe bandwidth timing (`g_pcie_transfer_ns/bytes`); io_uring deep telemetry; 100ms time-series sampling; N+1 prefetch temporal accuracy |
| **phasebuffer** `telemetry-interface-system.md` — engine-agnostic zero-overhead SPSC ring buffer (≤2% overhead, 30 event variants, JSON/binary export) | The event-model *pattern*: fixed-size `Copy` events, lock-free SPSC ring, two-level gate (compile-time `#[cfg]` + runtime `AtomicBool`). In C++ this maps to a `static atomic<bool>` gate + a fixed-capacity `std::array` ring with monotonic head/tail indices (see §4 remote-consumption interface). | The full Rust `TelemetryProducer`/`TelemetryConsumer` trait hierarchy; `request_id` propagation; `LatencyHistogram`; `AnomalyDetector` — all Phase 2 deepening (see `telemetry-deepening.md`) |
| **PULSAR-OPS-00** `telemetry-event-stream-endpoints.md` — the remote-consumption interface (SSE stream, snapshot, trace, export endpoints) | The *endpoint contract*: telemetry must be remotely consumable via a streaming interface. GB-T05 defines the C++-side data source (see §4). | The HTTP/SSE server, Chrome-trace/Perfetto export, alerting rules — Phase 2 (PULSAR-OPS-01 live web-UI timeline pattern) |

**R07↔Lidenburg counter reconciliation.** R07's six counter groups map onto
Lidenburg's existing tier-event model with these deltas:

| R07 counter | Lidenburg equivalent | Delta / adaptation |
|-------------|---------------------|--------------------|
| `hit_rate[tier]` | `g_cache_hits` / `g_cache_misses` (L1), `g_ram_hits` (L2), `g_disk_misses` (L3) | R07 expresses as a *rate* (hits/token); Lidenburg uses raw counters. **Adopt R07's rate semantics** but keep Lidenburg's per-tier breakdown as the underlying accumulator. Add `n_tokens` denominator to `layer_state`. |
| `bytes_served[tier]` | Lidenburg has no byte counter (only `g_disk_wait_ns`) | **New** — Lidenburg Gap 2 identified the need for `g_pcie_transfer_bytes`. GB-T05 implements the byte-accumulator version: `n_bytes_served[tier]` incremented at each tier transition. |
| `waste_bytes[tier]` | Lidenburg `g_eviction_reloads` (counts events, not bytes) | R07 is byte-granular; Lidenburg is event-granular. **Adopt R07's byte definition** (prefetched-but-evicted/overwritten bytes) but use Lidenburg's eviction-cause logging pattern (log victim expert id + usage count at eviction time). |
| `eviction_count[tier]` | Lidenburg `g_evictions` | Near-identical. **Adopt Lidenburg's `g_eviction_reloads` extension** too — count how many times an evicted expert is re-loaded (waste indicator). |

**Counter attachment points (reconciled):**

| Counter | Definition | Attachment point | Struct field |
|---------|-----------|-----------------|--------------|
| `hit_rate[tier]` | hits / token, per tier | `llama_moe_cache_step` → `print_stats` | `n_hit / (n_hit + n_miss)`, new `n_tokens` denominator |
| `bytes_served[tier]` | bytes served per tier | `llama_moe_cache_step` (per upload publish) | new `n_bytes_served[3]` (tier-indexed) |
| `waste_bytes[tier]` | prefetched but evicted/overwritten | eviction branch (victim ≥ 0) | new `n_waste_bytes[3]` |
| `eviction_count[tier]` | eviction events per tier | eviction branch | new `n_evictions[3]` |
| `eviction_reloads[tier]` | evicted expert re-loaded (Lidenburg extension) | miss branch (expert was previously evicted) | new `n_eviction_reloads[3]` |

**Tier mapping (Phase 1):** `tier[0] = VRAM` (cache hits), `tier[1] = host RAM`
(uncached path), `tier[2] = SSD` — **always zero in Phase 1**, reserved for
Phase 2. This matches Lidenburg's L1/L2/L3 layout exactly, so Phase 2's SSD
tier slots in without a data-model change.

**Swappable prefetch design:** `layer_state` accumulates raw counters; no
prefetch policy logic lives in the cache manager. Phase 2's three-band prefetch
(PR #25294) feeds the same counters via the RECONCILE adapter. This resolves the
R04×R05 conflict by deferring the policy to Phase 2 and instrumenting from day one.

**Promote the `LLAMA_LOG_DEBUG` emit:** Remove the 512-step debug print (diff line
753–758); replace with structured counter accumulation. The always-on surface is
`llama_moe_stream_print_stats` (public API + perf-stats path).

**Compilability:** Adds fields to `layer_state` (internal to `.cpp`); build green.

### GB-T06 — Build + smoke test

**Files:** `ci/smoke-moecache.sh` (new), build artifacts.

**Compile gate:**
```bash
cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-server -j$(nproc)
```
Expected: clean build, no warnings in moecache files.

**Smoke test (GPU lease required — `gpu-lease` skill):**
```bash
# 1. Flag appears in help
./build/bin/llama-server --help 2>&1 | grep -E "moe-expert-cache"
# → --moe-expert-cache, --moe-expert-cache-inserts

# 2. Server starts with cache on a MoE model
./build/bin/llama-server -m <moe-model.gguf> --cpu-moe -fa on \
    --moe-expert-cache 64 -ngl 99 --host 0.0.0.0 --port 8080 &
# → "MoE expert cache enabled: N layers x 64 slots, 2 inserts/step, X MiB device memory"

# 3. KVarN paths untouched
grep -rn "KVarN\|kvarn\|dflash\|DFlash" src/llama-moecache.cpp \
    src/llama-moecache.h src/llama-graph.cpp src/llama-context.cpp \
    include/llama.h common/arg.cpp common/common.h common/common.cpp \
    ggml/include/ggml.h ggml/src/ggml.c ggml/src/ggml-cpu/ggml-cpu.c
# → only expected hits in llama-graph.cpp (pre-existing DFlash arch checks in
#   build_moe_ffn's activation path, which the cache mirrors inline)
```

**Expected telemetry output (proving counters increment):**
After ~512+ decode steps with the cache active, the server log or a
`llama_moe_stream_print_stats` call should show lines like:
```
moe_cache: steps=512 hits=348 misses=164 hit-rate=67.9%
moe_cache: tier[VRAM] bytes_served=... waste_bytes=... evictions=...
moe_cache: tier[RAM]  bytes_served=... (uncached path)
moe_cache: tier[SSD]  bytes_served=0 (reserved, Phase 2)
```

**Model file:** Use whichever MoE GGUF is available in the workspace (e.g.
Qwen3.5-35B-A3B, Qwen3.8-Flash-Next, or Mixtral-8x7B). The cache activates for
any model with host-resident expert weights (`--cpu-moe` or `-ot`-pinned experts).

---

## 3. Prior-art adaptation map

Three prior telemetry designs exist in `/home/<user>/projects/phasebuffer`.
GB-T05 adapts them rather than reinventing. This section records the
adoption decisions so implementers know what is *reused* vs *deferred*.

### 3.1 Adopted in Phase 1 (GB-T05)

| Pattern | Source | How it lands in beellama-port |
|---------|--------|------------------------------|
| Per-tier hit/miss/eviction counters | Lidenburg `lidenburg-telemetry-concept.md` — `g_cache_hits`, `g_cache_misses`, `g_ram_hits`, `g_disk_misses` | `layer_state` accumulators indexed by tier; `llama_moe_stream_print_stats` formats them |
| Per-layer breakdown | Lidenburg Gap 1 — `layer_stats[]` vector | `layer_state` is already per-layer (one per cached layer), so the per-layer breakdown is free |
| Eviction-cause logging | Lidenburg Gap 4 — `g_evictions` + `g_eviction_reloads` | `n_evictions[tier]` + `n_eviction_reloads[tier]` in `layer_state`; victim expert id logged via `LLAMA_LOG_INFO` at eviction time |
| Event-model pattern (fixed-size Copy events, SPSC ring, two-level gate) | phasebuffer `telemetry-interface-system.md` §3–§6 | C++ adaptation: `static atomic<bool>` runtime gate + fixed-capacity ring in `llama-moecache.cpp` (see §4) |
| Remote-consumption endpoint contract | PULSAR-OPS-00 — SSE stream, snapshot, trace, export | C++ data source defined in §4; HTTP/SSE server deferred to Phase 2 |

### 3.2 Deferred to Phase 2

| Pattern | Source | Why deferred |
|---------|--------|-------------|
| PCIe bandwidth timing (`g_pcie_transfer_ns/bytes`) | Lidenburg Gap 2 | Requires async-copy completion instrumentation; PR #25294's streaming path is the right hook |
| io_uring deep telemetry (`g_io_uring_ops/ns`) | Lidenburg Gap 5 | SSD tier (L3) not present in Phase 1 |
| 100ms time-series sampling + CSV export | Lidenburg Gap 8 | Needs a background sampler thread; Phase 2 adds the SSD tier that makes time-series valuable |
| N+1 prefetch temporal accuracy | Lidenburg Gap 7 | Three-band prefetch is a Phase 2 (PR #25294) feature |
| `request_id` propagation on events | phasebuffer `telemetry-deepening.md` Phase B | Multi-request tracing needs the request lifecycle hooks that Phase 2's serve-mode integration provides |
| Expert heatmap / routing distribution histograms | phasebuffer `telemetry-deepening.md` Phase A | Post-processing consumers over the event stream; Phase 2 builds the stream |
| Latency waterfall / TTFT / ITL | phasebuffer `telemetry-deepening.md` Phase C/D | Needs `PhaseStart`/`PhaseEnd` event pairing; Phase 2 adds the execution-phase events |
| Anomaly detector (Welford online) | phasebuffer `telemetry-deepening.md` Phase E | Needs stable time-series history; Phase 2 |
| Live web-UI timeline (PULSAR-OPS-01 pattern) | PULSAR-OPS-00 + `telemetry-deepening.md` | HTTP/SSE server + Chrome-trace export; Phase 2 |

### 3.3 Phase-2 forward-compatibility note

The data model in `layer_state` (tier-indexed counters, per-layer breakdown,
eviction-cause logging) is designed so the live web-UI timeline (PULSAR-OPS-01
pattern) can be added without a breaking change:

- Tier-indexed arrays are sized `[3]` (VRAM/RAM/SSD) — Phase 2's SSD counters
  slot in at index 2 with no recompile of the data model.
- The ring-buffer event source (§4) produces a flat event stream; the
  Chrome-trace/Perfetto exporters (PULSAR-OPS-00) consume exactly this format.
- `llama_moe_stream_print_stats` is the *synchronous* snapshot API; the ring
  buffer is the *streaming* API. Both read the same `layer_state` accumulators,
  so adding the stream in Phase 2 does not duplicate the counter logic.

---

## 4. Remote-consumption interface (C++ side)

> **Owner requirement:** telemetry data must be *remotely consumable*. Adapted
> from PULSAR-OPS-00's endpoint design, C++ side. The HTTP/SSE server itself is
> Phase 2; this section defines the data source that Phase 2's server reads.

### 4.1 Design (from phasebuffer `telemetry-interface-system.md`)

The phasebuffer telemetry system uses a lock-free SPSC ring buffer with a
two-level gate (compile-time + runtime). The C++ adaptation for beellama-port:

```
Hot path (llama_moe_cache_step / moe_obs_cb)
  │
  ▼
Runtime gate: static std::atomic<bool> g_telemetry_enabled (Relaxed load)
  │
  ▼ (if enabled)
RingBuffer::push(TelemetrySlot { timestamp_ns, event })
  │
  ▼ (drain: background thread or post-run)
TelemetryConsumer::drain() → Vec<TelemetrySlot> → serialize / export
```

**Key properties (matching phasebuffer §1):**
- Overhead when disabled: single relaxed atomic load (~1ns) — ≈0%.
- Overhead when enabled: ~10ns per event (timestamp + ring push).
- Bounded, pre-allocated: power-of-2 capacity, no heap on hot path.
- Overwrite-oldest policy with dropped-event counter (matches phasebuffer §4.1).

### 4.2 C++ data structures

```cpp
// In llama-moecache.h (public, for consumer access)
enum class moe_tier : uint8_t { VRAM = 0, RAM = 1, SSD = 2 };

enum class moe_event_kind : uint8_t {
    Hit = 0,        // expert served from cache tier
    Miss = 1,       // expert not in cache (uncached path)
    Evict = 2,      // expert evicted from a tier
    Upload = 3,     // expert uploaded to a tier (H2D copy done)
    Waste = 4,      // prefetched bytes evicted before consumption
};

struct moe_telemetry_event {
    uint64_t timestamp_ns;
    int32_t  layer;
    int32_t  expert;
    moe_tier tier;
    moe_event_kind kind;
    uint64_t bytes;
};
```

**Size:** 32 bytes (matches phasebuffer's `TelemetryEvent` 32-byte contract).
The ring buffer stores `moe_telemetry_event` directly (no heap, no indirection).

### 4.3 Ring buffer API (C++ adaptation of phasebuffer `RingBuffer<T>`)

```cpp
// In llama-moecache.cpp (internal)
class moe_telemetry_ring {
    std::vector<moe_telemetry_event> storage;  // power-of-2 capacity
    std::atomic<uint64_t> head{0}, tail{0}, dropped{0};
    size_t mask;
public:
    explicit moe_telemetry_ring(size_t capacity_power_of_2);
    bool push(const moe_telemetry_event & event);   // hot path, ~10ns
    bool pop(moe_telemetry_event & event);           // consumer side
    void drain(std::vector<moe_telemetry_event> & out);
    size_t len() const;
    uint64_t dropped_count() const;
};
```

### 4.4 Public API for remote consumption

```cpp
// In llama-moecache.h — the seam Phase 2's HTTP server plugs into
LLAMA_API void llama_moe_telemetry_enable(void);
LLAMA_API void llama_moe_telemetry_disable(void);
LLAMA_API bool llama_moe_telemetry_is_enabled(void);
LLAMA_API uint64_t llama_moe_telemetry_dropped(void);

// Drain the ring buffer (called by Phase 2's SSE endpoint or post-run export)
// Returns the number of events written to `out`. `out` is cleared first.
LLAMA_API size_t llama_moe_telemetry_drain(moe_telemetry_event * out, size_t max_count);

// Serialize to JSON (for /telemetry/snapshot endpoint)
LLAMA_API size_t llama_moe_telemetry_json(char * buf, size_t buf_size);
```

### 4.5 Endpoint contract (Phase 2 maps PULSAR-OPS-00 to these)

| PULSAR-OPS-00 endpoint | Phase 2 implementation | C++ data source |
|------------------------|----------------------|-----------------|
| `GET /telemetry/stream` (SSE) | llama-server route calling `drain()` on a 100ms timer | `moe_telemetry_ring` |
| `GET /telemetry/snapshot` | llama-server route calling `llama_moe_telemetry_json()` | `moe_telemetry_ring` |
| `GET /telemetry/trace/:token_id` | llama-server route filtering drained events | `moe_telemetry_ring` |
| `POST /telemetry/export/chrome` | llama-server route converting to Chrome trace | `moe_telemetry_ring` |
| `POST /telemetry/export/perfetto` | llama-server route converting to Perfetto | `moe_telemetry_ring` |

**Phase 1 scope:** The ring buffer + `llama_moe_telemetry_*` API are defined in
GB-T05 but the ring is *populated* only by the existing `layer_state` counters
(hit/miss/eviction/upload/waste). The HTTP/SSE server, export formats, and
alerting rules are Phase 2. This mirrors phasebuffer's separation: the ring
buffer is the transport, the endpoints are the interface.

### 4.6 Wire format (adapted from phasebuffer §9)

JSON schema for `llama_moe_telemetry_json()` (matches phasebuffer's JSON schema
in §9.1, MoE-specific):

```json
{
  "version": 1,
  "engine": "beellama",
  "events": [
    {"t": 1000000, "kind": "Hit", "layer": 0, "expert": 5, "tier": "Vram", "bytes": 0},
    {"t": 1000050, "kind": "Upload", "layer": 0, "expert": 3, "tier": "Vram", "bytes": 67108864},
    {"t": 1000100, "kind": "Evict", "layer": 0, "expert": 1, "tier": "Vram", "bytes": 67108864}
  ],
  "dropped_events": 0
}
```

Binary format (for high-frequency capture, phasebuffer §9.2): flat fixed-size
records — 8-byte absolute timestamp + 32-byte event = 40 bytes/slot, with a
`[magic: u32 = 0x4D4F4554 ("MOET")] [version: u16]` header.

---

## 5. Verification per ticket

| Ticket | Compile gate | Functional gate |
|--------|-------------|-----------------|
| GB-T01 | `cmake --build build --target llama -j$(nproc)` | header declares 3 funcs; CMake lists moecache.cpp |
| GB-T02 | build green | `ggml_moe_obs_cb_t` ×3 in ggml.h; `moe_tbl` ≥3 in ggml-cpu.c |
| GB-T03 | build green | `n_moe_cache_slots` ≥2 in llama.h; `moe-expert-cache` ≥2 in arg.cpp |
| GB-T04 | build green | `mcache` ≥5 in llama-graph.cpp; `llama_moe_cache_step` in llama-context.cpp |
| GB-T05 | build green | print_stats emits 5 counter groups (R07's 4 + Lidenburg `eviction_reloads`); ring-buffer API present; `LLAMA_LOG_DEBUG` emit removed |
| GB-T06 | build green + `--help` | server starts on MoE model; telemetry lines appear; KVarN grep clean |

---

## 6. Risk register

| Risk | Severity | Mitigation |
|------|----------|------------|
| **KVarN D64 (R06)** — the `LLM_ARCH_DFLASH && hparams.dsv4_hc_mult > 0` check in the cache's device-chain activation mirrors the existing `build_moe_ffn` pattern. If KVarN D64 changes the DFlash activation semantics, the cache chain could diverge. | LOW | The cache path only activates for `type_op == LLM_FFN_SILU` with host-resident experts. KVarN D64 affects KV attention, not the MoE FFN activation. Verified orthogonal in ITERATION-2 Pattern 11. |
| **R04×R05 prefetch-assumption conflict** — R04 assumes three-band prefetch overlaps 4.4 GB; R05 says full 6.6 GB is on the critical path. | MEDIUM | **Design the cache so the prefetch policy is swappable.** The `layer_state` struct accumulates raw counters; no prefetch policy is baked into the cache manager. Phase 2's three-band prefetch feeds the same counters via the RECONCILE adapter. The R07 counters (GB-T05) measure the actual prefetch hit fraction from day one, resolving the conflict empirically. |
| **No-P2P staging** — the host box has no GPU P2P (both GPUs via PHB). Multi-GPU expert traffic must stage through host RAM. | LOW (Phase 1) | PR #27861's cache is per-device (each GPU gets its own cache, populated from host RAM). No P2P needed for Phase 1. Multi-GPU staging is a Phase 2 concern (PR #25294's ResidentStore). |
| **`build_moe_ffn` drift absorption** — the drift's `n_expert_used_il` and `LLM_ARCH_HY_V4` changes are near the cache insertion points. | LOW | The cache hook inserts at stable anchors (before `ggml_reshape_3d`, after `build_lora_mm_id(down_exps,...)`). The device chain is self-contained. Port verbatim; do NOT absorb drift changes into the cache path. |
| **`llama_moe_stream_print_stats` is a new function** — not in PR #27861. | LOW | Design it as a thin formatter over `layer_state` counters. Declaration in T03, definition in T05. No upstream conflict. |

---

## 7. Tickets review: confirm/amend GB-T01..T06

| ID | Verdict | Notes |
|----|---------|-------|
| **GB-T01** | **Confirm** | Clean extraction of new files + CMake. Well-scoped. |
| **GB-T02** | **Confirm** | ggml callback API is self-contained. Correct dependency on T01 (needs the typedef). |
| **GB-T03** | **Confirm** | Public API + CLI. The `llama_moe_stream_print_stats` forward decl is correctly placed here even though the definition lands in T05. |
| **GB-T04** | **Confirm with amendment** | The ticket description says "context wiring in `src/llama-context.cpp`" — this is correct, but the description should explicitly note that the `build_lora_mm_id` `ids_scale` parameter (from phase-1-issues.md Issue 5 / P1-T5) is **NOT part of PR #27861** and should NOT be added here. The `ids_scale` / `mstream` additions belong to PR #25294 (Phase 2). Adding them in T04 would conflate the two PRs and break the grain law. **Amendment: add a "Do NOT port" note to T04 excluding `ids_scale`, `mstream`, `llama_moe_stream` struct, and `build_expert_gemms` — these are Phase 2 (PR #25294) concerns.** |
| **GB-T05** | **Confirm with amendment** | The four counter groups are correct per R07 but must be **reconciled with Lidenburg's tier-event model** (see §3). **Amendments: (a)** adopt Lidenburg's `g_eviction_reloads` as a 5th counter (`n_eviction_reloads[tier]`); **(b)** reserve the SSD tier slot (tier[2]) as always-zero in Phase 1; **(c)** remove (not supplement) the `LLAMA_LOG_DEBUG` 512-step emit; **(d)** add the ring-buffer + `llama_moe_telemetry_*` public API (§4) as the remote-consumption seam — the HTTP/SSE server is Phase 2 but the C++ data source is defined here. |
| **GB-T06** | **Confirm** | Build + smoke test is well-specified. The KVarN-untouched grep gate is the right verification. **Note:** GPU lease is mandatory — the `gpu-lease` skill must be acquired before any GPU-touching test. |

**Summary of amendments:**
1. **GB-T04:** Add explicit "Do NOT port" exclusions for Phase 2 (`ids_scale`, `mstream`, `llama_moe_stream`, `build_expert_gemms`).
2. **GB-T05:** Reconcile R07 counters with Lidenburg's tier-event model (§3); add `n_eviction_reloads` from Lidenburg Gap 4; reserve SSD tier slot as always-zero; remove (not supplement) the `LLAMA_LOG_DEBUG` emit; add the ring-buffer + `llama_moe_telemetry_*` public API as the remote-consumption seam (§4).
3. **GB-T06:** Reiterate GPU-lease requirement.

---

## 8. Ordering & grain law

```
GB-T01 (new files + CMake)
  ├── GB-T02 (ggml callback) ──┐
  └── GB-T03 (public API/CLI) ─┴→ GB-T04 (graph integration) → GB-T05 (telemetry) → GB-T06 (build+smoke)
```

Each ticket leaves the tree compilable:
- **T01:** new files + CMake → build green (files compile, nothing calls them yet).
- **T02:** ggml typedef + accessors → build green (callback exists, not yet set).
- **T03:** struct fields + CLI flags + forward decl → build green (no call sites yet).
- **T04:** init + step + graph hooks → build green (cache is a no-op when `n_moe_cache_slots == 0`, which is the default).
- **T05:** counter accumulation + print_stats → build green (counters increment, print is callable).
- **T06:** smoke script → no source changes, only CI artifact.

The default `n_moe_cache_slots = 0` is the key safety property: the cache is
disabled at zero, so `llama_moe_cache_lookup` returns nullptr, the `if (mcache)`
branches are dead code, and the non-cache path is byte-identical to the
pre-port tree. This guarantees the grain law holds at every step.
