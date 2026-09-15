"""Opt-in Docker sandbox helpers for the engine.

Phase-1 rule: all functions are inert (no-ops) unless ``EngineConfig.sandbox``
is ``True``.  This keeps the engine fully functional without Docker installed.
"""

import os
from dataclasses import dataclass


@dataclass
class SandboxConfig:
    """Docker sandbox configuration."""
    image: str = "ubuntu:24.04"
    prefix: str = "ace-sandbox"
    network_mode: str = "host"
    timeout_create: int = 120
    timeout_exec: int = 300


def create_sandbox(project_path, transport, config=None):
    """Create a Docker sandbox for isolated code execution.

    Phase-1 rule: returns None immediately if config.sandbox is False.
    Returns container name on success, None on failure or inert mode.
    """
    if config is None or not getattr(config, "sandbox", False):
        return None  # inert — no Docker sandbox in Phase 1

    import time
    sandbox_cfg = SandboxConfig()
    container_name = f"{sandbox_cfg.prefix}-{int(time.time())}"

    # Create container with project volume-mounted
    cmd = (
        f"docker run -d --rm "
        f"--network {sandbox_cfg.network_mode} "
        f"--name {container_name} "
        f"-v {project_path}:/workspace:rw "
        f"{sandbox_cfg.image} sleep infinity"
    )
    stdout, stderr, rc = transport.run_command(cmd, timeout=sandbox_cfg.timeout_create)
    if rc != 0:
        return None
    return container_name


def exec_in_sandbox(container_name, command, transport, config=None, timeout=300):
    """Execute a command inside a Docker sandbox container.

    Phase-1 rule: returns inert result immediately if config.sandbox is False.
    Returns (stdout, stderr, rc) tuple.
    """
    if config is None or not getattr(config, "sandbox", False):
        return ("", "Docker sandbox disabled (Phase 1)", 0)  # inert

    stdout, stderr, rc = transport.run_command(
        f"docker exec {container_name} {command}", timeout=timeout)
    return (stdout, stderr, rc)


def destroy_sandbox(container_name, transport, config=None):
    """Force-remove a Docker sandbox container.

    Phase-1 rule: returns True immediately if config.sandbox is False.
    Returns True on success or inert mode.
    """
    if config is None or not getattr(config, "sandbox", False):
        return True  # inert

    transport.run_command(f"docker rm -f {container_name}", timeout=30)
    return True


def cleanup_all_sandboxes(transport, config=None):
    """Destroy all containers with the ace-sandbox prefix.

    Phase-1 rule: returns 0 immediately if config.sandbox is False.
    Returns number of containers destroyed.
    """
    if config is None or not getattr(config, "sandbox", False):
        return 0  # inert

    stdout, _, _ = transport.run_command(
        "docker ps -q --filter name=ace-sandbox", timeout=30)
    count = 0
    for container_id in stdout.strip().split("\n"):
        if container_id.strip():
            transport.run_command(f"docker rm -f {container_id}", timeout=30)
            count += 1
    return count


def sandbox_status(transport, config=None):
    """List active ace-sandbox containers.

    Phase-1 rule: returns empty list if config.sandbox is False.
    Returns list of container name strings.
    """
    if config is None or not getattr(config, "sandbox", False):
        return []  # inert

    stdout, _, _ = transport.run_command(
        "docker ps --filter name=ace-sandbox --format '{{.Names}}'", timeout=30)
    return [name.strip() for name in stdout.strip().split("\n") if name.strip()]
