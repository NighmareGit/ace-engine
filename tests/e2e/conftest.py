"""conftest.py for e2e tests — skip when engine_service is unreachable.

Respects environment variables:
  ENGINE_E2E=0  → force-skip all e2e tests
  ENGINE_E2E=1  → force-run all e2e tests (skip connectivity check)
  (unset)       → check engine_service /health; skip if unreachable
"""

import os
import urllib.request

import pytest

ENGINE_SERVICE_URL = os.environ.get(
    "ENGINE_SERVICE_URL", "http://<LAN_IP>:3082"
)
_engine_e2e = os.environ.get("ENGINE_E2E", "")


def _engine_reachable():
    """GET {ENGINE_SERVICE_URL}/health with 2s timeout (stdlib only)."""
    try:
        req = urllib.request.Request(
            f"{ENGINE_SERVICE_URL}/health", method="GET"
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


# --- skip decision -------------------------------------------------------
if _engine_e2e == "0":
    _skip_e2e = True
    _skip_reason = f"ENGINE_E2E=0 force-skip"
elif _engine_e2e == "1":
    _skip_e2e = False
    _skip_reason = ""
else:
    _skip_e2e = not _engine_reachable()
    _skip_reason = f"engine_service unreachable at {ENGINE_SERVICE_URL}"

# Module-level mark — applies to every test collected in this directory
pytestmark = pytest.mark.skipif(
    _skip_e2e,
    reason=_skip_reason,
)
