"""
dsh_adapter.py — Bridges DSH session events to the live streaming server.

Polls a running DSH instance (port 3080) for session events and projects
them into the event envelope format consumed by ``StreamingClient`` (port 3081).

Zero external dependencies — uses only the standard library (urllib, asyncio,
json, logging, signal, argparse).

Usage::

    # Live mode — polls DSH on :3080, forwards to streaming server on :3081
    python3 dsh_adapter.py

    # Custom endpoints
    python3 dsh_adapter.py --dsh-url http://127.0.0.1:3080 --stream-url http://127.0.0.1:3081

    # Mock/offline mode — generates synthetic events for testing
    python3 dsh_adapter.py --mock

    # Shorter poll interval
    python3 dsh_adapter.py --poll-interval 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Local import — StreamingClient lives in the same directory.
# Uses a lazy import so the module can still be imported for type-checking
# even when streaming_client.py is not on sys.path.
# ---------------------------------------------------------------------------

def _import_streaming_client():
    """Lazy-import StreamingClient from the same package directory."""
    import importlib
    import os
    import sys as _sys

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    mod = importlib.import_module("streaming_client")
    return mod.StreamingClient


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DSH_URL = "http://127.0.0.1:3080"
DEFAULT_STREAM_URL = "http://127.0.0.1:3081"
DEFAULT_POLL_INTERVAL = 2.0  # seconds

# Maximum byte length for any single data value before truncation.
_MAX_DATA_BYTES = 2048

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event-type mapping: DSH raw type → stream type
# ---------------------------------------------------------------------------

_DSH_EVENT_MAP: Dict[str, str] = {
    "turn/start":             "dsh.turn.start",
    "turn/end":               "dsh.turn.end",
    "step/start":             "dsh.step.start",
    "step/end":               "dsh.step.end",
    "tool/call":              "dsh.tool.call",
    "tool/result":            "dsh.tool.result",
    "assistant/chunk":        "dsh.assistant.chunk",
    "assistant/message":      "dsh.assistant.message",
    "llm/retry":              "dsh.llm.retry",
    "goal/change":            "dsh.goal.change",
    "subagent/descriptor":    "dsh.subagent.spawned",
    "compaction/start":       "dsh.compaction.start",
    "compaction/end":         "dsh.compaction.end",
}

# All known DSH event types — used by mock mode and to filter unknowns.
_ALL_DSH_TYPES: List[str] = list(_DSH_EVENT_MAP.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_value(val: Any, max_bytes: int = _MAX_DATA_BYTES) -> Any:
    """Truncate a string value that exceeds *max_bytes*.

    Non-string values are returned unchanged.  If truncation occurs the
    string is shortened and ``"…[truncated]"`` is appended.
    """
    if isinstance(val, str):
        raw = val.encode("utf-8")
        if len(raw) > max_bytes:
            truncated = raw[:max_bytes].decode("utf-8", errors="ignore")
            return truncated + "…[truncated]"
    return val


def _truncate_data(data: Dict[str, Any], max_bytes: int = _MAX_DATA_BYTES) -> Dict[str, Any]:
    """Walk a data dict and truncate every oversized string value."""
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = _truncate_data(v, max_bytes)
        elif isinstance(v, list):
            out[k] = [_truncate_value(item, max_bytes) for item in v]
        else:
            out[k] = _truncate_value(v, max_bytes)
    return out


def _safe_json_get(url: str, timeout: float = 5.0) -> Optional[Any]:
    """GET *url* and return parsed JSON, or ``None`` on any failure."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if body.strip():
                return json.loads(body)
            return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("_safe_json_get %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# DSHAdapter
# ---------------------------------------------------------------------------

class DSHAdapter:
    """Bridges DSH session events to the streaming server via HTTP polling.

    The adapter discovers active DSH sessions, polls each one for new events
    since the last seen sequence number, projects them into the standard
    streaming envelope, and forwards them to the streaming server through a
    :class:`StreamingClient` instance.

    Parameters
    ----------
    dsh_url:
        Base URL of the DSH instance (default ``http://127.0.0.1:3080``).
    stream_url:
        Base URL of the streaming server (default ``http://127.0.0.1:3081``).
    poll_interval:
        Seconds between poll cycles (default ``2.0``).
    mock:
        When ``True``, generate synthetic DSH events instead of polling a
        real instance.  Useful for testing without a running DSH.
    """

    def __init__(
        self,
        dsh_url: str = DEFAULT_DSH_URL,
        stream_url: str = DEFAULT_STREAM_URL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        mock: bool = False,
    ) -> None:
        self._dsh_url = dsh_url.rstrip("/")
        self._stream_url = stream_url.rstrip("/")
        self._poll_interval = max(0.5, poll_interval)
        self._mock = mock

        self._client: Any = None  # StreamingClient — lazily created
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Per-session tracking: session_id → last seen sequence number
        self._session_seqs: Dict[str, int] = {}
        # Sessions we already know about (for mock mode)
        self._known_sessions: Set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_client(self) -> None:
        """Create the StreamingClient on first use."""
        if self._client is None:
            StreamingClient = _import_streaming_client()
            self._client = StreamingClient(
                base_url=self._stream_url,
                timeout=5.0,
                max_retries=2,
                backoff_seconds=0.5,
            )

    async def start(self) -> None:
        """Start the adapter polling loop.  Runs until :meth:`stop` is called."""
        self._ensure_client()
        self._running = True
        self._shutdown_event.clear()

        logger.info(
            "DSHAdapter starting  dsh=%s  stream=%s  poll=%.1fs  mock=%s",
            self._dsh_url,
            self._stream_url,
            self._poll_interval,
            self._mock,
        )

        try:
            while self._running and not self._shutdown_event.is_set():
                if self._mock:
                    await self._mock_cycle()
                else:
                    await self._poll_cycle()
                # Sleep in small increments so we can respond to shutdown quickly
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._poll_interval,
                    )
                    # If we get here, shutdown was signalled
                    break
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("DSHAdapter task cancelled")
        finally:
            self._running = False
            logger.info("DSHAdapter stopped")

    async def stop(self) -> None:
        """Signal the adapter to stop gracefully."""
        logger.info("DSHAdapter stop requested")
        self._running = False
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Live polling
    # ------------------------------------------------------------------

    async def _poll_sessions(self) -> List[str]:
        """Discover active DSH session IDs.

        Expects ``GET {dsh_url}/api/sessions`` to return either:
        - A JSON array of session objects (each with an ``"id"`` key), or
        - A JSON object with a ``"sessions"`` key containing the array.

        Returns an empty list (without logging errors) when the endpoint is
        unreachable — this is normal when DSH is not running.
        """
        url = f"{self._dsh_url}/api/sessions"
        payload = await asyncio.get_event_loop().run_in_executor(
            None, _safe_json_get, url
        )
        if payload is None:
            # DSH unreachable — not an error in steady state
            return []

        # Normalise to a list of session dicts
        if isinstance(payload, list):
            sessions = payload
        elif isinstance(payload, dict):
            sessions = payload.get("sessions", payload.get("data", []))
        else:
            return []

        ids: List[str] = []
        for s in sessions:
            sid = s.get("id") or s.get("session_id") or s.get("sessionId")
            if sid:
                ids.append(str(sid))
        return ids

    async def _poll_session_events(self, session_id: str, since_seq: int) -> List[Dict[str, Any]]:
        """Fetch new events for *session_id* after *since_seq*.

        Expects ``GET {dsh_url}/api/sessions/{id}/events?since={seq}`` to
        return either a JSON array of event objects or a dict with an
        ``"events"`` key.

        Each event object is expected to have at least:
        - ``type`` (str) — the DSH event type
        - ``seq`` (int) — monotonic sequence number

        Additional keys are carried through as-is into the event payload.
        """
        url = f"{self._dsh_url}/api/sessions/{session_id}/events?since={since_seq}"
        payload = await asyncio.get_event_loop().run_in_executor(
            None, _safe_json_get, url
        )
        if payload is None:
            return []

        # Normalise
        if isinstance(payload, list):
            raw_events = payload
        elif isinstance(payload, dict):
            raw_events = payload.get("events", payload.get("data", []))
        else:
            return []

        # Project each raw event into the streaming envelope
        projected: List[Dict[str, Any]] = []
        for raw in raw_events:
            envelope = self._project_event(session_id, raw)
            if envelope is not None:
                projected.append(envelope)
        return projected

    # ------------------------------------------------------------------
    # Event projection
    # ------------------------------------------------------------------

    def _project_event(self, session_id: str, raw_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Project a DSH raw event into the streaming envelope format.

        Returns ``None`` when the event type is unknown and should be
        silently skipped.
        """
        raw_type = raw_event.get("type", "")
        stream_type = _DSH_EVENT_MAP.get(raw_type)
        if stream_type is None:
            logger.debug("Skipping unmapped DSH event type: %s", raw_type)
            return None

        # Build the data payload from everything except routing fields
        data: Dict[str, Any] = {}
        for k, v in raw_event.items():
            if k in ("type", "seq", "session_id", "sessionId"):
                continue
            data[k] = v

        data = _truncate_data(data)

        envelope: Dict[str, Any] = {
            "source": "dsh",
            "type": stream_type,
            "source_id": session_id,
            "data": data,
        }

        # Preserve sequence number if present (helps with dedup on server side)
        if "seq" in raw_event:
            envelope["dsh_seq"] = raw_event["seq"]

        return envelope

    # ------------------------------------------------------------------
    # Forwarding (fire-and-forget)
    # ------------------------------------------------------------------

    def _emit(self, envelope: Dict[str, Any]) -> None:
        """Fire-and-forget emit.  Logs failures but never raises."""
        try:
            self._client.emit(envelope)
        except Exception as exc:
            logger.warning("Failed to emit event %s/%s: %s",
                           envelope.get("source"), envelope.get("type"), exc)

    def _emit_batch(self, envelopes: List[Dict[str, Any]]) -> None:
        """Fire-and-forget batch emit."""
        if not envelopes:
            return
        try:
            self._client.emit_batch(envelopes)
        except Exception as exc:
            logger.warning("Failed to emit batch (%d events): %s", len(envelopes), exc)

    # ------------------------------------------------------------------
    # Override: poll cycle with emit (used by the public start() loop)
    # ------------------------------------------------------------------

    async def _poll_cycle(self) -> None:  # noqa: F811 — overrides the earlier definition
        """Single poll cycle: discover sessions, fetch new events, emit."""
        session_ids = await self._poll_sessions()
        if not session_ids:
            return

        batch: List[Dict[str, Any]] = []
        for sid in session_ids:
            since = self._session_seqs.get(sid, 0)
            events = await self._poll_session_events(sid, since)
            if events:
                max_seq = max(e.get("dsh_seq", 0) for e in events)
                if max_seq > since:
                    self._session_seqs[sid] = max_seq
                batch.extend(events)

        # Fire-and-forget: emit the whole batch at once
        if batch:
            await asyncio.get_event_loop().run_in_executor(
                None, self._emit_batch, batch
            )

    # ------------------------------------------------------------------
    # Mock / offline mode
    # ------------------------------------------------------------------

    async def _mock_cycle(self) -> None:
        """Generate a burst of synthetic DSH events for testing."""
        # Create or reuse a mock session
        if not self._known_sessions:
            mock_session_id = f"mock-session-{random.randint(1000, 9999)}"
            self._known_sessions.add(mock_session_id)
            self._session_seqs[mock_session_id] = 0
        else:
            mock_session_id = random.choice(list(self._known_sessions))

        seq_base = self._session_seqs.get(mock_session_id, 0)

        # Generate 1–4 events in a realistic lifecycle burst
        burst_size = random.randint(1, 4)
        events: List[Dict[str, Any]] = []

        for i in range(burst_size):
            seq = seq_base + i + 1
            raw = self._generate_mock_event(mock_session_id, seq)
            envelope = self._project_event(mock_session_id, raw)
            if envelope is not None:
                events.append(envelope)

        self._session_seqs[mock_session_id] = seq_base + burst_size

        if events:
            await asyncio.get_event_loop().run_in_executor(
                None, self._emit_batch, events
            )
            logger.debug(
                "Mock: emitted %d events for %s (seq %d→%d)",
                len(events), mock_session_id, seq_base, seq_base + burst_size,
            )

    @staticmethod
    def _generate_mock_event(session_id: str, seq: int) -> Dict[str, Any]:
        """Create a single synthetic DSH event with plausible data."""
        pick = random.random()

        if pick < 0.25:
            return {
                "type": "turn/start",
                "seq": seq,
                "session_id": session_id,
                "turnNumber": random.randint(1, 20),
            }
        elif pick < 0.40:
            return {
                "type": "turn/end",
                "seq": seq,
                "session_id": session_id,
                "turnNumber": random.randint(1, 20),
                "reason": random.choice(["complete", "blocked", "max-turns"]),
                "durationMs": random.randint(500, 30000),
            }
        elif pick < 0.55:
            return {
                "type": "step/start",
                "seq": seq,
                "session_id": session_id,
                "turn": random.randint(1, 20),
                "step": random.randint(1, 10),
            }
        elif pick < 0.65:
            return {
                "type": "step/end",
                "seq": seq,
                "session_id": session_id,
                "turn": random.randint(1, 20),
                "step": random.randint(1, 10),
                "durationMs": random.randint(100, 5000),
            }
        elif pick < 0.75:
            return {
                "type": "tool/call",
                "seq": seq,
                "session_id": session_id,
                "callId": f"call-{random.randint(1000, 9999)}",
                "name": random.choice(["bash", "read", "write", "edit", "grep", "glob"]),
                "arguments": json.dumps({"command": "ls -la /tmp"}),
            }
        elif pick < 0.82:
            return {
                "type": "tool/result",
                "seq": seq,
                "session_id": session_id,
                "callId": f"call-{random.randint(1000, 9999)}",
                "name": random.choice(["bash", "read", "write"]),
                "isError": random.random() < 0.1,
                "durationMs": random.randint(50, 2000),
            }
        elif pick < 0.88:
            return {
                "type": "assistant/chunk",
                "seq": seq,
                "session_id": session_id,
                "chunkType": random.choice(["text", "reasoning", "tool-call"]),
            }
        elif pick < 0.92:
            return {
                "type": "assistant/message",
                "seq": seq,
                "session_id": session_id,
                "content": "I'll help you implement the streaming adapter...",
            }
        elif pick < 0.95:
            return {
                "type": "llm/retry",
                "seq": seq,
                "session_id": session_id,
                "attempt": random.randint(1, 3),
                "reason": random.choice(["timeout", "rate-limit", "context-overflow"]),
            }
        elif pick < 0.97:
            return {
                "type": "goal/change",
                "seq": seq,
                "session_id": session_id,
                "goal": {"objective": "Implement DSH adapter", "phase": "active"},
            }
        elif pick < 0.99:
            return {
                "type": "subagent/descriptor",
                "seq": seq,
                "session_id": session_id,
                "childSessionId": f"child-{random.randint(10000, 99999)}",
            }
        else:
            return {
                "type": random.choice(["compaction/start", "compaction/end"]),
                "seq": seq,
                "session_id": session_id,
            }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh_adapter",
        description=(
            "Bridge DSH session events to the live streaming server. "
            "Polls DSH on :3080 and forwards events to :3081."
        ),
    )
    parser.add_argument(
        "--dsh-url",
        default=DEFAULT_DSH_URL,
        help=f"DSH base URL (default: {DEFAULT_DSH_URL})",
    )
    parser.add_argument(
        "--stream-url",
        default=DEFAULT_STREAM_URL,
        help=f"Streaming server URL (default: {DEFAULT_STREAM_URL})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between poll cycles (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help="Generate synthetic DSH events for testing (no live DSH needed)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )
    return parser


async def _run_adapter(args: argparse.Namespace) -> None:
    """Run the adapter with signal handlers installed."""
    adapter = DSHAdapter(
        dsh_url=args.dsh_url,
        stream_url=args.stream_url,
        poll_interval=args.poll_interval,
        mock=args.mock,
    )

    loop = asyncio.get_event_loop()

    def _signal_handler(signame: str) -> None:
        logger.info("Received %s — shutting down", signame)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(adapter.stop()))

    # Install signal handlers for clean shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig.name)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: _signal_handler(s))

    await adapter.start()


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    mode_label = "MOCK" if args.mock else "LIVE"
    logger.info("dsh_adapter [%s] — press Ctrl+C to stop", mode_label)

    try:
        asyncio.run(_run_adapter(args))
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting")


if __name__ == "__main__":
    main()
