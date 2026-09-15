"""SSH utilities for connecting to Triton and interacting with beellama endpoints."""

import json
import time
import subprocess
import os

TRITON_HOST = "<LAN_IP>"
TRITON_USER = "<user>"
TRITON_PW = "12345"
PORT_3090 = 8080
PORT_3070 = 8082


class SSHClient:
    """SSH client for Triton machine using sshpass + ssh."""

    def __init__(self):
        self.host = TRITON_HOST
        self.user = TRITON_USER
        self.pw = TRITON_PW
        self.connected = False

    def connect(self, host=None, user=None, pw=None, key_path=None):
        """Test SSH connectivity. Returns True if successful."""
        if host:
            self.host = host
        if user:
            self.user = user
        if pw:
            self.pw = pw

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                if key_path:
                    cmd = [
                        "ssh",
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=10",
                        "-i", key_path,
                        f"{self.user}@{self.host}",
                        "echo OK"
                    ]
                else:
                    cmd = [
                        "sshpass", "-p", self.pw,
                        "ssh",
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=10",
                        f"{self.user}@{self.host}",
                        "echo OK"
                    ]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=15
                )

                if result.returncode == 0 and "OK" in result.stdout:
                    self.connected = True
                    return True

            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass

            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)

        self.connected = False
        return False

    def run(self, cmd, timeout=30):
        """Run a command on Triton via SSH. Returns (stdout, stderr, exit_code)."""
        if not self.connected:
            raise ConnectionError("Not connected. Call connect() first.")

        ssh_cmd = [
            "sshpass", "-p", self.pw,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{self.user}@{self.host}",
            cmd
        ]

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "SSH command timed out", 1

    def curl_beellama(self, port, messages, max_tokens=512, temperature=0.3,
                      top_p=0.95, top_k=40, extra_params=None):
        """Send a chat completion request to beellama endpoint.

        Args:
            port: beellama port (8080 or 8082)
            messages: list of {"role": "user", "content": "..."} dicts
            max_tokens: max tokens to generate
            temperature: sampling temperature
            top_p: top-p sampling
            top_k: top-k sampling
            extra_params: additional API parameters

        Returns:
            dict with keys: content, reasoning_content, predicted_per_second,
            prompt_per_second, predicted_ms, prompt_ms, predicted_n,
            thinking_tokens, total_tokens
        """
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if extra_params:
            payload.update(extra_params)

        # Escape single quotes in JSON for shell
        payload_json = json.dumps(payload)
        escaped_payload = payload_json.replace("'", "'\\''")

        curl_cmd = (
            f"curl -s -X POST http://localhost:{port}/v1/chat/completions "
            f"-H 'Content-Type: application/json' "
            f"-d '{escaped_payload}'"
        )

        stdout, stderr, exit_code = self.run(curl_cmd, timeout=60)

        if exit_code != 0:
            raise RuntimeError(f"curl failed (exit {exit_code}): {stderr}")

        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse response JSON: {e}\nRaw: {stdout[:500]}")

        result = {
            "content": "",
            "reasoning_content": "",
            "predicted_per_second": 0.0,
            "prompt_per_second": 0.0,
            "predicted_ms": 0.0,
            "prompt_ms": 0.0,
            "predicted_n": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
        }

        # Extract content from the first choice
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            result["content"] = message.get("content", "")
            result["reasoning_content"] = message.get("reasoning_content", "")

        # Extract timings
        timings = response.get("timings", {})
        result["predicted_per_second"] = timings.get("predicted_per_second", 0.0)
        result["prompt_per_second"] = timings.get("prompt_per_second", 0.0)
        result["predicted_ms"] = timings.get("predicted_ms", 0.0)
        result["prompt_ms"] = timings.get("prompt_ms", 0.0)
        result["predicted_n"] = timings.get("predicted_n", 0)

        # Extract token counts
        usage = response.get("usage", {})
        result["total_tokens"] = usage.get("total_tokens", 0)

        # Count thinking tokens from reasoning_content length approximation
        # or from explicit thinking_tokens field if present
        result["thinking_tokens"] = timings.get("thinking_tokens", 0)
        if result["thinking_tokens"] == 0 and result["reasoning_content"]:
            # Rough approximation: ~4 chars per token
            result["thinking_tokens"] = len(result["reasoning_content"]) // 4

        return result

    def get_nvidia_smi(self, gpu_index=None):
        """Query nvidia-smi on Triton. Returns dict with memory, temperature, utilization."""
        query_fields = "index,name,memory.used,memory.total,temperature.gpu,utilization.gpu"
        cmd = (
            f"nvidia-smi --query-gpu={query_fields} --format=csv,noheader,nounits"
        )

        stdout, stderr, exit_code = self.run(cmd, timeout=10)
        if exit_code != 0:
            raise RuntimeError(f"nvidia-smi failed: {stderr}")

        gpus = []
        for line in stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpu = {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_used_mb": int(parts[2]),
                    "memory_total_mb": int(parts[3]),
                    "temperature_c": int(parts[4]),
                    "utilization_pct": int(parts[5]),
                }
                gpus.append(gpu)

        if gpu_index is not None:
            for gpu in gpus:
                if gpu["index"] == gpu_index:
                    return gpu
            return None

        return gpus

    def check_beellama_health(self, port):
        """Check if beellama endpoint is healthy. Returns bool."""
        cmd = f"curl -s http://localhost:{port}/health"
        stdout, stderr, exit_code = self.run(cmd, timeout=10)

        if exit_code != 0:
            return False

        try:
            response = json.loads(stdout)
            return response.get("status") == "ok"
        except (json.JSONDecodeError, AttributeError):
            return False

    def get_model_list(self, port):
        """Get list of models loaded on beellama endpoint. Returns list of model names."""
        cmd = f"curl -s http://localhost:{port}/v1/models"
        stdout, stderr, exit_code = self.run(cmd, timeout=10)

        if exit_code != 0:
            return []

        try:
            response = json.loads(stdout)
            models = response.get("data", [])
            return [m.get("id", "") for m in models]
        except (json.JSONDecodeError, AttributeError):
            return []

    def close(self):
        """Close connection (cleanup)."""
        self.connected = False


def get_ssh_client(host=None, user=None, pw=None):
    """Create and connect an SSHClient."""
    client = SSHClient()
    client.connect(host, user, pw)
    return client
