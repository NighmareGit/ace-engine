"""
test_dsh_adapter.py — Comprehensive unit tests for the DSH session adapter.

Tests the dsh_adapter module:
  1. _truncate_value / _truncate_data — value truncation helpers
  2. _DSH_EVENT_MAP — event type mapping completeness
  3. _project_event — raw DSH event → streaming envelope projection
  4. _generate_mock_event — synthetic event generation
  5. _safe_json_get — HTTP GET with failure handling
  6. DSHAdapter.__init__ — constructor defaults and custom values

Run with:
    python3 -m pytest test_dsh_adapter.py -v
    # or
    python3 -m unittest test_dsh_adapter -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure coder-harness is importable
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from dsh_adapter import (
    DSHAdapter,
    DEFAULT_DSH_URL,
    DEFAULT_STREAM_URL,
    DEFAULT_POLL_INTERVAL,
    _MAX_DATA_BYTES,
    _DSH_EVENT_MAP,
    _truncate_value,
    _truncate_data,
    _safe_json_get,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. _truncate_value tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTruncateValue(unittest.TestCase):
    """Tests for the _truncate_value helper function."""

    # ------------------------------------------------------------------
    # test_truncate_value_under_limit
    # ------------------------------------------------------------------
    def test_truncate_value_under_limit(self):
        """String shorter than max_bytes should be returned unchanged."""
        short = "hello world"
        result = _truncate_value(short, max_bytes=1024)
        self.assertEqual(result, short)

    def test_truncate_value_exact_limit(self):
        """String exactly at max_bytes should be returned unchanged."""
        exact = "a" * 100
        result = _truncate_value(exact, max_bytes=100)
        self.assertEqual(result, exact)

    # ------------------------------------------------------------------
    # test_truncate_value_over_limit
    # ------------------------------------------------------------------
    def test_truncate_value_over_limit(self):
        """String longer than max_bytes should be truncated with suffix."""
        long_str = "x" * 3000
        result = _truncate_value(long_str, max_bytes=100)
        self.assertIsInstance(result, str)
        self.assertTrue(result.endswith("…[truncated]"))
        # The truncated body should be <= max_bytes when encoded
        body = result.replace("…[truncated]", "")
        self.assertLessEqual(len(body.encode("utf-8")), 100)

    def test_truncate_value_default_max_bytes(self):
        """Default max_bytes should be _MAX_DATA_BYTES (2048)."""
        long_str = "y" * 3000
        result = _truncate_value(long_str)
        self.assertTrue(result.endswith("…[truncated]"))
        body = result.replace("…[truncated]", "")
        self.assertLessEqual(len(body.encode("utf-8")), _MAX_DATA_BYTES)

    # ------------------------------------------------------------------
    # test_truncate_value_non_string
    # ------------------------------------------------------------------
    def test_truncate_value_non_string(self):
        """Non-string values (int, None, list) should be returned unchanged."""
        self.assertEqual(_truncate_value(42), 42)
        self.assertIsNone(_truncate_value(None))
        self.assertEqual(_truncate_value([1, 2, 3]), [1, 2, 3])
        self.assertEqual(_truncate_value(True), True)

    def test_truncate_value_multibyte_utf8(self):
        """Multi-byte UTF-8 characters should be handled correctly."""
        # Each '€' is 3 bytes in UTF-8; 400 chars = 1200 bytes
        euro_str = "€" * 400
        result = _truncate_value(euro_str, max_bytes=100)
        self.assertTrue(result.endswith("…[truncated]"))
        # The result should be valid UTF-8
        body = result.replace("…[truncated]", "")
        body.encode("utf-8")  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# 2. _truncate_data tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTruncateData(unittest.TestCase):
    """Tests for the _truncate_data helper function."""

    # ------------------------------------------------------------------
    # test_truncate_data_nested
    # ------------------------------------------------------------------
    def test_truncate_data_nested(self):
        """Nested dict/list with oversized strings should be truncated at all levels."""
        data: Dict[str, Any] = {
            "top": "a" * 3000,  # oversized
            "nested": {
                "inner": "b" * 3000,  # oversized
                "safe": "short",  # fine
            },
            "items": [
                "c" * 3000,  # oversized
                "short",  # fine
                42,  # non-string
            ],
        }
        result = _truncate_data(data, max_bytes=100)

        # Top-level string truncated
        self.assertTrue(result["top"].endswith("…[truncated]"))

        # Nested dict
        self.assertTrue(result["nested"]["inner"].endswith("…[truncated]"))
        self.assertEqual(result["nested"]["safe"], "short")

        # List items
        self.assertTrue(result["items"][0].endswith("…[truncated]"))
        self.assertEqual(result["items"][1], "short")
        self.assertEqual(result["items"][2], 42)

    def test_truncate_data_empty(self):
        """Empty dict should return empty dict."""
        self.assertEqual(_truncate_data({}), {})

    def test_truncate_data_no_truncation_needed(self):
        """Data with all values under the limit should be unchanged."""
        data = {"a": "short", "b": 42, "c": [1, 2]}
        result = _truncate_data(data)
        self.assertEqual(result, data)


# ═══════════════════════════════════════════════════════════════════════════
# 3. _DSH_EVENT_MAP tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDSHEventMap(unittest.TestCase):
    """Tests for the _DSH_EVENT_MAP constant."""

    # ------------------------------------------------------------------
    # test_dsh_event_map_complete
    # ------------------------------------------------------------------
    def test_dsh_event_map_complete(self):
        """Every key in _DSH_EVENT_MAP should map to a 'dsh.*' stream type."""
        for raw_type, stream_type in _DSH_EVENT_MAP.items():
            self.assertIsInstance(raw_type, str)
            self.assertIsInstance(stream_type, str)
            self.assertTrue(
                stream_type.startswith("dsh."),
                f"Stream type for {raw_type!r} does not start with 'dsh.': {stream_type!r}",
            )

    def test_dsh_event_map_has_expected_keys(self):
        """_DSH_EVENT_MAP should contain all expected DSH event types."""
        expected_keys = {
            "turn/start", "turn/end",
            "step/start", "step/end",
            "tool/call", "tool/result",
            "assistant/chunk", "assistant/message",
            "llm/retry",
            "goal/change",
            "subagent/descriptor",
            "compaction/start", "compaction/end",
        }
        self.assertTrue(expected_keys.issubset(_DSH_EVENT_MAP.keys()))

    def test_dsh_event_map_no_empty_values(self):
        """No value in _DSH_EVENT_MAP should be empty or None."""
        for raw_type, stream_type in _DSH_EVENT_MAP.items():
            self.assertTrue(stream_type, f"Empty stream type for {raw_type!r}")


# ═══════════════════════════════════════════════════════════════════════════
# 4. _project_event tests
# ═══════════════════════════════════════════════════════════════════════════

class TestProjectEvent(unittest.TestCase):
    """Tests for DSHAdapter._project_event method."""

    def setUp(self):
        """Create a DSHAdapter instance for testing."""
        self.adapter = DSHAdapter()

    # ------------------------------------------------------------------
    # test_project_event_known_type
    # ------------------------------------------------------------------
    def test_project_event_known_type(self):
        """Known DSH type should produce envelope with source='dsh', correct type, and source_id."""
        raw = {"type": "turn/start", "seq": 1, "turnNumber": 5}
        envelope = self.adapter._project_event("session-1", raw)

        self.assertIsNotNone(envelope)
        self.assertEqual(envelope["source"], "dsh")
        self.assertEqual(envelope["type"], "dsh.turn.start")
        self.assertEqual(envelope["source_id"], "session-1")
        self.assertIn("data", envelope)

    def test_project_event_all_known_types(self):
        """Every known DSH event type should produce a valid envelope."""
        for raw_type in _DSH_EVENT_MAP:
            raw = {"type": raw_type, "seq": 1}
            envelope = self.adapter._project_event("sess-1", raw)
            self.assertIsNotNone(envelope, f"Envelope is None for type {raw_type!r}")
            self.assertEqual(envelope["source"], "dsh")
            self.assertEqual(envelope["type"], _DSH_EVENT_MAP[raw_type])
            self.assertEqual(envelope["source_id"], "sess-1")

    # ------------------------------------------------------------------
    # test_project_event_unknown_type
    # ------------------------------------------------------------------
    def test_project_event_unknown_type(self):
        """Unknown DSH type should return None."""
        raw = {"type": "unknown/future", "seq": 1}
        envelope = self.adapter._project_event("session-1", raw)
        self.assertIsNone(envelope)

    def test_project_event_empty_type(self):
        """Empty type should return None."""
        raw = {"type": "", "seq": 1}
        envelope = self.adapter._project_event("session-1", raw)
        self.assertIsNone(envelope)

    def test_project_event_missing_type(self):
        """Missing type key should return None."""
        raw = {"seq": 1}
        envelope = self.adapter._project_event("session-1", raw)
        self.assertIsNone(envelope)

    # ------------------------------------------------------------------
    # test_project_event_excludes_routing_keys
    # ------------------------------------------------------------------
    def test_project_event_excludes_routing_keys(self):
        """Data dict should NOT contain keys type, seq, session_id, sessionId."""
        raw = {
            "type": "turn/start",
            "seq": 1,
            "session_id": "sess-1",
            "sessionId": "sess-1",
            "turnNumber": 5,
            "extra": "value",
        }
        envelope = self.adapter._project_event("session-1", raw)
        data = envelope["data"]

        self.assertNotIn("type", data)
        self.assertNotIn("seq", data)
        self.assertNotIn("session_id", data)
        self.assertNotIn("sessionId", data)
        # Non-routing keys should be preserved
        self.assertEqual(data["turnNumber"], 5)
        self.assertEqual(data["extra"], "value")

    # ------------------------------------------------------------------
    # test_project_event_preserves_dsh_seq
    # ------------------------------------------------------------------
    def test_project_event_preserves_dsh_seq(self):
        """When raw event has 'seq', envelope should include 'dsh_seq'."""
        raw = {"type": "turn/start", "seq": 42}
        envelope = self.adapter._project_event("session-1", raw)
        self.assertEqual(envelope["dsh_seq"], 42)

    def test_project_event_no_seq(self):
        """When raw event has no 'seq', envelope should not have 'dsh_seq'."""
        raw = {"type": "turn/start"}
        envelope = self.adapter._project_event("session-1", raw)
        self.assertNotIn("dsh_seq", envelope)

    # ------------------------------------------------------------------
    # test_project_event_truncates_data
    # ------------------------------------------------------------------
    def test_project_event_truncates_data(self):
        """Data values over 2048 bytes should be truncated."""
        long_value = "x" * 3000
        raw = {"type": "turn/start", "seq": 1, "payload": long_value}
        envelope = self.adapter._project_event("session-1", raw)

        payload = envelope["data"]["payload"]
        self.assertTrue(payload.endswith("…[truncated]"))
        body = payload.replace("…[truncated]", "")
        self.assertLessEqual(len(body.encode("utf-8")), _MAX_DATA_BYTES)

    def test_project_event_preserves_short_data(self):
        """Short data values should not be truncated."""
        raw = {"type": "turn/start", "seq": 1, "msg": "hello"}
        envelope = self.adapter._project_event("session-1", raw)
        self.assertEqual(envelope["data"]["msg"], "hello")


# ═══════════════════════════════════════════════════════════════════════════
# 5. _generate_mock_event tests
# ═══════════════════════════════════════════════════════════════════════════

class TestGenerateMockEvent(unittest.TestCase):
    """Tests for DSHAdapter._generate_mock_event static method."""

    # ------------------------------------------------------------------
    # test_generate_mock_event_has_type_and_seq
    # ------------------------------------------------------------------
    def test_generate_mock_event_has_type_and_seq(self):
        """_generate_mock_event() should return dict with 'type' and 'seq' keys."""
        for _ in range(20):
            event = DSHAdapter._generate_mock_event("sess-1", 1)
            self.assertIn("type", event)
            self.assertIn("seq", event)
            self.assertIsInstance(event["type"], str)
            self.assertEqual(event["seq"], 1)

    # ------------------------------------------------------------------
    # test_generate_mock_event_types_valid
    # ------------------------------------------------------------------
    def test_generate_mock_event_types_valid(self):
        """All generated mock event types should be keys in _DSH_EVENT_MAP."""
        # Generate many events to hit all branches (random with 14 branches)
        seen_types = set()
        for _ in range(500):
            event = DSHAdapter._generate_mock_event("sess-1", 1)
            seen_types.add(event["type"])

        # All seen types should be valid
        for t in seen_types:
            self.assertIn(t, _DSH_EVENT_MAP, f"Generated type {t!r} not in _DSH_EVENT_MAP")

    def test_generate_mock_event_all_types_covered(self):
        """With enough samples, all mock event branches should be covered."""
        seen_types = set()
        # With 500+ iterations, the probability of missing any branch is negligible
        for _ in range(1000):
            event = DSHAdapter._generate_mock_event("sess-1", 1)
            seen_types.add(event["type"])

        self.assertGreaterEqual(
            len(seen_types), 10,
            f"Expected at least 10 different types, got {len(seen_types)}: {seen_types}",
        )

    def test_generate_mock_event_session_id(self):
        """Generated mock event should include session_id."""
        event = DSHAdapter._generate_mock_event("my-session", 5)
        self.assertEqual(event.get("session_id"), "my-session")
        self.assertEqual(event["seq"], 5)


# ═══════════════════════════════════════════════════════════════════════════
# 6. _safe_json_get tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSafeJsonGet(unittest.TestCase):
    """Tests for the _safe_json_get helper function."""

    # ------------------------------------------------------------------
    # test_safe_json_get_unreachable
    # ------------------------------------------------------------------
    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_unreachable(self, mock_urlopen):
        """_safe_json_get should return None when URL is unreachable."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = _safe_json_get("http://localhost:9999/api/test")
        self.assertIsNone(result)

    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_os_error(self, mock_urlopen):
        """_safe_json_get should return None on OSError."""
        mock_urlopen.side_effect = OSError("Network unreachable")

        result = _safe_json_get("http://localhost:9999/api/test")
        self.assertIsNone(result)

    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_invalid_json(self, mock_urlopen):
        """_safe_json_get should return None on invalid JSON."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not valid json {{{"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _safe_json_get("http://localhost:9999/api/test")
        self.assertIsNone(result)

    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_empty_body(self, mock_urlopen):
        """_safe_json_get should return None for empty response body."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _safe_json_get("http://localhost:9999/api/test")
        self.assertIsNone(result)

    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_whitespace_body(self, mock_urlopen):
        """_safe_json_get should return None for whitespace-only body."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"   \n  "
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _safe_json_get("http://localhost:9999/api/test")
        self.assertIsNone(result)

    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_success(self, mock_urlopen):
        """_safe_json_get should return parsed JSON on success."""
        payload = {"sessions": [{"id": "sess-1"}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _safe_json_get("http://localhost:3080/api/sessions")
        self.assertEqual(result, payload)

    @patch("dsh_adapter.urllib.request.urlopen")
    def test_safe_json_get_array_response(self, mock_urlopen):
        """_safe_json_get should handle JSON array responses."""
        payload = [{"id": "a"}, {"id": "b"}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _safe_json_get("http://localhost:3080/api/test")
        self.assertEqual(result, payload)


# ═══════════════════════════════════════════════════════════════════════════
# 7. DSHAdapter.__init__ tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDSHAdapterInit(unittest.TestCase):
    """Tests for DSHAdapter constructor."""

    # ------------------------------------------------------------------
    # test_adapter_init_defaults
    # ------------------------------------------------------------------
    def test_adapter_init_defaults(self):
        """DSHAdapter() should initialize with correct defaults."""
        adapter = DSHAdapter()
        self.assertEqual(adapter._dsh_url, DEFAULT_DSH_URL)
        self.assertEqual(adapter._stream_url, DEFAULT_STREAM_URL)
        self.assertEqual(adapter._poll_interval, DEFAULT_POLL_INTERVAL)
        self.assertFalse(adapter._mock)
        self.assertIsNone(adapter._client)
        self.assertFalse(adapter._running)
        self.assertEqual(adapter._session_seqs, {})
        self.assertEqual(adapter._known_sessions, set())

    # ------------------------------------------------------------------
    # test_adapter_init_custom
    # ------------------------------------------------------------------
    def test_adapter_init_custom(self):
        """DSHAdapter(dsh_url=..., stream_url=..., poll_interval=..., mock=...) should store values."""
        adapter = DSHAdapter(
            dsh_url="http://192.0.2.1:5000",
            stream_url="http://192.0.2.2:5001",
            poll_interval=5.5,
            mock=True,
        )
        self.assertEqual(adapter._dsh_url, "http://192.0.2.1:5000")
        self.assertEqual(adapter._stream_url, "http://192.0.2.2:5001")
        self.assertEqual(adapter._poll_interval, 5.5)
        self.assertTrue(adapter._mock)

    def test_adapter_init_strips_trailing_slash(self):
        """URLs should have trailing slashes stripped."""
        adapter = DSHAdapter(
            dsh_url="http://localhost:3080/",
            stream_url="http://localhost:3081/",
        )
        self.assertEqual(adapter._dsh_url, "http://localhost:3080")
        self.assertEqual(adapter._stream_url, "http://localhost:3081")

    def test_adapter_init_poll_interval_minimum(self):
        """poll_interval should be clamped to minimum 0.5."""
        adapter = DSHAdapter(poll_interval=0.1)
        self.assertEqual(adapter._poll_interval, 0.5)

        adapter2 = DSHAdapter(poll_interval=0.0)
        self.assertEqual(adapter2._poll_interval, 0.5)

        adapter3 = DSHAdapter(poll_interval=-1.0)
        self.assertEqual(adapter3._poll_interval, 0.5)

    def test_adapter_init_poll_interval_normal(self):
        """Normal poll_interval values should be preserved."""
        adapter = DSHAdapter(poll_interval=10.0)
        self.assertEqual(adapter._poll_interval, 10.0)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
