# Streaming Dashboard Gap Fix Plan

## Gap Audit

### GAP 1: Inference event field name mismatch
- **Engine emits:** `tok_s`, `prompt_ms`, `gen_ms`
- **Dashboard expects:** `tokens_per_sec`, `tps`, `throughput`
- **Impact:** Inference card shows nothing — tok/s never updates
- **Fix:** Add `tokens_per_sec` alias to the emission in `code_generator.py`

### GAP 2: No GPU snapshot events
- **Dashboard expects:** `gpu.snapshot` events with `gpu_3090: {temp, vram_used_mb, vram_total_mb}`, `gpu_3070: {...}`
- **Engine emits:** Nothing — `telemetry_collector.py` collects GPU data via SSH but doesn't POST to streaming server
- **Impact:** GPU cards show no data, sparklines empty
- **Fix:** Create `gpu_telemetry.py` — a lightweight background poller that queries nvidia-smi and emits `gpu.snapshot` events to the streaming server every 5s

### GAP 3: No cache.update events
- **Dashboard expects:** `cache.update` events with `hit_rate`
- **Engine emits:** Nothing — BeeLlama returns cache data in inference response timings but it's not emitted
- **Impact:** Cache card shows nothing
- **Fix:** Emit cache data from `code_generator.py` inference.complete (BeeLlama already returns `cache_n`, `cache_lcp_n`)

### GAP 4: DSH adapter not auto-started
- **Dashboard expects:** DSH events in left pane
- **Reality:** `dsh_adapter.py` exists but must be started manually
- **Impact:** Left pane always empty
- **Fix:** Add `--with-dsh` flag to streaming server that auto-starts the DSH adapter, or make it a separate systemd service

### GAP 5: Command bar sends to wrong endpoint
- **Dashboard:** Sends POST to `window.ENGINE_SERVICE_URL || '/exec'`
- **Problem:** `/exec` is on engine_service.py (port 3082), not streaming_server.py (port 3081). Dashboard served from 3081 can't reach 3082 cross-origin.
- **Impact:** Command bar silently fails
- **Fix:** Add `/exec` proxy endpoint to streaming_server.py that forwards to engine_service.py

### GAP 6: Task progress counter inaccurate
- **Dashboard:** `taskProgress.total` only set from `task.start` events with `data.total`
- **Engine:** `task.start` emits `{task_id, title, description}` — no `total` field
- **Impact:** Task counter shows "0/N" or wrong total
- **Fix:** Add `total_tasks` to pipeline.start event and have dashboard read it

### GAP 7: `task_id: None` in inference/quality events
- **Engine:** `code_generator.py` and `quality_gates.py` emit `task_id: None`
- **Dashboard:** Can't correlate inference events to specific tasks
- **Impact:** Minor — task-level events have task_id, inference events don't
- **Fix:** Pass task_id through from task_queue.py → code_generator.generate() → _run_inference()

## Implementation Tasks (Dependency Order)

### Wave 1: Dashboard + Field Alignment (no dependencies)
- **T1:** Fix inference event field names in `code_generator.py` — add `tokens_per_sec` alias
- **T2:** Fix dashboard to also accept `tok_s` as alias for `tokens_per_sec`
- **T3:** Fix `task.start` event to include `total_tasks` from pipeline context
- **T4:** Fix dashboard to read `total_tasks` from `pipeline.start` event

### Wave 2: GPU Telemetry (depends on nothing)
- **T5:** Create `gpu_telemetry.py` — background GPU poller that emits `gpu.snapshot` events
- **T6:** Add `--with-gpu-poll` flag to streaming_server.py that starts GPU poller

### Wave 3: Cache + Command Proxy (depends on Wave 1)
- **T7:** Emit `cache.update` events from `code_generator.py` using BeeLlama cache timing data
- **T8:** Add `/exec` proxy endpoint to `streaming_server.py` that forwards to engine_service

### Wave 4: DSH Auto-Start (depends on nothing)
- **T9:** Add `--with-dsh` flag to streaming_server.py that auto-starts DSH adapter

### Wave 5: Task ID Threading (depends on Wave 1)
- **T10:** Thread `task_id` through `task_queue.py` → `code_generator.generate()` → `_run_inference()`

### Wave 6: Tests + Verification
- **T11:** Update test_streaming.py with new event format assertions
- **T12:** Run full test suite, deploy to Triton, live verification
