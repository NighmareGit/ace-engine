"""T3 — docs_domains egress tool (P4, merged with P2 infra).

Allowlist-file driven documentation fetcher. Domains come from
``engine/intent/allowlists/docs_domains.txt`` (seeded with docs.python.org and
llama-cpp.readthedocs.io). GET + text-extraction (stdlib ``html.parser``, no new
deps), size cap, robots-respecting in-memory TTL cache.

Egress contract (invariant #4 / egress-allowlist law):
  * The fetcher ONLY contacts domains present in the allowlist file. Any URL
    whose host is not allowlisted is rejected (ok=False + telemetry event)
    BEFORE any network call. It is an allowlisted-impl tool: the registry
    marks ``network_egress=True`` and the egress-enforcement test (T6) asserts
    the full registry's network surface is exactly {searxng URL, api.github.com,
    docs allowlist domains}.
  * The model supplies a URL; the tool validates the host against the
    allowlist. The model cannot widen the egress surface.

Robots-respecting cache: a simple in-memory TTL cache keyed by URL avoids
repeated fetches and naturally rate-limits repeated requests to the same
domain (a primitive robots.txt respect mechanism — we do not fetch robots.txt
itself, but the TTL bounds our request rate to any single doc page).

Availability-gating: an empty/absent allowlist makes every fetch return
ok=False. Connection failures and non-200s return ok=False with a clear
error. A telemetry event is emitted per fetch — failures feed TGD G11.

No new runtime dependencies — stdlib ``urllib`` + ``html.parser`` only.
"""

from __future__ import annotations

import html.parser
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("engine.intent.docs")

DEFAULT_ALLOWLIST = os.path.join(
    os.path.dirname(__file__), "allowlists", "docs_domains.txt",
)
TIMEOUT_SEC = 8
SIZE_CAP = 50_000  # chars — bound context injection from a single doc page
DEFAULT_TTL_SEC = 300  # 5-minute in-memory cache


# ---------------------------------------------------------------------------
# HTML text extraction — stdlib html.parser, no new deps.
# ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    """Extract visible text from HTML, dropping script/style/nav content."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return " ".join(self._chunks)


def _extract_text(html_bytes: bytes) -> str:
    """Best-effort text extraction from HTML bytes (utf-8, errors replaced)."""
    parser = _TextExtractor()
    try:
        parser.feed(html_bytes.decode("utf-8", errors="replace"))
        parser.close()
    except Exception as exc:  # noqa: BLE001 — malformed HTML must not crash
        log.debug("html text extraction failed: %s", exc)
    return parser.text()


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def load_allowlist(path: str | os.PathLike) -> list[str]:
    """Load allowlisted domains from a file (one per line, # comments ok).

    Returns a lowercased, de-duplicated, blank-stripped list. An absent file
    yields an empty list (safe default: allow nothing).
    """
    domains: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                domains.append(line.lower())
    except FileNotFoundError:
        return []
    # De-dup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for d in domains:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Store — allowlist + cache + fetch
# ---------------------------------------------------------------------------

def _open(req, timeout=TIMEOUT_SEC):
    """urlopen wrapper so tests can monkeypatch a single seam."""
    return urllib.request.urlopen(req, timeout=timeout)


def _emit_telemetry(event: dict[str, Any]) -> None:
    """Emit a telemetry event for one doc fetch. Failures feed G11."""
    try:
        log.info(
            "docs_domains telemetry: status=%s url=%r",
            event.get("status"), event.get("url"),
        )
    except Exception:  # noqa: BLE001
        pass


class DocDomainStore:
    """Allowlist-driven doc fetcher with an in-memory TTL cache.

    Args:
        allowlist_path: path to the domains allowlist file.
        ttl_sec: cache TTL in seconds (also a primitive rate-limiter).
        size_cap: max chars returned per page.
    """

    def __init__(
        self,
        allowlist_path: str | os.PathLike | None = None,
        ttl_sec: int = DEFAULT_TTL_SEC,
        size_cap: int = SIZE_CAP,
    ) -> None:
        self._allowlist_path = str(allowlist_path) if allowlist_path else DEFAULT_ALLOWLIST
        self._ttl = ttl_sec
        self._size_cap = size_cap
        self._domains = load_allowlist(self._allowlist_path)
        # cache: url -> (timestamp, result_dict)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def domains(self) -> list[str]:
        return list(self._domains)

    def is_allowed(self, host: str) -> bool:
        """True if ``host`` (or its registered-domain suffix) is allowlisted."""
        h = host.lower().strip()
        # Exact match, or allow a subdomain of an allowlisted domain (e.g.
        # "docs.python.org" matches allowlist entry "docs.python.org"; we do
        # NOT wildcard — the host must equal an entry).
        return h in self._domains

    def _host_of(self, url: str) -> str:
        try:
            return urllib.parse.urlparse(url).hostname or ""
        except Exception:  # noqa: BLE001
            return ""

    def fetch(self, url: str) -> dict[str, Any]:
        """Fetch + text-extract a doc page. Returns ``{ok, url, text}`` or
        ``{ok: False, error}``. Never raises.

        Off-allowlist URLs are rejected before any network call.
        """
        host = self._host_of(url)
        if not host or not self.is_allowed(host):
            _emit_telemetry({"status": "rejected", "url": url,
                             "error": "not allowlisted"})
            return {"ok": False, "error": f"docs_domains: host '{host}' not allowlisted"}

        # Cache check.
        cached = self._cache.get(url)
        if cached is not None:
            ts, result = cached
            if (time.time() - ts) < self._ttl:
                return result

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ace-engine/docs_domains"},
            )
            with _open(req, timeout=TIMEOUT_SEC) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 0))
                if status != 200:
                    _emit_telemetry({"status": "failure", "url": url,
                                     "error": f"HTTP {status}"})
                    result = {"ok": False, "error": f"docs_domains: HTTP {status}"}
                    self._cache[url] = (time.time(), result)
                    return result
                raw = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            reason = getattr(exc, "reason", str(exc))
            _emit_telemetry({"status": "failure", "url": url,
                             "error": f"connection failed: {reason}"})
            return {"ok": False, "error": f"docs_domains: connection failed: {reason}"}
        except Exception as exc:  # noqa: BLE001
            _emit_telemetry({"status": "failure", "url": url, "error": str(exc)})
            return {"ok": False, "error": f"docs_domains: {exc}"}

        text = _extract_text(raw)
        if len(text) > self._size_cap:
            text = text[: self._size_cap - 1] + "…"
        result = {"ok": True, "url": url, "text": text}
        self._cache[url] = (time.time(), result)
        _emit_telemetry({"status": "success", "url": url,
                         "text_len": len(text)})
        return result


# ---------------------------------------------------------------------------
# Callable adapter — uniform signature for the IIL dispatch layer.
# ---------------------------------------------------------------------------

class DocsDomainsCallable:
    """Allowlisted-impl adapter registered in the tool registry.

    The dispatcher calls this with an IntentRequest; ``url`` comes from the
    intent's parsed arguments. The store is created lazily (allowlist may be
    empty on a dev box — fetch then returns ok=False with a clear error).
    """

    def __init__(self) -> None:
        self._store: DocDomainStore | None = None

    def _get_store(self) -> DocDomainStore:
        if self._store is None:
            self._store = DocDomainStore()
        return self._store

    def __call__(self, request, **kwargs: Any) -> dict[str, Any]:
        url = kwargs.get("url") or (request.arguments or {}).get("url") or ""
        if not url:
            return {"ok": False, "error": "docs_domains: empty url"}
        store = self._get_store()
        if not store.domains:
            return {"ok": False, "error": "docs_domains: no domains allowlisted"}
        return store.fetch(url)
