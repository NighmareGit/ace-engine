#!/usr/bin/env python3
"""
engine_client.py — HTTP client for the engine control layer.

Mirrors the LocalTransport/RemoteTransport API over HTTP.
Zero external dependencies — stdlib urllib only.

Usage:
    from engine_client import EngineClient
    client = EngineClient("http://localhost:3082", token="secret")
    result = client.curl_beellama(8080, [{"role":"user","content":"Hi"}])
    stdout, stderr, rc = client.run_command("ls -la")
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Iterator, List, Optional, Tuple


class EngineClientError(Exception):
    """Raised when an engine service request fails after retries."""

    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class EngineClient:
    """HTTP client for the engine service, mirroring the transport API.

    Features:
        - Token auth (Authorization: Token <secret> — NOT Bearer)
        - Retry with exponential backoff (3 attempts, 1s/2s/4s)
        - Per-endpoint timeout configuration
        - Connection reuse via urllib
    """

    # Retry config
    MAX_RETRIES = 3
    BACKOFF_DELAYS = [1, 2, 4]

    # Timeout config (seconds)
    TIMEOUT_INFERENCE = 300
    TIMEOUT_FILE_OPS = 60
    TIMEOUT_GIT = 30
    TIMEOUT_HEALTH = 10
    TIMEOUT_GPU = 15
    TIMEOUT_EXEC = 120

    def __init__(self, base_url: str, token: str = ""):
        """
        Args:
            base_url: Engine service base URL, e.g. "http://localhost:3082"
            token: Token for Authorization: Token <secret> header (NOT Bearer)
        """
        self.base_url = base_url.rstrip("/")
        self.token = token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Token {self.token}"
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[bytes] = None,
        timeout: int = 30,
        extra_headers: Optional[Dict[str, str]] = None,
        raw_response: bool = False,
    ) -> Any:
        """Execute an HTTP request with retry + backoff.

        Returns parsed JSON dict by default. Set raw_response=True to get
        the urllib response object (used by stream).
        """
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES):
            headers = self._headers(extra_headers)
            req = urllib.request.Request(
                url, data=data, headers=headers, method=method,
            )
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                if raw_response:
                    return resp
                body = resp.read().decode("utf-8")
                if not body:
                    return {}
                return json.loads(body)
            except urllib.error.HTTPError as e:
                last_exc = e
                status = e.code
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8")
                except Exception:
                    pass
                # Don't retry on 4xx (client errors) except 429
                if 400 <= status < 500 and status != 429:
                    raise EngineClientError(
                        f"HTTP {status} {method} {path}: {body_text}",
                        status_code=status,
                        body=body_text,
                    )
                # Retry on 5xx and 429
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BACKOFF_DELAYS[attempt])
                    continue
                raise EngineClientError(
                    f"HTTP {status} after {self.MAX_RETRIES} attempts: {body_text}",
                    status_code=status,
                    body=body_text,
                )
            except (urllib.error.URLError, OSError) as e:
                last_exc = e
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BACKOFF_DELAYS[attempt])
                    continue
                raise EngineClientError(
                    f"Connection failed after {self.MAX_RETRIES} attempts: {e}",
                )

        # Should not reach here, but just in case
        raise EngineClientError(
            f"Request failed after {self.MAX_RETRIES} attempts",
        )

    # ------------------------------------------------------------------
    # Transport-compatible methods (mirrors LocalTransport API)
    # ------------------------------------------------------------------

    def curl_beellama(
        self,
        port: int,
        messages: List[dict],
        max_tokens: int = 512,
        temperature: float = 0.3,
        top_p: float = 0.95,
        top_k: int = 40,
        extra_params: Optional[dict] = None,
    ) -> dict:
        """Send chat completion request via the engine service.

        Mirrors LocalTransport.curl_beellama().
        """
        payload: Dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if extra_params:
            payload.update(extra_params)

        body = json.dumps({
            "port": port,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }).encode("utf-8")

        resp = self._request(
            "POST", "/inference",
            data=body, timeout=self.TIMEOUT_INFERENCE,
            extra_headers={"Content-Type": "application/json"},
        )
        return resp

    def run_command(self, cmd: str, timeout: int = 30, cwd: str = None) -> Tuple[str, str, int]:
        """Execute a shell command via the engine service.

        Mirrors LocalTransport.run_command().
        Returns (stdout, stderr, exit_code).
        """
        payload = json.dumps({
            "command": cmd,
            "timeout": timeout,
        }).encode("utf-8")

        resp = self._request(
            "POST", "/exec",
            data=payload, timeout=timeout + 10,
            extra_headers={"Content-Type": "application/json"},
        )
        return (
            resp.get("stdout", ""),
            resp.get("stderr", ""),
            resp.get("returncode", -1),
        )

    def run_git(self, repo_path: str, *args, timeout: int = 30) -> Tuple[str, str, int]:
        """Run a git command via the engine service.

        Mirrors LocalTransport.run_git().
        """
        cmd = f"git -C '{repo_path}' {' '.join(args)}"
        return self.run_command(cmd, timeout=timeout)

    def check_beellama_health(self, port: int) -> bool:
        """Check BeeLlama health via the engine service.

        Mirrors LocalTransport.check_beellama_health().
        """
        try:
            resp = self._request("GET", "/health", timeout=self.TIMEOUT_HEALTH)
            return resp.get("status") == "ok"
        except EngineClientError:
            return False

    def nvidia_smi(self, gpu_index: Optional[int] = None) -> Any:
        """Query GPU status via the engine service.

        Mirrors LocalTransport.nvidia_smi().
        """
        try:
            resp = self._request("GET", "/gpu-status", timeout=self.TIMEOUT_GPU)
            gpus = resp.get("gpus", [])
            if gpu_index is not None:
                for gpu in gpus:
                    if gpu.get("index") == gpu_index:
                        return gpu
                return None
            return gpus
        except EngineClientError:
            return None

    # ------------------------------------------------------------------
    # Engine-specific methods (used by Issue #4 client spec)
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """GET /health — service status, uptime, model info."""
        return self._request("GET", "/health", timeout=self.TIMEOUT_HEALTH)

    def inference(
        self,
        port: int,
        messages: List[dict],
        max_tokens: int = 512,
        temperature: float = 0.3,
        **kwargs,
    ) -> dict:
        """POST /inference — proxy chat completions to BeeLlama."""
        payload = json.dumps({
            "port": port,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs,
        }).encode("utf-8")
        return self._request(
            "POST", "/inference",
            data=payload, timeout=self.TIMEOUT_INFERENCE,
            extra_headers={"Content-Type": "application/json"},
        )

    def write_files(self, project_path: str, files: List[dict]) -> dict:
        """POST /write-files — atomic write of project files."""
        payload = json.dumps({
            "project_path": project_path,
            "files": files,
        }).encode("utf-8")
        return self._request(
            "POST", "/write-files",
            data=payload, timeout=self.TIMEOUT_FILE_OPS,
            extra_headers={"Content-Type": "application/json"},
        )

    def run_tests(self, project_path: str, pattern: str = None) -> dict:
        """POST /run-tests — execute pytest in project directory."""
        body: Dict[str, Any] = {"project_path": project_path}
        if pattern:
            body["pattern"] = pattern
        payload = json.dumps(body).encode("utf-8")
        return self._request(
            "POST", "/run-tests",
            data=payload, timeout=self.TIMEOUT_INFERENCE,
            extra_headers={"Content-Type": "application/json"},
        )

    def git_commit(self, repo_path: str, files: List[str], message: str) -> dict:
        """POST /git-commit — stage + commit via subprocess."""
        payload = json.dumps({
            "repo_path": repo_path,
            "files": files,
            "message": message,
        }).encode("utf-8")
        return self._request(
            "POST", "/git-commit",
            data=payload, timeout=self.TIMEOUT_GIT,
            extra_headers={"Content-Type": "application/json"},
        )

    def exec_command(self, command: str, timeout: int = 60) -> dict:
        """POST /exec — execute shell command with dangerous-command blocking."""
        payload = json.dumps({
            "command": command,
            "timeout": timeout,
        }).encode("utf-8")
        return self._request(
            "POST", "/exec",
            data=payload, timeout=timeout + 10,
            extra_headers={"Content-Type": "application/json"},
        )

    def gpu_status(self) -> dict:
        """GET /gpu-status — per-GPU metrics from nvidia-smi."""
        return self._request("GET", "/gpu-status", timeout=self.TIMEOUT_GPU)

    def stream(self, channels: Optional[List[str]] = None) -> Iterator[dict]:
        """GET /stream — SSE endpoint for engine events.

        Yards parsed SSE event dicts with 'event', 'data', and optional 'id' fields.
        Blocks until a heartbeat or real event arrives (30s heartbeat cycle).
        """
        path = "/stream"
        if channels:
            qs = "&".join(f"channel={c}" for c in channels)
            path = f"{path}?{qs}"

        resp = self._request(
            "GET", path,
            timeout=self.TIMEOUT_INFERENCE,
            raw_response=True,
        )

        event_type = None
        event_data_lines: List[str] = []
        event_id = None

        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\n\r")

            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("id:"):
                event_id = line[len("id:"):].strip()
            elif line.startswith("data:"):
                event_data_lines.append(line[len("data:"):].strip())
            elif line == "":
                # Empty line = end of event
                if event_data_lines:
                    data_str = "\n".join(event_data_lines)
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = data_str
                    yield {
                        "event": event_type or "message",
                        "data": data,
                        "id": event_id,
                    }
                event_type = None
                event_data_lines = []
                event_id = None
