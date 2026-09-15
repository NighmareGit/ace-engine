#!/usr/bin/env bash
# deploy-host.sh — deploy ACE engine to Triton + build ace-sandbox image.
# Run on Triton. Idempotent: re-runs are safe (pull, re-create venv, rebuild).
set -euo pipefail

REPO_DIR="/home/<user>/ace-engine"
PY="python3.12"
LOG="/home/<user>/.ace-deploy-log.txt"
: > "$LOG"
mkdir -p /home/<user>

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
warn() { echo "[$(date +%H:%M:%S)] WARN: $*" | tee -a "$LOG" >&2; }

# --- 0. engine.env sanity (do NOT echo token values) ----------------------------
if [[ -f /home/<user>/shared-configs/engine.env ]]; then
    log "engine.env present ($(wc -l < /home/<user>/shared-configs/engine.env) lines, $(wc -c < /home/<user>/shared-configs/engine.env) bytes)"
else
    warn "engine.env NOT found at /home/<user>/shared-configs/engine.env"
fi

# --- 1. clone / pull ---------------------------------------------------------
if [[ -d "$REPO_DIR/.git" ]]; then
    log "repo exists — fetching..."
    cd "$REPO_DIR"
    git fetch --all --quiet 2>>"$LOG"
    git checkout main >>"$LOG" 2>&1 && git pull --ff-only >>"$LOG" 2>&1
    log "now on: $(git rev-parse --short HEAD) $(git log --oneline -1)"
else
    log "cloning repo..."
    # Reuse the Gitea origin URL from the local checkout (credentials baked in).
    git clone "http://<user>:<GITEA_TOKEN>@<LAN_IP>:3000/<user>/ace-engine.git" "$REPO_DIR" >>"$LOG" 2>&1
    cd "$REPO_DIR"
    log "cloned: $(git rev-parse --short HEAD)"
fi

# --- 2. python env -----------------------------------------------------------
# ensurepip is unavailable on Triton and we must NOT install globally. The
# engine is PYTHONPATH-based (no pyproject.toml), so we use the system
# python3.12 directly. pip is available in user site; we install test tooling
# there (user site is already on sys.path). No venv needed.
cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR:${PYTHONPATH:-}"
log "using $($PY --version 2>&1), PYTHONPATH=$PYTHONPATH"

# --- 3. deps (user-site; best-effort if registry offline) --------------------
log "installing test deps (user-site)..."
"$PY" -m pip install --quiet --user --upgrade pip >>"$LOG" 2>&1 || true
"$PY" -m pip install --quiet --user pytest pytest-xdist >>"$LOG" 2>&1 \
    || warn "pytest install failed"
"$PY" -m pip install --quiet --user requests >>"$LOG" 2>&1 \
    || warn "requests install failed (offline?)"
log "pytest: $("$PY" -m pytest --version 2>&1 | head -1)"

# --- 4. run the canonical suite ----------------------------------------------
cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR:${PYTHONPATH:-}"
log "PYTHONPATH=$PYTHONPATH"
log "running suite: pytest tests/engine tests/unit -q"
set +e
python3 -m pytest tests/engine tests/unit -q --tb=short -p no:cacheprovider > /home/<user>/ace-engine/.suite-output.txt 2>&1
SUITE_RC=$?
set -e
SUITE_SUMMARY=$(tail -15 /home/<user>/ace-engine/.suite-output.txt)
log "suite exit code: $SUITE_RC"
echo "$SUITE_SUMMARY" | tee -a "$LOG"

# --- 5. sandbox image ---------------------------------------------------------
log "building ace-sandbox image..."
set +e
docker build -t ace-sandbox:latest deploy/sandbox/ > /home/<user>/ace-engine/.sandbox-build.txt 2>&1
BUILD_RC=$?
set -e
log "sandbox build exit code: $BUILD_RC"
tail -5 /home/<user>/ace-engine/.sandbox-build.txt | tee -a "$LOG"

echo "===DONE=== suite_rc=$SUITE_RC build_rc=$BUILD_RC"
echo "suite-output: /home/<user>/ace-engine/.suite-output.txt"
echo "sandbox-build: /home/<user>/ace-engine/.sandbox-build.txt"
echo "deploy-log: $LOG"
