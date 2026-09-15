# Workflow State Machine

Documents the state machine that drives the coder-harness task execution pipeline and the deployment model-swap lifecycle.

---

## Task Execution State Machine

### State Diagram

```
INIT → SCOPING → DISPATCHING → EXECUTING → REVIEWING → TESTING → ITERATING → REPORTING → COMPLETE
                                                                    ↑           │
                                                                    └───────────┘
```

The machine starts at **INIT** and advances linearly through each phase. After **TESTING**, if the result is not satisfactory, it transitions to **ITERATING** which loops back to **DISPATCHING** for another round. Once satisfied, it moves through **REPORTING** to **COMPLETE**.

A fatal failure at any state transitions to **ERROR**, which requires manual intervention.

### States

| State | Description | Timeout |
|-------|-------------|---------|
| **INIT** | Verify connectivity to Triton, check Docker, confirm model is loaded | 60s |
| **SCOPING** | Read current state, identify affected files, plan the work | 120s |
| **DISPATCHING** | Launch coding agents, allocate sandboxes | 300s |
| **EXECUTING** | Agents implement changes in isolated sandboxes | 600s |
| **REVIEWING** | Automated code review: diffs, style, correctness | 120s |
| **TESTING** | Run test suite against modified code | 180s |
| **ITERATING** | Analyze test results, plan next iteration if needed | 60s |
| **REPORTING** | Generate final report with diffs, metrics, and summary | 120s |
| **COMPLETE** | Task finished successfully | — |
| **ERROR** | Unrecoverable failure; requires manual intervention | — |

### State Transitions

```
INIT ──────success──────→ SCOPING ──done──→ DISPATCHING ──launched──→ EXECUTING ──done──→ REVIEWING ──pass──→ TESTING
  │                          │                    │                       │                    │                   │
  │fail                      │fail                │fail                   │fail                │fail               │
  ↓                          ↓                    ↓                       ↓                    ↓                   ↓
ERROR                      ERROR                ERROR                   ERROR                ERROR          ITERATING/COMPLETE
                                                                                        │                  │
                                                                           ──needs work──→ITERATING        ──pass──→REPORTING
                                                                                        │                  │
                                                                   ──dispatch──→ DISPATCHING              ──done──→COMPLETE
```

### Transition Rules

| From | To | Condition |
|------|----|-----------|
| INIT → SCOPING | SSH to Triton succeeds, Docker responsive, model loaded |
| SCOPING → DISPATCHING | Work plan generated, affected files identified |
| DISPATCHING → EXECUTING | Sandboxes allocated, agents launched |
| EXECUTING → REVIEWING | All agents completed, results collected |
| REVIEWING → TESTING | Code review passed (no blocking issues) |
| TESTING → ITERATING | Tests failed or quality below threshold |
| TESTING → REPORTING | Tests passed, quality acceptable |
| ITERATING → DISPATCHING | Next iteration plan ready |
| REPORTING → COMPLETE | Report generated and saved |
| Any → ERROR | Unrecoverable failure (timeout, crash, connectivity loss) |

### Task Pool

The current task pool is scoped for **Config-I** (188 tok/s):

| Metric | Value |
|--------|-------|
| Total tasks | 10 |
| Estimated tokens | ~43,000 |
| Estimated time | ~4 minutes |

Tasks are dispatched across sandboxes in parallel where possible, with sequential dependency chains respected.

### Usage

```bash
python3 workflow-state-machine.py           # Run full workflow
python3 workflow-state-machine.py --dry-run # Show plan
python3 workflow-state-machine.py --status  # Current state
python3 workflow-state-machine.py --reset   # Reset to INIT
```

### Additional Flags

```bash
python3 workflow-state-machine.py --config config-i      # Override model config
python3 workflow-state-machine.py --max-iterations 5      # Limit iteration loops
python3 workflow-state-machine.py --timeout 300           # Override global timeout (seconds)
python3 workflow-state-machine.py --verbose               # Enable detailed logging
```

### Error Handling

When the machine enters the **ERROR** state:

1. Check the last log output for the root cause
2. Fix the underlying issue (connectivity, disk space, model crash, etc.)
3. Run `--reset` to return to INIT
4. Re-run the workflow

Common ERROR triggers:
- SSH connection lost to Triton during EXECUTING
- Docker sandbox OOM during code generation
- Model inference timeout (exceeds per-state timeout)
- Gitea push failure (network or auth issue)

---

## Deploy State Machine (Model Swap)

The deploy state machine (`tickets/deploy/deploy-state-machine.py`, 1185 lines) manages the model swap lifecycle on Triton. This is the lower-level machine that `harness.py model swap` delegates to.

### State Diagram

```
IDLE → STOPPING → LOADING → HEALTH_CHECK → READY → BENCHMARKING → SWAPPING → ERROR
```

### States

| State | Description |
|-------|-------------|
| **IDLE** | No model swap in progress; system ready |
| **STOPPING** | Shutting down current model container |
| **LOADING** | Starting new model container, waiting for load |
| **HEALTH_CHECK** | Polling `/health` endpoint until ready |
| **READY** | Model loaded and responding to inference |
| **BENCHMARKING** | Running throughput/quality benchmarks |
| **SWAPPING** | Mid-swap: old model stopped, new model starting |
| **ERROR** | Swap failed; requires manual intervention |

### Usage

```bash
python3 deploy-state-machine.py swap config-i       # Swap to config-i
python3 deploy-state-machine.py bench config-h      # Swap + benchmark
python3 deploy-state-machine.py status              # Current state
python3 deploy-state-machine.py history             # Swap history
python3 deploy-state-machine.py rollback            # Last known-good
```

### Swap Sequence

1. **STOPPING** — `docker compose --profile <old-profile> down`
2. **LOADING** — `docker compose --profile <new-profile> up -d`
3. **HEALTH_CHECK** — Poll `http://localhost:8080/health` every 5s (max 180s)
4. **READY** — Model responding, VRAM stable

If any step fails, the machine transitions to **ERROR**. `rollback` restores the last known-good configuration.

---

## Orchestrator Pipeline States

The main orchestrator (`orchestrate.py`) uses a simpler two-phase model:

### Phase 0: GPU Fit Matrix

```
PROBE → MEASURE → RECORD → NEXT_CONFIG → COMPLETE
```

For each (config × context_size) pair:
1. SSH → rewrite `.env` on Triton
2. Restart docker-compose
3. Poll `/health` until ready (180s timeout)
4. Send probe inference ("Say hello in one word.")
5. Read nvidia-smi for VRAM usage
6. Persist to `gpu_fit_matrix` table

### Phase 1: Full Evaluation

```
PREFLIGHT → RUN_BATCH → SCORE → NEXT_CONFIG → REPORT
```

1. **Preflight** — 7-point health check (SSH, GPUs, BeeLlama, disk, load, stress)
2. **Run batch** — All tasks × 3 reps per config
3. **Score** — Automated judge scoring (5 dimensions)
4. **Report** — Comparison tables + scatter plots

### Crash Recovery

The checkpoint system (`checkpoint.py`) saves progress every N runs (default: 5):

```bash
python3 orchestrate.py --phase 1 --resume   # Resume from last checkpoint
```

On resume:
1. `running` status runs → marked `incomplete`
2. Completed runs are skipped
3. Benchmark continues from where it left off

---

## Error Handling Across All Machines

### Common Failure Modes

| Machine | Failure | Recovery |
|---------|---------|----------|
| Task Execution | SSH lost during EXECUTING | Check Triton, `--reset`, re-run |
| Task Execution | Sandbox OOM | `harness.py sandbox cleanup`, re-run |
| Task Execution | Model timeout | `harness.py model swap`, re-run |
| Deploy | Container won't start | `docker compose --profile X logs`, check model files |
| Deploy | Health check timeout | Model loading takes 60–120s for 35B+; wait or rollback |
| Deploy | OOM on 3090 | Lower CONTEXT_SIZE, use KVarN, or switch model |
| Orchestrator | Benchmark interrupted | `--resume` to recover from checkpoint |
| Orchestrator | Judge model crash | Re-run scoring with `python3 judge.py --batch` |

### GPU Watchdog

The background GPU watchdog (`watchdog.py`) monitors hardware during all operations:

- Polls nvidia-smi every 30s
- **Abort triggers:** GPU disappears from nvidia-smi, temperature > 83°C, or nvidia-smi query failure
- Automatically stops all in-progress work on abort
- Status queryable via `watchdog.is_aborted()` or `watchdog.get_status_summary()`

---

## Work Engine Pipeline (work_engine.py)

The work engine chains all tools into a 7-step pipeline. Each step has its own lifecycle with telemetry events.

### Pipeline State Diagram

```
IDLE → SWAPPING → SANDBOX_READY → PREPARING → INFERENCING → COLLECTING → PUSHING → CLEANUP → DONE
  │         │            │             │             │             │           │          │
  │fail     │fail        │fail         │fail         │fail         │           │          │
  ↓         ↓            ↓             ↓             ↓             │           │          │
ERROR ← ERROR ← ERROR ← ERROR ← ERROR ←─────────────│─────────────│───────────│──────────│
                                                  (partial)     (skip)     (skip)    (always)
```

### Pipeline States

| State | Description | Telemetry Event |
|-------|-------------|-----------------|
| `created` | Session created in DB | `sandbox_create_start` |
| `swapping` | Model swap in progress | `model_swap_start` |
| `sandbox_ready` | Docker container created | `sandbox_created` |
| `running` | Inference executing | `inference_start` |
| `completed` | Pipeline finished successfully | `pipeline_done` |
| `failed` | Pipeline failed (see error_message) | `pipeline_abort` |
| `destroyed` | Sandbox container removed | `sandbox_destroyed` |

### Step-by-Step

```
Step 1: SWAPPING
  │  ├── deploy-state-machine.py swap <profile> (or direct docker compose)
  │  └── Poll /health every 3s (max 180s)
  ▼
Step 2: SANDBOX_READY
  │  ├── docker run -d --name harness-<name> ubuntu:24.04 sleep infinity
  │  ├── Volume mount ~/projects/<project> → /workspace
  │  └── Install python3, git, curl, jq
  ▼
Step 3: PREPARING
  │  ├── Verify project exists at ~/projects/<project>
  │  ├── Check git branch
  │  └── Verify files accessible in container
  ▼
Step 4: INFERENCING
  │  ├── Write JSON payload to temp file on Triton
  │  ├── curl POST http://localhost:<port>/v1/chat/completions
  │  ├── Parse response (content, timings, usage)
  │  └── Record inference metrics
  ▼
Step 5: COLLECTING
  │  ├── Write results JSON to project directory
  │  └── Record event
  ▼
Step 6: PUSHING (skipped if inference failed)
  │  ├── git checkout -b results/<name>
  │  ├── git add + commit + push
  │  └── git checkout <original-branch>
  ▼
Step 7: CLEANUP (always runs)
  └── docker rm -f harness-<name>
```

### Error Handling

- Each step records events in the telemetry DB
- If step 4 (inference) fails, steps 5-6 are partially skipped, but step 7 (cleanup) always runs
- Session status is `completed` on success, `failed` on error

---

## Sandbox Lifecycle (sandbox_manager.py)

### Sandbox State Diagram

```
                  ┌──────────┐
                  │  (none)  │
                  └────┬─────┘
                       │ create
                       ▼
                  ┌──────────┐
                  │ creating │ ──── error ──→ (error)
                  └────┬─────┘
                       │ success
                       ▼
              ┌────────────────┐
              │    running     │ ◄─── exec / collect
              │  (harness-*)   │
              └────┬───────────┘
                   │ destroy / destroy-all
                   ▼
              ┌────────────┐
              │ destroying │
              └────┬───────┘
                   │ done
                   ▼
              ┌────────────┐
              │  (removed) │
              └────────────┘
```

### Sandbox States

| State | Description | Container Status |
|-------|-------------|-----------------|
| `created` | Container created, deps installed | `running` (sleep infinity) |
| `running` | Active — exec commands, collect files | `running` |
| `destroyed` | Container removed via `docker rm -f` | (removed) |

### Container Naming

| Input | Container Name | Example |
|-------|---------------|---------|
| User provides: `test-01` | `harness-test-01` | `harness-my-sandbox` |
| Auto-generated: `None` | `harness-<timestamp>` | `harness-20260903-115850` |

### Volume Mounts

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `~/projects/<project>` | `/workspace` | read-only | Project source code |
| `~/models` | `/models` | read-only | Model files |

### File Collection Path

```
sandbox (container)
  │  docker cp harness-<name>:/workspace/results /tmp/harness-<name>-collect-<pid>
  ▼
Triton host (/tmp/staging)
  │  scp -r <user>@<LAN_IP>:/tmp/staging ./local-path
  ▼
nightmare (local)
  │  rm -rf /tmp/staging (cleanup Triton)
  ▼
Done
```

---

## Orchestrator Pipeline States

The main orchestrator (`orchestrate.py`) uses a simpler two-phase model:

### Phase 0: GPU Fit Matrix

```
PROBE → MEASURE → RECORD → NEXT_CONFIG → COMPLETE
```

For each (config × context_size) pair:
1. SSH → rewrite `.env` on Triton
2. Restart docker-compose
3. Poll `/health` until ready (180s timeout)
4. Send probe inference ("Say hello in one word.")
5. Read nvidia-smi for VRAM usage
6. Persist to `gpu_fit_matrix` table

### Phase 1: Full Evaluation

```
PREFLIGHT → RUN_BATCH → SCORE → NEXT_CONFIG → REPORT
```

1. **Preflight** — 7-point health check (SSH, GPUs, BeeLlama, disk, load, stress)
2. **Run batch** — All tasks × 3 reps per config
3. **Score** — Automated judge scoring (5 dimensions)
4. **Report** — Comparison tables + scatter plots

### Crash Recovery

The checkpoint system (`checkpoint.py`) saves progress every N runs (default: 5):

```bash
python3 orchestrate.py --phase 1 --resume   # Resume from last checkpoint
```

On resume:
1. `running` status runs → marked `incomplete`
2. Completed runs are skipped
3. Benchmark continues from where it left off

---

## Error Handling Across All Machines

### Common Failure Modes

| Machine | Failure | Recovery |
|---------|---------|----------|
| Task Execution | SSH lost during EXECUTING | Check Triton, `--reset`, re-run |
| Task Execution | Sandbox OOM | `harness.py sandbox cleanup`, re-run |
| Task Execution | Model timeout | `harness.py model swap`, re-run |
| Deploy | Container won't start | `docker compose --profile X logs`, check model files |
| Deploy | Health check timeout | Model loading takes 60–120s for 35B+; wait or rollback |
| Deploy | OOM on 3090 | Lower CONTEXT_SIZE, use KVarN, or switch model |
| Orchestrator | Benchmark interrupted | `--resume` to recover from checkpoint |
| Orchestrator | Judge model crash | Re-run scoring with `python3 judge.py --batch` |

### GPU Watchdog

The background GPU watchdog (`watchdog.py`) monitors hardware during all operations:

- Polls nvidia-smi every 30s
- **Abort triggers:** GPU disappears from nvidia-smi, temperature > 83°C, or nvidia-smi query failure
- Automatically stops all in-progress work on abort
- Status queryable via `watchdog.is_aborted()` or `watchdog.get_status_summary()`

---

## Quick Reference

```bash
# Task execution
python3 workflow-state-machine.py              # Full workflow
python3 workflow-state-machine.py --dry-run    # Preview plan
python3 workflow-state-machine.py --status     # Current state
python3 workflow-state-machine.py --reset      # Reset to INIT

# Model swap (deploy state machine)
python3 deploy-state-machine.py swap config-i  # Swap model
python3 deploy-state-machine.py bench config-h # Swap + benchmark
python3 deploy-state-machine.py status         # Current state
python3 deploy-state-machine.py history        # Swap history
python3 deploy-state-machine.py rollback       # Last known-good

# Orchestrator
python3 orchestrate.py --phase 0               # GPU fit matrix
python3 orchestrate.py --phase 1               # Full evaluation
python3 orchestrate.py --phase 1 --resume      # Resume after crash

# Work engine pipeline
python3 work_engine.py run --project coder-harness --task "Fix X" --config 3090-qwen36-35b
python3 work_engine.py sessions                # List sessions
python3 work_engine.py collect --name fix-01   # Collect results
python3 work_engine.py cleanup                 # Destroy all sandboxes
python3 work_engine.py status                  # System status

# Sandbox lifecycle
python3 sandbox_manager.py create --project coder-harness --name test-01
python3 sandbox_manager.py list
python3 sandbox_manager.py exec --name test-01 --command "ls"
python3 sandbox_manager.py collect --name test-01 --from /workspace/results --to ./results/
python3 sandbox_manager.py destroy --name test-01
python3 sandbox_manager.py destroy-all

# Quick entry via harness
harness.py model swap config-i                 # Swap model
harness.py bench throughput -c 3090-qwen36-35b # Benchmark
harness.py run -p coder-harness -t "task"      # Full pipeline
harness.py status                              # System health
```
