"""
BeeLlama Telemetry Data Models

Dataclasses representing all telemetry types in the BeeLlama telemetry pipeline.
Serializable to/from dict and JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def to_json(obj) -> str:
    """Serialize any dataclass to JSON."""
    if hasattr(obj, "__dataclass_fields__"):
        return json.dumps(asdict(obj), indent=2, default=str)
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# TimingsData
# ---------------------------------------------------------------------------

@dataclass
class TimingsData:
    """Parsed from BeeLlama API response timings object."""

    cache_n: int = 0
    cache_lcp_n: int = 0
    cache_planned_n: int = 0
    cache_reprocessed_n: int = 0
    cache_source: str = "none"
    cache_reason: str = ""
    prompt_n: int = 0
    prompt_ms: float = 0.0
    prompt_per_token_ms: float = 0.0
    prompt_per_second: float = 0.0
    predicted_n: int = 0
    predicted_ms: float = 0.0
    predicted_per_token_ms: float = 0.0
    predicted_per_second: float = 0.0
    draft_n: int = 0
    draft_n_accepted: int = 0

    # -- derived properties --------------------------------------------------

    @property
    def draft_acceptance_rate(self) -> float:
        if self.draft_n == 0:
            return 0.0
        return self.draft_n_accepted / self.draft_n

    @property
    def total_tokens(self) -> int:
        return self.prompt_n + self.predicted_n

    @property
    def total_ms(self) -> float:
        return self.prompt_ms + self.predicted_ms

    @property
    def cache_efficiency(self) -> float:
        """Fraction of prompt tokens served from cache."""
        if self.prompt_n == 0:
            return 0.0
        return self.cache_n / self.prompt_n

    # -- serialisation -------------------------------------------------------

    @classmethod
    def from_api_response(cls, timings_dict: dict) -> TimingsData:
        """Parse from BeeLlama API timings JSON."""
        if timings_dict is None:
            return cls()
        d = timings_dict
        return cls(
            cache_n=d.get("cache_n", 0),
            cache_lcp_n=d.get("cache_lcp_n", 0),
            cache_planned_n=d.get("cache_planned_n", 0),
            cache_reprocessed_n=d.get("cache_reprocessed_n", 0),
            cache_source=d.get("cache_source", "none"),
            cache_reason=d.get("cache_reason", ""),
            prompt_n=d.get("prompt_n", 0),
            prompt_ms=d.get("prompt_ms", 0.0),
            prompt_per_token_ms=d.get("prompt_per_token_ms", 0.0),
            prompt_per_second=d.get("prompt_per_second", 0.0),
            predicted_n=d.get("predicted_n", 0),
            predicted_ms=d.get("predicted_ms", 0.0),
            predicted_per_token_ms=d.get("predicted_per_token_ms", 0.0),
            predicted_per_second=d.get("predicted_per_second", 0.0),
            draft_n=d.get("draft_n", 0),
            draft_n_accepted=d.get("draft_n_accepted", 0),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TimingsData:
        if d is None:
            return cls()
        return cls(
            cache_n=d.get("cache_n", 0),
            cache_lcp_n=d.get("cache_lcp_n", 0),
            cache_planned_n=d.get("cache_planned_n", 0),
            cache_reprocessed_n=d.get("cache_reprocessed_n", 0),
            cache_source=d.get("cache_source", "none"),
            cache_reason=d.get("cache_reason", ""),
            prompt_n=d.get("prompt_n", 0),
            prompt_ms=d.get("prompt_ms", 0.0),
            prompt_per_token_ms=d.get("prompt_per_token_ms", 0.0),
            prompt_per_second=d.get("prompt_per_second", 0.0),
            predicted_n=d.get("predicted_n", 0),
            predicted_ms=d.get("predicted_ms", 0.0),
            predicted_per_token_ms=d.get("predicted_per_token_ms", 0.0),
            predicted_per_second=d.get("predicted_per_second", 0.0),
            draft_n=d.get("draft_n", 0),
            draft_n_accepted=d.get("draft_n_accepted", 0),
        )


# ---------------------------------------------------------------------------
# UsageData
# ---------------------------------------------------------------------------

@dataclass
class UsageData:
    """Parsed from BeeLlama API response usage object."""

    completion_tokens: int = 0
    prompt_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    visible_tokens: int = 0

    @classmethod
    def from_api_response(cls, usage_dict: dict) -> UsageData:
        if usage_dict is None:
            return cls()
        d = usage_dict
        return cls(
            completion_tokens=d.get("completion_tokens", 0),
            prompt_tokens=d.get("prompt_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            cached_tokens=d.get("cached_tokens", 0),
            reasoning_tokens=d.get("reasoning_tokens", 0),
            visible_tokens=d.get("visible_tokens", 0),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> UsageData:
        if d is None:
            return cls()
        return cls(
            completion_tokens=d.get("completion_tokens", 0),
            prompt_tokens=d.get("prompt_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            cached_tokens=d.get("cached_tokens", 0),
            reasoning_tokens=d.get("reasoning_tokens", 0),
            visible_tokens=d.get("visible_tokens", 0),
        )


# ---------------------------------------------------------------------------
# GPUStats
# ---------------------------------------------------------------------------

@dataclass
class GPUStats:
    """Enhanced GPU statistics from nvidia-smi."""

    gpu_index: int = 0
    name: str = ""
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    temperature_c: float = 0.0
    utilization_gpu_pct: float = 0.0
    utilization_memory_pct: float = 0.0
    power_draw_watts: float = 0.0
    power_limit_watts: float = 0.0
    clock_sm_mhz: float = 0.0
    clock_mem_mhz: float = 0.0
    process_count: int = 0
    process_vram_mb: float = 0.0
    pcie_gen: int = 0
    pcie_width: int = 0
    timestamp: str = ""

    # -- derived properties --------------------------------------------------

    @property
    def memory_utilization_pct(self) -> float:
        if self.memory_total_mb == 0:
            return 0.0
        return (self.memory_used_mb / self.memory_total_mb) * 100.0

    @property
    def thermal_throttling_detected(self) -> bool:
        return self.temperature_c > 83.0

    # -- serialisation -------------------------------------------------------

    @classmethod
    def from_nvidia_smi(cls, gpu_dict: dict) -> GPUStats:
        """Parse from an nvidia-smi style dictionary."""
        if gpu_dict is None:
            return cls()
        d = gpu_dict
        return cls(
            gpu_index=d.get("gpu_index", d.get("index", 0)),
            name=d.get("name", d.get("gpu_name", "")),
            memory_used_mb=d.get("memory_used_mb", d.get("memory.used", 0.0)),
            memory_total_mb=d.get("memory_total_mb", d.get("memory.total", 0.0)),
            temperature_c=d.get("temperature_c", d.get("temperature.gpu", 0.0)),
            utilization_gpu_pct=d.get("utilization_gpu_pct", d.get("utilization.gpu", 0.0)),
            utilization_memory_pct=d.get("utilization_memory_pct", d.get("utilization.memory", 0.0)),
            power_draw_watts=d.get("power_draw_watts", d.get("power.draw", 0.0)),
            power_limit_watts=d.get("power_limit_watts", d.get("power.limit", 0.0)),
            clock_sm_mhz=d.get("clock_sm_mhz", d.get("clocks.current.graphics", 0.0)),
            clock_mem_mhz=d.get("clock_mem_mhz", d.get("clocks.current.memory", 0.0)),
            process_count=d.get("process_count", 0),
            process_vram_mb=d.get("process_vram_mb", 0.0),
            pcie_gen=d.get("pcie_gen", d.get("pcie.link.gen.current", 0)),
            pcie_width=d.get("pcie_width", d.get("pcie.link.width.current", 0)),
            timestamp=d.get("timestamp", datetime.utcnow().isoformat()),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> GPUStats:
        if d is None:
            return cls()
        return cls(
            gpu_index=d.get("gpu_index", 0),
            name=d.get("name", ""),
            memory_used_mb=d.get("memory_used_mb", 0.0),
            memory_total_mb=d.get("memory_total_mb", 0.0),
            temperature_c=d.get("temperature_c", 0.0),
            utilization_gpu_pct=d.get("utilization_gpu_pct", 0.0),
            utilization_memory_pct=d.get("utilization_memory_pct", 0.0),
            power_draw_watts=d.get("power_draw_watts", 0.0),
            power_limit_watts=d.get("power_limit_watts", 0.0),
            clock_sm_mhz=d.get("clock_sm_mhz", 0.0),
            clock_mem_mhz=d.get("clock_mem_mhz", 0.0),
            process_count=d.get("process_count", 0),
            process_vram_mb=d.get("process_vram_mb", 0.0),
            pcie_gen=d.get("pcie_gen", 0),
            pcie_width=d.get("pcie_width", 0),
            timestamp=d.get("timestamp", ""),
        )


# ---------------------------------------------------------------------------
# PipelineEvent
# ---------------------------------------------------------------------------

@dataclass
class PipelineEvent:
    """A discrete event in the inference pipeline."""

    trace_id: int = 0
    event_type: str = ""  # batch_submit, draft_accept, stall_detected, etc.
    event_data: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PipelineEvent:
        if d is None:
            return cls()
        return cls(
            trace_id=d.get("trace_id", 0),
            event_type=d.get("event_type", ""),
            event_data=d.get("event_data", {}),
            duration_ms=d.get("duration_ms", 0.0),
            timestamp=d.get("timestamp", ""),
        )


# ---------------------------------------------------------------------------
# InferenceTrace  (depends on PipelineEvent)
# ---------------------------------------------------------------------------

@dataclass
class InferenceTrace:
    """Complete trace of a single inference request."""

    request_id: str = ""
    config_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    status: str = "running"
    # Prompt
    prompt_tokens: int = 0
    prompt_ms: float = 0.0
    prompt_per_second: float = 0.0
    # Generation
    predicted_tokens: int = 0
    predicted_ms: float = 0.0
    predicted_per_second: float = 0.0
    # Draft
    draft_n: int = 0
    draft_n_accepted: int = 0
    draft_acceptance_rate: float = 0.0
    # Cache
    cache_n: int = 0
    cache_lcp_n: int = 0
    cache_planned_n: int = 0
    cache_reprocessed_n: int = 0
    cache_source: str = ""
    cache_reason: str = ""
    # GPU state
    gpu_temperature: float = 0.0
    gpu_vram_used_mb: float = 0.0
    gpu_utilization_pct: float = 0.0
    gpu_power_watts: float = 0.0
    # Derived
    total_tokens: int = 0
    total_ms: float = 0.0
    tokens_per_ms: float = 0.0
    # Metadata
    model_name: str = ""
    model_path: str = ""
    context_size: int = 0
    kvarn_level: str = ""
    thinking_tokens: int = 0
    visible_tokens: int = 0
    raw_timings_json: str = ""
    raw_usage_json: str = ""
    # Events
    events: List[PipelineEvent] = field(default_factory=list)

    # -- derived computation -------------------------------------------------

    def compute_derived(self) -> None:
        """Compute derived fields from raw data."""
        self.total_tokens = self.prompt_tokens + self.predicted_tokens
        self.total_ms = self.prompt_ms + self.predicted_ms
        self.tokens_per_ms = (
            self.predicted_tokens / self.predicted_ms
            if self.predicted_ms > 0
            else 0.0
        )
        self.draft_acceptance_rate = (
            self.draft_n_accepted / self.draft_n if self.draft_n > 0 else 0.0
        )

    # -- serialisation -------------------------------------------------------

    @classmethod
    def from_api_response(
        cls,
        response: dict,
        config_id: str,
        gpu_stats: Optional[GPUStats] = None,
    ) -> InferenceTrace:
        """Build trace from full BeeLlama API response + optional GPU snapshot."""
        if response is None:
            return cls(config_id=config_id)

        timings_dict = response.get("timings", {})
        usage_dict = response.get("usage", {})

        timings = TimingsData.from_api_response(timings_dict)
        usage = UsageData.from_api_response(usage_dict)

        gpu_temp = 0.0
        gpu_vram = 0.0
        gpu_util = 0.0
        gpu_power = 0.0
        if gpu_stats is not None:
            gpu_temp = gpu_stats.temperature_c
            gpu_vram = gpu_stats.memory_used_mb
            gpu_util = gpu_stats.utilization_gpu_pct
            gpu_power = gpu_stats.power_draw_watts

        trace = cls(
            request_id=response.get("id", response.get("request_id", "")),
            config_id=config_id,
            started_at=response.get("created_at", response.get("started_at", "")),
            completed_at=response.get("completed_at", datetime.utcnow().isoformat()),
            status=response.get("status", "completed"),
            # Prompt
            prompt_tokens=timings.prompt_n,
            prompt_ms=timings.prompt_ms,
            prompt_per_second=timings.prompt_per_second,
            # Generation
            predicted_tokens=timings.predicted_n,
            predicted_ms=timings.predicted_ms,
            predicted_per_second=timings.predicted_per_second,
            # Draft
            draft_n=timings.draft_n,
            draft_n_accepted=timings.draft_n_accepted,
            draft_acceptance_rate=timings.draft_acceptance_rate,
            # Cache
            cache_n=timings.cache_n,
            cache_lcp_n=timings.cache_lcp_n,
            cache_planned_n=timings.cache_planned_n,
            cache_reprocessed_n=timings.cache_reprocessed_n,
            cache_source=timings.cache_source,
            cache_reason=timings.cache_reason,
            # GPU state
            gpu_temperature=gpu_temp,
            gpu_vram_used_mb=gpu_vram,
            gpu_utilization_pct=gpu_util,
            gpu_power_watts=gpu_power,
            # Metadata
            model_name=response.get("model", response.get("model_name", "")),
            model_path=response.get("model_path", ""),
            context_size=response.get("context_size", 0),
            kvarn_level=response.get("kvarn_level", ""),
            thinking_tokens=usage.reasoning_tokens,
            visible_tokens=usage.visible_tokens,
            # Raw JSON dumps for audit
            raw_timings_json=json.dumps(timings_dict, default=str),
            raw_usage_json=json.dumps(usage_dict, default=str),
        )

        trace.compute_derived()
        return trace

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> InferenceTrace:
        if d is None:
            return cls()
        events_raw = d.get("events", [])
        events = [
            PipelineEvent.from_dict(e) if isinstance(e, dict) else e
            for e in events_raw
        ]
        trace = cls(
            request_id=d.get("request_id", ""),
            config_id=d.get("config_id", ""),
            started_at=d.get("started_at", ""),
            completed_at=d.get("completed_at", ""),
            status=d.get("status", "running"),
            prompt_tokens=d.get("prompt_tokens", 0),
            prompt_ms=d.get("prompt_ms", 0.0),
            prompt_per_second=d.get("prompt_per_second", 0.0),
            predicted_tokens=d.get("predicted_tokens", 0),
            predicted_ms=d.get("predicted_ms", 0.0),
            predicted_per_second=d.get("predicted_per_second", 0.0),
            draft_n=d.get("draft_n", 0),
            draft_n_accepted=d.get("draft_n_accepted", 0),
            draft_acceptance_rate=d.get("draft_acceptance_rate", 0.0),
            cache_n=d.get("cache_n", 0),
            cache_lcp_n=d.get("cache_lcp_n", 0),
            cache_planned_n=d.get("cache_planned_n", 0),
            cache_reprocessed_n=d.get("cache_reprocessed_n", 0),
            cache_source=d.get("cache_source", ""),
            cache_reason=d.get("cache_reason", ""),
            gpu_temperature=d.get("gpu_temperature", 0.0),
            gpu_vram_used_mb=d.get("gpu_vram_used_mb", 0.0),
            gpu_utilization_pct=d.get("gpu_utilization_pct", 0.0),
            gpu_power_watts=d.get("gpu_power_watts", 0.0),
            total_tokens=d.get("total_tokens", 0),
            total_ms=d.get("total_ms", 0.0),
            tokens_per_ms=d.get("tokens_per_ms", 0.0),
            model_name=d.get("model_name", ""),
            model_path=d.get("model_path", ""),
            context_size=d.get("context_size", 0),
            kvarn_level=d.get("kvarn_level", ""),
            thinking_tokens=d.get("thinking_tokens", 0),
            visible_tokens=d.get("visible_tokens", 0),
            raw_timings_json=d.get("raw_timings_json", ""),
            raw_usage_json=d.get("raw_usage_json", ""),
            events=events,
        )
        return trace


# ---------------------------------------------------------------------------
# CacheEfficiencySnapshot
# ---------------------------------------------------------------------------

@dataclass
class CacheEfficiencySnapshot:
    """Aggregated cache performance over a time window."""

    config_id: str = ""
    total_requests: int = 0
    cache_hit_count: int = 0
    cache_hit_rate: float = 0.0
    avg_lcp_tokens: float = 0.0
    avg_reprocessed_tokens: float = 0.0
    kv_used_bytes: int = 0
    kv_capacity_bytes: int = 0
    kv_utilization_pct: float = 0.0
    kvarn_compressed_bytes: int = 0
    kvarn_exact_tail_bytes: int = 0
    kvarn_compression_ratio: float = 0.0
    raw_kv_stats_json: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CacheEfficiencySnapshot:
        if d is None:
            return cls()
        return cls(
            config_id=d.get("config_id", ""),
            total_requests=d.get("total_requests", 0),
            cache_hit_count=d.get("cache_hit_count", 0),
            cache_hit_rate=d.get("cache_hit_rate", 0.0),
            avg_lcp_tokens=d.get("avg_lcp_tokens", 0.0),
            avg_reprocessed_tokens=d.get("avg_reprocessed_tokens", 0.0),
            kv_used_bytes=d.get("kv_used_bytes", 0),
            kv_capacity_bytes=d.get("kv_capacity_bytes", 0),
            kv_utilization_pct=d.get("kv_utilization_pct", 0.0),
            kvarn_compressed_bytes=d.get("kvarn_compressed_bytes", 0),
            kvarn_exact_tail_bytes=d.get("kvarn_exact_tail_bytes", 0),
            kvarn_compression_ratio=d.get("kvarn_compression_ratio", 0.0),
            raw_kv_stats_json=d.get("raw_kv_stats_json", ""),
        )


# ---------------------------------------------------------------------------
# TelemetrySession
# ---------------------------------------------------------------------------

@dataclass
class TelemetrySession:
    """A collection session tracking metadata."""

    session_name: str = ""
    config_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    total_traces: int = 0
    total_snapshots: int = 0
    total_events: int = 0
    status: str = "active"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TelemetrySession:
        if d is None:
            return cls()
        return cls(
            session_name=d.get("session_name", ""),
            config_id=d.get("config_id", ""),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            total_traces=d.get("total_traces", 0),
            total_snapshots=d.get("total_snapshots", 0),
            total_events=d.get("total_events", 0),
            status=d.get("status", "active"),
            notes=d.get("notes", ""),
        )
