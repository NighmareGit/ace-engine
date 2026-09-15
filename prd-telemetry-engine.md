# PRD: BeeLlama Telemetry & Tracing Engine

## Problem Statement

We run a dual-GPU Triton machine (RTX 3090 24GB + RTX 3070 8GB) with a BeeLlama.cpp (Anbeeld fork, v0.4.4) inference server hosting multiple model configurations (Qwen3.6-35B, Muse-Glimmer-30B, Laguna-XS, Qwen3.5-9B, GLM-4.7). We have a coder-harness benchmark platform that measures throughput (tok/s) and quality (judge-scored), but we have almost zero visibility into **what happens inside the inference pipeline** during a request.

Currently we know:
- Prompt tokens processed and speed (prompt_ms, prompt_per_second)
- Generation tokens and speed (predicted_ms, predicted_per_second)
- Draft model speculative acceptance rate (draft_n, draft_n_accepted)
- Cache hit data (cache_n, cache_lcp_n, cache_planned_n, cache_reprocessed_n, cache_source, cache_reason)
- GPU-level nvidia-smi snapshots (VRAM, temperature, utilization)

We do NOT know:
- **Layer-level compute times** — which layers are slow, where attention bottlenecks occur
- **KV cache internal state** — KVarN compression ratios, exact tail sizes, defragmentation events
- **Memory bandwidth utilization** — whether we're memory-bound or compute-bound
- **Pipeline stalls** — gaps between batch submissions, draft rejection storms, context shifts
- **Cross-GPU correlation** — what the 3070 is doing while the 3090 generates
- **CPU thread utilization** — with `-t 8` threads, are we saturating CPU?
- **Request queue depth** — are requests queuing behind each other?
- **Model loading times** — how long each config takes to load and become ready
- **Power and thermal trends** — are we approaching throttling during sustained inference?

Without this telemetry, we cannot:
1. Diagnose why Config-I (Qwen3.6-35B) achieves 201 tok/s but sometimes drops to 146 tok/s
2. Understand why DFlash draft acceptance varies from 64% to 92% across request types
3. Optimize KVarN cache settings (kvarn2 through kvarn8) with data-driven decisions
4. Detect memory pressure before OOM kills happen
5. Compare GPU utilization patterns between 3090 and 3070 configurations
6. Build a real-time monitoring dashboard for production inference

## Solution

Build a three-tier telemetry system that collects, stores, analyzes, and visualizes comprehensive inference pipeline data from the BeeLlama engine:

**Tier 1 — Data Collection Layer:** Python modules that poll BeeLlama API endpoints (/slots, /metrics), nvidia-smi, and SSH-extracted system stats. Collects per-request inference traces with full timing breakdowns, GPU state snapshots with power/clock/memory bandwidth, KV cache efficiency metrics, and pipeline event streams.

**Tier 2 — Storage & Processing Layer:** SQLite database with normalized schema for inference traces, layer timings, GPU snapshots, cache efficiency snapshots, pipeline events, and telemetry sessions. Includes derived metric computation (stall detection, memory pressure signals, thermal throttling alerts).

**Tier 3 — Analysis & Visualization Layer:** Post-hoc trace analysis (p50/p95/p99 latencies, throughput distributions), waterfall/timeline charts for request execution, HTML dashboard with real-time GPU status and historical comparisons, and configurable alert system for threshold-based warnings.

## User Stories

1. As a **ML engineer**, I want to see per-request inference traces showing prompt processing time, generation time, draft verification time, and cache lookup time, so that I can identify which phase of inference is the bottleneck
2. As a **ML engineer**, I want to see cache efficiency metrics (LCP contribution, reprocessing cost, hit rate) for each request, so that I can optimize KVarN cache settings
3. As a **ML engineer**, I want to track GPU memory bandwidth utilization over time, so that I can determine if inference is memory-bound or compute-bound
4. As a **ML engineer**, I want to see pipeline stall events (gaps between batch submissions, draft rejection storms) on a timeline, so that I can identify and fix throughput drops
5. As a **ML engineer**, I want to compare inference traces across different model configs (Config-I vs Config-H vs Config-G), so that I can make data-driven decisions about which config to use
6. As a **system administrator**, I want real-time GPU temperature, power draw, and VRAM monitoring with alerts when approaching thermal limits, so that I can prevent hardware throttling
7. As a **system administrator**, I want to track model loading times for each config swap, so that I can optimize the swap sequence
8. As a **system administrator**, I want cross-GPU correlation showing what both GPUs are doing simultaneously, so that I can detect resource contention
9. As a **developer**, I want waterfall charts showing the execution phases of each inference request, so that I can visually identify timing anomalies
10. As a **developer**, I want to query the SQLite telemetry database directly for custom analysis, so that I can answer ad-hoc questions
11. As a **developer**, I want a CLI tool to collect a single telemetry snapshot on demand, so that I can debug specific issues
12. As a **developer**, I want a continuous monitoring mode that polls all endpoints at configurable intervals, so that I can capture long-running trends
13. As a **developer**, I want the telemetry system to auto-cleanup old data beyond configurable retention limits, so that the database doesn't grow unbounded
14. As a **ML engineer**, I want to see KV cache defragmentation events correlated with latency spikes, so that I can tune defrag thresholds
15. As a **ML engineer**, I want to see per-slot utilization on the /slots endpoint over time, so that I can optimize the number of parallel server slots
16. As a **system administrator**, I want to detect OOM risk by monitoring VRAM headroom trends, so that I can preemptively reduce context size or switch configs
17. As a **developer**, I want to export telemetry data as JSON for external analysis tools, so that I can use specialized visualization software
18. As a **ML engineer**, I want to see draft model acceptance rates broken down by request type (short vs long, code vs text), so that I can optimize draft model selection
19. As a **developer**, I want configurable alert thresholds for temperature, VRAM pressure, throughput degradation, and stall duration, so that I can customize monitoring sensitivity
20. As a **ML engineer**, I want to see CPU thread utilization during inference, so that I can determine if thread count (-t flag) is optimal
21. As a **system administrator**, I want to see PCIe generation and width information for each GPU, so that I can verify optimal interconnect configuration
22. As a **developer**, I want the telemetry system to integrate with the existing coder-harness benchmark runner, so that benchmark runs automatically collect detailed telemetry
23. As a **ML engineer**, I want to see memory pressure events (VRAM utilization > 90%) correlated with throughput drops, so that I can understand memory-speed tradeoffs
24. As a **developer**, I want to see context shift events on the pipeline timeline, so that I can understand when the engine is forced to truncate history
25. As a **system administrator**, I want a self-contained HTML dashboard that works offline, so that I can share reports without deploying a web server
26. As a **ML engineer**, I want to track KVarN compression ratios over time, so that I can validate that compression is stable across different input patterns
27. As a **developer**, I want to see the full JSON dump of BeeLlama API timings for each request, so that I can debug new timing fields as they're added to the engine
28. As a **ML engineer**, I want to see slot-level queue depth (how many requests are waiting for a slot), so that I can determine if we need more parallel slots
29. As a **system administrator**, I want telemetry collection to add less than 5% overhead to inference throughput, so that monitoring doesn't significantly impact performance
30. As a **developer**, I want the telemetry schema to be forward-compatible with future BeeLlama telemetry extensions (per-layer timings, Prometheus metrics), so that we don't need schema migrations for every new feature

## Implementation Decisions

### Architecture: Three-Tier Pipeline

The system follows a **Collector → Storage → Analyzer** pipeline:

1. **Collectors** are stateless Python modules that poll external endpoints (BeeLlama API, nvidia-smi via SSH) and produce typed dataclass objects
2. **Storage** is SQLite with WAL mode, using a schema that extends the existing `schema_unified.py` baseline (adds 6 new tables: inference_traces, layer_timings, gpu_snapshots_enhanced, cache_efficiency_snapshots, pipeline_events, telemetry_sessions)
3. **Analyzers** are stateless functions that query SQLite and produce analysis results (statistics, alerts, dashboard HTML)

### API Surface: Existing BeeLlama Endpoints (No C++ Changes)

We use the **existing API surface** without requiring C++ modifications to the BeeLlama fork:

- **POST /v1/chat/completions** — Already returns extended `timings` object with: cache_n, cache_lcp_n, cache_planned_n, cache_reprocessed_n, cache_source, cache_reason, prompt_ms, predicted_ms, draft_n, draft_n_accepted, and full prompt/predicted throughput
- **GET /slots** — Already returns per-slot state (n_ctx, speculative flag, is_processing, n_prompt_tokens, params, next_token info)
- **GET /metrics** — Prometheus endpoint (needs `--metrics` flag enabled in docker-compose)
- **GET /health** — Health check

The `--perf` flag enables `llama_perf_context_data` (t_start_ms, t_load_ms, t_p_eval_ms, t_eval_ms) internally but this data is NOT yet exposed in the API response. When BeeLlama adds per-layer timings to the API, our schema already has `layer_timings` table ready.

### SSH Transport Pattern

All remote data collection uses the proven SSH pattern from `ssh_utils.py`:
```
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> {cmd}
```

nvidia-smi data comes via SSH. The existing `telemetry_collector.py` already has `ssh()` helper — we extend rather than replace it.

### Schema Extension Strategy

We extend `schema_unified.py` with a new `ensure_telemetry_schema()` function rather than modifying the existing unified schema. This keeps backward compatibility while adding the 6 new tables. The existing tables (work_sessions, work_events, gpu_snapshots, benchmark_results) remain unchanged.

### Module Organization

New modules in `coder-harness/`:
- `telemetry_schema.py` — DDL for new tables + migration (extends schema_unified.py)
- `telemetry_models.py` — Typed dataclasses for all telemetry types (TimingsData, GPUStats, InferenceTrace, PipelineEvent, CacheEfficiencySnapshot, TelemetrySession)
- `telemetry_config.py` — Centralized configuration (endpoints, intervals, thresholds, storage paths)
- `beellama_telemetry.py` — API response parsing and enrichment
- `slot_monitor.py` — /slots endpoint polling and state tracking
- `cache_analyzer.py` — Cache efficiency computation from extended timings
- `gpu_collector.py` — Enhanced GPU stats collection (nvidia-smi + derived metrics)
- `inference_tracer.py` — Wraps API calls to produce complete inference traces
- `pipeline_analyzer.py` — Stall detection, memory pressure identification, bubble analysis
- `memory_profiler.py` — VRAM tracking, fragmentation detection, KV cache growth monitoring
- `bandwidth_monitor.py` — PCIe/RAM bandwidth estimation from clock and memory deltas
- `trace_analyzer.py` — Post-hoc statistics (percentiles, distributions, outlier detection)
- `waterfall_renderer.py` — HTML/SVG waterfall chart generation
- `dashboard_v2.py` — Enhanced self-contained HTML dashboard
- `alert_manager.py` — Threshold-based alerting
- `telemetry_cli.py` — Unified CLI entry point
- `telemetry_integration.py` — Bridge to existing coder-harness benchmark runner

### Data Flow

```
BeeLlama API (/v1/chat/completions) ──→ beellama_telemetry.py ──→ InferenceTrace
BeeLlama API (/slots)               ──→ slot_monitor.py       ──→ slot state deltas
nvidia-smi (via SSH)                 ──→ gpu_collector.py      ──→ GPUStats
Derived from traces                  ──→ cache_analyzer.py     ──→ CacheEfficiencySnapshot
Derived from traces                  ──→ pipeline_analyzer.py  ──→ PipelineEvent list
All sources                          ──→ telemetry_schema.py   ──→ SQLite DB
SQLite queries                       ──→ trace_analyzer.py     ──→ statistics
SQLite queries                       ──→ waterfall_renderer.py  ──→ HTML charts
SQLite queries                       ──→ dashboard_v2.py       ──→ HTML dashboard
All analysis                         ──→ alert_manager.py      ──→ alerts
```

### Pipeline Event Types

The system tracks 19 discrete event types:
`batch_submit`, `batch_complete`, `draft_propose`, `draft_accept`, `draft_reject`, `cache_lookup`, `cache_miss`, `cache_hit`, `defrag_start`, `defrag_end`, `context_shift`, `slot_acquire`, `slot_release`, `kv_serialize`, `kv_deserialize`, `stall_detected`, `memory_pressure`, `thermal_warning`, `oom_risk`

### Stall Detection Algorithm

Pipeline stalls are detected by comparing actual inter-batch intervals against a rolling baseline:
1. Compute rolling mean and standard deviation of inter-batch intervals over last 100 batches
2. Flag intervals > mean + 3*stddev as stalls
3. Correlate stall events with GPU state (was GPU idle? was memory pressure high?)
4. Store stall events with full context in pipeline_events table

### Memory Pressure Detection

Memory pressure is detected by:
1. Monitoring VRAM utilization % over time
2. Flagging when utilization exceeds configurable threshold (default 90%)
3. Correlating with inference throughput drops
4. Tracking KV cache growth rate and predicting OOM timeline

### Dashboard Design

Self-contained HTML file (no external dependencies) with:
- Dark theme, monospace font (consistent with existing telemetry_dashboard.html)
- Section 1: GPU Status Panel (real-time temperature, VRAM, power, utilization for both GPUs)
- Section 2: Inference Waterfall Chart (SVG timeline showing prompt/draft/generate phases)
- Section 3: Throughput History (tok/s over time with config coloring)
- Section 4: Cache Efficiency Trends (hit rate, LCP contribution, reprocessing cost)
- Section 5: Pipeline Health (stall frequency, memory pressure events, thermal warnings)
- Section 6: Alert Log (recent threshold violations)
- Auto-refresh every 60 seconds

## Testing Decisions

- **Good test:** Validates external behavior (API response parsing, SQLite writes, dashboard HTML output) not implementation details
- **Modules tested:** telemetry_schema (table creation, migration), telemetry_models (serialization round-trips), beellama_telemetry (API parsing), cache_analyzer (math correctness), pipeline_analyzer (stall detection algorithm), gpu_collector (nvidia-smi parsing)
- **Prior art:** coder-harness has 131 existing tests across test_integration.py (62 tests, 14 classes) and test_prompts.py (69 tests). Follow same unittest patterns.
- **Mock strategy:** Mock SSH calls and HTTP responses for unit tests. Use real SQLite for integration tests.
- **Edge cases to test:** Empty API responses, missing timing fields, GPU not responding, database locked, concurrent collection cycles, maximum retention cleanup

## Out of Scope

1. **C++ changes to BeeLlama fork** — We use the existing API surface only. When per-layer timings are added to the API, our `layer_timings` table is ready but we don't implement the C++ side.
2. **CUDA event-level profiling** — Requiring ggml/llama.cpp core changes for per-operator GPU timing.
3. **nsys/nsight integration** — NVIDIA profiling tools for GPU timeline traces.
4. **Real-time WebSocket streaming** — Push-based telemetry delivery (future enhancement).
5. **Prometheus/Grafana deployment** — We collect from `/metrics` but don't deploy a monitoring stack.
6. **CPU thread profiling** — `/proc`-level thread utilization tracking (would need kernel-level tools).
7. **Cross-machine telemetry** — Only covers the Triton machine, not distributed inference.
8. **Cost analysis** — Power cost calculation, hardware depreciation tracking.
9. **Model quality correlation** — Linking telemetry data to judge scores (covered by existing benchmark pipeline).

## Further Notes

### Existing Data Gold Mine

The BeeLlama API already returns undocumented telemetry fields that we're not using:
- `cache_lcp_n` — longest common prefix tokens (prompt cache effectiveness)
- `cache_planned_n` — tokens planned for cache processing
- `cache_reprocessed_n` — tokens that had to be reprocessed (cache miss cost)
- `cache_source` — cache source type ("none", "prefix", "slot")
- `cache_reason` — why cache was used/not used ("no_common_prefix", etc.)

These fields provide deep insight into KV cache behavior and should be the primary focus of early telemetry collection.

### BeeLlama Flags to Enable

The following flags should be added to docker-compose.yml to enable full telemetry:
- `--metrics` — Enables Prometheus-compatible metrics endpoint at `/metrics`
- `--perf` — Enables internal libllama performance timings
- `--slots` (already enabled by default) — Exposes per-slot monitoring endpoint

### Integration with Existing System

The telemetry system extends (does not replace) the existing:
- `telemetry_collector.py` — GPU snapshot collection (we add enhanced snapshots)
- `telemetry_dashboard.py` — HTML dashboard (we create dashboard_v2.py as replacement)
- `schema_unified.py` — SQLite schema (we add new tables via separate ensure function)
- `benchmark-results.db` — Benchmark data (telemetry traces reference config_ids from model_configs)
