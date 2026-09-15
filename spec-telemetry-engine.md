# Specification: BeeLlama Telemetry & Tracing Engine

## Overview

This specification defines the technical implementation details for the BeeLlama Telemetry & Tracing Engine, extending the coder-harness benchmark platform with comprehensive inference pipeline observability.

## 1. Schema Extension (`telemetry_schema.py`)

### New Tables

#### `inference_traces`
Per-request trace capturing the complete lifecycle of an inference call.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| request_id | TEXT UNIQUE | BeeLlama chatcmpl-* ID |
| config_id | TEXT | Model config ID (FK to model_configs) |
| started_at | TEXT | ISO datetime |
| completed_at | TEXT | ISO datetime |
| status | TEXT | running/complete/failed |
| prompt_tokens | INTEGER | Input token count |
| prompt_ms | REAL | Prompt processing time |
| prompt_per_second | REAL | Prompt throughput |
| predicted_tokens | INTEGER | Output token count |
| predicted_ms | REAL | Generation time |
| predicted_per_second | REAL | Generation throughput |
| draft_n | INTEGER | Draft proposals made |
| draft_n_accepted | INTEGER | Draft proposals accepted |
| draft_acceptance_rate | REAL | Computed ratio |
| cache_n | INTEGER | Tokens served from cache |
| cache_lcp_n | INTEGER | Longest common prefix tokens |
| cache_planned_n | INTEGER | Tokens planned for cache |
| cache_reprocessed_n | INTEGER | Tokens reprocessed (cache miss) |
| cache_source | TEXT | Cache source type |
| cache_reason | TEXT | Cache reason string |
| gpu_temperature | REAL | GPU temp at request time |
| gpu_vram_used_mb | REAL | VRAM usage at request time |
| gpu_utilization_pct | REAL | GPU utilization at request time |
| gpu_power_watts | REAL | GPU power draw at request time |
| total_tokens | INTEGER | prompt + predicted |
| total_ms | INTEGER | prompt + predicted |
| tokens_per_ms | REAL | predicted_tokens / predicted_ms |
| model_name | TEXT | Model identifier |
| model_path | TEXT | Full GGUF path |
| context_size | INTEGER | Configured context size |
| kvarn_level | TEXT | KVarN cache level |
| thinking_tokens | INTEGER | Reasoning tokens (thinking models) |
| visible_tokens | INTEGER | Visible output tokens |
| raw_timings_json | TEXT | Full API timings JSON |
| raw_usage_json | TEXT | Full API usage JSON |

#### `layer_timings`
Per-layer compute timing (populated when BeeLlama exposes per-layer data via API).

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| trace_id | INTEGER FK | → inference_traces.id |
| layer_index | INTEGER | Layer number (0-based) |
| layer_type | TEXT | attention/ffn/norm/embed |
| compute_ms | REAL | Compute time |
| memory_ms | REAL | Memory operation time |
| gpu_memory_bytes | INTEGER | Memory used by layer |
| timestamp | TEXT | ISO datetime |

#### `gpu_snapshots_enhanced`
Extended GPU telemetry beyond basic nvidia-smi.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| gpu_index | INTEGER | GPU device index |
| timestamp | TEXT | ISO datetime |
| name | TEXT | GPU model name |
| memory_used_mb | REAL | VRAM used |
| memory_total_mb | REAL | Total VRAM |
| temperature_c | REAL | Temperature |
| utilization_gpu_pct | REAL | GPU compute utilization |
| utilization_memory_pct | REAL | Memory controller utilization |
| power_draw_watts | REAL | Current power draw |
| power_limit_watts | REAL | Power limit |
| clock_sm_mhz | REAL | SM clock speed |
| clock_mem_mhz | REAL | Memory clock speed |
| process_count | INTEGER | GPU process count |
| process_vram_mb | REAL | Per-process VRAM |
| memory_bandwidth_utilization_pct | REAL | Estimated BW utilization |
| thermal_throttling_detected | INTEGER | Boolean flag |
| pcie_gen | INTEGER | PCIe generation |
| pcie_width | INTEGER | PCIe lane width |

#### `cache_efficiency_snapshots`
Periodic cache performance aggregation.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| snapshot_time | TEXT | ISO datetime |
| config_id | TEXT | Model config |
| total_requests | INTEGER | Requests in window |
| cache_hit_count | INTEGER | Cache hits |
| cache_hit_rate | REAL | hit_count / total |
| avg_lcp_tokens | REAL | Average LCP tokens |
| avg_reprocessed_tokens | REAL | Average reprocessed |
| kv_used_bytes | INTEGER | KV cache used |
| kv_capacity_bytes | INTEGER | KV cache capacity |
| kv_utilization_pct | REAL | KV utilization |
| kvarn_compressed_bytes | INTEGER | KVarN compressed size |
| kvarn_exact_tail_bytes | INTEGER | Exact tail size |
| kvarn_compression_ratio | REAL | Compression ratio |
| raw_kv_stats_json | TEXT | Full KV stats JSON |

#### `pipeline_events`
Discrete inference pipeline events (19 types).

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| trace_id | INTEGER FK | → inference_traces.id |
| event_type | TEXT | One of 19 event types |
| event_data | TEXT | JSON payload |
| duration_ms | REAL | Event duration |
| timestamp | TEXT | ISO datetime |

#### `telemetry_sessions`
Collection session metadata.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| session_name | TEXT | Session identifier |
| config_id | TEXT | Active model config |
| started_at | TEXT | ISO datetime |
| ended_at | TEXT | ISO datetime |
| total_traces | INTEGER | Traces collected |
| total_snapshots | INTEGER | GPU snapshots collected |
| total_events | INTEGER | Pipeline events collected |
| status | TEXT | active/completed |
| notes | TEXT | Free-text notes |

### Indexes

```sql
CREATE INDEX idx_traces_config ON inference_traces(config_id);
CREATE INDEX idx_traces_status ON inference_traces(status);
CREATE INDEX idx_traces_started ON inference_traces(started_at);
CREATE INDEX idx_layer_trace ON layer_timings(trace_id);
CREATE INDEX idx_gpu_time ON gpu_snapshots_enhanced(timestamp);
CREATE INDEX idx_pipeline_trace ON pipeline_events(trace_id);
CREATE INDEX idx_pipeline_type ON pipeline_events(event_type);
```

## 2. Data Models (`telemetry_models.py`)

### `TimingsData`
Parses BeeLlama API `timings` object. Properties: `draft_acceptance_rate`, `total_tokens`, `total_ms`, `cache_efficiency`. Factory method: `from_api_response(dict)`.

### `UsageData`
Parses BeeLlama API `usage` object. Extracts: completion_tokens, prompt_tokens, cached_tokens, reasoning_tokens, visible_tokens.

### `GPUStats`
Parses nvidia-smi output. Properties: `memory_utilization_pct`, `thermal_throttling_detected`. Factory method: `from_nvidia_smi(dict)`.

### `InferenceTrace`
Complete trace of one inference request. Composes TimingsData + UsageData + optional GPUStats. Factory method: `from_api_response(response, config_id, gpu_stats)`.

### `PipelineEvent`
Discrete event with type, JSON data payload, duration.

### `CacheEfficiencySnapshot`
Aggregated cache metrics over a time window.

### `TelemetrySession`
Collection session tracking metadata.

## 3. Configuration (`telemetry_config.py`)

### `BeeLlamaEndpoints`
- base_url: http://localhost:8080
- slots_path: /slots
- metrics_path: /metrics
- base_url_3070: http://localhost:8082

### `CollectionConfig`
- poll_interval_seconds: 5
- inference_sample_rate: 1.0
- slot_poll_interval_seconds: 2
- cache_snapshot_interval_seconds: 30
- max_traces_retained: 10000
- max_snapshots_retained: 50000
- max_events_retained: 100000

### `AlertThresholds`
- temperature_warning_c: 75.0
- temperature_critical_c: 83.0
- vram_pressure_pct: 90.0
- throughput_degradation_pct: 30.0
- stall_duration_ms: 100.0
- draft_acceptance_min: 0.3
- cache_hit_rate_min: 0.1

### Config Priority: explicit file → TELEMETRY_CONFIG env var → env overrides → defaults

## 4. Collector Modules

### `beellama_telemetry.py`
- `parse_chat_response(response_json, config_id, gpu_stats)` → InferenceTrace
- `enrich_trace(trace, gpu_stats)` → InferenceTrace (adds GPU context)

### `slot_monitor.py`
- `poll_slots(base_url)` → list[dict] (raw slot data)
- `track_slot_changes(prev_slots, curr_slots)` → list[PipelineEvent] (state transitions)

### `cache_analyzer.py`
- `compute_cache_efficiency(traces: list[InferenceTrace])` → CacheEfficiencySnapshot
- `detect_cache_anomalies(snapshots: list[CacheEfficiencySnapshot])` → list[PipelineEvent]

### `gpu_collector.py`
- `collect_gpu_stats(ssh_host, gpu_indices)` → list[GPUStats]
- `detect_thermal_throttling(stats)` → list[PipelineEvent]
- `estimate_memory_bandwidth(stats)` → float (GB/s estimated)

### `inference_tracer.py`
- `trace_request(beellama_url, messages, config_id, gpu_stats_fn)` → InferenceTrace
- `trace_batch(requests, beellama_url, config_id, gpu_stats_fn)` → list[InferenceTrace]

### `pipeline_analyzer.py`
- `detect_stalls(events: list[PipelineEvent], threshold_ms=100)` → list[PipelineEvent]
- `detect_memory_pressure(gpu_snapshots: list[GPUStats], threshold_pct=90)` → list[PipelineEvent]
- `detect_draft_storms(traces: list[InferenceTrace], acceptance_threshold=0.3)` → list[PipelineEvent]

### `memory_profiler.py`
- `track_vram_trends(snapshots: list[GPUStats])` → dict (growth rate, fragmentation signals)
- `predict_oom_timeline(snapshots: list[GPUStats])` → dict (estimated time to OOM)

### `bandwidth_monitor.py`
- `estimate_pcie_bandwidth(prev_stats, curr_stats)` → dict (read/write bandwidth estimates)
- `correlate_bandwidth_throughput(bandwidth_data, throughput_data)` → dict (correlation analysis)

## 5. Analysis Modules

### `trace_analyzer.py`
- `compute_statistics(traces: list[InferenceTrace])` → dict (p50/p95/p99, mean, stddev for each metric)
- `compare_configs(db_path, config_ids)` → dict (per-config comparison)
- `detect_outliers(traces, sigma=3)` → list[InferenceTrace]

### `waterfall_renderer.py`
- `render_waterfall_html(trace: InferenceTrace)` → str (HTML with inline SVG)
- `render_batch_waterfall(traces: list[InferenceTrace])` → str (multiple traces stacked)

### `dashboard_v2.py`
- `generate_dashboard(db_path, output_path)` → str (path to generated HTML)
- Sections: GPU Status, Waterfall, Throughput History, Cache Trends, Pipeline Health, Alerts

### `alert_manager.py`
- `check_alerts(db_path, thresholds: AlertThresholds)` → list[dict] (active alerts)
- `format_alert(alert)` → str (human-readable alert message)

## 6. Integration Points

### `telemetry_cli.py`
Commands:
- `collect` — Single collection cycle (all endpoints)
- `monitor --interval N` — Continuous collection
- `analyze --trace-id N` — Analyze single trace
- `dashboard [--output path]` — Generate HTML dashboard
- `alerts` — Check current alerts
- `export --output path` — Export all data as JSON
- `status` — Show collection status
- `cleanup` — Remove old data beyond retention limits

### `harness.py` Integration
New subcommands:
- `telemetry collect` → telemetry_cli collect
- `telemetry monitor` → telemetry_cli monitor
- `telemetry dashboard` → telemetry_cli dashboard
- `telemetry alerts` → telemetry_cli alerts

### `telemetry_integration.py`
- `auto_collect_during_benchmark(runner, config_id)` — Wraps benchmark runner to auto-collect telemetry
- `correlate_with_scores(traces, judge_scores)` — Links telemetry metrics to quality scores

## 7. SSH Transport

All remote data collection via:
```python
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> {cmd}
```

Key SSH commands:
- `nvidia-smi --query-gpu=... --format=csv,noheader` — GPU stats
- `nvidia-smi dmon -s pcm -c 1` — Power/clock/memory detailed
- `nvidia-smi --query-compute-apps=... --format=csv,noheader` — Process info
- `curl -sf http://localhost:8080/slots` — Slot state (executed on Triton)
- `curl -sf http://localhost:8080/metrics` — Prometheus metrics (executed on Triton)

## 8. Storage Configuration

- Default DB: `~/coder-harness-telemetry.db` (shared with existing telemetry_collector)
- WAL mode enabled for concurrent read/write
- Auto-vacuum every 24 hours
- Retention: 10K traces, 50K snapshots, 100K events
- Cleanup runs on each `collect` cycle
