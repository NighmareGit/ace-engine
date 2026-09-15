"""Telemetry configuration for BeeLlama inference engine monitoring.

Centralizes all config: poll intervals, endpoints, storage paths, alert thresholds.
Supports YAML/JSON config file loading and environment variable overrides.

Usage:
    from telemetry_config import TelemetryConfig
    config = TelemetryConfig()  # loads defaults
    config = TelemetryConfig.from_file("telemetry.yaml")  # from file
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional YAML support
# ---------------------------------------------------------------------------
try:
    import yaml

    HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

# ---------------------------------------------------------------------------
# Module-level default config dict (for quick serialization / comparison)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict[str, Any] = {
    "endpoints": {
        "base_url": "http://localhost:8080",
        "health_path": "/health",
        "chat_path": "/v1/chat/completions",
        "models_path": "/v1/models",
        "slots_path": "/slots",
        "metrics_path": "/metrics",
        "telemetry_path": "/telemetry",
        "base_url_3070": "http://localhost:8082",
    },
    "collection": {
        "poll_interval_seconds": 5,
        "inference_sample_rate": 1.0,
        "slot_poll_interval_seconds": 2,
        "cache_snapshot_interval_seconds": 30,
        "max_traces_retained": 10000,
        "max_snapshots_retained": 50000,
        "max_events_retained": 100000,
    },
    "storage": {
        "db_path": "",
        "wal_mode": True,
        "vacuum_interval_hours": 24,
    },
    "alerts": {
        "temperature_warning_c": 75.0,
        "temperature_critical_c": 83.0,
        "vram_pressure_pct": 90.0,
        "throughput_degradation_pct": 30.0,
        "stall_duration_ms": 100.0,
        "draft_acceptance_min": 0.3,
        "cache_hit_rate_min": 0.1,
        "power_limit_pct": 95.0,
    },
    "ssh": {
        "host": "<LAN_IP>",
        "user": "<user>",
        "timeout_seconds": 10,
        "key_check": "no",
    },
    "dashboard": {
        "output_dir": "",
        "refresh_interval_seconds": 60,
        "dark_theme": True,
        "chart_width": 1200,
        "chart_height": 400,
    },
    "verbose": False,
    "log_file": "",
}


# ===================================================================
# Dataclass definitions
# ===================================================================


@dataclass
class BeeLlamaEndpoints:
    """BeeLlama API endpoints configuration."""

    base_url: str = "http://localhost:8080"
    health_path: str = "/health"
    chat_path: str = "/v1/chat/completions"
    models_path: str = "/v1/models"
    slots_path: str = "/slots"
    metrics_path: str = "/metrics"  # Prometheus
    telemetry_path: str = "/telemetry"  # future custom endpoint

    # 3070 endpoint
    base_url_3070: str = "http://localhost:8082"


@dataclass
class CollectionConfig:
    """Telemetry collection parameters."""

    poll_interval_seconds: int = 5  # GPU snapshot interval
    inference_sample_rate: float = 1.0  # 1.0 = collect every request
    slot_poll_interval_seconds: int = 2  # /slots polling
    cache_snapshot_interval_seconds: int = 30  # cache efficiency aggregation
    max_traces_retained: int = 10000  # auto-cleanup old traces
    max_snapshots_retained: int = 50000
    max_events_retained: int = 100000


@dataclass
class StorageConfig:
    """SQLite storage configuration."""

    db_path: str = ""  # defaults to ~/coder-harness-telemetry.db
    wal_mode: bool = True
    vacuum_interval_hours: int = 24

    def __post_init__(self) -> None:
        if not self.db_path:
            self.db_path = os.path.expanduser("~/coder-harness-telemetry.db")


@dataclass
class AlertThresholds:
    """Thresholds for telemetry alerts."""

    temperature_warning_c: float = 75.0
    temperature_critical_c: float = 83.0
    vram_pressure_pct: float = 90.0
    throughput_degradation_pct: float = 30.0  # % below baseline
    stall_duration_ms: float = 100.0
    draft_acceptance_min: float = 0.3
    cache_hit_rate_min: float = 0.1
    power_limit_pct: float = 95.0  # % of power limit


@dataclass
class SSHConfig:
    """SSH connection to Triton."""

    host: str = "<LAN_IP>"
    user: str = "<user>"
    timeout_seconds: int = 10
    key_check: str = "no"  # StrictHostKeyChecking


@dataclass
class DashboardConfig:
    """Dashboard generation settings."""

    output_dir: str = ""  # defaults to coder-harness/reports/
    refresh_interval_seconds: int = 60
    dark_theme: bool = True
    chart_width: int = 1200
    chart_height: int = 400

    def __post_init__(self) -> None:
        if not self.output_dir:
            self.output_dir = os.path.join(os.path.dirname(__file__), "reports")


@dataclass
class TelemetryConfig:
    """Master configuration for the telemetry system."""

    endpoints: BeeLlamaEndpoints = field(default_factory=BeeLlamaEndpoints)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    alerts: AlertThresholds = field(default_factory=AlertThresholds)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    verbose: bool = False
    log_file: str = ""

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation of the full config."""
        return {
            "endpoints": asdict(self.endpoints),
            "collection": asdict(self.collection),
            "storage": asdict(self.storage),
            "alerts": asdict(self.alerts),
            "ssh": asdict(self.ssh),
            "dashboard": asdict(self.dashboard),
            "verbose": self.verbose,
            "log_file": self.log_file,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TelemetryConfig:
        """Reconstruct a ``TelemetryConfig`` from a plain dictionary.

        Unknown keys are silently ignored so forward/backward compatibility
        is maintained when loading old config files.
        """
        endpoints_data = d.get("endpoints", {})
        collection_data = d.get("collection", {})
        storage_data = d.get("storage", {})
        alerts_data = d.get("alerts", {})
        ssh_data = d.get("ssh", {})
        dashboard_data = d.get("dashboard", {})

        return cls(
            endpoints=BeeLlamaEndpoints(**{
                k: v
                for k, v in endpoints_data.items()
                if k in BeeLlamaEndpoints.__dataclass_fields__
            }),
            collection=CollectionConfig(**{
                k: v
                for k, v in collection_data.items()
                if k in CollectionConfig.__dataclass_fields__
            }),
            storage=StorageConfig(**{
                k: v
                for k, v in storage_data.items()
                if k in StorageConfig.__dataclass_fields__
            }),
            alerts=AlertThresholds(**{
                k: v
                for k, v in alerts_data.items()
                if k in AlertThresholds.__dataclass_fields__
            }),
            ssh=SSHConfig(**{
                k: v
                for k, v in ssh_data.items()
                if k in SSHConfig.__dataclass_fields__
            }),
            dashboard=DashboardConfig(**{
                k: v
                for k, v in dashboard_data.items()
                if k in DashboardConfig.__dataclass_fields__
            }),
            verbose=d.get("verbose", False),
            log_file=d.get("log_file", ""),
        )

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str) -> TelemetryConfig:
        """Load config from a JSON or YAML file.

        YAML requires the ``pyyaml`` package to be installed.  When YAML
        support is unavailable and the file extension is ``.yaml`` / ``.yml``
        a ``RuntimeError`` is raised.
        """
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"Config file not found: {file_path}")

        text = file_path.read_text(encoding="utf-8")
        suffix = file_path.suffix.lower()

        if suffix in (".yaml", ".yml"):
            if not HAS_YAML:
                raise RuntimeError(
                    "YAML config requested but PyYAML is not installed. "
                    "Install it with: pip install pyyaml"
                )
            data = yaml.safe_load(text)
        else:
            # Default to JSON for .json and any other extension
            data = json.loads(text)

        if not isinstance(data, dict):
            raise ValueError(
                f"Config file must contain a YAML/JSON mapping at the top level, "
                f"got {type(data).__name__}"
            )

        logger.info("Loaded telemetry config from %s", file_path)
        return cls.from_dict(data)

    def to_file(self, path: str) -> None:
        """Save config to a JSON file.

        If the path ends with ``.yaml`` or ``.yml`` and PyYAML is installed,
        YAML format is used instead.
        """
        file_path = Path(path).expanduser().resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)

        data = self.to_dict()
        suffix = file_path.suffix.lower()

        if suffix in (".yaml", ".yml"):
            if not HAS_YAML:
                raise RuntimeError(
                    "YAML output requested but PyYAML is not installed. "
                    "Install it with: pip install pyyaml"
                )
            file_path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
        else:
            file_path.write_text(
                json.dumps(data, indent=2, sort_keys=False) + "\n",
                encoding="utf-8",
            )

        logger.info("Saved telemetry config to %s", file_path)

    # ------------------------------------------------------------------
    # Environment variable overrides
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> TelemetryConfig:
        """Override defaults from environment variables.

        Supported prefixes:

        * ``TELEMETRY_*``  — general telemetry settings
        * ``BEE_LLAMA_*``  — BeeLlama endpoint overrides

        Mapping examples::

            TELEMETRY_POLL_INTERVAL=10        -> collection.poll_interval_seconds
            TELEMETRY_DB_PATH=/tmp/test.db     -> storage.db_path
            TELEMETRY_VERBOSE=1                -> verbose
            TELEMETRY_LOG_FILE=/tmp/t.log      -> log_file
            BEE_LLAMA_BASE_URL=http://h:80     -> endpoints.base_url
            BEE_LLAMA_BASE_URL_3070=http://h:83 -> endpoints.base_url_3070
            SSH_HOST=host                    -> ssh.host
            SSH_USER=root                      -> ssh.user
        """
        cfg = cls()  # start from defaults

        def _env(key: str, default: Any = None) -> str | None:
            return os.environ.get(key, default)

        def _env_int(key: str, default: int) -> int:
            val = os.environ.get(key)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    logger.warning("Invalid int for %s: %s", key, val)
            return default

        def _env_float(key: str, default: float) -> float:
            val = os.environ.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    logger.warning("Invalid float for %s: %s", key, val)
            return default

        def _env_bool(key: str, default: bool) -> bool:
            val = os.environ.get(key)
            if val is not None:
                return val.lower() in ("1", "true", "yes", "on")
            return default

        # -- endpoints --
        base = cfg.endpoints
        base.base_url = _env("BEE_LLAMA_BASE_URL", base.base_url) or base.base_url
        base.base_url_3070 = _env("BEE_LLAMA_BASE_URL_3070", base.base_url_3070) or base.base_url_3070
        base.health_path = _env("BEE_LLAMA_HEALTH_PATH", base.health_path) or base.health_path
        base.chat_path = _env("BEE_LLAMA_CHAT_PATH", base.chat_path) or base.chat_path
        base.models_path = _env("BEE_LLAMA_MODELS_PATH", base.models_path) or base.models_path
        base.slots_path = _env("BEE_LLAMA_SLOTS_PATH", base.slots_path) or base.slots_path
        base.metrics_path = _env("BEE_LLAMA_METRICS_PATH", base.metrics_path) or base.metrics_path
        base.telemetry_path = _env("BEE_LLAMA_TELEMETRY_PATH", base.telemetry_path) or base.telemetry_path

        # -- collection --
        col = cfg.collection
        col.poll_interval_seconds = _env_int("TELEMETRY_POLL_INTERVAL", col.poll_interval_seconds)
        col.inference_sample_rate = _env_float(
            "TELEMETRY_INFERENCE_SAMPLE_RATE", col.inference_sample_rate
        )
        col.slot_poll_interval_seconds = _env_int(
            "TELEMETRY_SLOT_POLL_INTERVAL", col.slot_poll_interval_seconds
        )
        col.cache_snapshot_interval_seconds = _env_int(
            "TELEMETRY_CACHE_SNAPSHOT_INTERVAL", col.cache_snapshot_interval_seconds
        )
        col.max_traces_retained = _env_int("TELEMETRY_MAX_TRACES", col.max_traces_retained)
        col.max_snapshots_retained = _env_int("TELEMETRY_MAX_SNAPSHOTS", col.max_snapshots_retained)
        col.max_events_retained = _env_int("TELEMETRY_MAX_EVENTS", col.max_events_retained)

        # -- storage --
        sto = cfg.storage
        sto.db_path = _env("TELEMETRY_DB_PATH", sto.db_path) or sto.db_path
        sto.wal_mode = _env_bool("TELEMETRY_WAL_MODE", sto.wal_mode)
        sto.vacuum_interval_hours = _env_int(
            "TELEMETRY_VACUUM_INTERVAL_HOURS", sto.vacuum_interval_hours
        )

        # -- alerts --
        al = cfg.alerts
        al.temperature_warning_c = _env_float("TELEMETRY_TEMP_WARNING", al.temperature_warning_c)
        al.temperature_critical_c = _env_float("TELEMETRY_TEMP_CRITICAL", al.temperature_critical_c)
        al.vram_pressure_pct = _env_float("TELEMETRY_VRAM_PRESSURE", al.vram_pressure_pct)
        al.throughput_degradation_pct = _env_float(
            "TELEMETRY_THROUGHPUT_DEGRADATION", al.throughput_degradation_pct
        )
        al.stall_duration_ms = _env_float("TELEMETRY_STALL_DURATION", al.stall_duration_ms)
        al.draft_acceptance_min = _env_float("TELEMETRY_DRAFT_ACCEPTANCE", al.draft_acceptance_min)
        al.cache_hit_rate_min = _env_float("TELEMETRY_CACHE_HIT_MIN", al.cache_hit_rate_min)
        al.power_limit_pct = _env_float("TELEMETRY_POWER_LIMIT", al.power_limit_pct)

        # -- ssh --
        ssh = cfg.ssh
        ssh.host = _env("SSH_HOST", ssh.host) or ssh.host
        ssh.user = _env("SSH_USER", ssh.user) or ssh.user
        ssh.timeout_seconds = _env_int("SSH_TIMEOUT", ssh.timeout_seconds)
        ssh.key_check = _env("SSH_KEY_CHECK", ssh.key_check) or ssh.key_check

        # -- dashboard --
        dash = cfg.dashboard
        dash.output_dir = _env("TELEMETRY_DASHBOARD_DIR", dash.output_dir) or dash.output_dir
        dash.refresh_interval_seconds = _env_int(
            "TELEMETRY_DASHBOARD_REFRESH", dash.refresh_interval_seconds
        )
        dash.dark_theme = _env_bool("TELEMETRY_DARK_THEME", dash.dark_theme)
        dash.chart_width = _env_int("TELEMETRY_CHART_WIDTH", dash.chart_width)
        dash.chart_height = _env_int("TELEMETRY_CHART_HEIGHT", dash.chart_height)

        # -- top-level --
        cfg.verbose = _env_bool("TELEMETRY_VERBOSE", cfg.verbose)
        cfg.log_file = _env("TELEMETRY_LOG_FILE", cfg.log_file) or cfg.log_file

        if any(
            k.startswith("TELEMETRY_") or k.startswith("BEE_LLAMA_") or k.startswith("SSH_")
            for k in os.environ
        ):
            logger.info("Applied environment variable overrides to telemetry config")

        return cfg


# ===================================================================
# Convenience function
# ===================================================================


def get_config(path: str | None = None) -> TelemetryConfig:
    """Load telemetry configuration with a priority chain.

    1. If *path* is given and exists, load from that file.
    2. Otherwise check ``TELEMETRY_CONFIG`` env var for a file path.
    3. If that file exists, load from it.
    4. As a last resort, apply any ``TELEMETRY_*`` / ``BEE_LLAMA_*``
       environment variable overrides on top of compiled defaults.

    Returns:
        A fully hydrated ``TelemetryConfig`` instance.
    """
    # 1. Explicit path argument
    if path is not None:
        cfg_path = Path(path).expanduser()
        if cfg_path.exists():
            return TelemetryConfig.from_file(str(cfg_path))
        logger.warning("Config file %s not found, falling back to defaults", cfg_path)

    # 2. TELEMETRY_CONFIG environment variable
    env_path = os.environ.get("TELEMETRY_CONFIG")
    if env_path:
        cfg_path = Path(env_path).expanduser()
        if cfg_path.exists():
            return TelemetryConfig.from_file(str(cfg_path))
        logger.warning(
            "TELEMETRY_CONFIG points to %s which does not exist, falling back",
            cfg_path,
        )

    # 3. Environment overrides on defaults
    return TelemetryConfig.from_env()


# ===================================================================
# CLI entry point
# ===================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BeeLlama telemetry config utility")
    sub = parser.add_subparsers(dest="command")

    # -- show --
    show_p = sub.add_parser("show", help="Print current config as JSON")
    show_p.add_argument("--file", "-f", help="Load from config file first")
    show_p.add_argument("--yaml", action="store_true", help="Output as YAML")

    # -- save --
    save_p = sub.add_parser("save", help="Save default config to a file")
    save_p.add_argument("output", help="Path to write (JSON or YAML)")

    # -- validate --
    val_p = sub.add_parser("validate", help="Validate a config file")
    val_p.add_argument("config_file", help="Path to validate")

    # -- defaults --
    sub.add_parser("defaults", help="Print compiled defaults as JSON")

    args = parser.parse_args()

    if args.command == "show":
        cfg = get_config(args.file if hasattr(args, "file") else None)
        if hasattr(args, "yaml") and args.yaml and HAS_YAML:
            print(yaml.safe_dump(cfg.to_dict(), default_flow_style=False), end="")
        else:
            print(json.dumps(cfg.to_dict(), indent=2))

    elif args.command == "save":
        cfg = TelemetryConfig()
        cfg.to_file(args.output)
        print(f"Default config written to {args.output}")

    elif args.command == "validate":
        try:
            cfg = TelemetryConfig.from_file(args.config_file)
            print(f"OK — config loaded successfully")
            print(f"  endpoints.base_url = {cfg.endpoints.base_url}")
            print(f"  storage.db_path    = {cfg.storage.db_path}")
            print(f"  alerts.temp_warn   = {cfg.alerts.temperature_warning_c}C")
        except Exception as exc:
            print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "defaults":
        print(json.dumps(DEFAULT_CONFIG, indent=2))

    else:
        # No subcommand — show the current resolved config
        cfg = get_config()
        print(json.dumps(cfg.to_dict(), indent=2))
        print(f"\n# YAML support: {'available' if HAS_YAML else 'not installed (pip install pyyaml)'}")
