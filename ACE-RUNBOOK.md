# ACE Runbook — Autonomous Coding Engine

> Operational playbook for the coder-harness ACE. A "dumb" session agent can follow this literally.
> Engine HTTP: `http://<LAN_IP>:3082` | Engine CLI: `python3 engine/cli.py`
> BeeLlama: port 8080 (3090) / 8082 (3070) | Triton: `<user>@<LAN_IP>`

---

## 1. What ACE Is

The **Autonomous Coding Engine (ACE)** parses a PRD into atomic tasks, generates code via BeeLlama inference on Triton, runs deterministic quality gates, executes tests, and commits to Gitea — end-to-end with zero human intervention. It is implemented as the `engine/` Python package with a 12-state pipeline state machine (`engine/pipeline.py`) and a FastAPI HTTP service (`engine_service.py`) on port 3082.

**Architecture:** `engine_service.py` (:3082) is the HTTP control plane — it proxies inference to BeeLlama, writes files, runs tests, and commits git. It does NOT hold tokens; it forwards requests to BeeLlama on :8080 (RTX 3090, orchestrator/models) or :8082 (RTX 3070, judge/worker). All state is persisted in two SQLite ledgers: `engine.db` (runs, tasks, judge verdicts) and `benchmark-results.db` (benchmark data — **never touch its schema**).

**Auth:** Every engine-service endpoint except `/health` and `/gpu-snapshot` requires `Authorization: Token <secret>` (note: `Token`, not `Bearer`). The token is set via `--token` flag or `ENGINE_TOKEN` env var. BeeLlama endpoints (`:8080`, `:8082`) require no auth.

```
DSH Agent ──→ engine_service.py (:3082) ──→ BeeLlama (:8080 / :8082)
                    │                              │
                    ├── /inference ───────────────→ POST /v1/chat/completions
                    ├── /write-files ─────────────→ disk (atomic rename)
                    ├── /run-tests ───────────────→ pytest subprocess
                    ├── /git-commit ──────────────→ git add + commit
                    ├── /exec ────────────────────→ subprocess (dangerous-cmd block)
                    ├── /gpu-status ──────────────→ nvidia-smi
                    └── /stream ──────────────────→ SSE heartbeat
```

---

## 2. Decision Tree

```
I have an idea
  └─→ §3.1 Prep Path: plan → PRD → parse → run → judge → close → commit

I have a ticket (task already defined)
  └─→ §3.2 Direct Dispatch: engine/cli.py ace run --prd <file> --project <path>

I just want to test a model (A/B)
  └─→ §4 Preflight → §5 Model Swap → §3.3 Benchmark Path

Something is broken
  └─→ §7 Debug Quick-Reference
```

---

## 3. Step-by-Step Recipes

### 3.1 Prep Path (idea → running code)

```bash
# Step 1: Write a PRD (markdown with | ID | Title | Module | tables)
# Example: my-prd.md with tasks T01..T05

# Step 2: Parse PRD into tasks (dry run — no inference)
python3 engine/cli.py ace parse my-prd.md
# Expected: JSON list of tasks [{id, title, module, dependencies}]

# Step 3: Run the full pipeline
python3 engine/cli.py ace run --prd my-prd.md --project /home/<user>/projects/my-project --judge
# Expected: JSON {run_id, success, tasks: [...]}
# --judge enables deterministic scoring → writes to engine.db judge_verdicts table

# Step 4: Check status
python3 engine/cli.py ace status --run <run_id>
# Expected: JSON {state, current_task, tasks: [{id, state, commit_sha}]}

# Step 5: Resume if interrupted
python3 engine/cli.py ace resume --run <run_id>
```

**What failure looks like:**
- `ace parse` fails → PRD table format wrong. Must have `| T01 | Title | module.py |` rows.
- `ace run` fails immediately → BeeLlama not healthy. See §7 Debug.
- `ace run` hangs → inference timeout (300s default). Check `docker compose logs` on Triton.
- `ace status` returns `No checkpoint found` → run_id wrong or engine.db missing.

### 3.2 Direct Dispatch (ticket → code)

```bash
# If you have a single task, wrap it in a minimal PRD:
cat > /tmp/ticket-prd.md << 'EOF'
# Ticket: Fix SSH pattern

| T01 | Fix SSH pattern in ssh_utils.py | ssh_utils.py |
EOF

python3 engine/cli.py ace run --prd /tmp/ticket-prd.md --project /home/<user>/projects/coder-harness --judge
```

### 3.4 Campaign Path (DSH harness campaign → ACE engine)

> Use this when the DSH harness manages the project as a campaign with WAVEPLAN.

```bash
# Step 1: Compile the PRD into a campaign (DSH harness tool)
compile_campaign --campaignId <id> --prdPath <prd.md>
# → generates campaigns/<id>/WAVEPLAN.json + SPECS/ + issues/

# Step 2: Approve the waveplan
# (set WAVEPLAN.approved = true, or use autoApprove on compile)

# Step 3: Run a wave of tickets
run_wave --campaignId <id> --waveId <wave-name>
# → dispatches each ticket to a subagent

# Step 4: Judge/verify each completed ticket
# (deterministic judge via --judge flag, or manual verification)

# Step 5: Close ticket with evidence
close_ticket --campaignId <id> --ticketId <T01> --evidencePath <path>
# → flips ticket to done; unblocks dependent tickets
```

**What failure looks like:**
- `compile_campaign` fails → PRD malformed or campaignId invalid
- `run_wave` fails → WAVEPLAN not approved
- `close_ticket` fails → evidence path doesn't match deliverable exactly

### 3.3 Benchmark Path (A/B model testing)

```bash
# Step 1: Acquire GPU lease (MANDATORY before heavy GPU work)
# See §4 Preflight — write lease to .scratch/leases/<agent>-<timestamp>.lease

# Step 2: Run benchmark for a specific config
python3 orchestrate.py --config 3090-qwen36-35b
# Or single task:
python3 runner.py --config 3090-qwen36-35b --task T14

# Step 3: Score results
python3 judge.py --batch

# Step 4: Generate report
python3 report.py --format md
# → reports/benchmark-report.md

# Step 5: Release GPU lease
rm .scratch/leases/<your-lease>.lease
```

---

## 4. Model/Backend Preflight

> Run this before any engine run, benchmark, or swap. Verifies the full stack is live.

```bash
# 1. SSH to Triton
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 <user>@<LAN_IP> echo "SSH OK"

# 2. BeeLlama 3090 health
curl -sf http://<LAN_IP>:8080/health && echo " 3090 OK" || echo " 3090 FAIL"

# 3. BeeLlama 3070 health
curl -sf http://<LAN_IP>:8082/health && echo " 3070 OK" || echo " 3070 FAIL"

# 4. Engine service health
curl -sf http://<LAN_IP>:3082/health | python3 -m json.tool

# 5. GPU status (VRAM guard)
curl -sf http://<LAN_IP>:3082/gpu-snapshot | python3 -m json.tool

# 6. Gitea reachability
ssh <user>@<LAN_IP> 'curl -sf http://localhost:3000/api/v1/repos/search | python3 -c "import sys,json; print(\"Gitea OK:\", len(json.load(sys.stdin)[\"data\"]), \"repos\")"'

# 7. Acquire GPU lease (mandatory before heavy work)
mkdir -p .scratch/leases
cat > .scratch/leases/$(whoami)-$(date +%s).lease << EOF
agent: $(whoami)
gpus: 0,1
acquired: $(date -Iseconds)
expires: $(date -d '+90 minutes' -Iseconds)
purpose: ace-run
EOF
echo "Lease acquired: $(ls -t .scratch/leases/ | head -1)"
```

**Expected output:** All 6 checks pass. If any fail, see §7 Debug.

---

## 5. Model Swap Recipe

> **Rule:** Always acquire a GPU lease before swapping. Always restore the previous model after.

```bash
# Step 1: Acquire lease
cat > .scratch/leases/$(whoami)-$(date +%s).lease << EOF
agent: $(whoami)
gpus: 0,1
acquired: $(date -Iseconds)
expires: $(date -d '+90 minutes' -Iseconds)
purpose: model-swap
EOF

# Step 2: Check current model
python3 engine/cli.py ace status   # or:
curl -s http://<LAN_IP>:3082/health | python3 -m json.tool
# → {model: "...", status: "ok"}

# Step 3: Swap (two options — prefer state machine)
# Option A: State machine (recommended — has rollback)
ssh <user>@<LAN_IP> 'cd ~/dockers/beellama-benchmark && python3 deploy-state-machine.py swap config-i'
# Option B: Direct compose
ssh <user>@<LAN_IP> 'cd ~/dockers/beellama-benchmark && docker compose --profile config-i up -d'

# Step 4: Wait for health (poll every 5s, max 180s)
until curl -sf http://<LAN_IP>:8080/health >/dev/null 2>&1; do sleep 5; done
echo "Model healthy"

# Step 5: Real chat-completion proof (not just /health)
curl -s -X POST http://<LAN_IP>:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello in one word."}],"max_tokens":16,"temperature":0.3}'
# Expected: JSON with choices[0].content == "Hello." (or similar)

# Step 5b: MODEL IDENTITY GATE (mandatory — a serving backend is not a correctly-configured backend)
# The loaded model filename MUST match the one you intended to load, EXACTLY.
curl -s http://<LAN_IP>:8080/v1/models | grep -o 'GPT-OSS-20B-Pruned-IQ4_NL.gguf'   # ← substitute YOUR intended filename
# If grep finds nothing → WRONG MODEL IS LOADED → HARD STOP. Do NOT proceed, do NOT
# "make it work": never edit cache/quant/ctx flags to force an unexpected model to load.
# Real incident (2026-09-07): an agent searched for "*REAP*", found GLM-4.7-Flash-REAP
# instead of GPT-OSS-20B-REAP, silently rewrote the KV cache flags to make it load,
# and ran a full benchmark against the wrong model. The run was worthless.
# Restore from the COMMITTED compose file (git), never from an agent's runtime backup —
# the backup may itself be contaminated by earlier failed swaps.

# Step 6: VRAM check
curl -s http://<LAN_IP>:3082/gpu-snapshot | python3 -m json.tool
# → {gpus: [{index, name, memory_used_mb, memory_total_mb}]}
# VRAM guard: ensure used < 23 GB on 3090 (leaves 1 GB headroom)

# Step 7: Restore previous model (swap back)
ssh <user>@<LAN_IP> 'cd ~/dockers/beellama-benchmark && python3 deploy-state-machine.py swap <previous-profile>'

# Step 8: Release lease
rm .scratch/leases/<your-lease>.lease
```

**Config ID ↔ Profile Mapping:**

| Manifest ID | Docker Profile | GPU | Port |
|---|---|---|---|
| `3090-qwen36-35b` | `config-i` | 3090 | 8080 |
| `3090-muse-glimmer` | `config-h` | 3090 | 8080 |
| `3090-qwen35-9b` | `config-h-qwen35` | 3090 | 8080 |
| `3070-qwen35-9b` | (shared 3070) | 3070 | 8080 |

---

## 6. Engine HTTP API (curl recipes)

```bash
ENGINE=http://<LAN_IP>:3082
TOKEN=your-engine-token   # set via --token or ENGINE_TOKEN env

# Health (no auth)
curl -s $ENGINE/health | python3 -m json.tool
# → {status: "ok", uptime: 123.45, model: "Qwen3.6-35B", gpu_temp: [...]}

# Inference (auth required — note: Token, NOT Bearer)
curl -s -X POST $ENGINE/inference \
  -H 'Content-Type: application/json' \
  -H "Authorization: Token $TOKEN" \
  -d '{"messages":[{"role":"user","content":"Write a hello world"}],"max_tokens":128,"temperature":0.3,"port":8080}'
# → {content: "...", timings: {predicted_per_second: 188.5}, usage: {total_tokens: 150}}

# Write files (auth required)
curl -s -X POST $ENGINE/write-files \
  -H 'Content-Type: application/json' \
  -H "Authorization: Token $TOKEN" \
  -d '{"project_path":"/home/<user>/projects/my-project","files":[{"path":"src/main.py","content":"print(hello)"}]}'
# → {written: ["src/main.py"], errors: [], total: 1}

# Run tests (auth required)
curl -s -X POST $ENGINE/run-tests \
  -H 'Content-Type: application/json' \
  -H "Authorization: Token $TOKEN" \
  -d '{"project_path":"/home/<user>/projects/my-project","framework":"pytest"}'
# → {passed: 5, failed: 0, errors: 0, output: "..."}

# Git commit (auth required)
curl -s -X POST $ENGINE/git-commit \
  -H 'Content-Type: application/json' \
  -H "Authorization: Token $TOKEN" \
  -d '{"project_path":"/home/<user>/projects/my-project","message":"feat: add health check","files":["src/main.py"]}'
# → {sha: "abc123", message: "feat: add health check", files_changed: 1}

# GPU status (no auth)
curl -s $ENGINE/gpu-snapshot | python3 -m json.tool
# → {gpus: [{index: 0, name: "RTX 3090", memory_used_mb: 19000, memory_total_mb: 24576}]}
```

---

## 7. Debug Quick-Reference

| Symptom | Cause | Fix |
|---|---|---|
| `/health` 404 on :8082 | openresty proxy intercepts; BeeLlama uses `/healthz` | Use `curl http://localhost:8082/healthz` or check `docker compose logs` |
| `python requests` 400s from engine | Wrong auth scheme or malformed body | Use `curl` first; ensure `Authorization: Token <secret>` (not Bearer) |
| 0 tokens reported in judge | Empty/invalid BeeLlama response | Instrument raw response: `curl ... \| python3 -m json.tool`; check `usage.total_tokens` |
| `BLOCKED: command matches dangerous-pattern` | `/exec` caught `rm -rf`, `chmod -R 777`, `dd`, etc. | Use `--allow-exec` flag on engine_service, or avoid the pattern |
| Worktree leakage | Previous run left `harness-*` containers | `docker rm -f $(docker ps -a \| grep harness- \| awk '{print $1}')` |
| Lease conflict | Another agent holds GPU lease | Check `.scratch/leases/`; wait or fail fast — never force |
| KV OOM on 3090 | Context too large for VRAM | Lower `CTX` in `.env`; use `ctk q8_0 / ctv q4_0`; or switch to kvarn4 |
| Host freeze after model swap | Orphaned generation saturated GPU | Swap config via `deploy-state-machine.py rollback`; **don't kill** — let it drain |
| `port 3082 already in use` | Previous engine_service still running | `pkill -f engine_service.py` or use a different `--port` |
| `No checkpoint found for run_id` | Wrong run_id or engine.db deleted | Run `python3 engine/cli.py ace status` (no --run) to list all runs |
| Judge score < 0.8 (fail-closed) | Missing commit, 0 tokens, or non-terminal tasks | Check `engine.db`: `SELECT * FROM judge_verdicts WHERE run_id=<id>` |
| `model does not support different K/V cache types` | Model loaded doesn't support mixed KV (e.g. GLM) — likely the WRONG model was loaded | **HARD STOP.** Verify `/v1/models` filename matches intent; never change cache flags to force an unexpected model to load |
| Benchmark ran against wrong model | Agent searched by partial name (`*REAP*`) and grabbed a lookalike | Model identity gate (§5 Step 5b): grep `/v1/models` for the EXACT intended filename before any run; results from a wrong-model run are invalid — quarantine and discard |
| Restored config is itself wrong/contaminated | Restore source was an agent's runtime backup taken after earlier edits | Always restore compose from the **committed git copy** of the repo, never from runtime backups |

**Fail-closed judge semantics** (`engine/judge.py`):
- 5 weighted dimensions: `success` (0.30), `tasks` (0.20), `commit` (0.20), `tokens` (0.15), `error_free` (0.15)
- `PASS_THRESHOLD = 0.8` → weighted sum must be ≥ 0.8 to pass
- A run with no commit, 0 tokens, or a single error **fails automatically**

---

## 8. Hard Rules

1. **Fail-closed judges are final.** If the deterministic judge scores < 0.8, the run failed. Don't override — fix the root cause.
2. **Honest failure is data.** A failed run with a clear error message is more useful than a silently "passed" run. Report failures.
3. **Validity gates are mandatory.** The 4-stage validator (`engine/validator.py`: AST → syntax → imports → execution) must pass before a task is committed.
4. **Never pop git stash.** If you didn't create it, don't pop it. Use worktree isolation instead.
5. **Worktree isolation.** Run code-modifying agents in isolated worktrees (`worktree-guard` skill), not on the main branch.
6. **VRAM guard: 7600 MiB headroom on 3090.** Never load a config that leaves < 1 GB VRAM free. Check with `/gpu-snapshot` before and after loading.
7. **GPU lease before heavy work.** Acquire a lease from `.scratch/leases/` before any benchmark, profiling, or multi-GPU test. Release it when done.
8. **Never touch `benchmark-results.db` schema.** It is the benchmark ledger (Rule 6). Engine state goes in `engine.db`.
9. **Auth scheme is `Token`, not `Bearer`.** `engine_service.py` rejects `Bearer` with 401.
10. **Two compose files exist.** `tickets/deploy/docker-compose.yml` (benchmark profiles) and `beellama-kvarn-deploy/docker-compose.yml` (KVarN deploy). Know which you're editing.
11. **Model identity gate (mandatory).** After ANY configure/swap step, assert `/v1/models` contains the EXACT intended filename before running anything. Never modify serving flags (cache types, quant, ctx) to make an unexpected model load — wrong model = hard stop, not a config puzzle.
12. **Restore from git, not runtime backups.** Compose restore sources must come from the committed repo copy; an agent's mid-session backup may already contain edits from earlier failed attempts.
13. **Minimal secret injection.** Never inject a whole keystore (`.env`, `secrets.env`, compose `env_file`) into a container or agent platform that doesn't need every key in it — especially anything meant to run autonomously or remotely. Inject ONLY the specific credential an instance needs, scoped and least-privilege. Any instance that can reach a paid cloud API without an explicit, dated opt-in is a misconfiguration, not a convenience. (2026-09-09: whole `.env` injected into the DSH docker silently exposed a DeepSeek key → ~$18 burn.)

---

## 9. Pointers (the runbook is the router, not the replacement)

| Topic | Authoritative Doc |
|---|---|
| Engine endpoints & auth | `engine_service.py` (source of truth) |
| Engine CLI commands | `engine/cli.py` (source of truth) |
| Judge scoring logic | `engine/judge.py` (source of truth) |
| Pipeline state machine | `engine/pipeline.py` + `engine/state.py` |
| Benchmark CLI reference | `docs/API-REFERENCE.md` §4 |
| Remote control & model swap | `docs/remote-control.md` |
| Deploy state machine | `docs/workflow-state-machine.md` §2 |
| Docker architecture | `docs/docker-architecture.md` |
| Engine unification ADR | `docs/ADR-001-engine-unification.md` |
| Gitea integration | `AGENTS.md` §13 |
| Transport layer | `AGENTS.md` §14 |
| CCBS | `AGENTS.md` §19 |
| Config/KVarN index | `beellama-kvarn-deploy/CONFIG-INDEX.md` |
| Engine audit (honest) | `ENGINE-AUDIT.md` — **read before trusting engine maturity claims**: confidence self-assessed at 45%, "the entire engine is theoretical" (§73), live-fire coverage was one inference call |
| Historical handoff (2026-09-04) | `HANDOFF-ENGINE-UNIFICATION.md` — marked HISTORICAL; migration long since implemented |
| GPU lease protocol | `gpu-lease` DSH skill |

---

## 10. Campaign Lifecycle (DSH Harness → ACE Engine)

> When the DSH harness manages the project as a **campaign** (PRD → tickets → dispatched agents), the lifecycle below connects the DSH campaign tools to the ACE engine. For full detail on each DSH tool see `docs/blueprints/campaign-runbook.md` and `docs/blueprints/campaign-state-machine-v2.md`.

### How a PRD becomes a dispatched campaign

```
PRD.md ──(compile_campaign)──→ campaigns/<id>/{WAVEPLAN.json, SPECS/, issues/*}
                                        │
                                        ▼
                              WAVEPLAN.approved = true
                                        │
                                        ▼
                              run_wave (dispatches tickets)
                                        │
                                        ▼
                              subagent per ticket → ACE engine
                                        │
                                        ▼
                              close_ticket (evidence path)
```

### What each step does

1. **`compile_campaign`** — Compiles a PRD markdown file into a campaign: generates `WAVEPLAN.json` (the wave/ticket plan), `SPECS/` (per-ticket specs), and `issues/` (individual ticket files). One sentence: *turns a PRD into a structured, dispatchable campaign.* Requires `campaignId` and `prdPath`.

2. **WAVEPLAN approval** — Sets `WAVEPLAN.approved = true` (or pass `autoApprove` to compile). One sentence: *gates dispatch — `run_wave` refuses to run an unapproved plan.* This is the go/no-go checkpoint.

3. **`run_wave`** — Dispatches every ticket in a wave to subagents for execution. One sentence: *runs the tickets, optionally retrying failed ones (`onlyFailed`).* Requires `campaignId` and `waveId`.

4. **`close_ticket`** — Flips a ticket to `done` after verification and unblocks dependent tickets. One sentence: *completes a ticket once its deliverable is verified.* **Critical rule:** the `evidencePath` must be the **exact deliverable path** defined for that ticket — the tool verifies the path matches the contract, not just that the file exists.

5. **`list_campaigns`** — Lists campaign statuses (wave states, ticket states, approved flag). One sentence: *use to monitor overall campaign progress.*

6. **`manage_roster`** — Manages role assignments, model mappings, and fallback chains for a campaign. One sentence: *controls which models/roles handle which tickets.*

### Connecting to the ACE engine

- Each dispatched ticket can run `python3 engine/cli.py ace run --prd <ticket-prd> --project <path> --judge` (see §3.1).
- The deterministic judge (`engine/judge.py`) scores each run; `close_ticket` evidence should point to the verified output (e.g., the commit SHA, test results, or generated file).
- Auth for engine-service endpoints: `Authorization: Token <secret>` (not Bearer — see §1 and §6).

---

*Last verified against source: engine_service.py (1265 lines), engine/cli.py, engine/judge.py, engine/pipeline.py, engine/state.py. If a command here contradicts the source code, the source code wins.*
