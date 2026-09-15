#!/usr/bin/env python3
"""
engine_service.py — FastAPI HTTP server wrapping coder-harness engine operations.

Issue #1: Core + Auth + Health
Issue #2: Inference + File Ops + Tests + Git

Usage:
    ENGINE_TOKEN=secret python3 engine_service.py --port 3082
    python3 engine_service.py --token mysecret --port 3082
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(format=LOG_FMT, level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("engine_service")


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------

class ServiceState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    PROCESSING = "PROCESSING"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class LifecycleManager:
    """Tracks service state and consecutive error count."""

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.STOPPED
        self.started_at: float = time.time()
        self.consecutive_errors: int = 0
        self._state_file: Path = Path(__file__).parent / ".engine_state"

    # -- transitions --------------------------------------------------------

    def set(self, new_state: ServiceState) -> None:
        old = self.state
        self.state = new_state
        logger.info("state transition: %s → %s", old.value, new_state.value)
        self._persist()

    def record_error(self) -> None:
        self.consecutive_errors += 1
        logger.warning(
            "consecutive error count: %d", self.consecutive_errors
        )
        if self.consecutive_errors >= 10:
            self.set(ServiceState.FAILED)
        elif self.consecutive_errors >= 5:
            self.set(ServiceState.DEGRADED)

    def record_success(self) -> None:
        self.consecutive_errors = 0
        if self.state in (ServiceState.DEGRADED, ServiceState.PROCESSING):
            self.set(ServiceState.READY)

    def uptime(self) -> float:
        return time.time() - self.started_at

    # -- persistence --------------------------------------------------------

    def _persist(self) -> None:
        try:
            self._state_file.write_text(json.dumps({
                "state": self.state.value,
                "consecutive_errors": self.consecutive_errors,
                "started_at": self.started_at,
            }))
        except OSError:
            pass

    def recover(self) -> None:
        """Attempt to recover state from a previous .state file."""
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                saved = ServiceState(data.get("state", ServiceState.STOPPED.value))
                if saved not in (ServiceState.FAILED, ServiceState.STOPPED):
                    self.consecutive_errors = data.get("consecutive_errors", 0)
                self.started_at = data.get("started_at", time.time())
            except (json.JSONDecodeError, ValueError, KeyError):
                pass


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

lifecycle = LifecycleManager()
shutdown_event = asyncio.Event()
START_TIME: float = time.time()


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------

# -- shared ----------------------------------------------------------------

class ErrorResponse(BaseModel):
    error: str
    detail: str
    status_code: int


# -- health ----------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    uptime: float
    model: str
    gpu_temp: list[dict[str, Any]]


# -- inference -------------------------------------------------------------

class InferenceMessage(BaseModel):
    role: str = Field(..., examples=["user"])
    content: str = Field(..., examples=["Hello, how are you?"])


class InferenceRequest(BaseModel):
    messages: list[InferenceMessage]
    port: int = Field(default=8080, description="BeeLlama port: 8080 (3090) or 8082 (3070)")
    max_tokens: int = Field(default=512, ge=1, le=32768)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=1)
    stream: bool = Field(default=False)


class InferenceTimings(BaseModel):
    prompt_per_second: Optional[float] = None
    predicted_per_second: Optional[float] = None
    prompt_ms: Optional[float] = None
    predicted_ms: Optional[float] = None


class InferenceUsage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class InferenceResponse(BaseModel):
    content: str
    reasoning_content: Optional[str] = None
    timings: Optional[InferenceTimings] = None
    usage: Optional[InferenceUsage] = None
    model: Optional[str] = None


# -- write-files -----------------------------------------------------------

class FileEntry(BaseModel):
    path: str = Field(..., examples=["src/main.py"])
    content: str = Field(..., description="Base64-encoded file content")
    executable: bool = Field(default=False, description="Set file +x")


class WriteFilesRequest(BaseModel):
    project_path: str = Field(..., examples=["/home/<user>/projects/my-project"])
    files: list[FileEntry]


class WriteFileResult(BaseModel):
    path: str
    status: str  # "written" | "error"
    detail: Optional[str] = None


class WriteFilesResponse(BaseModel):
    written: list[str]
    errors: list[dict[str, str]]
    total: int


# -- run-tests -------------------------------------------------------------

class RunTestsRequest(BaseModel):
    project_path: str
    pattern: Optional[str] = Field(default=None, description="pytest -k pattern filter")
    test_path: Optional[str] = Field(default=None, description="Subdirectory or file to test")
    framework: str = Field(default="pytest", pattern="^(pytest|unittest)$")


class TestResult(BaseModel):
    name: str
    status: str  # "passed" | "failed" | "error" | "skipped"
    detail: Optional[str] = None
    duration: Optional[float] = None


class RunTestsResponse(BaseModel):
    framework: str
    passed: int
    failed: int
    errors: int
    skipped: int
    total: int
    results: list[TestResult]
    output: str
    timed_out: bool = False
    return_code: Optional[int] = None
    duration: float = 0.0


# -- git-commit ------------------------------------------------------------

class GitCommitRequest(BaseModel):
    project_path: str
    message: str = Field(..., min_length=1, max_length=1000)
    files: Optional[list[str]] = Field(
        default=None,
        description="Specific files to stage. None = git add -A",
    )


class GitCommitResponse(BaseModel):
    sha: str
    message: str
    files_changed: int


# ---------------------------------------------------------------------------
# Dependency: Token auth (Authorization: Token <secret> — NOT Bearer)
# ---------------------------------------------------------------------------

ENGINE_TOKEN: str = ""


async def verify_token(request: Request) -> None:
    """Validate Authorization: Token <secret> header."""
    auth_header = request.headers.get("authorization", "")

    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail=json.dumps({
                "error": "missing_auth",
                "detail": "Authorization header required. Use: Token <secret>",
                "status_code": 401,
            }),
        )

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "token" or not token:
        raise HTTPException(
            status_code=401,
            detail=json.dumps({
                "error": "invalid_auth_format",
                "detail": "Expected 'Authorization: Token <secret>'",
                "status_code": 401,
            }),
        )

    if token != ENGINE_TOKEN:
        raise HTTPException(
            status_code=403,
            detail=json.dumps({
                "error": "invalid_token",
                "detail": "The provided token is not valid",
                "status_code": 403,
            }),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def gpu_status_query() -> list[dict[str, Any]]:
    """Query nvidia-smi for per-GPU metrics."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,temperature.gpu,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": int(parts[0]),
                    "temperature_gpu": float(parts[1]),
                    "memory_used_mb": float(parts[2]),
                    "memory_total_mb": float(parts[3]),
                    "utilization_gpu": float(parts[4]),
                })
        return gpus
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return []


def get_model_name() -> str:
    """Attempt to read the current BeeLlama model name."""
    for port in (8080, 8082):
        try:
            result = subprocess.run(
                ["curl", "-sf", f"http://localhost:{port}/v1/models"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                models = data.get("data", [])
                if models:
                    return models[0].get("id", "unknown")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            continue
    return "unknown"


# ---------------------------------------------------------------------------
# Dangerous command blocking
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS: list[str] = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf .",
    ":(){ :|:& };:",   # fork bomb
    "mkfs",
    "dd if=",
    "dd of=",
    "> /dev/sd",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init 0",
    "init 6",
    "systemctl stop",
    "killall",
]

# CLI flag — when True, dangerous-command blocking is disabled
_allow_exec: bool = False


def is_dangerous(cmd: str) -> bool:
    """Return True if *cmd* matches a dangerous-pattern entry."""
    lower = cmd.lower().strip()
    for pat in DANGEROUS_PATTERNS:
        if pat in lower:
            return True
    return False


def is_safe_path(project_path: str, file_path: str) -> bool:
    """Reject path traversal (../) in file paths."""
    try:
        resolved_project = Path(project_path).resolve()
        resolved_file = (resolved_project / file_path).resolve()
        return str(resolved_file).startswith(str(resolved_project))
    except (ValueError, OSError):
        return False


def atomic_write(filepath: Path, data: bytes) -> None:
    """Write to a temp file in the same directory, then atomic rename."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_path, str(filepath))
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def parse_pytest_output(output: str) -> list[TestResult]:
    """Parse pytest verbose output into structured results."""
    results: list[TestResult] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("=") or line.startswith("-"):
            continue
        if " PASSED" in line:
            name = line.split(" PASSED")[0].strip()
            results.append(TestResult(name=name, status="passed"))
        elif " FAILED" in line:
            parts = line.split(" FAILED ", 1)
            name = parts[0].strip()
            detail = parts[1] if len(parts) > 1 else None
            results.append(TestResult(name=name, status="failed", detail=detail))
        elif " ERROR" in line:
            name = line.split(" ERROR")[0].strip()
            results.append(TestResult(name=name, status="error"))
        elif " SKIPPED" in line:
            name = line.split(" SKIPPED")[0].strip()
            results.append(TestResult(name=name, status="skipped"))
    return results


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Coder Harness Engine Service",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- middleware: request logging --------------------------------------------

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = time.time()
    method = request.method
    path = request.url.path
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        logger.exception("unhandled exception on %s %s", method, path)
        status_code = 500
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": str(exc),
                "status_code": 500,
            },
        )
    finally:
        duration_ms = (time.time() - start) * 1000
        logger.info(
            json.dumps({
                "method": method,
                "path": path,
                "status": status_code,
                "duration_ms": round(duration_ms, 2),
            })
        )


# -- lifecycle events ------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    lifecycle.recover()
    lifecycle.set(ServiceState.STARTING)
    logger.info("engine service starting on port %s", os.environ.get("PORT", "3082"))

    # Optionally start the in-process GPU telemetry poller
    if getattr(app.state, "with_gpu", False):
        try:
            from gpu_telemetry import GPUTelemetryPoller
            gpu_poller = GPUTelemetryPoller(
                streaming_url=getattr(app.state, "streaming_url", "http://127.0.0.1:3081"),
                streaming_token=getattr(app.state, "streaming_token", None),
                interval=getattr(app.state, "gpu_interval", 5.0),
            )
            gpu_poller.start()
            app.state.gpu_poller = gpu_poller
            logger.info("in-process GPU telemetry poller started")
        except Exception as exc:
            logger.warning("failed to start GPU telemetry poller: %s", exc)
            app.state.gpu_poller = None
    else:
        app.state.gpu_poller = None

    # Readiness probe is async — allow startup to complete first
    asyncio.get_event_loop().call_later(0.5, lambda: asyncio.ensure_future(_mark_ready()))


async def _mark_ready():
    # Quick GPU + model check before marking ready
    await asyncio.get_event_loop().run_in_executor(None, gpu_status_query)
    lifecycle.set(ServiceState.READY)


@app.on_event("shutdown")
async def on_shutdown():
    # Stop GPU telemetry poller if running
    gpu_poller = getattr(app.state, "gpu_poller", None)
    if gpu_poller is not None:
        gpu_poller.stop()
        logger.info("GPU telemetry poller stopped")

    lifecycle.set(ServiceState.STOPPED)
    logger.info("engine service shutdown complete")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# -- GET /health -----------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Service status, uptime, model info, GPU temp."""
    gpus = await asyncio.get_event_loop().run_in_executor(None, gpu_status_query)
    model = await asyncio.get_event_loop().run_in_executor(None, get_model_name)

    status_str = lifecycle.state.value
    if lifecycle.state == ServiceState.PROCESSING:
        status_str = "processing"
    elif lifecycle.state == ServiceState.READY:
        status_str = "ok"

    return HealthResponse(
        status=status_str,
        uptime=round(lifecycle.uptime(), 2),
        model=model,
        gpu_temp=gpus,
    )


# -- POST /inference -------------------------------------------------------

@app.post(
    "/inference",
    response_model=InferenceResponse,
    dependencies=[Depends(verify_token)],
)
async def inference(req: InferenceRequest):
    """Proxy chat completions to BeeLlama with 300s timeout."""
    lifecycle.set(ServiceState.PROCESSING)

    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    payload = json.dumps({
        "messages": messages,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "top_k": req.top_k,
    }).encode()

    port = req.port
    url = f"http://localhost:{port}/v1/chat/completions"

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    "curl", "-sf", "-X", "POST", url,
                    "-H", "Content-Type: application/json",
                    "-d", payload.decode(),
                    "--max-time", "300",
                ],
                capture_output=True,
                text=True,
                timeout=305,
            ),
        )
        if proc.returncode != 0:
            lifecycle.record_error()
            raise HTTPException(
                status_code=503,
                detail=json.dumps({
                    "error": "beellama_unreachable",
                    "detail": f"BeeLlama on port {port} returned error: {proc.stderr[:500]}",
                    "status_code": 503,
                }),
            )

        data = json.loads(proc.stdout)
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        reasoning = message.get("reasoning_content")
        timings_raw = data.get("timings", {})
        usage_raw = data.get("usage", {})

        lifecycle.record_success()

        return InferenceResponse(
            content=content,
            reasoning_content=reasoning,
            timings=InferenceTimings(
                prompt_per_second=timings_raw.get("prompt_per_second"),
                predicted_per_second=timings_raw.get("predicted_per_second"),
                prompt_ms=timings_raw.get("prompt_ms"),
                predicted_ms=timings_raw.get("predicted_ms"),
            ) if timings_raw else None,
            usage=InferenceUsage(
                prompt_tokens=usage_raw.get("prompt_tokens"),
                completion_tokens=usage_raw.get("completion_tokens"),
                total_tokens=usage_raw.get("total_tokens"),
            ) if usage_raw else None,
            model=data.get("model"),
        )

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        lifecycle.record_error()
        raise HTTPException(
            status_code=504,
            detail=json.dumps({
                "error": "inference_timeout",
                "detail": "Inference request exceeded 300s timeout",
                "status_code": 504,
            }),
        )
    except json.JSONDecodeError as exc:
        lifecycle.record_error()
        raise HTTPException(
            status_code=502,
            detail=json.dumps({
                "error": "invalid_beellama_response",
                "detail": f"Could not parse BeeLlama response: {exc}",
                "status_code": 502,
            }),
        )
    except Exception as exc:
        lifecycle.record_error()
        raise HTTPException(
            status_code=500,
            detail=json.dumps({
                "error": "inference_error",
                "detail": str(exc),
                "status_code": 500,
            }),
        )


# -- POST /write-files -----------------------------------------------------

@app.post(
    "/write-files",
    response_model=WriteFilesResponse,
    dependencies=[Depends(verify_token)],
)
async def write_files(req: WriteFilesRequest):
    """Write files to disk with atomic temp+rename and path traversal protection."""
    written: list[str] = []
    errors: list[dict[str, str]] = []

    project = Path(req.project_path).resolve()

    for entry in req.files:
        if not is_safe_path(req.project_path, entry.path):
            errors.append({"path": entry.path, "error": "path traversal rejected"})
            continue

        try:
            # Accept both raw text and base64-encoded content
            try:
                content_bytes = base64.b64decode(entry.content)
            except Exception:
                # Not base64 — treat as raw UTF-8 text
                content_bytes = entry.content.encode("utf-8")
        except Exception as exc:
            errors.append({"path": entry.path, "error": f"base64 decode failed: {exc}"})
            continue

        target = (project / entry.path).resolve()
        try:
            atomic_write(target, content_bytes)
            if entry.executable:
                target.chmod(target.stat().st_mode | 0o111)
            written.append(entry.path)
        except Exception as exc:
            errors.append({"path": entry.path, "error": str(exc)})

    return WriteFilesResponse(
        written=written,
        errors=errors,
        total=len(req.files),
    )


# -- POST /run-tests -------------------------------------------------------

@app.post(
    "/run-tests",
    response_model=RunTestsResponse,
    dependencies=[Depends(verify_token)],
)
async def run_tests(req: RunTestsRequest):
    """Run pytest or unittest in the project directory."""
    project = Path(req.project_path).resolve()
    if not project.is_dir():
        raise HTTPException(
            status_code=400,
            detail=json.dumps({
                "error": "invalid_project_path",
                "detail": f"Project path does not exist: {req.project_path}",
                "status_code": 400,
            }),
        )

    test_target = str(project / req.test_path) if req.test_path else str(project)

    if req.framework == "pytest":
        cmd = ["python3", "-m", "pytest", test_target, "-v", "--tb=short", "--no-header"]
        if req.pattern:
            cmd.extend(["-k", req.pattern])
    else:
        cmd = ["python3", "-m", "unittest", "discover", "-v", "-s", test_target]
        if req.pattern:
            cmd.extend(["-p", req.pattern])

    start = time.time()
    timed_out = False

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project),
        )
        output = proc.stdout + "\n" + proc.stderr
        return_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        output = "Test execution timed out after 120 seconds"
        return_code = -1

    duration = round(time.time() - start, 2)
    results = parse_pytest_output(output)

    passed = sum(1 for r in results if r.status == "passed")
    failed = sum(1 for r in results if r.status == "failed")
    errors = sum(1 for r in results if r.status == "error")
    skipped = sum(1 for r in results if r.status == "skipped")

    return RunTestsResponse(
        framework=req.framework,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        total=passed + failed + errors + skipped,
        results=results,
        output=output[-4000:] if len(output) > 4000 else output,
        timed_out=timed_out,
        return_code=return_code,
        duration=duration,
    )


# -- POST /git-commit ------------------------------------------------------

@app.post(
    "/git-commit",
    response_model=GitCommitResponse,
    dependencies=[Depends(verify_token)],
)
async def git_commit(req: GitCommitRequest):
    """Stage files and commit via git."""
    project = Path(req.project_path).resolve()
    if not project.is_dir():
        raise HTTPException(
            status_code=400,
            detail=json.dumps({
                "error": "invalid_project_path",
                "detail": f"Project path does not exist: {req.project_path}",
                "status_code": 400,
            }),
        )

    # Stage files
    if req.files:
        for f in req.files:
            proc = subprocess.run(
                ["git", "add", "--", f],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(project),
            )
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=400,
                    detail=json.dumps({
                        "error": "git_add_failed",
                        "detail": f"Failed to stage {f}: {proc.stderr[:500]}",
                        "status_code": 400,
                    }),
                )
    else:
        proc = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project),
        )
        if proc.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=json.dumps({
                    "error": "git_add_failed",
                    "detail": f"git add -A failed: {proc.stderr[:500]}",
                    "status_code": 400,
                }),
            )

    # Commit
    proc = subprocess.run(
        ["git", "commit", "-m", req.message],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(project),
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=json.dumps({
                "error": "git_commit_failed",
                "detail": proc.stderr[:1000],
                "status_code": 400,
            }),
        )

    # Get commit SHA
    sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(project),
    )
    sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else "unknown"

    # Count changed files in last commit
    count_proc = subprocess.run(
        ["git", "diff", "--stat", "HEAD~1..HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(project),
    )
    files_changed = 0
    if count_proc.returncode == 0 and count_proc.stdout.strip():
        # Parse "N files changed" from diff --stat summary
        for line in count_proc.stdout.strip().splitlines():
            if "files changed" in line:
                files_changed = int(line.split()[0])
                break

    return GitCommitResponse(
        sha=sha,
        message=req.message,
        files_changed=files_changed,
    )


# ---------------------------------------------------------------------------
# Issue #3 — Exec, GPU Status, and SSE Streaming
# ---------------------------------------------------------------------------

# -- POST /exec -----------------------------------------------------------

class ExecRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=10000)
    timeout: int = Field(default=60, ge=1, le=600)


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    blocked: bool = False


@app.post(
    "/exec",
    response_model=ExecResponse,
    dependencies=[Depends(verify_token)],
)
async def exec_command(req: ExecRequest):
    """Execute a shell command with dangerous-command blocking.

    Blocks commands matching the dangerous-pattern blocklist unless the
    service was started with --allow-exec.  Returns partial stdout/stderr
    when the command times out.
    """
    # Dangerous command check
    if is_dangerous(req.command) and not _allow_exec:
        logger.warning("blocked dangerous command: %s", req.command)
        return ExecResponse(
            stdout="",
            stderr=f"BLOCKED: command matches dangerous-pattern blocklist: {req.command}",
            returncode=126,
            blocked=True,
        )

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                req.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=req.timeout,
            ),
        )
        return ExecResponse(
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecResponse(
            stdout=exc.stdout or "" if hasattr(exc, "stdout") else "",
            stderr=exc.stderr or f"Command timed out after {req.timeout}s"
            if hasattr(exc, "stderr")
            else f"Command timed out after {req.timeout}s",
            returncode=-1,
            timed_out=True,
        )
    except Exception as exc:
        return ExecResponse(
            stdout="",
            stderr=f"exec error: {exc}",
            returncode=1,
        )


# -- GET /gpu-status ------------------------------------------------------

class GpuInfo(BaseModel):
    index: int
    temperature_gpu: float
    memory_used_mb: float
    memory_total_mb: float
    utilization_gpu: float
    name: Optional[str] = None


class GpuStatusResponse(BaseModel):
    gpus: list[GpuInfo]
    query_time: str


@app.get(
    "/gpu-status",
    response_model=GpuStatusResponse,
    dependencies=[Depends(verify_token)],
)
async def gpu_status_endpoint():
    """Return per-GPU metrics from nvidia-smi as JSON."""
    gpus_data = await asyncio.get_event_loop().run_in_executor(
        None, gpu_status_query,
    )
    return GpuStatusResponse(
        gpus=[GpuInfo(**g) for g in gpus_data],
        query_time=datetime.now(timezone.utc).isoformat(),
    )


# -- GET /gpu-snapshot (on-demand) -----------------------------------------

@app.get("/gpu-snapshot")
async def get_gpu_snapshot():
    """Return latest GPU stats on-demand (no auth required, not via SSE).

    Queries nvidia-smi directly and returns per-GPU metrics.  Returns 503
    if nvidia-smi is unavailable (e.g. local dev without a GPU).
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return JSONResponse(
                status_code=503,
                content={"error": "nvidia-smi unavailable", "detail": result.stderr[:500]},
            )
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temperature_gpu": float(parts[2]),
                    "utilization_gpu": float(parts[3]),
                    "memory_used_mb": float(parts[4]),
                    "memory_total_mb": float(parts[5]),
                })
        return {
            "gpus": gpus,
            "query_time": datetime.now(timezone.utc).isoformat(),
        }
    except FileNotFoundError:
        return JSONResponse(
            status_code=503,
            content={"error": "nvidia-smi not found on PATH"},
        )
    except subprocess.TimeoutExpired:
        return JSONResponse(
            status_code=503,
            content={"error": "nvidia-smi timed out after 10s"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"error": str(exc)},
        )


# -- GET /stream (SSE) ----------------------------------------------------

# Channels available for SSE streaming
SSE_CHANNELS = {"engine", "gpu", "all"}


@app.get("/stream")
async def stream_endpoint(
    channel: Optional[list[str]] = Query(default=None),
):
    """SSE endpoint streaming engine events.

    Heartbeat every 30 s keeps proxies and clients alive.  Optional
    ``?channel=gpu&channel=engine`` filtering is supported; ``?channel=all``
    (or omitting ``channel``) sends everything.
    """
    # Auth check — extract token from header manually (SSE can't use Depends)
    # FastAPI will still run the middleware for logging.

    requested_channels: set[str] = set()
    if channel:
        requested_channels = {c.lower() for c in channel}

    def _should_send(event_channel: str) -> bool:
        if not requested_channels or "all" in requested_channels:
            return True
        return event_channel in requested_channels

    async def event_generator():
        sent = 0
        while not shutdown_event.is_set():
            now_iso = datetime.now(timezone.utc).isoformat()
            heartbeat_payload = json.dumps({
                "type": "heartbeat",
                "uptime": round(lifecycle.uptime(), 2),
                "state": lifecycle.state.value,
                "timestamp": now_iso,
                "sequence": sent,
            })

            if _should_send("engine"):
                yield f"event: heartbeat\ndata: {heartbeat_payload}\n\n"
                sent += 1

            # Yield control and sleep in 1 s increments so we notice shutdown
            for _ in range(30):
                if shutdown_event.is_set():
                    return
                await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Graceful shutdown — SIGTERM handler
# ---------------------------------------------------------------------------

def _sigterm_handler(signum, frame):
    logger.info("received SIGTERM — initiating graceful shutdown")
    shutdown_event.set()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def check_port_available(host: str, port: int) -> bool:
    """Return True if the port is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main() -> None:
    global ENGINE_TOKEN

    parser = argparse.ArgumentParser(description="Coder Harness Engine Service")
    parser.add_argument("--port", type=int, default=3082, help="Port to listen on (default: 3082)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--token", default=None, help="Token for Authorization: Token <secret> (overrides ENGINE_TOKEN env). NOTE: scheme is Token, not Bearer.")
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Disable dangerous-command blocking for POST /exec endpoint",
    )
    parser.add_argument(
        "--with-gpu",
        action="store_true",
        default=False,
        help="Start an in-process GPU telemetry poller (emits gpu.snapshot events to streaming server)",
    )
    parser.add_argument(
        "--gpu-interval",
        type=float,
        default=5.0,
        help="GPU polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--streaming-url",
        type=str,
        default=os.environ.get("STREAMING_SERVER_URL", "http://127.0.0.1:3081"),
        help="Streaming server URL for GPU telemetry events",
    )
    parser.add_argument(
        "--streaming-token",
        type=str,
        default=os.environ.get("STREAMING_TOKEN"),
        help="Auth token for streaming server",
    )
    args = parser.parse_args()

    # Propagate --allow-exec to module-level flag
    global _allow_exec
    _allow_exec = args.allow_exec

    # Store GPU-related config on app.state for startup/shutdown handlers
    app.state.with_gpu = args.with_gpu
    app.state.gpu_interval = args.gpu_interval
    app.state.streaming_url = args.streaming_url
    app.state.streaming_token = args.streaming_token

    # Token resolution: CLI arg > env var
    ENGINE_TOKEN = args.token or os.environ.get("ENGINE_TOKEN", "")
    if not ENGINE_TOKEN:
        logger.error("no token provided. Set ENGINE_TOKEN env or use --token")
        sys.exit(1)

    # Port conflict detection
    if not check_port_available(args.host, args.port):
        logger.error(
            "port %d is already in use on %s — cannot start", args.port, args.host
        )
        sys.exit(1)

    # Register SIGTERM for graceful shutdown
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Set PORT env for logging middleware
    os.environ["PORT"] = str(args.port)

    logger.info(
        "starting engine service on %s:%d", args.host, args.port
    )

    import uvicorn

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,  # We handle logging in our middleware
    )
    server = uvicorn.Server(config)

    # Run with graceful shutdown support
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Install the SIGTERM handler into the running loop
    def _loop_sigterm():
        logger.info("SIGTERM received — shutting down uvicorn")
        server.should_exit = True

    loop.add_signal_handler(signal.SIGTERM, _loop_sigterm)

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        logger.info("interrupted — shutting down")
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("engine service stopped")


if __name__ == "__main__":
    main()
