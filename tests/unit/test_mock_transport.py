"""Unit tests for engine.mock_transport — deterministic mock transport."""

import json
import os
import sys

import pytest

# Ensure coder-harness root is on sys.path so engine.* is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.mock_transport import MockTransport, _request_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EXCHANGE_A = {
    "messages": [{"role": "user", "content": "Generate code for health check"}],
    "max_tokens": 512,
    "temperature": 0.3,
    "response": {
        "choices": [{"message": {"content": "def health_check(): ..."}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 256},
        "timings": {"predicted_per_second": 150.0, "predicted_ms": 1000.0},
    },
}

EXCHANGE_B = {
    "messages": [{"role": "user", "content": "Generate code for auth middleware"}],
    "max_tokens": 1024,
    "temperature": 0.7,
    "response": {
        "choices": [{"message": {"content": "def auth_middleware(): ..."}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 512},
        "timings": {"predicted_per_second": 120.0, "predicted_ms": 2000.0},
    },
}


def _write_fixture(path, exchanges):
    """Write a list of exchanges to a JSON fixture file."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(exchanges, fh, indent=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRequestHash:
    def test_deterministic(self):
        """Same inputs produce same hash."""
        msgs = [{"role": "user", "content": "hello"}]
        h1 = _request_hash(msgs, 512, 0.3)
        h2 = _request_hash(msgs, 512, 0.3)
        assert h1 == h2
        assert isinstance(h1, str)
        assert len(h1) == 64  # SHA-256 hex digest

    def test_different_messages(self):
        """Different messages produce different hashes."""
        h1 = _request_hash([{"role": "user", "content": "a"}], 512, 0.3)
        h2 = _request_hash([{"role": "user", "content": "b"}], 512, 0.3)
        assert h1 != h2

    def test_different_max_tokens(self):
        """Different max_tokens produce different hashes."""
        msgs = [{"role": "user", "content": "x"}]
        h1 = _request_hash(msgs, 512, 0.3)
        h2 = _request_hash(msgs, 1024, 0.3)
        assert h1 != h2

    def test_different_temperature(self):
        """Different temperature produces different hashes."""
        msgs = [{"role": "user", "content": "x"}]
        h1 = _request_hash(msgs, 512, 0.3)
        h2 = _request_hash(msgs, 512, 0.7)
        assert h1 != h2

    def test_message_order_independent(self):
        """Message key order does not affect hash (sort_keys=True)."""
        h1 = _request_hash([{"role": "user", "content": "hi"}], 512, 0.3)
        h2 = _request_hash([{"content": "hi", "role": "user"}], 512, 0.3)
        assert h1 == h2


class TestMockTransportReplay:
    def test_curl_beellama_returns_correct_response(self, tmp_path):
        """curl_beellama returns the recorded response for a matching request."""
        fixture = tmp_path / "test.transcript.json"
        _write_fixture(fixture, [EXCHANGE_A, EXCHANGE_B])
        t = MockTransport(str(fixture))

        resp = t.curl_beellama(
            port=8080,
            messages=EXCHANGE_A["messages"],
            max_tokens=EXCHANGE_A["max_tokens"],
            temperature=EXCHANGE_A["temperature"],
        )
        assert resp["choices"][0]["message"]["content"] == "def health_check(): ..."
        assert resp["usage"]["total_tokens"] == 256

    def test_curl_beellama_raises_keyerror_for_unknown(self, tmp_path):
        """curl_beellama raises KeyError for an unknown request."""
        fixture = tmp_path / "test.transcript.json"
        _write_fixture(fixture, [EXCHANGE_A])
        t = MockTransport(str(fixture))

        with pytest.raises(KeyError, match="No recorded response for hash"):
            t.curl_beellama(
                port=8080,
                messages=[{"role": "user", "content": "unknown request"}],
                max_tokens=999,
                temperature=0.99,
            )

    def test_empty_fixture(self, tmp_path):
        """An empty array fixture creates an empty index (KeyError on lookup)."""
        fixture = tmp_path / "empty.json"
        _write_fixture(fixture, [])
        t = MockTransport(str(fixture))
        with pytest.raises(KeyError):
            t.curl_beellama(8080, [{"role": "user", "content": "x"}])

    def test_missing_fixture_file(self, tmp_path):
        """A missing fixture file is handled gracefully (empty index)."""
        t = MockTransport(str(tmp_path / "nonexistent.json"))
        with pytest.raises(KeyError):
            t.curl_beellama(8080, [{"role": "user", "content": "x"}])

    def test_check_beellama_health_returns_true(self, tmp_path):
        """check_beellama_health returns True in replay mode."""
        fixture = tmp_path / "test.json"
        _write_fixture(fixture, [])
        t = MockTransport(str(fixture))
        assert t.check_beellama_health(8080) is True

    def test_get_model_list_returns_mock(self, tmp_path):
        """get_model_list returns ['mock-model'] in replay mode."""
        fixture = tmp_path / "test.json"
        _write_fixture(fixture, [])
        t = MockTransport(str(fixture))
        assert t.get_model_list(8080) == ["mock-model"]

    def test_run_command_returns_canned(self, tmp_path):
        """run_command returns ('', '', 0) in replay mode."""
        fixture = tmp_path / "test.json"
        _write_fixture(fixture, [])
        t = MockTransport(str(fixture))
        stdout, stderr, rc = t.run_command("echo hello")
        assert stdout == ""
        assert stderr == ""
        assert rc == 0


class TestFixturePathFor:
    def test_default_golden_dir(self):
        from engine.mock_transport import _fixture_path_for
        result = _fixture_path_for("my-prd")
        assert result == os.path.join("tests/golden", "my-prd.transcript.json")

    def test_custom_golden_dir(self):
        from engine.mock_transport import _fixture_path_for
        result = _fixture_path_for("my-prd", golden_dir="/tmp/golden")
        assert result == "/tmp/golden/my-prd.transcript.json"
