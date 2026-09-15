# Beellama Tiered-Memory Port — Aggregated Design Recommendation

> **Source:** Aggregation of 8 research verdicts (R01–R08) from the `beellama-dogfood` campaign.
> **Workspace:** `/home/<user>/projects/beellama-dogfood-ws/verdicts/` (branch `feature/beellama-dogfood-run1`, read-only).
> **Context:** HW-MEASUREMENTS-host.md, beellama-port PRD.md + CONCEPT.md, PR diffs in `.scratch/beellama-dogfood/sources/`.
> **Date:** 2026-09-10.

---

## 1. Executive Recommendation

Port the two upstream MoE PRs onto **beellama.cpp tracking tag `preview-v0.4.7`** (commit `53a68d3c3f36c3467fdb7431cd72473a45c03e86`), carrying local changes as a **quilt-style patch series** with a deterministic reapplication procedure (extract → rebase per-patch → resolve layout conflicts in `llama-graph.cpp` → verify MoE path stability), and a fork contingency triggered only if drift exceeds 400 commits or the MoE dispatch logic itself diverges. The port targets a **three-tier memory layout**: **Tier 1 = VRAM** (24 GB on RTX 3090 / 8 GB on RTX 3070) holding active KV cache and current-expert weights; **Tier 2 = host RAM** (64 GB) for DFlash staging and inactive expert blocks; **Tier 3 = SSD** with the expert store on the **ADATA L800 NVMe** (1.6 GB/s sequential, 1.2 TB free) and the Kingston NV2 (673 MB/s) reserved for logs and cold snapshots. Eviction at the GPU tier is **LRU** (per PR #27861's `llama_moe_cache_step`); the SSD tier uses **route-ahead three-band prefetch** to overlap transfer with the verify window. Telemetry extends the existing `llama_moe_stream_print_stats` surface with per-tier hit rate, bytes served, waste bytes, and eviction counts. Upstream drift is managed by **snapshot-committing** both open PRs (pin the fetched diffs, reapply on force-push / design-altering review / final merge) and isolating port patches from the measured 220-commit upstream drift via a CI canary that verifies applied-patch-hash == snapshotted-diff-hash before each build.

---

## 2. Decision Table

| Verdict | Position | Confidence | Load-Bearing Claim(s) | Citation |
|---------|----------|------------|----------------------|----------|
| **R01** | **Supported** — track `preview-v0.4.7` tag | 0.85 | Local repo only has tags up to v0.4.6; upstream main has drifted 220 commits past merge-base; `llama-graph.cpp` shows 310 changed lines of drift; PRs #27861 and #25294 remain open on main. Tracking `preview-v0.4.7` captures latest expert-path work with a stable baseline. | [src-1] upstream-drift.log; [src-2] upstream-drift-stat.txt; [src-3] llama-graph-drift.diff |
| **R02** | **Supported** — quilt-style patch series | 0.88 | 220-commit drift + 310-line `llama-graph.cpp` diff indicate frequent but localized structural changes; quilt preserves linear history, enables automated rebasing, isolates local changes. Fork contingency at >400 commits or MoE dispatch divergence. | [src-1] upstream-drift-stat.txt; [src-2] llama-graph-drift.diff |
| **R03** | **Supported** — reconcile adapter for the two PRs | 0.87 | Both PRs modify `llama-graph.cpp` + `build_moe_ffn`; PR #27861 = GPU-resident LRU cache, PR #25294 = O_DIRECT SSD streaming; 7 shared files with conflicting hunks; a RECONCILE adapter (I/O multiplexer: cold experts via O_DIRECT → GPU LRU cache for hits, eviction synchronized via shared context lock) resolves the fetch-path conflict. | [src-1] pr27861.diff; [src-2] pr25294.diff; [src-3] pr27861-files.json; [src-4] pr25294-files.json; [src-5] llama-graph-drift.diff |
| **R04** | **Supported** — L800 hosts expert store, three-band prefetch | 0.88 | L800 (1.6 GB/s) hosts expert store, NV2 (673 MB/s) for auxiliary; no GPU P2P (3090 Gen4 ↔ 3070 Gen3 both via PHB); DFlash verify-round working set ≈ 6.6 GB; three-band prefetch exploits temporal locality across expert gates; 6.6 GB / 1.6 GB/s = 4.125 s raw, reduced to ~1.375 s with 2.2 GB on-demand remaining. | [src-1] HW-MEASUREMENTS-host.md; [src-2] dflash-port-notes.md |
| **R05** | **Refuted** — SSD streaming does NOT shift DFlash break-even | 0.85 | 6.6 GB / 1.6 GB/s = 4.125 s, 6.6 GB / 673 MB/s = 9.81 s — both exceed the ~0.3–0.5 s per-round compute budget implied by the 7-commits/round break-even threshold; route-ahead prefetch cannot hide I/O latency behind the draft window. | [src-1] dflash-port-notes.md; [src-2] pr25294.diff; [src-3] HW-MEASUREMENTS-host.md |
| **R06** | **Supported** — rebase to align with latest upstream | 0.88 | Upstream has drifted significantly; rebase onto latest tag resolves integration conflicts. *(Note: R06 cites "142 new commits" — see §5.)* | [src-1] upstream-drift.log; [src-2] dependency-changes.txt |
| **R07** | **Supported** — extend `llama_moe_stream_print_stats` | 0.90 | Existing surface already prints base cache hit/miss + total bytes; add per-tier hit rate (hits/token), bytes served per tier, waste bytes (prefetched but evicted/unused), eviction counts per tier; hooks attach via existing `cache_stats` struct fields. | [src-1] pr27861.diff; [src-2] pr25294.diff |
| **R08** | **Supported** — snapshot-commit policy for both PRs | 0.89 | Pin fetched diffs as-is; reapplication triggers = upstream force-push, design-altering review comments, final merge; CI canary verifies applied-patch-hash == snapshotted-diff-hash before compile; 220-commit drift justifies snapshot isolation. | [src-1] pr27861.diff; [src-2] pr25294.diff; [src-3] upstream-drift.log |

---

## 3. The Port Design in Full

### 3a. Tracking & Reapplication Procedure (from R01 + R02 + R08)

**Base tracking decision.** The port tracks upstream tag **`preview-v0.4.7`** (`53a68d3c3f36c3467fdb7431cd72473a45c03e86`), not the stale v0.4.6 release or the volatile main branch. Rationale: the local repo only knew tags up to v0.4.6 (R01 claim-1), while main has drifted 220 commits past the merge-base (R01 claim-2; confirmed: `upstream-drift.log` has exactly 220 lines; HW-MEASUREMENTS-host.md appendix confirms "220 commits behind origin/main"). `preview-v0.4.7` captures the latest expert-path work (KVarN D64, DFlash KVarN caches) while providing a reproducible baseline.

**Mechanism: quilt-style patch series** (R02). Local changes are carried as a sequential patch series, not a self-contained module (which would fragment the codebase) and not a long-lived fork (unnecessary at current drift levels). The reapplication procedure on each upstream bump:

1. Extract the quilt series.
2. Rebase each patch against the new upstream tag.
3. Resolve layout-refactor conflicts in `llama-graph.cpp` (the primary conflict surface — 310 changed lines of drift, concentrated in layout refactors rather than core MoE expert-GEMM dispatch logic; R02 claim-2).
4. Verify MoE expert-path stability before merging.

**Fork contingency** (R02 claim-5): if drift exceeds **400 commits** or the MoE dispatch logic itself diverges, shift to a long-lived fork with periodic upstream cherry-picks and a strict ABI compatibility layer.

**Snapshot-commit policy for open PRs** (R08). Both PRs (#27861, #25294) are already snapshotted locally and must be pinned as fetched diffs, not tracked continuously. Reapplication triggers:
- Upstream force-push to the PR branch.
- Review comments indicating design changes.
- Final upstream merge.

A CI canary detects policy drift by verifying `hash(applied patch) == hash(snapshotted diff)` before compilation (R08 claim-3). The 220-commit drift rate justifies this isolation (R08 claim-4).

### 3b. Reconciliation Adapter vs. the Two Upstream PRs (from R03 + R06)

**Structural overlap.** The two PRs share exactly **7 files**: `common/arg.cpp`, `common/common.cpp`, `common/common.h`, `include/llama.h`, `src/CMakeLists.txt`, `src/llama-context.cpp`, `src/llama-graph.cpp` (verified against `pr27861-files.json` and `pr25294-files.json`; R03 claim-3 is accurate). The critical integration point is `build_moe_ffn` in `llama-graph.cpp`, where both PRs inject logic.

**Complementary memory hierarchies** (R03 claim-2):
- **PR #27861** (GPU-resident LRU cache): per-layer companion tensors `up_c`/`gate_c`/`down_c` in VRAM; I32 expert-id→slot table; cached experts served by a parallel device-side `mul_mat_id` chain, uncached experts served by the CPU chain; LRU eviction via `llama_moe_cache_step()` at end of `decode()`. CLI: `--moe-expert-cache N`.
- **PR #25294** (O_DIRECT SSD streaming): async I/O worker pool reading expert weights directly from GGUF (bypassing page cache); CPU id-remap custom op; Wave-Partitional Prefill. CLI: `--moe-stream-cache <N>G`.

**The RECONCILE adapter** (R03 claim-4): an I/O multiplexer that routes **cold expert weights through O_DIRECT SSD streaming** and populates the **GPU-resident LRU cache for subsequent hits**, with eviction policies synchronized via a shared context lock. The adapter abstracts the weight fetch path so O_DIRECT streaming feeds directly into the GPU LRU cache without duplicating I/O threads or violating the existing graph layout.

**Rebase prerequisite** (R06): the upstream repo has drifted significantly from the local merge-base. A rebase onto the latest upstream tag is required before the port can proceed. HW-MEASUREMENTS-host.md's rebase-surface assessment confirms this is **low-risk**: trial merge-tree of the 4 campaign commits (docs/campaign scaffolding, no code) against `origin/v0.4.7` produced **0 conflicts**. However, the port-design work must target the **new graph-construction layout** (357 changed lines in `llama-graph.cpp`), not the one the PRs were diffed against.

> **⚠ Open question (R06 internal inconsistency):** R06 claim-1 states "142 new commits since the local merge-base," but R01, R02, R08, and HW-MEASUREMENTS-host.md all state **220 commits**. The `upstream-drift.log` contains exactly 220 entries. The 142-commit figure appears to be an error (possibly counting only a subset of branches or a stale fetch). The rebase scope is 220 commits, not 142.

### 3c. Tier Sizes + Eviction + Prefetch with Bandwidth Arithmetic (R04 + R05)

**Tier layout:**

| Tier | Capacity | Role | Hardware |
|------|----------|------|----------|
| VRAM (L1) | 24 GB (3090) / 8 GB (3070) | Active KV cache + current expert weights | RTX 3090 (Gen4) / RTX 3070 (Gen3) |
| RAM (L2) | 64 GB | DFlash staging + inactive expert blocks | Host DDR |
| SSD (L3, hot) | 2 TB (L800, 1.2 TB free) | Expert store | ADATA LEGEND 800 NVMe @ **1.6 GB/s** seq |
| SSD (L3, cold) | 1 TB (NV2, 390 GB free) | Logs + cold snapshots | Kingston SNV2 NV2 @ **673 MB/s** seq |

**Key hardware constraints** (from HW-MEASUREMENTS-host.md):
- **No GPU P2P**: GPU0↔GPU1 both via PHB (CPU PCIe host bridge) — all expert traffic between devices goes through host RAM. Multi-GPU ResidentStore routing must stage through host RAM.
- **DRAM-less NVMe**: both drives lack on-drive DRAM cache → sustained mixed read/write performance degrades below sequential numbers under concurrent engine writes. The expert store should get its own partition or the L800 drive (faster + more free space).
- **RAM→VRAM over PCIe Gen4** is ~20–40× the SSD ceiling — the L2 (host RAM) tier absorbs bursts; sizing RAM cache to the DFlash round working set is the high-leverage knob.

**Verified bandwidth arithmetic:**

| Calculation | Result | Source |
|-------------|--------|--------|
| 6.6 GB / 1.6 GB/s (L800 peak) | **4.125 s** | R04 claim-1, R05 claim-4 ✓ |
| 6.6 GB / 673 MB/s (NV2 sustained) | **9.81 s** | R05 claim-4 ✓ |
| Per-round compute budget (implied by 7-commits/round break-even) | **~0.3–0.5 s** | R05 claim-4 |

**Eviction policy.** GPU tier uses **LRU** (per PR #27861): `llama_moe_cache_step()` evicts the least-recently-used slot (tracked via a lamport clock) and schedules throttled uploads (at most `n_moe_cache_inserts` per layer per step). R04 selects **three-band prefetch** over LRU/ARC for the SSD tier, arguing it exploits temporal locality across expert gates.

> **⚠ CONFLICT — R04 vs R05 (the central open question of this port design):**
>
> **R04 (supported, conf 0.88)** claims three-band prefetch reduces effective stall to **~1.375 s** (2.2 GB on-demand / 1.6 GB/s), which it asserts is "well within the verify-round budget." The internal arithmetic is consistent: 2.2 / 1.6 = 1.375.
>
> **R05 (refuted, conf 0.85)** claims the full 6.6 GB must be streamed at 4.125 s (L800) or 9.81 s (NV2), which **exceeds the ~0.3–0.5 s per-round compute budget** by 8–20×, so route-ahead prefetch cannot hide I/O latency and SSD streaming does not shift the DFlash break-even.
>
> **The conflict reduces to a single question: how much of the 6.6 GB working set can be prefetched out of the critical path?** R04 assumes 4.4 GB (67%) is overlapped, leaving 2.2 GB on-demand. R05 assumes the full 6.6 GB is on the critical path. Neither verdict provides direct measurement of the prefetch hit fraction on this hardware with this model. The dflash-port-notes.md (post-tier-pass measurements) show that a tier-aware lean resolve achieved **~98% of expert slots tier-served** on a 4060 Ti with 12.2 GB resident — suggesting that with sufficient prefetch, the on-demand fraction can be small. However, that measurement was on a single-GPU box with different tier ratios.
>
> **Resolution path:** The port should implement the three-band prefetch (R04) but instrument it with the R07 telemetry counters from day one, so the first implementation epic measures the actual prefetch hit fraction and wasted-prefetch bytes on the host box. If the on-demand fraction exceeds ~0.5 GB (→ <0.3 s), R04's optimism is validated; if it exceeds ~2.0 GB (→ >1.2 s), R05's pessimism holds and SSD streaming should be deferred to a batched-prefetch model rather than per-round streaming.

### 3d. Telemetry Counters Spec (from R07)

Extend the existing `llama_moe_stream_print_stats` surface (already prints base cache hit/miss counts and total bytes used; R07 claim-1) with the following **minimum additional counters**:

| Counter | Definition | Hook Point |
|---------|-----------|------------|
| `hit_rate[tier]` | hits / tokens, per tier (VRAM / RAM / SSD) | `cache_stats` struct fields in `llama_moe_stream_print_stats` |
| `bytes_served[tier]` | bytes served from each tier | `cache_stats` struct fields |
| `waste_bytes[tier]` | bytes prefetched but evicted or overwritten before consumption by the inference stream (R07 claim-4) | tiered dispatch logic in pr25294.diff |
| `eviction_count[tier]` | eviction events per tier | tiered dispatch logic in pr25294.diff |

These counters attach directly to `llama_moe_stream_print_stats` via existing `cache_stats` struct fields (R07 claim-3), and the tiered dispatch logic in PR #25294 provides the hook points for per-tier accounting. The existing `llama_moe_cache_step()` already emits hit/miss counts every 512 steps via `LLAMA_LOG_DEBUG` — the new counters promote this to a structured, always-on surface.

---

## 4. Phase-1 Implementation Epic Seed

Ordered work items the recommendation implies, traced to verdicts, sized S/M/L. **The first 3 items are the critical path and should start immediately.**

| # | Work Item | Traced To | Size | Notes |
|---|-----------|-----------|------|-------|
| **1** | **Rebase campaign onto `preview-v0.4.7`** — full fetch, trial merge-tree, resolve the 220-commit drift, update all diffs to target the new `llama-graph.cpp` layout (357 changed lines). | R01, R06, HW-MEASUREMENTS appendix-2 | **M** | Prerequisite for everything. Low-risk (0 conflicts on campaign docs commits) but the port code must target the new graph layout. |
| **2** | **Snapshot-pin both PRs** — freeze `pr27861.diff` and `pr25294.diff` as local quilt patches; implement the CI canary that verifies applied-patch-hash == snapshotted-diff-hash before compile. | R08 | **S** | Immediate; prevents build instability from upstream force-pushes during active review. |
| **3** | **Port PR #27861 (GPU↔RAM LRU cache)** — cherry-pick or manual port onto `preview-v0.4.7`; resolve conflicts in the 7 shared files; integrate `llama-moecache.cpp` + `llama-moecache.h`; verify KVarN paths untouched. | R02, R03, R07 | **L** | Phase 1 core. The GPU cache is the higher-leverage tier (RAM→VRAM is 20–40× SSD bandwidth). |
| **4** | **Implement R07 telemetry counters** — extend `llama_moe_stream_print_stats` with per-tier hit rate, bytes served, waste bytes, eviction counts. | R07 | **S** | Do this alongside item 3 so the cache ships with instrumentation. |
| **5** | **Design the RECONCILE adapter** — abstract the weight fetch path so O_DIRECT streaming (PR #25294) feeds into the GPU LRU cache (PR #27861); define the shared context lock for synchronized eviction. | R03 | **M** | Required before Phase 2 implementation; the adapter is the integration contract. |
| **6** | **Port PR #25294 (RAM↔SSD streaming)** — O_DIRECT async I/O pool, Wave-Partitional Prefill; place expert store on L800 partition. | R04, R05 | **L** | Phase 2 core. Must implement three-band prefetch (R04) but instrument with R07 counters to resolve the R04/R05 conflict empirically. |
| **7** | **Three-band prefetch + eviction implementation** — route-ahead prefetch across expert gates; LRU eviction at GPU tier synchronized with SSD prefetch via shared lock. | R04, R05 | **M** | The highest-risk item — the R04/R05 conflict means the prefetch effectiveness is unproven on this hardware. |
| **8** | **Fork-contingency threshold test** — verify the 400-commit / MoE-dispatch-divergence triggers are measurable; document the fork procedure. | R02 | **S** | Guardrail; do once after item 1. |
| **9** | **Multi-GPU staging through host RAM** — since P2P is unavailable, implement the host-RAM staging path for expert traffic between 3090 and 3070. | HW-MEASUREMENTS (tier-sizing) | **M** | Needed for the 2-GPU host box; the phasebuffer ExpertStore v1→v2 fallback design already anticipates this. |
| **10** | **Benchmark + tune** — measure tok/s, tier hit rates, DFlash break-even; tune cache sizes, eviction params, I/O thread count. | R04, R05, R07 | **M** | Validation gate; resolves R04/R05 conflict with data. |

**Start with items 1, 2, 3.** Item 1 (rebase) is the prerequisite. Item 2 (snapshot-pin) is cheap insurance. Item 3 (GPU cache port) is the highest-leverage implementation because the RAM→VRAM bandwidth advantage (20–40× over SSD) makes the GPU cache the tier that matters most for the DFlash verify-round working set.

---

## 5. Verdict Quality Notes

### Solid verdicts
- **R01 (conf 0.85):** Well-sourced, claims verified against `upstream-drift.log` (220 entries) and HW-MEASUREMENTS-host.md. The `preview-v0.4.7` recommendation is sound.
- **R02 (conf 0.88):** The quilt-series recommendation is well-reasoned; the fork-contingency threshold (400 commits) is a useful guardrail. The 310-line `llama-graph.cpp` drift figure matches `upstream-drift-stat.txt` (`src/llama-graph.cpp | 310 +-`).
- **R03 (conf 0.87):** The 7 shared files claim is **exactly verified** against the PR file lists. The reconcile adapter design is a reasonable synthesis. The only weakness: the adapter is a sketch, not a detailed design — it needs elaboration in the implementation epic.
- **R07 (conf 0.90):** The highest-confidence verdict. The telemetry extension is concrete, well-sourced to actual diffs, and the counter definitions are precise.
- **R08 (conf 0.89):** The snapshot-commit policy is well-justified by the measured 220-commit drift. The CI canary idea is practical.

### Verdicts needing human review
- **R04 (conf 0.88):** The arithmetic is **internally consistent** (2.2 GB / 1.6 GB/s = 1.375 s ✓), but the **load-bearing assumption** — that three-band prefetch can overlap 4.4 GB of the 6.6 GB working set, leaving only 2.2 GB on-demand — is **not measured**. It is an architectural assertion, not an empirical fact. The verdict reads as optimistic about prefetch effectiveness. The conflict with R05 (which is more empirically grounded in the dflash-port-notes.md measurements) is not adequately resolved. **Human review recommended:** decide whether to commit to three-band prefetch (R04) or defer SSD streaming to a batched model (R05), pending the telemetry data from items 3+4 above.
- **R05 (conf 0.85):** The arithmetic is **correct** (4.125 s and 9.81 s both verified ✓). The verdict is empirically grounded in the dflash-port-notes.md measurements (6.1 tok/s DFlash with ~98% tier-served experts). However, R05 was tagged **"refuted"** — meaning the campaign rejected the position that SSD streaming shifts the break-even. This is the correct verdict given the arithmetic (4.125 s >> 0.5 s), but the **implication** (that the SSD tier is useless) is too strong: the SSD tier still serves as the expert store backing the RAM staging tier, even if per-round streaming is not viable. **Human review recommended:** the SSD tier has value as a storage tier even if the streaming prefetch doesn't shift DFlash break-even.
- **R06 (conf 0.88):** **Arithmetic error detected.** R06 claim-1 states "142 new commits since the local merge-base." Every other source (R01, R02, R08, HW-MEASUREMENTS-host.md, and the 220-line `upstream-drift.log`) states **220 commits**. The 142-commit figure is incorrect — possibly from a stale fetch or a subset count. The rebase recommendation itself is correct and low-risk (confirmed by the merge-tree assessment in HW-MEASUREMENTS appendix-2), but the confidence should be discounted until the commit-count discrepancy is resolved. **Human review recommended:** re-fetch upstream and confirm the drift count before executing the rebase.

### Summary of concerns
1. **R04/R05 conflict** is the central design risk — the prefetch effectiveness assumption is unmeasured. The port should implement telemetry-first and resolve empirically.
2. **R06's 142-commit claim** is arithmetically inconsistent with all other sources (220 commits). The rebase scope is 220.
3. **R03's reconcile adapter** is a sketch, not a spec — it needs detailed design before Phase 2 implementation.
4. **No verdict addresses the DRAM-less NVMe degradation** under concurrent mixed read/write (HW-MEASUREMENTS point 3) — the expert store should be on its own L800 partition, not shared with engine writes. This is a hardware constraint the implementation must respect.
