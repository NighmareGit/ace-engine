"""Budget caps — LLM call count, total tokens, wall-clock (T8).

Caps are set in EngineConfig (0 = unbounded). Counters live in T1's
orchestrator session and are persisted atomically on every increment, so a
crash mid-run does not lose the tally. ``check_budget`` returns (stopped,
reason) and is consulted by the engine before each task and after each LLM
call; when a cap is exceeded the run stops via the same graceful path as
the stop-file (CANCELLED).
"""

import time
from datetime import datetime

from engine.orchestrator import session as sess


def check_budget(run_id: str, counters: dict) -> tuple[bool, str]:
    """Return (stopped, reason) given the current budget counters.

    A cap value of 0 means unbounded (disabled). Caps are checked independently;
    the first exceeded cap wins.
    """
    calls = counters.get("llm_calls", 0)
    tokens = counters.get("total_tokens", 0)
    max_calls = counters.get("max_llm_calls", 0)
    max_tokens = counters.get("max_total_tokens", 0)
    max_wall = counters.get("max_wall_clock_s", 0)

    if max_calls > 0 and calls >= max_calls:
        return (True, f"budget cap: llm_calls {calls} >= max_llm_calls {max_calls}")

    if max_tokens > 0 and tokens >= max_tokens:
        return (True, f"budget cap: total_tokens {tokens} >= max_total_tokens {max_tokens}")

    if max_wall > 0:
        start_str = counters.get("wall_clock_start")
        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str)
                elapsed = (datetime.now() - start_dt).total_seconds()
                if elapsed >= max_wall:
                    return (True, f"budget cap: wall_clock {elapsed:.1f}s >= "
                                  f"max_wall_clock_s {max_wall}")
            except (ValueError, TypeError):
                pass

    return (False, "")


def increment_and_check(run_id: str, tokens: int = 0) -> tuple[dict, bool, str]:
    """Atomically increment the counters and check the budget. Returns
    (counters, stopped, reason)."""
    counters = sess.increment_llm_calls(run_id, tokens=tokens)
    stopped, reason = check_budget(run_id, counters)
    return counters, stopped, reason
