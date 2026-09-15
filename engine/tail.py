"""Live-tail event stream — tail a running run's events from engine_events.

``ace tail <run_id>`` polls the engine_events table for new rows since last
check and prints them in real-time.  Enables observing a running run without
tailing run.log.

OBS-08: builds on OBS-04 (engine_events table).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from engine.engine_events import query_events, _db_path


def tail_run(
    run_id: str,
    event_type: Optional[str] = None,
    interval: float = 1.0,
    timeout: float = 30.0,
    db_path: Optional[str] = None,
    print_events: bool = True,
) -> list[dict]:
    """Poll engine_events for new rows for *run_id* and print them.

    Args:
        run_id: The engine run ID to tail.
        event_type: Optional filter by event type.
        interval: Polling interval in seconds (default: 1.0).
        timeout: Max seconds to tail (default: 30.0).
        db_path: Override DB path.
        print_events: If True, print events to stdout as they appear.

    Returns:
        List of all events seen during the tail session.
    """
    db = db_path or _db_path()
    last_id = 0
    all_events: list[dict] = []
    start = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            break
        events = query_events(
            run_id=run_id,
            event_type=event_type,
            since_id=last_id,
            db_path=db,
        )
        for ev in events:
            all_events.append(ev)
            last_id = max(last_id, ev["id"])
            if print_events:
                ts = ev.get("created_at", "")
                et = ev.get("event_type", "")
                tid = ev.get("task_id", "")
                data = ev.get("data_json", "{}")
                prefix = f"[{ts}] {et}"
                if tid:
                    prefix += f" ({tid})"
                print(f"{prefix}: {data}")
        if events:
            # Immediately poll again for more.
            continue
        # No new events — sleep for the interval.
        remaining = timeout - (time.time() - start)
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    return all_events


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python3 engine/tail.py``."""
    parser = argparse.ArgumentParser(
        description="Live-tail a running run's events from engine_events."
    )
    parser.add_argument("run", help="Run ID to tail")
    parser.add_argument("--type", default=None, help="Filter by event type")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Polling interval in seconds (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Max seconds to tail (default: 30.0)")
    parser.add_argument("--db", default=None, help="Database path override")
    args = parser.parse_args(argv)

    events = tail_run(
        args.run,
        event_type=args.type,
        interval=args.interval,
        timeout=args.timeout,
        db_path=args.db,
    )
    print(f"\n[tail complete — {len(events)} event(s) seen]", file=sys.stderr)


if __name__ == "__main__":
    main()
