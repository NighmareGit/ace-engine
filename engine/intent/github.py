"""T2 — github_lookup egress tool (P3).

Read-only GitHub REST API (https://api.github.com). Token from env
``ACE_GITHUB_TOKEN`` (optional; unauthenticated rate-limited fallback).

Egress contract (invariant #4 / egress-allowlist law):
  * This module makes HTTPS calls to exactly ONE host: ``api.github.com``.
    It NEVER contacts any other host. It is an allowlisted-impl tool: the
    registry marks ``network_egress=True`` and the egress-enforcement test
    (T6) asserts the full registry's network surface is exactly
    {searxng URL, api.github.com, docs allowlist domains}.
  * Only THREE endpoints are exposed (read-only):
      - repo metadata  GET /repos/{owner}/{repo}
      - file contents  GET /repos/{owner}/{repo}/contents/{path}
      - issue search   GET /search/issues?q=...
    Nothing else (no POST/PUT/DELETE, no users, no gists, no graphql).

Token handling: the token is read from env at call time and injected as an
``Authorization: Bearer`` header. It is NEVER logged, never included in
telemetry events, and never appears in error messages.

Availability-gating: ``available()`` probes the API root; every public
function returns ``{ok: False, ...}`` (never raises) on any failure. A
telemetry event is emitted per call — failures feed TGD G11.

No new runtime dependencies — stdlib ``urllib`` + ``base64`` only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("engine.intent.github")

API_HOST = "api.github.com"
API_BASE = f"https://{API_HOST}"
TIMEOUT_SEC = 8
MAX_ISSUES = 10  # cap on returned issue items (bounds context injection)


def _token() -> str:
    """Read the GitHub token from env. Empty string when unset (anon)."""
    return os.environ.get("ACE_GITHUB_TOKEN", "")


def _headers() -> dict[str, str]:
    """Build request headers. Token included only when set; never logged."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ace-engine/github_lookup",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def available() -> bool:
    """Probe the GitHub API root. True only if it answers 200.

    Never raises: an unreachable API means the tool is unavailable.
    """
    try:
        req = urllib.request.Request(
            API_BASE, headers=_headers(),
        )
        with _open(req, timeout=TIMEOUT_SEC) as resp:
            return getattr(resp, "status", getattr(resp, "code", 0)) == 200
    except Exception as exc:  # noqa: BLE001
        log.debug("github available() probe failed: %s", exc)
        return False


def _open(req, timeout=TIMEOUT_SEC):
    """urlopen wrapper so tests can monkeypatch a single seam."""
    return urllib.request.urlopen(req, timeout=timeout)


def _emit_telemetry(event: dict[str, Any]) -> None:
    """Emit a telemetry event for one GitHub call.

    Failures feed TGD G11. The token is never part of the event. Best-effort
    — never raises past the caller.
    """
    try:
        log.info(
            "github_lookup telemetry: status=%s endpoint=%r",
            event.get("status"), event.get("endpoint"),
        )
    except Exception:  # noqa: BLE001
        pass


def _get_json(path: str) -> dict[str, Any]:
    """Perform a GET against API_BASE + path and return the parsed JSON.

    Returns a ``{"ok": ..., ...}`` dict; never raises.
    """
    url = f"{API_BASE}{path}"
    try:
        req = urllib.request.Request(url, headers=_headers())
        with _open(req, timeout=TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 0))
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"HTTP {exc.code}", "_status": exc.code}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"connection failed: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", "_status": status}

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "error": f"malformed JSON: {exc}"}
    return {"ok": True, **data} if isinstance(data, dict) else {"ok": True, "_raw": data}


def repo(owner: str, name: str) -> dict[str, Any]:
    """Read-only repo metadata for ``owner/name``.

    Returns a normalized dict with the common fields (full_name, description,
    stargazers_count, language, html_url) plus ``ok``. Never raises.
    """
    path = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
    res = _get_json(path)
    if not res.get("ok"):
        _emit_telemetry({"status": "failure", "endpoint": path,
                         "error": res.get("error", "")})
        return res
    normalized = {
        "ok": True,
        "full_name": res.get("full_name", ""),
        "description": res.get("description") or "",
        "stargazers_count": res.get("stargazers_count", 0),
        "language": res.get("language") or "",
        "html_url": res.get("html_url", ""),
    }
    _emit_telemetry({"status": "success", "endpoint": path})
    return normalized


def contents(owner: str, name: str, path: str) -> dict[str, Any]:
    """Read-only file contents for ``path`` in ``owner/name``.

    Decodes GitHub's base64 ``content`` field and returns the decoded text
    under ``decoded`` (best-effort utf-8). Path-scoped: only the single file
    at ``path`` is returned. Never raises.
    """
    enc_path = "/".join(
        urllib.parse.quote(p, safe="") for p in path.split("/") if p
    )
    api_path = (
        f"/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}/contents/{enc_path}"
    )
    res = _get_json(api_path)
    if not res.get("ok"):
        _emit_telemetry({"status": "failure", "endpoint": api_path,
                         "error": res.get("error", "")})
        return res
    decoded = ""
    raw_content = res.get("content")
    if res.get("encoding") == "base64" and raw_content:
        try:
            decoded = base64.b64decode(raw_content.replace("\n", "")).decode(
                "utf-8", errors="replace"
            )
        except Exception:  # noqa: BLE001
            decoded = ""
    normalized = {
        "ok": True,
        "name": res.get("name", ""),
        "path": res.get("path", ""),
        "size": res.get("size", 0),
        "html_url": res.get("html_url", ""),
        "decoded": decoded,
    }
    _emit_telemetry({"status": "success", "endpoint": api_path})
    return normalized


def search_issues(query: str) -> dict[str, Any]:
    """Read-only issue search. Returns up to MAX_ISSUES items.

    ``query`` is a GitHub search qualifier string (e.g.
    ``repo:foo/bar is:open``). Never raises.
    """
    params = urllib.parse.urlencode({"q": query, "per_page": MAX_ISSUES})
    path = f"/search/issues?{params}"
    res = _get_json(path)
    if not res.get("ok"):
        _emit_telemetry({"status": "failure", "endpoint": path,
                         "error": res.get("error", "")})
        return res
    items = []
    for it in (res.get("items") or [])[:MAX_ISSUES]:
        items.append({
            "number": it.get("number"),
            "title": it.get("title", ""),
            "html_url": it.get("html_url", ""),
            "state": it.get("state", ""),
        })
    _emit_telemetry({"status": "success", "endpoint": path,
                     "result_count": len(items)})
    return {"ok": True, "total_count": res.get("total_count", 0), "items": items}


# ---------------------------------------------------------------------------
# Callable adapter — uniform signature for the IIL dispatch layer.
# ---------------------------------------------------------------------------

class GithubLookupCallable:
    """Allowlisted-impl adapter registered in the tool registry.

    The dispatcher calls this with an IntentRequest; the operation + args come
    from the intent's parsed arguments:
      * ``repo``      -> (owner, name)
      * ``contents``  -> (owner, name, path)
      * ``search_issues`` -> (query,)
    Availability is checked at call time — an unreachable API returns
    ``ok=False`` with a clear error and a telemetry event, never a raised
    exception.
    """

    def __call__(self, request, **kwargs: Any) -> dict[str, Any]:
        op = kwargs.get("operation") or (request.arguments or {}).get("operation")
        args = kwargs.get("args") or (request.arguments or {}).get("args") or {}
        if op == "repo":
            return repo(args.get("owner", ""), args.get("name", ""))
        if op == "contents":
            return contents(args.get("owner", ""), args.get("name", ""),
                            args.get("path", ""))
        if op == "search_issues":
            if not available():
                _emit_telemetry({"status": "failure", "endpoint": "search_issues",
                                 "error": "api.github.com unavailable"})
                return {"ok": False, "error": "github_lookup unavailable"}
            return search_issues(args.get("query", ""))
        return {"ok": False, "error": f"github_lookup: unknown operation {op!r}"}
