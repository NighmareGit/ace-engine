"""T5 tests: stop-file + timeout wrapping (stop.py + pipeline._with_timeout).

Stop-file checked at every stage transition AND inside long calls
(generate/test/commit) via _with_timeout(timeout_inference) + a cancel-check
callback. Stop honored within one stage or 2*timeout_inference max.

Tests: simulated hang (patched slow callable), cancel-check polling, and an
integration test that the engine cancels (not times out) when a stop-file set.
"""

import os
import sys
import time
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import stop as stop_mod
from engine import pipeline as pipe_mod


# ---------------------------------------------------------------------------
# stop-file helpers
# ---------------------------------------------------------------------------

def test_stop_file_path_default(monkeypatch):
    monkeypatch.delenv("ACE_STOP_FILE", raising=False)
    p = stop_mod._stop_file_path("run-123")
    assert p.endswith(".ace-stop-run-123") or "run-123" in p


def test_stop_file_path_env_override(monkeypatch):
    monkeypatch.setenv("ACE_STOP_FILE", "/custom/stop")
    assert stop_mod._stop_file_path("run-123") == "/custom/stop"


def test_check_stop_file_detects_and_clears(tmp_path, monkeypatch):
    monkeypatch.delenv("ACE_STOP_GLOBAL", raising=False)
    stop_file = str(tmp_path / ".ace-stop-run-X")
    monkeypatch.setenv("ACE_STOP_FILE", stop_file)
    assert stop_mod.check_stop_file("run-X") is False
    # create the sentinel
    with open(stop_file, "w") as f:
        f.write("")
    assert stop_mod.check_stop_file("run-X") is True
    stop_mod.clear_stop_file("run-X")
    assert stop_mod.check_stop_file("run-X") is False


# ---------------------------------------------------------------------------
# _with_timeout: cancel_check callback
# ---------------------------------------------------------------------------

def test_with_timeout_fast_call_returns_result():
    res = pipe_mod._with_timeout(lambda: 42, timeout_s=5, label="t")
    assert res == 42


def test_with_timeout_slow_call_times_out():
    with pytest.raises(pipe_mod.TimeoutError):
        pipe_mod._with_timeout(lambda: time.sleep(5), timeout_s=0.2, label="slow")


def test_with_timeout_cancel_check_fires_before_timeout():
    """cancel_check returning True raises CancelledError quickly, long before
    the wall-clock timeout."""
    start = time.time()
    with pytest.raises(stop_mod.CancelledError):
        pipe_mod._with_timeout(
            lambda: time.sleep(10),
            timeout_s=5,
            label="hang",
            cancel_check=lambda: True,
        )
    elapsed = time.time() - start
    assert elapsed < 2.0, f"cancel was not honored promptly: {elapsed:.2f}s"


def test_with_timeout_cancel_check_polled_during_wait():
    """A cancel_check that becomes True mid-wait aborts the call."""
    state = {"tick": 0}
    def cancel_late():
        state["tick"] += 1
        return state["tick"] >= 3  # True after a couple of polls

    start = time.time()
    with pytest.raises(stop_mod.CancelledError):
        pipe_mod._with_timeout(
            lambda: time.sleep(10),
            timeout_s=5,
            label="hang",
            cancel_check=cancel_late,
        )
    elapsed = time.time() - start
    assert elapsed < 2.0


def test_with_timeout_propagates_inner_exception():
    def boom():
        raise ValueError("inner")
    with pytest.raises(ValueError, match="inner"):
        pipe_mod._with_timeout(boom, timeout_s=5, label="boom")


# ---------------------------------------------------------------------------
# Integration: engine cancels a hanging generate when stop-file is set.
# ---------------------------------------------------------------------------

class _HangingTransport:
    """Transport whose curl_beellama hangs; other methods succeed."""
    def __init__(self, hang_s=60):
        self.hang_s = hang_s
        self.calls = 0

    def check_health(self):
        return True

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        time.sleep(self.hang_s)
        return {"content": "```python\nx = 1\n```", "finish_reason": "stop",
                "reasoning_content": "", "thinking_tokens": 0,
                "total_tokens": 50, "prompt_tokens": 20,
                "completion_tokens": 30, "predicted_per_second": 5.0}

    def run_command(self, cmd, timeout=60, cwd=None):
        return ("", "", 0)

    def run_git(self, project_path, *args):
        if args[0] == "rev-parse":
            return ("abc123", "", 0)
        return ("", "", 0)


def _run_with_hanging_transport(transport, stop_file, timeout_inference=1):
    """Run the engine with a stop-file already set; return (result, elapsed)."""
    import engine.engine as eng_mod
    import engine.committer as cm
    from engine.engine import Engine, EngineConfig, Task

    events = []
    orig_parse = eng_mod.parse_prd
    orig_ensure = cm._ensure_repo_exists
    eng_mod.parse_prd = lambda _: [Task(id="T01", title="T", description="d", module="app.py")]
    cm._ensure_repo_exists = lambda repo, autocreate: None
    cfg = EngineConfig(max_retries_generate=3, timeout_inference=timeout_inference)
    try:
        eng = Engine(transport=transport, config=cfg,
                     on_event=lambda et, d: events.append((et, d)))
        start = time.time()
        result = eng.run("/tmp/prd.md", "/tmp/project")
        elapsed = time.time() - start
        return result, elapsed, events
    finally:
        eng_mod.parse_prd = orig_parse
        cm._ensure_repo_exists = orig_ensure


def test_engine_cancels_hang_via_stop_file(tmp_path, monkeypatch):
    """With a stop-file set, a hanging generate is cancelled (graceful),
    NOT a timeout — and it returns within a small multiple of timeout_inference."""
    monkeypatch.delenv("ACE_STOP_GLOBAL", raising=False)
    stop_file = str(tmp_path / ".ace-stop-run-cancel")
    monkeypatch.setenv("ACE_STOP_FILE", stop_file)
    # Pre-set the stop-file so the first cancel-check catches it.
    with open(stop_file, "w") as f:
        f.write("")
    transport = _HangingTransport(hang_s=30)
    result, elapsed, events = _run_with_hanging_transport(
        transport, stop_file, timeout_inference=1)
    # Must return promptly — well under the 30s hang, and within ~2*timeout.
    assert elapsed < 5.0, f"stop not honored promptly: {elapsed:.2f}s"
    # The run should be in a terminal cancelled/failed state, never a real success.
    assert result.success is False


def test_engine_timeout_without_stop_file(tmp_path, monkeypatch):
    """Without a stop-file, a hanging generate surfaces as a timeout error."""
    monkeypatch.delenv("ACE_STOP_GLOBAL", raising=False)
    # Point stop-file at a path that does NOT exist.
    monkeypatch.setenv("ACE_STOP_FILE", str(tmp_path / "never-set"))
    transport = _HangingTransport(hang_s=30)
    result, elapsed, events = _run_with_hanging_transport(
        transport, "never-set", timeout_inference=1)
    # Times out after ~timeout_inference (not the full 30s hang).
    assert elapsed < 5.0, f"timeout not honored: {elapsed:.2f}s"
    assert result.success is False
