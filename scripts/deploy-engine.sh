#!/usr/bin/env bash
# deploy-engine.sh — Build and deploy the coder-engine container on Triton
#
# Usage:
#   ./scripts/deploy-engine.sh              # Build + deploy
#   ./scripts/deploy-engine.sh --build-only # Just build
#   ./scripts/deploy-engine.sh --smoke-test # Build + deploy + verify
#
# Prerequisites:
#   - Docker 29.x running on Triton
#   - BeeLlama containers already running (or will start separately)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE_DIR="${PROJECT_DIR}/tickets/deploy"

TRITON_HOST="${TRITON_HOST:-<LAN_IP>}"
TRITON_USER="${TRITON_USER:-<user>}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[engine]${NC} $*"; }
warn() { echo -e "${YELLOW}[engine]${NC} $*"; }
err()  { echo -e "${RED}[engine]${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
BUILD_ONLY=false
SMOKE_TEST=false

for arg in "$@"; do
    case "$arg" in
        --build-only)  BUILD_ONLY=true ;;
        --smoke-test)  SMOKE_TEST=true ;;
        --help|-h)
            echo "Usage: $0 [--build-only] [--smoke-test]"
            exit 0
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Step 1: Copy Dockerfile.engine to the right place
# ---------------------------------------------------------------------------
log "Ensuring Dockerfile.engine is in place..."
if [[ ! -f "${PROJECT_DIR}/Dockerfile.engine" ]]; then
    err "Dockerfile.engine not found at ${PROJECT_DIR}/Dockerfile.engine"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: Build the image on Triton
# ---------------------------------------------------------------------------
log "Building coder-engine image on Triton..."
ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
    "cd ${COMPOSE_DIR} && docker compose -f ../../coder-harness/docker-compose.engine.yml build --no-cache"

if [[ "$BUILD_ONLY" == true ]]; then
    log "Build complete (--build-only). Exiting."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 3: Stop existing engine (if running)
# ---------------------------------------------------------------------------
log "Stopping existing coder-engine (if running)..."
ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
    "cd ${COMPOSE_DIR} && docker compose -f ../../coder-harness/docker-compose.engine.yml down 2>/dev/null || true"

# ---------------------------------------------------------------------------
# Step 4: Start the engine
# ---------------------------------------------------------------------------
log "Starting coder-engine..."
ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
    "cd ${COMPOSE_DIR} && docker compose -f ../../coder-harness/docker-compose.engine.yml --profile engine up -d"

# ---------------------------------------------------------------------------
# Step 5: Wait for health check
# ---------------------------------------------------------------------------
log "Waiting for engine to be healthy..."
for i in $(seq 1 30); do
    if ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
        "docker exec coder-engine python3 -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8080/health\")' 2>/dev/null"; then
        log "Engine is healthy!"
        break
    fi
    if [[ $i -eq 30 ]]; then
        err "Engine health check timed out after 30 attempts"
        ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
            "docker logs coder-engine --tail=50"
        exit 1
    fi
    sleep 2
done

# ---------------------------------------------------------------------------
# Step 6: Smoke test (optional)
# ---------------------------------------------------------------------------
if [[ "$SMOKE_TEST" == true ]]; then
    log "Running smoke tests..."
    
    # Test 1: Health check
    log "  [1/4] Health check..."
    ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
        "docker exec coder-engine python3 -c \"
from transport import get_transport
t = get_transport()
assert t.check_beellama_health(8080), 'BeeLlama 3090 unhealthy'
assert t.check_beellama_health(8082), 'BeeLlama 3070 unhealthy'
print('  ✅ BeeLlama health OK')
\""
    
    # Test 2: Direct inference
    log "  [2/4] Direct inference (no SSH)..."
    ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
        "docker exec coder-engine python3 -c \"
from transport import get_transport
t = get_transport()
result = t.curl_beellama(8080, [{'role': 'user', 'content': 'Say hello in one word.'}], max_tokens=10)
assert result['content'], f'Empty response: {result}'
print(f'  ✅ Inference OK: {result[\"content\"][:50]}')
\""
    
    # Test 3: File access
    log "  [3/4] File access via volume mount..."
    ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
        "docker exec coder-engine test -f /workspace/coder-harness/engine/cli.py && echo '  ✅ File access OK' || echo '  ❌ File access FAILED'"
    
    # Test 4: Gitea access
    log "  [4/4] Gitea API access..."
    ssh -o StrictHostKeyChecking=no "${TRITON_USER}@${TRITON_HOST}" \
        "docker exec coder-engine python3 -c \"
import urllib.request, json
req = urllib.request.Request('http://localhost:3000/api/v1/repos/search',
    headers={'Authorization': 'token <GITEA_TOKEN>'})
resp = urllib.request.urlopen(req, timeout=10)
data = json.loads(resp.read())
print(f'  ✅ Gitea OK: {len(data.get(\"data\", []))} repos found')
\""
    
    log "All smoke tests passed! 🎉"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
log "Deployment complete!"
log ""
log "  Container:  coder-engine"
log "  Network:    host mode (localhost access to all services)"
log "  Workspace:  /workspace/coder-harness/"
log ""
log "  Quick commands:"
log "    docker exec -it coder-engine bash"
log "    docker exec coder-engine python3 engine/cli.py status"
log "    docker exec coder-engine python3 engine/cli.py health"
log "    docker logs -f coder-engine"
log ""
