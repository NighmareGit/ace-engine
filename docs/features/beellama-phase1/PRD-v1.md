# PRD — Beellama Phase-1

> **PRD ID:** beellama-phase1-v1
> **Epic:** `docs/features/beellama-phase1/EPIC.md` (authoritative)
> **Status:** DRAFT
> **Source of truth:** `PORT-DESIGN-RECOMMENDATION.md` (R01–R08)
> **Date:** 2026-09-11

---

## 1. Objective

Rebase the beellama-port `feature/three-tier-expert-cache` branch onto upstream `preview-v0.4.7`, snapshot-pin the two open upstream PRs with a CI canary, port PR #27861's GPU↔RAM LRU cache with live R07 telemetry (reconciled with the phasebuffer/Lidenburg per-tier event model), expose the telemetry via a remote-consumption event-stream endpoint, and produce the RECONCILE adapter detailed design. This is the prerequisite epic for Phase 2 (SSD streaming, prefetch, benchmark).

## 2. Background

- beellama-port `feature/three-tier-expert-cache` is **220 commits** behind `origin/main` (confirmed: `upstream-drift.log` has 220 entries; `git rev-list --count` = 220).
- `llama-graph.cpp` has **310 changed lines of drift** (layout refactors, not core MoE dispatch).
- PR #27861 (GPU-resident LRU cache) and PR #25294 (O_DIRECT SSD streaming) are both OPEN, actively reviewed, subject to force-push.
- The R04/R05 conflict (prefetch effectiveness) is unmeasured — telemetry-first is the resolution strategy.
- R06's "142 commits" claim is arithmetically inconsistent with all other sources; the drift is **220**.
- The phasebuffer and Lidenburg projects have already designed and partially implemented a telemetry architecture for the *same* three-tier expert-cache topology (VRAM↔RAM↔SSD). Phase-1 adapts this prior art rather than reinventing it (see EPIC §7 for the full reconciliation).

## 3. Scope

| # | Item | Traced To | Design-only? |
|---|------|-----------|--------------|
| 1 | Rebase onto `preview-v0.4.7`, retarget patches to new graph layout | R01, R06 | no |
| 2 | Snapshot-pin PRs + CI canary (hash equality before compile) | R08 | no |
| 3 | Port PR #27861 LRU cache onto rebased base | R02, R03, R07 | no |
| 4 | Implement R07 telemetry counters (per-tier hit rate, bytes served, waste, evictions) reconciled with Lidenburg per-tier event model | R07 + prior art | no |
| 5 | Remote-consumption event-stream endpoint (JSON-lines + SSE) for the telemetry counters | PULSAR-OPS-00 adaptation | no |
| 6 | RECONCILE adapter detailed design doc | R03 | **yes** |

## 4. Non-Goals (Phase-2)

SSD streaming (PR #25294), three-band prefetch, fork-contingency test, multi-GPU staging, benchmark+tune, live web UI timeline, expert heatmap, latency waterfall, binary capture format, io_uring deep telemetry, time-series CSV sampling.

## 5. Sources

- `docs/features/beellama-tiered-memory/PORT-DESIGN-RECOMMENDATION.md`
- `.scratch/beellama-dogfood/sources/` (`upstream-drift.log`, `upstream-drift-stat.txt`, `llama-graph-drift.diff`, `pr27861.diff`, `pr25294.diff`, `pr27861-files.json`, `pr25294-files.json`, `HW-MEASUREMENTS-host.md`, `dflash-port-notes.md`)
- `/home/<user>/projects/beellama-port/campaign/PRD.md`
- `/home/<user>/projects/beellama-port/campaign/issues/phase-1-issues.md`
- **Telemetry prior art (adapted, not reinvented):**
  - `/home/<user>/projects/phasebuffer/docs/design/telemetry-interface-system.md` — SPSC ring buffer, runtime gate, event model, JSON/binary export. Provides the capture discipline (§4) and export format (§8).
  - `/home/<user>/projects/phasebuffer/docs/lidenburg-telemetry-concept.md` — three-tier cache telemetry (per-layer counters, PCIe timing, eviction telemetry, expert event log). Provides the per-tier event model (§6) and the gap analysis that motivates Phase-1's counter selection.
  - `/home/<user>/projects/phasebuffer/docs/prd/telemetry-deepening.md` — expert heatmap, routing histograms, latency waterfall, request_id propagation. Deferred to Phase-2 (§9).
  - `/home/<user>/projects/phasebuffer/.scratch/issues/PULSAR-OPS-00-telemetry-event-stream-endpoints.md` — SSE/JSON-lines event stream endpoints. Provides the remote-consumption interface design (§8).
  - `/home/<user>/projects/phasebuffer/.scratch/issues/PULSAR-OPS-01-web-ui-live-telemetry-trace-timeline.md` — live web UI timeline. Deferred to Phase-2 (§9).

## 6. User Stories

### 6.1 Telemetry Consumer (Remote Dashboard)

> **As a** performance engineer running a remote dashboard,
> **I want** to read the beellama cache's per-tier hit rates, bytes served, waste bytes, and eviction counts from a standard event-stream endpoint on the running llama-server,
> **So that** I can monitor cache behavior in real time without ssh'ing into the host box or parsing stdout logs.

**Acceptance criteria:**
- `GET /moe-cache/telemetry` returns a JSON array of per-tier snapshots (one entry per layer).
- `GET /moe-cache/telemetry/stream` (SSE) pushes a new JSON-lines snapshot on each stats tick (~10s).
- Each snapshot is self-describing: `{"ts":<ns>,"tier":"VRAM","layer":15,"hit_rate":0.75,"bytes_served":131072,"waste_bytes":0,"evictions":3}`.
- The endpoint returns HTTP 404 when telemetry is disabled (runtime gate off).
- A consumer on a separate machine can connect and read the stream with standard tools (`curl`, a Python script, Grafana JSON datasource).

**Traced to:** PULSAR-OPS-00 (`/telemetry/stream`, `/telemetry/snapshot`); `telemetry-interface-system.md` §9.1 JSON schema; `lidenburg-telemetry-concept.md` §Gap 6 (JSON-lines export).

### 6.2 Offline Analysis of Captured Runs

> **As a** researcher analyzing cache behavior after a benchmark run,
> **I want** the server to dump a complete JSON-lines telemetry log to a file at exit,
> **So that** I can reconstruct the per-tier hit-rate timeline, identify thrashing layers, and correlate evictions with inference phases offline.

**Acceptance criteria:**
- On server exit (or via `POST /moe-cache/telemetry/export` with `{action:"start", path:"..."}`), a JSON-lines file is written containing every stats-tick snapshot.
- The file is self-describing: header line with `{version, engine, model, started_at}`, then one snapshot per line, then a footer with `{total_events, dropped_events}`.
- The file can be consumed by standard tools (jq, pandas, Grafana).

**Traced to:** `telemetry-interface-system.md` §9.1 (JSON schema); `lidenburg-telemetry-concept.md` §Gap 6 Option A (JSON-lines from stats thread); PULSAR-OPS-00 (`POST /telemetry/export`).

### 6.3 Per-Tier Hit-Rate Queries

> **As a** developer tuning the cache,
> **I want** to query the live per-tier hit rate, bytes served, waste bytes, and eviction counts — broken down by layer — from the running server,
> **So that** I can answer questions like "is layer 15 thrashing the VRAM cache?" or "how many bytes are being wasted by premature eviction?" without stopping the server.

**Acceptance criteria:**
- The `llama_moe_stream_print_stats` surface is extended with per-tier counters: `hit_rate[tier]`, `bytes_served[tier]`, `waste_bytes[tier]`, `eviction_count[tier]`.
- Each counter is tracked per-layer (adopted from Lidenburg Gap 1).
- The stats thread prints a per-layer breakdown: `[expert cache] layer 15: VRAM=45 (75%) RAM=10 (17%) disk=5 (8%)`.
- The same counters are exposed via the remote endpoint (story 6.1).

**Traced to:** R07 (the six counter groups); `lidenburg-telemetry-concept.md` §Gap 1 (per-layer), §Gap 2 (PCIe bytes), §Gap 4 (evictions); `telemetry-interface-system.md` §3.1 (ExpertFetch event with tier tag).

## 7. Functional Requirements

| ID | Requirement | Traced To |
|---|---|---|
| F1 | Rebase `feature/three-tier-expert-cache` onto `preview-v0.4.7`; retarget all local patches to the new `llama-graph.cpp` layout. | R01, R06 |
| F2 | Snapshot-pin PR #27861 and PR #25294 as local quilt patches; CI canary verifies `hash(applied) == hash(snapshotted)` before compile. | R08 |
| F3 | Port PR #27861's GPU↔RAM LRU cache onto the rebased base; `--moe-expert-cache` CLI flag live. | R02, R03, R07 |
| F4 | Extend `llama_moe_stream_print_stats` with per-tier counters: `hit_rate[tier]`, `bytes_served[tier]`, `waste_bytes[tier]`, `eviction_count[tier]`. Each tracked per-layer. | R07; `lidenburg-telemetry-concept.md` §Gap 1, §Gap 4 |
| F5 | Capture PCIe transfer timing for RAM→VRAM promotions (bytes + duration). | `lidenburg-telemetry-concept.md` §Gap 2 |
| F6 | Emit a timestamped expert-event on every tier transition (RAM→GPU, SSD→RAM, GPU→RAM eviction) into a small ring buffer (≤4 MiB). | `lidenburg-telemetry-concept.md` §Gap 3; `telemetry-interface-system.md` §4 |
| F7 | Runtime gate (CLI flag `--moe-expert-cache-telemetry` or env var) enabling/disabling telemetry without recompile. Default off. | `telemetry-interface-system.md` §5.3, §6.4 |
| F8 | Remote-consumption endpoint: `GET /moe-cache/telemetry` (JSON array snapshot) and `GET /moe-cache/telemetry/stream` (SSE live stream). Both return 404 when telemetry disabled. | PULSAR-OPS-00 (`/telemetry/stream`, `/telemetry/snapshot`) |
| F9 | JSON-lines export to file on demand (`POST /moe-cache/telemetry/export`) or at server exit. | PULSAR-OPS-00 (`POST /telemetry/export`); `lidenburg-telemetry-concept.md` §Gap 6 |
| F10 | RECONCILE adapter detailed design doc delivered and approved as Phase-2 integration contract. | R03 |

## 8. Remote-Consumption Interface Spec

Adapted from PULSAR-OPS-00. The C++ side exposes:

| Endpoint | Method | Response | Notes |
|---|---|---|---|
| `/moe-cache/telemetry` | GET | JSON array of per-tier snapshots | Current ring-buffer drain |
| `/moe-cache/telemetry/stream` | GET | `text/event-stream` (SSE) | Live push on each stats tick; keep-alive every 15s; `overrun` event on ring-buffer loss |
| `/moe-cache/telemetry/export` | POST | `{status: "started"\|"stopped"}` | Body: `{action, path}`; wraps JSON-lines file sink |

**JSON snapshot schema** (adapted from `telemetry-interface-system.md` §9.1):
```json
{"ts":1234567890,"tier":"VRAM","layer":15,"hit_rate":0.75,"bytes_served":131072,"waste_bytes":0,"evictions":3}
```

**SSE framing** (adapted from PULSAR-OPS-00):
```
event: moe-cache
data: {"ts":1234567890,"tier":"VRAM","layer":15,...}
```

## 9. Out of Scope (Phase-2)

The following prior-art capabilities are explicitly deferred to Phase-2:

| Capability | Source doc | Reason |
|---|---|---|
| Live web UI timeline (Trace Timeline tab, waterfall) | PULSAR-OPS-01 | Needs stable event stream + browser rendering; deferred until Phase-1 endpoint is live |
| Expert heatmap (per-(layer, expert) frequency) | `telemetry-deepening.md` Phase A | Needs full three-tier cache at scale |
| Latency waterfall (Start/End pairing by token_id) | `telemetry-deepening.md` Phase A | Needs request_id propagation (deepening Phase B) |
| Binary capture format (high-frequency disk export) | `telemetry-interface-system.md` §9.2 | JSON-lines suffices for Phase-1 event rates |
| io_uring deep telemetry (per-op timing, queue depth) | `lidenburg-telemetry-concept.md` §Gap 5 | Needs SSD tier (Phase-2 item 6) |
| Time-series CSV sampling (100ms snapshots) | `lidenburg-telemetry-concept.md` §Gap 8 | JSON-lines stream can be sampled by consumer |
| MoE routing distribution histograms | `telemetry-deepening.md` Phase A | Deferred |
| Anomaly detection (Welford's algorithm) | `telemetry-deepening.md` Phase E | Deferred |
| pulsar-diag CLI analysis modules | `telemetry-deepening.md` Phase F | Deferred |

## 10. Success Criteria

1. Branch rebased clean onto `preview-v0.4.7`; retargeted patches; merge-tree assessed.
2. CI canary green — `hash(applied) == hash(snapshotted)` for both PRs before compile.
3. Cache ported — `--moe-expert-cache` live, KVarN paths untouched, server starts on MoE model.
4. Telemetry live — `hit_rate[VRAM]`, `bytes_served[VRAM]` non-zero in stats output; per-layer breakdown printed.
5. **Telemetry remotely readable** — `GET /moe-cache/telemetry` returns JSON array; `GET /moe-cache/telemetry/stream` pushes SSE; both 404 when disabled.
6. RECONCILE adapter design doc delivered and approved as Phase-2 integration contract.

## 11. Tickets

See `docs/features/beellama-phase1/TICKETS.md`.
