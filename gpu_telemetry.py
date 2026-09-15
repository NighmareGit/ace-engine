"""
gpu_telemetry.py — Lightweight background GPU poller.

Queries ``nvidia-smi`` on a configurable interval and emits ``gpu.snapshot``
events to the streaming server via :class:`StreamingClient`.

Designed to run both locally (Triton — direct nvidia-smi) and remotely
(via SSH).  Zero external dependencies beyond the Python standard library
and the ``StreamingClient`` sibling module.

Usage::

    # As a library
    from gpu_telemetry import GPUTelemetryPoller
    poller = GPUTelemetryPoller(interval=5.0, ssh_host="<user>@<LAN_IP>")
    poller.start()
    # ... later ...
    poller.stop()

    # As a CLI
    python3 gpu_telemetry.py --url http://127.0.0.1:3081 --interval 5
    python3 gpu_telemetry.py --ssh <user>@<LAN_IP> --token secret
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from streaming_client import StreamingClient

__all__ = ["GPUTelemetryPoller"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardware mapping — GPU index → dashboard key
# ---------------------------------------------------------------------------

# Our known hardware: GPU 0 = RTX 3090, GPU 1 = RTX 3070.
# Extend this dict as new GPUs are added.
_GPU_INDEX_MAP: Dict[int, str] = {
    0: "gpu_3090",
    1: "gpu_3070",
}

# nvidia-smi query fields (order matters — must match --query-gpu=…)
_NVIDIA_SMI_FIELDS: str = (
    "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total"
)
_NVIDIA_SMI_CMD: List[str] = [
    "nvidia-smi",
    "--query-gpu=" + _NVIDIA_SMI_FIELDS,
    "--format=csv,noheader,nounits",
]


# ---------------------------------------------------------------------------
# GPUTelemetryPoller
# ---------------------------------------------------------------------------


class GPUTelemetryPoller:
    """Background GPU telemetry poller.

    Periodically queries ``nvidia-smi`` (locally or via SSH), parses the
    CSV output, and emits ``gpu.snapshot`` events to the streaming server.

    Parameters
    ----------
    streaming_url:
        Base URL of the streaming server (default ``http://127.0.0.1:3081``).
    streaming_token:
        Optional bearer token for the streaming server.
    interval:
        Seconds between polls (default ``5.0``).
    ssh_host:
        If set, run ``nvidia-smi`` on the remote host via SSH.
        Example: ``"<user>@<LAN_IP>"``.
    gpu_map:
        Optional override for the GPU-index → dashboard-key mapping.
        Defaults to ``{0: "gpu_3090", 1: "gpu_3070"}``.
    queue_depth:
        Static queue depth value emitted with every snapshot (default ``0``).
    """

    def __init__(
        self,
        streaming_url: str = "http://127.0.0.1:3081",
        streaming_token: Optional[str] = None,
        interval: float = 5.0,
        ssh_host: Optional[str] = None,
        gpu_map: Optional[Dict[int, str]] = None,
        queue_depth: int = 0,
    ) -> None:
        self._url = streaming_url
        self._token = streaming_token
        self._interval = max(0.5, interval)  # floor at 0.5 s
        self._ssh_host = ssh_host
        self._gpu_map = gpu_map or dict(_GPU_INDEX_MAP)
        self._queue_depth = queue_depth

        self._client = StreamingClient(
            base_url=streaming_url,
            token=streaming_token,
            timeout=5.0,
            max_retries=1,  # fire-and-forget: no retry delay
        )

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start polling in a daemon background thread.

        Safe to call multiple times — subsequent calls are no-ops while the
        poller is already running.
        """
        if self._running:
            logger.debug("Poller already running — ignoring start()")
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="gpu-telemetry-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "GPU telemetry poller started (interval=%.1fs, ssh=%s)",
            self._interval,
            self._ssh_host or "local",
        )

    def stop(self) -> None:
        """Stop polling and wait for the background thread to finish."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2.0)
            self._thread = None
        logger.info("GPU telemetry poller stopped.")

    def poll_once(self) -> Optional[Dict[str, Any]]:
        """Query nvidia-smi and emit a ``gpu.snapshot`` event.

        Returns
        -------
        dict | None
            The event payload dict on success, or ``None`` if the query
            or emission failed.
        """
        try:
            raw = self._query_nvidia_smi()
            if raw is None:
                return None

            gpu_data = self._parse_nvidia_smi(raw)
            if not gpu_data:
                logger.warning("nvidia-smi returned no GPU data.")
                return None

            event_data: Dict[str, Any] = dict(gpu_data)
            event_data["queue_depth"] = self._queue_depth

            result = self._client.emit_event(
                source="engine",
                event_type="gpu.snapshot",
                data=event_data,
            )
            if result is not None:
                logger.debug("Emitted gpu.snapshot with %d GPU(s).", len(gpu_data))
            return event_data

        except Exception:
            # Never crash — fire-and-forget
            logger.exception("poll_once failed unexpectedly")
            return None

    # ------------------------------------------------------------------
    # nvidia-smi queries
    # ------------------------------------------------------------------

    def _query_nvidia_smi(self) -> Optional[str]:
        """Dispatch to local or SSH query based on configuration.

        Returns
        -------
        str | None
            Raw CSV output from nvidia-smi, or ``None`` on failure.
        """
        if self._ssh_host:
            return self._query_nvidia_smi_ssh(self._ssh_host)
        return self._query_nvidia_smi_local()

    def _query_nvidia_smi_local(self) -> Optional[str]:
        """Query nvidia-smi locally via subprocess.

        Returns
        -------
        str | None
            Raw stdout, or ``None`` on failure.
        """
        try:
            result = subprocess.run(
                _NVIDIA_SMI_CMD,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.error(
                    "nvidia-smi exited with code %d: %s",
                    result.returncode,
                    result.stderr.strip()[:200],
                )
                return None
            return result.stdout
        except FileNotFoundError:
            logger.error("nvidia-smi not found — is it installed and on PATH?")
            return None
        except subprocess.TimeoutExpired:
            logger.error("nvidia-smi timed out after 10 s.")
            return None
        except OSError as exc:
            logger.error("Failed to execute nvidia-smi: %s", exc)
            return None

    def _query_nvidia_smi_ssh(self, host: str) -> Optional[str]:
        """Query nvidia-smi on a remote host via SSH.

        Parameters
        ----------
        host:
            SSH target, e.g. ``"<user>@<LAN_IP>"``.

        Returns
        -------
        str | None
            Raw stdout from the remote nvidia-smi, or ``None`` on failure.
        """
        cmd_str = " ".join(_NVIDIA_SMI_CMD)
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            host,
            cmd_str,
        ]
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.error(
                    "SSH nvidia-smi exited with code %d: %s",
                    result.returncode,
                    result.stderr.strip()[:200],
                )
                return None
            return result.stdout
        except FileNotFoundError:
            logger.error("ssh binary not found — is OpenSSH installed?")
            return None
        except subprocess.TimeoutExpired:
            logger.error("SSH nvidia-smi timed out after 15 s.")
            return None
        except OSError as exc:
            logger.error("Failed to execute SSH command: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_nvidia_smi(self, output: str) -> Dict[str, Dict[str, Any]]:
        """Parse nvidia-smi CSV output into a dict of GPU telemetry dicts.

        Parameters
        ----------
        output:
            Raw CSV output with ``noheader,nounits`` formatting.

        Returns
        -------
        dict
            Mapping of dashboard key → GPU stats dict.  Example::

                {
                    "gpu_3090": {
                        "temp": 62,
                        "vram_used_mb": 18400,
                        "vram_total_mb": 24576,
                        "utilization_pct": 85
                    }
                }
        """
        gpus: Dict[str, Dict[str, Any]] = {}

        for line in output.strip().splitlines():
            line = line.strip()
            if not line:
                continue

            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                logger.warning(
                    "Skipping malformed nvidia-smi line (%d fields): %s",
                    len(parts),
                    line[:120],
                )
                continue

            try:
                gpu_index = int(parts[0])
                # parts[1] = GPU name (e.g. "NVIDIA GeForce RTX 3090")
                temp = int(parts[2])
                utilization = int(parts[3])
                vram_used = int(parts[4])
                vram_total = int(parts[5])
            except (ValueError, IndexError) as exc:
                logger.warning("Failed to parse nvidia-smi line: %s — %s", line[:120], exc)
                continue

            key = self._gpu_map.get(gpu_index, f"gpu_{gpu_index}")
            gpus[key] = {
                "temp": temp,
                "vram_used_mb": vram_used,
                "vram_total_mb": vram_total,
                "utilization_pct": utilization,
            }

        return gpus

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background thread: poll nvidia-smi at the configured interval."""
        logger.debug("Poll loop started.")
        while self._running and not self._stop_event.is_set():
            self.poll_once()
            # Sleep in small increments so stop() is responsive
            self._stop_event.wait(timeout=self._interval)
        logger.debug("Poll loop exited.")

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        mode = f"ssh={self._ssh_host}" if self._ssh_host else "local"
        return (
            f"GPUTelemetryPoller(url={self._url!r}, interval={self._interval}, "
            f"{mode}, token={'***' if self._token else 'none'})"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    """Parse CLI arguments and run the GPU telemetry poller."""
    parser = argparse.ArgumentParser(
        description="Background GPU telemetry poller — emits gpu.snapshot events to the streaming server.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("STREAMING_URL", "http://127.0.0.1:3081"),
        help="Streaming server URL (default: http://127.0.0.1:3081)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("STREAMING_TOKEN"),
        help="bearer <token> for the streaming server (default: none)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--ssh",
        dest="ssh_host",
        default=None,
        help="SSH host for remote nvidia-smi (e.g. <user>@<LAN_IP>)",
    )
    parser.add_argument(
        "--queue-depth",
        type=int,
        default=0,
        help="Static queue depth value to include in snapshots (default: 0)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    poller = GPUTelemetryPoller(
        streaming_url=args.url,
        streaming_token=args.token,
        interval=args.interval,
        ssh_host=args.ssh_host,
        queue_depth=args.queue_depth,
    )
    logger.info("Initializing poller: %r", poller)

    # --- Signal handling for graceful shutdown ---
    shutdown_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down.", sig_name)
        shutdown_event.set()
        poller.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # --- Start polling ---
    poller.start()

    # Block until signal
    logger.info("Polling every %.1fs. Press Ctrl+C to stop.", args.interval)
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    finally:
        poller.stop()
        logger.info("Bye.")


if __name__ == "__main__":
    main()
