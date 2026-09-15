# enginectl Command Reference

## Status
enginectl status
→ JSON: engine, streaming, gpu, model, beellama status

## Stream Control
enginectl stream start [--port 3081] [--token TOKEN]
enginectl stream stop
enginectl stream status

## Dashboard
enginectl dashboard
→ JSON: {url, demo_url, api_base}

## Run (Wave 2)
enginectl run <prd> --project <path> --config <id>
→ JSON: {run_id, status}

## Parse (Wave 2)
enginectl parse <prd>
→ JSON: {tasks: [{id, title, description, files}]}

## Model (Wave 3)
enginectl model list
enginectl model swap <config>
enginectl model status

## GPU
enginectl gpu
→ JSON: {gpus: [{index, name, temp_c, vram_used, vram_total}]}

## Telemetry (Wave 3)
enginectl telemetry query --type <event> --since <time> --limit <n>
enginectl telemetry export --format json
