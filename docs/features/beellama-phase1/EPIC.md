# EPIC — Beellama Phase-1: Rebase, Pin, Cache Port, Telemetry, Adapter Design

> **Epic ID:** beellama-phase1
> **Status:** DRAFT
> **Source of truth:** `PORT-DESIGN-RECOMMENDATION.md` (aggregated verdicts R01–R08)
> **Port repo:** `/home/<user>/projects/beellama-port` (branch `feature/three-tier-expert-cache`)
> **Engine repo:** `/home/<user>/projects/ace-engine` (branch `main`)
> **Date:** 2026-09-11

---

## 1. Problem

The beellama-port `feature/three-tier-expert-cache` branch is built on a base that has drifted **220 commits** behind `origin/main` (confirmed: `upstream-drift.log` has exactly 220 entries; `git rev-list --count` from merge-base to `origin/main` = 220). The two upstream MoE PRs the port depends on — **PR #27861** (GPU-resident LRU cache) and **PR #25294** (O_DIRECT SSD streaming) — are both still OPEN on `main`, actively reviewed, and subject to force-push. `llama-graph.cpp`, the primary conflict surface, has **310 changed lines of drift** (layout refactors, not core MoE dispatch) and the port's patches were diffed against the *old* graph layout.

Three concrete failures follow from this drift if left unaddressed:

1. **Integration rot:** every local patch against the old `llama-graph.cpp` layout degrades silently as upstream refactors the graph-construction code. The port must retarget onto a known tag before the patches stale.
2. **Build instability from unpinned PRs:** neither PR #27861 nor #25294 is merged; a force-push or design-altering review comment silently invalidates the port's assumptions. There is no canary detecting this.
3. **No empirical grounding for the central design risk:** the R04/R05 conflict (does three-band prefetch hide SSD latency, or is the full 6.6 GB on the critical path?) is unresolved because the cache ships without telemetry. Hit rates and waste bytes are unmeasured.

The port cannot proceed to Phase 2 (SSD streaming, prefetch, benchmark) until the base is rebased, the PRs are pinned with a canary, and the GPU cache ships with live telemetry counters.

---

## 2. Scope (Phase-1 seed items 1–5)

Phase-1 covers the first five items of the recommendation's seed, traced to verdicts. Items 1–4 are implementable; item 5 is **design-only** (a deliverable verdict, no code).

| # | Work Item | Traced To | Grain | TARGET-REPO |
|---|-----------|-----------|-------|-------------|
| **1** | Rebase `feature/three-tier-expert-cache` onto upstream tag `preview-v0.4.7` (`53a68d3c3`). Resolve the 220-commit drift; retarget all local patches to the new `llama-graph.cpp` layout (357 changed lines). | R01, R06, HW-MEASUREMENTS appendix-2 | M | **beellama-port** |
| **2** | Snapshot-pin both PRs — freeze `pr27861.diff` and `pr25294.diff` as local quilt patches; implement the CI canary verifying `hash(applied patch) == hash(snapshotted diff)` before compile. | R08 | S | **beellama-port** |
| **3** | Port PR #27861's GPU↔RAM LRU cache onto the rebased base. Resolve conflicts in the 7 shared files; integrate `llama-moecache.cpp` + `.h`; verify KVarN paths untouched. | R02, R03, R07 | L | **beellama-port** |
| **4** | Implement R07 telemetry counters — extend `llama_moe_stream_print_stats` with per-tier hit rate, bytes served, waste bytes, eviction counts. Attach at the named functions. Reconcile with the Lidenburg/phasebuffer per-tier event model (see §7). | R07 + prior art (§7) | S | **beellama-port** |
| **5** | **Design-only** — produce the RECONCILE adapter detailed design doc from R03's sketch. Define the I/O multiplexer that routes cold experts via O_DIRECT into the GPU LRU cache, with synchronized eviction via a shared context lock. | R03 | M | **ace-engine** (design doc deliverable) |

Items 1 and 2 are the critical path and should start immediately. Item 1 is the prerequisite for everything. Item 2 is cheap insurance against build instability. Item 3 (GPU cache) is the highest-leverage implementation because RAM→VRAM bandwidth (20–40× SSD) makes the GPU cache the tier that matters most for the DFlash verify-round working set. Item 4 ships alongside item 3 so the cache has instrumentation from day one. Item 5 is the integration contract required before Phase 2.

---

## 3. Non-Goals (deferred to Phase-2)

The following seed items are explicitly **out of scope** for Phase-1 and will be addressed in a later epic:

| # | Deferred Work Item | Traced To | Reason |
|---|-------------------|-----------|--------|
| **6** | Port PR #25294 (RAM↔SSD streaming) — O_DIRECT async I/O pool, Wave-Partitional Prefill, expert store on L800 partition. | R04, R05 | Requires the RECONCILE adapter (item 5) as its integration contract. |
| **7** | Three-band prefetch + eviction implementation — route-ahead prefetch across expert gates; LRU eviction synchronized with SSD prefetch. | R04, R05 | Highest-risk item; the R04/R05 conflict means prefetch effectiveness is unproven on this hardware. Deferred until telemetry from items 3+4 resolves it empirically. |
| **8** | Fork-contingency threshold test — verify the 400-commit / MoE-dispatch-divergence triggers are measurable. | R02 | Guardrail; do once after item 1 settles. |
| **9** | Multi-GPU staging through host RAM — no P2P, so expert traffic between 3090↔3070 stages through host RAM. | HW-MEASUREMENTS | Needed for the 2-GPU host box; deferred. |
| **10** | Benchmark + tune — measure tok/s, tier hit rates, DFlash break-even; tune cache sizes, eviction params, I/O thread count. | R04, R05, R07 | Validation gate that resolves R04/R05 with data; deferred until items 3+4+6+7 land. |

---

## 4. Sources

The following documents are authoritative for this epic, in order of precedence:

1. **`docs/features/beellama-tiered-memory/PORT-DESIGN-RECOMMENDATION.md`** — aggregated verdicts R01–R08, the executive recommendation, the 10-item seed, and the verdict-quality notes (R04/R05 prefetch conflict = design risk; R06's 142-commit claim corrected to 220).
2. **`.scratch/beellama-dogfood/sources/`** — primary sources the verdicts cite:
   - `upstream-drift.log` (220 entries — the authoritative drift count)
   - `upstream-drift-stat.txt` (`src/llama-graph.cpp | 310 +-`)
   - `llama-graph-drift.diff` (the 310-line drift diff)
   - `pr27861.diff`, `pr25294.diff` (the snapshotted PR diffs)
   - `pr27861-files.json`, `pr25294-files.json` (the 7 shared files, exactly verified)
   - `HW-MEASUREMENTS-host.md` (bandwidths, no P2P, DRAM-less NVMe)
   - `dflash-port-notes.md` (DFlash break-even, 6.6 GB working set)
3. **`/home/<user>/projects/beellama-port/campaign/PRD.md`** — the existing port plan this epic aligns with (Phase 1 state machine, gates, agent roster).
4. **`/home/<user>/projects/beellama-port/campaign/issues/phase-1-issues.md`** — the existing 10-issue decomposition (P1-T1…P1-T10) that the new tickets supersede on the rebased base.
5. **Telemetry prior-art sources** (adapted into the Phase-1 telemetry model — see §7):
   - `/home/<user>/projects/phasebuffer/docs/design/telemetry-interface-system.md` — engine-agnostic zero-overhead telemetry: SPSC ring buffer, runtime gate ≤2%/≈0%, event variants, JSON/binary export.
   - `/home/<user>/projects/phasebuffer/docs/lidenburg-telemetry-concept.md` — VRAM↔RAM↔SSD tiered expert cache telemetry: L1 LFU-aging, L2 pinned RAM, L3 io_uring O_DIRECT (same architecture as this port).
   - `/home/<user>/projects/phasebuffer/docs/prd/telemetry-deepening.md` — expert heatmap, routing histograms, latency waterfall, request_id propagation.
   - `/home/<user>/projects/phasebuffer/.scratch/issues/PULSAR-OPS-00-telemetry-event-stream-endpoints.md` — remote consumption: SSE event stream endpoints.
   - `/home/<user>/projects/phasebuffer/.scratch/issues/PULSAR-OPS-01-web-ui-live-telemetry-trace-timeline.md` — live web UI timeline (deferred to Phase-2).

---

## 5. Success Criteria

Phase-1 is complete when **all** of the following hold:

1. **Branch rebased clean.** `feature/three-tier-expert-cache` is rebased onto `preview-v0.4.7` (`53a68d3c3`). `git rev-list --count` from the new base to branch tip equals the local patch count (no orphaned upstream commits). The 7 shared files carry retargeted patches against the new `llama-graph.cpp` layout. Trial merge-tree of the rebased branch against `origin/main` is assessed and documented.
2. **Canary green in CI.** The CI canary runs on every push and verifies `hash(applied quilt patch) == hash(snapshotted diff)` for both PR #27861 and PR #25294 before compile. A deliberate force-push to either diff fails the canary (tested once). Build succeeds with `-DGGML_CUDA=ON`.
3. **Cache ported with telemetry live.** PR #27861's LRU cache is ported onto the rebased base. `llama-server --help` shows `--moe-expert-cache`. The server starts with `--moe-expert-cache 64 --cpu-moe -fa on` on a MoE model. KVarN paths are verified untouched (`grep` for KVarN/DFlash symbols in modified files returns only expected hits).
4. **Hit rates measurable + remotely readable.** The R07 telemetry counters are live in `llama_moe_stream_print_stats`: per-tier hit rate, bytes served per tier, waste bytes, eviction counts. Running the cache with `--moe-expert-cache` produces non-zero `hit_rate[VRAM]` and `bytes_served[VRAM]` in the stats output. **Additionally**, the telemetry is remotely consumable: a structured event-stream endpoint (SSE or JSON-lines, adapted from PULSAR-OPS-00) exposes the per-tier counters to a remote dashboard in a standard format (JSON). This is the instrumentation that will resolve the R04/R05 conflict empirically in Phase 2.
5. **RECONCILE adapter design delivered.** A detailed design doc (from R03's sketch) defines the I/O multiplexer, the shared context lock for synchronized eviction, and the fetch-path contract between O_DIRECT streaming and the GPU LRU cache. Reviewed and approved as the integration contract for Phase 2.

---

## 6. Verdict-Quality Caveats (carried from the recommendation)

These open risks are inherited from the source verdicts and constrain the work:

- **R04/R05 prefetch conflict is the central design risk.** The prefetch-effectiveness assumption is unmeasured. Phase-1 resolves it by shipping telemetry (item 4), not by betting on prefetch. Phase 2's streaming design must be gated on the measured hit fraction.
- **R06's 142-commit claim is wrong; the drift is 220.** All rebase scoping uses 220. The rebase itself is low-risk (0 conflicts on the campaign docs commits), but the port code must target the new graph layout.
- **R03's RECONCILE adapter is a sketch, not a spec.** Item 5 elaborates it to a detailed design; it is design-only in Phase-1.
- **DRAM-less NVMe degradation** under concurrent mixed read/write is unaddressed by any verdict. The expert store should be on its own L800 partition, not shared with engine writes. Respected in Phase-2 item 6.

---

## 7. Telemetry Prior-Art Adaptation

This section documents how Phase-1 adapts the existing phasebuffer/Lidenburg telemetry architecture rather than reinventing it. The R07 verdict's six counter groups are reconciled with the prior-art per-tier event model; the ring-buffer capture discipline and export format are adopted; and the remote-consumption interface commitment is defined.

### 7.1 What Phase-1 Adopts from phasebuffer/Lidenburg

| Prior-art concept | Source doc | Phase-1 adaptation |
|---|---|---|
| **Per-tier event model** (VRAM / RAM / SSD tier-tagged events) | `lidenburg-telemetry-concept.md` — the three-tier cache (L1 GPU LFU-aging, L2 pinned RAM, L3 io_uring O_DIRECT) is the *same architecture* as this port | Adopted directly. R07's counters are expressed as per-tier events so each hit/miss/eviction carries its tier tag. |
| **Ring-buffer capture discipline** (lock-free SPSC, bounded, overwrite-oldest, ~10ns/event hot path) | `telemetry-interface-system.md` §4 | Adopted as the capture model. The existing `llama_moe_stream_print_stats` surface is extended to write tier-tagged events into a small ring buffer (default 64K slots, ~4 MiB) before promotion to the stats thread. This preserves the ≤2% overhead budget. |
| **Runtime gate** (AtomicBool, relaxed load ≈1ns when disabled) | `telemetry-interface-system.md` §5.3 | Adopted. Telemetry defaults off; a CLI flag (`--moe-expert-cache-telemetry`) or env var enables it. |
| **JSON export format** (structured, versioned, machine-readable) | `telemetry-interface-system.md` §9.1 | Adopted. The event-stream endpoint and stats dump emit JSON. Binary capture (high-frequency) is deferred to Phase-2. |
| **Per-layer hit/miss counters** (`layer_stats[]` — Gap 1 in lidenburg doc) | `lidenburg-telemetry-concept.md` §Gap 1 | Adopted. R07's `hit_rate[tier]` is tracked per-layer so the stats thread can identify thrashing layers. |
| **PCIe bandwidth monitoring** (`g_pcie_transfer_ns/bytes` — Gap 2) | `lidenburg-telemetry-concept.md` §Gap 2 | Adopted as `bytes_served[RAM→VRAM]` timing. The RAM→GPU copy is the critical path for L2→L1 promotion; its duration is captured. |
| **Eviction telemetry** (`g_evictions`, `g_eviction_reloads` — Gap 4) | `lidenburg-telemetry-concept.md` §Gap 4 | Adopted as `eviction_count[tier]` plus a reload-after-eviction counter to measure wasted evictions. |
| **Expert event ring buffer** (per-expert load/offload timeline — Gap 3) | `lidenburg-telemetry-concept.md` §Gap 3 | Adopted in ring-buffer form: each tier transition (RAM→GPU, SSD→RAM, GPU→RAM) emits a timestamped event. |
| **JSON-lines export from stats thread** (Gap 6, Option A) | `lidenburg-telemetry-concept.md` §Gap 6 | Adopted as the remote-consumption interface (see §7.3). |

### 7.2 R07 Counters × Lidenburg Events: Reconciliation

R07 defines six counter groups. Each is mapped to the prior-art event model:

| R07 counter | Lidenburg event equivalent | Verdict | Notes |
|---|---|---|---|
| `hit_rate[tier]` | `g_cache_hits` / `g_ram_hits` / `g_disk_misses` per-layer breakdown (Gap 1) | **Adopted, renamed** | R07's "hit rate" = Lidenburg's per-tier hit/miss counters normalized by token count. Per-layer granularity adopted from Gap 1. |
| `bytes_served[tier]` | `g_pcie_transfer_bytes` (Gap 2) + per-tier byte accounting | **Adopted, extended** | Lidenburg only tracked PCIe bytes; Phase-1 adds per-tier byte accounting (VRAM-served, RAM-served, SSD-served). |
| `waste_bytes[tier]` | `g_eviction_reloads` (Gap 4) + prefetch lead-time (Gap 7) | **Adopted, extended** | R07's "waste bytes" = bytes prefetched but evicted/unused. Lidenburg's Gap 4 + Gap 7 provide the measurement hooks. |
| `eviction_count[tier]` | `g_evictions` (Gap 4) | **Adopted, renamed** | Direct 1:1 mapping. Lidenburg's `g_evictions` is per-GPU; Phase-1 extends to per-tier. |
| *(implicit: PCIe timing)* | `g_pcie_transfer_ns` (Gap 2) | **Adopted** | Not explicitly in R07 but required by the Lidenburg model. Captured as `bytes_served[RAM→VRAM]` duration. |
| *(implicit: per-layer breakdown)* | `layer_stats[]` (Gap 1) | **Adopted** | Not explicitly in R07 but required to identify thrashing layers. |

**Dropped / deferred:** Lidenburg's io_uring deep telemetry (Gap 5 — `g_io_uring_ops/ns/max`) and time-series sampling (Gap 8 — 100ms snapshots) are deferred to Phase-2 because they require the SSD tier (item 6) to be meaningful. The N+1 prefetch accuracy counter (`g_n1_prefetch_hits`, Gap 7) is deferred for the same reason.

### 7.3 Remote-Consumption Interface Commitment

Adapted from PULSAR-OPS-00 (`/telemetry/stream` SSE endpoint). Phase-1 commits to:

- **A structured event-stream endpoint** on llama-server that exposes the per-tier telemetry counters to remote consumers. The endpoint emits JSON-lines (one object per tick) containing the reconciled R07 counters (hit_rate, bytes_served, waste_bytes, eviction_count) tagged by tier and layer.
- **Format:** JSON-lines (not SSE for Phase-1 — simpler to implement on the C++ side, no framing overhead). Each line is a self-describing snapshot: `{"ts":<ns>,"tier":"VRAM","layer":15,"hit_rate":0.75,"bytes_served":131072,"waste_bytes":0,"evictions":3}`.
- **Endpoint:** `GET /moe-cache/telemetry` returns the current snapshot as a JSON array; `GET /moe-cache/telemetry/stream` (Server-Sent Events) pushes live updates. Both return 404 when telemetry is disabled.
- **Rationale:** This makes telemetry *remotely consumable* — a dashboard on a separate machine can read the event stream without ssh/stdout parsing. This is the C++-side commitment adapted from PULSAR-OPS-00's SSE design.

### 7.4 What Stays in Phase-2

The following prior-art capabilities are explicitly deferred:

| Capability | Source | Reason for deferral |
|---|---|---|
| **Live web UI timeline** (Trace Timeline tab, waterfall visualization) | PULSAR-OPS-01 | Requires a stable event stream + browser-side rendering. Deferred until the Phase-1 endpoint is live and the SSD tier adds enough event variety to justify a timeline. |
| **Expert heatmap** (per-(layer, expert) frequency counts) | `telemetry-deepening.md` Phase A | Requires the MoE routing event stream at scale. Deferred until Phase-2 when the full three-tier cache is active. |
| **Latency waterfall** (pair Start/End events by token_id) | `telemetry-deepening.md` Phase A | Requires request_id propagation (Phase B of the deepening PRD). Deferred. |
| **Binary capture format** (high-frequency disk export) | `telemetry-interface-system.md` §9.2 | JSON-lines suffices for Phase-1 event rates. Binary format deferred until event rates justify it. |
| **io_uring deep telemetry** (per-op timing, queue depth) | `lidenburg-telemetry-concept.md` §Gap 5 | Requires the SSD tier (Phase-2 item 6). |
| **Time-series sampling** (100ms snapshots → CSV) | `lidenburg-telemetry-concept.md` §Gap 8 | Deferred; the JSON-lines stream can be sampled by the consumer. |
