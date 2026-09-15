# ACE Watch — Live Engine Execution with Dashboard

> **Legacy flow** — this skill orchestrates the engine via `harness.py ace run` (deprecated). For current ACE operation read [`coder-harness/ACE-RUNBOOK.md`](ACE-RUNBOOK.md) first. The live CLI is `python3 engine/cli.py ace run ...`.

## Overview

Run a PRD through the coder-harness engine with live dashboard visibility. The operator watches task progress, inference metrics, GPU temperatures, and quality gates in real-time.

## Trigger Conditions

- User asks to "run a PRD" or "execute a PRD" or "run the engine"
- User says `/ace-watch <prd> --project <path>`
- User wants to watch an engine execution in real-time

## Input Variables

- `PRD_PATH` — Path to the PRD markdown file
- `PROJECT_PATH` — Path to the project directory
- `CONFIG` — Model config ID (default: 3090-qwen36-35b)
- `TRITON_HOST` — Triton SSH host (default: <LAN_IP>)
- `STREAMING_PORT` — Streaming server port (default: 3081)
- `ENGINE_PORT` — Engine service port (default: 3082)

## Execution Protocol

### Phase 1: Pre-Flight

1. Check streaming server health:
   ```bash
   curl -sf http://<LAN_IP>:3081/health
   ```
   If not running, start it:
   ```bash
   ssh <user>@<LAN_IP> 'cd ~/projects/coder-harness && nohup python3 streaming_server.py --port 3081 --host 0.0.0.0 --token "streaming-secret-2024" > /tmp/streaming-server.log 2>&1 &'
   ```
   Wait 2s, verify health.

2. Check GPU telemetry poller:
   ```bash
   ssh <user>@<LAN_IP> 'pgrep -f gpu_telemetry'
   ```
   If not running, start it:
   ```bash
   ssh <user>@<LAN_IP> 'cd ~/projects/coder-harness && nohup python3 gpu_telemetry.py --url http://127.0.0.1:3081 --token "streaming-secret-2024" --interval 5 > /tmp/gpu-telemetry.log 2>&1 &'
   ```

3. Check BeeLlama health:
   ```bash
   ssh <user>@<LAN_IP> 'curl -sf http://localhost:8080/health'
   ```

4. Report status to session and operator.

### Phase 2: Deploy

1. Inject welcome annotation into dashboard:
   ```bash
   curl -s -X POST http://<LAN_IP>:3081/api/inject \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer <token>" \
     -d '{"type":"system.annotation","data":{"text":"🚀 Session started: running [PRD_TITLE]","style":"success"}}'
   ```

2. Print dashboard URL to operator:
   ```
   📺 Dashboard: http://<LAN_IP>:3081/
   ```

### Phase 3: Run

Execute the PRD:
```bash
# Legacy command (harness.py — deprecated):
# ssh <user>@<LAN_IP> 'cd ~/projects/coder-harness && \
#   STREAMING_SERVER_URL=http://127.0.0.1:3081 \
#   STREAMING_TOKEN=streaming-secret-2024 \
#   PYTHONUNBUFFERED=1 python3 harness.py ace run \
#     --project ~/projects/PROJECT_PATH PRD_PATH'

# Live command (engine/cli.py — preferred):
ssh <user>@<LAN_IP> 'cd ~/projects/coder-harness && \
  STREAMING_SERVER_URL=http://127.0.0.1:3081 \
  STREAMING_TOKEN=streaming-secret-2024 \
  PYTHONUNBUFFERED=1 python3 engine/cli.py ace run \
    --prd PRD_PATH --project ~/projects/PROJECT_PATH'
```

### Phase 4: Observe

Monitor progress by checking engine logs:
```bash
ssh <user>@<LAN_IP> 'tail -5 /tmp/engine-run.log 2>/dev/null'
```

And querying streaming events:
```bash
curl -sf "http://<LAN_IP>:3081/events/recent?limit=5" | python3 -m json.tool
```

Report milestones to the session.

### Phase 5: Complete

Report final results:
- Tasks completed / total
- Average tok/s
- Total duration
- Any failures

Ask operator: keep dashboard running or shut down?

## User Instruction Translation

When the user asks the agent to do something with the dashboard, translate to API calls:

| User Says | Agent Action |
|-----------|-------------|
| "Highlight T03" | POST /api/config with highlight_tasks=["T03"] |
| "Show GPU temps" | POST /api/panel with GPU telemetry data |
| "Add notes panel" | POST /api/panel with user's content |
| "Focus on engine" | POST /api/config with focus_source="engine" |
| "Clear highlights" | POST /api/config with highlight_tasks=[] |

## Error Handling

- Streaming server not starting: check port 3081, check logs at /tmp/streaming-server.log
- Engine service unreachable: check port 3082, check engine PID
- BeeLlama not healthy: model may be loading (wait 60s) or crashed (check docker logs)
- SSE disconnect: dashboard auto-reconnects; if persistent, restart streaming server
