"""T1 tests: web_search tool (engine/intent/websearch.py).

web_search is the P2 egress tool: it queries a searxng instance (URL from env
ACE_SEARXNG_URL, default http://127.0.0.1:8888). It is the ONLY network call the
tool makes — never any other egress. Results are normalized (title/url/snippet,
hard cap 5, snippet length capped). Telemetry events feed TGD G11 on failure.

These tests mock ALL network (no real searxng calls). We mock at the
http.client.HTTPConnection socket level so the egress-enforcement test can
assert the exact set of hostnames contacted.

Target: >=8 tests.
"""

import json
import os
import sys
import urllib.parse

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent import websearch as ws


# ---------------------------------------------------------------------------
# Helpers — mock HTTP at the socket/connection level.
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    """Mimics http.client.HTTPResponse just enough for urlopen/HTTPConnection."""

    def __init__(self, status, body_bytes, reason="OK"):
        self.status = status
        self._body = body_bytes
        self.reason = reason
        self.code = status  # urllib.compat compatibility

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SocketRecorder:
    """Records every hostname:port a connection attempt targets.

    Used by the egress-enforcement test (T6) to prove the tool only contacts
    the configured searxng.
    """

    def __init__(self):
        self.connections = []  # list of (host, port)

    def install(self, monkeypatch):
        import http.client as hc

        orig_init = hc.HTTPConnection.__init__
        orig_https_init = hc.HTTPSConnection.__init__
        recorder = self

        def _record(host, port):
            # urllib may pass "host:port" as the host arg with port=None.
            if port is None and isinstance(host, str) and ":" in host:
                host, port = host.rsplit(":", 1)
                try:
                    port = int(port)
                except ValueError:
                    pass
            recorder.connections.append((host, port))

        def fake_init(self_conn, host, port=None, *a, **kw):
            _record(host, port)
            return orig_init(self_conn, host, port, *a, **kw)

        def fake_https_init(self_conn, host, port=None, *a, **kw):
            _record(host, port)
            return orig_https_init(self_conn, host, port, *a, **kw)

        monkeypatch.setattr(hc.HTTPConnection, "__init__", fake_init)
        monkeypatch.setattr(hc.HTTPSConnection, "__init__", fake_https_init)
        return self


def _searx_results(n=3):
    return json.dumps({
        "query": "hello",
        "results": [
            {
                "url": f"https://example.com/{i}",
                "title": f"Result {i}",
                "content": "A snippet " * 20,
            }
            for i in range(n)
        ],
    }).encode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAvailability:
    """available() reflects whether the searxng URL is reachable."""

    def test_available_true_when_healthy(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")

        def fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(200, b'{"status":"ok"}')

        monkeypatch.setattr(ws, "_open", fake_urlopen)
        assert ws.available() is True

    def test_available_false_when_down(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")

        def fake_urlopen(req, timeout=None):
            raise ConnectionError("refused")

        monkeypatch.setattr(ws, "_open", fake_urlopen)
        assert ws.available() is False

    def test_available_false_on_non200(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")

        def fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(500, b"boom", reason="Server Error")

        monkeypatch.setattr(ws, "_open", fake_urlopen)
        assert ws.available() is False


class TestSearchNormalization:
    """search() normalizes searxng JSON into the contract shape."""

    def _fake_urlopen(self, status=200, body=b""):
        def _fn(req, timeout=None):
            if isinstance(body, Exception):
                raise body
            return _FakeHTTPResponse(status, body)
        return _fn

    def test_returns_normalized_results(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        monkeypatch.setattr(ws, "_open", self._fake_urlopen(200, _searx_results(3)))
        res = ws.search("hello")
        assert res["ok"] is True
        assert len(res["results"]) == 3
        for r in res["results"]:
            assert set(r.keys()) == {"title", "url", "snippet"}

    def test_hard_cap_at_five_results(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        monkeypatch.setattr(ws, "_open", self._fake_urlopen(200, _searx_results(10)))
        res = ws.search("hello")
        assert len(res["results"]) == 5

    def test_snippet_length_capped(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        monkeypatch.setattr(ws, "_open", self._fake_urlopen(200, _searx_results(1)))
        res = ws.search("hello")
        for r in res["results"]:
            assert len(r["snippet"]) <= ws.SNIPPET_CAP

    def test_empty_results_is_ok(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        monkeypatch.setattr(ws, "_open", self._fake_urlopen(200, b'{"results":[]}'))

        res = ws.search("hello")
        assert res["ok"] is True
        assert res["results"] == []

    def test_malformed_json_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        monkeypatch.setattr(ws, "_open", self._fake_urlopen(200, b"<<not json>>"))
        res = ws.search("hello")
        assert res["ok"] is False
        assert "error" in res

    def test_connection_failure_returns_ok_false_with_telemetry(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        events = []

        def fake_urlopen(req, timeout=None):
            raise ConnectionError("refused")

        monkeypatch.setattr(ws, "_open", fake_urlopen)
        monkeypatch.setattr(ws, "_emit_telemetry", lambda e: events.append(e))
        res = ws.search("hello")
        assert res["ok"] is False
        assert "refused" in res["error"] or "error" in res
        # Telemetry event fired for the failure (feeds G11).
        assert len(events) == 1
        assert events[0]["status"] == "failure"

    def test_timeout_is_5s(self, monkeypatch):
        """The search must pass timeout=5 to the opener."""
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://127.0.0.1:8888")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _FakeHTTPResponse(200, b'{"results":[]}')

        monkeypatch.setattr(ws, "_open", fake_urlopen)
        ws.search("hello")
        assert captured["timeout"] == 5


class TestEgressEnforcement:
    """The tool must ONLY contact the configured searxng host."""

    def test_only_contacts_configured_searxng_host(self, monkeypatch):
        monkeypatch.setenv("ACE_SEARXNG_URL", "http://searx.example.com:8888")
        recorder = SocketRecorder().install(monkeypatch)

        # Patch urlopen (not ws._open) so the real HTTPConnection is
        # constructed — the recorder sees the host — but no real socket I/O
        # occurs. We stub the connection's request/response instead.
        import http.client as hc

        def fake_urlopen(req, timeout=None, context=None):
            # Build the connection (recorder fires on __init__), then short-
            # circuit the actual HTTP round-trip.
            conn = hc.HTTPConnection(req.host, timeout=timeout or 5)
            return _FakeHTTPResponse(200, b'{"results":[]}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        ws.search("hello")
        # The only host contacted is the searxng.
        hosts = {h for h, _ in recorder.connections}
        assert "searx.example.com" in hosts
        # And nothing else.
        assert hosts == {"searx.example.com"}

    def test_default_url_is_localhost_8888(self, monkeypatch):
        # No env var set -> default.
        monkeypatch.delenv("ACE_SEARXNG_URL", raising=False)
        assert ws.searxng_url() == "http://127.0.0.1:8888"
