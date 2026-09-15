# RECONCILE — Adapter Design (Phase 1 → Phase 2 Integration Contract)

> **Ticket:** GC-T02 (design doc authoring)
> **Source verdict:** R03 (`verdicts/R03-verdict.md`) — adapter sketch
> **Evidence:** `.scratch/beellama-phase1/adapter-research-evidence.md` (GC-T01)
> **TARGET-REPO:** ace-engine · **Design-only — no code.**
> **Date:** 2026-09-11 · **~300 lines.**
> **Supersedes:** R03's sketch (this doc elaborates the sketch to a Phase-2-implementable spec).

---

## Verdict

**Position:** supported — with amendments from GC-T01 evidence.
**Confidence:** 0.78 (re-scored by the Phase-1 Group C gate review; deductions: unspecified lock-ordering −0.05, counter-reuse ambiguity −0.03, `n_preload_ready` factual error −0.02, Q3/Q4 framing −0.02. The lock-ordering spec added in §4.3 partially recovers the original delta; final score held to 0.78 pending Phase-2 implementation verification).

The RECONCILE adapter is the integration contract that lets PR #25294's O_DIRECT SSD
streamer feed PR #27861's GPU-resident LRU cache. The two PRs are **complementary** (target
different memory hierarchies) but **interleaving** in `build_moe_ffn` (same anchor, different
insertion semantics). The adapter resolves this by defining:

1. A **fetch-path ordering** at the shared `ggml_build_forward_expand(gf, weights)` anchor.
2. A **shared context lock** that synchronizes eviction between the two caches.
3. A **`build_lora_mm_id` signature reconciliation** (the `ids_scale` divergence).
4. A **swappable-prefetch-policy** seam (raw counters, no policy in cache — DESIGN-GB constraint).

The adapter is the prerequisite for Phase 2 implementation: every Phase-2 ticket hooks
through this contract.

---

## 1. Per-Shared-File Conflict Semantics (Verified Spans)

From GC-T01 evidence. The 7 shared files, with verbatim conflict spans:

### 1.1 `src/llama-graph.cpp` — CRITICAL

Both PRs insert into `build_moe_ffn` immediately after the same anchor:

**Anchor (structurally stable, verified in rebased `beellama-port` line 2554):**
```cpp
ggml_build_forward_expand(gf, weights);
```

**PR #27861 insertion** (pr27861.diff:255-272) — cache lookup guard:
```cpp
const llama_moe_cache_layer * mcache = nullptr;
ggml_tensor * mc_slot_ids = nullptr;
if (n_tokens == 1 && !gate_up_exps && gate_exps && down_exps && ...) {
    mcache = llama_moe_cache_lookup(up_exps);
}
```

**PR #25294 insertion** (pr25294.diff:313-351) — stream remap + wave planning:
```cpp
llama_moe_stream_layer * msl = mstream ? mstream->layer(il) : nullptr;
// ... wave capacity computation ...
ggml_tensor * ids_gemm = selected_experts;
if (msl && n_stream_waves == 1) {
    ids_gemm = ggml_map_custom1(ctx0, ids_cont, llama_moe_stream_remap, 1, msl);
}
```

**Conflict:** Both insert at the same anchor. The adapter defines the composition order:
**stream remap FIRST** (produces cache-resident slot ids), **cache lookup SECOND** (serves
those slots from VRAM). See §3 for the full fetch-path contract.

**`build_lora_mm_id` signature conflict** (pr25294.diff:483-489):
```cpp
// PR #25294 adds ids_scale:
ggml_tensor * build_lora_mm_id(ggml_tensor * w, ggml_tensor * cur,
    ggml_tensor * ids, ggml_tensor * w_s = nullptr,
    ggml_tensor * ids_scale = nullptr) const;
// PR #27861 uses the 4-parameter signature (no ids_scale).
```
The adapter reconciles this: when both PRs are active, `ids_gemm` (remapped slots) is
passed as `ids` and `selected_experts` (original ids) is passed as `ids_scale`.

### 1.2 `src/llama-context.cpp` — COMPLEMENTARY

| PR | Span | Anchor |
|----|------|--------|
| #27861 | `llama_moe_cache_init(model, params.n_moe_cache_slots, params.n_moe_cache_inserts)` (diff:215) | after constructing log |
| #27861 | `llama_moe_cache_step()` (diff:225) | before `return 0` in `decode()` |
| #25294 | `moe_stream` context wiring + op_offload guard (diff:195-211) | after `cparams.kv_unified` |
| #25294 | `graph_max_nodes` wave planning (diff:2321-2447) | `graph_max_nodes` function |

No direct conflict. The adapter's shared context lock (§4) bridges the two.

### 1.3 `common/arg.cpp` — ADDITIVE

| PR | Flags | Span |
|----|-------|------|
| #27861 | `--moe-expert-cache`, `--moe-expert-cache-inserts` | diff:9-22 |
| #25294 | `--moe-stream`, `--moe-stream-cache`, `--moe-stream-io-threads`, `--moe-stream-direct` | diff:9-52 |

Both insert `add_opt` blocks in the same function region. Merge is mechanical.

### 1.4 `common/common.cpp` — COMPLEMENTARY

| PR | Span |
|----|------|
| #27861 | `cparams.n_moe_cache_slots = params.n_moe_cache_slots;` (diff:34-35) |
| #25294 | warmup skip + `mparams.moe_stream_*` forwarding (diff:64-82) |

### 1.5 `common/common.h` — ADDITIVE

| PR | Fields |
|----|--------|
| #27861 | `n_moe_cache_slots`, `n_moe_cache_inserts` |
| #25294 | `moe_stream`, `moe_stream_slots`, `moe_stream_budget`, `moe_stream_io_threads`, `moe_stream_direct` |

### 1.6 `include/llama.h` — ADDITIVE

| PR | Fields / decls |
|----|----------------|
| #27861 | `n_moe_cache_slots`, `n_moe_cache_inserts` in `llama_context_params` |
| #25294 | `moe_stream_slots/budget/io_threads/direct` in `llama_model_params` + `moe_stream` bool + `llama_moe_stream_print_stats` fwd decl |

### 1.7 `src/CMakeLists.txt` — ADDITIVE

Both add a one-liner after `llama-model.cpp`: `llama-moecache.cpp` (#27861) and
`llama-moe-stream.cpp` (#25294).

---

## 2. Adapter Architecture

### 2.1 Conceptual Model

```
┌─────────────────────────────────────────────────────────────────┐
│                        llama_context                             │
│                                                                  │
│  ┌──────────────────┐    shared context lock    ┌──────────────┐│
│  │  llama_moe_stream │◄────────────────────────►│llama_moe_cache││
│  │  (PR #25294)      │   (RECONCILE adapter)    │(PR #27861)    ││
│  │                   │                          │              ││
│  │  O_DIRECT streamer│  expert → slot remap     │  GPU LRU     ││
│  │  SSD → device     │  ─────────────────────►  │  VRAM cache  ││
│  │  cache tensors    │  slot ids feed cache     │  slot serve  ││
│  └──────────────────┘                          └──────────────┘│
│           │                                            │         │
│           ▼                                            ▼         │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │              build_moe_ffn (llama-graph.cpp)                 ││
│  │                                                              ││
│  │  1. ggml_build_forward_expand(gf, weights)                   ││
│  │  2. [STREAM] remap ids → cache slots  (ids_gemm)             ││
│  │  3. [CACHE] lookup mcache for slot_ids                       ││
│  │  4. build_lora_mm_id(..., ids_gemm, ..., selected_experts)   ││
│  │  5. [CACHE] device chain over VRAM slots → ggml_add          ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 The Two Seam Interfaces

The adapter bridges **two seam interfaces**:

**Seam A — Streamer → Cache fetch path (in `build_moe_ffn`):**

The streamer (`llama_moe_stream_remap` custom-op) rewrites expert ids to cache slot ids.
The cache (`llama_moe_cache_lookup` + device chain) serves those slots from VRAM. The
adapter defines the contract:

```
Input:  selected_experts  [n_expert_used, n_tokens]  (original expert ids)
Step 1: ids_gemm = llama_moe_stream_remap(selected_experts)  → cache slot ids
Step 2: mcache   = llama_moe_cache_lookup(up_exps)           → cache layer (or nullptr)
Step 3: mc_slot_ids = ggml_get_rows(mcache->dev_table, ids_gemm)  → device slot ids
Step 4: experts  = build_lora_mm_id(down_exps, cur, ids_gemm, ..., selected_experts)
          ↑ uses ids_gemm for GEMM, selected_experts for scale (ids_scale)
Step 5: if (mcache): experts = ggml_add(experts, device_chain(mc_slot_ids))
Output: experts  [n_embd, n_expert_used, n_tokens]
```

**Seam B — Shared context lock (in `llama_context`):**

Both caches hold device-resident slots. The streamer's eviction (SSD → device) and the
cache's eviction (VRAM → host) must be synchronized so a slot evicted by one is not
referenced by the other's in-flight graph. The adapter introduces a single
`std::mutex` in `llama_context` (or a unified `llama_moe_cache_manager` that owns both
sub-caches) that guards all cross-cache state transitions.

### 2.3 Interface Definitions

**Current graph layout (post-preview-v0.4.7 + moe_ffn_desc collapse):**

After GB-T01 lands, `build_moe_ffn` will take a `moe_ffn_desc` struct. The adapter
hooks through the desc:

```cpp
// In llama-graph.h (post-GB-T01)
struct moe_ffn_desc {
    // ... weights, biases, scales, routing config ...
    // Adapter additions (Phase 2):
    llama_moe_stream_layer * mstream   = nullptr;  // PR #25294 stream state for this layer
    llama_moe_cache_layer  * mcache    = nullptr;  // PR #27861 cache state for this layer
};
```

**Streamer interface (PR #25294, `llama-moe-stream.h`):**
```cpp
struct llama_moe_stream {
    uint32_t n_slots;
    int32_t  n_io_threads;
    std::vector<std::unique_ptr<llama_moe_stream_layer>> layers;
    // ... residency state, I/O workers, stats ...

    llama_moe_stream_layer * layer(int32_t il) const;
    ggml_tensor * create_cache_tensor(int32_t il, ggml_backend_buffer_type_t buft,
        const ggml_tensor * meta, uint16_t file_idx, size_t offs);
    void alloc_bufs(bool no_alloc);
    void open_files(const std::vector<std::string> & paths);
};

// Custom-op callbacks:
void llama_moe_stream_remap(ggml_tensor * dst, const ggml_tensor * a,
    int ith, int nth, void * userdata);
void llama_moe_stream_wave_ids(ggml_tensor * dst, int ith, int nth, void * userdata);
void llama_moe_stream_wave_mask(ggml_tensor * dst, int ith, int nth, void * userdata);
```

**Cache interface (PR #27861, `llama-moecache.h`):**
```cpp
struct llama_moe_cache_layer {
    int il = -1;
    int32_t n_slots = 0;
    ggml_tensor * up_src, * gate_src, * down_src;    // host-resident authoritative
    ggml_tensor * up_c, * gate_c, * down_c;          // device-resident cache [ne0,ne1,n_slots+1]
    ggml_tensor * dev_table, * host_table;           // I32 [1, n_expert] expert→slot
};

void llama_moe_cache_init(const llama_model & model, int32_t n_slots, int32_t max_inserts);
const llama_moe_cache_layer * llama_moe_cache_lookup(const ggml_tensor * up_exps);
void llama_moe_cache_step();
```

**Adapter bridge interface (new — the RECONCILE contract):**
```cpp
// The adapter owns the shared lock and the fetch-path ordering.
// Lives in llama_context (or a dedicated llama_moe_cache_manager).
struct llama_moe_reconcile {
    std::mutex mtx;  // shared context lock — guards cross-cache eviction

    // Called from build_moe_ffn after ggml_build_forward_expand(gf, weights):
    // 1. Runs stream remap (if stream active) → ids_gemm
    // 2. Runs cache lookup (if cache active) → mcache
    // 3. Returns the composed fetch-path state for the GEMM pipeline
    struct fetch_state {
        ggml_tensor * ids_gemm;      // remapped slot ids (or selected_experts if no stream)
        ggml_tensor * mc_slot_ids;   // device cache slot ids (or nullptr)
        llama_moe_cache_layer * mcache;
        llama_moe_stream_layer * msl;
    };

    fetch_state compose(ggml_tensor * selected_experts, const ggml_tensor * up_exps,
                        int il, int n_tokens, bool cache_guard_conditions);
};
```

---

## 3. Fetch-Path Contract

### 3.1 Decode Path (n_tokens == 1, single-pass)

When the cache guard conditions are met (`n_tokens == 1 && !gate_up_exps && gate_exps &&
down_exps && !*_b && !*_s && type_op == LLM_FFN_SILU && !weight_before_ffn && loras->empty()`):

```
selected_experts → [stream remap] → ids_gemm (cache slot ids)
                 → [cache lookup] → mcache + mc_slot_ids
                 → build_lora_mm_id(up_exps, cur, ids_gemm, up_exps_s, selected_experts)
                 → build_lora_mm_id(gate_exps, cur, ids_gemm, gate_exps_s, selected_experts)
                 → build_lora_mm_id(down_exps, cur, ids_gemm, down_exps_s, selected_experts)
                 → [cache device chain] → ggml_add(experts, down_g)
```

The stream remap runs first because it ensures the requested experts are resident in the
cache's device slots. The cache lookup then maps those slots to VRAM addresses. The two
outputs sum to the exact result (uncached ids contribute 0 through the cache chain's zero
slot; cached ids contribute 0 through the CPU chain's skip).

### 3.2 Prefill Path (n_tokens > 1, multi-pass waves)

When a ubatch touches more experts than the stream cache holds, PR #25294's wave splitting
kicks in. The adapter handles this by:

1. **Wave planning** (`plan_waves_locked`): splits distinct experts into groups of
   `stream_wave_cap = (n_slots - n_expert_used) / 2`.
2. **Per-wave execution**: each wave makes its expert slice resident, runs the GEMM, and
   masks out other waves' pairs.
3. **Cache interaction**: the cache device chain is **disabled** during multi-pass prefill
   (the cache guard requires `n_tokens == 1`). Only the stream path is active.

This is correct because the cache is a decode-time optimization (single-token, temporal
locality). The streamer handles both decode and prefill.

### 3.3 `build_lora_mm_id` Signature Reconciliation

PR #25294 adds `ids_scale` to `build_lora_mm_id`. The adapter's contract:

- **When both PRs active:** `ids = ids_gemm` (remapped slots), `ids_scale = selected_experts`
  (original ids for per-expert scale gather).
- **When only #27861 active:** `ids = selected_experts`, `ids_scale = nullptr` (original behavior).
- **When only #25294 active:** `ids = ids_gemm`, `ids_scale = selected_experts`.

The `ids_scale` parameter is the adapter's key signature-level contribution.

---

## 4. Shared Context Lock

### 4.1 Problem

PR #27861's `moe_cache` has `std::mutex mtx` (guards pending lists + clock).
PR #25294's `llama_moe_stream` has `mutable std::mutex mtx` (guards residency state + I/O queue).
These are independent. A graph could reference a slot that the other cache just evicted.

### 4.2 Solution

Introduce a single `std::mutex` in `llama_context` (or a unified cache manager) that guards
all cross-cache state transitions:

```cpp
// In llama_context (or llama_moe_reconcile)
std::mutex moe_cache_mtx;  // RECONCILE shared context lock

// All cross-cache operations acquire this lock:
// - llama_moe_cache_step() publishes completed uploads
// - llama_moe_stream_remap() reserves slots + enqueues loads
// - Eviction in either cache
```

The lock is held only for state transitions (microseconds), not for the duration of I/O or
GEMM. The existing per-cache mutexes remain for their internal state; the shared lock is
a higher-level ordering guarantee.

### 4.3 Hierarchical Lock-Ordering Invariant

The adapter enforces a **strict two-level lock hierarchy** to prevent deadlock between the
shared `moe_cache_mtx` and the per-cache mutexes (`llama_moe_cache::mtx`,
`llama_moe_cache::wmtx`, `llama_moe_stream::mtx`):

> **Invariant:** `moe_cache_mtx` is always acquired **before** any per-cache mutex.
> Per-cache mutexes are **never** acquired while holding `moe_cache_mtx` unless the nested
> order is explicitly documented below (the only permitted nesting is `moe_cache_mtx` →
> per-cache mutex, never the reverse).

**Concrete deadlock scenario this prevents:** `llama_moe_cache_step()` holds `mc->mtx`
during pending-list iteration (pr27861.diff:706+ — the `std::lock_guard<std::mutex>
lock(mc->mtx)` at line 706 guards the entire eviction/scheduling loop). If a streamer path
were to acquire `moe_cache_mtx` while holding its own `mgr->mtx`, the two paths could
deadlock: the cache path holds `mc->mtx` and waits for `moe_cache_mtx`; the streamer path
holds `mgr->mtx` (= `moe_cache_mtx` in the unified manager) and waits for `mc->mtx`. The
hierarchical invariant eliminates this by requiring `moe_cache_mtx` to be the **outer**
lock in any nested acquisition.

**Lock classification:**

| Lock | Held by | Duration | Role |
|------|---------|----------|------|
| `moe_cache_mtx` | Cross-cache eviction, slot reservation | **Brief** (microseconds — state transition only) | Outer lock in the hierarchy |
| `llama_moe_cache::mtx` | `llama_moe_cache_step()` pending-list iteration, clock advance | **Long** (covers the full eviction/scheduling loop — pr27861.diff:706+) | Inner lock; never acquired while holding `moe_cache_mtx` |
| `llama_moe_cache::wmtx` | Worker completion sync (publish done queue) | Brief (sync point) | Inner lock; acquired together with `mc->mtx` in `mc->mtx` → `mc->wmtx` order (pr27861.diff:693-694) |
| `llama_moe_stream::mtx` | Streamer residency, wave planning, slot reservation | **Long** (covers `stage_wave_locked`, `plan_waves_locked` — pr25294.diff:1453,1641) | Inner lock; never acquired while holding `moe_cache_mtx` |

**Eviction protocol (protecting mutex per table):**

1. Acquire `moe_cache_mtx` (outer lock — guarantees exclusive cross-cache access).
2. Acquire the evicting cache's per-cache mutex (inner lock — e.g., `mc->mtx` for the GPU
   LRU, or `mgr->mtx` for the streamer). This nesting (`moe_cache_mtx` → per-cache mutex) is
   the **only** permitted nested order.
3. Mark slot as evicted in both caches' tables (under both locks).
4. Publish the eviction (update `dev_table` + `host_table`).
5. Release per-cache mutex, then `moe_cache_mtx`.
6. Only then allow the next graph to reference the slot.

Each table (`dev_table`, `host_table`) is protected by its owning cache's per-cache mutex;
cross-cache reads of the other cache's table are serialized through `moe_cache_mtx`.

### 4.4 Ordering Guarantee

The shared lock ensures: **a slot evicted by one cache is never referenced by the other's
in-flight graph.** The eviction protocol (§4.3) guarantees that no graph can observe a
stale table entry: the eviction is published under both `moe_cache_mtx` and the per-cache
mutex, and any graph referencing the slot must acquire `moe_cache_mtx` first.

---

## 5. Swappable Prefetch Policy (DESIGN-GB Constraint)

### 5.1 Constraint

Per DESIGN-GB §"Swappable prefetch design": `layer_state` accumulates **raw counters**;
no prefetch policy logic lives in the cache manager. The prefetch policy is swappable.

### 5.2 Adapter Compliance

The RECONCILE adapter respects this constraint:

- **The cache (`llama-moecache.cpp`)** accumulates raw counters: `n_hit`, `n_miss`,
  `n_bytes_served[tier]`, `n_waste_bytes[tier]`, `n_evictions[tier]`, `n_eviction_reloads[tier]`.
  No prefetch policy decisions are made here.
- **The streamer (`llama-moe-stream.cpp`)** has an eviction policy (decaying route hotness
  + LRU tiebreak), but this is a **cache residency** policy, not a prefetch policy. The
  prefetch policy (which experts to load ahead of time) is a separate concern.
- **The adapter** defines the prefetch-policy seam: a function pointer or vtable that
  Phase 2's three-band prefetch (PR #25294's `stage_wave_locked` preload logic) plugs into.

```cpp
// Prefetch policy seam (the adapter's swappable-prefetch-policy contract)
using moe_prefetch_policy = std::function<void(
    llama_moe_stream_layer & sl,
    const std::vector<int32_t> & upcoming_expert_ids,
    std::vector<int32_t> & preload_slots
)>;

// Phase 2 plugs in the three-band policy:
// - Band 1: experts routed in the current ubatch (demand load)
// - Band 2: experts predicted by route-ahead prefetch
// - Band 3: experts from the LRU cache's hot set
```

### 5.3 How This Resolves the R04×R05 Conflict

R04 assumes three-band prefetch overlaps 4.4 GB; R05 says the full 6.6 GB is on the
critical path. The adapter resolves this by:

1. **Deferring the prefetch policy to Phase 2** (the seam is defined now; the policy plugs in later).
2. **Instrumenting from day one** (R07 counters in GB-T05 measure the actual prefetch hit fraction).
3. **Letting telemetry resolve the conflict empirically**: the `n_hit`/`n_miss` counters
   (from the cache's `layer_state`) and the `n_preload_ready` counter (from the streamer's
   separate stream-stats struct — **not** the cache's `layer_state`) measure whether prefetch
   is effective. If R05 is right (full 6.6 GB on critical path), the counters will show low
   prefetch hit fractions. If R04 is right, the counters will show high hit fractions with
   three-band prefetch.

**Counter-ownership clarification:** The streamer's counters (`n_preload_ready`,
`n_preload_issued`, `n_wave_calls`, etc.) live in a dedicated `stats` struct inside
`llama_moe_stream` (pr25294.diff:1496,1876), **not** in the cache's `layer_state`. The
adapter defines the seam between the two counter domains, but counter **unification** (a
single merged stats surface) is explicitly a **Phase-2 task** — the adapter does not merge
them. The `n_preload_ready` counter is streamer-only (it counts experts already resident
from the previous wave's preload — pr25294.diff:1496); it does **not** come from the cache.

---

## 6. Phase-2 Integration Order

The adapter is the prerequisite for Phase 2. Integration order:

| Step | Ticket | What | Hooks through adapter |
|------|--------|------|----------------------|
| 1 | GB-T01 | `moe_ffn_desc` collapse | The desc is the carrier for `mstream`/`mcache` pointers |
| 2 | GB-T02..T04 | Cache port (PR #27861) | Cache interface (§2.3) |
| 3 | GB-T05..T06 | Telemetry counters | Raw counters (§5.2) |
| 4 | **Phase 2a** | Stream port (PR #25294) — `llama-moe-stream.{h,cpp}` + `build_moe_ffn` remap | Streamer interface (§2.3) + fetch-path contract (§3) |
| 5 | **Phase 2b** | `build_lora_mm_id` `ids_scale` reconciliation | Signature reconciliation (§3.3) |
| 6 | **Phase 2c** | Shared context lock wiring | Lock protocol (§4) |
| 7 | **Phase 2d** | Three-band prefetch policy plug-in | Prefetch seam (§5.2) |
| 8 | **Phase 2e** | Multi-pass prefill wave integration | Prefill path (§3.2) |
| 9 | **Phase 2f** | KVarN D64 risk mitigation (see §7) | Orthogonal — verified non-conflicting |

---

## 7. KVarN D64 Risk Interaction

### 7.1 Context

KVarN D64 (R06) adds optimized 64-dimensional KV head support. The rebased
`llama-graph.cpp` already includes KVarN D64 changes (commit `e1f6d6fe6` in beellama-port).

### 7.2 Interaction with the Adapter

The cache's device-chain activation mirrors the existing `build_moe_ffn` pattern:
```cpp
if (arch == LLM_ARCH_DEEPSEEK4 || (arch == LLM_ARCH_DFLASH && hparams.dsv4_hc_mult > 0)) {
    cur = ggml_swiglu_clamp(ctx0, cur, up, limit);
}
```
This is the same pattern that exists in the rebased tree (line 2627). KVarN D64 affects
**KV attention** (`build_attn`, `build_attn_mha`, `build_qkv`), not the MoE FFN activation.

**Risk level: LOW** (confirmed by DESIGN-GB §6 risk register).

The cache path only activates for `type_op == LLM_FFN_SILU` with host-resident experts.
KVarN D64's changes are in the attention path (`build_attn_mha` now takes `n_kv_max`,
`build_qkv` has a `reshape` parameter, KVarN routing uses `uses_materialization_indices()`).
These are orthogonal to the MoE FFN fetch path the adapter bridges.

**Mitigation:** The adapter's fetch-path contract (§3) is activated by the cache guard
conditions, which are entirely within the MoE FFN path. As long as the guard conditions
remain correct (they reference `type_op`, `gate_exps`, `down_exps`, etc. — none of which
KVarN D64 touches), the adapter is unaffected.

---

## 8. Resolved Design Decisions

The following questions were carried as "open" in earlier drafts but are now answered by
the spec; they are collected here as binding decisions.

| # | Question | Decision | Spec ref |
|---|----------|----------|----------|
| D1 | **Multi-pass prefill + cache interaction:** Should the cache device chain be disabled during multi-pass prefill? | Yes — the cache guard requires `n_tokens == 1`. During prefill, only the stream path is active. The cache is a decode-time optimization. | §3.2 |
| D2 | **`ids_scale` default value:** When only #27861 is active, should `ids_scale` be `nullptr` or should the adapter always pass `selected_experts`? | `nullptr` when only #27861 is active (preserves original behavior). `selected_experts` when #25294 is active. The adapter's `compose()` function handles this. | §3.3 |

---

## 9. Open Questions

| # | Question | Resolution path |
|---|----------|-----------------|
| Q1 | **R04×R05 prefetch-effectiveness conflict:** Does three-band prefetch actually hide I/O latency, or is the full 6.6 GB on the critical path? | **Telemetry resolves it.** R07 counters (GB-T05) measure prefetch hit fraction from day one. Phase 2d plugs in the three-band policy; the counters tell us if it works. If `n_preload_ready / n_miss` is high → R04 is right. If low → R05 is right. |
| Q2 | **Shared context lock granularity:** Should the lock be per-context or per-layer? | Per-context is simpler and sufficient for Phase 1 (single-GPU). Per-layer is a Phase 2 optimization for multi-GPU. Start per-context. |
| Q3 | **SSD tier (tier[2]) activation:** When does the SSD tier start accumulating non-zero counters? | Phase 2, when the streamer is ported. Until then, `n_bytes_served[SSD]` = 0 (reserved). |
| Q4 | **Concurrent multi-context streaming:** PR #25294's header notes "multiple contexts decoding the same streamed model concurrently are not supported." Does the adapter change this? | No. The adapter does not remove this limitation. Multi-context support is a Phase 2 deepening (requires per-context cache isolation). |
| Q5 | **Wave cap formula stability:** `stream_wave_cap = (n_slots - n_expert_used) / 2` — does this hold when the cache also consumes slots? | The cache and streamer use **separate** slot pools (cache: `n_moe_cache_slots`; streamer: `moe_stream_slots`). The wave cap formula uses the streamer's slot count, so it is independent of the cache. |
| Q6 | **KVarN D64 activation divergence:** If KVarN D64 changes the DFlash activation semantics, could the cache's mirrored activation diverge? | LOW risk. The cache mirrors the existing pattern inline. If KVarN D64 changes the pattern, the cache's mirror must be updated in the same change. The adapter does not add new coupling. |

---

## 10. Verification

| Criterion | Evidence |
|-----------|----------|
| Diff-span citations for each conflict claim | §1 (all 7 shared files, verbatim spans from pr27861.diff / pr25294.diff) |
| Current (rebased, post-preview-v0.4.7) `llama-graph.cpp` layout referenced | §2.3, §3 (anchors verified against `beellama-port` lines 2554, 2627, 2703) |
| 7 shared files named with per-file conflict semantics | §1.1–1.7 |
| R03's sketch elaborated to Phase-2-implementable spec | §2 (architecture), §3 (fetch-path), §4 (lock-ordering invariant), §5 (prefetch seam) |
| Swappable-prefetch-policy constraint respected | §5 (raw counters, no policy in cache) |
| Phase-2 integration order | §6 |
| Resolved design decisions (multi-pass prefill, ids_scale default) | §8 |
| Open questions (incl. R04×R05 + telemetry resolution) | §9 |
| KVarN D64 risk interaction | §7 |
| Hierarchical lock-ordering invariant specified | §4.3 |
