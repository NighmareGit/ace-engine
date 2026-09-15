"""Pipeline — 12-state state machine for the engine."""

import json
import threading
import signal
import time
from datetime import datetime
from engine.state import State, TransitionResult, VALID_TRANSITIONS, checkpoint_run, load_run
from engine import Task


class Pipeline:
    def __init__(self, engine):
        self.engine = engine
        self.state = State.IDLE
        self.run_id = None
        self.tasks = []         # list[Task]
        self.current_task = None
        self.current_task_idx = 0
        self.states_visited = []

        # Retry tracking: {task_id: {"generate": 0, "test": 0, "commit": 0,
        #                             "evidence_check": 0}}
        # NOTE: the vestigial "edit" key was removed (final epic gate S2) —
        # edit-atom retry metrics are now recorded directly by
        # EditTaskHandler via record_edit_op_result(), which captures
        # per-attempt applied/match_count semantics that the generic retry
        # counter cannot express.
        self.task_retries = {}

    def transition(self, new_state, **kwargs):
        """Validate transition, update state, log, checkpoint, emit event."""
        # CHAOS FIX C10: Guard QUEUED→DONE — only valid when all tasks terminal.
        # COMMIT is also terminal (research handler returns COMMIT state).
        if self.state == State.QUEUED and new_state == State.DONE:
            task_states = kwargs.get("task_states", {})
            _terminal = ("DONE", "FAILED", "CANCELLED", State.COMMIT.value)
            all_terminal = all(
                s in _terminal for s in task_states.values()
            ) if task_states else False
            if not all_terminal and len(self.tasks) > 0:
                # Not all tasks done — this is an error, not DONE
                new_state = State.FAILED
                kwargs["error"] = "QUEUED→DONE guard: not all tasks terminal"

        # 1. Validate
        if new_state not in VALID_TRANSITIONS.get(self.state, []):
            raise ValueError(f"Invalid transition: {self.state.value} → {new_state.value}")

        # 2. Capture old state for TransitionResult
        old_state = self.state

        # 3. Update state
        self.state = new_state
        self.states_visited.append(new_state)

        # 4. Log
        timestamp = datetime.now().isoformat()
        print(f"[{timestamp}] {old_state.value} → {new_state.value}")

        # 5. Checkpoint
        checkpoint_saved = False
        try:
            checkpoint_run(self.run_id, {
                "state": self.state.value,
                "current_task_id": self.current_task.id if self.current_task else None,
                "current_task_idx": self.current_task_idx,
                "tasks_json": json.dumps([t.__dict__ for t in self.tasks]),
                "task_retries_json": json.dumps(self.task_retries),
            })
            checkpoint_saved = True
        except Exception:
            pass

        # 6. Emit event
        event_emitted = False
        if self.engine.on_event is not None:
            try:
                self.engine.on_event("state_transition", {
                    "from": old_state.value,
                    "to": new_state.value,
                    "run_id": self.run_id,
                    "task_id": self.current_task.id if self.current_task else None,
                })
                event_emitted = True
            except Exception:
                pass

        return TransitionResult(
            from_state=old_state,
            to_state=new_state,
            timestamp=timestamp,
            checkpoint_saved=checkpoint_saved,
            event_emitted=event_emitted,
            error=None,
        )

    def get_retry_count(self, task_id, kind):
        """Get retry count for a task and kind (generate/test/commit/evidence_check)."""
        if task_id not in self.task_retries:
            self.task_retries[task_id] = {"generate": 0, "test": 0, "commit": 0,
                                          "evidence_check": 0}
        return self.task_retries[task_id].get(kind, 0)

    def increment_retry(self, task_id, kind):
        """Increment retry count and return new count."""
        if task_id not in self.task_retries:
            self.task_retries[task_id] = {"generate": 0, "test": 0, "commit": 0,
                                          "evidence_check": 0}
        self.task_retries[task_id][kind] = self.task_retries[task_id].get(kind, 0) + 1
        return self.task_retries[task_id][kind]

    def total_attempts(self, task_id):
        """Sum generate+test+commit attempts for a task (P3 cross-stage cap)."""
        retries = self.task_retries.get(task_id) or {"generate": 0, "test": 0, "commit": 0,
                                                     "evidence_check": 0}
        return retries.get("generate", 0) + retries.get("test", 0) + retries.get("commit", 0)

    def checkpoint(self):
        """Persist current state to SQLite."""
        checkpoint_run(self.run_id, {
            "state": self.state.value,
            "current_task_id": self.current_task.id if self.current_task else None,
            "current_task_idx": self.current_task_idx,
            "tasks_json": json.dumps([t.__dict__ for t in self.tasks]),
            "task_retries_json": json.dumps(self.task_retries),
        })

    @classmethod
    def resume(cls, run_id, engine):
        """Restore from SQLite checkpoint. Full state reconstruction."""
        data = load_run(run_id)
        if data is None:
            raise ValueError(f"No checkpoint found for run_id={run_id}")
        pipeline = cls(engine)
        pipeline.run_id = run_id
        pipeline.state = State(data["state"])
        # Reconstruct FULL Task objects from tasks_json
        tasks_data = json.loads(data.get("tasks_json", "[]"))
        pipeline.tasks = [Task(**t) for t in tasks_data]
        # Restore the task index and current_task reference
        pipeline.current_task_idx = data.get("current_task_idx", 0)
        if 0 <= pipeline.current_task_idx < len(pipeline.tasks):
            pipeline.current_task = pipeline.tasks[pipeline.current_task_idx]
        # Restore retry counters (persists across crash)
        pipeline.task_retries = json.loads(data.get("task_retries_json", "{}"))
        return pipeline

    def cancel(self):
        """Transition to CANCELLED, checkpoint."""
        self.transition(State.CANCELLED)


# Per-task total attempt cap (prevents COMMIT→QUEUED infinite loop)
MAX_TOTAL_ATTEMPTS_PER_TASK = 5

# Transport health re-check on failure
def _recheck_transport(engine):
    """Re-detect transport on first failure. Falls back HTTP→Local→SSH."""
    from transport import get_transport
    try:
        engine.transport = get_transport()
        return True
    except Exception:
        return False

# Timeout wrapper for state execution
class TimeoutError(Exception):
    pass

def _with_timeout(func, timeout_s, label="operation", cancel_check=None):
    """Run func with timeout. Raises TimeoutError if exceeded.

    If *cancel_check* is provided, it is polled every 100ms while waiting.
    When it returns True, raises CancelledError immediately — so a stop-file
    can abort a long call well before the wall-clock timeout. The stop is
    honored within one poll interval (<< 2 * timeout_s).
    """
    from engine.orchestrator.stop import CancelledError
    result = [None]
    error = [None]
    def target():
        try:
            result[0] = func()
        except Exception as e:
            error[0] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    deadline = time.time() + timeout_s
    # Poll in 100ms slices so a cancel_check can abort promptly.
    while t.is_alive() and time.time() < deadline:
        remaining = deadline - time.time()
        t.join(min(0.1, remaining))
        if cancel_check is not None and cancel_check():
            raise CancelledError(f"{label} cancelled via stop-file")
    if t.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout_s}s")
    if error[0]:
        raise error[0]
    return result[0]

# Checkpoint thread safety
_checkpoint_lock = threading.Lock()
