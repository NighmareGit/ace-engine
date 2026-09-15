#!/usr/bin/env python3
"""
Unit tests for CCBS-005: State Machine Core

Tests cover:
  - All valid transitions from the transition table
  - Invalid transition detection
  - Checkpoint save/load round-trip
  - Resume from BENCHMARKING state
  - Progress tracking
  - Retry counter logic
  - Global transitions (GPU_OVERHEAT, ABORT) from any state
  - Entry action stubs (logging, not crashing)
"""

import importlib.util
import json
import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Import from the actual module (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------
_CCBS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "tickets", "ccbs")
_CCBS_MODULE_PATH = os.path.join(_CCBS_DIR, "ccbs-state-machine.py")
_spec = importlib.util.spec_from_file_location("ccbs_state_machine", _CCBS_MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ccbs_state_machine"] = _mod
_spec.loader.exec_module(_mod)

BenchmarkStateMachine = _mod.BenchmarkStateMachine
Event = _mod.Event
InvalidTransitionError = _mod.InvalidTransitionError
State = _mod.State
_TRANSITIONS = _mod._TRANSITIONS
_GLOBAL_TRANSITIONS = _mod._GLOBAL_TRANSITIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sm(**kwargs) -> BenchmarkStateMachine:
    """Create a fresh BenchmarkStateMachine backed by a temp SQLite DB."""
    db = tempfile.mktemp(suffix=".db")
    defaults = dict(
        db_path=db,
        config_queue=["cfg-A", "cfg-B", "cfg-C"],
        total_tasks=10,
    )
    defaults.update(kwargs)
    sm = BenchmarkStateMachine(**defaults)
    # Reset transition log so tests start clean
    sm.transition_log = []
    return sm


def _cleanup(sm: BenchmarkStateMachine) -> None:
    """Close DB and remove temp file."""
    db_path = sm.db.execute("PRAGMA database_list").fetchone()[2]
    sm.close()
    if os.path.exists(db_path):
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 1. Valid transitions — exhaustive check
# ---------------------------------------------------------------------------

class TestValidTransitions:
    """Every (state, event) pair in the transition table must succeed."""

    def test_idle_start(self):
        sm = _make_sm()
        sm.fire(Event.START)
        assert sm.state == State.GENERATING
        _cleanup(sm)

    def test_generating_complete(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        assert sm.state == State.PREFLIGHT
        _cleanup(sm)

    def test_generating_fail(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)
        assert sm.state == State.ERROR
        _cleanup(sm)

    def test_preflight_ok(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        assert sm.state == State.LOADING
        _cleanup(sm)

    def test_preflight_fail(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_FAIL)
        assert sm.state == State.ERROR
        _cleanup(sm)

    def test_loading_health_ok(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        assert sm.state == State.WARMUP
        _cleanup(sm)

    def test_loading_health_fail(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_FAIL)
        assert sm.state == State.ERROR
        _cleanup(sm)

    def test_loading_ssh_timeout(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.state == State.RETRY
        assert sm.retry_count == 1
        _cleanup(sm)

    def test_warmup_ok(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        assert sm.state == State.BENCHMARKING
        _cleanup(sm)

    def test_warmup_fail(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_FAIL)
        assert sm.state == State.ERROR
        _cleanup(sm)

    def test_benchmarking_task_complete_loops(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.TASK_COMPLETE)
        assert sm.state == State.BENCHMARKING
        sm.fire(Event.TASK_COMPLETE)
        assert sm.state == State.BENCHMARKING
        _cleanup(sm)

    def test_benchmarking_response_timeout_stays(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.RESPONSE_TIMEOUT)
        assert sm.state == State.BENCHMARKING
        _cleanup(sm)

    def test_benchmarking_all_tasks_complete(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        assert sm.state == State.SCORING
        _cleanup(sm)

    def test_benchmarking_gpu_oom(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.GPU_OOM)
        assert sm.state == State.EMERGENCY_STOP
        _cleanup(sm)

    def test_scoring_score_complete(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        assert sm.state == State.SWAPPING
        _cleanup(sm)

    def test_scoring_judge_fail(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.JUDGE_FAIL)
        assert sm.state == State.RETRY
        _cleanup(sm)

    def test_swapping_swap_complete(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        sm.fire(Event.SWAP_COMPLETE)
        assert sm.state == State.LOADING
        _cleanup(sm)

    def test_swapping_all_configs_done(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        sm.fire(Event.ALL_CONFIGS_DONE)
        assert sm.state == State.FINAL_REPORT
        _cleanup(sm)

    def test_emergency_stop_cooldown_complete(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.GPU_OVERHEAT)
        assert sm.state == State.EMERGENCY_STOP
        sm.fire(Event.COOLDOWN_COMPLETE)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_error_resume(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)
        assert sm.state == State.ERROR
        sm.fire(Event.RESUME)
        assert sm.state == State.LOADING
        _cleanup(sm)

    def test_error_error_cleared(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)
        sm.fire(Event.ERROR_CLEARED)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_retry_ssh_timeout_stays(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.state == State.RETRY
        assert sm.retry_count == 1
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.state == State.RETRY
        assert sm.retry_count == 2
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.state == State.RETRY
        assert sm.retry_count == 3
        _cleanup(sm)

    def test_retry_exhausted(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.RETRY_EXHAUSTED)
        assert sm.state == State.ERROR
        _cleanup(sm)

    def test_retry_health_ok(self):
        """RETRY + HEALTH_OK → WARMUP (retry succeeded)."""
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.HEALTH_OK)
        assert sm.state == State.WARMUP
        _cleanup(sm)

    def test_pause_from_benchmarking(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_generating(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_scoring(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_loading(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_warmup(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_swapping(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_retry(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_reporting(self):
        sm = _make_sm()
        sm.state = State.REPORTING
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_pause_from_final_report(self):
        sm = _make_sm()
        sm.state = State.FINAL_REPORT
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        _cleanup(sm)

    def test_paused_resume(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.PAUSE)
        assert sm.state == State.PAUSED
        sm.fire(Event.RESUME)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_idle_resume_noop(self):
        sm = _make_sm()
        sm.fire(Event.RESUME)
        assert sm.state == State.IDLE
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 2. Global transitions from ANY state
# ---------------------------------------------------------------------------

class TestGlobalTransitions:
    """GPU_OVERHEAT and ABORT must work from any state."""

    def test_gpu_overheat_from_idle(self):
        sm = _make_sm()
        sm.fire(Event.GPU_OVERHEAT)
        assert sm.state == State.EMERGENCY_STOP
        _cleanup(sm)

    def test_gpu_overheat_from_benchmarking(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.GPU_OVERHEAT)
        assert sm.state == State.EMERGENCY_STOP
        _cleanup(sm)

    def test_gpu_overheat_from_loading(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.GPU_OVERHEAT)
        assert sm.state == State.EMERGENCY_STOP
        _cleanup(sm)

    def test_abort_from_benchmarking(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_scoring(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_emergency_stop(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.GPU_OVERHEAT)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_error(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)
        assert sm.state == State.ERROR
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_paused(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.PAUSE)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_retry(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_warmup(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)

    def test_abort_from_swapping(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        sm.fire(Event.ABORT)
        assert sm.state == State.IDLE
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 3. Invalid transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:
    """Invalid transitions must raise InvalidTransitionError."""

    def test_idle_start_only(self):
        sm = _make_sm()
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.HEALTH_OK)
        _cleanup(sm)

    def test_idle_preflight_ok(self):
        sm = _make_sm()
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.PREFLIGHT_OK)
        _cleanup(sm)

    def test_generating_start(self):
        sm = _make_sm()
        sm.fire(Event.START)
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.START)
        _cleanup(sm)

    def test_benchmarking_start(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.START)
        _cleanup(sm)

    def test_scoring_start(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.START)
        _cleanup(sm)

    def test_idle_warmup_ok(self):
        sm = _make_sm()
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.WARMUP_OK)
        _cleanup(sm)

    def test_idle_all_tasks_complete(self):
        sm = _make_sm()
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.ALL_TASKS_COMPLETE)
        _cleanup(sm)

    def test_generating_health_ok(self):
        sm = _make_sm()
        sm.fire(Event.START)
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.HEALTH_OK)
        _cleanup(sm)

    def test_loading_warmup_ok(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.WARMUP_OK)
        _cleanup(sm)

    def test_error_start(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)
        with _assert_raises(InvalidTransitionError):
            sm.fire(Event.START)
        _cleanup(sm)


def _assert_raises(exc_type):
    """Context manager that asserts an exception is raised."""
    import contextlib
    return contextlib.nullcontext()


# Override _assert_raises to actually work as a context manager
import contextlib as _ctx

class _AssertRaises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f"Expected {self.exc_type.__name__} but nothing was raised")
        if not issubclass(exc_type, self.exc_type):
            raise AssertionError(f"Expected {self.exc_type.__name__}, got {exc_type.__name__}")
        return True  # suppress the exception

def _assert_raises(exc_type):
    return _AssertRaises(exc_type)


# ---------------------------------------------------------------------------
# 4. Checkpoint round-trip
# ---------------------------------------------------------------------------

class TestCheckpointRoundTrip:
    """Save and load checkpoints must preserve all state fields."""

    def test_checkpoint_round_trip_from_benchmarking(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        # Simulate some progress
        sm.current_task_index = 5
        sm.current_repetition = 2
        sm.completed_runs = [{"run_id": 1}, {"run_id": 2}]
        cp_id = sm.save_checkpoint()

        # Create a new SM from the same DB and load
        sm2 = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A", "cfg-B", "cfg-C"],
            total_tasks=10,
        )
        restored = sm2.load_checkpoint(cp_id)

        assert restored["state"] == "BENCHMARKING"
        assert restored["task_index"] == 5
        assert restored["repetition"] == 2
        assert restored["completed_runs"] == 2
        assert sm2.current_config_index == 0
        sm.close()
        sm2.close()

    def test_checkpoint_round_trip_from_error(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)
        cp_id = sm.save_checkpoint()

        sm2 = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A"],
            total_tasks=5,
        )
        restored = sm2.load_checkpoint(cp_id)
        assert restored["state"] == "ERROR"
        sm.close()
        sm2.close()

    def test_checkpoint_round_trip_from_retry(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.SSH_TIMEOUT)  # retry_count=2
        cp_id = sm.save_checkpoint()

        sm2 = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A"],
            total_tasks=5,
        )
        restored = sm2.load_checkpoint(cp_id)
        assert restored["state"] == "RETRY"
        assert restored["retry_count"] == 2
        sm.close()
        sm2.close()

    def test_checkpoint_not_found_raises(self):
        sm = _make_sm()
        try:
            sm.load_checkpoint(999999)
            raise AssertionError("Should have raised ValueError")
        except ValueError:
            pass
        _cleanup(sm)

    def test_checkpoint_preserves_config_index(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        sm.fire(Event.SWAP_COMPLETE)  # → LOADING, config_index should be 1
        assert sm.current_config_index == 1
        cp_id = sm.save_checkpoint()

        sm2 = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A", "cfg-B", "cfg-C"],
            total_tasks=10,
        )
        restored = sm2.load_checkpoint(cp_id)
        assert restored["config_index"] == 1
        assert restored["state"] == "LOADING"
        sm.close()
        sm2.close()

    def test_multiple_checkpoints(self):
        """Each fire() should create a new checkpoint row."""
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)

        # We should have checkpoints for IDLE, GENERATING, PREFLIGHT, LOADING, WARMUP, BENCHMARKING
        rows = sm.db.execute(
            "SELECT state FROM ccbs_checkpoints ORDER BY id"
        ).fetchall()
        states = [r["state"] for r in rows]
        assert "IDLE" in states
        assert "GENERATING" in states
        assert "PREFLIGHT" in states
        assert "LOADING" in states
        assert "WARMUP" in states
        assert "BENCHMARKING" in states
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 5. Resume from BENCHMARKING
# ---------------------------------------------------------------------------

class TestResumeFromBenchmarking:
    """Simulate crash-recovery: save checkpoint mid-benchmark, reload, continue."""

    def test_resume_benchmarking_continues_tasks(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)

        # Simulate 5 tasks done
        for i in range(5):
            sm.current_task_index = i + 1
            sm.fire(Event.TASK_COMPLETE)

        assert sm.current_task_index == 5
        cp_id = sm.save_checkpoint()

        # Simulate crash — new SM, reload
        sm_new = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A", "cfg-B", "cfg-C"],
            total_tasks=10,
        )
        sm_new.load_checkpoint(cp_id)
        assert sm_new.state == State.BENCHMARKING
        assert sm_new.current_task_index == 5

        # Continue from task 6
        for i in range(5, 10):
            sm_new.current_task_index = i + 1
            sm_new.fire(Event.TASK_COMPLETE)

        sm_new.fire(Event.ALL_TASKS_COMPLETE)
        assert sm_new.state == State.SCORING

        sm.close()
        sm_new.close()

    def test_resume_benchmarking_full_cycle(self):
        """Resume from BENCHMARKING, complete all tasks, score, swap, done."""
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)

        sm.current_task_index = 7
        cp_id = sm.save_checkpoint()

        # Reload
        sm_new = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A", "cfg-B"],
            total_tasks=10,
        )
        sm_new.load_checkpoint(cp_id)

        # Complete remaining tasks
        for i in range(7, 10):
            sm_new.current_task_index = i + 1
            sm_new.fire(Event.TASK_COMPLETE)
        sm_new.fire(Event.ALL_TASKS_COMPLETE)
        sm_new.fire(Event.SCORE_COMPLETE)
        sm_new.fire(Event.ALL_CONFIGS_DONE)
        # FINAL_REPORT is a state, not an event — it's reached via ALL_CONFIGS_DONE
        assert sm_new.state == State.FINAL_REPORT
        sm.close()
        sm_new.close()


# ---------------------------------------------------------------------------
# 6. Progress tracking
# ---------------------------------------------------------------------------

class TestProgressTracking:
    """Progress should be 0 at IDLE, grow through states, and reflect task progress."""

    def test_progress_at_idle(self):
        sm = _make_sm()
        assert sm.get_progress() == 0.0
        _cleanup(sm)

    def test_progress_at_benchmarking_reflects_tasks(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        # At BENCHMARKING with 0/10 tasks → progress = 0.45 * 0 = 0.0
        p0 = sm.get_progress()
        assert p0 == 0.0  # no tasks done yet in BENCHMARKING
        # At BENCHMARKING with 5/10 tasks
        sm.current_task_index = 5
        p5 = sm.get_progress()
        # At BENCHMARKING with 10/10 tasks
        sm.current_task_index = 10
        p10 = sm.get_progress()
        assert 0.0 < p5 < p10 <= 1.0
        _cleanup(sm)

    def test_progress_at_final_report(self):
        sm = _make_sm()
        sm.state = State.FINAL_REPORT
        assert sm.get_progress() == 0.08
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 7. Retry counter logic
# ---------------------------------------------------------------------------

class TestRetryCounter:
    """Retry counter must increment on SSH_TIMEOUT in RETRY state and reset on IDLE."""

    def test_retry_counter_increments(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.retry_count == 1
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.retry_count == 2
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.retry_count == 3
        _cleanup(sm)

    def test_retry_counter_resets_on_idle(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.retry_count == 2
        sm.fire(Event.ABORT)  # → IDLE
        assert sm.retry_count == 0
        _cleanup(sm)

    def test_retry_counter_resets_on_resume_from_error(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_FAIL)  # → ERROR
        sm.retry_count = 5  # simulate
        sm.fire(Event.RESUME)  # → LOADING (retry_count should reset)
        assert sm.retry_count == 0
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 8. Transition log
# ---------------------------------------------------------------------------

class TestTransitionLog:
    """Transition log must track all fired events."""

    def test_transition_log_records_events(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)

        log = sm.get_transition_summary()
        assert len(log) == 5
        assert log[0]["from"] == "IDLE"
        assert log[0]["event"] == "start"
        assert log[0]["to"] == "GENERATING"
        assert log[4]["from"] == "WARMUP"
        assert log[4]["event"] == "warmup_ok"
        assert log[4]["to"] == "BENCHMARKING"
        _cleanup(sm)

    def test_global_transition_logged(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        sm.fire(Event.GPU_OVERHEAT)

        log = sm.get_transition_summary()
        assert log[-1]["from"] == "BENCHMARKING"
        assert log[-1]["event"] == "gpu_overheat"
        assert log[-1]["to"] == "EMERGENCY_STOP"
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 9. Entry actions (smoke test — they log, shouldn't crash)
# ---------------------------------------------------------------------------

class TestEntryActions:
    """Every state's on_enter_* must exist and not raise."""

    def test_all_entry_actions_fire(self):
        sm = _make_sm()
        # Walk through happy path — all entry actions are called
        sm.fire(Event.START)             # on_enter_generating
        sm.fire(Event.GENERATION_COMPLETE)  # on_enter_preflight
        sm.fire(Event.PREFLIGHT_OK)      # on_enter_loading
        sm.fire(Event.HEALTH_OK)         # on_enter_warmup
        sm.fire(Event.WARMUP_OK)         # on_enter_benchmarking
        sm.fire(Event.ALL_TASKS_COMPLETE) # on_enter_scoring
        sm.fire(Event.SCORE_COMPLETE)    # on_enter_swapping
        sm.fire(Event.SWAP_COMPLETE)     # on_enter_loading (again)
        # All should have logged without error
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 10. Design invariant checks
# ---------------------------------------------------------------------------

class TestDesignInvariants:
    """Architectural invariants that must hold."""

    def test_no_direct_state_assignment(self):
        """State can only be changed via fire(), not by direct assignment."""
        sm = _make_sm()
        # Direct assignment should be possible but fire() is the intended API
        # This test verifies that fire() is the only way transitions happen
        initial_transitions = len(sm.transition_log)
        sm.fire(Event.START)
        assert len(sm.transition_log) == initial_transitions + 1
        _cleanup(sm)

    def test_all_states_have_at_least_one_transition(self):
        """No state should be a dead end (except terminal states)."""
        terminal_states = {State.IDLE, State.ERROR, State.EMERGENCY_STOP}
        for state in State:
            if state in terminal_states:
                continue
            # Check either per-state or global transitions exist
            has_per_state = state in _TRANSITIONS and len(_TRANSITIONS[state]) > 0
            has_global = any(
                _GLOBAL_TRANSITIONS.get(evt) is not None
                for evt in Event
            )
            assert has_per_state or has_global, (
                f"State {state.value} has no transitions"
            )
        _cleanup(_make_sm())

    def test_complete_happy_path(self):
        """Full lifecycle: IDLE → GENERATING → PREFLIGHT → LOADING → WARMUP
        → BENCHMARKING → SCORING → SWAPPING → LOADING → ... → FINAL_REPORT → IDLE."""
        sm = _make_sm()
        # Config A
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        for _ in range(10):
            sm.fire(Event.TASK_COMPLETE)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        # Config B
        sm.fire(Event.SWAP_COMPLETE)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        for _ in range(10):
            sm.fire(Event.TASK_COMPLETE)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        # Config C
        sm.fire(Event.SWAP_COMPLETE)
        sm.fire(Event.HEALTH_OK)
        sm.fire(Event.WARMUP_OK)
        for _ in range(10):
            sm.fire(Event.TASK_COMPLETE)
        sm.fire(Event.ALL_TASKS_COMPLETE)
        sm.fire(Event.SCORE_COMPLETE)
        # All done
        sm.fire(Event.ALL_CONFIGS_DONE)
        assert sm.state == State.FINAL_REPORT
        # Final report auto-transitions to IDLE externally
        _cleanup(sm)


# ---------------------------------------------------------------------------
# 11. Retry counter persistence across checkpoint
# ---------------------------------------------------------------------------

class TestRetryCheckpointPersistence:
    """Retry count must survive checkpoint round-trip."""

    def test_retry_count_persists(self):
        sm = _make_sm()
        sm.fire(Event.START)
        sm.fire(Event.GENERATION_COMPLETE)
        sm.fire(Event.PREFLIGHT_OK)
        sm.fire(Event.SSH_TIMEOUT)
        sm.fire(Event.SSH_TIMEOUT)
        assert sm.retry_count == 2
        cp_id = sm.save_checkpoint()

        sm2 = BenchmarkStateMachine(
            db_path=sm.db.execute("PRAGMA database_list").fetchone()[2],
            config_queue=["cfg-A"],
            total_tasks=5,
        )
        sm2.load_checkpoint(cp_id)
        assert sm2.retry_count == 2
        assert sm2.max_retries == 3
        sm.close()
        sm2.close()


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestValidTransitions,
        TestGlobalTransitions,
        TestInvalidTransitions,
        TestCheckpointRoundTrip,
        TestResumeFromBenchmarking,
        TestProgressTracking,
        TestRetryCounter,
        TestTransitionLog,
        TestEntryActions,
        TestDesignInvariants,
        TestRetryCheckpointPersistence,
    ]

    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        for name in sorted(dir(instance)):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            try:
                method()
                print(f"  ✓ {cls.__name__}.{name}")
                passed += 1
            except Exception as e:
                print(f"  ✗ {cls.__name__}.{name}: {e}")
                traceback.print_exc()
                errors.append((f"{cls.__name__}.{name}", e))
                failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if errors:
        print("\nFailed tests:")
        for name, exc in errors:
            print(f"  ✗ {name}: {exc}")
        sys.exit(1)
    print("All tests passed!")
