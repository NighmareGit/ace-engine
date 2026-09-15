#!/usr/bin/env python3
"""Standalone prerequisites probe for engine infrastructure.

Read-only diagnostic: prints a structured JSON report of pass/fail
for each external dependency. No fixes, no retries, no side effects.
"""

import json
import os
import subprocess
import urllib.request


def check_beellama_3090():
    """Check BeeLlama 3090 health on port 8080."""
    try:
        req = urllib.request.Request("http://localhost:8080/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {
                "name": "beellama_3090",
                "status": "pass" if data.get("status") == "ok" else "fail",
                "detail": str(data),
            }
    except Exception as e:
        return {"name": "beellama_3090", "status": "fail", "detail": str(e)}


def check_beellama_3070():
    """Check BeeLlama 3070 health on port 8082."""
    try:
        req = urllib.request.Request("http://localhost:8082/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {
                "name": "beellama_3070",
                "status": "pass" if data.get("status") == "ok" else "fail",
                "detail": str(data),
            }
    except Exception as e:
        return {"name": "beellama_3070", "status": "fail", "detail": str(e)}


def check_gitea_auth():
    """Check Gitea auth on port 3000 using GITEA_TOKEN."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return {
            "name": "gitea_auth",
            "status": "fail",
            "detail": "GITEA_TOKEN env var not set",
        }
    try:
        req = urllib.request.Request(
            "http://localhost:3000/api/v1/user",
            headers={"Authorization": f"token {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {
                "name": "gitea_auth",
                "status": "pass",
                "detail": f"authenticated as {data.get('login', '?')}",
            }
    except Exception as e:
        return {"name": "gitea_auth", "status": "fail", "detail": str(e)}


def check_engine_service():
    """Check engine_service health on port 3082."""
    try:
        req = urllib.request.Request("http://localhost:3082/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {
                "name": "engine_service",
                "status": "pass" if data.get("status") == "ok" else "fail",
                "detail": str(data),
            }
    except Exception as e:
        return {"name": "engine_service", "status": "fail", "detail": str(e)}


def check_pytest():
    """Check that pytest is installed and runnable."""
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            version_line = result.stdout.strip().split("\n")[0]
            return {"name": "pytest_installed", "status": "pass", "detail": version_line}
        return {
            "name": "pytest_installed",
            "status": "fail",
            "detail": result.stderr.strip() or "pytest not found",
        }
    except FileNotFoundError:
        return {"name": "pytest_installed", "status": "fail", "detail": "python3 not found"}
    except Exception as e:
        return {"name": "pytest_installed", "status": "fail", "detail": str(e)}


def main():
    checks = [
        check_beellama_3090(),
        check_beellama_3070(),
        check_gitea_auth(),
        check_engine_service(),
        check_pytest(),
    ]
    overall = "pass" if all(c["status"] == "pass" for c in checks) else "fail"
    report = {"overall": overall, "checks": checks}
    print(json.dumps(report))
    raise SystemExit(0 if overall == "pass" else 1)


if __name__ == "__main__":
    main()
