"""T2 tests: github_lookup tool (engine/intent/github.py).

github_lookup is the P3 egress tool: read-only GitHub REST API via
https://api.github.com. Token from env ACE_GITHUB_TOKEN (optional; unauthenticated
rate-limited fallback). ONLY three endpoints are exposed:
  * repo metadata   -> GET /repos/{owner}/{repo}
  * file contents   -> GET /repos/{owner}/{repo}/contents/{path}
  * issue search    -> GET /search/issues?q=...

The token is read from env and NEVER logged. Same telemetry contract as T1
(events feed G11 on failure). Availability-gated: if api.github.com is
unreachable the tool returns ok=False with a clear error, never raises.

These tests mock ALL network. Target: >=10 tests.
"""

import base64
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent import github as gh


class _FakeResp:
    def __init__(self, status, body_bytes, reason="OK"):
        self.status = status
        self._body = body_bytes
        self.reason = reason
        self.code = status
        self.headers = {"X-RateLimit-Remaining": "4999"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(body):
    return _FakeResp(200, json.dumps(body).encode())


class TestAvailability:
    def test_available_true_when_api_healthy(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeResp(200, b'{"current_user_url":"x"}')
        monkeypatch.setattr(gh, "_open", fake_urlopen)
        assert gh.available() is True

    def test_available_false_when_down(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise ConnectionError("refused")
        monkeypatch.setattr(gh, "_open", fake_urlopen)
        assert gh.available() is False


class TestTokenHandling:
    def test_token_read_from_env(self, monkeypatch):
        monkeypatch.setenv("ACE_GITHUB_TOKEN", "ghp_fake123")
        assert gh._token() == "ghp_fake123"

    def test_token_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("ACE_GITHUB_TOKEN", raising=False)
        assert gh._token() == ""

    def test_token_never_in_telemetry(self, monkeypatch):
        monkeypatch.setenv("ACE_GITHUB_TOKEN", "ghp_secret")
        events = []
        monkeypatch.setattr(gh, "_emit_telemetry", lambda e: events.append(e))

        def fake_urlopen(req, timeout=None):
            return _FakeResp(200, b'{"results":[]}')
        monkeypatch.setattr(gh, "_open", fake_urlopen)
        gh.search_issues("repo:foo/bar is:open")
        for ev in events:
            assert "ghp_secret" not in json.dumps(ev), "token leaked into telemetry"


class TestRepoMetadata:
    def test_repo_returns_normalized_fields(self, monkeypatch):
        payload = {
            "full_name": "python/cpython",
            "description": "The Python language.",
            "stargazers_count": 50000,
            "language": "Python",
            "html_url": "https://github.com/python/cpython",
        }

        def fake_urlopen(req, timeout=None):
            return _ok(payload)
        monkeypatch.setattr(gh, "_open", fake_urlopen)

        res = gh.repo("python", "cpython")
        assert res["ok"] is True
        assert res["full_name"] == "python/cpython"
        assert res["stargazers_count"] == 50000
        assert "description" in res

    def test_repo_404_returns_ok_false(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeResp(404, b'{"message":"Not Found"}')
        monkeypatch.setattr(gh, "_open", fake_urlopen)
        res = gh.repo("no", "such")
        assert res["ok"] is False
        assert "404" in res["error"]


class TestFileContents:
    def test_contents_decodes_base64(self, monkeypatch):
        raw = "# hello\nprint('hi')\n"
        payload = {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(raw.encode()).decode(),
            "name": "main.py",
            "path": "main.py",
        }

        def fake_urlopen(req, timeout=None):
            return _ok(payload)
        monkeypatch.setattr(gh, "_open", fake_urlopen)

        res = gh.contents("owner", "repo", "main.py")
        assert res["ok"] is True
        assert res["decoded"] == raw
        assert res["name"] == "main.py"

    def test_contents_404_returns_ok_false(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeResp(404, b'{"message":"Not Found"}')
        monkeypatch.setattr(gh, "_open", fake_urlopen)
        res = gh.contents("owner", "repo", "nope.py")
        assert res["ok"] is False


class TestIssueSearch:
    def test_search_returns_items(self, monkeypatch):
        payload = {
            "total_count": 2,
            "items": [
                {"number": 1, "title": "Bug", "html_url": "https://x/1"},
                {"number": 2, "title": "Feature", "html_url": "https://x/2"},
            ],
        }

        def fake_urlopen(req, timeout=None):
            return _ok(payload)
        monkeypatch.setattr(gh, "_open", fake_urlopen)

        res = gh.search_issues("repo:foo/bar is:open")
        assert res["ok"] is True
        assert res["total_count"] == 2
        assert len(res["items"]) == 2
        assert res["items"][0]["title"] == "Bug"

    def test_search_caps_items(self, monkeypatch):
        """Result item list is capped to bound context injection."""
        payload = {
            "total_count": 100,
            "items": [{"number": i, "title": f"Issue {i}"} for i in range(100)],
        }

        def fake_urlopen(req, timeout=None):
            return _ok(payload)
        monkeypatch.setattr(gh, "_open", fake_urlopen)

        res = gh.search_issues("repo:foo/bar")
        assert len(res["items"]) <= gh.MAX_ISSUES


class TestTelemetry:
    def test_failure_emits_telemetry(self, monkeypatch):
        events = []
        monkeypatch.setattr(gh, "_emit_telemetry", lambda e: events.append(e))

        def fake_urlopen(req, timeout=None):
            raise ConnectionError("refused")
        monkeypatch.setattr(gh, "_open", fake_urlopen)

        res = gh.repo("a", "b")
        assert res["ok"] is False
        assert len(events) == 1
        assert events[0]["status"] == "failure"

    def test_success_emits_telemetry(self, monkeypatch):
        events = []
        monkeypatch.setattr(gh, "_emit_telemetry", lambda e: events.append(e))

        def fake_urlopen(req, timeout=None):
            return _ok({"full_name": "a/b"})
        monkeypatch.setattr(gh, "_open", fake_urlopen)

        gh.repo("a", "b")
        assert len(events) == 1
        assert events[0]["status"] == "success"


class TestEgressEnforcement:
    def test_only_contacts_api_github_com(self, monkeypatch):
        """github_lookup must ONLY contact api.github.com."""
        import http.client as hc
        hosts = []

        # Record host at __init__ time (no real socket needed). Patch the
        # connection's network methods so no actual I/O occurs.
        orig_init = hc.HTTPSConnection.__init__

        def recording_init(self_conn, host, port=None, *a, **kw):
            hosts.append(host)
            return orig_init(self_conn, host, port, *a, **kw)

        monkeypatch.setattr(hc.HTTPSConnection, "__init__", recording_init)
        monkeypatch.setattr(
            hc.HTTPSConnection, "request",
            lambda self, *a, **kw: None,
        )
        monkeypatch.setattr(
            hc.HTTPSConnection, "getresponse",
            lambda self: _ok({"full_name": "a/b"}),
        )

        gh.repo("a", "b")
        assert hosts == ["api.github.com"]
