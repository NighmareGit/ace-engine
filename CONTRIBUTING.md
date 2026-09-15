# Contributing to Coder-Harness

Guide for contributors to the coder-harness benchmark platform.

---

## Development Setup

### Requirements

- Python 3.10+ (stdlib only — no pip dependencies)
- SSH keys to Triton machine configured
- BeeLlama containers running on Triton

### Quick Start

```bash
cd coder-harness

# Validate schema loads
python3 -c "import sqlite3; exec(open('schema.sql').read())"

# Dry-run GPU fit check
python3 gpu_fit.py --dry-run

# List model configs
python3 -c "import json; manifest = json.load(open('models/manifest.json')); print(manifest)"
```

### No Dependencies

All modules use only Python standard library:

- `sqlite3` — persistence
- `subprocess` — SSH execution
- `json` — serialization
- `logging` — structured logging
- `dataclasses` — typed structures
- `argparse` — CLI parsing
- `ast` — Python source analysis (generate_readme.py)
- `html` — HTML generation (telemetry_dashboard.py)
- `uuid` — Session UUID generation (work_engine.py)
- `re` — Regular expressions (throughout)

---

## Code Conventions

### Dataclasses for Structured Data

Every module uses dataclasses for structured data. No raw dicts for inter-module
communication.

```python
@dataclass
class ModelConfig:
    id: str
    gpu: str
    model_path: str
    port: int
    context_size: int
    kvarn_level: str | None
    speculative_type: str | None = None
```

### SSH via subprocess + sshpass

SSH to Triton uses the subprocess + sshpass pattern, NOT paramiko.

```python
subprocess.run([
    "sshpass", "-p", password,
    "ssh", "-o", "StrictHostKeyChecking=no",
    f"<user>@{host}", command
], capture_output=True, text=True)
```

### SQLite for Persistence

- File-based SQLite, never `:memory:` for multi-module tests
- Schema in `schema.sql`, applied via `exec(open('schema.sql').read())`
- All queries use parameterized statements

### Logging

Use the Python logging module. Configure at module level:

```python
import logging
log = logging.getLogger(__name__)
```

### CLI via argparse

All entry points use argparse with subcommands:

```python
parser = argparse.ArgumentParser(description="Tool name")
subparsers = parser.add_subparsers(dest="command")
# subcommands here
```

---

## Testing

### Import Checks

Validate a module imports cleanly:

```bash
python3 -c "from models import ModelConfig; print('ok')"
python3 -c "from gpu_fit import GPUFitProbe; print('ok')"
```

### Schema Validation

Verify the SQLite schema loads without errors:

```bash
python3 -c "import sqlite3; exec(open('schema.sql').read())"
```

### Dry-Run Mode

Several tools support dry-run mode:

```bash
python3 gpu_fit.py --dry-run          # Check fit without querying Triton
python3 orchestrate.py --dry-run      # Plan runs without executing
```

### Running a Pilot

After adding a new config, run a pilot to validate methodology:

```bash
python3 orchestrate.py --config new-config --dry-run
```

---

## Config ID ↔ Docker Profile Mapping

The Python CLI and Docker CLI use different identifiers for the same configs.
Manifest IDs (used by `orchestrate.py`, `runner.py`, etc.) look like `3090-qwen36-35b`,
while Docker profile names (used by `docker compose --profile`) look like `config-i`.
They are different identifiers for the same underlying configuration.

| Manifest Config ID | Docker Profile |
|---|---|
| `3090-qwen36-35b` | `config-i` |
| `3090-qwen35-9b` | `config-h-qwen35` |
| `3090-muse-glimmer` | `config-h` |
| `3090-laguna-xs` | `config-g-laguna` |
| `3070-qwen35-9b` | *(shared 3070 service)* |
| `3070-qwen35-4b` | *(shared 3070 service)* |

When adding a new config, you need to update **both** `models/manifest.json` (for the
Python CLI) **and** `tickets/deploy/docker-compose.yml` (for Docker deployment).

---

## Adding New Model Configs

1. Add entry to `models/manifest.json` with required fields
2. Create `.env` file in `tickets/deploy/configs/`
3. Add profile to `docker-compose.yml` on Triton
4. Run `gpu_fit.py` to validate VRAM fit
5. Run pilot to validate methodology

### Manifest Entry Format

```json
{
  "id": "new-model-3090-4bit",
  "gpu": "rtx3090",
  "model_path": "/models/new-model-q4_k_m.gguf",
  "port": 8082,
  "context_size": 8192,
  "kvarn_level": 4,
  "speculative_type": null
}
```

---

## Adding New Benchmark Tasks

1. Add entry to `corpus.json` with id, role, category, prompt, scoring_criteria
2. Add `automated_check` for executable tasks
3. Update `rubric_anchors.md` if new scoring dimensions are needed

### Task Entry Format

```json
{
  "id": "task-001",
  "role": "coder",
  "category": "implementation",
  "prompt": "Implement a function that...",
  "scoring_criteria": ["correctness", "readability", "efficiency"],
  "automated_check": "python3 -c 'assert ...'"
}
```

---

## Module Reference

### Core Platform (original)

| Module | Lines | Purpose |
|--------|-------|---------|
| `orchestrate.py` | 274 | Main CLI entry — sequences the full benchmark pipeline |
| `runner.py` | 501 | Core benchmark runner — inference + SQLite logging |
| `judge.py` | 417 | Automated scoring via judge model |
| `gpu_fit.py` | 524 | GPU fit matrix probe (configs × context sizes) |
| `pilot.py` | 516 | Methodology pilot (5 tasks × 3 configs × 3 reps) |
| `report.py` | 591 | Results reporter — tables, scatter plots, recommendations |
| `preflight.py` | 174 | Pre-flight health check (7-point) |
| `watchdog.py` | 158 | GPU watchdog — monitors temps, triggers abort |
| `checkpoint.py` | 162 | Crash recovery — save/resume progress |
| `ssh_utils.py` | 261 | SSH + BeeLlama + nvidia-smi transport layer |

### Remote Control Layer (new)

| Module | Lines | Purpose |
|--------|-------|---------|
| `harness.py` | 730 | Single entry point — thin CLI router to all modules |
| `remote_control.py` | 1088 | Unified controller for Triton (model, bench, sandbox, project, telemetry) |
| `work_engine.py` | 1477 | Full pipeline orchestration (swap → sandbox → infer → push → cleanup) |
| `sandbox_manager.py` | 621 | Docker sandbox lifecycle management on Triton |
| `gitea_utils.py` | 663 | Gitea REST API client (tunnelled via SSH) |
| `telemetry_collector.py` | 448 | Active telemetry collection (GPU stats, events, benchmarks) |
| `telemetry_dashboard.py` | 1446 | HTML telemetry dashboard generator (self-contained, dark theme) |

### Tooling

| Module | Lines | Purpose |
|--------|-------|---------|
| `generate_readme.py` | 846 | Auto-generate README.md from codebase inspection |

---

## Architecture Decisions

### Why SSH, not paramiko

Triton has SSH keys already configured. The subprocess + sshpass pattern is
simpler, has no dependencies, and matches existing infrastructure. Paramiko
would add a dependency for no benefit.

### Why SQLite, not Postgres

Single-machine deployment with no concurrency needs. SQLite is portable,
file-based, and requires zero setup. The benchmark harness runs on one
machine and results feed into DSH — there's no multi-user access pattern.

### Why separate from DSH core

The benchmark harness runs independently. Results feed into DSH's orchestration
layer via structured outputs. Keeping them separate means:

- Benchmarks can run without DSH
- DSH can consume results without running benchmarks
- Each evolves at its own pace

### Why Docker Compose profiles

Docker Compose profiles provide clean model isolation without duplicating
files. Each model config gets its own profile that activates the correct
BeeLlama container with the right model loaded.
