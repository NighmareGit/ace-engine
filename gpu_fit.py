#!/usr/bin/env python3
"""GPU fit matrix probe — tests which model configs fit at which context sizes.

For each (model_config, context_size) pair this probe:
  1. SSHes into Triton
  2. Rewrites the beellama .env with the target model path + context size
  3. Restarts the docker-compose stack
  4. Polls /health until the endpoint is ready (or times out)
  5. Sends one lightweight inference request
  6. Reads nvidia-smi for VRAM usage
  7. Persists the row to gpu_fit_matrix in SQLite

Usage:
    python3 gpu_fit.py --dry-run                  # plan only
    python3 gpu_fit.py --config 3090-qwen36-35b   # single config
    python3 gpu_fit.py --sizes 4096,8192           # specific sizes
    python3 gpu_fit.py                             # full matrix
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(SCRIPT_DIR, "models", "manifest.json")
DB_PATH = os.path.join(SCRIPT_DIR, "benchmark-results.db")
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "schema.sql")

# Default context sizes to sweep (from small to large)
DEFAULT_CONTEXT_SIZES = [4096, 8192, 32768, 65536, 131072]

# Triton docker-compose paths
DOCKER_COMPOSE_3090 = "/home/<user>/dockers/beellama-kvarn/docker-compose.yml"
DOCKER_COMPOSE_3070 = "/home/<user>/dockers/beellama-kvarn-3070/docker-compose.yml"
ENV_PATH_3090 = "/home/<user>/dockers/beellama-kvarn/.env"
ENV_PATH_3070 = "/home/<user>/dockers/beellama-kvarn-3070/.env"

# Health-check settings
HEALTH_POLL_INTERVAL = 5   # seconds between polls
HEALTH_TIMEOUT = 180       # max seconds to wait for healthy

# Simple inference probe prompt
PROBE_PROMPT = "Say hello in one word."
PROBE_MAX_TOKENS = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_paths_for_gpu(gpu: str):
    """Return (compose_file, env_file) for the given GPU identifier."""
    if gpu.startswith("3070"):
        return DOCKER_COMPOSE_3070, ENV_PATH_3070
    # Default to 3090 stack
    return DOCKER_COMPOSE_3090, ENV_PATH_3090


def _env_var_name_for(key: str, gpu: str) -> str:
    """Map a .env key to the correct variable name for the compose stack.

    Known beellama .env patterns:
      MODEL_PATH=...          or  MODEL_3070_PATH=...
      CONTEXT_SIZE=...        or  CONTEXT_3070_SIZE=...
    We normalise by always reading/writing the canonical names and letting
    the compose file reference whatever it references.
    """
    return key


class GPUFitProbe:
    """Tests which model / context-size combinations fit in GPU VRAM."""

    def __init__(self, db_path=None, ssh_client=None):
        self.db_path = db_path or DB_PATH
        self.ssh = ssh_client
        self.manifest = self._load_manifest()
        self._init_db()

    # -- manifest & DB -----------------------------------------------------

    def _load_manifest(self):
        with open(MANIFEST_PATH) as f:
            return json.load(f)["configs"]

    def _init_db(self):
        """Create tables from schema.sql if they don't exist yet."""
        conn = sqlite3.connect(self.db_path)
        try:
            with open(SCHEMA_PATH) as f:
                conn.executescript(f.read())
        except sqlite3.OperationalError:
            # Tables already exist — fine
            pass
        conn.close()

    def _get_config(self, config_id: str):
        """Look up a single config dict from the manifest."""
        for c in self.manifest:
            if c["id"] == config_id:
                return c
        raise ValueError(f"Config '{config_id}' not found in manifest")

    # -- SSH helpers -------------------------------------------------------

    def _ssh_run(self, cmd: str, timeout: int = 30):
        """Run a command on Triton via SSH. Returns (stdout, stderr, rc)."""
        if self.ssh is None:
            raise ConnectionError("No SSH client provided")
        return self.ssh.run(cmd, timeout=timeout)

    # -- beellama lifecycle ------------------------------------------------

    def _write_env(self, config: dict, context_size: int):
        """Rewrite the beellama .env on Triton for the given config + ctx."""
        _, env_file = _docker_paths_for_gpu(config["gpu"])

        # Read the current .env so we only touch the fields we care about
        stdout, stderr, rc = self._ssh_run(f"cat {env_file}")
        if rc != 0:
            raise RuntimeError(f"Failed to read {env_file}: {stderr}")

        env_lines = stdout.splitlines()
        new_lines = []
        keys_seen = set()

        for line in env_lines:
            stripped = line.strip()
            # Skip blank lines and comments but preserve them
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            key = stripped.split("=", 1)[0].strip()
            keys_seen.add(key)

            # Update known keys
            if key.upper() in ("MODEL_PATH", "MODEL"):
                new_lines.append(f'{key}="{config["model_path"]}"')
            elif key.upper() in ("CONTEXT_SIZE", "N_CTX", "N_CTX法律规定", "CTX_SIZE"):
                new_lines.append(f"{key}={context_size}")
            elif key.upper() in ("DRAFT_MODEL", "DRAFT"):
                draft = config.get("draft_model") or ""
                new_lines.append(f'{key}="{draft}"')
            else:
                new_lines.append(line)

        # Append keys that were missing
        if "MODEL_PATH" not in keys_seen and "MODEL" not in keys_seen:
            new_lines.append(f'MODEL_PATH="{config["model_path"]}"')
        if "CONTEXT_SIZE" not in keys_seen and "N_CTX" not in keys_seen:
            new_lines.append(f"CONTEXT_SIZE={context_size}")

        new_env = "\n".join(new_lines) + "\n"

        # Write via heredoc (avoids quoting hell)
        escaped = new_env.replace("\\", "\\\\").replace("$", "\\$")
        write_cmd = f"cat > {env_file} << 'DSH_ENVEOF'\n{new_env}DSH_ENVEOF"
        # Use a temp file approach instead for safety
        tmp = f"/tmp/dsh_env_{os.getpid()}.env"
        # Write locally, scp over
        local_tmp = f"/tmp/dsh_env_{os.getpid()}.env"
        with open(local_tmp, "w") as f:
            f.write(new_env)

        if self.ssh:
            import subprocess
            scp_cmd = [
                "sshpass", "-p", self.ssh.pw,
                "scp", "-o", "StrictHostKeyChecking=no",
                local_tmp,
                f"{self.ssh.user}@{self.ssh.host}:{env_file}",
            ]
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=15)
            os.unlink(local_tmp)
            if result.returncode != 0:
                raise RuntimeError(f"SCP env file failed: {result.stderr}")
        else:
            os.unlink(local_tmp)

    def _restart_compose(self, config: dict):
        """Restart the docker-compose stack for this config's GPU."""
        compose_file, _ = _docker_paths_for_gpu(config["gpu"])
        cmd = f"docker compose -f {compose_file} restart"
        stdout, stderr, rc = self._ssh_run(cmd, timeout=120)
        if rc != 0:
            raise RuntimeError(f"docker compose restart failed (rc={rc}): {stderr}")
        # Give containers a moment to begin their startup sequence
        time.sleep(3)

    def _wait_for_health(self, port: int, timeout: int = HEALTH_TIMEOUT) -> bool:
        """Poll /health until healthy or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ssh.check_beellama_health(port):
                return True
            time.sleep(HEALTH_POLL_INTERVAL)
        return False

    # -- probe core --------------------------------------------------------

    def probe_one(self, config_id: str, context_size: int, dry_run: bool = False):
        """Probe one model config at one context size.

        Returns a dict ready to be inserted into gpu_fit_matrix.
        """
        config = self._get_config(config_id)
        gpu = config["gpu"]
        port = config["port"]

        base = {
            "model_config_id": config_id,
            "gpu": gpu,
            "context_size": context_size,
            "fits": None,
            "vram_used_mb": None,
            "vram_total_mb": None,
            "inference_ok": None,
            "inference_tokens_per_sec": None,
            "error_message": None,
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }

        if dry_run:
            base["error_message"] = "dry_run"
            return base

        # Step 1: write .env
        print(f"    Writing .env for {config_id} ctx={context_size} ...")
        try:
            self._write_env(config, context_size)
        except Exception as e:
            base["error_message"] = f"env_write_failed: {e}"
            return base

        # Step 2: restart compose
        print(f"    Restarting docker-compose (gpu={gpu}) ...")
        try:
            self._restart_compose(config)
        except Exception as e:
            base["error_message"] = f"compose_restart_failed: {e}"
            return base

        # Step 3: wait for healthy
        print(f"    Waiting for /health on port {port} (timeout {HEALTH_TIMEOUT}s) ...")
        healthy = self._wait_for_health(port)
        if not healthy:
            base["fits"] = 0
            base["inference_ok"] = 0
            base["error_message"] = "health_timeout"
            return base

        # Step 4: run inference
        print(f"    Running inference probe ...")
        try:
            result = self.ssh.curl_beellama(
                port=port,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
                max_tokens=PROBE_MAX_TOKENS,
                temperature=0.1,
            )
            inference_ok = True
            tps = result.get("predicted_per_second", 0.0)
        except Exception as e:
            inference_ok = False
            tps = 0.0
            base["error_message"] = f"inference_failed: {e}"

        # Step 5: capture VRAM
        print(f"    Capturing nvidia-smi ...")
        gpu_index = int(re.search(r"\d+", gpu).group()) if re.search(r"\d+", gpu) else None
        try:
            smi = self.ssh.get_nvidia_smi(gpu_index=gpu_index)
            if smi:
                base["vram_used_mb"] = smi.get("memory_used_mb")
                base["vram_total_mb"] = smi.get("memory_total_mb")
        except Exception as e:
            base["error_message"] = (base["error_message"] or "") + f" | smi_failed: {e}"

        base["fits"] = 1 if inference_ok else 0
        base["inference_ok"] = 1 if inference_ok else 0
        base["inference_tokens_per_sec"] = tps

        return base

    # -- sweep -------------------------------------------------------------

    def probe_all(self, config_ids=None, context_sizes=None, dry_run: bool = False):
        """Probe all (config × context_size) combinations.

        Returns list of result dicts.
        """
        if config_ids:
            configs = [self._get_config(cid) for cid in config_ids]
        else:
            configs = list(self.manifest)

        sizes = context_sizes or DEFAULT_CONTEXT_SIZES
        results = []

        total = len(configs) * len(sizes)
        idx = 0
        for config in configs:
            for ctx in sizes:
                idx += 1
                print(f"[{idx}/{total}] Probing {config['id']} @ {ctx} ctx ...")
                result = self.probe_one(config["id"], ctx, dry_run=dry_run)
                results.append(result)

                if not dry_run:
                    self._save_result(result)

                # Brief cooldown between restarts to let GPU settle
                if not dry_run and idx < total:
                    print("    Cooling down 5s ...")
                    time.sleep(5)

        return results

    # -- persistence -------------------------------------------------------

    def _save_result(self, row: dict):
        """Upsert one probe result into gpu_fit_matrix."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO gpu_fit_matrix
                (model_config_id, gpu, context_size, fits, vram_used_mb,
                 vram_total_mb, inference_ok, inference_tokens_per_sec,
                 error_message, measured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["model_config_id"],
                row["gpu"],
                row["context_size"],
                row["fits"],
                row["vram_used_mb"],
                row["vram_total_mb"],
                row["inference_ok"],
                row["inference_tokens_per_sec"],
                row["error_message"],
                row["measured_at"],
            ),
        )
        conn.commit()
        conn.close()

    # -- reporting ---------------------------------------------------------

    def print_summary(self, results=None):
        """Print a formatted fit-matrix table to stdout."""
        if results is None:
            results = self._load_from_db()

        if not results:
            print("\nNo results to display.")
            return

        # Collect unique values
        configs_seen = []
        sizes_seen = []
        gpu_map = {}
        for r in results:
            cid = r["model_config_id"]
            ctx = r["context_size"]
            if cid not in configs_seen:
                configs_seen.append(cid)
            if ctx not in sizes_seen:
                sizes_seen.append(ctx)
            gpu_map[cid] = r.get("gpu", "?")

        sizes_seen.sort()

        # Build lookup
        lookup = {}
        for r in results:
            key = (r["model_config_id"], r["context_size"])
            lookup[key] = r

        # Header
        col_w = 12
        name_w = 24
        hdr = f"{'Config':<{name_w}} {'GPU':>4}"
        for ctx in sizes_seen:
            hdr += f" {ctx // 1024}K".rjust(col_w)
        sep = "-" * len(hdr)

        print(f"\n{'=' * len(hdr)}")
        print(" GPU FIT MATRIX")
        print(f"{'=' * len(hdr)}")
        print(hdr)
        print(sep)

        for cid in configs_seen:
            row_str = f"{cid:<{name_w}} {gpu_map.get(cid, '?'):>4}"
            for ctx in sizes_seen:
                r = lookup.get((cid, ctx))
                if r is None:
                    cell = "  —"
                elif r["error_message"] == "dry_run":
                    cell = "  ?"
                elif r["fits"] == 1:
                    vram = f"{r['vram_used_mb']:.0f}" if r.get("vram_used_mb") else ""
                    cell = f" ✅{vram}"
                elif r["fits"] == 0:
                    err = (r.get("error_message") or "")[:8]
                    cell = f" ❌{err}"
                else:
                    cell = "  ?"
                row_str += cell.rjust(col_w)
            print(row_str)

        print(sep)

        # VRAM detail
        print(f"\n{'VRAM Detail':=^{len(hdr)}}")
        for cid in configs_seen:
            for ctx in sizes_seen:
                r = lookup.get((cid, ctx))
                if r and r.get("vram_used_mb"):
                    tps = r.get("inference_tokens_per_sec") or 0
                    print(
                        f"  {cid} @ {ctx//1024}K  →  "
                        f"VRAM {r['vram_used_mb']:.0f}/{r['vram_total_mb']:.0f} MB  "
                        f"  {tps:.1f} tok/s"
                    )
        print()

    def _load_from_db(self):
        """Load all existing results from gpu_fit_matrix."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM gpu_fit_matrix ORDER BY model_config_id, context_size"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="GPU Fit Matrix Probe — tests model × context-size combinations"
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Single config ID to test (default: all configs in manifest)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be tested without restarting containers",
    )
    parser.add_argument(
        "--sizes", type=str, default=None,
        help="Comma-separated context sizes to test (default: 4096,8192,32768,65536,131072)",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print existing results from the database and exit",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Override database path",
    )
    args = parser.parse_args()

    probe = GPUFitProbe(db_path=args.db)

    # --show: just display stored results
    if args.show:
        probe.print_summary()
        return

    # Parse context sizes
    if args.sizes:
        sizes = [int(s.strip()) for s in args.sizes.split(",")]
    else:
        sizes = DEFAULT_CONTEXT_SIZES

    config_ids = [args.config] if args.config else None

    # Optionally connect SSH (skip for dry-run / --show)
    if not args.dry_run:
        from ssh_utils import SSHClient

        ssh = SSHClient()
        print("Connecting to Triton ...")
        if not ssh.connect():
            print("ERROR: Could not connect to Triton via SSH.", file=sys.stderr)
            sys.exit(1)
        print("Connected.\n")
        probe.ssh = ssh
    else:
        print("=== DRY RUN — no SSH, no restarts ===\n")

    # Run the sweep
    results = probe.probe_all(
        config_ids=config_ids, context_sizes=sizes, dry_run=args.dry_run
    )

    # Summary
    probe.print_summary(results)

    if not args.dry_run:
        print(f"Results saved to {probe.db_path}")
    else:
        print("(Dry run — nothing persisted)")


if __name__ == "__main__":
    main()
