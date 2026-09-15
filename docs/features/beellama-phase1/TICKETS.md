# TICKETS — Beellama Phase-1 (THINNER re-issue)

> **Source:** `EPIC.md` (authoritative), `PRD-v1.md`, `PORT-DESIGN-RECOMMENDATION.md` (R01–R08),
> `DESIGN-GA.md`, `DESIGN-GB.md`, `.scratch/beellama-phase1/ARCH-REVIEW-SUMMARY.md`.
> **Port repo:** `/home/<user>/projects/beellama-port` (branch `feature/three-tier-expert-cache`).
> **Engine repo:** `/home/<user>/projects/ace-engine` (branch `main`).
> **Date:** 2026-09-11 (THINNER re-issue — supersedes the 11-ticket coarse issue).

Grain follows the proven law: **one concern per ticket**; single file ≤~150 lines where
possible; multi-file tickets explicitly flagged with justification. Each ticket carries a
**TARGET-REPO** marker because engine runs execute in a workspace clone of `ace-engine`;
tickets that operate on `beellama-port` must be dispatched to a beellama-port agent or
executed directly (the engine cannot reach across repos). Every ticket leaves the tree
**compilable** — atomic = a 9B-class implementation agent completes it in ONE focused
pass without guessing.

Groups run in order **A → gate → (B ∥ C) → gates**.

---

## GROUP A — rebase-and-pin (seed items 1 + 2)

TARGET-REPO: **beellama-port** (all GA tickets). Prerequisite for everything.

Rebase `feature/three-tier-expert-cache` onto upstream tag `preview-v0.4.7`
(`53a68d3c3f36c3467fdb7431cd72473a45c03e86`), carrying local changes as a
`git format-patches` + `series` patch series with a deterministic reapplication
procedure. Then snapshot-pin both open PRs and wire a CI canary. The conflict
surface is `llama-graph.cpp` layout refactors (310 changed lines of drift,
concentrated in layout refactors rather than core MoE expert-GEMM dispatch logic),
per R02's reapplication procedure. The 4 local commits are docs-only — the rebase
is mechanically clean; `llama-graph.cpp` retargeting is deferred to Group B.

| ID | Title | Files | Deps | ~ln | Multi-file |
|---|---|---|---|---|---|
| GA-T01 | Verify rebase prerequisites — confirm tag, drift count, local-patch inventory | beellama-port: `campaign/results/ga-t01-prereqs.md` (new) | — | 30 | no |
| GA-T02 | Trial merge-tree against `preview-v0.4.7` — assess conflict surface before mutating history | beellama-port: `campaign/results/ga-t02-mergetree.txt` (new) | GA-T01 | 20 | no |
| GA-T03 | Execute rebase onto `preview-v0.4.7` — `git rebase --onto`, verify patch count preserved + build green | beellama-port: `feature/three-tier-expert-cache` branch | GA-T02 | 40 | no (branch op) |
| GA-T04 | Extract local changes to quilt series — `git format-patches` → `quilt/*.patch` + `quilt/series` | beellama-port: `quilt/*.patch`, `quilt/series` | GA-T03 | 50 | no (patch dir) |
| GA-T05 | Write `rebase-and-reapply.sh` — idempotent reapplication script (loops `git am --3way`, stops+names on conflict) | beellama-port: `quilt/rebase-and-reapply.sh` | GA-T04 | 120 | no |
| GA-T06 | Snapshot-pin both PRs — freeze `pr27861.diff` + `pr25294.diff` + SHA-256 manifest + reapplication-triggers doc | beellama-port: `quilt/pr27861.diff`, `quilt/pr25294.diff`, `quilt/sources-manifest.json`, `quilt/REAPPLICATION-TRIGGERS.md` | GA-T03 | 60 | yes (4 files — all new, atomic pin unit) |
| GA-T07 | Write `ci/quilt-canary.sh` — dry-run `git apply --check` + `sha256sum -c` against manifest | beellama-port: `ci/quilt-canary.sh` | GA-T06 | 80 | no |
| GA-T08 | Wire `.github/workflows/quilt-canary.yml` — triggers on push, runs canary before compile | beellama-port: `.github/workflows/quilt-canary.yml` | GA-T07 | 40 | no |

### Acceptance criteria (per ticket)

- **GA-T01:** `campaign/results/ga-t01-prereqs.md` records: `git rev-parse preview-v0.4.7` == `53a68d3c3`; `git rev-list --count` from merge-base to `origin/main` == 220; local patch count == 4 (all docs-only, paths `campaign/`); `git diff main..HEAD --stat -- src/` is empty.
- **GA-T02:** `campaign/results/ga-t02-mergetree.txt` is the output of `git merge-tree --write-tree`; documents whether the merge is clean (expected: clean — docs-only commits, disjoint paths).
- **GA-T03:** `git merge-base --is-ancestor 53a68d3c3 HEAD` is true; local patch count == 4 (unchanged from GA-T01); `cmake -B build -DGGML_CUDA=ON .. && cmake --build build -j$(nproc)` exits 0; `grep -q build_moe_ffn src/llama-graph.cpp` confirms MoE path present.
- **GA-T04:** `quilt/` contains 4 `.patch` files + `series` file; `cat quilt/series` lists all patches in order; each patch applies cleanly with `git am --3way`.
- **GA-T05:** `quilt/rebase-and-reapply.sh` is idempotent (running twice yields same tree); loops `git am --3way` over `quilt/series`; on conflict it **exits non-zero and names the patch** (never silently takes ours/theirs); `set -euo pipefail`.
- **GA-T06:** `sha256sum quilt/pr27861.diff quilt/pr25294.diff` matches `.scratch/beellama-dogfood/sources/`; `quilt/sources-manifest.json` records both SHA-256; `quilt/REAPPLICATION-TRIGGERS.md` lists all three triggers (force-push, design-altering review, final merge).
- **GA-T07:** `bash ci/quilt-canary.sh` exits 0 on real diffs; dry-runs `git apply --check` on both; verifies `sha256sum -c` against manifest; a deliberately altered diff (flip one byte) fails — tested once and logged.
- **GA-T08:** `.github/workflows/quilt-canary.yml` triggers on push; step 1 runs `bash ci/quilt-canary.sh`; step 2 (compile) only runs if canary exits 0; workflow file is valid YAML.

---

## GROUP B — cache-port (seed items 3 + 4)

TARGET-REPO: **beellama-port** (all GB tickets). Depends on GROUP A (GA-T03).

Port PR #27861's GPU↔RAM LRU cache onto the rebased base, then attach the R07
telemetry counters at the named functions. These are C/C++ implementation atoms with
files in beellama-port. The 7 shared files are `common/arg.cpp`, `common/common.cpp`,
`common/common.h`, `include/llama.h`, `src/CMakeLists.txt`, `src/llama-context.cpp`,
`src/llama-graph.cpp` (verified against `pr27861-files.json`).

**ARCH-REVIEW PREREQUISITE (Candidate 1):** The first GB ticket collapses
`build_moe_ffn`'s 28-parameter signature into a `moe_ffn_desc` struct. This is the
top recommendation from `ARCH-REVIEW-SUMMARY.md` and a **prerequisite for the cache
hook** (the cache handle lives on the desc). It must land before any graph-integration
ticket. Candidates 2/4/5/6 are deferred to Phase-2 (see §"Arch-review disposition").

| ID | Title | Files | Deps | ~ln | Multi-file | Category |
|---|---|---|---|---|---|---|
| GB-T01 | Collapse `build_moe_ffn` signature → `moe_ffn_desc` struct — prerequisite for cache hook (ARCH-REVIEW Candidate 1) | beellama-port: `src/llama-graph.h` (struct), `src/llama-graph.cpp` (2 overloads → 1), `src/models/qwen3moe.cpp` + `deepseek2.cpp` + `llama4.cpp` + `dflash.cpp` (call-site updates) | GA-T03 | 150 | yes (6 files — struct + call sites; justified: the desc is the seam every later ticket hooks) | api |
| GB-T02 | Port `llama-moecache.h` — declare cache manager types + 3 public functions | beellama-port: `src/llama-moecache.h` | GB-T01 | 80 | no | api |
| GB-T03 | Port `llama-moecache.cpp` — define `llama_moe_cache_init/lookup/step` + `llama_moe_cache_layer` internals | beellama-port: `src/llama-moecache.cpp` | GB-T02 | 150 | no | implement |
| GB-T04 | Register `llama-moecache.cpp` in `src/CMakeLists.txt` — one-liner after `llama-model.cpp` | beellama-port: `src/CMakeLists.txt` | GB-T03 | 10 | no | infra |
| GB-T05 | Port ggml MoE observation callback to `ggml/include/ggml.h` — `ggml_moe_obs_cb_t` typedef + setter/getter declarations | beellama-port: `ggml/include/ggml.h` | GB-T02 | 30 | no | api |
| GB-T06 | Port ggml MoE observation callback to `ggml/src/ggml.c` — globals `g_moe_obs_cb/g_moe_obs_ud` + getter/setter definitions | beellama-port: `ggml/src/ggml.c` | GB-T05 | 40 | no | implement |
| GB-T07 | Port ggml MoE observation callback to `ggml/src/ggml-cpu/ggml-cpu.c` — `moe_tbl` skip logic + callback invocation | beellama-port: `ggml/src/ggml-cpu/ggml-cpu.c` | GB-T06 | 60 | no | implement |
| GB-T08 | Add `n_moe_cache_slots`/`n_moe_cache_inserts` to `llama_context_params` in `include/llama.h` | beellama-port: `include/llama.h` | GB-T02 | 15 | no | api |
| GB-T09 | Add `--moe-expert-cache` + `--moe-expert-cache-inserts` CLI flags to `common/arg.cpp` | beellama-port: `common/arg.cpp` | GB-T08 | 40 | no | api |
| GB-T10 | Add `n_moe_cache_slots`/`n_moe_cache_inserts` to `common_params` in `common/common.h` + wire in `common/common.cpp` | beellama-port: `common/common.h`, `common/common.cpp` | GB-T09 | 30 | yes (2 files — struct field + assignment; trivial coupling) | api |
| GB-T11 | Forward-declare `llama_moe_stream_print_stats` + `llama_moe_telemetry_*` API in `include/llama.h` | beellama-port: `include/llama.h` | GB-T08 | 20 | no | api |
| GB-T12 | Wire cache init + `llama_moe_cache_step()` in `src/llama-context.cpp` — include header, call init after constructing log, call step before `return 0` in `decode()` | beellama-port: `src/llama-context.cpp` | GB-T03, GB-T10 | 40 | no | implement |
| GB-T13 | Hook `llama_moe_cache_lookup` + device chain into `build_moe_ffn` in `src/llama-graph.cpp` — the deepest seam (3 insertion points per DESIGN-GB §2 GB-T04) | beellama-port: `src/llama-graph.cpp` | GB-T01, GB-T07, GB-T11, GB-T12 | 150 | no | implement |
| GB-T14 | Add `layer_state` per-tier counters to `src/llama-moecache.cpp` — `n_hit/n_miss/n_bytes_served[3]/n_waste_bytes[3]/n_evictions[3]/n_eviction_reloads[3]` + `n_tokens` denominator | beellama-port: `src/llama-moecache.cpp`, `src/llama-moecache.h` | GB-T13 | 80 | yes (2 files — struct field in .h + accumulation logic in .cpp; tight coupling) | api |
| GB-T15 | Define telemetry ring-buffer types in `src/llama-moecache.h` — `moe_tier`, `moe_event_kind`, `moe_telemetry_event` (32-byte), `moe_telemetry_ring` class declaration | beellama-port: `src/llama-moecache.h` | GB-T14 | 60 | no | api |
| GB-T16 | Implement `moe_telemetry_ring` + `llama_moe_telemetry_*` functions in `src/llama-moecache.cpp` — ring push/pop/drain, enable/disable/is_enabled/dropped, JSON serialization | beellama-port: `src/llama-moecache.cpp` | GB-T15 | 150 | no | implement |
| GB-T17 | Define `llama_moe_stream_print_stats` in `src/llama-moecache.cpp` — format per-tier counters (hit_rate, bytes_served, waste_bytes, eviction_count, eviction_reloads) per layer; remove the `LLAMA_LOG_DEBUG` 512-step emit | beellama-port: `src/llama-moecache.cpp` | GB-T14, GB-T16 | 120 | no | implement |
| GB-T18 | Build + smoke test — `cmake -DGGML_CUDA=ON`, `--moe-expert-cache` in `--help`, server starts on MoE model, KVarN paths untouched, telemetry lines appear | beellama-port: `ci/smoke-moecache.sh` | GB-T04, GB-T17 | 80 | no | infra |

### Acceptance criteria (per ticket)

- **GB-T01:** `src/llama-graph.h` declares `moe_ffn_desc` struct bundling weights/biases/scales/routing config; `build_moe_ffn` signature collapses from 2 overloads (24–28 params) to 1 (`(const moe_ffn_desc & desc, struct ggml_tensor * cur)`); 4 model call sites updated; build green; `grep -c "build_moe_ffn" src/models/qwen3moe.cpp src/models/deepseek2.cpp src/models/llama4.cpp src/models/dflash.cpp` == 4.
- **GB-T02:** `src/llama-moecache.h` declares `llama_moe_cache_layer` struct, `llama_moe_cache_init`, `llama_moe_cache_lookup`, `llama_moe_cache_step`; file compiles standalone (include-only).
- **GB-T03:** `src/llama-moecache.cpp` defines all 3 public functions; LRU logic with per-layer companion tensors, I32 expert→slot table, split `mul_mat_id` chain; build green.
- **GB-T04:** `grep -q "llama-moecache.cpp" src/CMakeLists.txt`; entry is after `llama-model.cpp`; build green.
- **GB-T05:** `ggml/include/ggml.h` contains `ggml_moe_obs_cb_t` typedef, `ggml_set_moe_obs_callback`, `ggml_get_moe_obs_callback` declarations; build green.
- **GB-T06:** `ggml/src/ggml.c` contains `g_moe_obs_cb`/`g_moe_obs_ud` globals + getter/setter definitions; build green.
- **GB-T07:** `ggml/src/ggml-cpu/ggml-cpu.c` contains `moe_tbl`/`moe_dummy` from `dst->src[3]` + `op_params[0]`, skip logic (zero dst row + `continue`), observation callback invocation after `matrix_row_counts` loop when `strstr(src0->name, "ffn_gate_exps")`; build green.
- **GB-T08:** `grep -q "n_moe_cache_slots" include/llama.h` and `grep -q "n_moe_cache_inserts" include/llama.h`; both in `llama_context_params`; build green.
- **GB-T09:** `grep -q "moe-expert-cache" common/arg.cpp`; both flags present with `set_env("LLAMA_ARG_MOE_EXPERT_CACHE")` and `set_env("LLAMA_ARG_MOE_EXPERT_CACHE_INSERTS")`; build green.
- **GB-T10:** `common/common.h` has `n_moe_cache_slots`/`n_moe_cache_inserts` in `common_params`; `common/common.cpp` assigns them in `common_context_params_to_llama`; build green.
- **GB-T11:** `include/llama.h` declares `llama_moe_stream_print_stats` + `llama_moe_telemetry_enable/disable/is_enabled/dropped/drain/json`; no call sites yet — build green (linker unreferenced is OK).
- **GB-T12:** `src/llama-context.cpp` includes `llama-moecache.h`, calls `llama_moe_cache_init(model, params.n_moe_cache_slots, params.n_moe_cache_inserts)` after constructing log, calls `llama_moe_cache_step()` before `return 0` in `decode()`; build green.
- **GB-T13:** `build_moe_ffn` in `src/llama-graph.cpp` has 3 cache hook points: (1) `llama_moe_cache_lookup(up_exps)` after `ggml_build_forward_expand(gf, weights)` guarded by the `n_tokens==1 && !gate_up_exps && ...` condition; (2) `tensor->src[3] = mcache->host_table`, `tensor->op_params[0] = mcache->n_slots` after each `build_lora_mm_id`; (3) device chain (`up_c`/`gate_c`/`down_c` via `ggml_mul_mat_id` + inline SwiGLU) + `ggml_add` into `experts` after `build_lora_mm_id(down_exps,...)`; build green.
- **GB-T14:** `src/llama-moecache.h` adds counter fields to `layer_state`; `src/llama-moecache.cpp` increments `n_hit/n_miss/n_bytes_served[tier]/n_waste_bytes[tier]/n_evictions[tier]/n_eviction_reloads[tier]` and `n_tokens` at the named attachment points; build green.
- **GB-T15:** `src/llama-moecache.h` declares `moe_tier` (VRAM=0/RAM=1/SSD=2), `moe_event_kind` (Hit/Miss/Evict/Upload/Waste), `moe_telemetry_event` (32-byte struct), `moe_telemetry_ring` class with `push/pop/drain/len/dropped_count`; build green.
- **GB-T16:** `src/llama-moecache.cpp` defines `moe_telemetry_ring` (power-of-2 capacity, `atomic<uint64_t>` head/tail/dropped, overwrite-oldest) + `llama_moe_telemetry_enable/disable/is_enabled/dropped/drain/json`; ring is populated from `layer_state` counters; build green.
- **GB-T17:** `llama_moe_stream_print_stats` formats 5 counter groups per tier per layer (hit_rate = n_hit/(n_hit+n_miss), bytes_served, waste_bytes, eviction_count, eviction_reloads); `LLAMA_LOG_DEBUG` 512-step emit removed; build green.
- **GB-T18:** `cmake -B build -DGGML_CUDA=ON .. && cmake --build build --target llama-server -j$(nproc)` exits 0; `./build/bin/llama-server --help 2>&1 | grep -E "moe-expert-cache"` shows both flags; server starts with `--moe-expert-cache 64 --cpu-moe -fa on` on a MoE model; `grep -rn "KVarN\|kvarn\|dflash\|DFlash" src/llama-moecache.cpp src/llama-moecache.h src/llama-graph.cpp src/llama-context.cpp include/llama.h common/arg.cpp common/common.h common/common.cpp ggml/include/ggml.h ggml/src/ggml.c ggml/src/ggml-cpu/ggml-cpu.c` returns only expected hits; `ci/smoke-moecache.sh` documents the procedure.

---

## GROUP C — adapter-design (seed item 5)

TARGET-REPO: **ace-engine** (design-doc deliverable). Depends on GROUP A (GA-T03).
Design-only — no code, no conflict with GROUP B.

Produce the RECONCILE adapter detailed design doc from R03's sketch. This is a
research/design atom: the deliverable is a design doc verdict. Sources are the PR
diffs + the port's `build_moe_ffn`. The adapter is the integration contract required
before Phase 2 implementation.

| ID | Title | Files | Deps | ~ln | Multi-file | Category |
|---|---|---|---|---|---|---|
| GC-T01 | Research + diff-span evidence for RECONCILE adapter — read both PR diffs, map conflict surface against rebased `llama-graph.cpp`, collect per-file conflict semantics | ace-engine: `.scratch/beellama-phase1/adapter-research-evidence.md` (new) | GA-T03 | — | no | research |
| GC-T02 | Write RECONCILE-ADAPTER-DESIGN.md — I/O multiplexer, shared context lock, fetch-path contract, 7 shared files + conflict semantics, elaborated to Phase-2-implementable spec | ace-engine: `docs/features/beellama-phase1/RECONCILE-ADAPTER-DESIGN.md` | GC-T01 | — | no | research |

### Acceptance criteria (per ticket)

- **GC-T01:** `.scratch/beellama-phase1/adapter-research-evidence.md` records: diff-span citations from `pr27861.diff` and `pr25294.diff` for each conflict claim; the CURRENT (rebased, post-`preview-v0.4.7`) `llama-graph.cpp` layout vs. the layout the PRs were diffed against; the 7 shared files named with per-file conflict semantics; every claim cites a `file://` source.
- **GC-T02:** `docs/features/beellama-phase1/RECONCILE-ADAPTER-DESIGN.md` has a `## Verdict` section naming the adapter contract; documents the I/O multiplexer, the shared context lock for synchronized eviction, and the fetch-path contract; every conflict claim cites diff spans from `pr27861.diff` and `pr25294.diff`; references the CURRENT (rebased, post-`preview-v0.4.7`) `llama-graph.cpp` layout; names the 7 shared files and per-file conflict semantics; R03's adapter sketch is elaborated to a spec sufficient to implement Phase 2.

---

## Dependency graph (between and within groups)

```
GROUP A (rebase-and-pin)
  GA-T01 (verify prereqs)
    → GA-T02 (trial merge-tree)
      → GA-T03 (execute rebase)
        → GA-T04 (extract quilt series)
          → GA-T05 (rebase-and-reapply.sh)
        → GA-T06 (snapshot-pin PRs)
          → GA-T07 (ci/quilt-canary.sh)
            → GA-T08 (workflow yml)

── code-review gate A ──

GROUP B (cache-port)                          GROUP C (adapter-design)
  GB-T01 (moe_ffn_desc collapse)                GC-T01 (research + evidence)
    → GB-T02 (moecache.h)                         → GC-T02 (design doc)
      → GB-T03 (moecache.cpp)
        → GB-T04 (CMakeLists.txt)
      → GB-T05 (ggml.h callback decl)
        → GB-T06 (ggml.c callback impl)
          → GB-T07 (ggml-cpu.c skip+invoke)
      → GB-T08 (llama.h params)
        → GB-T09 (arg.cpp CLI flags)
          → GB-T10 (common.h+cpp params)
        → GB-T11 (llama.h telemetry fwd-decl)
      → GB-T12 (llama-context.cpp init+step) ──┐
      GB-T07 ───────────────────────────────────┤
      GB-T11 ───────────────────────────────────┤
      GB-T01 ───────────────────────────────────┼→ GB-T13 (build_moe_ffn hook)
                                                 │    → GB-T14 (layer_state counters)
                                                 │         → GB-T15 (ring types in .h)
                                                 │              → GB-T16 (ring impl in .cpp)
                                                 │         → GB-T17 (print_stats)
                                                 │              → GB-T18 (build+smoke)
      GB-T04 ───────────────────────────────────→ GB-T18

── code-review gate B ──                     ── code-review gate C ──
```

- **A → B:** GROUP B ports onto the rebased base; cannot start until GA-T03 lands.
- **A → C:** GC-T01 references the rebased layout; runs in parallel with GROUP B (design-only, no code conflict).
- **B ∥ C:** Groups B and C are independent after gate A.

---

## Arch-review disposition

From `.scratch/beellama-phase1/ARCH-REVIEW-SUMMARY.md`:

| Candidate | Disposition | Notes |
|---|---|---|
| **1** Collapse `build_moe_ffn` signature → `moe_ffn_desc` struct | **GB-T01** (FIRST Group-B ticket, prerequisite for cache hook) | Top recommendation. Creates the seam where the cache handle lives. |
| 2 Split `llm_graph_context` into per-concern builders | **Phase-2** | `moe_ffn_desc` must NOT preclude this — keep the desc as plain data, do not bake in builder ownership. |
| 3 Extract perf+timing from `llama_context` into telemetry module | **Phase-2** | Deferred; `moe_ffn_desc` must not embed telemetry state that would conflict with a future `llm_telemetry` module. |
| 4 Place telemetry ring buffer behind perf snapshot seam | **Phase-2** | Partially realized in GB-T15/GB-T16 (ring buffer exists); the perf-snapshot seam unification is Phase-2. |
| 5 Deduplicate MoE graph-build pattern across model files | **Phase-2** | `moe_ffn_desc` enables a future `make_moe_desc()` helper but does not implement it. |
| 6 Unify `server_metrics` and `llama_perf_context_data` | **Phase-2** | Deferred. |

---

## Skill mapping

Skills the implementing agent should load per ticket. Skill keys: `tdd`,
`worktree-guard`, `gpu-lease`, `perf-verification`, `check-work`, `code-review`,
`codebase-design`, `resolving-merge-conflicts`, `research`.

| Ticket | Skills | Notes |
|---|---|---|
| GA-T01 | worktree-guard, check-work | Prerequisite verification |
| GA-T02 | worktree-guard, check-work | Trial merge-tree (read-only) |
| GA-T03 | worktree-guard, resolving-merge-conflicts, check-work | Rebase execution |
| GA-T04 | worktree-guard, check-work | Patch extraction |
| GA-T05 | tdd, worktree-guard, check-work | Reapply script (idempotent) |
| GA-T06 | worktree-guard, check-work | Diff pinning + manifest |
| GA-T07 | tdd, worktree-guard, check-work | Canary script (negative test) |
| GA-T08 | worktree-guard, check-work | Workflow YAML |
| GB-T01 | tdd, worktree-guard, codebase-design, check-work | Signature collapse (api seam) |
| GB-T02 | tdd, worktree-guard, check-work | Header-only |
| GB-T03 | tdd, worktree-guard, check-work | Cache manager impl |
| GB-T04 | worktree-guard, check-work | CMake one-liner |
| GB-T05 | tdd, worktree-guard, check-work | ggml header |
| GB-T06 | tdd, worktree-guard, check-work | ggml globals |
| GB-T07 | tdd, worktree-guard, check-work | ggml-cpu skip logic |
| GB-T08 | tdd, worktree-guard, check-work | llama.h params |
| GB-T09 | tdd, worktree-guard, check-work | CLI flags |
| GB-T10 | tdd, worktree-guard, check-work | common_params |
| GB-T11 | tdd, worktree-guard, check-work | Forward decls |
| GB-T12 | tdd, worktree-guard, check-work | Context wiring |
| GB-T13 | tdd, worktree-guard, codebase-design, check-work | Graph integration (deepest seam) |
| GB-T14 | tdd, worktree-guard, check-work | Counter accumulation |
| GB-T15 | tdd, worktree-guard, check-work | Ring types |
| GB-T16 | tdd, worktree-guard, check-work | Ring impl + telemetry API |
| GB-T17 | tdd, worktree-guard, check-work | print_stats formatter |
| GB-T18 | gpu-lease, worktree-guard, check-work | Build + smoke (GPU lease) |
| GC-T01 | research, codebase-design, check-work | Research + evidence gathering |
| GC-T02 | research, codebase-design, check-work | Design doc authoring |
| **Group closing gates** | code-review | Run at end of A, B, C (per group) |

---

## Group run order with gates

```
GA-T01 → GA-T02 → GA-T03 → GA-T04 → GA-T05
                  → GA-T06 → GA-T07 → GA-T08
    → code-review gate A
    → GB-T01 → GB-T02 → GB-T03 → GB-T04
                     → GB-T05 → GB-T06 → GB-T07
                     → GB-T08 → GB-T09 → GB-T10
                            → GB-T11
              (GB-T03 → GB-T12) ──┐
              (GB-T07) ───────────┤
              (GB-T11) ───────────┤
              (GB-T01) ───────────┼→ GB-T13 → GB-T14 → GB-T15 → GB-T16
                                            │               → GB-T17
                                            └──────────────→ GB-T17
              (GB-T04) ──────────────────────────────────→ GB-T18
              (GB-T17) ──────────────────────────────────→ GB-T18
    → code-review gate B
    → GC-T01 → GC-T02 (parallel with GB run after GA-T03)
    → code-review gate C
```

- **GA run:** strictly sequential T01→T02→T03, then T04→T05 and T06→T07→T08 parallel after T03.
- **GB run:** T01 first (prerequisite seam). T02 then fans out to T03/T05/T08. T03→T04. T05→T06→T07. T08→T09→T10 and T08→T11. T03+T10→T12. T01+T07+T11+T12→T13. T13→T14. T14→T15→T16 and T14→T17. T04+T16+T17→T18.
- **GC run:** T01→T02, parallel with GB after GA-T03.

---

## TARGET-REPO split

| Group | Tickets | TARGET-REPO | Rationale |
|---|---|---|---|
| A (rebase-and-pin) | GA-T01…GA-T08 | **beellama-port** | Branch rebase, quilt, CI canary all operate on the port repo |
| B (cache-port) | GB-T01…GB-T18 | **beellama-port** | C/C++ implementation atoms with files in the port repo |
| C (adapter-design) | GC-T01…GC-T02 | **ace-engine** | Design-doc deliverable lands in ace-engine docs |

All engine runs execute in a workspace clone of `ace-engine`. Tickets for beellama-port
work (Groups A, B) must either be executed directly by agents with beellama-port access
or dispatched via a beellama-port workspace — the engine cannot reach across repos.
Group C's design doc is the one deliverable that belongs in ace-engine.
