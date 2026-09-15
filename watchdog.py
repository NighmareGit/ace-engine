"""GPU watchdog that monitors Triton GPUs and triggers abort on failure."""

import threading
import time
from datetime import datetime


class GPUWatchdog:
    """Polls nvidia-smi via SSH every N seconds. Sets abort_event on GPU failure."""

    def __init__(self, ssh_client=None, gpu_indices=None, poll_interval=30,
                 abort_event=None, max_temperature=83):
        """
        Args:
            ssh_client: SSHClient instance (or None to create one)
            gpu_indices: list of GPU indices to monitor (default [0, 1])
            poll_interval: seconds between polls (default 30)
            abort_event: threading.Event to set on failure (created if None)
            max_temperature: GPU temp threshold in Celsius (default 83)
        """
        self.ssh = ssh_client
        self.gpu_indices = gpu_indices or [0, 1]
        self.poll_interval = poll_interval
        self.abort_event = abort_event or threading.Event()
        self.max_temperature = max_temperature
        self._thread = None
        self._running = False
        self.gpu_status = {}  # {gpu_index: {temp, vram_used, vram_total, present}}
        self._last_poll_time = None
        self._poll_count = 0
        self._failure_reason = None

    def poll_once(self):
        """Poll nvidia-smi once. Returns dict with GPU status.

        Returns:
            dict: {gpu_index: {present: bool, temperature_c: float,
                   memory_used_mb: float, memory_total_mb: float}}
        """
        status = {}
        try:
            if self.ssh is None:
                # No SSH client — return empty status, don't abort
                for idx in self.gpu_indices:
                    status[idx] = {
                        "present": False,
                        "temperature_c": 0,
                        "memory_used_mb": 0,
                        "memory_total_mb": 0,
                    }
                return status

            gpu_list = self.ssh.get_nvidia_smi()
            # Build lookup by index
            gpu_by_index = {g["index"]: g for g in gpu_list}

            for idx in self.gpu_indices:
                gpu = gpu_by_index.get(idx)
                if gpu is None:
                    # GPU disappeared from nvidia-smi output
                    status[idx] = {
                        "present": False,
                        "temperature_c": 0,
                        "memory_used_mb": 0,
                        "memory_total_mb": 0,
                    }
                    self._failure_reason = f"GPU {idx} disappeared from nvidia-smi"
                    self.abort_event.set()
                else:
                    status[idx] = {
                        "present": True,
                        "temperature_c": gpu["temperature_c"],
                        "memory_used_mb": gpu["memory_used_mb"],
                        "memory_total_mb": gpu["memory_total_mb"],
                        "utilization_pct": gpu["utilization_pct"],
                        "name": gpu["name"],
                    }
                    # Check temperature threshold
                    if gpu["temperature_c"] > self.max_temperature:
                        self._failure_reason = (
                            f"GPU {idx} temperature {gpu['temperature_c']}C "
                            f"> {self.max_temperature}C threshold"
                        )
                        self.abort_event.set()

            self.gpu_status = status

        except Exception as e:
            # SSH or nvidia-smi failure — treat as GPU issue
            self._failure_reason = f"nvidia-smi query failed: {e}"
            for idx in self.gpu_indices:
                status[idx] = {
                    "present": False,
                    "temperature_c": 0,
                    "memory_used_mb": 0,
                    "memory_total_mb": 0,
                }
            self.gpu_status = status
            self.abort_event.set()

        self._last_poll_time = datetime.now()
        self._poll_count += 1
        return status

    def start(self):
        """Start background monitoring thread."""
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        """Background loop that polls periodically."""
        while self._running and not self.abort_event.is_set():
            self.poll_once()
            # Sleep in small increments so we can respond to stop quickly
            for _ in range(self.poll_interval):
                if not self._running or self.abort_event.is_set():
                    break
                time.sleep(1)

    def is_aborted(self):
        """Check if abort was triggered."""
        return self.abort_event.is_set()

    def get_status_summary(self):
        """Return human-readable status summary."""
        lines = []
        lines.append(f"Watchdog: {self._poll_count} polls completed")
        if self._last_poll_time:
            lines.append(f"  Last poll: {self._last_poll_time:%Y-%m-%d %H:%M:%S}")
        if self.is_aborted():
            lines.append(f"  ABORTED: {self._failure_reason}")
        lines.append(f"  Monitoring GPUs: {self.gpu_indices}")
        lines.append(f"  Temperature threshold: {self.max_temperature}C")
        lines.append(f"  Poll interval: {self.poll_interval}s")

        for idx in self.gpu_indices:
            info = self.gpu_status.get(idx, {})
            if info.get("present"):
                name = info.get("name", "unknown")
                temp = info.get("temperature_c", 0)
                vram_used = info.get("memory_used_mb", 0)
                vram_total = info.get("memory_total_mb", 0)
                util = info.get("utilization_pct", 0)
                lines.append(
                    f"  GPU {idx} ({name}): {temp}C, "
                    f"{vram_used}/{vram_total} MB VRAM, {util}% util"
                )
            else:
                lines.append(f"  GPU {idx}: NOT PRESENT")

        return "\n".join(lines)
