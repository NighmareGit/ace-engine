"""
test_streaming.py — Comprehensive unit tests for the live event streaming system.

Tests three modules:
  1. event_schema.py  — table creation, sequence generation, cleanup
  2. streaming_client.py — envelope building, ULID generation, emit (mock server)
  3. streaming_server.py — RingBuffer, SSEClientManager, SSE formatting, auth check

Run with:
    python3 -m pytest test_streaming.py -v
    # or
    python3 -m unittest test_streaming -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, mock_open, patch, PropertyMock

# ---------------------------------------------------------------------------
# Ensure coder-harness is importable
# ---------------------------------------------------------------------------
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from event_schema import (
    ensure_schema,
    get_next_sequence,
    cleanup_old_events,
    insert_event,
    get_replay_cursor,
    set_replay_cursor,
    _TABLES,
    _INDEXES,
)
from streaming_client import (
    StreamingClient,
    generate_ulid,
    _encode_base32,
    _C32,
    VALID_ENGINE_EVENT_TYPES,
    VALID_SOURCES,
)
from streaming_core import (
    RingBuffer,
    SSEClientManager,
    StreamingServer,
    _format_sse,
    generate_ulid as server_generate_ulid,
    _encode_base32 as server_encode_base32,
    HEARTBEAT_INTERVAL,
    RING_BUFFER_MAXLEN,
)


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
# 1. event_schema tests
# ═══════════════════════════════════════════════════════════════════════════

class TestEventSchema(unittest.TestCase):
    """Tests for event_schema.py — SQLite schema, sequences, cleanup."""

    def setUp(self):
        """Create a temporary SQLite database for each test."""
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self._tmp.name
        self._tmp.close()
        # Remove the empty file so ensure_schema creates it fresh
        os.unlink(self.db_path)

    def tearDown(self):
        """Clean up temp database files."""
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ------------------------------------------------------------------
    # test_ensure_schema_creates_tables
    # ------------------------------------------------------------------
    def test_ensure_schema_creates_tables(self):
        """ensure_schema should create all expected tables and indexes."""
        ensure_schema(self.db_path)

        conn = sqlite3.connect(self.db_path)
        try:
            # Check tables exist
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            }
            self.assertIn("live_events", tables)
            self.assertIn("source_sequences", tables)
            self.assertIn("replay_cursors", tables)

            # Check indexes exist
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%';"
                ).fetchall()
            }
            self.assertGreaterEqual(len(indexes), 6, f"Expected ≥6 indexes, got {indexes}")
        finally:
            conn.close()

    def test_ensure_schema_idempotent(self):
        """Calling ensure_schema twice should not raise."""
        ensure_schema(self.db_path)
        ensure_schema(self.db_path)  # second call — no error

    # ------------------------------------------------------------------
    # test_get_next_sequence_increments
    # ------------------------------------------------------------------
    def test_get_next_sequence_increments(self):
        """get_next_sequence should return strictly increasing values."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            seq1 = get_next_sequence(conn, "engine")
            seq2 = get_next_sequence(conn, "engine")
            self.assertEqual(seq1, 1)
            self.assertEqual(seq2, 2)
            self.assertGreater(seq2, seq1)
        finally:
            conn.close()

    def test_get_next_sequence_separate_sources(self):
        """Different sources should have independent sequences."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            s1 = get_next_sequence(conn, "engine")
            s2 = get_next_sequence(conn, "dsh")
            s3 = get_next_sequence(conn, "engine")
            self.assertEqual(s1, 1)
            self.assertEqual(s2, 1)  # independent source
            self.assertEqual(s3, 2)  # engine continues from 1
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # test_cleanup_old_events
    # ------------------------------------------------------------------
    def test_cleanup_old_events(self):
        """Events older than max_age_hours should be deleted."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            old_ms = now_ms - (48 * 3600 * 1000)  # 48 hours ago

            # Insert one old event and one fresh event
            insert_event(
                conn,
                event_id="OLD01",
                source="engine",
                type="task.start",
                time=old_ms,
            )
            insert_event(
                conn,
                event_id="FRESH01",
                source="engine",
                type="task.complete",
                time=now_ms,
            )

            # Cleanup events older than 24 hours
            deleted = cleanup_old_events(conn, max_age_hours=24)
            self.assertEqual(deleted, 1)

            # Only fresh event should remain
            remaining = conn.execute("SELECT id FROM live_events;").fetchall()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0][0], "FRESH01")
        finally:
            conn.close()

    def test_cleanup_old_events_no_deletes_when_all_fresh(self):
        """No rows should be deleted if all events are within the age window."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            insert_event(
                conn,
                event_id="FRESH01",
                source="engine",
                type="task.start",
                time=now_ms,
            )
            deleted = cleanup_old_events(conn, max_age_hours=24)
            self.assertEqual(deleted, 0)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # test_insert_event
    # ------------------------------------------------------------------
    def test_insert_event(self):
        """insert_event should store the event and return its sequence number."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            seq = insert_event(
                conn,
                event_id="ULID-TEST-001",
                source="engine",
                type="task.start",
                data={"task_id": "T01", "title": "Test"},
                time=1700000000000,
                source_time=1699999999000,
                source_id="session-1",
                correlation_id="corr-abc",
                schema_version=1,
            )
            self.assertEqual(seq, 1)

            # Verify stored correctly
            row = conn.execute(
                "SELECT id, seq, source, type, data, source_time, source_id, correlation_id "
                "FROM live_events WHERE id = ?;",
                ("ULID-TEST-001",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "ULID-TEST-001")
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], "engine")
            self.assertEqual(row[3], "task.start")
            parsed_data = json.loads(row[4])
            self.assertEqual(parsed_data["task_id"], "T01")
            self.assertEqual(row[5], 1699999999000)
            self.assertEqual(row[6], "session-1")
            self.assertEqual(row[7], "corr-abc")
        finally:
            conn.close()

    def test_insert_event_empty_id_raises(self):
        """insert_event with empty event_id should raise ValueError."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(ValueError):
                insert_event(conn, event_id="", source="engine", type="task.start")
        finally:
            conn.close()

    def test_insert_event_string_data(self):
        """insert_event should accept pre-encoded JSON strings for data."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            insert_event(
                conn,
                event_id="ULID-STR-001",
                source="engine",
                type="gpu.snapshot",
                data='{"gpu_index": 0, "temp": 65.0}',
            )
            row = conn.execute(
                "SELECT data FROM live_events WHERE id = ?;",
                ("ULID-STR-001",),
            ).fetchone()
            self.assertEqual(row[0], '{"gpu_index": 0, "temp": 65.0}')
        finally:
            conn.close()

    def test_insert_event_none_data(self):
        """insert_event with data=None should default to '{}'."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            insert_event(
                conn,
                event_id="ULID-NONE-001",
                source="engine",
                type="task.start",
                data=None,
            )
            row = conn.execute(
                "SELECT data FROM live_events WHERE id = ?;",
                ("ULID-NONE-001",),
            ).fetchone()
            self.assertEqual(row[0], "{}")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # test_concurrent_sequences
    # ------------------------------------------------------------------
    def test_concurrent_sequences(self):
        """Two threads calling get_next_sequence should produce no duplicates."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            results_lock = threading.Lock()
            results: list[int] = []

            def worker():
                # Each thread opens its own connection for WAL concurrency
                c = sqlite3.connect(self.db_path, timeout=10)
                c.execute("PRAGMA journal_mode=WAL;")
                c.execute("PRAGMA busy_timeout=5000;")
                try:
                    for _ in range(20):
                        seq = get_next_sequence(c, "engine")
                        with results_lock:
                            results.append(seq)
                finally:
                    c.close()

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # 40 calls total → 40 unique sequences from 1..40
            self.assertEqual(len(results), 40)
            self.assertEqual(len(set(results)), 40, "Duplicate sequences detected!")
            self.assertEqual(min(results), 1)
            self.assertEqual(max(results), 40)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Replay cursor tests
    # ------------------------------------------------------------------
    def test_replay_cursor_set_and_get(self):
        """set_replay_cursor should persist and get_replay_cursor should retrieve."""
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            # No cursor initially
            self.assertIsNone(get_replay_cursor(conn, "client-1"))

            set_replay_cursor(conn, "client-1", 42)
            self.assertEqual(get_replay_cursor(conn, "client-1"), 42)

            # Upsert
            set_replay_cursor(conn, "client-1", 100)
            self.assertEqual(get_replay_cursor(conn, "client-1"), 100)
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 2. streaming_client tests
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamingClient(unittest.TestCase):
    """Tests for streaming_client.py — envelopes, ULID, emit (mocked)."""

    # ------------------------------------------------------------------
    # test_build_envelope
    # ------------------------------------------------------------------
    def test_build_envelope(self):
        """build_envelope should produce a dict with all required keys."""
        client = StreamingClient("http://localhost:9999")
        envelope = client.build_envelope(
            source="engine",
            event_type="task.start",
            data={"task_id": "T01"},
            source_id="session-1",
        )

        required_keys = {"id", "source_time", "source", "type", "data"}
        self.assertTrue(required_keys.issubset(envelope.keys()))
        self.assertEqual(envelope["source"], "engine")
        self.assertEqual(envelope["type"], "task.start")
        self.assertEqual(envelope["data"], {"task_id": "T01"})
        self.assertEqual(envelope["source_id"], "session-1")
        self.assertIsInstance(envelope["id"], str)
        self.assertIsInstance(envelope["source_time"], int)

    def test_build_envelope_no_source_id(self):
        """build_envelope without source_id should not include it."""
        client = StreamingClient("http://localhost:9999")
        envelope = client.build_envelope(source="dsh", event_type="task.complete")
        self.assertNotIn("source_id", envelope)

    def test_build_envelope_default_data(self):
        """build_envelope with no data should default to {}."""
        client = StreamingClient("http://localhost:9999")
        envelope = client.build_envelope(source="system", event_type="gpu.snapshot")
        self.assertEqual(envelope["data"], {})

    # ------------------------------------------------------------------
    # test_ulid_format
    # ------------------------------------------------------------------
    def test_ulid_format(self):
        """ULID should be 26 characters, all Crockford Base32."""
        for _ in range(100):
            ulid = generate_ulid()
            self.assertEqual(len(ulid), 26, f"ULID length is {len(ulid)}: {ulid}")
            for ch in ulid:
                self.assertIn(ch, _C32, f"Invalid character '{ch}' in ULID: {ulid}")

    # ------------------------------------------------------------------
    # test_ulid_monotonic
    # ------------------------------------------------------------------
    def test_ulid_monotonic(self):
        """Each ULID generated in sequence should be strictly greater than the previous."""
        prev = ""
        for _ in range(200):
            current = generate_ulid()
            if prev:
                self.assertGreater(current, prev, f"ULID not monotonic: {prev} → {current}")
            prev = current

    # ------------------------------------------------------------------
    # test_emit_returns_id
    # ------------------------------------------------------------------
    @patch("streaming_client.urllib.request.urlopen")
    def test_emit_returns_id(self, mock_urlopen):
        """emit should return the server's JSON response body on success."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "evt-001", "seq": 1}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = StreamingClient("http://localhost:9999")
        result = client.emit({"source": "engine", "type": "task.start", "data": {}})

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "evt-001")
        self.assertEqual(result["seq"], 1)
        mock_urlopen.assert_called_once()

    @patch("streaming_client.urllib.request.urlopen")
    def test_emit_auto_generates_id(self, mock_urlopen):
        """emit should auto-generate an id if missing from the event."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = StreamingClient("http://localhost:9999")
        result = client.emit({"source": "engine", "type": "task.start"})
        self.assertIsNotNone(result)

        # Verify the request was made with an auto-generated id
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        self.assertIn("id", body)
        self.assertEqual(len(body["id"]), 26)

    @patch("streaming_client.urllib.request.urlopen")
    def test_emit_empty_response(self, mock_urlopen):
        """emit should return default ok when server returns empty body."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = StreamingClient("http://localhost:9999")
        result = client.emit({"source": "engine", "type": "task.start"})
        self.assertEqual(result, {"status": "ok"})

    # ------------------------------------------------------------------
    # test_emit_failure_returns_none
    # ------------------------------------------------------------------
    @patch("streaming_client.urllib.request.urlopen")
    @patch("streaming_client.time.sleep")
    def test_emit_failure_returns_none(self, mock_sleep, mock_urlopen):
        """Unreachable server should cause emit to return None after retries."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        client = StreamingClient(
            "http://localhost:9999",
            max_retries=2,
            backoff_seconds=0.0,  # no sleep in tests
        )
        result = client.emit({"source": "engine", "type": "task.start"})
        self.assertIsNone(result)
        self.assertEqual(mock_urlopen.call_count, 2)

    # ------------------------------------------------------------------
    # test_emit_batch
    # ------------------------------------------------------------------
    @patch("streaming_client.urllib.request.urlopen")
    def test_emit_batch(self, mock_urlopen):
        """emit_batch should POST a wrapped list of events."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"count": 2}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = StreamingClient("http://localhost:9999")
        events = [
            client.build_envelope("engine", "pipeline.start", {"run_id": "R01"}),
            client.build_envelope("engine", "task.start", {"task_id": "T01"}),
        ]
        result = client.emit_batch(events)

        self.assertIsNotNone(result)
        self.assertEqual(result["count"], 2)

        # Verify the URL and payload structure
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertIn("/events/batch", req.full_url)
        body = json.loads(req.data.decode())
        self.assertIn("events", body)
        self.assertEqual(len(body["events"]), 2)

    @patch("streaming_client.urllib.request.urlopen")
    def test_emit_batch_auto_populates_ids(self, mock_urlopen):
        """emit_batch should auto-generate id and source_time for events missing them."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = StreamingClient("http://localhost:9999")
        events = [
            {"source": "engine", "type": "task.start"},
            {"source": "dsh", "type": "task.complete", "id": "existing-id"},
        ]
        client.emit_batch(events)

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        evts = body["events"]

        # First event got auto-generated id and source_time
        self.assertEqual(len(evts[0]["id"]), 26)
        self.assertIn("source_time", evts[0])

        # Second event kept its existing id
        self.assertEqual(evts[1]["id"], "existing-id")

    # ------------------------------------------------------------------
    # Token / auth header test
    # ------------------------------------------------------------------
    @patch("streaming_client.urllib.request.urlopen")
    def test_emit_sends_auth_header(self, mock_urlopen):
        """When token is set, emit should include Authorization header."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = StreamingClient("http://localhost:9999", token="my-secret")
        client.emit({"source": "engine", "type": "task.start"})

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer <token>")

    # ------------------------------------------------------------------
    # Client repr
    # ------------------------------------------------------------------
    def test_repr_with_token(self):
        client = StreamingClient("http://localhost:9999", token="abc")
        r = repr(client)
        self.assertIn("token=***", r)

    def test_repr_without_token(self):
        client = StreamingClient("http://localhost:9999")
        r = repr(client)
        self.assertIn("no-auth", r)

    # ------------------------------------------------------------------
    # _encode_base32
    # ------------------------------------------------------------------
    def test_encode_base32_zero(self):
        """Encoding 0 should produce all '0' characters."""
        result = _encode_base32(0, 5)
        self.assertEqual(result, "00000")

    def test_encode_base32_exact_width(self):
        """_encode_base32 should always produce exactly num_chars."""
        for val in [0, 1, 31, 32, 1023, 0xDEADBEEF]:
            for n in [5, 8, 10, 16]:
                result = _encode_base32(val, n)
                self.assertEqual(len(result), n)

    def test_valid_sources(self):
        """VALID_SOURCES should contain the expected source names."""
        self.assertIn("engine", VALID_SOURCES)
        self.assertIn("dsh", VALID_SOURCES)
        self.assertIn("system", VALID_SOURCES)

    def test_valid_engine_event_types(self):
        """VALID_ENGINE_EVENT_TYPES should contain expected types."""
        expected = {"pipeline.start", "task.start", "inference.start", "gpu.snapshot"}
        self.assertTrue(expected.issubset(VALID_ENGINE_EVENT_TYPES))


# ═══════════════════════════════════════════════════════════════════════════
# 3. streaming_server tests
# ═══════════════════════════════════════════════════════════════════════════

class TestRingBuffer(unittest.TestCase):
    """Tests for the in-memory RingBuffer in streaming_server.py."""

    # ------------------------------------------------------------------
    # test_ring_buffer_append
    # ------------------------------------------------------------------
    def test_ring_buffer_append(self):
        """Appending events should increase the buffer size."""
        buf = RingBuffer(maxlen=10)
        self.assertEqual(buf.size, 0)

        run_async(buf.append({"seq": 1, "id": "a"}))
        self.assertEqual(buf.size, 1)

        run_async(buf.append({"seq": 2, "id": "b"}))
        run_async(buf.append({"seq": 3, "id": "c"}))
        self.assertEqual(buf.size, 3)

    def test_ring_buffer_maxlen_property(self):
        """maxlen property should return the configured maximum."""
        buf = RingBuffer(maxlen=123)
        self.assertEqual(buf.maxlen, 123)

    # ------------------------------------------------------------------
    # test_ring_buffer_eviction
    # ------------------------------------------------------------------
    def test_ring_buffer_eviction(self):
        """When maxlen+1 events are appended, the oldest should be evicted."""
        buf = RingBuffer(maxlen=5)
        for i in range(1, 7):  # append 6 events into buffer of size 5
            run_async(buf.append({"seq": i, "id": f"evt-{i}"}))

        self.assertEqual(buf.size, 5)

        all_events = run_async(buf.get_all())
        seqs = [e["seq"] for e in all_events]
        # First event (seq=1) should have been evicted
        self.assertNotIn(1, seqs)
        self.assertEqual(seqs, [2, 3, 4, 5, 6])

    # ------------------------------------------------------------------
    # test_ring_buffer_replay_since
    # ------------------------------------------------------------------
    def test_ring_buffer_replay_since(self):
        """replay_since should return only events with seq > last_seq."""
        buf = RingBuffer(maxlen=100)
        for i in range(1, 11):
            run_async(buf.append({"seq": i, "id": f"evt-{i}"}))

        # Replay after seq 5 → should get 6, 7, 8, 9, 10
        result = run_async(buf.replay_since(5))
        self.assertEqual(len(result), 5)
        self.assertEqual([e["seq"] for e in result], [6, 7, 8, 9, 10])

    def test_ring_buffer_replay_since_empty(self):
        """replay_since with a seq beyond all events should return empty list."""
        buf = RingBuffer(maxlen=10)
        run_async(buf.append({"seq": 1, "id": "a"}))

        result = run_async(buf.replay_since(999))
        self.assertEqual(result, [])

    def test_ring_buffer_replay_since_zero(self):
        """replay_since(0) should return all events."""
        buf = RingBuffer(maxlen=10)
        for i in range(1, 6):
            run_async(buf.append({"seq": i}))

        result = run_async(buf.replay_since(0))
        self.assertEqual(len(result), 5)


class TestSSEClientManager(unittest.TestCase):
    """Tests for SSEClientManager connect/disconnect/broadcast."""

    # ------------------------------------------------------------------
    # test_sse_manager_connect_disconnect
    # ------------------------------------------------------------------
    def test_sse_manager_connect_disconnect(self):
        """connect should register a client; disconnect should remove it."""
        mgr = SSEClientManager()
        self.assertEqual(mgr.client_count, 0)

        cid, queue = mgr.connect()
        self.assertEqual(mgr.client_count, 1)
        self.assertIsInstance(queue, asyncio.Queue)

        mgr.disconnect(cid)
        self.assertEqual(mgr.client_count, 0)

    def test_sse_manager_connect_multiple(self):
        """Multiple connects should increment the client counter."""
        mgr = SSEClientManager()
        c1, _ = mgr.connect()
        c2, _ = mgr.connect()
        c3, _ = mgr.connect()
        self.assertEqual(mgr.client_count, 3)
        self.assertNotEqual(c1, c2)
        self.assertNotEqual(c2, c3)

    def test_sse_manager_disconnect_nonexistent(self):
        """Disconnecting an unknown client id should be a no-op."""
        mgr = SSEClientManager()
        mgr.disconnect("nonexistent-id")  # no exception

    # ------------------------------------------------------------------
    # test_sse_manager_broadcast
    # ------------------------------------------------------------------
    def test_sse_manager_broadcast(self):
        """broadcast should deliver the event to all connected client queues."""
        mgr = SSEClientManager()
        _, q1 = mgr.connect()
        _, q2 = mgr.connect()

        event = {"seq": 1, "type": "task.start", "id": "abc"}
        delivered = run_async(mgr.broadcast(event))
        self.assertEqual(delivered, 2)

        # Both queues should have the event
        self.assertFalse(q1.empty())
        self.assertFalse(q2.empty())
        self.assertEqual(q1.get_nowait(), event)
        self.assertEqual(q2.get_nowait(), event)

    def test_sse_manager_broadcast_no_clients(self):
        """broadcast with zero clients should return 0."""
        mgr = SSEClientManager()
        delivered = run_async(mgr.broadcast({"seq": 1}))
        self.assertEqual(delivered, 0)

    # ------------------------------------------------------------------
    # test_sse_manager_slow_consumer
    # ------------------------------------------------------------------
    def test_sse_manager_slow_consumer(self):
        """A slow consumer with a full queue should be evicted after broadcast."""
        mgr = SSEClientManager()
        _, fast_q = mgr.connect()
        cid_slow, slow_q = mgr.connect()

        # Fill the slow client's queue (maxsize=256)
        for _ in range(256):
            slow_q.put_nowait({"seq": -1})

        # Broadcast — slow client should be evicted
        event = {"seq": 999, "type": "overflow"}
        delivered = run_async(mgr.broadcast(event))

        # Fast client should receive, slow client should be evicted
        self.assertFalse(fast_q.empty())
        self.assertEqual(mgr.client_count, 1)  # slow client was removed

    def test_sse_manager_disconnect_sends_sentinel(self):
        """disconnect should place a None sentinel in the client queue."""
        mgr = SSEClientManager()
        cid, queue = mgr.connect()
        mgr.disconnect(cid)

        sentinel = queue.get_nowait()
        self.assertIsNone(sentinel)


class TestFormatSSE(unittest.TestCase):
    """Tests for the _format_sse wire format."""

    # ------------------------------------------------------------------
    # test_format_sse
    # ------------------------------------------------------------------
    def test_format_sse(self):
        """_format_sse should produce correct SSE wire format."""
        event = {
            "type": "task.start",
            "seq": 42,
            "id": "ULID123",
            "source": "engine",
            "data": {"task_id": "T01"},
        }
        result = _format_sse(event)

        # Should start with "event: "
        self.assertTrue(result.startswith("event: task.start\n"))
        # Should contain "id: 42"
        self.assertIn("id: 42\n", result)
        # Should contain "data: "
        self.assertIn("data: ", result)
        # Should end with double newline
        self.assertTrue(result.endswith("\n\n"))

        # Parse the data line
        lines = result.strip().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "event: task.start")
        self.assertEqual(lines[1], "id: 42")

        # data line should be valid JSON
        data_line = lines[2]
        self.assertTrue(data_line.startswith("data: "))
        data_json = json.loads(data_line[6:])
        self.assertEqual(data_json["type"], "task.start")
        self.assertEqual(data_json["seq"], 42)
        self.assertEqual(data_json["data"]["task_id"], "T01")

    def test_format_sse_no_seq(self):
        """_format_sse should handle events with missing seq."""
        event = {"type": "gpu.snapshot"}
        result = _format_sse(event)
        self.assertIn("id: \n", result)
        self.assertIn("event: gpu.snapshot\n", result)

    def test_format_sse_empty_event(self):
        """_format_sse with empty event should use defaults."""
        result = _format_sse({})
        self.assertIn("event: unknown\n", result)
        self.assertIn("id: \n", result)


class TestAuthCheck(unittest.TestCase):
    """Tests for StreamingServer.check_auth."""

    def _make_request(self, authorization: str = "") -> MagicMock:
        """Create a mock request with a given Authorization header."""
        req = MagicMock()
        req.headers = {"Authorization": authorization} if authorization else {}
        return req

    # ------------------------------------------------------------------
    # test_auth_check_valid
    # ------------------------------------------------------------------
    def test_auth_check_valid(self):
        """Valid bearer token should pass authentication."""
        server = StreamingServer(db_path=":memory:", auth_token="super-secret")
        req = self._make_request("Bearer <token>")
        self.assertTrue(server.check_auth(req))

    # ------------------------------------------------------------------
    # test_auth_check_invalid
    # ------------------------------------------------------------------
    def test_auth_check_invalid(self):
        """Wrong token should be rejected."""
        server = StreamingServer(db_path=":memory:", auth_token="super-secret")
        req = self._make_request("Bearer <wrong-token>")
        self.assertFalse(server.check_auth(req))

    def test_auth_check_no_bearer_prefix(self):
        """Authorization without a bearer prefix should be rejected."""
        server = StreamingServer(db_path=":memory:", auth_token="super-secret")
        req = self._make_request("Basic super-secret")
        self.assertFalse(server.check_auth(req))

    def test_auth_check_no_auth_header(self):
        """Missing Authorization header should be rejected."""
        server = StreamingServer(db_path=":memory:", auth_token="super-secret")
        req = self._make_request("")
        self.assertFalse(server.check_auth(req))

    # ------------------------------------------------------------------
    # test_auth_check_no_token_configured
    # ------------------------------------------------------------------
    def test_auth_check_no_token_configured(self):
        """No auth_token configured → all requests pass."""
        server = StreamingServer(db_path=":memory:", auth_token=None)
        req = self._make_request("")
        self.assertTrue(server.check_auth(req))

        req2 = self._make_request("Bearer <token>")
        self.assertTrue(server.check_auth(req2))


class TestServerULID(unittest.TestCase):
    """Tests for the ULID generator in streaming_server.py."""

    def test_server_ulid_format(self):
        """Server ULID should be 26 Crockford Base32 characters."""
        for _ in range(50):
            ulid = server_generate_ulid()
            self.assertEqual(len(ulid), 26)
            for ch in ulid:
                self.assertIn(ch, _C32)

    def test_server_encode_base32(self):
        """server_encode_base32 should match client _encode_base32."""
        test_vals = [0, 1, 42, 1023, 0xDEADBEEF]
        for val in test_vals:
            self.assertEqual(
                server_encode_base32(val, 8),
                _encode_base32(val, 8),
            )


class TestStreamingServerState(unittest.TestCase):
    """Tests for StreamingServer state management (ring buffer, event count)."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self._tmp.name
        self._tmp.close()
        os.unlink(self.db_path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def test_server_init_db(self):
        """init_db should create schema and set event count."""
        server = StreamingServer(db_path=self.db_path)
        run_async(server.init_db())

        # Verify schema was created
        conn = sqlite3.connect(self.db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        conn.close()
        self.assertIn("live_events", tables)
        self.assertEqual(server._event_count, 0)

    def test_server_ingest_event(self):
        """ingest_event should persist to DB, buffer, and return envelope."""
        server = StreamingServer(db_path=self.db_path)
        run_async(server.init_db())

        result = run_async(server.ingest_event({
            "source": "engine",
            "type": "task.start",
            "data": {"task_id": "T01"},
        }))

        self.assertEqual(result["source"], "engine")
        self.assertEqual(result["type"], "task.start")
        self.assertEqual(result["seq"], 1)
        self.assertIsNotNone(result["id"])
        self.assertEqual(result["data"], {"task_id": "T01"})
        self.assertEqual(server._event_count, 1)

        # Verify ring buffer has the event
        self.assertEqual(server.ring_buffer.size, 1)

    def test_server_ingest_event_missing_fields(self):
        """ingest_event with missing source/type should raise ValueError."""
        server = StreamingServer(db_path=self.db_path)
        run_async(server.init_db())

        with self.assertRaises(ValueError):
            run_async(server.ingest_event({"source": "engine"}))

        with self.assertRaises(ValueError):
            run_async(server.ingest_event({"type": "task.start"}))

    def test_server_ingest_batch(self):
        """ingest_batch should process multiple events sequentially."""
        server = StreamingServer(db_path=self.db_path)
        run_async(server.init_db())

        # All events use the same source to avoid seq collisions
        # (get_next_sequence is per-source, but live_events.seq is globally UNIQUE)
        results = run_async(server.ingest_batch([
            {"source": "engine", "type": "task.start"},
            {"source": "engine", "type": "inference.start"},
            {"source": "engine", "type": "task.complete"},
        ]))

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["seq"], 1)
        self.assertEqual(results[1]["seq"], 2)
        self.assertEqual(results[2]["seq"], 3)
        self.assertEqual(server._event_count, 3)

    def test_server_snapshot(self):
        """get_snapshot should return correct ring buffer and count data."""
        server = StreamingServer(db_path=self.db_path)
        run_async(server.init_db())

        run_async(server.ingest_event({
            "source": "engine",
            "type": "task.start",
        }))

        snapshot = run_async(server.get_snapshot())
        self.assertEqual(snapshot["event_count"], 1)
        self.assertEqual(snapshot["ring_buffer_size"], 1)
        self.assertEqual(snapshot["ring_buffer_maxlen"], RING_BUFFER_MAXLEN)
        self.assertEqual(snapshot["sse_clients"], 0)
        self.assertIn("uptime_seconds", snapshot)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
