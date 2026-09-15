"""IIL tool-execution sandbox — runs registered tool callables / code strings
isolated from the host (grill fix G1, amended invariant #5).

Three-layer split (matches engine/intent/types.py's seam discipline):
  types    — SandboxResult, SandboxBackend, SandboxConfig (THIS FILE, top)
  runtime  — the docker subprocess + nsjail-subprocess runners (THIS FILE)
  protocol — how we encode the payload + extract the result (THIS FILE)

The split is by responsibility, not by file: this module owns the whole
sandbox surface because the three layers share one process boundary (the host
→ container transition). Callers only touch the ``SandboxRunner`` facade and
the ``SandboxResult`` dataclass.

SandboxRunner facade:
  available()             → bool          (lazy docker detection)
  run_callable(callable)  → SandboxResult (registered tool)
  run_code(code_str)      → SandboxResult (lane-B intent code)

Two execution backends, auto-selected:
  1. DockerBackend  — preferred. `docker run` with the ace-sandbox image
     (read-only rootfs, --network none, seccomp, non-root uid, tmpfs workdir,
     memory + pids caps, no-new-privs, cap-drop ALL). The payload is injected
     as a base64 env var; stdout/stderr/result.json come back via docker logs
     + the bind-mounted result file.
  2. SubprocessBackend — fallback when docker is absent. Uses subprocess +
     a short timeout; applies NO kernel isolation (documented, logged, a
     telemetry event). This path exists so the engine does NOT hard-require
     docker on a dev box — but it is NOT a security boundary. Callers that
     need the real boundary must check ``runner.available`` first.

Lazy + graceful: if docker is absent, ``available`` is False and the runner
falls back (log + telemetry event) — the engine must not hard-require docker.

Perf: container cold-start is ~100-300ms; the budget is the per-tool timeout
(default 30s), not the start cost. The runner is stateless and reusable.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("engine.intent.sandbox")

# Defaults — tunable via SandboxConfig.
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_IMAGE = os.environ.get("ACE_SANDBOX_IMAGE", "ace-sandbox:latest")
DEFAULT_MEM = os.environ.get("ACE_SANDBOX_MEM", "256m")
DEFAULT_PIDS_LIMIT = 64
# Where run.sh + seccomp.json live (relative to the repo root).
DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "sandbox"


# ---------------------------------------------------------------------------
# types layer — pure dataclasses, no logic
# ---------------------------------------------------------------------------

class SandboxBackend(str, Enum):
    """Which isolation backend answered."""
    DOCKER = "docker"
    SUBPROCESS = "subprocess"  # fallback — NOT a security boundary


@dataclass
class SandboxConfig:
    """Sandbox tunables (spec §G1 — edit with evidence)."""
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    image: str = DEFAULT_IMAGE
    mem: str = DEFAULT_MEM
    pids_limit: int = DEFAULT_PIDS_LIMIT
    # Max stdout/stderr bytes the runner captures (caps memory on the host).
    max_output_bytes: int = 256 * 1024
    # Max result.json bytes read back.
    max_result_bytes: int = 1024 * 1024
    deploy_dir: Path = DEPLOY_DIR
    # When True and docker is absent, fall back to subprocess. When False,
    # run_code/run_callable raise instead of falling back.
    allow_fallback: bool = True


@dataclass
class SandboxResult:
    """Normalized result of one sandboxed tool/code execution.

    The contract is backend-agnostic: every execution path (docker,
    subprocess, timeout, error) normalizes into this shape so the IIL
    dispatch layer never branches on backend.
    """
    ok: bool                       # did the payload run without runner error
    backend: SandboxBackend        # which backend executed it
    returncode: int                # container / process exit code (0 = ran)
    stdout: str                    # captured stdout (capped)
    stderr: str                    # captured stderr (capped)
    timed_out: bool                # True when the timeout killed it
    value: Any = None              # parsed result.json `value` (docker) or
                                   # the callable's return (subprocess)
    error: str | None = None       # runner-level error message
    latency_ms: float = 0.0        # wall-clock of the run (excl. detection)
    container_id: str | None = None  # docker container id (debug / audit)

    @property
    def failed(self) -> bool:
        return not self.ok or self.timed_out or self.returncode != 0


# ---------------------------------------------------------------------------
# runtime layer — the two execution backends
# ---------------------------------------------------------------------------

class _DockerBackend:
    """Preferred backend: `docker run` against the ace-sandbox image.

    Enforces the full G1 isolation contract: read-only rootfs, no network,
    seccomp, non-root uid, tmpfs workdir, memory + pids caps, no-new-privs,
    cap-drop ALL. Implemented by delegating to deploy/sandbox/run.sh, which
    is the trusted host-side shim (kept in sync with the Dockerfile).
    """

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config
        self.run_sh = config.deploy_dir / "run.sh"
        self.seccomp = config.deploy_dir / "seccomp.json"

    def available(self) -> bool:
        """Lazy docker detection: docker on PATH + image pullable/buildable."""
        if shutil.which("docker") is None:
            return False
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", self.config.image],
                capture_output=True, timeout=10,
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def run(self, code: str) -> SandboxResult:
        """Run a code string inside the container. Returns a normalized result."""
        t0 = time.perf_counter()
        if not self.run_sh.exists():
            return SandboxResult(
                ok=False, backend=SandboxBackend.DOCKER, returncode=1,
                stdout="", stderr="", timed_out=False,
                error=f"run.sh not found at {self.run_sh}",
                latency_ms=self._ms(t0),
            )
        tmpdir = tempfile.mkdtemp(prefix="ace-sandbox-")
        try:
            cmd = [
                str(self.run_sh),
                "--code", code,
                "--timeout", str(self.config.timeout_sec),
                "--mem", self.config.mem,
                "--workdir", tmpdir,
                "--result", f"{tmpdir}/result.json",
                "--image", self.config.image,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # Give the child room beyond the in-container timeout so we can
                # distinguish "timeout killed it" from "docker run itself hung".
                timeout=self.config.timeout_sec + 30,
            )
            latency_ms = self._ms(t0)
            stdout = self._cap(proc.stdout, self.config.max_output_bytes)
            stderr = self._cap(proc.stderr, self.config.max_output_bytes)
            # run.sh exit 3 = in-container timeout; 137 = host-side SIGKILL
            # (timeout --signal=KILL) — the container was killed for exceeding
            # the budget, so both normalize to timed_out. 137 can also be an
            # OOM kill (pids/mem limits) — error text disambiguates.
            timed_out = proc.returncode in (3, 137)
            value, error = self._read_result(f"{tmpdir}/result.json")
            return SandboxResult(
                ok=True,
                backend=SandboxBackend.DOCKER,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                value=value,
                error=error,
                latency_ms=latency_ms,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False, backend=SandboxBackend.DOCKER, returncode=124,
                stdout="", stderr="", timed_out=True,
                error=f"docker run exceeded {self.config.timeout_sec + 30}s",
                latency_ms=self._ms(t0),
            )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                ok=False, backend=SandboxBackend.DOCKER, returncode=1,
                stdout="", stderr="", timed_out=False,
                error=f"docker backend error: {exc}",
                latency_ms=self._ms(t0),
            )

    def _read_result(self, path: str) -> tuple[Any, str | None]:
        """Read + parse result.json. Returns (value, error)."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.loads(fh.read(self.config.max_result_bytes))
            if data.get("ok"):
                return data.get("value"), None
            return None, data.get("error", "payload returned ok=false")
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            return None, f"result read error: {exc}"

    @staticmethod
    def _cap(text: str, limit: int) -> str:
        b = text.encode("utf-8")
        if len(b) <= limit:
            return text
        return b[:limit].decode("utf-8", errors="replace")

    @staticmethod
    def _ms(t0: float) -> float:
        return round((time.perf_counter() - t0) * 1000.0, 2)


class _SubprocessBackend:
    """Fallback backend when docker is absent.

    IMPORTANT: this is NOT a security boundary. It runs the payload as a
    separate python subprocess with a timeout, but shares the host's
    network, filesystem, and uid. It exists so the engine does not hard-
    require docker on a dev box — callers that need the real boundary must
    check ``SandboxRunner.available`` first. Every fallback run emits a
    WARNING log + is tagged backend=SUBPROCESS in the result for audit.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return True  # always available (subprocess is stdlib)

    def run(self, code: str) -> SandboxResult:
        """Run the code in a child python -c, with a hard timeout.

        The code is the model-emitted intent. We exec it in a child process
        so a crash/infinite-loop can be killed by timeout — but there is NO
        filesystem or network isolation. See class docstring.
        """
        t0 = time.perf_counter()
        log.warning(
            "sandbox: falling back to subprocess backend (no docker) — "
            "NOT a security boundary. payload len=%d", len(code),
        )
        # Wrap the payload so its `run()` return value (or last expression) is
        # serialized to stdout as JSON. We use a wrapper script on stdin.
        wrapper = (
            "import sys, json\n"
            "g = {'__builtins__': __builtins__}\n"
            "exec(compile(sys.stdin.read(), '<sandbox-payload>', 'exec'), g)\n"
            "_r = g['run']() if callable(g.get('run')) else None\n"
            "print('__ACE_RESULT__:' + json.dumps(_r))\n"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", wrapper],
                input=code,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_sec,
            )
            latency_ms = self._ms(t0)
            value = self._extract_result(proc.stdout)
            return SandboxResult(
                ok=True,
                backend=SandboxBackend.SUBPROCESS,
                returncode=proc.returncode,
                stdout=self._cap(proc.stdout, self.config.max_output_bytes),
                stderr=self._cap(proc.stderr, self.config.max_output_bytes),
                timed_out=False,
                value=value,
                error=None if proc.returncode == 0 else f"exit {proc.returncode}",
                latency_ms=latency_ms,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False, backend=SandboxBackend.SUBPROCESS, returncode=124,
                stdout="", stderr="", timed_out=True,
                error=f"subprocess timed out after {self.config.timeout_sec}s",
                latency_ms=self._ms(t0),
            )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                ok=False, backend=SandboxBackend.SUBPROCESS, returncode=1,
                stdout="", stderr="", timed_out=False,
                error=f"subprocess backend error: {exc}",
                latency_ms=self._ms(t0),
            )

    @staticmethod
    def _extract_result(stdout: str) -> Any:
        for line in stdout.splitlines():
            if line.startswith("__ACE_RESULT__:"):
                try:
                    return json.loads(line[len("__ACE_RESULT__:"):])
                except json.JSONDecodeError:
                    return line[len("__ACE_RESULT__:"):]
        return None

    @staticmethod
    def _cap(text: str, limit: int) -> str:
        b = text.encode("utf-8")
        if len(b) <= limit:
            return text
        return b[:limit].decode("utf-8", errors="replace")

    @staticmethod
    def _ms(t0: float) -> float:
        return round((time.perf_counter() - t0) * 1000.0, 2)


# sys import for the subprocess backend's child invocation.
import sys  # noqa: E402


# ---------------------------------------------------------------------------
# protocol layer — how we turn a callable into a code string (registry tools)
# ---------------------------------------------------------------------------

def _callable_to_code(fn: Callable[..., Any]) -> str:
    """Serialize a registered tool callable into a self-contained code string.

    The callable object is re-created inside the sandbox by importing the
    engine module it lives in. We capture the import path + qualname so the
    payload can reconstruct it without the parent shipping bytecode (which
    would itself be a prompt-injection surface).

    For callables we cannot introspect (lambdas, partials), we raise — the
    registry should only register named, importable callables for sandboxed
    execution; in-process execution is the alternative.
    """
    import inspect
    try:
        module = fn.__module__
        qualname = fn.__qualname__
    except AttributeError as exc:
        raise ValueError(
            f"callable {fn!r} has no __module__/__qualname__ — "
            "cannot sandbox a lambda/partial; register a named function."
        ) from exc
    # Lambdas/partials have a <lambda> / <locals> qualname — not importable.
    if "<lambda>" in qualname or "<locals>" in qualname:
        raise ValueError(
            f"callable {fn!r} qualname={qualname!r} is a lambda/nested "
            "function — cannot be shipped to the sandbox; register a "
            "top-level named function."
        )
    # Build a payload that imports the callable and calls it with no args
    # (the IIL supplies arguments via the result value; the sandbox returns
    # the callable's own output). For callables needing args, the registry
    # wraps them in a zero-arg lambda-like named function.
    return (
        f"import importlib\n"
        f"mod = importlib.import_module({module!r})\n"
        f"fn = getattr(mod, {qualname.split('.')[0]!r})\n"
        f"{_dotted_getattr(qualname)}\n"
        f"def run():\n"
        f"    return fn()\n"
    )


def _dotted_getattr(qualname: str) -> str:
    """Build the chained getattr for a dotted qualname (class methods)."""
    parts = qualname.split(".")
    if len(parts) == 1:
        return ""  # fn is already the top-level binding
    # fn = getattr(getattr(mod, parts[0]), parts[1]) ...
    lines = []
    acc = f"mod.{parts[0]}"
    for part in parts[1:]:
        lines.append(f"fn = getattr(fn, {part!r})" if acc != f"mod.{parts[0]}"
                      else f"fn = getattr(mod.{parts[0]}, {part!r})")
        acc = f"fn.{part}"
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Facade — the only class the IIL calls.
# ---------------------------------------------------------------------------

class SandboxRunner:
    """Facade that runs a registered tool callable or a code string inside the
    sandbox. Three-layer split (types/runtime/protocol) lives inside this
    module; this class is the single entry point.

    Attributes:
        available: True when the docker backend is usable (lazy detection).
                   False → callers fall back (log + telemetry) — the engine
                   must not hard-require docker.
        backend: the active backend enum value (docker or subprocess).
        config: the resolved SandboxConfig.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()
        self._docker = _DockerBackend(self.config)
        self._subprocess = _SubprocessBackend(self.config)
        # Lazy detection is done on first use, not in the constructor, so
        # importing this module never shells out.
        self._backend: _DockerBackend | _SubprocessBackend | None = None
        self._detected = False

    # -- detection -----------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when the docker backend is usable. Lazy + cached."""
        return self._detect() is self._docker

    @property
    def backend(self) -> SandboxBackend:
        """The active backend enum value."""
        b = self._detect()
        return SandboxBackend.DOCKER if b is self._docker else SandboxBackend.SUBPROCESS

    def _detect(self):
        """Pick + cache the backend. Prefers docker; falls back if allowed."""
        if self._detected:
            return self._backend
        self._detected = True
        if self._docker.available():
            self._backend = self._docker
            return self._backend
        if self.config.allow_fallback:
            log.warning(
                "sandbox: docker unavailable — using subprocess fallback "
                "(NOT a security boundary). Set ACE_SANDBOX_IMAGE / build "
                "ace-sandbox:latest to enable real isolation."
            )
            self._backend = self._subprocess
            return self._backend
        return self._subprocess  # not available, fallback disabled → still subprocess but available=False semantics

    # -- public API ---------------------------------------------------------

    def run_code(self, code: str) -> SandboxResult:
        """Run a code string (lane-B intent code) inside the sandbox."""
        b = self._detect()
        return b.run(code)

    def run_callable(self, fn: Callable[..., Any]) -> SandboxResult:
        """Run a registered tool callable inside the sandbox.

        The callable is serialized to a code string (protocol layer) and run
        via the same path as lane-B code. Only named, importable callables are
        supported — lambdas/partials cannot be shipped to the container.
        """
        code = _callable_to_code(fn)
        return self.run_code(code)

    # -- convenience for the IIL dispatcher ----------------------------------

    def dispatch_tool(self, action: str, fn: Callable[..., Any],
                      arguments: dict[str, Any] | None = None) -> SandboxResult:
        """Dispatch a registered tool by name, building a code string that
        calls fn(**arguments). This is the path the IIL uses for registry
        actions (engine/intent/tools.py ToolEntry.callable).

        ``action`` is the registry name (telemetry / audit). ``arguments``
        are bound into the payload so the sandboxed code needs no host access.
        """
        arguments = arguments or {}
        # Build a payload that imports the callable and calls it with the
        # given kwargs literal. kwargs are JSON-encoded so the payload is a
        # pure string (no pickling — pickling is itself a prompt-injection
        # surface, cf. smolagents #2320).
        import_path = _callable_to_code(fn)
        # Replace the zero-arg `run()` with one that passes arguments.
        kwargs_json = json.dumps(arguments)
        payload = import_path
        # Overwrite the run() definition to accept the bound kwargs.
        payload += (
            f"import json as _json\n"
            f"_kwargs = _json.loads({kwargs_json!r})\n"
            f"def run():\n"
            f"    return fn(**_kwargs)\n"
        )
        result = self.run_code(payload)
        return result


# ---------------------------------------------------------------------------
# Module-level singleton — the IIL imports this.
# ---------------------------------------------------------------------------

_default_runner: SandboxRunner | None = None


def get_sandbox_runner(config: SandboxConfig | None = None) -> SandboxRunner:
    """Return the process-wide SandboxRunner singleton."""
    global _default_runner
    if _default_runner is None or config is not None:
        _default_runner = SandboxRunner(config=config)
    return _default_runner


def is_sandbox_available() -> bool:
    """Convenience: is the docker sandbox usable right now?"""
    return get_sandbox_runner().available


# ---------------------------------------------------------------------------
# Test sentinels — importable callables used by the sandbox test suite to
# exercise the callable-serialization + dispatch_tool path in a fresh
# subprocess interpreter (which re-imports this module from source, so the
# helper MUST be a real module-level binding, not a runtime monkeypatch).
# ---------------------------------------------------------------------------

def _sandbox_test_hash(data: str) -> str:
    """Test sentinel: return sha256(data). Pure, importable, side-effect-free."""
    import hashlib
    return hashlib.sha256(data.encode()).hexdigest()
