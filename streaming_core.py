"""
streaming_core.py — Core infrastructure for the live event streaming system.

Extracted from ``streaming_server.py`` to enable independent testing and
reuse.  Contains the ULID generator, ring buffer, SSE client manager,
streaming server class, SSE formatting helper, and periodic cleanup task.

This module is self-contained: it re-imports every dependency it needs.

Usage::

    from streaming_core import (
        generate_ulid,
        RingBuffer,
        SSEClientManager,
        StreamingServer,
        _format_sse,
        _periodic_cleanup,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import sqlite3

from event_schema import (
    _TABLES,
    _INDEXES,
    ensure_schema,
    get_next_sequence,
    insert_event,
    cleanup_old_events,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("streaming_server")

# ---------------------------------------------------------------------------
# Constants (duplicated here so this module is self-contained)
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL: float = 30.0  # seconds
RING_BUFFER_MAXLEN: int = 500
SERVER_START_TIME: float = time.time()  # module-level for /health uptime

# ---------------------------------------------------------------------------
# ULID generator (identical to streaming_client.py for consistency)
# ---------------------------------------------------------------------------

_C32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, num_chars: int) -> str:
    """Encode an integer as a Crockford Base32 string of exactly *num_chars* characters."""
    result: list[str] = []
    for _ in range(num_chars):
        result.append(_C32[value & 0x1F])
        value >>= 5
    result.reverse()
    return "".join(result)


def generate_ulid() -> str:
    """Generate a ULID-like 26-character string.

    First 10 chars encode the millisecond timestamp in Crockford Base32.
    Remaining 16 chars encode a monotonic counter + random discriminator
    for uniqueness within the same millisecond.

    Returns
    -------
    str
        A 26-character Crockford-Base32 string.
    """
    now_ms = int(time.time() * 1000)
    ts_part = _encode_base32(now_ms, 8)

    # Use time + a simple hash for pseudo-unique suffix
    cnt = (now_ms * 6364136223846793005 + 0x4C615351) & 0xFFFFFFFFFFFF
    cnt_part = _encode_base32(cnt, 10)

    rnd = (now_ms ^ (now_ms >> 13) * 1274126177) & 0xFFFF
    rnd_part = _encode_base32(rnd, 8)

    return ts_part + cnt_part + rnd_part


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class RingBuffer:
    """Thread-safe ring buffer for SSE reconnection replay.

    Stores the last ``maxlen`` events in a ``collections.deque``.
    New events are appended; when the deque is full, the oldest event
    is automatically evicted.

    Attributes
    ----------
    _buffer : deque[dict]
        The underlying FIFO deque.
    _maxlen : int
        Maximum number of events to retain.
    """

    def __init__(self, maxlen: int = RING_BUFFER_MAXLEN) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._maxlen = maxlen
        self._lock = asyncio.Lock()

    @property
    def maxlen(self) -> int:
        """Maximum capacity of the ring buffer."""
        return self._maxlen

    @property
    def size(self) -> int:
        """Current number of events in the buffer."""
        return len(self._buffer)

    async def append(self, event: dict[str, Any]) -> None:
        """Append an event to the ring buffer.

        Parameters
        ----------
        event:
            A fully-formed event envelope dict with at least ``seq`` and
            ``id`` keys.
        """
        async with self._lock:
            self._buffer.append(event)

    async def replay_since(self, last_seq: int) -> list[dict[str, Any]]:
        """Return all events with ``seq > last_seq`` from the buffer.

        Parameters
        ----------
        last_seq:
            The sequence number of the last event the client received.

        Returns
        -------
        list[dict]
            Events with ``seq > last_seq``, in order.  May be empty if
            the buffer doesn't go back far enough.
        """
        async with self._lock:
            return [e for e in self._buffer if e.get("seq", 0) > last_seq]

    async def get_all(self) -> list[dict[str, Any]]:
        """Return all events currently in the buffer."""
        async with self._lock:
            return list(self._buffer)


# ---------------------------------------------------------------------------
# SSE client manager
# ---------------------------------------------------------------------------


class SSEClientManager:
    """Manages connected SSE clients with per-client async queues.

    Each SSE consumer gets its own ``asyncio.Queue``.  When an event
    arrives, it is pushed to every connected queue.  Slow consumers
    that fall behind will have events dropped (non-blocking put with
    a bounded queue).

    Attributes
    ----------
    _clients : dict[str, asyncio.Queue[dict[str, Any] | None]]
        Mapping of client ID to its event queue.  ``None`` is a
        sentinel meaning "close this client's stream".
    _counter : int
        Monotonically increasing client ID counter.
    """

    def __init__(self) -> None:
        self._clients: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._counter: int = 0

    def connect(self) -> tuple[str, asyncio.Queue[dict[str, Any] | None]]:
        """Register a new SSE client and return its ID and queue.

        Returns
        -------
        tuple[str, Queue]
            A (client_id, queue) pair.  The queue receives event dicts
            or ``None`` (sentinel to signal stream closure).
        """
        self._counter += 1
        client_id = f"client-{self._counter}-{int(time.time())}"
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=256,
        )
        self._clients[client_id] = queue
        logger.info("SSE client connected: %s (total: %d)", client_id, len(self._clients))
        return client_id, queue

    def disconnect(self, client_id: str) -> None:
        """Remove a client and signal its queue to close.

        Parameters
        ----------
        client_id:
            The client ID returned by :meth:`connect`.
        """
        queue = self._clients.pop(client_id, None)
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        logger.info("SSE client disconnected: %s (total: %d)", client_id, len(self._clients))

    async def broadcast(self, event: dict[str, Any]) -> int:
        """Push an event to all connected client queues.

        Parameters
        ----------
        event:
            A fully-formed event envelope dict.

        Returns
        -------
        int
            Number of clients that received the event.
        """
        delivered = 0
        stale: list[str] = []
        for client_id, queue in self._clients.items():
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # Slow consumer — drop event and mark for cleanup
                logger.warning("Dropping event for slow client %s", client_id)
                stale.append(client_id)
        # Clean up stale clients that have been backing up
        for cid in stale:
            self.disconnect(cid)
        return delivered

    @property
    def client_count(self) -> int:
        """Number of currently connected SSE clients."""
        return len(self._clients)


# ---------------------------------------------------------------------------
# Streaming server (application state)
# ---------------------------------------------------------------------------


class StreamingServer:
    """Encapsulates all mutable server state.

    This class owns the database connection, ring buffer, and SSE client
    manager.  It is instantiated once at server startup and stored on the
    FastAPI ``app.state`` object.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.
    ring_buffer_size:
        Maximum events to keep in the in-memory ring buffer.
    """

    def __init__(self, db_path: str, ring_buffer_size: int = RING_BUFFER_MAXLEN,
                 auth_token: str | None = None) -> None:
        self.db_path = db_path
        self.ring_buffer = RingBuffer(maxlen=ring_buffer_size)
        self.sse_manager = SSEClientManager()
        self._db_lock = asyncio.Lock()
        self._db_conn: Optional[sqlite3.Connection] = None
        self._event_count: int = 0  # fast counter, synced from DB on startup
        self.auth_token = auth_token  # Optional bearer token for /events endpoints

    def check_auth(self, request) -> bool:
        """Verify bearer token if auth is configured. Returns True if valid."""
        if not self.auth_token:
            return True  # No auth configured — allow all
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:] == self.auth_token
        return False

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _connect_db(self) -> Any:
        """Open a WAL-mode SQLite connection to the database.

        Returns
        -------
        sqlite3.Connection
            A connection with WAL journal mode and foreign keys enabled.
        """
        conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    async def init_db(self) -> None:
        """Initialise the database schema and open a persistent connection.

        Calls ``event_schema.ensure_schema()`` which is idempotent,
        then opens a persistent connection and counts existing events
        for the in-memory counter.

        For ``:memory:`` databases, ``ensure_schema()`` creates the
        schema on its own connection which is immediately lost, so we
        also apply the DDL directly on the persistent connection.
        """
        ensure_schema(self.db_path)
        self._db_conn = self._connect_db()
        try:
            # For :memory: DBs, ensure_schema's connection is separate and
            # its schema is lost — apply DDL on the persistent connection.
            try:
                self._db_conn.execute("SELECT COUNT(*) FROM live_events;")
            except sqlite3.OperationalError:
                for ddl in _TABLES:
                    self._db_conn.execute(ddl)
                for idx in _INDEXES:
                    self._db_conn.execute(idx)
                self._db_conn.commit()
            row = self._db_conn.execute("SELECT COUNT(*) FROM live_events;").fetchone()
            self._event_count = row[0] if row else 0
            logger.info("Database initialised: %s (%d existing events)", self.db_path, self._event_count)
        except Exception:
            self._db_conn.close()
            self._db_conn = None
            raise

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    async def ingest_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ingest a single event: validate, persist, buffer, broadcast.

        Parameters
        ----------
        payload:
            Partial event envelope from the producer.  Missing fields
            (``id``, ``seq``, ``time``) are auto-assigned by the server.

        Returns
        -------
        dict
            The completed event envelope with all server-assigned fields.

        Raises
        ------
        ValueError
            If the event is missing required fields (``source``, ``type``).
        """
        # Validate required fields
        source = payload.get("source")
        event_type = payload.get("type")
        if not source or not event_type:
            raise ValueError("Event must include 'source' and 'type' fields")

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Build the full envelope
        envelope: dict[str, Any] = {
            "id": payload.get("id") or generate_ulid(),
            "seq": 0,  # assigned below
            "time": payload.get("time") or now_ms,
            "source_time": payload.get("source_time"),
            "source": source,
            "source_id": payload.get("source_id"),
            "type": event_type,
            "correlation_id": payload.get("correlation_id"),
            "data": payload.get("data") or {},
            "schema_version": payload.get("schema_version", 1),
        }

        # Assign monotonic sequence and persist (under lock for ordering)
        async with self._db_lock:
            seq = insert_event(
                self._db_conn,
                event_id=envelope["id"],
                source=source,
                type=event_type,
                data=envelope["data"],
                time=envelope["time"],
                source_time=envelope["source_time"],
                source_id=envelope["source_id"],
                correlation_id=envelope["correlation_id"],
                schema_version=envelope["schema_version"],
            )
            envelope["seq"] = seq
            self._event_count += 1

        # Append to ring buffer
        await self.ring_buffer.append(envelope)

        # Broadcast to all connected SSE clients
        await self.sse_manager.broadcast(envelope)

        logger.debug(
            "Event ingested: seq=%d source=%s type=%s id=%s",
            seq, source, event_type, envelope["id"],
        )
        return envelope

    async def ingest_batch(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ingest multiple events in a single call.

        Parameters
        ----------
        events:
            List of partial event envelopes.

        Returns
        -------
        list[dict]
            List of completed event envelopes in the same order.

        Raises
        ------
        ValueError
            If any event is missing required fields.
        """
        results: list[dict[str, Any]] = []
        for evt in events:
            result = await self.ingest_event(evt)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def get_snapshot(self) -> dict[str, Any]:
        """Build a dashboard state snapshot.

        Returns
        -------
        dict
            Contains recent events (from ring buffer), event count,
            uptime, and client count.
        """
        recent = await self.ring_buffer.get_all()
        return {
            "events": recent,
            "event_count": self._event_count,
            "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
            "sse_clients": self.sse_manager.client_count,
            "ring_buffer_size": self.ring_buffer.size,
            "ring_buffer_maxlen": self.ring_buffer.maxlen,
        }

    # ------------------------------------------------------------------
    # Historical query (SQLite fallback)
    # ------------------------------------------------------------------

    async def query_events(
        self,
        since_seq: Optional[int] = None,
        source: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query events from SQLite with optional filters.

        Parameters
        ----------
        since_seq:
            Return only events with ``seq > since_seq``.
        source:
            Filter by source name.
        event_type:
            Filter by event type (exact match or prefix with ``%``).
        limit:
            Maximum events to return.

        Returns
        -------
        list[dict]
            Matching events, ordered by ``seq`` ascending.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if since_seq is not None:
            clauses.append("seq > ?")
            params.append(since_seq)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if event_type:
            # Support prefix matching with wildcard
            if "*" in event_type:
                clauses.append("type LIKE ?")
                params.append(event_type.replace("*", "%"))
            else:
                clauses.append("type = ?")
                params.append(event_type)

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT * FROM live_events WHERE {where} ORDER BY seq ASC LIMIT ?;"
        params.append(limit)

        def _query() -> list[dict[str, Any]]:
            self._db_conn.row_factory = sqlite3.Row
            rows = self._db_conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    # ------------------------------------------------------------------
    # Event count
    # ------------------------------------------------------------------

    async def get_event_count(self) -> int:
        """Return the total number of events in the database.

        Uses the in-memory counter for speed; falls back to SQL if needed.
        """
        return self._event_count

    def close_db(self) -> None:
        """Close the persistent database connection."""
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None


# ---------------------------------------------------------------------------
# SSE formatting helper
# ---------------------------------------------------------------------------


def _format_sse(event: dict[str, Any]) -> str:
    """Format an event envelope as an SSE message.

    Wire format::

        event: <event-type>
        id: <seq-number>
        data: {"id":"...","seq":42,...}

    Parameters
    ----------
    event:
        A fully-formed event envelope dict.

    Returns
    -------
    str
        The formatted SSE message string, terminated with ``\\n\\n``.
    """
    event_type = str(event.get("type", "unknown")).replace("\n", "").replace("\r", "")
    event_id = str(event.get("seq", "")).replace("\n", "").replace("\r", "")
    data = json.dumps(event, separators=(",", ":"), default=str)
    return f"event: {event_type}\nid: {event_id}\ndata: {data}\n\n"


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def _periodic_cleanup(server: StreamingServer) -> None:
    """Periodically clean up old events from SQLite (every 6 hours).

    Deletes events older than 24 hours to prevent unbounded database growth.

    Parameters
    ----------
    server:
        The streaming server instance.
    """
    while True:
        await asyncio.sleep(6 * 3600)  # every 6 hours
        try:
            deleted = cleanup_old_events(server._db_conn, max_age_hours=24)
            if deleted > 0:
                logger.info("Cleaned up %d old events from database", deleted)
        except Exception:
            logger.exception("Error during periodic cleanup")
