"""Tests for engine/retrieval/embeddings.py — EmbeddingStore."""

from __future__ import annotations

import sqlite3
import pytest

from engine.retrieval.embeddings import EmbeddingStore
from engine.retrieval.repo_map import RepoMap, Def


class MockPrepStage:
    def embed_batch(self, model, texts):
        return [[0.1 * (i + 1), 0.2, 0.3] for i in range(len(texts))]


class _TrackingPrepStage:
    """Mock that returns a deterministic, unique embedding per text so we
    can recover which text produced each stored row."""

    def embed_batch(self, model, texts):
        return [[float(hash(t) % 1000), 0.2, 0.3] for t in texts]


class TestGracefulDegradation:
    """Without sqlite-vec, store disables itself gracefully."""

    def test_ingest_returns_zero_without_sqlite_vec(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        store = EmbeddingStore(db_path, MockPrepStage())
        if not store._vec_available:
            rm = RepoMap()
            rm.add_def(Def("foo", "function", 1, 5, "test.py"))
            assert store.ingest(rm) == 0

    def test_query_returns_empty_without_sqlite_vec(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        store = EmbeddingStore(db_path, MockPrepStage())
        if not store._vec_available:
            assert store.query("question") == []

    def test_never_raises_without_sqlite_vec(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        store = EmbeddingStore(db_path, MockPrepStage())
        rm = RepoMap()
        rm.add_def(Def("foo", "function", 1, 5, "test.py"))
        # Should not raise
        store.ingest(rm)
        store.query("test")


class TestEmbeddingStoreInit:
    """Store initializes DB schema on construction."""

    def test_creates_embedding_chunks_table(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        EmbeddingStore(db_path, MockPrepStage())
        import sqlite3
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "embedding_chunks" in tables


class TestSerialization:
    """Float vector serialization round-trip."""

    def test_serialize_deserialize_roundtrip(self):
        emb = [0.1, 0.2, 0.3, 0.4]
        blob = EmbeddingStore._serialize(emb)
        recovered = EmbeddingStore._deserialize(blob)
        assert len(recovered) == 4
        for a, b in zip(emb, recovered):
            assert abs(a - b) < 1e-6


class TestIngestChunkTextIntegrity:
    """Regression: stored chunk_text must match the per-def source text,
    not the batch-start text (G2 M1 bug)."""

    def test_ingest_stores_correct_chunk_text_across_batches(
            self, tmp_path):
        """Ingest 40 defs with batch_size=32 — the second batch (items
        32..39) must store each def's own text, not the first batch's."""
        db_path = str(tmp_path / "test.db")
        stage = _TrackingPrepStage()
        store = EmbeddingStore(db_path, stage)
        if not store._vec_available:
            pytest.skip("sqlite-vec not installed")

        # Create 40 real source files with distinct, known content.
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        rm = RepoMap()
        expected: dict[str, str] = {}  # symbol_name -> expected text
        for i in range(40):
            f = src_dir / f"mod_{i}.py"
            body = f"def func_{i}(x):\n    return x + {i}\n"
            f.write_text(body)
            # Read exactly as ingest() does (1-indexed lines).
            lines = body.splitlines(keepends=True)
            text = "".join(lines[0:2])  # both lines
            expected[f"func_{i}"] = text
            rm.add_def(Def(f"func_{i}", "function", 1, 2, str(f)))

        n = store.ingest(rm, batch_size=32)
        assert n == 40

        # Verify every stored chunk_text matches its def's source text.
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT symbol_name, chunk_text FROM embedding_chunks").fetchall()
        conn.close()
        assert len(rows) == 40
        for symbol_name, chunk_text in rows:
            assert chunk_text == expected[symbol_name], (
                f"chunk_text for {symbol_name} corrupted: "
                f"got {chunk_text!r}, expected {expected[symbol_name]!r}")
