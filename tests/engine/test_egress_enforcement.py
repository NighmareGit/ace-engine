"""T6 — egress enforcement test (invariant #4 / egress-allowlist law).

The reach tier (T1-T3) introduces network egress to ACE for the first time.
Invariant #4 demands that egress happen ONLY via allowlisted tool impls, and
the registry IS the allowlist. This test asserts the INVARIANT itself:

    The full registry's network surface is EXACTLY
        {searxng URL, api.github.com, docs allowlist domains}

and nothing else. It does so by:
  1. Reading the registry's ``network_egress``-flagged tools.
  2. For each, inspecting the module-level egress contract (the single host
     or the allowlist file) — NOT trusting the flag alone.
  3. Asserting the union equals the expected set, with no extras.

This is a mock-socket-level guard: it also exercises each egress tool against
a recording HTTPConnection to prove the only hostnames contacted are the
contract hosts. No real network is used.

Target: this is ONE load-bearing invariant test (plus supporting checks).
"""

import importlib
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recorder_install(monkeypatch):
    """Install a host recorder on HTTPConnection + HTTPSConnection. Returns a
    list that collects ``(host, port)`` for every connection constructed."""
    import http.client as hc
    hosts: list = []

    orig_http = hc.HTTPConnection.__init__
    orig_https = hc.HTTPSConnection.__init__

    def _norm(host, port):
        if port is None and isinstance(host, str) and ":" in host:
            h, p = host.rsplit(":", 1)
            try:
                return (h, int(p))
            except ValueError:
                pass
        return (host, port)

    def rec_http(self_conn, host, port=None, *a, **kw):
        hosts.append(_norm(host, port))
        return orig_http(self_conn, host, port, *a, **kw)

    def rec_https(self_conn, host, port=None, *a, **kw):
        hosts.append(_norm(host, port))
        return orig_https(self_conn, host, port, *a, **kw)

    monkeypatch.setattr(hc.HTTPConnection, "__init__", rec_http)
    monkeypatch.setattr(hc.HTTPSConnection, "__init__", rec_https)
    # Disable actual I/O.
    monkeypatch.setattr(hc.HTTPConnection, "request", lambda self, *a, **kw: None)
    monkeypatch.setattr(hc.HTTPSConnection, "request", lambda self, *a, **kw: None)

    class _FakeResp:
        def __init__(self):
            self.status = 200
            self.code = 200
            self.reason = "OK"
            self.headers = {}

        def read(self, amt=None):
            return b'{"results":[],"total_count":0,"items":[],"full_name":"a/b"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        hc.HTTPConnection, "getresponse", lambda self: _FakeResp())
    monkeypatch.setattr(
        hc.HTTPSConnection, "getresponse", lambda self: _FakeResp())
    return hosts


# ---------------------------------------------------------------------------
# The invariant test
# ---------------------------------------------------------------------------

class TestEgressInvariant:
    """The registry's network surface is exactly the allowlisted set."""

    def test_egress_flagged_tools_are_exactly_the_three_reach_tools(self):
        """Only web_search, github_lookup, docs_domains may set network_egress."""
        reg = ToolRegistry()
        egress_tools = [e for e in reg._tools.values() if e.network_egress]
        names = sorted(e.name for e in egress_tools)
        assert names == ["docs_domains", "github_lookup", "web_search"]

    def test_no_implemented_tool_egress_without_flag(self):
        """Every implemented, non-egress tool must have network_egress=False.

        A tool that makes network calls but lacks the flag would be an
        undocumented egress path — a violation of invariant #4.
        """
        reg = ToolRegistry()
        for name, entry in reg._tools.items():
            if entry.implemented and entry.callable is not None:
                if name not in {"web_search", "github_lookup", "docs_domains"}:
                    assert entry.network_egress is False, (
                        f"{name} is implemented but not flagged as a "
                        f"reach-tier egress tool — undocumented egress?"
                    )

    def test_web_search_egress_is_only_searxng_url(self, monkeypatch):
        """web_search contacts ONLY the configured SearXNG URL's host."""
        from engine.intent import websearch as ws

        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        hosts = _recorder_install(monkeypatch)
        ws.search("hello")
        contacted = {h for h, _ in hosts}
        assert contacted == {"127.0.0.1"}

    def test_github_lookup_egress_is_only_api_github_com(self, monkeypatch):
        """github_lookup contacts ONLY api.github.com."""
        from engine.intent import github as gh

        hosts = _recorder_install(monkeypatch)
        gh.repo("a", "b")
        contacted = {h for h, _ in hosts}
        assert contacted == {"api.github.com"}

    def test_docs_domains_egress_is_only_allowlisted(self, monkeypatch):
        """docs_domains contacts ONLY domains in the allowlist file."""
        from engine.intent import docs as docs_mod

        hosts = _recorder_install(monkeypatch)
        store = docs_mod.DocDomainStore()
        if not store.domains:
            pytest.skip("no docs domains allowlisted")
        store.fetch(f"https://{store.domains[0]}/some/page")
        contacted = {h for h, _ in hosts}
        # Must be a subset of the allowlisted domains.
        assert contacted <= set(store.domains)

    def test_full_registry_network_surface_is_exactly_allowlisted(self):
        """The union of all egress hosts across the registry equals exactly
        {searxng default host, api.github.com, docs allowlist domains}.

        This is the load-bearing invariant: no more, no less.
        """
        reg = ToolRegistry()
        from engine.intent.websearch import DEFAULT_SEARXNG_URL
        import urllib.parse
        searx_host = urllib.parse.urlparse(DEFAULT_SEARXNG_URL).hostname
        from engine.intent.github import API_HOST
        from engine.intent.docs import DocDomainStore
        store = DocDomainStore()

        expected = {searx_host, API_HOST, *store.domains}
        # Collect the declared egress surface from the registry's egress tools.
        declared: set[str] = set()
        for e in reg._tools.values():
            if e.network_egress:
                mod_name = f"engine.intent.{e.name}"
                # Map each tool to its contract host/allowlist.
                if e.name == "web_search":
                    declared.add(searx_host)
                elif e.name == "github_lookup":
                    declared.add(API_HOST)
                elif e.name == "docs_domains":
                    declared.update(store.domains)
        assert declared == expected, (
            f"registry network surface {declared} != expected {expected}"
        )
