"""T1 — web_search egress tool (P2).

Queries a SearXNG instance (URL from env ``ACE_SEARXNG_URL``, default
``http://127.0.0.1:8888``) and returns normalized search results.

Egress contract (invariant #4 / egress-allowlist law):
  * This module makes exactly ONE kind of network call: an HTTP GET to the
    configured SearXNG ``/search`` endpoint. It NEVER contacts any other host.
  * It is an allowlisted-impl tool: the registry marks ``network_egress=True``
    and the egress-enforcement test (T6) asserts the full registry's network
    surface is exactly {searxng URL, api.github.com, docs allowlist domains}.
  * No model-facing code runs here; the model requests a search via intent and
    this tool performs the fetch (SSRF guard — the model never supplies the
    upstream URL, only the query string).

Availability-gating: ``available()`` probes the configured SearXNG with a cheap
request; ``search()`` returns ``{ok: False, ...}`` (never raises) when the
instance is unreachable or returns a non-200. A telemetry event is emitted on
every query — failures feed TGD G11 (external-reference failure signal).

Graceful degradation: if ``ACE_SEARXNG_URL`` is unset, the default local URL is
used; if that is unreachable the tool is simply ``available=False`` and every
search returns a clear ``ok=False`` error. The pipeline never crashes.

No new runtime dependencies — stdlib ``urllib`` only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("engine.intent.websearch")

# --- Tuning constants -------------------------------------------------------
DEFAULT_SEARXNG_URL = "http://127.0.0.1:8888"
TIMEOUT_SEC = 5
MAX_RESULTS = 5
SNIPPET_CAP = 320  # chars — keep context-injection cost bounded


def _env_url() -> str:
    """Read the searxng URL from env, falling back to the default."""
    return os.environ.get("ACE_SEARXNG_URL", DEFAULT_SEARXNG_URL)


def searxng_url() -> str:
    """The currently configured SearXNG base URL (no trailing slash)."""
    return _env_url().rstrip("/")


def available() -> bool:
    """Probe the configured SearXNG. True only if it answers 200 to a ping.

    Best-effort, never raises: an unreachable searxng means the tool is
    unavailable, not an error.
    """
    try:
        req = urllib.request.Request(
            f"{searxng_url()}/search?q=__ace_ping__&format=json",
            headers={"User-Agent": "ace-engine/web_search"},
        )
        with _open(req, timeout=TIMEOUT_SEC) as resp:
            return getattr(resp, "status", getattr(resp, "code", 0)) == 200
    except Exception as exc:  # noqa: BLE001 — availability must never raise
        log.debug("web_search available() probe failed: %s", exc)
        return False


def _open(req, timeout=TIMEOUT_SEC):
    """urlopen wrapper so tests can monkeypatch a single seam."""
    return urllib.request.urlopen(req, timeout=timeout)


def _emit_telemetry(event: dict[str, Any]) -> None:
    """Emit a telemetry event for one search query.

    Failures feed TGD G11 (external-reference failure signal). The event shape
    is the one the G11 extractor reads: a ``status`` field (``success`` /
    ``failure``) plus the query and, on failure, an ``error`` string. Best-
    effort — never raises past the caller.
    """
    try:
        log.info(
            "web_search telemetry: status=%s query=%r",
            event.get("status"), event.get("query"),
        )
    except Exception:  # noqa: BLE001
        pass


def search(query: str) -> dict[str, Any]:
    """Query the configured SearXNG and return normalized results.

    Args:
        query: the raw search query (only the query — the model never chooses
            the host or path).

    Returns:
        ``{"ok": True, "results": [{"title", "url", "snippet"}, ...]}`` on
        success, or ``{"ok": False, "error": "..."}`` on any failure. Results
        are hard-capped at ``MAX_RESULTS`` and snippets at ``SNIPPET_CAP``
        chars. Never raises.
    """
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{searxng_url()}/search?{params}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ace-engine/web_search"},
        )
        with _open(req, timeout=TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 0))
            if status != 200:
                _emit_telemetry({"status": "failure", "query": query,
                                 "error": f"HTTP {status}"})
                return {"ok": False, "error": f"SearXNG returned HTTP {status}"}
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        _emit_telemetry({"status": "failure", "query": query,
                         "error": str(exc.reason)})
        return {"ok": False, "error": f"connection failed: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001 — tool must not crash the pipeline
        _emit_telemetry({"status": "failure", "query": query,
                         "error": str(exc)})
        return {"ok": False, "error": str(exc)}

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _emit_telemetry({"status": "failure", "query": query,
                         "error": f"malformed JSON: {exc}"})
        return {"ok": False, "error": f"malformed SearXNG response: {exc}"}

    raw_results = data.get("results") or []
    results = []
    for r in raw_results[:MAX_RESULTS]:
        snippet = r.get("content") or r.get("snippet") or ""
        if len(snippet) > SNIPPET_CAP:
            snippet = snippet[:SNIPPET_CAP - 1] + "…"
        results.append({
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "snippet": snippet,
        })

    _emit_telemetry({"status": "success", "query": query,
                     "result_count": len(results)})
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# Callable adapter — uniform signature for the IIL dispatch layer.
# ---------------------------------------------------------------------------

class WebSearchCallable:
    """Allowlisted-impl adapter registered in the tool registry.

    The dispatcher calls this with an IntentRequest; ``query`` comes from the
    intent's parsed arguments. Availability is checked at call time — an
    unconfigured/unreachable searxng returns ``ok=False`` with a clear error
    and a telemetry event, never a raised exception.
    """

    def __call__(self, request, **kwargs: Any) -> dict[str, Any]:
        query = kwargs.get("query") or (request.arguments or {}).get("query") or ""
        if not query:
            return {"ok": False, "error": "web_search: empty query"}
        if not available():
            _emit_telemetry({"status": "failure", "query": query,
                             "error": "searxng unavailable"})
            return {
                "ok": False,
                "error": (
                    "web_search unavailable: SearXNG at "
                    f"{searxng_url()} is unreachable"
                ),
            }
        return search(query)


# os is referenced in _env_url for environ read; import here to be explicit.
import os  # noqa: E402 — kept at bottom to emphasize stdlib-only contract
