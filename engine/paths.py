"""Engine path constants — absolute paths anchored to the repo root."""

import os
from pathlib import Path

# RED-TEAM V4 FIX: absolute path anchored to the repo root — a relative DB_PATH
# silently creates a second database if the CLI is invoked from another directory.
# Docker-deploy override: ENGINE_DB_PATH lets the compose mount a named volume
# (e.g. /app/data/engine.db) so results survive container rebuilds.
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("ENGINE_DB_PATH") or str(REPO_ROOT / "engine.db")  # Rule 6: separate from benchmark-results.db
BACKUP_PATH = os.environ.get("ENGINE_DB_BACKUP") or str(REPO_ROOT / "engine.db.bak")

# SANDBOX flag — when True, run tests inside Docker container instead of local
SANDBOX = os.environ.get("ENGINE_SANDBOX", "").lower() in ("1", "true", "yes")
