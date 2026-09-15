"""Stop-file mechanism for graceful orchestrator cancellation (T5).

A stop-file is a sentinel on disk. The engine checks for it at every stage
transition and inside long calls (via the cancel-check callback of
pipeline._with_timeout). When present, the current task is cancelled
gracefully and the run transitions to CANCELLED.

The stop-file path is configurable via ACE_STOP_FILE (a per-run sentinel) or
ACE_STOP_GLOBAL (a global kill switch that stops any run). This keeps the
mechanism testable without touching a live run.
"""

import os

DEFAULT_STOP_DIR = "/tmp"


class CancelledError(Exception):
    """Raised when a stop-file is detected, cancelling the current operation."""


def _stop_file_path(run_id: str) -> str:
    """Resolve the per-run stop-file path (ACE_STOP_FILE overrides)."""
    custom = os.environ.get("ACE_STOP_FILE")
    if custom:
        return custom
    return os.path.join(DEFAULT_STOP_DIR, f".ace-stop-{run_id}")


def _global_stop_path() -> str:
    """Resolve the global kill-switch path (ACE_STOP_GLOBAL overrides)."""
    return os.environ.get("ACE_STOP_GLOBAL", os.path.join(DEFAULT_STOP_DIR, ".ace-stop"))


def check_stop_file(run_id: str) -> bool:
    """True if a stop-file exists for this run OR a global stop is set."""
    if os.path.exists(_stop_file_path(run_id)):
        return True
    if os.path.exists(_global_stop_path()):
        return True
    return False


def raise_if_stopped(run_id: str) -> None:
    """Convenience: raise CancelledError if a stop-file is present."""
    if check_stop_file(run_id):
        raise CancelledError(f"stop-file detected for run {run_id}")


def clear_stop_file(run_id: str) -> None:
    """Remove the per-run and global stop sentinels (idempotent)."""
    for path in (_stop_file_path(run_id), _global_stop_path()):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
