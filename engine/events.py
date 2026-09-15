"""Engine event emission — fire-and-forget bridge to callback + optional streaming.

OBS-04: emit_event now also writes each event to the ``engine_events`` table
in engine.db (additive parallel write).  The callback path is unchanged —
this is best-effort and wrapped in try/except so observability never breaks
the pipeline.
"""

import os


def emit_event(on_event, event_type, data):
    """
    If on_event is set, call it (synchronous, primary path).
    Additionally bridge to the streaming server when STREAMING_URL is set.
    Additionally persist to engine_events table (OBS-04, best-effort).
    Never raises — observability must never break the pipeline.
    """
    if on_event is not None:
        try:
            on_event(event_type, data)
        except Exception:
            pass  # callback failures must not crash the engine
    if os.environ.get("STREAMING_URL"):
        try:
            from streaming_client import emit_event_fire_and_forget
            emit_event_fire_and_forget("engine", event_type, data)
        except Exception:
            pass  # streaming is opt-in (AD4); absence is fine
    # OBS-04: parallel write to engine_events table (best-effort).
    try:
        from engine.engine_events import persist_event
        persist_event(event_type, data)
    except Exception:
        pass  # DB write failure must not break the pipeline
