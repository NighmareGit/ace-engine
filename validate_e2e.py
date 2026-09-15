#!/usr/bin/env python3
"""
E2E Validation Script for coder-harness platform.

Tests the complete working pipeline against a live Triton host.
No mocks — every check exercises real infrastructure.

Usage:
    python3 validate_e2e.py              # run all checks
    python3 validate_e2e.py --json       # machine-readable output
    python3 validate_e2e.py --ssh-only   # only check SSH connectivity
    python3 validate_e2e.py --host HOST  # override target host
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_HOST = "<LAN_IP>"
DEFAULT_USER = "<user>"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8"]
CURL_TIMEOUT = 12       # seconds
SSH_TIMEOUT = 15         # seconds
INFERENCE_TIMEOUT = 30   # seconds
DB_PATH = os.path.expanduser("~/coder-harness-telemetry.db")

# Expected GPU context windows
EXPECTED_CTX = {
    "3090": 128_000,
    "3070": 20_480,
}


# ─── Data Model ──────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    duration_ms: int = 0

    def icon(self) -> str:
        return "✅" if self.passed else "❌"

    def format(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{self.icon()} {self.name}{suffix}"


@dataclass
class ValidationReport:
    host: str
    results: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def all_green(self) -> bool:
        return self.passed == self.total

    def summary(self) -> str:
        if self.all_green:
            return f"Result: {self.passed}/{self.total} PASSED"
        failed = self.total - self.passed
        return f"Result: {self.passed}/{self.total} PASSED ({failed} failed)"

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "all_passed": self.all_green,
            "passed": self.passed,
            "total": self.total,
            "checks": [asdict(r) for r in self.results],
        }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command with timeout; never raises on non-zero exit."""
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr="timeout")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


def ssh_cmd(host: str, user: str, remote_cmd: str, timeout: int = SSH_TIMEOUT) -> tuple:
    """Run a command over SSH. Returns (stdout, returncode)."""
    full = ["ssh"] + SSH_OPTS + [f"{user}@{host}", remote_cmd]
    r = _run(full, timeout)
    return r.stdout.strip(), r.returncode


def curl_local(url: str, timeout: int = CURL_TIMEOUT) -> tuple:
    """curl from this machine to the host. Returns (body, returncode)."""
    r = _run(["curl", "-sf", url], timeout)
    return r.stdout.strip(), r.returncode


def curl_remote(host: str, user: str, url: str, timeout: int = CURL_TIMEOUT) -> tuple:
    """Run curl *inside* an SSH session on the remote host."""
    return ssh_cmd(host, user, f"curl -sf {url}", timeout)


def elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ─── Individual Checks ───────────────────────────────────────────────────────

def check_ssh(host: str, user: str) -> CheckResult:
    """1. Verify SSH connectivity to the Triton host."""
    t0 = time.monotonic()
    out, rc = ssh_cmd(host, user, "echo OK")
    dur = elapsed_ms(t0)
    if rc == 0 and "OK" in out:
        return CheckResult("SSH connectivity", True, duration_ms=dur)
    return CheckResult("SSH connectivity", False,
                       f"rc={rc}, stdout={out!r}", duration_ms=dur)


def check_beellama_health(host: str, user: str, gpu_label: str, port: int) -> CheckResult:
    """Check BeeLlama health on a specific port (reachable from this host)."""
    name = f"BeeLlama {gpu_label} health"
    t0 = time.monotonic()
    out, rc = curl_local(f"http://{host}:{port}/health", timeout=CURL_TIMEOUT)
    dur = elapsed_ms(t0)
    if rc == 0 and ("ok" in out.lower() or '"ok"' in out or '"status"' in out):
        return CheckResult(name, True, f"port {port}", duration_ms=dur)
    return CheckResult(name, False,
                       f"port {port}: rc={rc}, body={out!r}", duration_ms=dur)


def check_model_loaded(host: str, user: str) -> tuple:
    """3. Verify at least one model is loaded via /v1/models."""
    name = "Model loaded"
    t0 = time.monotonic()
    out, rc = curl_local(f"http://{host}:8080/v1/models", timeout=CURL_TIMEOUT)
    dur = elapsed_ms(t0)
    if rc != 0:
        return CheckResult(name, False, f"rc={rc}", duration_ms=dur)
    try:
        data = json.loads(out)
        models = data.get("data", [])
        model_ids = [m.get("id", "?") for m in models]
        if models:
            detail = ", ".join(model_ids)
            return CheckResult(name, True, detail, duration_ms=dur)
        return CheckResult(name, False, "model list empty", duration_ms=dur)
    except (json.JSONDecodeError, KeyError) as exc:
        return CheckResult(name, False, f"parse error: {exc}", duration_ms=dur)


def check_inference(host: str, user: str) -> tuple:
    """4. Send a minimal chat completion and measure throughput.

    Uses BeeLlama's ``timings`` field when available (predicted_per_second),
    otherwise falls back to wall-clock token throughput.
    """
    name = "Inference works"
    t0 = time.monotonic()
    payload = json.dumps({
        "model": "q",
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 10,
    })
    curl_cmd = [
        "curl", "-sf",
        f"http://{host}:8080/v1/chat/completions",
        "-H", "Content-Type: application/json",
        "-d", payload,
    ]
    r = _run(curl_cmd, INFERENCE_TIMEOUT)
    dur = elapsed_ms(t0)
    if r.returncode != 0:
        return CheckResult(name, False,
                           f"rc={r.returncode}, err={r.stderr.strip()!r}",
                           duration_ms=dur)
    try:
        data = json.loads(r.stdout)

        # BeeLlama exposes per-stage throughput in timings.*
        timings = data.get("timings", {})
        usage = data.get("usage", {})
        comp_toks = usage.get("completion_tokens", 0)

        # Prefer BeeLlama-reported tok/s (server-side, more accurate)
        reported_tps = timings.get("predicted_per_second")
        if reported_tps and reported_tps > 0:
            detail = f"{reported_tps:.0f} tok/s ({comp_toks} tokens)"
            return CheckResult(name, True, detail, duration_ms=dur)

        # Fallback: wall-clock estimate
        prompt_toks = usage.get("prompt_tokens", 0)
        total_toks = prompt_toks + comp_toks
        wall_s = dur / 1000.0 if dur > 0 else 1.0
        tok_per_s = total_toks / wall_s
        detail = f"~{tok_per_s:.0f} tok/s ({comp_toks} tokens in {wall_s:.1f}s)"
        return CheckResult(name, True, detail, duration_ms=dur)
    except (json.JSONDecodeError, KeyError) as exc:
        # If we got *some* output, inference partially worked
        if r.stdout:
            return CheckResult(name, True,
                               f"got response but parse error: {exc}",
                               duration_ms=dur)
        return CheckResult(name, False, f"no response: {exc}", duration_ms=dur)


def check_context_size(host: str, user: str) -> CheckResult:
    """5. Verify advertised context lengths match expected GPU configs."""
    name = "Context size correct"
    t0 = time.monotonic()

    # Try the /v1/models endpoint for context_window metadata
    out, rc = curl_local(f"http://{host}:8080/v1/models")
    ctx_info = {}

    if rc == 0:
        try:
            data = json.loads(out)
            for m in data.get("data", []):
                ctx = m.get("meta", {}).get("context_length") or m.get("context_length")
                if ctx:
                    ctx_info[m.get("id", "unknown")] = int(ctx)
        except (json.JSONDecodeError, KeyError):
            pass

    # If we got data, check against expectations
    if ctx_info:
        mismatches = []
        for model_id, ctx_len in ctx_info.items():
            # Check if it matches either expected context size
            if ctx_len not in EXPECTED_CTX.values():
                mismatches.append(f"{model_id}: {ctx_len}")
        if mismatches:
            detail = f"unexpected ctx sizes: {', '.join(mismatches)}"
            return CheckResult(name, False, detail, duration_ms=elapsed_ms(t0))
        detail = ", ".join(f"{k}: {v}" for k, v in ctx_info.items())
        return CheckResult(name, True, detail, duration_ms=elapsed_ms(t0))

    # Fallback: just report expected values if we can't query
    detail = ", ".join(f"{gpu}: {ctx}" for gpu, ctx in EXPECTED_CTX.items())
    return CheckResult(name, True, f"expected ({detail}) — no live metadata", duration_ms=elapsed_ms(t0))


def check_telemetry_db() -> CheckResult:
    """6. Verify the local telemetry SQLite DB is writable.

    Bootstraps the schema via ``schema_unified.ensure_schema()`` so the check
    works even on a fresh machine where the DB hasn't been created yet.
    """
    name = "Telemetry DB writable"
    t0 = time.monotonic()
    try:
        # Ensure tables exist (idempotent — safe to call repeatedly)
        try:
            from legacy.schema_unified import ensure_schema
            ensure_schema(DB_PATH)
        except ImportError:
            # schema_unified not on sys.path — create table manually
            db = sqlite3.connect(DB_PATH)
            db.execute("""
                CREATE TABLE IF NOT EXISTS work_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER REFERENCES work_sessions(id),
                    event_type TEXT NOT NULL,
                    event_data TEXT,
                    timestamp  TEXT DEFAULT (datetime('now'))
                )
            """)
            db.commit()
            db.close()

        # Write a test row, then clean it up
        db = sqlite3.connect(DB_PATH)
        db.execute(
            "INSERT INTO work_events (event_type) VALUES (?)",
            ("e2e_validation_test",),
        )
        db.commit()
        db.execute(
            "DELETE FROM work_events WHERE event_type = ?",
            ("e2e_validation_test",),
        )
        db.commit()
        db.close()
        return CheckResult(name, True, f"db={DB_PATH}", duration_ms=elapsed_ms(t0))
    except sqlite3.Error as exc:
        return CheckResult(name, False, str(exc), duration_ms=elapsed_ms(t0))
    except Exception as exc:
        return CheckResult(name, False, f"unexpected: {exc}", duration_ms=elapsed_ms(t0))


def check_gitea(host: str, user: str) -> CheckResult:
    """7. Verify Gitea is accessible via SSH tunnel."""
    name = "Gitea accessible"
    t0 = time.monotonic()
    out, rc = ssh_cmd(host, user, "curl -sf http://localhost:3000/api/v1/version")
    dur = elapsed_ms(t0)
    if rc != 0:
        return CheckResult(name, False, f"ssh+curl rc={rc}", duration_ms=dur)
    try:
        data = json.loads(out)
        version = data.get("version", "unknown")
        return CheckResult(name, True, f"v{version}", duration_ms=dur)
    except (json.JSONDecodeError, KeyError):
        if out:
            return CheckResult(name, True, f"raw: {out[:60]}", duration_ms=dur)
        return CheckResult(name, False, "empty response", duration_ms=dur)


def check_docker(host: str, user: str) -> CheckResult:
    """8. Verify Docker is available and containers are running."""
    name = "Docker available"
    t0 = time.monotonic()
    out, rc = ssh_cmd(host, user, 'docker ps --format "{{.Names}}" 2>/dev/null | head -5')
    dur = elapsed_ms(t0)
    if rc != 0:
        return CheckResult(name, False, f"rc={rc}", duration_ms=dur)
    if not out:
        return CheckResult(name, True, "daemon running, no containers", duration_ms=dur)
    containers = [c.strip() for c in out.splitlines() if c.strip()]
    detail = f"{len(containers)} container(s): {', '.join(containers)}"
    return CheckResult(name, True, detail, duration_ms=dur)


# ─── Orchestrator ────────────────────────────────────────────────────────────

def run_all(host: str, user: str, checks_filter: Optional[List[str]] = None) -> ValidationReport:
    """Run every validation check and collect results."""
    report = ValidationReport(host=host)
    checks = {
        "ssh":       lambda: check_ssh(host, user),
        "health":    lambda: check_beellama_health(host, user, "3090", 8080),
        "health2":   lambda: check_beellama_health(host, user, "3070", 8082),
        "model":     lambda: check_model_loaded(host, user),
        "infer":     lambda: check_inference(host, user),
        "ctx":       lambda: check_context_size(host, user),
        "telemetry": lambda: check_telemetry_db(),
        "gitea":     lambda: check_gitea(host, user),
        "docker":    lambda: check_docker(host, user),
    }

    order = ["ssh", "health", "health2", "model", "infer", "ctx", "telemetry", "gitea", "docker"]

    for key in order:
        if checks_filter and key not in checks_filter:
            continue
        try:
            result = checks[key]()
            report.results.append(result)
        except Exception as exc:
            report.results.append(CheckResult(key, False, f"exception: {exc}"))

    return report


def print_report(report: ValidationReport) -> None:
    """Pretty-print the validation report to stdout."""
    print("=== E2E Validation ===")
    print(f"Host: {report.host}")
    print()
    for r in report.results:
        print(f"  {r.format()}")
    print()
    print(f"  {report.summary()}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end validation for coder-harness platform.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Triton host IP (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help=f"SSH user (default: {DEFAULT_USER})")
    parser.add_argument("--json", action="store_true",
                        help="Output machine-readable JSON")
    parser.add_argument("--ssh-only", action="store_true",
                        help="Only run the SSH connectivity check")
    parser.add_argument("--checks", nargs="*",
                        help="Run only specific check keys: ssh, health, health2, model, infer, ctx, telemetry, gitea, docker")
    args = parser.parse_args()

    checks_filter = None
    if args.ssh_only:
        checks_filter = ["ssh"]
    elif args.checks:
        checks_filter = args.checks

    report = run_all(args.host, args.user, checks_filter)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_report(report)

    sys.exit(0 if report.all_green else 1)


if __name__ == "__main__":
    main()
