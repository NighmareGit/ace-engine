"""Tests for engine/retrieval/vram_scheduler.py — PrepStage."""

from __future__ import annotations

import json

import pytest

from engine.retrieval.vram_scheduler import (
    PrepStage, ModelHandle, _StdlibTransport,
)


class MockTransport:
    """Records call sequence and returns fixture embeddings."""

    def __init__(self, embeddings=None):
        self.calls: list[tuple[str, dict]] = []
        self._embeddings = embeddings or [[0.1, 0.2, 0.3]]

    def post(self, url, body, timeout=30.0):
        payload = json.loads(body)
        self.calls.append((url, payload))
        return {"data": [{"embedding": e} for e in self._embeddings]}


class TestPrepStageLifecycle:
    """embed_batch = load → embed → unload lifecycle."""

    def test_embed_batch_returns_embeddings(self):
        t = MockTransport([[0.1, 0.2], [0.3, 0.4]])
        stage = PrepStage("http://localhost:8080", transport=t)
        result = stage.embed_batch("model.gguf", ["hello", "world"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    def _embed_call(self, calls):
        """Return the (url, payload) for the /v1/embeddings call."""
        return next((c for c in calls if "/v1/embeddings" in c[0]), None)

    def test_embed_batch_calls_embeddings_endpoint(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        stage.embed_batch("model.gguf", ["text"])
        embed = self._embed_call(t.calls)
        assert embed is not None
        assert embed[0] == "http://localhost:8080/v1/embeddings"

    def test_embed_batch_passes_model_name(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        stage.embed_batch("bge-small.gguf", ["text"])
        embed = self._embed_call(t.calls)
        assert embed[1]["model"] == "bge-small.gguf"

    def test_embed_batch_passes_input_texts(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        stage.embed_batch("m", ["a", "b"])
        embed = self._embed_call(t.calls)
        assert embed[1]["input"] == ["a", "b"]

    def test_load_returns_handle(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        h = stage.load("model.gguf")
        assert isinstance(h, ModelHandle)
        assert h.model_name == "model.gguf"
        assert h.base_url == "http://localhost:8080"

    def test_unload_posts_to_models_unload(self):
        """unload() must POST to /models/unload with the model name
        (the llama.cpp-sanctioned VRAM release mechanism)."""
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        h = stage.load("model.gguf")
        stage.unload(h)
        unload_calls = [c for c in t.calls if "/models/unload" in c[0]]
        assert len(unload_calls) == 1
        assert unload_calls[0][1]["model"] == "model.gguf"

    def test_embed_batch_unload_sequence(self):
        """embed_batch must call load → embed → unload in order."""
        t = MockTransport([[0.1, 0.2]])
        stage = PrepStage("http://localhost:8080", transport=t)
        stage.embed_batch("m.gguf", ["text"])
        urls = [c[0] for c in t.calls]
        assert any("/models/load" in u for u in urls)
        assert any("/v1/embeddings" in u for u in urls)
        assert any("/models/unload" in u for u in urls)
        # Unload must come after embed.
        embed_idx = next(i for i, u in enumerate(urls) if "/v1/embeddings" in u)
        unload_idx = next(i for i, u in enumerate(urls) if "/models/unload" in u)
        assert unload_idx > embed_idx

    def test_swap_for_judge_returns_judge_handle(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        h = stage.swap_for_judge("judge.gguf", "orch.gguf")
        assert h.model_name == "judge.gguf"
        # Orchestrator must be unloaded before judge is loaded.
        unload_calls = [c for c in t.calls if "/models/unload" in c[0]]
        load_calls = [c for c in t.calls if "/models/load" in c[0]]
        assert len(unload_calls) == 1
        assert unload_calls[0][1]["model"] == "orch.gguf"
        assert len(load_calls) == 1
        assert load_calls[0][1]["model"] == "judge.gguf"

    def test_embed_empty_texts_returns_empty(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        h = stage.load("m")
        assert stage.embed(h, []) == []

    def test_embed_batch_empty_texts_returns_empty(self):
        t = MockTransport()
        stage = PrepStage("http://localhost:8080", transport=t)
        assert stage.embed_batch("m", []) == []


class TestPrepStageResponseShapes:
    """Handle different llama.cpp response formats."""

    def test_embeddings_key_format(self):
        """Some builds return {'embeddings': [...]}."""
        class T:
            def post(self, url, body, timeout=30.0):
                return {"embeddings": [[0.5, 0.6]]}
        stage = PrepStage("http://x", transport=T())
        h = stage.load("m")
        result = stage.embed(h, ["hi"])
        assert result == [[0.5, 0.6]]

    def test_single_embedding_key_format(self):
        """Some builds return {'embedding': [...]}."""
        class T:
            def post(self, url, body, timeout=30.0):
                return {"embedding": [0.7, 0.8]}
        stage = PrepStage("http://x", transport=T())
        h = stage.load("m")
        result = stage.embed(h, ["hi"])
        assert result == [[0.7, 0.8]]
