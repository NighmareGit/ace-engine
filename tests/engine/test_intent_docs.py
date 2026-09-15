"""T3 tests: docs_domains tool (engine/intent/docs.py).

docs_domains is the P4 egress tool (merged with P2 infra): an allowlist-file
driven fetcher. Domains come from engine/intent/allowlists/docs_domains.txt
(seeded with docs.python.org, llama-cpp.readthedocs.io). GET + text-extract
(stdlib html.parser, no new deps), size cap, robots-respecting in-memory TTL
cache.

Egress contract (invariant #4):
  * The fetcher ONLY contacts domains present in the allowlist file. Any URL
    whose host is not allowlisted is rejected with ok=False and a telemetry
    event — no network call is made.
  * It is an allowlisted-impl tool: the registry marks network_egress=True
    and the egress-enforcement test (T6) asserts the full registry's network
    surface is exactly {searxng URL, api.github.com, docs allowlist domains}.

These tests mock ALL network. Target: >=10 tests.
"""

import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent import docs as docs_mod


class _FakeResp:
    def __init__(self, status, body_bytes, reason="OK"):
        self.status = status
        self._body = body_bytes
        self.reason = reason
        self.code = status
        self.headers = {}

    def read(self, amt=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _allowlist(tmp_path, domains):
    p = tmp_path / "docs_domains.txt"
    p.write_text("\n".join(domains) + "\n")
    return str(p)


class TestAllowlist:
    def test_loads_domains_from_file(self, tmp_path):
        path = _allowlist(tmp_path, ["docs.python.org", "example.com"])
        store = docs_mod.DocDomainStore(allowlist_path=path)
        assert store.is_allowed("docs.python.org")
        assert store.is_allowed("example.com")
        assert not store.is_allowed("evil.com")

    def test_empty_allowlist_blocks_everything(self, tmp_path):
        path = _allowlist(tmp_path, [])
        store = docs_mod.DocDomainStore(allowlist_path=path)
        assert not store.is_allowed("docs.python.org")

    def test_comments_and_blanks_ignored(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("# a comment\n\ndocs.python.org\n# another\n")
        store = docs_mod.DocDomainStore(allowlist_path=str(p))
        assert store.is_allowed("docs.python.org")
        assert len(store.domains) == 1

    def test_off_allowlist_url_rejected_without_network(self, tmp_path):
        """An off-allowlist URL must be rejected with NO network call."""
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path)
        contacted = []

        def fake_urlopen(req, timeout=None):
            contacted.append(req.full_url if hasattr(req, "full_url") else str(req))
            return _FakeResp(200, b"<p>hi</p>")

        # Patch at module level via the store's opener seam.
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        res = store.fetch("https://evil.com/secret")
        monkeypatch.undo()
        assert res["ok"] is False
        assert "not allowlisted" in res["error"]
        assert contacted == []


class TestFetch:
    def test_fetch_extracts_text(self, tmp_path):
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path)

        html = b"<html><body><h1>Title</h1><p>Paragraph text.</p></body></html>"

        def fake_urlopen(req, timeout=None):
            return _FakeResp(200, html)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        res = store.fetch("https://docs.python.org/3/library/os.html")
        monkeypatch.undo()
        assert res["ok"] is True
        assert "Title" in res["text"]
        assert "Paragraph text." in res["text"]
        assert res["url"] == "https://docs.python.org/3/library/os.html"

    def test_fetch_respects_size_cap(self, tmp_path):
        """Huge pages are truncated to the size cap."""
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path)
        huge = b"<p>" + (b"x" * (docs_mod.SIZE_CAP + 1000)) + b"</p>"

        def fake_urlopen(req, timeout=None):
            return _FakeResp(200, huge)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        res = store.fetch("https://docs.python.org/big")
        monkeypatch.undo()
        assert res["ok"] is True
        assert len(res["text"]) <= docs_mod.SIZE_CAP + 64  # small slack for strip

    def test_fetch_non200_returns_ok_false(self, tmp_path):
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path)

        def fake_urlopen(req, timeout=None):
            return _FakeResp(404, b"Not Found")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        res = store.fetch("https://docs.python.org/missing")
        monkeypatch.undo()
        assert res["ok"] is False
        assert "404" in res["error"]

    def test_fetch_connection_failure_ok_false(self, tmp_path):
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path)

        def fake_urlopen(req, timeout=None):
            raise ConnectionError("refused")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        res = store.fetch("https://docs.python.org/3")
        monkeypatch.undo()
        assert res["ok"] is False


class TestCache:
    def test_cache_hit_avoids_second_fetch(self, tmp_path):
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path, ttl_sec=60)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            return _FakeResp(200, b"<p>cached body</p>")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        store.fetch("https://docs.python.org/cached")
        store.fetch("https://docs.python.org/cached")
        monkeypatch.undo()
        assert len(calls) == 1

    def test_cache_expires_after_ttl(self, tmp_path, monkeypatch):
        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path, ttl_sec=60)
        calls = []
        t = [0]

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            return _FakeResp(200, b"<p>body</p>")

        monkeypatch.setattr(docs_mod, "_open", fake_urlopen)
        # Fake time so we can advance past the TTL.
        monkeypatch.setattr(docs_mod.time, "time", lambda: t[0])

        store.fetch("https://docs.python.org/x")
        t[0] = 100  # jump past the 60s TTL
        store.fetch("https://docs.python.org/x")
        assert len(calls) == 2


class TestEgressEnforcement:
    def test_only_contacts_allowlisted_domains(self, tmp_path, monkeypatch):
        """fetch() must ONLY contact hosts in the allowlist."""
        import http.client as hc
        import urllib.parse

        path = _allowlist(tmp_path, ["docs.python.org"])
        store = docs_mod.DocDomainStore(allowlist_path=path)
        hosts = []

        # Record host at connection-instruction time via HTTP(S)Connection init.
        orig_http = hc.HTTPConnection.__init__
        orig_https = hc.HTTPSConnection.__init__

        def rec_http(self_conn, host, port=None, *a, **kw):
            if port is None and isinstance(host, str) and ":" in host:
                host, port = host.rsplit(":", 1)
            hosts.append(host)
            return orig_http(self_conn, host, port, *a, **kw)

        def rec_https(self_conn, host, port=None, *a, **kw):
            if port is None and isinstance(host, str) and ":" in host:
                host, port = host.rsplit(":", 1)
            hosts.append(host)
            return orig_https(self_conn, host, port, *a, **kw)

        monkeypatch.setattr(hc.HTTPConnection, "__init__", rec_http)
        monkeypatch.setattr(hc.HTTPSConnection, "__init__", rec_https)
        monkeypatch.setattr(
            hc.HTTPConnection, "request", lambda self, *a, **kw: None)
        monkeypatch.setattr(
            hc.HTTPSConnection, "request", lambda self, *a, **kw: None)
        monkeypatch.setattr(
            hc.HTTPConnection, "getresponse",
            lambda self: _FakeResp(200, b"<p>hi</p>"))
        monkeypatch.setattr(
            hc.HTTPSConnection, "getresponse",
            lambda self: _FakeResp(200, b"<p>hi</p>"))

        store.fetch("https://docs.python.org/3")
        assert set(hosts) == {"docs.python.org"}
