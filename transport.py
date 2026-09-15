#!/usr/bin/env python3
"""
transport.py — Abstraction layer for local vs remote execution.

When running inside Docker on Triton (network_mode: host), the engine
talks to BeeLlama/Gitea via direct HTTP and runs commands locally.
When running on nightmare, it SSHs into Triton as before.
When the engine service is running, HTTPTransport talks to it.

Usage:
    from transport import get_transport
    t = get_transport()
    result = t.curl_beellama(8080, [{"role": "user", "content": "Hello"}])
    stdout, stderr, rc = t.run_command("git status")
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import urllib.request
import urllib.error
from typing import Any, Optional

log = logging.getLogger("transport")


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

RUNNING_IN_DOCKER = os.path.exists("/.dockerenv") or os.environ.get("CONTAINER") == "docker"

# Engine service URL — configurable via env
ENGINE_SERVICE_URL = os.environ.get("ENGINE_SERVICE_URL", "http://<LAN_IP>:3082")
ENGINE_TOKEN = os.environ.get("ENGINE_TOKEN", "")

# Transport mode override: "http", "local", "ssh", or "auto" (default)
TRANSPORT_MODE = os.environ.get("TRANSPORT_MODE", "auto")

def _is_on_triton() -> bool:
    """Detect if we're running directly on the Triton machine (not via SSH).

    Checks multiple signals:
    1. We're in a Docker container on Triton
    2. BeeLlama is reachable at localhost (meaning we're on the same host)
    3. The TRITON_HOST env var points to localhost/127.0.0.1 (explicit local mode)
    """
    if RUNNING_IN_DOCKER:
        return True
    # Check if TRITON_HOST is explicitly set to localhost
    host = os.environ.get("TRITON_HOST", "")
    if host in ("localhost", "127.0.0.1", ""):
        return True
    # Check if BeeLlama is reachable locally (quick health check)
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:8080/health", timeout=2)
        return True
    except Exception:
        return False

RUNNING_ON_TRITON = _is_on_triton()

def _engine_service_reachable() -> bool:
    """Check if the engine service is reachable at the configured URL."""
    try:
        req = urllib.request.Request(
            f"{ENGINE_SERVICE_URL}/health",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False

# Connection targets — configurable via environment
TRITON_HOST = os.environ.get("TRITON_HOST", "<LAN_IP>")
TRITON_USER = os.environ.get("TRITON_USER", "<user>")
TRITON_KEY_PATH = os.environ.get("TRITON_KEY_PATH", "")  # SSH key path (empty = agent default)

BEE_LLAMA_3090_URL = os.environ.get("BEE_LLAMA_3090", "http://localhost:8080")
BEE_LLAMA_3070_URL = os.environ.get("BEE_LLAMA_3070", "http://localhost:8082")
GITEA_URL = os.environ.get("GITEA_URL", "http://localhost:3000")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")

PORT_3090 = 8080
PORT_3070 = 8082


# ---------------------------------------------------------------------------
# Local transport (running inside Docker on Triton)
# ---------------------------------------------------------------------------

class LocalTransport:
    """Direct HTTP and subprocess calls — no SSH."""

    def raw_chat_completion(self, port: int, payload: dict) -> dict:
        """Send a chat-completion request and return the RAW OpenAI response.

        Unlike ``curl_beellama`` (which flattens the response and drops
        ``tool_calls``), this returns the unmodified response dict so callers
        like the IIL native lane can parse ``choices[*].message.tool_calls``.
        Additive — does not change ``curl_beellama`` for existing callers.
        """
        url = BEE_LLAMA_3090_URL if port == PORT_3090 else BEE_LLAMA_3070_URL
        endpoint = f"{url}/v1/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise RuntimeError(f"BeeLlama request failed: {e}")

    def curl_beellama(self, port: int, messages: list, max_tokens: int = 512,
                      temperature: float = 0.3, top_p: float = 0.95,
                      top_k: int = 40, extra_params: Optional[dict] = None) -> dict:
        """Send chat completion request directly via HTTP."""
        url = BEE_LLAMA_3090_URL if port == PORT_3090 else BEE_LLAMA_3070_URL
        endpoint = f"{url}/v1/chat/completions"

        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if extra_params:
            payload.update(extra_params)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                response = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise RuntimeError(f"BeeLlama request failed: {e}")

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
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            result["content"] = message.get("content", "")
            result["reasoning_content"] = message.get("reasoning_content", "")

        timings = response.get("timings", {})
        result["predicted_per_second"] = timings.get("predicted_per_second", 0.0)
        result["prompt_per_second"] = timings.get("prompt_per_second", 0.0)
        result["predicted_ms"] = timings.get("predicted_ms", 0.0)
        result["prompt_ms"] = timings.get("prompt_ms", 0.0)
        result["predicted_n"] = timings.get("predicted_n", 0)

        usage = response.get("usage", {})
        result["total_tokens"] = usage.get("total_tokens", 0)
        result["prompt_tokens"] = usage.get("prompt_tokens", 0)
        result["completion_tokens"] = usage.get(
            "completion_tokens", result["predicted_n"])

        result["thinking_tokens"] = timings.get("thinking_tokens", 0)
        if result["thinking_tokens"] == 0 and result["reasoning_content"]:
            result["thinking_tokens"] = len(result["reasoning_content"]) // 4

        return result

    def check_beellama_health(self, port: int) -> bool:
        """Check BeeLlama health endpoint directly."""
        url = BEE_LLAMA_3090_URL if port == PORT_3090 else BEE_LLAMA_3070_URL
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("status") == "ok"
        except Exception:
            return False

    def get_model_list(self, port: int) -> list:
        """Get loaded models from BeeLlama directly."""
        url = BEE_LLAMA_3090_URL if port == PORT_3090 else BEE_LLAMA_3070_URL
        try:
            with urllib.request.urlopen(f"{url}/v1/models", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return [m.get("id", "") for m in data.get("data", [])]
        except Exception:
            return []

    def run_command(self, cmd: str, timeout: int = 30, cwd: str = None) -> tuple:
        """Execute a local command. Returns (stdout, stderr, exit_code)."""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", 1

    def run_git(self, repo_path: str, *args, timeout: int = 30) -> tuple:
        """Run a git command in a repository. Returns (stdout, stderr, exit_code)."""
        cmd = f"git -C {shlex.quote(str(repo_path))} " + " ".join(shlex.quote(str(a)) for a in args)
        return self.run_command(cmd, timeout=timeout)

    def docker_exec(self, container: str, cmd: str, timeout: int = 30) -> tuple:
        """Execute a command inside a Docker container."""
        docker_cmd = f"docker exec {container} {cmd}"
        return self.run_command(docker_cmd, timeout=timeout)

    def docker_run(self, image: str, volumes: list = None, env: dict = None,
                   name: str = None, detach: bool = False, timeout: int = 30) -> tuple:
        """Run a Docker container."""
        parts = ["docker run"]
        if detach:
            parts.append("-d")
        if name:
            parts.append(f"--name {name}")
        if volumes:
            for v in volumes:
                parts.append(f"-v {v}")
        if env:
            for k, v in env.items():
                parts.append(f"-e {k}={v}")
        parts.append(image)
        cmd = " ".join(parts)
        return self.run_command(cmd, timeout=timeout)

    def nvidia_smi(self, gpu_index: Optional[int] = None) -> Any:
        """Query nvidia-smi directly."""
        query = "index,name,memory.used,memory.total,temperature.gpu,utilization.gpu"
        cmd = f"nvidia-smi --query-gpu={query} --format=csv,noheader,nounits"
        stdout, stderr, rc = self.run_command(cmd, timeout=10)
        if rc != 0:
            return None

        gpus = []
        for line in stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_used_mb": int(parts[2]),
                    "memory_total_mb": int(parts[3]),
                    "temperature_c": int(parts[4]),
                    "utilization_pct": int(parts[5]),
                })

        if gpu_index is not None:
            for gpu in gpus:
                if gpu["index"] == gpu_index:
                    return gpu
            return None
        return gpus

    def check_health(self) -> bool:
        """Check if this transport is reachable (always True for local)."""
        return True


# ---------------------------------------------------------------------------
# HTTP transport (via engine service)
# ---------------------------------------------------------------------------

class HTTPTransport:
    """HTTP-based transport — talks to the engine service on Triton.

    Mirrors the LocalTransport/RemoteTransport API but sends requests
    to the engine service instead of running commands directly.
    """

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or ENGINE_SERVICE_URL).rstrip("/")
        self.token = token or ENGINE_TOKEN

    def _request(self, method: str, path: str, data: Optional[dict] = None,
                 timeout: int = 30) -> dict:
        """Make an authenticated HTTP request to the engine service."""
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"Authorization": f"Token {self.token}"} if self.token else {}),
            },
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Engine service {method} {path} failed: "
                f"HTTP {e.code} — {body_text}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Engine service unreachable at {self.base_url}: {e}"
            ) from e

    def raw_chat_completion(self, port: int, payload: dict) -> dict:
        """Send a chat-completion request via the engine service, returning the
        RAW response (OpenAI choices format, with tool_calls intact)."""
        return self._request("POST", "/inference", data=payload, timeout=300)

    def curl_beellama(self, port: int, messages: list, max_tokens: int = 512,
                      temperature: float = 0.3, top_p: float = 0.95,
                      top_k: int = 40, extra_params: Optional[dict] = None) -> dict:
        """Send chat completion request via engine service."""
        payload: dict[str, Any] = {
            "port": port,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if extra_params:
            payload.update(extra_params)

        response = self._request("POST", "/inference", data=payload, timeout=300)

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
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        # Handle both flat format (engine service) and OpenAI choices format
        if "content" in response and "choices" not in response:
            # Flat format: engine service returns {content, reasoning_content, ...}
            result["content"] = response.get("content", "")
            result["reasoning_content"] = response.get("reasoning_content", "")
        else:
            # OpenAI format: {choices: [{message: {content, reasoning_content}}]}
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                result["content"] = message.get("content", "")
                result["reasoning_content"] = message.get("reasoning_content", "")

        timings = response.get("timings", {})
        result["predicted_per_second"] = timings.get("predicted_per_second", 0.0)
        result["prompt_per_second"] = timings.get("prompt_per_second", 0.0)
        result["predicted_ms"] = timings.get("predicted_ms", 0.0)
        result["prompt_ms"] = timings.get("prompt_ms", 0.0)
        result["predicted_n"] = timings.get("predicted_n", 0)

        usage = response.get("usage", {})
        result["total_tokens"] = usage.get("total_tokens", 0)
        result["prompt_tokens"] = usage.get("prompt_tokens", 0)
        result["completion_tokens"] = usage.get(
            "completion_tokens", result["predicted_n"])
        result["thinking_tokens"] = timings.get("thinking_tokens", 0)
        if result["thinking_tokens"] == 0 and result["reasoning_content"]:
            result["thinking_tokens"] = len(result["reasoning_content"]) // 4

        return result

    def run_command(self, cmd: str, timeout: int = 30, cwd: str = None) -> tuple:
        """Execute a command via engine service /exec endpoint."""
        payload: dict[str, Any] = {"command": cmd, "timeout": timeout}
        if cwd:
            payload["cwd"] = cwd
        try:
            resp = self._request("POST", "/exec", data=payload, timeout=timeout + 10)
            return (
                resp.get("stdout", ""),
                resp.get("stderr", ""),
                resp.get("returncode", 1),
            )
        except RuntimeError:
            return "", "Engine service exec failed", 1

    def run_git(self, repo_path: str, *args, timeout: int = 30) -> tuple:
        """Run a git command via engine service /exec endpoint."""
        cmd = f"git -C {shlex.quote(str(repo_path))} " + " ".join(shlex.quote(str(a)) for a in args)
        return self.run_command(cmd, timeout=timeout)

    def check_beellama_health(self, port: int) -> bool:
        """Check BeeLlama health via engine service."""
        try:
            resp = self._request("GET", f"/beellama/health?port={port}", timeout=10)
            return resp.get("status") == "ok"
        except RuntimeError:
            return False

    def get_model_list(self, port: int) -> list:
        """Get loaded models via engine service."""
        try:
            resp = self._request("GET", f"/beellama/models?port={port}", timeout=10)
            return [m.get("id", "") for m in resp.get("data", [])]
        except RuntimeError:
            return []

    def docker_exec(self, container: str, cmd: str, timeout: int = 30) -> tuple:
        """Execute a command inside a Docker container via engine service."""
        full_cmd = f"docker exec {container} {cmd}"
        return self.run_command(full_cmd, timeout=timeout)

    def docker_run(self, image: str, volumes: list = None, env: dict = None,
                   name: str = None, detach: bool = False, timeout: int = 30) -> tuple:
        """Run a Docker container via engine service."""
        parts = ["docker run"]
        if detach:
            parts.append("-d")
        if name:
            parts.append(f"--name {name}")
        if volumes:
            for v in volumes:
                parts.append(f"-v {v}")
        if env:
            for k, v in env.items():
                parts.append(f"-e {k}={v}")
        parts.append(image)
        return self.run_command(" ".join(parts), timeout=timeout)

    def nvidia_smi(self, gpu_index: Optional[int] = None) -> Any:
        """Query nvidia-smi via engine service /gpu-status endpoint."""
        try:
            path = "/gpu-status"
            if gpu_index is not None:
                path += f"?gpu_index={gpu_index}"
            resp = self._request("GET", path, timeout=10)
            return resp.get("gpus") if gpu_index is None else resp.get("gpu")
        except RuntimeError:
            return None

    def check_health(self) -> bool:
        """Check if the engine service is reachable."""
        return _engine_service_reachable()


# ---------------------------------------------------------------------------
# Remote transport (running on nightmare, SSH to Triton)
# ---------------------------------------------------------------------------

class RemoteTransport:
    """SSH-based transport — preserved as fallback when HTTP is unavailable.

    Uses SSH key authentication only (no sshpass dependency).
    Connection pooling via ControlMaster=auto.
    """

    def __init__(self):
        self.host = TRITON_HOST
        self.user = TRITON_USER
        self.key_path = TRITON_KEY_PATH

    def _ssh_cmd(self, cmd: str, timeout: int = 30) -> tuple:
        """Run command via SSH. Returns (stdout, stderr, exit_code).

        Uses key-based auth with ControlMaster for connection pooling.
        """
        ssh_args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", f"ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/cm-%r@%h:%p",
            "-o", "ControlPersist=300",
            "-o", f"ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=10",
        ]
        if self.key_path:
            ssh_args.extend(["-i", self.key_path])

        ssh_args.append(f"{self.user}@{self.host}")
        ssh_args.append(cmd)

        try:
            result = subprocess.run(ssh_args, capture_output=True, text=True, timeout=timeout)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "SSH command timed out", 1

    def raw_chat_completion(self, port: int, payload: dict) -> dict:
        """Send a chat-completion request via SSH tunnel, returning the RAW
        OpenAI response (with tool_calls intact)."""
        payload_json = json.dumps(payload)
        escaped = payload_json.replace("'", "'\\''")
        curl_cmd = (
            f"curl -s -X POST http://localhost:{port}/v1/chat/completions "
            f"-H 'Content-Type: application/json' "
            f"-d '{escaped}'"
        )
        stdout, stderr, rc = self._ssh_cmd(curl_cmd, timeout=120)
        if rc != 0:
            raise RuntimeError(f"curl failed (exit {rc}): {stderr}")
        return json.loads(stdout)

    def curl_beellama(self, port: int, messages: list, max_tokens: int = 512,
                      temperature: float = 0.3, top_p: float = 0.95,
                      top_k: int = 40, extra_params: Optional[dict] = None) -> dict:
        """Send chat completion via SSH tunnel."""
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if extra_params:
            payload.update(extra_params)

        payload_json = json.dumps(payload)
        escaped = payload_json.replace("'", "'\\''")
        curl_cmd = (
            f"curl -s -X POST http://localhost:{port}/v1/chat/completions "
            f"-H 'Content-Type: application/json' "
            f"-d '{escaped}'"
        )

        stdout, stderr, rc = self._ssh_cmd(curl_cmd, timeout=60)
        if rc != 0:
            raise RuntimeError(f"curl failed (exit {rc}): {stderr}")

        response = json.loads(stdout)

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
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            result["content"] = message.get("content", "")
            result["reasoning_content"] = message.get("reasoning_content", "")

        timings = response.get("timings", {})
        result["predicted_per_second"] = timings.get("predicted_per_second", 0.0)
        result["prompt_per_second"] = timings.get("prompt_per_second", 0.0)
        result["predicted_ms"] = timings.get("predicted_ms", 0.0)
        result["prompt_ms"] = timings.get("prompt_ms", 0.0)
        result["predicted_n"] = timings.get("predicted_n", 0)

        usage = response.get("usage", {})
        result["total_tokens"] = usage.get("total_tokens", 0)
        result["prompt_tokens"] = usage.get("prompt_tokens", 0)
        result["completion_tokens"] = usage.get(
            "completion_tokens", result["predicted_n"])
        result["thinking_tokens"] = timings.get("thinking_tokens", 0)
        if result["thinking_tokens"] == 0 and result["reasoning_content"]:
            result["thinking_tokens"] = len(result["reasoning_content"]) // 4

        return result

    def check_beellama_health(self, port: int) -> bool:
        cmd = f"curl -s http://localhost:{port}/health"
        stdout, stderr, rc = self._ssh_cmd(cmd, timeout=10)
        if rc != 0:
            return False
        try:
            return json.loads(stdout).get("status") == "ok"
        except Exception:
            return False

    def get_model_list(self, port: int) -> list:
        cmd = f"curl -s http://localhost:{port}/v1/models"
        stdout, stderr, rc = self._ssh_cmd(cmd, timeout=10)
        if rc != 0:
            return []
        try:
            return [m.get("id", "") for m in json.loads(stdout).get("data", [])]
        except Exception:
            return []

    def run_command(self, cmd: str, timeout: int = 30, cwd: str = None) -> tuple:
        if cwd:
            cmd = f"cd '{cwd}' && {cmd}"
        return self._ssh_cmd(cmd, timeout=timeout)

    def run_git(self, repo_path: str, *args, timeout: int = 30) -> tuple:
        cmd = f"git -C {shlex.quote(str(repo_path))} " + " ".join(shlex.quote(str(a)) for a in args)
        return self._ssh_cmd(cmd, timeout=timeout)

    def docker_exec(self, container: str, cmd: str, timeout: int = 30) -> tuple:
        return self._ssh_cmd(f"docker exec {container} {cmd}", timeout=timeout)

    def docker_run(self, image: str, volumes: list = None, env: dict = None,
                   name: str = None, detach: bool = False, timeout: int = 30) -> tuple:
        parts = ["docker run"]
        if detach:
            parts.append("-d")
        if name:
            parts.append(f"--name {name}")
        if volumes:
            for v in volumes:
                parts.append(f"-v {v}")
        if env:
            for k, v in env.items():
                parts.append(f"-e {k}={v}")
        parts.append(image)
        return self._ssh_cmd(" ".join(parts), timeout=timeout)

    def nvidia_smi(self, gpu_index: Optional[int] = None) -> Any:
        query = "index,name,memory.used,memory.total,temperature.gpu,utilization.gpu"
        cmd = f"nvidia-smi --query-gpu={query} --format=csv,noheader,nounits"
        stdout, stderr, rc = self._ssh_cmd(cmd, timeout=10)
        if rc != 0:
            return None

        gpus = []
        for line in stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_used_mb": int(parts[2]),
                    "memory_total_mb": int(parts[3]),
                    "temperature_c": int(parts[4]),
                    "utilization_pct": int(parts[5]),
                })

        if gpu_index is not None:
            for gpu in gpus:
                if gpu["index"] == gpu_index:
                    return gpu
            return None
        return gpus

    def check_health(self) -> bool:
        """Check if SSH to Triton is reachable."""
        stdout, stderr, rc = self._ssh_cmd("echo ok", timeout=10)
        return rc == 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_transport_instance = None

def get_transport():
    """Get the appropriate transport based on environment and availability.

    Detection order (unless overridden by TRANSPORT_MODE env var):
    1. Try HTTP — probe engine service at ENGINE_SERVICE_URL
    2. Try local — we're on Triton (BeeLlama reachable at localhost)
    3. Fall back to SSH — remote via key-based auth

    TRANSPORT_MODE env var can force: "http", "local", "ssh", or "auto" (default).
    """
    global _transport_instance
    if _transport_instance is not None:
        return _transport_instance

    mode = TRANSPORT_MODE.lower()

    # Forced mode
    if mode == "http":
        log.info("Transport forced to HTTP via TRANSPORT_MODE env")
        _transport_instance = HTTPTransport()
        return _transport_instance
    elif mode == "local":
        log.info("Transport forced to local via TRANSPORT_MODE env")
        _transport_instance = LocalTransport()
        return _transport_instance
    elif mode == "ssh":
        log.info("Transport forced to SSH via TRANSPORT_MODE env")
        _transport_instance = RemoteTransport()
        return _transport_instance

    # Auto-detection: HTTP → Local → SSH
    if _engine_service_reachable():
        log.info("Transport: HTTP engine service detected at %s", ENGINE_SERVICE_URL)
        _transport_instance = HTTPTransport()
        return _transport_instance

    if RUNNING_ON_TRITON:
        log.info("Transport: local (on Triton, BeeLlama reachable)")
        _transport_instance = LocalTransport()
        return _transport_instance

    log.info("Transport: SSH fallback to %s@%s", TRITON_USER, TRITON_HOST)
    _transport_instance = RemoteTransport()
    return _transport_instance

def reset_transport():
    """Reset the singleton (for testing)."""
    global _transport_instance
    _transport_instance = None
