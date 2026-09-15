"""engine/mock_transport.py — Deterministic mock transport with transcript replay.

Replays recorded BeeLlama responses from a JSON transcript fixture,
producing byte-identical tokens with zero network and zero GPU.

Recording mode (RECORD=1) captures real responses; replay mode consumes them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, Optional

log = logging.getLogger("engine.mock_transport")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request_hash(messages: list[dict], max_tokens: int, temperature: float) -> str:
    """Deterministic SHA-256 hash for a request tuple."""
    key = json.dumps(messages, sort_keys=True) + "|" + str(max_tokens) + "|" + str(temperature)
    return hashlib.sha256(key.encode()).hexdigest()


def _fixture_path_for(prd_name: str, golden_dir: str = "tests/golden") -> str:
    """Canonical fixture path for a given PRD name."""
    return os.path.join(golden_dir, f"{prd_name}.transcript.json")


# ---------------------------------------------------------------------------
# MockTransport
# ---------------------------------------------------------------------------

class MockTransport:
    """Deterministic mock transport that replays recorded BeeLlama responses.

    REPLAY mode: every ``curl_beellama`` call computes a SHA-256 hash of
    the request and looks it up in the pre-built index.  Returns the
    recorded response verbatim, or raises ``KeyError`` if not found.

    RECORD mode: delegates to a real transport, records each exchange,
    and appends it to the fixture file for future replay.

    Other methods return canned values in replay mode and delegate in
    record mode.
    """

    def __init__(
        self,
        fixture_path: str,
        real_transport: Any = None,
        record_mode: bool = False,
    ) -> None:
        self.fixture_path = fixture_path
        self.real_transport = real_transport
        self.record_mode = record_mode
        self._lock = threading.Lock()
        self._index: dict[str, dict] = self._load_and_index()

    # -- Transport interface -------------------------------------------------

    def curl_beellama(
        self,
        port: int,
        messages: list[dict],
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> dict:
        """Replay or record a BeeLlama chat-completion call."""
        if self.record_mode:
            if self.real_transport is None:
                raise RuntimeError("record mode requires a real_transport")
            response = self.real_transport.curl_beellama(
                port, messages, max_tokens=max_tokens, temperature=temperature,
            )
            self._append_to_fixture(messages, max_tokens, temperature, response)
            return response
        h = _request_hash(messages, max_tokens, temperature)
        if h in self._index:
            return self._index[h]
        raise KeyError(
            f"No recorded response for hash {h}. Available: {len(self._index)} entries."
        )

    def check_beellama_health(self, port: int) -> bool:
        """REPLAY: ``True``.  RECORD: delegates to real transport."""
        if self.record_mode and self.real_transport is not None:
            return self.real_transport.check_beellama_health(port)
        return True

    def get_model_list(self, port: int) -> list[str]:
        """REPLAY: ``['mock-model']``.  RECORD: delegates to real transport."""
        if self.record_mode and self.real_transport is not None:
            return self.real_transport.get_model_list(port)
        return ["mock-model"]

    def run_command(
        self, cmd: str, timeout: int = 60, cwd: Optional[str] = None,
    ) -> tuple[str, str, int]:
        """REPLAY: ``("", "", 0)``.  RECORD: delegates to real transport."""
        if self.record_mode and self.real_transport is not None:
            return self.real_transport.run_command(cmd, timeout=timeout, cwd=cwd)
        return ("", "", 0)

    # -- Internal helpers ----------------------------------------------------

    def _load_and_index(self) -> dict[str, dict]:
        """Load fixture JSON and build hash→response index."""
        try:
            with open(self.fixture_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            log.debug("Could not load fixture %s: %s", self.fixture_path, exc)
            return {}
        if not isinstance(data, list):
            log.warning("Fixture %s is not a JSON array; ignoring", self.fixture_path)
            return {}
        index: dict[str, dict] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            msgs = entry.get("messages")
            mt = entry.get("max_tokens", 512)
            temp = entry.get("temperature", 0.3)
            resp = entry.get("response")
            if msgs is None or resp is None:
                continue
            h = _request_hash(msgs, mt, temp)
            index[h] = resp
        return index

    def _append_to_fixture(
        self, messages: list[dict], max_tokens: int, temperature: float, response: dict,
    ) -> None:
        """Append one exchange to the fixture JSON file (thread-safe)."""
        entry = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response": response,
        }
        with self._lock:
            existing: list[dict] = []
            try:
                with open(self.fixture_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                existing = []
            existing.append(entry)
            tmp_path = self.fixture_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, self.fixture_path)
            h = _request_hash(messages, max_tokens, temperature)
            self._index[h] = response


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_mock_transport(fixture_path: str, record: bool = False) -> MockTransport:
    """Create a ``MockTransport`` instance.

    If *record* is ``True`` or ``RECORD=1`` env var is set, a real
    transport is lazily imported and the mock runs in record mode.
    """
    if record or os.environ.get("RECORD") == "1":
        from transport import get_transport  # type: ignore[import-untyped]
        real = get_transport()
        return MockTransport(fixture_path, real_transport=real, record_mode=True)
    return MockTransport(fixture_path, record_mode=False)
