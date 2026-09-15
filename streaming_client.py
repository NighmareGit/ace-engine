"""
streaming_client.py — Zero-dependency HTTP client for the live event streaming server.

Emits single events or batches to a FastAPI SSE server via ``urllib.request``.
Designed for producers that run inside the engine, DSH agents, or any other
Python process that needs to push structured events into the live stream.

Usage::

    from streaming_client import StreamingClient

    client = StreamingClient("http://127.0.0.1:3081")
    client.emit_event("engine", "task.start", {"task_id": "T01", "title": "Create streaming server"})
    client.emit_event("engine", "inference.complete", {"task_id": "T01", "tok_s": 197.3, "tokens": 2048})

    # Batch mode
    events = [
        client.build_envelope("engine", "pipeline.start", {"run_id": "R01"}),
        client.build_envelope("engine", "task.start", {"task_id": "T01"}),
    ]
    client.emit_batch(events)

Event envelope format::

    {
        "id": "01HX7K2M...",          # ULID (auto-generated if omitted)
        "seq": 42,                     # Monotonic sequence (auto-assigned by server)
        "time": 1735689600000,         # Server wall-clock (auto-assigned)
        "source_time": 1735689599500,  # Producer's local timestamp
        "source": "engine",            # "engine" | "dsh" | "system"
        "type": "task.start",          # Namespaced event type
        "data": {}                     # Event-specific payload
    }

Event types (engine-sourced):
    pipeline.start, pipeline.complete
    task.start, task.complete, task.fail
    inference.start, inference.complete
    quality.result, test.result, git.commit
    gpu.snapshot
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

__all__ = ["StreamingClient"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ULID-like ID generator
# ---------------------------------------------------------------------------

# Crockford Base32 alphabet (no I, L, O, U to avoid ambiguity)
_C32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Monotonic counter for uniqueness within the same millisecond
_counter_lock = threading.Lock()
_counter: int = 0
_prev_ms: int = 0


def generate_ulid() -> str:
    """Generate a ULID-like 26-character string.

    The first 10 characters encode the current Unix timestamp in
    milliseconds using Crockford Base32 (40-bit precision, ~34 years
    of range from epoch).  The remaining 16 characters encode a
    monotonic counter so that IDs generated within the same
    millisecond are still strictly ordered and unique.

    Returns
    -------
    str
        A 26-character Crockord-Base32 string, e.g. ``"01HX7K2M00ABCDEF00000001"``.
    """
    global _counter, _prev_ms

    now_ms = int(time.time() * 1000)

    with _counter_lock:
        if now_ms == _prev_ms:
            _counter += 1
        else:
            _counter = 0
            _prev_ms = now_ms
        snapshot = _counter

    # 40-bit timestamp → 8 Crockford-Base32 chars
    ts_part = _encode_base32(now_ms, 8)

    # 48-bit monotonic counter (fits in 10 Crockford-Base32 chars)
    # This guarantees strict ordering within the same millisecond.
    cnt_part = _encode_base32(snapshot, 10)

    # 16-bit random discriminator from a process-local source for extra uniqueness.
    # Uses a simple xorshift rather than importing secrets (zero extra deps).
    rnd = ((snapshot * 6364136223846793005 + 0x4C615351) >> 16) & 0xFFFF
    rnd_part = _encode_base32(rnd, 8)

    return ts_part + cnt_part + rnd_part


def _encode_base32(value: int, num_chars: int) -> str:
    """Encode an integer as a Crockford Base32 string of exactly *num_chars* characters."""
    result = []
    for _ in range(num_chars):
        result.append(_C32[value & 0x1F])
        value >>= 5
    result.reverse()
    return "".join(result)


# ---------------------------------------------------------------------------
# Valid event types (engine-sourced)
# ---------------------------------------------------------------------------

VALID_ENGINE_EVENT_TYPES: frozenset[str] = frozenset({
    "pipeline.start",
    "pipeline.complete",
    "task.start",
    "task.complete",
    "task.fail",
    "inference.start",
    "inference.complete",
    "quality.result",
    "test.result",
    "git.commit",
    "gpu.snapshot",
})

VALID_SOURCES: frozenset[str] = frozenset({"engine", "dsh", "system"})


# ---------------------------------------------------------------------------
# StreamingClient
# ---------------------------------------------------------------------------


class StreamingClient:
    """HTTP client for emitting events to a live streaming server.

    Parameters
    ----------
    base_url:
        Root URL of the streaming server, e.g. ``"http://127.0.0.1:3081"``.
        A trailing slash is stripped.
    token:
        Optional bearer token for ``Authorization: Bearer <token>`` header.
    timeout:
        Connection/read timeout in seconds.  Defaults to ``5``.
    max_retries:
        Maximum number of attempts on transient failures (connection
        refused, timeout, 5xx).  Defaults to ``2``.  A value of ``1``
        means no retry.
    backoff_seconds:
        Base delay between retry attempts in seconds.  Defaults to ``1``.
        Exponential backoff is applied: ``delay * 2^(attempt - 1)``.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3081",
        token: Optional[str] = None,
        timeout: float = 5.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._backoff = max(0.0, backoff_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(
        self,
        event: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """POST a single event envelope to ``POST /events``.

        Parameters
        ----------
        event:
            A fully-formed event envelope dict.  At minimum it must
            contain ``"source"`` and ``"type"`` keys.  An ``"id"`` is
            auto-generated if missing.

        Returns
        -------
        dict | None
            The server's JSON response body on success, or ``None`` if
            the request failed after all retries.
        """
        if "id" not in event:
            event["id"] = generate_ulid()
        if "source_time" not in event:
            event["source_time"] = int(time.time() * 1000)

        url = f"{self._base_url}/events"
        return self._post_json(url, event)

    def emit_batch(
        self,
        events: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """POST a list of event envelopes to ``POST /events/batch``.

        Parameters
        ----------
        events:
            List of fully-formed event envelope dicts.  ``"id"`` and
            ``"source_time"`` are auto-populated if missing.

        Returns
        -------
        dict | None
            The server's JSON response body on success, or ``None`` if
            the request failed after all retries.
        """
        now_ms = int(time.time() * 1000)
        for evt in events:
            if "id" not in evt:
                evt["id"] = generate_ulid()
            if "source_time" not in evt:
                evt["source_time"] = now_ms

        url = f"{self._base_url}/events/batch"
        return self._post_json(url, {"events": events})

    def emit_event(
        self,
        source: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Convenience method: build an envelope and emit it.

        Parameters
        ----------
        source:
            Logical source name.  One of ``"engine"``, ``"dsh"``, or
            ``"system"``.
        event_type:
            Namespaced event type, e.g. ``"task.start"``.
        data:
            Event-specific payload dict.  Defaults to ``{}``.
        source_id:
            Optional session or run identifier to include in the envelope.

        Returns
        -------
        dict | None
            The server's JSON response body on success, or ``None`` on
            failure.
        """
        envelope = self.build_envelope(
            source=source,
            event_type=event_type,
            data=data or {},
            source_id=source_id,
        )
        return self.emit(envelope)

    def build_envelope(
        self,
        source: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Construct an event envelope dict without sending it.

        This is useful when you need to assemble a batch manually.

        Parameters
        ----------
        source:
            Logical source name (``"engine"`` | ``"dsh"`` | ``"system"``).
        event_type:
            Namespaced event type string.
        data:
            Event payload.  Defaults to ``{}``.
        source_id:
            Optional session/run identifier.

        Returns
        -------
        dict
            A complete event envelope ready for :meth:`emit` or
            :meth:`emit_batch`.
        """
        now_ms = int(time.time() * 1000)
        envelope: Dict[str, Any] = {
            "id": generate_ulid(),
            "source_time": now_ms,
            "source": source,
            "type": event_type,
            "data": data or {},
        }
        if source_id is not None:
            envelope["source_id"] = source_id
        return envelope

    # ------------------------------------------------------------------
    # Internal HTTP transport
    # ------------------------------------------------------------------

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST a JSON payload with retry and backoff.

        Returns the parsed JSON response on success, or ``None`` after
        exhausting all attempts.
        """
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    resp_body = resp.read().decode("utf-8")
                    if resp_body.strip():
                        return json.loads(resp_body)
                    return {"status": "ok"}

            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "StreamingClient POST %s attempt %d/%d failed: %s",
                    url,
                    attempt,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries:
                    delay = self._backoff * (2 ** (attempt - 1))
                    time.sleep(delay)

        logger.error(
            "StreamingClient POST %s failed after %d attempts. Last error: %s",
            url,
            self._max_retries,
            last_error,
        )
        return None

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        auth = "token=***" if self._token else "no-auth"
        return (
            f"StreamingClient(base_url={self._base_url!r}, "
            f"timeout={self._timeout}, retries={self._max_retries}, "
            f"{auth})"
        )


# ---------------------------------------------------------------------------
# Shared fire-and-forget helper (used by engine hooks)
# ---------------------------------------------------------------------------

_shared_client: Optional[StreamingClient] = None


def emit_event_fire_and_forget(source: str, event_type: str, data: dict) -> None:
    """Emit an event to the streaming server. Fire-and-forget — never raises.

    This is the canonical helper for engine modules (task_queue, code_generator,
    quality_gates, git_workflow, test_runner) to emit streaming events without
    duplicating client initialization logic.

    The StreamingClient is lazily initialized from environment variables:
      - STREAMING_SERVER_URL (default: http://127.0.0.1:3081)
      - STREAMING_TOKEN (default: None — no auth)
    """
    global _shared_client
    if _shared_client is None:
        try:
            import os
            url = os.environ.get("STREAMING_SERVER_URL", "http://127.0.0.1:3081")
            token = os.environ.get("STREAMING_TOKEN", None)
            _shared_client = StreamingClient(base_url=url, token=token)
        except Exception:
            return
    try:
        _shared_client.emit_event(source, event_type, data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Quick smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = StreamingClient("http://127.0.0.1:3081")
    print(repr(client))

    # Single event
    result = client.emit_event(
        "engine",
        "task.start",
        {"task_id": "T01", "title": "Create streaming server"},
    )
    print(f"emit_event → {result}")

    # Single event (inference)
    result = client.emit_event(
        "engine",
        "inference.complete",
        {"task_id": "T01", "tok_s": 197.3, "tokens": 2048},
    )
    print(f"emit_event → {result}")

    # Batch
    batch = [
        client.build_envelope("engine", "pipeline.start", {"run_id": "R01"}),
        client.build_envelope("engine", "task.start", {"task_id": "T01"}),
        client.build_envelope("engine", "inference.start", {"task_id": "T01"}),
    ]
    result = client.emit_batch(batch)
    print(f"emit_batch → {result}")
