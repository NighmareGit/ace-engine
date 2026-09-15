"""
test_streaming_split.py — Tests for the split streaming modules.

After S.1-S.6, the streaming system is split into:
  1. streaming_core.py  — RingBuffer, SSEClientManager, StreamingServer, ULID, _format_sse
  2. streaming_routes.py — create_app(), route handlers, lifespan
  3. streaming_server.py — CLI entry point only

This test file validates each module directly after the split.

Run with:
    python3 -m pytest test_streaming_split.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Ensure coder-harness is importable
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from streaming_core import (
    RingBuffer,
    SSEClientManager,
    StreamingServer,
    _format_sse,
    generate_ulid,
    _encode_base32,
    _C32,
    HEARTBEAT_INTERVAL,
    RING_BUFFER_MAXLEN,
)
from streaming_routes import create_app


# ═══════════════════════════════════════════════════════════════════════════
# Helper: run async tests
# ═══════════════════════════════════════════════════════════════════════════

def run_async(coro):
    """Run an async coroutine in a new event loop (for use in unittest)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════════════
# RingBuffer tests
# ═══════════════════════════════════════════════════════════════════════════

class TestRingBuffer(unittest.TestCase):
    """Tests for the RingBuffer class in streaming_core.py."""

    def test_ring_buffer_append_and_get(self):
        """Append 3 events, verify get_all() returns all 3."""
        buf = RingBuffer(maxlen=10)
        run_async(buf.append({"seq": 1, "id": "a"}))
        run_async(buf.append({"seq": 2, "id": "b"}))
        run_async(buf.append({"seq": 3, "id": "c"}))

        all_events = run_async(buf.get_all())
        self.assertEqual(len(all_events), 3)
        self.assertEqual(all_events[0]["seq"], 1)
        self.assertEqual(all_events[1]["seq"], 2)
        self.assertEqual(all_events[2]["seq"], 3)

    def test_ring_buffer_maxlen(self):
        """Create buffer with maxlen=5, append 10 events, verify only 5 retained."""
        buf = RingBuffer(maxlen=5)
        for i in range(1, 11):
            run_async(buf.append({"seq": i, "id": f"evt-{i}"}))

        self.assertEqual(buf.size, 5)
        all_events = run_async(buf.get_all())
        seqs = [e["seq"] for e in all_events]
        # Oldest 5 should be evicted; newest 5 (6-10) retained
        self.assertEqual(seqs, [6, 7, 8, 9, 10])

    def test_ring_buffer_replay_since(self):
        """Append events with seq 1-10, replay_since(7) returns seq 8,9,10."""
        buf = RingBuffer(maxlen=100)
        for i in range(1, 11):
            run_async(buf.append({"seq": i, "id": f"evt-{i}"}))

        result = run_async(buf.replay_since(7))
        self.assertEqual(len(result), 3)
        self.assertEqual([e["seq"] for e in result], [8, 9, 10])

    def test_ring_buffer_replay_since_empty(self):
        """replay_since(999) on a buffer with seqs 1-10 returns empty list."""
        buf = RingBuffer(maxlen=100)
        for i in range(1, 11):
            run_async(buf.append({"seq": i, "id": f"evt-{i}"}))

        result = run_async(buf.replay_since(999))
        self.assertEqual(result, [])

    def test_ring_buffer_size(self):
        """After appending 3 events, size property returns 3."""
        buf = RingBuffer(maxlen=10)
        self.assertEqual(buf.size, 0)
        run_async(buf.append({"seq": 1, "id": "a"}))
        self.assertEqual(buf.size, 1)
        run_async(buf.append({"seq": 2, "id": "b"}))
        run_async(buf.append({"seq": 3, "id": "c"}))
        self.assertEqual(buf.size, 3)


# ═══════════════════════════════════════════════════════════════════════════
# SSEClientManager tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSSEClientManager(unittest.TestCase):
    """Tests for SSEClientManager connect/disconnect/broadcast."""

    def test_sse_manager_connect(self):
        """connect() returns (client_id, queue) and increments client_count."""
        mgr = SSEClientManager()
        self.assertEqual(mgr.client_count, 0)

        cid, queue = mgr.connect()
        self.assertIsInstance(cid, str)
        self.assertIsInstance(queue, asyncio.Queue)
        self.assertEqual(mgr.client_count, 1)

    def test_sse_manager_disconnect(self):
        """After connect then disconnect, client_count returns to 0."""
        mgr = SSEClientManager()
        cid, _ = mgr.connect()
        self.assertEqual(mgr.client_count, 1)

        mgr.disconnect(cid)
        self.assertEqual(mgr.client_count, 0)

    def test_sse_manager_broadcast(self):
        """Connect 2 clients, broadcast event, verify both queues receive it."""
        mgr = SSEClientManager()
        _, q1 = mgr.connect()
        _, q2 = mgr.connect()

        event = {"seq": 42, "type": "task.start", "id": "abc"}
        run_async(mgr.broadcast(event))

        self.assertFalse(q1.empty())
        self.assertFalse(q2.empty())
        self.assertEqual(q1.get_nowait(), event)
        self.assertEqual(q2.get_nowait(), event)

    def test_sse_manager_broadcast_count(self):
        """broadcast() returns the number of clients that received the event."""
        mgr = SSEClientManager()
        mgr.connect()
        mgr.connect()

        delivered = run_async(mgr.broadcast({"seq": 1}))
        self.assertEqual(delivered, 2)

        # Zero clients
        mgr2 = SSEClientManager()
        delivered_zero = run_async(mgr2.broadcast({"seq": 1}))
        self.assertEqual(delivered_zero, 0)

    def test_sse_manager_disconnect_signals_queue(self):
        """After disconnect, the queue receives None sentinel."""
        mgr = SSEClientManager()
        cid, queue = mgr.connect()
        mgr.disconnect(cid)

        sentinel = queue.get_nowait()
        self.assertIsNone(sentinel)


# ═══════════════════════════════════════════════════════════════════════════
# ULID tests
# ═══════════════════════════════════════════════════════════════════════════

class TestULID(unittest.TestCase):
    """Tests for the ULID generator in streaming_core.py."""

    def test_ulid_format(self):
        """generate_ulid() returns a 26-character string."""
        ulid = generate_ulid()
        self.assertIsInstance(ulid, str)
        self.assertEqual(len(ulid), 26)

    def test_ulid_crockford_base32(self):
        """ULID only contains characters from Crockford Base32 alphabet."""
        for _ in range(100):
            ulid = generate_ulid()
            for ch in ulid:
                self.assertIn(
                    ch, _C32,
                    f"Invalid character '{ch}' in ULID: {ulid}"
                )

    def test_ulid_uniqueness(self):
        """1000 generated ULIDs are all unique."""
        ulids = set()
        for _ in range(1000):
            ulids.add(generate_ulid())
            time.sleep(0.002)  # ensure different millisecond per ULID
        self.assertEqual(len(ulids), 1000, "Duplicate ULIDs detected!")

    def test_ulid_monotonic(self):
        """Two ULIDs generated in sequence, second is lexicographically greater."""
        first = generate_ulid()
        time.sleep(0.002)  # ensure different millisecond
        second = generate_ulid()
        self.assertGreater(second, first,
                           f"ULID not monotonic: {first} → {second}")


# ═══════════════════════════════════════════════════════════════════════════
# _format_sse tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatSSE(unittest.TestCase):
    """Tests for the _format_sse wire format."""

    def test_format_sse_structure(self):
        """Output contains event:, id:, and data: lines separated by newlines."""
        event = {"type": "task.start", "seq": 42, "data": {"key": "val"}}
        result = _format_sse(event)

        self.assertIn("event:", result)
        self.assertIn("id:", result)
        self.assertIn("data:", result)
        # Ends with double newline (SSE termination)
        self.assertTrue(result.endswith("\n\n"))

    def test_format_sse_data_is_json(self):
        """The data line is valid JSON matching the input event dict."""
        event = {"type": "task.start", "seq": 42, "id": "ULID123", "data": {"task_id": "T01"}}
        result = _format_sse(event)

        lines = result.strip().split("\n")
        data_line = lines[2]
        self.assertTrue(data_line.startswith("data: "))
        data_json = json.loads(data_line[6:])
        self.assertEqual(data_json["type"], "task.start")
        self.assertEqual(data_json["seq"], 42)
        self.assertEqual(data_json["data"]["task_id"], "T01")

    def test_format_sse_id_is_seq(self):
        """The id line value matches the event's seq field."""
        event = {"type": "task.start", "seq": 42, "data": {}}
        result = _format_sse(event)

        lines = result.strip().split("\n")
        id_line = lines[1]
        self.assertTrue(id_line.startswith("id: "))
        self.assertEqual(id_line, "id: 42")

    def test_format_sse_event_type(self):
        """The event line matches the event's type field."""
        event = {"type": "gpu.snapshot", "seq": 1, "data": {}}
        result = _format_sse(event)

        lines = result.strip().split("\n")
        event_line = lines[0]
        self.assertTrue(event_line.startswith("event: "))
        self.assertEqual(event_line, "event: gpu.snapshot")


# ═══════════════════════════════════════════════════════════════════════════
# StreamingServer tests (with :memory: DB)
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamingServer(unittest.TestCase):
    """Tests for StreamingServer using an in-memory database."""

    def setUp(self):
        """Create a StreamingServer with :memory: DB and initialise it."""
        self.server = StreamingServer(db_path=":memory:")
        run_async(self.server.init_db())

    def tearDown(self):
        """Clean up the server's database connection."""
        self.server.close_db()

    def test_server_init(self):
        """Create server with :memory: DB, verify attributes."""
        srv = StreamingServer(db_path=":memory:")
        self.assertEqual(srv.db_path, ":memory:")
        self.assertIsInstance(srv.ring_buffer, RingBuffer)
        self.assertIsInstance(srv.sse_manager, SSEClientManager)
        self.assertEqual(srv._event_count, 0)

    def test_server_check_auth_no_token(self):
        """Server without auth token, check_auth() returns True."""
        srv = StreamingServer(db_path=":memory:", auth_token=None)
        req = MagicMock()
        req.headers = {}
        self.assertTrue(srv.check_auth(req))

    def test_server_check_auth_with_token(self):
        """Server with auth token, check_auth() returns True for correct bearer token."""
        srv = StreamingServer(db_path=":memory:", auth_token="secret123")
        req = MagicMock()
        req.headers = {"Authorization": "Bearer <token>"}
        self.assertTrue(srv.check_auth(req))

    def test_server_check_auth_wrong_token(self):
        """Server with auth token, check_auth() returns False for wrong token."""
        srv = StreamingServer(db_path=":memory:", auth_token="secret123")
        req = MagicMock()
        req.headers = {"Authorization": "Bearer <wrong-token>"}
        self.assertFalse(srv.check_auth(req))

    def test_server_check_auth_no_header(self):
        """Server with auth token, check_auth() returns False when no Authorization header."""
        srv = StreamingServer(db_path=":memory:", auth_token="secret123")
        req = MagicMock()
        req.headers = {}
        self.assertFalse(srv.check_auth(req))

    def test_server_ingest_event(self):
        """Ingest event, verify returned envelope has required fields."""
        result = run_async(self.server.ingest_event({
            "source": "engine",
            "type": "task.start",
            "data": {"task_id": "T01"},
        }))

        self.assertIn("id", result)
        self.assertIn("seq", result)
        self.assertIn("time", result)
        self.assertIn("source", result)
        self.assertIn("type", result)
        self.assertEqual(result["source"], "engine")
        self.assertEqual(result["type"], "task.start")
        self.assertEqual(result["seq"], 1)

    def test_server_ingest_event_auto_seq(self):
        """Ingest 3 events, verify seq numbers are 1, 2, 3."""
        for i in range(3):
            result = run_async(self.server.ingest_event({
                "source": "engine",
                "type": "task.start",
                "id": f"auto-seq-event-{i}",
            }))
            self.assertEqual(result["seq"], i + 1)

    def test_server_ingest_event_missing_source(self):
        """Ingest event without source field raises ValueError."""
        with self.assertRaises(ValueError):
            run_async(self.server.ingest_event({"type": "task.start"}))

    def test_server_ingest_event_missing_type(self):
        """Ingest event without type field raises ValueError."""
        with self.assertRaises(ValueError):
            run_async(self.server.ingest_event({"source": "engine"}))

    def test_server_get_event_count(self):
        """After ingesting 5 events, get_event_count() returns 5."""
        for i in range(5):
            run_async(self.server.ingest_event({
                "source": "engine",
                "type": "task.start",
                "id": f"count-event-{i}",
            }))

        count = run_async(self.server.get_event_count())
        self.assertEqual(count, 5)

    def test_server_query_events(self):
        """Ingest events of different types, query_events filters correctly."""
        run_async(self.server.ingest_event({"source": "engine", "type": "task.start", "id": "q-event-0"}))
        run_async(self.server.ingest_event({"source": "engine", "type": "gpu.snapshot", "id": "q-event-1"}))
        run_async(self.server.ingest_event({"source": "engine", "type": "task.start", "id": "q-event-2"}))

        results = run_async(self.server.query_events(event_type="task.start"))
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["type"], "task.start")

    def test_server_snapshot(self):
        """get_snapshot() returns dict with expected keys."""
        run_async(self.server.ingest_event({
            "source": "engine",
            "type": "task.start",
        }))

        snapshot = run_async(self.server.get_snapshot())
        self.assertIn("events", snapshot)
        self.assertIn("event_count", snapshot)
        self.assertIn("uptime_seconds", snapshot)
        self.assertIn("sse_clients", snapshot)
        self.assertIn("ring_buffer_size", snapshot)
        self.assertIn("ring_buffer_maxlen", snapshot)
        self.assertEqual(snapshot["event_count"], 1)
        self.assertEqual(snapshot["ring_buffer_size"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# create_app / routes tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCreateApp(unittest.TestCase):
    """Tests for create_app() and route registration."""

    def test_create_app_returns_fastapi(self):
        """create_app() returns a FastAPI instance."""
        from fastapi import FastAPI
        app = create_app()
        self.assertIsInstance(app, FastAPI)

    def test_create_app_has_routes(self):
        """The app has routes for /events, /stream, /health, /api/snapshot, /api/config, /api/panel."""
        app = create_app()
        route_paths = set()
        for route in app.routes:
            if hasattr(route, "path"):
                route_paths.add(route.path)

        expected_routes = ["/events", "/stream", "/health", "/api/snapshot", "/api/config", "/api/panel"]
        for path in expected_routes:
            self.assertIn(path, route_paths, f"Missing route: {path}")

    def test_create_app_lifespan(self):
        """The app has a lifespan handler."""
        app = create_app()
        # FastAPI stores the lifespan context on the router
        self.assertTrue(
            app.router.lifespan_context is not None,
            "App should have a lifespan handler",
        )

    def test_create_app_with_auth(self):
        """create_app(auth_token='secret') rejects unauthenticated POST /events."""
        from starlette.testclient import TestClient

        app = create_app(db_path=":memory:", auth_token="secret")
        client = TestClient(app, raise_server_exceptions=False)

        # Unauthenticated POST should return 401
        resp = client.post(
            "/events",
            json={"source": "engine", "type": "task.start"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_app_without_auth(self):
        """create_app() with no token allows POST /events without auth."""
        from starlette.testclient import TestClient

        app = create_app(db_path=":memory:")
        with TestClient(app, raise_server_exceptions=False) as client:
            # No auth token configured → POST should succeed
            resp = client.post(
                "/events",
                json={"source": "engine", "type": "task.start", "data": {}},
            )
            self.assertEqual(resp.status_code, 201)
            body = resp.json()
            self.assertEqual(body["source"], "engine")
            self.assertEqual(body["type"], "task.start")


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
