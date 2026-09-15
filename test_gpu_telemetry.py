"""
test_gpu_telemetry.py — Comprehensive unit tests for gpu_telemetry.py.

Tests the GPU telemetry poller: CSV parsing, poller lifecycle, and event
emission via mocked StreamingClient.

Run with:
    python3 -m pytest test_gpu_telemetry.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure coder-harness is importable
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from gpu_telemetry import (
    GPUTelemetryPoller,
    _NVIDIA_SMI_FIELDS,
    _GPU_INDEX_MAP,
)


# ═══════════════════════════════════════════════════════════════════════════
# Parsing tests
# ═══════════════════════════════════════════════════════════════════════════

class TestParseNvidiaSmi(unittest.TestCase):
    """Tests for GPUTelemetryPoller._parse_nvidia_smi()."""

    def _make_poller(self, gpu_map=None):
        """Create a poller with mocked StreamingClient for parser tests."""
        with patch("gpu_telemetry.StreamingClient"):
            return GPUTelemetryPoller(gpu_map=gpu_map)

    # ------------------------------------------------------------------
    # test_parse_nvidia_smi_two_gpus
    # ------------------------------------------------------------------
    def test_parse_nvidia_smi_two_gpus(self):
        """Valid 2-GPU CSV output should return dict with both GPUs."""
        poller = self._make_poller()
        csv = (
            "0, NVIDIA GeForce RTX 3090, 62, 85, 18400, 24576\n"
            "1, NVIDIA GeForce RTX 3070, 55, 40, 6900, 8192"
        )
        result = poller._parse_nvidia_smi(csv)

        self.assertIn("gpu_3090", result)
        self.assertIn("gpu_3070", result)

        gpu3090 = result["gpu_3090"]
        self.assertEqual(gpu3090["temp"], 62)
        self.assertEqual(gpu3090["utilization_pct"], 85)
        self.assertEqual(gpu3090["vram_used_mb"], 18400)
        self.assertEqual(gpu3090["vram_total_mb"], 24576)

        gpu3070 = result["gpu_3070"]
        self.assertEqual(gpu3070["temp"], 55)
        self.assertEqual(gpu3070["utilization_pct"], 40)
        self.assertEqual(gpu3070["vram_used_mb"], 6900)
        self.assertEqual(gpu3070["vram_total_mb"], 8192)

    # ------------------------------------------------------------------
    # test_parse_nvidia_smi_single_gpu
    # ------------------------------------------------------------------
    def test_parse_nvidia_smi_single_gpu(self):
        """Single GPU line should return dict with one entry."""
        poller = self._make_poller()
        csv = "0, NVIDIA GeForce RTX 3090, 62, 85, 18400, 24576"
        result = poller._parse_nvidia_smi(csv)

        self.assertEqual(len(result), 1)
        self.assertIn("gpu_3090", result)
        self.assertEqual(result["gpu_3090"]["temp"], 62)

    # ------------------------------------------------------------------
    # test_parse_nvidia_smi_empty
    # ------------------------------------------------------------------
    def test_parse_nvidia_smi_empty(self):
        """Empty string should return empty dict."""
        poller = self._make_poller()
        result = poller._parse_nvidia_smi("")
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # test_parse_nvidia_smi_malformed
    # ------------------------------------------------------------------
    def test_parse_nvidia_smi_malformed(self):
        """Line with < 6 fields should be skipped."""
        poller = self._make_poller()
        csv = "0, NVIDIA RTX 3090, 62"  # only 3 fields
        result = poller._parse_nvidia_smi(csv)
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # test_parse_nvidia_smi_non_numeric
    # ------------------------------------------------------------------
    def test_parse_nvidia_smi_non_numeric(self):
        """Line with non-numeric temperature should be skipped (ValueError caught)."""
        poller = self._make_poller()
        csv = "0, NVIDIA RTX 3090, not_a_number, 85, 18400, 24576"
        result = poller._parse_nvidia_smi(csv)
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # test_parse_nvidia_smi_custom_map
    # ------------------------------------------------------------------
    def test_parse_nvidia_smi_custom_map(self):
        """Custom gpu_map should override default keys."""
        poller = self._make_poller(gpu_map={0: "my_gpu"})
        csv = "0, NVIDIA RTX 3090, 62, 85, 18400, 24576"
        result = poller._parse_nvidia_smi(csv)

        self.assertIn("my_gpu", result)
        self.assertNotIn("gpu_3090", result)
        self.assertEqual(result["my_gpu"]["temp"], 62)


# ═══════════════════════════════════════════════════════════════════════════
# Module constant tests
# ═══════════════════════════════════════════════════════════════════════════

class TestNvidiaSmiFields(unittest.TestCase):
    """Tests for _NVIDIA_SMI_FIELDS constant."""

    # ------------------------------------------------------------------
    # test_nvidia_smi_fields
    # ------------------------------------------------------------------
    def test_nvidia_smi_fields(self):
        """_NVIDIA_SMI_FIELDS should contain all expected nvidia-smi query fields."""
        expected_fields = [
            "index",
            "name",
            "temperature.gpu",
            "utilization.gpu",
            "memory.used",
            "memory.total",
        ]
        for field in expected_fields:
            self.assertIn(
                field, _NVIDIA_SMI_FIELDS,
                f"_NVIDIA_SMI_FIELDS missing '{field}'",
            )


# ═══════════════════════════════════════════════════════════════════════════
# GPUTelemetryPoller init tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPollerInit(unittest.TestCase):
    """Tests for GPUTelemetryPoller constructor defaults and clamping."""

    # ------------------------------------------------------------------
    # test_poller_init_defaults
    # ------------------------------------------------------------------
    def test_poller_init_defaults(self):
        """Default constructor should set expected parameter values."""
        with patch("gpu_telemetry.StreamingClient"):
            poller = GPUTelemetryPoller()

        self.assertEqual(poller._url, "http://127.0.0.1:3081")
        self.assertIsNone(poller._token)
        self.assertEqual(poller._interval, 5.0)
        self.assertIsNone(poller._ssh_host)
        self.assertEqual(poller._queue_depth, 0)

    # ------------------------------------------------------------------
    # test_poller_init_interval_floor
    # ------------------------------------------------------------------
    def test_poller_init_interval_floor(self):
        """Interval < 0.5 should be floored to 0.5."""
        with patch("gpu_telemetry.StreamingClient"):
            poller = GPUTelemetryPoller(interval=0.1)
        self.assertEqual(poller._interval, 0.5)

    def test_poller_init_interval_negative(self):
        """Negative interval should be floored to 0.5."""
        with patch("gpu_telemetry.StreamingClient"):
            poller = GPUTelemetryPoller(interval=-1.0)
        self.assertEqual(poller._interval, 0.5)


# ═══════════════════════════════════════════════════════════════════════════
# Poller start/stop tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPollerStartStop(unittest.TestCase):
    """Tests for GPUTelemetryPoller start/stop lifecycle."""

    # ------------------------------------------------------------------
    # test_poller_start_stop
    # ------------------------------------------------------------------
    def test_poller_start_stop(self):
        """start() should begin a daemon thread; stop() should cleanly exit."""
        with patch("gpu_telemetry.StreamingClient"):
            poller = GPUTelemetryPoller(interval=0.5)

        with patch.object(poller, "poll_once", return_value=None):
            poller.start()
            self.assertTrue(poller._running)
            self.assertIsNotNone(poller._thread)
            self.assertTrue(poller._thread.is_alive())
            self.assertTrue(poller._thread.daemon)

            poller.stop()
            self.assertFalse(poller._running)
            self.assertIsNone(poller._thread)

    # ------------------------------------------------------------------
    # test_poller_start_idempotent
    # ------------------------------------------------------------------
    def test_poller_start_idempotent(self):
        """Calling start() twice should be a no-op (no second thread created)."""
        with patch("gpu_telemetry.StreamingClient"):
            poller = GPUTelemetryPoller(interval=0.5)

        with patch.object(poller, "poll_once", return_value=None):
            poller.start()
            first_thread = poller._thread

            poller.start()  # second call — should be a no-op
            self.assertIs(poller._thread, first_thread)

            poller.stop()


# ═══════════════════════════════════════════════════════════════════════════
# poll_once tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPollOnce(unittest.TestCase):
    """Tests for GPUTelemetryPoller.poll_once()."""

    # ------------------------------------------------------------------
    # test_poll_once_with_mock
    # ------------------------------------------------------------------
    def test_poll_once_with_mock(self):
        """poll_once with valid CSV should call client.emit_event correctly."""
        valid_csv = (
            "0, NVIDIA GeForce RTX 3090, 62, 85, 18400, 24576\n"
            "1, NVIDIA GeForce RTX 3070, 55, 40, 6900, 8192"
        )
        with patch("gpu_telemetry.StreamingClient") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.emit_event.return_value = {"status": "ok"}

            poller = GPUTelemetryPoller()

            with patch.object(poller, "_query_nvidia_smi", return_value=valid_csv):
                result = poller.poll_once()

        # Verify emit_event was called with correct arguments
        mock_client.emit_event.assert_called_once()
        call_kwargs = mock_client.emit_event.call_args[1]
        self.assertEqual(call_kwargs["source"], "engine")
        self.assertEqual(call_kwargs["event_type"], "gpu.snapshot")
        self.assertIn("gpu_3090", call_kwargs["data"])
        self.assertIn("gpu_3070", call_kwargs["data"])

        # Verify return value
        self.assertIsNotNone(result)
        self.assertIn("gpu_3090", result)
        self.assertIn("queue_depth", result)

    # ------------------------------------------------------------------
    # test_poll_once_query_fails
    # ------------------------------------------------------------------
    def test_poll_once_query_fails(self):
        """poll_once when _query_nvidia_smi returns None should return None."""
        with patch("gpu_telemetry.StreamingClient") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client

            poller = GPUTelemetryPoller()

            with patch.object(poller, "_query_nvidia_smi", return_value=None):
                result = poller.poll_once()

        self.assertIsNone(result)
        mock_client.emit_event.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
