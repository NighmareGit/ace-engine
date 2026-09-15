# Streaming Event Schema

## Event Envelope

Every event follows this structure:

```json
{
  "id": "01HX7K2M...",
  "seq": 42,
  "time": 1735689600000,
  "source": "engine|dsh|system",
  "type": "event.type.name",
  "data": {}
}
```

## Engine Events

| Type | Data Fields | Emitted By |
|------|-------------|------------|
| `pipeline.start` | prd_title, total_tasks | task_queue.py |
| `pipeline.complete` | total, completed, failed, duration_s | task_queue.py |
| `task.start` | task_id, title, description | task_queue.py |
| `task.complete` | task_id, duration_s | task_queue.py |
| `task.fail` | task_id, error | task_queue.py |
| `inference.start` | task_id, port, model | code_generator.py |
| `inference.complete` | task_id, tok_s, tokens_per_sec, tokens, prompt_ms, gen_ms | code_generator.py |
| `quality.result` | task_id, overall, details | quality_gates.py |
| `test.result` | passed, failed, total, duration_s | test_runner.py |
| `git.commit` | message, stdout | git_workflow.py |
| `cache.update` | hit_rate, cache_n, cache_lcp_n | code_generator.py |
| `gpu.snapshot` | gpu_3090: {temp, vram_used_mb, vram_total_mb}, gpu_3070: {...} | gpu_telemetry.py |

## DSH Events

| Type | Data Fields |
|------|-------------|
| `dsh.turn.start` | turn |
| `dsh.turn.end` | turn, reason, duration |
| `dsh.tool.call` | callId, name, arguments |
| `dsh.tool.result` | callId, name, isError, duration |
| `dsh.assistant.chunk` | chunkType, content |
| `dsh.assistant.message` | content |

## System Events (Agent-Injected)

| Type | Data Fields | Dashboard Effect |
|------|-------------|-----------------|
| `system.annotation` | text, style (info/warning/error/success) | Colored banner |
| `system.panel` | id, title, content, position, created_by | Custom panel |
| `system.panel_removed` | id | Remove panel |
| `system.highlight` | task_ids[], color | Highlight task rows |
| `system.focus` | source (dsh/engine/both) | Dim/emphasize panes |
| `system.command` | command, status, result | Command status in header |
| `system.config_changed` | (full config) | Live config update |
