"""
dsh_adapter_ws.py — SSE/WebSocket variant of the DSH adapter.

Provides lower-latency event forwarding from DSH to the streaming server
via Server-Sent Events (SSE) or WebSocket, with automatic fallback to
HTTP polling when neither transport is available.

Transport priority:
  1. SSE (Server-Sent Events) — preferred, uses aiohttp, zero extra deps
  2. WebSocket — requires ``websockets`` library (pip install websockets)
  3. HTTP polling — always available, uses stdlib urllib (DSHAdapter fallback)

DSH exposes two event streams:
  - /api/events.mux  — session events (turns, steps, tool calls, assistant chunks)
  - /api/events.host — session lifecycle (added, removed, status changes)

Both SSE and WebSocket carry the same frame structure::

    {
      "type": "server-request",
      "rpcId": "...",
      "method": "session/event",          // or other frame types
      "payload": {
        "type": "session/event",
        "sessionId": "...",
        "event": {
          "type": "turn/start",           // DSH event type
          "seq": 42,
          "time": 1735689600000,
          "data": { ... }
        }
      }
    }

Usage::

    # SSE mode (preferred)
    python3 dsh_adapter_ws.py --transport sse

    # WebSocket mode (requires websockets library)
    python3 dsh_adapter_ws.py --transport ws

    # Auto-detect best transport
    python3 dsh_adapter_ws.py --transport auto

    # Mock mode (same as dsh_adapter.py)
    python3 dsh_adapter_ws.py --mock
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Feature detection: aiohttp (SSE) and websockets (WS)
# ---------------------------------------------------------------------------

try:
    import aiohttp  # type: ignore[import-untyped]
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    import websockets  # type: ignore[import-untyped]
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

# ---------------------------------------------------------------------------
# Lazy import: StreamingClient from the same directory
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
# Import shared constants from the base adapter
# ---------------------------------------------------------------------------

# We import these from dsh_adapter so the event mapping stays DRY.
from dsh_adapter import (
    _DSH_EVENT_MAP,
    _truncate_data,
    _safe_json_get,
    DEFAULT_DSH_URL,
    DEFAULT_STREAM_URL,
    DEFAULT_POLL_INTERVAL,
    _MAX_DATA_BYTES,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# DSH SSE/WebSocket paths
_MUX_EVENTS_PATH = "/api/events.mux"
_HOST_EVENTS_PATH = "/api/events.host"

# Reconnect backoff bounds (seconds)
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0


# ---------------------------------------------------------------------------
# SSE adapter (preferred transport)
# ---------------------------------------------------------------------------

class _SSETransport:
    """SSE-based event transport using aiohttp.

    Connects to /api/events.mux and /api/events.host via HTTP GET with
    ``Accept: text/event-stream``.  Each frame is a JSON-encoded
    ``ServerRequest`` envelope containing a ``MuxFrame`` or ``HostFrame``
    payload.
    """

    def __init__(self, dsh_url: str) -> None:
        self._dsh_url = dsh_url.rstrip("/")
        self._mux_url = f"{self._dsh_url}{_MUX_EVENTS_PATH}"
        self._host_url = f"{self._dsh_url}{_HOST_EVENTS_PATH}"

    async def run(
        self,
        on_frame: Any,
        shutdown: asyncio.Event,
    ) -> None:
        """Connect to both SSE streams and feed frames to *on_frame*.

        Reconnects automatically on transient failures with exponential
        backoff.  Returns only when *shutdown* is set or the adapter
        encounters a fatal error.
        """
        if not HAS_AIOHTTP:
            raise RuntimeError("aiohttp is required for SSE transport")

        backoff = _RECONNECT_BASE

        while not shutdown.is_set():
            try:
                logger.info("SSE connecting to %s", self._mux_url)
                backoff = _RECONNECT_BASE

                timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    # Start both streams concurrently
                    mux_task = asyncio.create_task(
                        self._pump_stream(session, self._mux_url, on_frame, shutdown, "mux")
                    )
                    host_task = asyncio.create_task(
                        self._pump_stream(session, self._host_url, on_frame, shutdown, "host")
                    )

                    # Wait until one finishes (disconnect) or shutdown
                    done, pending = await asyncio.wait(
                        [mux_task, host_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Cancel the other stream
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    # Check for fatal errors
                    for t in done:
                        exc = t.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            logger.warning("SSE stream error: %s", exc)

            except asyncio.CancelledError:
                logger.info("SSE transport cancelled")
                break
            except Exception as exc:
                logger.warning("SSE connection failed: %s", exc)

            if shutdown.is_set():
                break

            logger.info("SSE reconnecting in %.1fs…", backoff)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=backoff)
                break  # shutdown was signalled during backoff
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_MAX)

    async def _pump_stream(
        self,
        session: Any,
        url: str,
        on_frame: Any,
        shutdown: asyncio.Event,
        label: str,
    ) -> None:
        """Read SSE frames from one stream and call *on_frame* for each."""
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"SSE {label} returned HTTP {resp.status}")
            logger.info("SSE %s stream connected (HTTP %d)", label, resp.status)

            buffer = ""
            async for raw_chunk in resp.content.iter_any():
                if shutdown.is_set():
                    break

                buffer += raw_chunk.decode("utf-8", errors="replace")

                # SSE frames are separated by blank lines
                while "\n\n" in buffer:
                    frame_text, buffer = buffer.split("\n\n", 1)
                    for line in frame_text.split("\n"):
                        line = line.strip()
                        if not line or line.startswith(":"):
                            # Empty line or comment — skip
                            continue
                        if line.startswith("data: "):
                            payload_str = line[6:]
                            try:
                                frame = json.loads(payload_str)
                                await on_frame(frame)
                            except json.JSONDecodeError as exc:
                                logger.debug("SSE frame JSON error: %s", exc)


# ---------------------------------------------------------------------------
# WebSocket adapter (requires websockets library)
# ---------------------------------------------------------------------------

class _WSTransport:
    """WebSocket-based event transport.

    Connects to /api/events.mux and /api/events.host via WebSocket upgrade.
    DSH WebSocket endpoints are "downlink only" — the server sends frames,
    the client never sends.
    """

    def __init__(self, dsh_url: str) -> None:
        # Convert http:// to ws://, https:// to wss://
        ws_url = dsh_url.rstrip("/")
        if ws_url.startswith("https://"):
            ws_url = "wss://" + ws_url[8:]
        elif ws_url.startswith("http://"):
            ws_url = "ws://" + ws_url[5:]
        elif not ws_url.startswith("ws"):
            ws_url = "ws://" + ws_url
        self._ws_base = ws_url
        self._mux_url = f"{ws_url}{_MUX_EVENTS_PATH}"
        self._host_url = f"{ws_url}{_HOST_EVENTS_PATH}"

    async def run(
        self,
        on_frame: Any,
        shutdown: asyncio.Event,
    ) -> None:
        """Connect to both WebSocket streams and feed frames to *on_frame*."""
        if not HAS_WEBSOCKETS:
            raise RuntimeError("websockets library is required for WS transport")

        backoff = _RECONNECT_BASE

        while not shutdown.is_set():
            try:
                logger.info("WebSocket connecting to %s", self._mux_url)
                backoff = _RECONNECT_BASE

                async with websockets.connect(self._mux_url) as mux_ws, \
                           websockets.connect(self._host_url) as host_ws:
                    logger.info("WebSocket streams connected")
                    mux_task = asyncio.create_task(
                        self._pump_ws(mux_ws, on_frame, shutdown, "mux")
                    )
                    host_task = asyncio.create_task(
                        self._pump_ws(host_ws, on_frame, shutdown, "host")
                    )

                    done, pending = await asyncio.wait(
                        [mux_task, host_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    for t in done:
                        exc = t.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            logger.warning("WebSocket stream error: %s", exc)

            except asyncio.CancelledError:
                logger.info("WebSocket transport cancelled")
                break
            except Exception as exc:
                logger.warning("WebSocket connection failed: %s", exc)

            if shutdown.is_set():
                break

            logger.info("WebSocket reconnecting in %.1fs…", backoff)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_MAX)

    async def _pump_ws(
        self,
        ws: Any,
        on_frame: Any,
        shutdown: asyncio.Event,
        label: str,
    ) -> None:
        """Read WebSocket frames and call *on_frame* for each."""
        async for raw_msg in ws:
            if shutdown.is_set():
                break

            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8", errors="replace")

            try:
                frame = json.loads(raw_msg)
                await on_frame(frame)
            except json.JSONDecodeError as exc:
                logger.debug("WebSocket frame JSON error: %s", exc)


# ---------------------------------------------------------------------------
# HTTP polling adapter (fallback, reuses DSHAdapter logic)
# ---------------------------------------------------------------------------

async def _http_poll_fallback(
    dsh_url: str,
    stream_url: str,
    poll_interval: float,
    shutdown: asyncio.Event,
) -> None:
    """Fall back to HTTP polling by delegating to DSHAdapter."""
    from dsh_adapter import DSHAdapter

    adapter = DSHAdapter(
        dsh_url=dsh_url,
        stream_url=stream_url,
        poll_interval=poll_interval,
    )
    # Monkey-patch the shutdown event so stop() propagates
    adapter._shutdown_event = shutdown
    adapter._running = True
    adapter._ensure_client()

    logger.info("Falling back to HTTP polling (poll_interval=%.1fs)", poll_interval)

    try:
        while not shutdown.is_set():
            await adapter._poll_cycle()
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
                break
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        adapter._running = False


# ---------------------------------------------------------------------------
# DSHWebSocketAdapter
# ---------------------------------------------------------------------------

class DSHWebSocketAdapter:
    """DSH adapter using SSE/WebSocket transport with HTTP fallback.

    Connects to DSH's real-time event streams (/api/events.mux and
    /api/events.host) to receive session events with minimal latency,
    then projects and forwards them to the streaming server via
    ``StreamingClient``.

    Parameters
    ----------
    dsh_url:
        Base URL of the DSH instance (default ``http://127.0.0.1:3080``).
    stream_url:
        Base URL of the streaming server (default ``http://127.0.0.1:3081``).
    transport:
        Preferred transport: ``"sse"`` (Server-Sent Events), ``"ws"``
        (WebSocket), or ``"auto"`` (try SSE, then WS, then HTTP polling).
    mock:
        When ``True``, generate synthetic DSH events for testing.
    poll_interval:
        Seconds between poll cycles for the HTTP polling fallback.
    """

    def __init__(
        self,
        dsh_url: str = DEFAULT_DSH_URL,
        stream_url: str = DEFAULT_STREAM_URL,
        transport: str = "auto",
        mock: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._dsh_url = dsh_url
        self._stream_url = stream_url
        self._transport = transport
        self._mock = mock
        self._poll_interval = poll_interval

        self._client: Any = None  # StreamingClient — lazily created
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Per-session tracking for dedup
        self._session_seqs: Dict[str, int] = {}
        self._known_sessions: Set[str] = set()

        # Statistics
        self._frames_received = 0
        self._events_emitted = 0
        self._transport_used: Optional[str] = None

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
        """Start the adapter.  Runs until :meth:`stop` is called."""
        self._ensure_client()
        self._running = True
        self._shutdown_event.clear()

        logger.info(
            "DSHWebSocketAdapter starting  dsh=%s  stream=%s  transport=%s  mock=%s",
            self._dsh_url,
            self._stream_url,
            self._transport,
            self._mock,
        )

        try:
            if self._mock:
                await self._mock_loop()
            elif self._transport == "auto":
                await self._auto_transport()
            elif self._transport == "sse":
                await self._run_sse()
            elif self._transport == "ws":
                await self._run_ws()
            elif self._transport == "http":
                await self._run_http_fallback()
            else:
                logger.error("Unknown transport: %s", self._transport)
        except asyncio.CancelledError:
            logger.info("DSHWebSocketAdapter task cancelled")
        finally:
            self._running = False
            logger.info(
                "DSHWebSocketAdapter stopped  transport=%s  frames=%d  emitted=%d",
                self._transport_used,
                self._frames_received,
                self._events_emitted,
            )

    async def stop(self) -> None:
        """Signal the adapter to stop gracefully."""
        logger.info("DSHWebSocketAdapter stop requested")
        self._running = False
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Auto-detect best transport
    # ------------------------------------------------------------------

    async def _auto_transport(self) -> None:
        """Try transports in priority order: SSE → WS → HTTP polling."""
        # 1. Try SSE
        if HAS_AIOHTTP:
            logger.info("Auto-detect: trying SSE transport…")
            self._transport_used = "sse"
            await self._run_sse()
            if not self._running:
                return
        else:
            logger.info("Auto-detect: aiohttp not available, skipping SSE")

        # 2. Try WebSocket
        if HAS_WEBSOCKETS and self._running:
            logger.info("Auto-detect: trying WebSocket transport…")
            self._transport_used = "ws"
            await self._run_ws()
            if not self._running:
                return

        # 3. Fall back to HTTP polling
        if self._running:
            logger.info("Auto-detect: falling back to HTTP polling")
            self._transport_used = "http"
            await self._run_http_fallback()

    # ------------------------------------------------------------------
    # Transport runners
    # ------------------------------------------------------------------

    async def _run_sse(self) -> None:
        """Run the SSE transport."""
        transport = _SSETransport(self._dsh_url)
        await transport.run(self._handle_frame, self._shutdown_event)

    async def _run_ws(self) -> None:
        """Run the WebSocket transport."""
        transport = _WSTransport(self._dsh_url)
        await transport.run(self._handle_frame, self._shutdown_event)

    async def _run_http_fallback(self) -> None:
        """Run the HTTP polling fallback."""
        await _http_poll_fallback(
            self._dsh_url,
            self._stream_url,
            self._poll_interval,
            self._shutdown_event,
        )

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    async def _handle_frame(self, frame: Dict[str, Any]) -> None:
        """Process a single SSE/WebSocket frame from DSH.

        Frame structure::

            {
              "type": "server-request",
              "rpcId": "...",
              "method": "session/event",
              "payload": {
                "type": "session/event",
                "sessionId": "...",
                "event": { "type": "turn/start", "seq": 42, ... }
              }
            }
        """
        self._frames_received += 1

        frame_type = frame.get("type", "")
        if frame_type != "server-request":
            # Not a standard frame — skip (e.g., stream/error)
            if frame_type == "stream/error":
                logger.warning("Stream error: %s", frame.get("error", {}))
            return

        payload = frame.get("payload", {})
        payload_type = payload.get("type", "")

        if payload_type == "session/event":
            session_id = payload.get("sessionId", "")
            event = payload.get("event", {})

            # Project into streaming envelope
            envelope = self._project_event(session_id, event)
            if envelope is not None:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._emit, envelope
                )
                self._events_emitted += 1

        elif payload_type == "session/subscribed":
            session_id = payload.get("sessionId", "")
            last_seq = payload.get("lastSeq", 0)
            if session_id not in self._session_seqs or self._session_seqs[session_id] < last_seq:
                self._session_seqs[session_id] = last_seq
            logger.debug("Subscribed to session %s (lastSeq=%d)", session_id, last_seq)

        elif payload_type in ("host/session-added", "host/session-removed",
                               "host/session-status"):
            session_id = payload.get("sessionId", "")
            if payload_type == "host/session-removed":
                self._session_seqs.pop(session_id, None)
                self._known_sessions.discard(session_id)
            elif payload_type == "host/session-added":
                self._known_sessions.add(session_id)

        # All other frame types (approval, question, queue, jobs, projection, etc.)
        # are silently ignored — we only forward session events.

    # ------------------------------------------------------------------
    # Event projection (shared logic with DSHAdapter)
    # ------------------------------------------------------------------

    def _project_event(
        self,
        session_id: str,
        raw_event: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Project a DSH raw event into the streaming envelope format.

        Returns ``None`` when the event type is unknown and should be
        silently skipped.  Identical logic to ``DSHAdapter._project_event()``.
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

        # Preserve sequence number if present
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
            logger.warning(
                "Failed to emit event %s/%s: %s",
                envelope.get("source"),
                envelope.get("type"),
                exc,
            )

    def _emit_batch(self, envelopes: List[Dict[str, Any]]) -> None:
        """Fire-and-forget batch emit."""
        if not envelopes:
            return
        try:
            self._client.emit_batch(envelopes)
        except Exception as exc:
            logger.warning("Failed to emit batch (%d events): %s", len(envelopes), exc)

    # ------------------------------------------------------------------
    # Mock mode (reuses DSHAdapter mock logic)
    # ------------------------------------------------------------------

    async def _mock_loop(self) -> None:
        """Generate synthetic DSH events for testing."""
        self._transport_used = "mock"
        import random
        from dsh_adapter import DSHAdapter

        # Reuse the mock event generator from DSHAdapter
        adapter = DSHAdapter.__new__(DSHAdapter)
        adapter._session_seqs = self._session_seqs
        adapter._known_sessions = self._known_sessions
        adapter._client = self._client

        while self._running and not self._shutdown_event.is_set():
            if not self._known_sessions:
                mock_session_id = f"mock-session-{random.randint(1000, 9999)}"
                self._known_sessions.add(mock_session_id)
                self._session_seqs[mock_session_id] = 0
            else:
                mock_session_id = random.choice(list(self._known_sessions))

            seq_base = self._session_seqs.get(mock_session_id, 0)
            burst_size = random.randint(1, 4)
            events: List[Dict[str, Any]] = []

            for i in range(burst_size):
                seq = seq_base + i + 1
                raw = DSHAdapter._generate_mock_event(mock_session_id, seq)
                envelope = self._project_event(mock_session_id, raw)
                if envelope is not None:
                    events.append(envelope)

            self._session_seqs[mock_session_id] = seq_base + burst_size

            if events:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._emit_batch, events
                )
                self._frames_received += burst_size
                self._events_emitted += len(events)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._poll_interval,
                )
                break
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh_adapter_ws",
        description=(
            "Bridge DSH session events to the live streaming server via "
            "SSE/WebSocket with automatic HTTP polling fallback."
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
        "--transport",
        choices=["auto", "sse", "ws", "http"],
        default="auto",
        help=(
            "Event transport: auto (SSE→WS→HTTP), sse, ws, or http polling "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help="Generate synthetic DSH events for testing (no live DSH needed)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between poll cycles for HTTP fallback (default: {DEFAULT_POLL_INTERVAL})",
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
    adapter = DSHWebSocketAdapter(
        dsh_url=args.dsh_url,
        stream_url=args.stream_url,
        transport=args.transport,
        mock=args.mock,
        poll_interval=args.poll_interval,
    )

    loop = asyncio.get_event_loop()

    def _signal_handler(signame: str) -> None:
        logger.info("Received %s — shutting down", signame)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(adapter.stop()))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig.name)
        except NotImplementedError:
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

    logging.getLogger("urllib3").setLevel(logging.WARNING)

    mode_label = "MOCK" if args.mock else "LIVE"
    transport_label = args.transport.upper()
    logger.info(
        "dsh_adapter_ws [%s] [%s] — press Ctrl+C to stop",
        mode_label,
        transport_label,
    )

    # Log feature detection
    if not HAS_AIOHTTP:
        logger.warning("aiohttp not installed — SSE transport unavailable")
    if not HAS_WEBSOCKETS:
        logger.warning("websockets not installed — WS transport unavailable (pip install websockets)")

    try:
        asyncio.run(_run_adapter(args))
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting")


if __name__ == "__main__":
    main()
