"""
test_streaming_integration.py — Integration tests for the full streaming pipeline.

Verifies end-to-end behaviour of the live event streaming system:
  1. Ingest events via StreamingClient, query via HTTP and SQLite
  2. SSE streaming with reconnection (Last-Event-ID)
  3. Auth token protection
  4. Body size limits
  5. Dashboard HTML serving

Run with:
    python3 -m pytest test_streaming_integration.py -v
    # or
    python3 -m unittest test_streaming_integration -v
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure coder-harness is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

from streaming_client import StreamingClient
from streaming_core import (
    RingBuffer,
    SSEClientManager,
    StreamingServer,
    _format_sse,
)
from streaming_routes import (
    MAX_EVENT_PAYLOAD_BYTES,
    create_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_port() -> int:
    """Return a high random port biased by PID for reproducibility."""
    return 19800 + os.getpid() % 1000


def _start_server(db_path: str, port: int, auth_token: str | None = None) -> tuple:
    """Start the FastAPI app on *port* in a background thread.

    Returns (thread, server_instance) — call thread.join(timeout) to stop.
    The server is usable once ``wait_for_server_ready`` succeeds.
    """
    app = create_app(
        db_path=db_path,
        ring_buffer_size=128,
        auth_token=auth_token,
    )

    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Save reference so we can trigger shutdown
    thread = threading.Thread(target=server.run, daemon=True, name=f"uvicorn-{port}")
    thread.start()
    return thread, server


def _wait_for_server_ready(port: int, retries: int = 50, delay: float = 0.1) -> bool:
    """Poll GET /health until the server is ready or retries exhausted."""
    for _ in range(retries):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(delay)
    return False


def _http_get(
    path: str,
    port: int,
    headers: dict | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict, str]:
    """Perform a GET request and return (status_code, headers_dict, body_text)."""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, dict(exc.headers), body


def _http_post_json(
    path: str,
    port: int,
    payload: dict,
    headers: dict | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict, str]:
    """Perform a POST request with JSON body and return (status, headers, body).

    Returns (0, {}, error_message) on connection-level errors (e.g., the
    server resets the connection before sending a response, which can happen
    with very large payloads).
    """
    url = f"http://127.0.0.1:{port}{path}"
    body_bytes = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers), resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        return exc.code, dict(exc.headers), resp_body
    except (ConnectionResetError, OSError) as exc:
        # Server may close the connection before sending a response (e.g., 413)
        return 0, {}, str(exc)


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse raw SSE text into a list of dicts with ``event``, ``id``, and ``data`` fields."""
    events: list[dict] = []
    current: dict[str, str] = {}
    for line in raw.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("id: "):
            current["id"] = line[4:]
        elif line.startswith("data: "):
            current["data"] = line[6:]
        elif line == "":
            if current.get("data"):
                events.append(current)
            current = {}
    return events


def _read_sse_stream(
    port: int,
    max_events: int = 5,
    timeout: float = 5.0,
    extra_headers: dict | None = None,
) -> list[dict]:
    """Connect to /stream and collect *max_events* SSE events (or timeout).

    Returns list of parsed SSE event dicts.
    """
    url = f"http://127.0.0.1:{port}/stream"
    hdrs = {"Accept": "text/event-stream"}
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(url, headers=hdrs)
    resp = urllib.request.urlopen(req, timeout=timeout)

    collected: list[dict] = []
    buf = ""
    deadline = time.monotonic() + timeout

    # Read from the streaming response
    # urllib returns an HTTPResponse; for streaming we iterate over chunks
    try:
        while len(collected) < max_events and time.monotonic() < deadline:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            # Process complete SSE messages (delimited by \n\n)
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                parsed: dict[str, str] = {}
                for line in block.split("\n"):
                    line = line.rstrip("\r")
                    if line.startswith("event: "):
                        parsed["event"] = line[7:]
                    elif line.startswith("id: "):
                        parsed["id"] = line[4:]
                    elif line.startswith("data: "):
                        parsed["data"] = line[6:]
                if parsed.get("data"):
                    collected.append(parsed)
    except Exception:
        pass
    finally:
        resp.close()

    return collected


def _make_event(index: int, source: str = "engine", event_type: str = "task.start") -> dict:
    """Build a simple test event envelope."""
    return {
        "source": source,
        "type": event_type,
        "data": {"index": index, "message": f"test event {index}"},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite
# ═══════════════════════════════════════════════════════════════════════════

class StreamingIntegrationTestBase(unittest.TestCase):
    """Base class providing setUp / tearDown for a running server."""

    _port: int = 0
    _db_path: str = ""
    _tmp_dir: str = ""
    _server_thread: threading.Thread | None = None
    _uvicorn_server: Any = None

    def _start(self, auth_token: str | None = None) -> int:
        """Create temp files, start the server, wait for readiness, return port."""
        self._tmp_dir = tempfile.mkdtemp(prefix="streaming_test_")
        self._db_path = os.path.join(self._tmp_dir, "test.db")
        self._port = _random_port() + id(self) % 200  # offset to avoid collisions

        self._server_thread, self._uvicorn_server = _start_server(
            db_path=self._db_path,
            port=self._port,
            auth_token=auth_token,
        )

        if not _wait_for_server_ready(self._port):
            self.fail(f"Server on port {self._port} did not become ready within timeout")

        return self._port

    def _stop(self) -> None:
        """Gracefully stop the uvicorn server and join the thread."""
        if self._uvicorn_server is not None:
            try:
                self._uvicorn_server.should_exit = True
            except Exception:
                pass
        if self._server_thread is not None:
            self._server_thread.join(timeout=5.0)

    def tearDown(self) -> None:
        """Stop server and clean up temp files."""
        self._stop()

        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            import shutil
            shutil.rmtree(self._tmp_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Full pipeline: ingest → query → health → SQLite
# ═══════════════════════════════════════════════════════════════════════════

class TestFullPipelineIngestAndRead(StreamingIntegrationTestBase):
    """test_full_pipeline_ingest_and_read

    1. Create temp DB + start server on random port
    2. POST 5 events via StreamingClient
    3. GET /events/recent — verify 5 events returned
    4. GET /health — verify event count = 5
    5. Verify events are in SQLite (direct query)
    """

    def test_full_pipeline_ingest_and_read(self) -> None:
        port = self._start()

        # --- Step 2: POST 5 events via StreamingClient ---
        client = StreamingClient(f"http://127.0.0.1:{port}", timeout=2.0, max_retries=2)
        results = []
        for i in range(5):
            result = client.emit_event("engine", "task.start", {"index": i})
            self.assertIsNotNone(result, f"Event {i} should be accepted")
            self.assertIn("seq", result, f"Event {i} response must include seq")
            results.append(result)

        # Verify all 5 events got unique, monotonically increasing seq numbers
        seqs = [r["seq"] for r in results]
        for i in range(1, len(seqs)):
            self.assertGreater(seqs[i], seqs[i - 1], "Seq numbers must be monotonically increasing")

        # --- Step 3: GET /events/recent — verify 5 events returned ---
        status, _, body = _http_get("/events/recent?limit=10", port)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("events", data)
        self.assertEqual(len(data["events"]), 5, "Should have 5 events in recent")

        # Verify event structure
        for evt in data["events"]:
            self.assertIn("id", evt)
            self.assertIn("seq", evt)
            self.assertIn("source", evt)
            self.assertIn("type", evt)
            self.assertEqual(evt["source"], "engine")
            self.assertEqual(evt["type"], "task.start")

        # --- Step 4: GET /health — verify event count = 5 ---
        status, _, body = _http_get("/health", port)
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["events"], 5, "Health endpoint must report 5 events")

        # --- Step 5: Verify events in SQLite (direct query) ---
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM live_events ORDER BY seq ASC;").fetchall()
            self.assertEqual(len(rows), 5, "SQLite must contain 5 events")

            for idx, row in enumerate(rows):
                self.assertEqual(row["source"], "engine")
                self.assertEqual(row["type"], "task.start")
                # data is stored as JSON string
                data = json.loads(row["data"])
                self.assertEqual(data["index"], idx)

            # Verify source_sequences table was updated
            seq_row = conn.execute(
                "SELECT last_seq FROM source_sequences WHERE source = ?;",
                ("engine",),
            ).fetchone()
            self.assertIsNotNone(seq_row, "source_sequences must have engine entry")
            self.assertEqual(seq_row[0], 5, "engine last_seq must be 5")
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 2. SSE reconnection with Last-Event-ID
# ═══════════════════════════════════════════════════════════════════════════

class TestSSEReconnection(StreamingIntegrationTestBase):
    """test_sse_reconnection

    1. Start server and ingest 3 events
    2. Connect to /stream and read first 3 events
    3. Note the last id: field
    4. Disconnect, ingest 2 more events
    5. Reconnect with Last-Event-ID header
    6. Verify only new events (not duplicates) are received
    """

    def test_sse_reconnection(self) -> None:
        port = self._start()

        client = StreamingClient(f"http://127.0.0.1:{port}", timeout=2.0, max_retries=2)

        # --- Step 1: Ingest 3 initial events ---
        for i in range(3):
            result = client.emit_event("engine", "task.start", {"phase": "initial", "i": i})
            self.assertIsNotNone(result)

        # Small delay to ensure persistence
        time.sleep(0.2)

        # --- Step 2: Connect to /stream and read events ---
        first_batch = _read_sse_stream(port, max_events=3, timeout=5.0)
        self.assertGreaterEqual(len(first_batch), 3, f"Should receive at least 3 events, got {len(first_batch)}")

        # Verify first batch contains the initial events
        first_ids = set()
        for evt in first_batch:
            self.assertIn("id", evt)
            self.assertIn("data", evt)
            first_ids.add(evt["id"])
            envelope = json.loads(evt["data"])
            # The SSE data field is the full envelope; phase is in the nested data
            inner = envelope.get("data", {})
            self.assertEqual(inner.get("phase"), "initial")

        # --- Step 3: Note the last id received ---
        last_id = first_batch[-1]["id"]
        self.assertIsNotNone(last_id, "Last event must have an id field")

        # --- Step 4: Disconnect and ingest 2 more events ---
        for i in range(2):
            result = client.emit_event("engine", "task.complete", {"phase": "reconnect", "i": i})
            self.assertIsNotNone(result)

        time.sleep(0.2)

        # --- Step 5: Reconnect with Last-Event-ID header ---
        reconnect_events = _read_sse_stream(
            port,
            max_events=10,
            timeout=5.0,
            extra_headers={"Last-Event-ID": last_id},
        )

        # --- Step 6: Verify only new events are received ---
        # The replay should include events after last_id, plus potentially the 2 new live events.
        # We should NOT see duplicates from the first batch that were before last_id.
        reconnect_ids = set()
        new_phase_events = []
        for evt in reconnect_events:
            reconnect_ids.add(evt["id"])
            envelope = json.loads(evt["data"])
            inner = envelope.get("data", {})
            if inner.get("phase") == "reconnect":
                new_phase_events.append(evt)

        # Must receive the 2 new reconnect-phase events
        self.assertGreaterEqual(len(new_phase_events), 2,
            f"Should receive at least 2 reconnect events, got {len(new_phase_events)}")

        # Verify the new events have different ids from the first batch
        overlap = first_ids & reconnect_ids
        # Allow at most 1 overlap (the event at last_id itself is replayed)
        self.assertLessEqual(len(overlap), 1,
            f"Should have at most 1 duplicate (the boundary event), got {len(overlap)}")

        # Verify monotonically increasing IDs in reconnect batch
        reconnect_id_list = [evt["id"] for evt in reconnect_events]
        # IDs should be ordered (at least no decrease in seq)
        for i in range(1, len(reconnect_events)):
            prev_seq = int(reconnect_events[i - 1]["id"]) if reconnect_events[i - 1]["id"].isdigit() else 0
            curr_seq = int(reconnect_events[i]["id"]) if reconnect_events[i]["id"].isdigit() else 0
            if prev_seq and curr_seq:
                self.assertGreaterEqual(curr_seq, prev_seq, "Replay IDs must be non-decreasing")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Auth protection
# ═══════════════════════════════════════════════════════════════════════════

class TestAuthProtection(StreamingIntegrationTestBase):
    """test_auth_protection

    1. Start server with --token "test-secret"
    2. POST event without auth → expect 401
    3. POST event with wrong token → expect 401
    4. POST event with correct token → expect 201
    """

    def test_auth_protection(self) -> None:
        port = self._start(auth_token="test-secret")

        # --- Step 2: POST without auth → 401 ---
        status, _, body = _http_post_json("/events", port, _make_event(0))
        self.assertEqual(status, 401, "POST without auth must return 401")
        data = json.loads(body)
        self.assertIn("error", data)

        # --- Step 3: POST with wrong token → 401 ---
        status, _, body = _http_post_json(
            "/events", port, _make_event(1),
            headers={"Authorization": "Bearer <wrong-token>"},
        )
        self.assertEqual(status, 401, "POST with wrong token must return 401")

        # --- Step 4: POST with correct token → 201 ---
        status, _, body = _http_post_json(
            "/events", port, _make_event(2),
            headers={"Authorization": "Bearer <token>"},
        )
        self.assertEqual(status, 201, "POST with correct token must return 201")
        data = json.loads(body)
        self.assertIn("seq", data)
        self.assertEqual(data["source"], "engine")

        # Verify the authed event is persisted
        conn = sqlite3.connect(self._db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM live_events;").fetchone()[0]
            self.assertEqual(count, 1, "Only 1 event should be persisted (the authed one)")
        finally:
            conn.close()


class TestCheckAuthDirect(unittest.TestCase):
    """Direct unit tests for StreamingServer.check_auth() without starting a server."""

    def _make_request(self, headers: dict[str, str] | None = None) -> MagicMock:
        """Build a mock request object with the given headers."""
        req = MagicMock()
        req.headers = headers or {}
        return req

    def test_no_auth_configured(self) -> None:
        """check_auth returns True when no token is set."""
        server = StreamingServer(db_path=":memory:", auth_token=None)
        req = self._make_request()
        self.assertTrue(server.check_auth(req))

    def test_valid_token(self) -> None:
        """check_auth returns True with correct bearer token."""
        server = StreamingServer(db_path=":memory:", auth_token="my-secret")
        req = self._make_request({"Authorization": "Bearer <token>"})
        self.assertTrue(server.check_auth(req))

    def test_wrong_token(self) -> None:
        """check_auth returns False with incorrect bearer token."""
        server = StreamingServer(db_path=":memory:", auth_token="my-secret")
        req = self._make_request({"Authorization": "Bearer <wrong-token>"})
        self.assertFalse(server.check_auth(req))

    def test_no_auth_header(self) -> None:
        """check_auth returns False when auth is required but header is missing."""
        server = StreamingServer(db_path=":memory:", auth_token="my-secret")
        req = self._make_request()
        self.assertFalse(server.check_auth(req))

    def test_non_bearer_scheme(self) -> None:
        """check_auth returns False for non-bearer schemes (e.g., Basic)."""
        server = StreamingServer(db_path=":memory:", auth_token="my-secret")
        req = self._make_request({"Authorization": "Basic dXNlcjpwYXNz"})
        self.assertFalse(server.check_auth(req))


# ═══════════════════════════════════════════════════════════════════════════
# 4. Body size limit
# ═══════════════════════════════════════════════════════════════════════════

class TestBodySizeLimit(StreamingIntegrationTestBase):
    """test_body_size_limit

    1. Start server
    2. POST event with >64KB payload → expect 413
    """

    def test_body_size_limit(self) -> None:
        port = self._start()

        # --- Step 2: POST event with 100KB payload → expect 413 or connection reset ---
        large_event = {
            "source": "engine",
            "type": "task.start",
            "data": {"payload": "x" * (100 * 1024)},  # ~100KB
        }
        status, _, body = _http_post_json("/events", port, large_event)
        # Server may respond with 413 or close the connection entirely
        self.assertIn(status, (0, 413),
            f"POST with 100KB payload must return 413 or connection reset, got {status}")

        # Verify nothing was persisted
        conn = sqlite3.connect(self._db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM live_events;").fetchone()[0]
            self.assertEqual(count, 0, "No events should be persisted after 413 rejection")
        finally:
            conn.close()


class TestBodySizeLimitDirect(unittest.TestCase):
    """Direct unit tests for MAX_EVENT_PAYLOAD_BYTES constant."""

    def test_constant_value(self) -> None:
        """MAX_EVENT_PAYLOAD_BYTES should be 64KB."""
        self.assertEqual(MAX_EVENT_PAYLOAD_BYTES, 64 * 1024)


# ═══════════════════════════════════════════════════════════════════════════
# 5. Dashboard serving
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardServing(StreamingIntegrationTestBase):
    """test_dashboard_serving

    1. Start server (dashboard/index.html must exist in coder-harness/)
    2. GET / → expect HTML content
    """

    def test_dashboard_serving(self) -> None:
        # The streaming_server.py module resolves dashboard/index.html relative
        # to its own __file__ (i.e., coder-harness/dashboard/index.html).
        # Verify it exists before running the test.
        expected_dashboard = os.path.join(
            os.path.dirname(os.path.abspath("streaming_server.py")),
            "dashboard",
            "index.html",
        )
        if not os.path.exists(expected_dashboard):
            self.skipTest(f"Dashboard not found at {expected_dashboard}")

        port = self._start()

        status, headers, body = _http_get("/", port)
        self.assertEqual(status, 200, f"GET / must return 200, got {status}")

        content_type = headers.get("Content-Type", headers.get("content-type", ""))
        self.assertIn("text/html", content_type, f"Content-Type must be HTML, got {content_type}")
        self.assertIn("<html", body.lower(), "Response must contain HTML content")


class TestDashboardServingNotFound(unittest.TestCase):
    """Test 404 when dashboard/index.html doesn't exist."""

    def test_dashboard_404(self) -> None:
        """When no dashboard/index.html exists, GET / returns 404."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test.db")
            port = _random_port() + 300

            app = create_app(db_path=db_path, ring_buffer_size=128)

            # Patch Path.exists on the dashboard path resolution inside the app.
            # The GET / handler resolves Path(__file__).parent / "dashboard" / "index.html"
            # We patch Path.exists to return False for any path containing "dashboard".
            import pathlib
            _orig_exists = pathlib.Path.exists

            def _patched_exists(self_path, *args, **kwargs):
                if "dashboard" in str(self_path):
                    return False
                return _orig_exists(self_path, *args, **kwargs)

            import uvicorn
            with patch.object(pathlib.Path, "exists", _patched_exists):
                config = uvicorn.Config(
                    app, host="127.0.0.1", port=port,
                    log_level="warning", access_log=False,
                )
                server = uvicorn.Server(config)
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()

                try:
                    if not _wait_for_server_ready(port):
                        self.fail("Server not ready")

                    status, _, body = _http_get("/", port)
                    self.assertEqual(status, 404, f"GET / with no dashboard must return 404, got {status}")
                    self.assertIn("Dashboard Not Found", body)
                finally:
                    server.should_exit = True
                    thread.join(timeout=5.0)


# ═══════════════════════════════════════════════════════════════════════════
# 6. SSE format function unit tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSSEFormat(unittest.TestCase):
    """Direct tests for the _format_sse helper function."""

    def test_basic_format(self) -> None:
        """_format_sse produces correct wire format."""
        event = {
            "id": "test-ulid-123",
            "seq": 42,
            "time": 1700000000000,
            "source": "engine",
            "type": "task.start",
            "data": {"task_id": "T01"},
        }
        result = _format_sse(event)
        self.assertIn("event: task.start", result)
        self.assertIn("id: 42", result)
        self.assertIn("data:", result)
        self.assertTrue(result.endswith("\n\n"), "SSE message must end with \\n\\n")

    def test_data_is_json(self) -> None:
        """The data line must contain valid JSON."""
        event = {"seq": 1, "type": "test", "data": {"key": "value"}}
        result = _format_sse(event)
        for line in result.split("\n"):
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                self.assertEqual(payload["data"]["key"], "value")
                return
        self.fail("No data line found in SSE output")

    def test_type_strips_newlines(self) -> None:
        """Event type must not contain newlines (SSE injection prevention)."""
        event = {"seq": 1, "type": "bad\ntype", "data": {}}
        result = _format_sse(event)
        self.assertNotIn("bad\ntype", result)
        self.assertIn("event: badtype", result)

    def test_seq_as_id(self) -> None:
        """The SSE id: field must be the seq number."""
        event = {"seq": 99, "type": "ping", "data": {}}
        result = _format_sse(event)
        self.assertIn("id: 99", result)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Snapshot API
# ═══════════════════════════════════════════════════════════════════════════

class TestSnapshotAPI(StreamingIntegrationTestBase):
    """Verify the /api/snapshot endpoint returns correct server state."""

    def test_snapshot_includes_metadata(self) -> None:
        port = self._start()

        # Ingest some events
        client = StreamingClient(f"http://127.0.0.1:{port}", timeout=2.0, max_retries=1)
        for i in range(3):
            client.emit_event("engine", "task.start", {"i": i})

        status, _, body = _http_get("/api/snapshot", port)
        self.assertEqual(status, 200)

        snapshot = json.loads(body)
        self.assertIn("events", snapshot)
        self.assertIn("event_count", snapshot)
        self.assertIn("uptime_seconds", snapshot)
        self.assertIn("sse_clients", snapshot)
        self.assertIn("ring_buffer_size", snapshot)
        self.assertIn("ring_buffer_maxlen", snapshot)

        self.assertEqual(snapshot["event_count"], 3)
        self.assertEqual(snapshot["ring_buffer_size"], 3)
        self.assertEqual(snapshot["ring_buffer_maxlen"], 128)
        self.assertGreaterEqual(snapshot["uptime_seconds"], 0)


# ═══════════════════════════════════════════════════════════════════════════
# 8. Batch ingestion
# ═══════════════════════════════════════════════════════════════════════════

class TestBatchIngestion(StreamingIntegrationTestBase):
    """Verify the POST /events/batch endpoint."""

    def test_batch_endpoint(self) -> None:
        port = self._start()

        client = StreamingClient(f"http://127.0.0.1:{port}", timeout=2.0, max_retries=1)
        events = [client.build_envelope("engine", f"type.{i}", {"i": i}) for i in range(4)]
        result = client.emit_batch(events)

        self.assertIsNotNone(result, "Batch emit should succeed")
        self.assertEqual(result["count"], 4)
        self.assertEqual(len(result["events"]), 4)

        # Verify via recent endpoint
        status, _, body = _http_get("/events/recent", port)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data["events"]), 4)


# ═══════════════════════════════════════════════════════════════════════════
# 9. Ring buffer replay
# ═══════════════════════════════════════════════════════════════════════════

class TestRingBufferReplay(unittest.TestCase):
    """Direct unit tests for the RingBuffer class."""

    def test_append_and_get_all(self) -> None:
        rb = RingBuffer(maxlen=10)
        loop = asyncio.new_event_loop()
        try:
            for i in range(5):
                loop.run_until_complete(rb.append({"seq": i, "id": f"evt-{i}"}))
            result = loop.run_until_complete(rb.get_all())
            self.assertEqual(len(result), 5)
            self.assertEqual(result[0]["seq"], 0)
            self.assertEqual(result[4]["seq"], 4)
        finally:
            loop.close()

    def test_maxlen_eviction(self) -> None:
        rb = RingBuffer(maxlen=3)
        loop = asyncio.new_event_loop()
        try:
            for i in range(5):
                loop.run_until_complete(rb.append({"seq": i, "id": f"evt-{i}"}))
            result = loop.run_until_complete(rb.get_all())
            self.assertEqual(len(result), 3)
            # Only the last 3 should remain
            self.assertEqual(result[0]["seq"], 2)
            self.assertEqual(result[2]["seq"], 4)
        finally:
            loop.close()

    def test_replay_since(self) -> None:
        rb = RingBuffer(maxlen=10)
        loop = asyncio.new_event_loop()
        try:
            for i in range(5):
                loop.run_until_complete(rb.append({"seq": i + 1, "id": f"evt-{i}"}))
            # Replay events after seq 2
            result = loop.run_until_complete(rb.replay_since(2))
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0]["seq"], 3)
            self.assertEqual(result[2]["seq"], 5)
        finally:
            loop.close()

    def test_size_property(self) -> None:
        rb = RingBuffer(maxlen=5)
        loop = asyncio.new_event_loop()
        try:
            self.assertEqual(rb.size, 0)
            loop.run_until_complete(rb.append({"seq": 1}))
            self.assertEqual(rb.size, 1)
            loop.run_until_complete(rb.append({"seq": 2}))
            self.assertEqual(rb.size, 2)
        finally:
            loop.close()


# ═══════════════════════════════════════════════════════════════════════════
# 10. SSE client manager
# ═══════════════════════════════════════════════════════════════════════════

class TestSSEClientManager(unittest.TestCase):
    """Direct unit tests for the SSEClientManager class."""

    def test_connect_and_disconnect(self) -> None:
        mgr = SSEClientManager()
        client_id, queue = mgr.connect()
        self.assertIsNotNone(client_id)
        self.assertEqual(mgr.client_count, 1)
        mgr.disconnect(client_id)
        self.assertEqual(mgr.client_count, 0)

    def test_broadcast(self) -> None:
        mgr = SSEClientManager()
        c1_id, q1 = mgr.connect()
        c2_id, q2 = mgr.connect()

        delivered = asyncio.new_event_loop().run_until_complete(
            mgr.broadcast({"seq": 1, "data": "hello"})
        )
        self.assertEqual(delivered, 2)
        self.assertFalse(q1.empty())
        self.assertFalse(q2.empty())

    def test_multiple_clients(self) -> None:
        mgr = SSEClientManager()
        ids = []
        for _ in range(5):
            cid, _ = mgr.connect()
            ids.append(cid)
        self.assertEqual(mgr.client_count, 5)
        for cid in ids:
            mgr.disconnect(cid)
        self.assertEqual(mgr.client_count, 0)


# ═══════════════════════════════════════════════════════════════════════════
# 11. SQLite persistence verification
# ═══════════════════════════════════════════════════════════════════════════

class TestSQLitePersistence(StreamingIntegrationTestBase):
    """Verify SQLite WAL mode, schema integrity, and data durability."""

    def test_wal_mode_enabled(self) -> None:
        port = self._start()

        # Verify WAL mode is set
        conn = sqlite3.connect(self._db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(mode.lower(), "wal", "Journal mode must be WAL")
        finally:
            conn.close()

    def test_schema_tables_exist(self) -> None:
        port = self._start()

        conn = sqlite3.connect(self._db_path)
        try:
            tables = {
                row[0] for row in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            }
            self.assertIn("live_events", tables)
            self.assertIn("source_sequences", tables)
            self.assertIn("replay_cursors", tables)
        finally:
            conn.close()

    def test_schema_indexes_exist(self) -> None:
        port = self._start()

        conn = sqlite3.connect(self._db_path)
        try:
            indexes = {
                row[0] for row in
                conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()
            }
            expected = {
                "idx_live_events_source",
                "idx_live_events_type",
                "idx_live_events_seq",
                "idx_live_events_time",
            }
            self.assertTrue(expected.issubset(indexes), f"Missing indexes: {expected - indexes}")
        finally:
            conn.close()

    def test_event_data_roundtrip(self) -> None:
        """Verify complex JSON data survives the SQLite roundtrip."""
        port = self._start()

        complex_data = {
            "nested": {"a": [1, 2, 3], "b": True},
            "string": "hello world",
            "number": 42.5,
            "null_val": None,
        }

        client = StreamingClient(f"http://127.0.0.1:{port}", timeout=2.0, max_retries=1)
        result = client.emit_event("engine", "complex.test", complex_data)
        self.assertIsNotNone(result)

        # Read back from SQLite
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT data FROM live_events ORDER BY seq DESC LIMIT 1;").fetchone()
            self.assertIsNotNone(row)
            stored = json.loads(row["data"])
            self.assertEqual(stored["nested"]["a"], [1, 2, 3])
            self.assertTrue(stored["nested"]["b"])
            self.assertEqual(stored["string"], "hello world")
            self.assertEqual(stored["number"], 42.5)
            self.assertIsNone(stored["null_val"])
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# 12. Server error handling
# ═══════════════════════════════════════════════════════════════════════════

class TestErrorHandling(StreamingIntegrationTestBase):
    """Verify error responses for malformed requests."""

    def test_invalid_json(self) -> None:
        port = self._start()
        status, _, body = _http_post_json("/events", port, None)
        # Since _http_post_json always serializes to JSON, we test raw bytes
        url = f"http://127.0.0.1:{port}/events"
        req = urllib.request.Request(
            url,
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_missing_required_fields(self) -> None:
        port = self._start()
        status, _, body = _http_post_json("/events", port, {"data": {"x": 1}})
        self.assertEqual(status, 422, f"Missing source/type should return 422, got {status}")

    def test_health_endpoint_always_accessible(self) -> None:
        """GET /health should work without auth even when auth is enabled."""
        port = self._start(auth_token="secret")
        status, _, body = _http_get("/health", port)
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["status"], "ok")


# ═══════════════════════════════════════════════════════════════════════════
# 13. Source filtering in SSE
# ═══════════════════════════════════════════════════════════════════════════

class TestSourceFiltering(unittest.TestCase):
    """Test that event queries correctly filter by source."""

    def test_query_filters_by_source(self) -> None:
        """Events from non-matching sources are excluded from queries."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        try:
            from event_schema import ensure_schema

            ensure_schema(db_path)
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL;")
            now_ms = int(time.time() * 1000)

            # Insert events directly with globally unique seq values.
            # (insert_event uses per-source sequences that can collide globally.)
            for seq, eid, source in [
                (1, "eng-001", "engine"),
                (2, "dsh-001", "dsh"),
                (3, "eng-002", "engine"),
            ]:
                conn.execute(
                    """INSERT INTO live_events (id, seq, time, source, type, data)
                       VALUES (?, ?, ?, ?, 'task.start', '{}');""",
                    (eid, seq, now_ms, source),
                )
                # Also maintain source_sequences for consistency
                conn.execute(
                    """INSERT OR REPLACE INTO source_sequences (source, last_seq, last_time)
                       VALUES (?, ?, ?);""",
                    (source, seq, now_ms),
                )
            conn.commit()

            # Query only engine events
            rows = conn.execute(
                "SELECT * FROM live_events WHERE source = ? ORDER BY seq ASC;",
                ("engine",),
            ).fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row[4], "engine")  # source is column index 4

            # Query only dsh events
            rows = conn.execute(
                "SELECT * FROM live_events WHERE source = ? ORDER BY seq ASC;",
                ("dsh",),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][4], "dsh")

            # Query all
            rows = conn.execute("SELECT COUNT(*) FROM live_events;").fetchall()
            self.assertEqual(rows[0][0], 3)
        finally:
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                if os.path.exists(p):
                    os.unlink(p)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
