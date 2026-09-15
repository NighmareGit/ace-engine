# Agent Guide: ACE Watch Engine Tools

## Quick Start

When the user asks you to run a PRD or use the engine, follow these steps:

1. **Check status**: Run `python3 enginectl.py status` to see what's running
2. **Start services if needed**: 
   - `python3 enginectl.py stream start --token streaming-secret-2024`
   - Start GPU poller via SSH
3. **Run the PRD**: `python3 enginectl.py run <prd> --project <path> --config <id>`
4. **Monitor progress**: Check logs and query events
5. **Report to user**: Share results and dashboard URL

## Tool Reference

### enginectl.py

All commands output JSON. Parse the output to make decisions.

| Command | Returns | Use When |
|---------|---------|----------|
| `status` | Full system status | Initial check, before any operation |
| `run <prd> --project <p>` | run_id + log_path | User asks to execute a PRD |
| `parse <prd>` | Task list JSON | User wants to see tasks before running |
| `gpu` | GPU temperatures + VRAM | User asks about GPU status |
| `model list` | Available configs | User asks what models are available |
| `model swap <config>` | Success/failure | User wants to switch models |
| `telemetry query --type <t>` | Event list | User asks about past events |
| `stream start/stop/status` | Service lifecycle | Managing the streaming server |
| `dashboard` | Dashboard URL | User wants to watch the dashboard |

### Dashboard API (via HTTP)

Base URL: `http://<LAN_IP>:3081`
Auth: `Authorization: Bearer <token>`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/inject` | POST | Inject events (annotations, panels, highlights) |
| `/api/config` | GET/POST | Read/write dashboard configuration |
| `/api/panel` | POST | Add/update custom panels |
| `/api/panel/{id}` | DELETE | Remove custom panels |
| `/stream` | GET | SSE event stream |
| `/health` | GET | Server health |

### Translating User Instructions to Dashboard Actions

When the user says something that should change what the dashboard shows:

**"Highlight task T03"**
```bash
curl -X POST http://<LAN_IP>:3081/api/config \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"highlight_tasks": ["T03"]}'
```

**"Show GPU temperatures as a panel"**
```bash
# First query GPU status
GPU_DATA=$(python3 enginectl.py gpu)
# Then inject as panel
curl -X POST http://<LAN_IP>:3081/api/panel \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"title": "GPU Status", "content": "3090: 62°C, 18.4GB VRAM\n3070: 48°C, 6.2GB VRAM", "position": "bottom"}'
```

**"Add a notes panel with my observations"**
```bash
curl -X POST http://<LAN_IP>:3081/api/panel \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"title": "User Notes", "content": "The user'\''s observations here...", "position": "bottom"}'
```

**"Focus on the engine stream"**
```bash
curl -X POST http://<LAN_IP>:3081/api/config \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"focus_source": "engine"}'
```

**"Clear all highlights"**
```bash
curl -X POST http://<LAN_IP>:3081/api/config \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"highlight_tasks": []}'
```

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| Connection refused on port 3081 | Streaming server not running | `enginectl.py stream start` |
| Connection refused on port 8080 | BeeLlama not running | Check Docker on Triton |
| 401 Unauthorized | Wrong/missing auth token | Check STREAMING_TOKEN env var |
| GPU not found in nvidia-smi | Driver issue or GPU crashed | Check `nvidia-smi` directly |
| Engine process exited | PRD processing failed | Check log file at /tmp/ace-watch-*.log |
| SSE disconnect | Network issue | Dashboard auto-reconnects; restart if persistent |

## Common Workflows

### Run a PRD and watch it
1. `enginectl.py status` — check everything is healthy
2. `enginectl.py run my-prd.md --project my-project` — start execution
3. Open `http://<LAN_IP>:3081/` in browser — watch live
4. Poll status periodically and report to user

### Compare two configs
1. `enginectl.py model list` — show available configs
2. Run PRD with config A, record results
3. `enginectl.py model swap <config-b>` — switch model
4. Run PRD with config B, record results
5. Inject comparison panel: `POST /api/panel`

### Debug a failed task
1. `enginectl.py telemetry query --type "task.fail"` — find failures
2. `enginectl.py telemetry query --type "engine.*" --since <seq>` — get context
3. Inject annotation: `POST /api/inject` with error details
