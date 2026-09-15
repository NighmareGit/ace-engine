"""EmbeddingStore — sqlite-vec ingest + query (conditional on Phase 0 gate).

This module ships ONLY if G2-T01 Phase 0 data shows repo_map_recall@10 < 0.85
(the pre-registered threshold).  The gate is documented in the baseline
report; activation is a manual decision-point.

Provides:
    EmbeddingStore.ingest(repo_map) — embed function/class-level defs via
        PrepStage.embed_batch() and persist to the embedding_chunks table.
    EmbeddingStore.query(question, top_k) — embed the question, run ANN
        search via sqlite-vec, return top-k Chunks.

Graceful degradation: if the sqlite-vec extension is not installed, the
module logs a warning and disables itself (query returns [], ingest
returns 0).  Never crashes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
from typing import Optional

from engine.retrieval import prompt_assemble

Chunk = prompt_assemble.Chunk


logger = logging.getLogger(__name__)

# ── DDL (additive to engine.db) ─────────────────────────────────────────────

EMBEDDING_CHUNKS_DDL = """\
CREATE TABLE IF NOT EXISTS embedding_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_kind TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    embedding_model TEXT NOT NULL,
    embedded_at TEXT DEFAULT (datetime('now'))
)"""

EMBEDDING_CHUNKS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_emb_chunks_run ON embedding_chunks(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_emb_chunks_file ON embedding_chunks(file_path)",
]


def _sqlite_vec_available() -> bool:
    """Check if the sqlite-vec extension can be loaded."""
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _vec_ddl(dim: int) -> str:
    """Return DDL for the sqlite-vec virtual table with the given dimension."""
    return f"""\
CREATE VIRTUAL TABLE IF NOT EXISTS embedding_chunks_vec USING vec0(
    embedding FLOAT[{dim}]
)"""


# ── EmbeddingStore ───────────────────────────────────────────────────────────

class EmbeddingStore:
    """Ingest repo-map defs as embeddings and query them via ANN search.

    Usage::

        store = EmbeddingStore("engine.db", prep_stage)
        n = store.ingest(repo_map)        # embed + persist
        chunks = store.query("how does X work?", top_k=10)
    """

    def __init__(self, db_path: str, prep_stage) -> None:
        self._db_path = db_path
        self._prep_stage = prep_stage
        self._vec_available = _sqlite_vec_available()
        self._dim: Optional[int] = None
        self._init_db()

    def _init_db(self) -> None:
        """Idempotently create the embedding_chunks table and indexes."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(EMBEDDING_CHUNKS_DDL)
            for idx in EMBEDDING_CHUNKS_INDEXES:
                conn.execute(idx)
            conn.commit()
        finally:
            conn.close()

    def _get_dim(self, embedding: list[float]) -> int:
        """Infer embedding dimension, creating the vec virtual table if needed."""
        dim = len(embedding)
        if self._dim is None:
            self._dim = dim
            if self._vec_available:
                conn = sqlite3.connect(self._db_path)
                try:
                    conn.execute(_vec_ddl(dim))
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    logger.warning(
                        "sqlite-vec virtual table creation failed: %s. "
                        "Falling back to brute-force search.", exc)
                    self._vec_available = False
                finally:
                    conn.close()
        return dim

    @staticmethod
    def _serialize(embedding: list[float]) -> bytes:
        """Serialize a float vector to a little-endian float32 blob."""
        return struct.pack(f"<{len(embedding)}f", *embedding)

    @staticmethod
    def _deserialize(blob: bytes) -> list[float]:
        """Deserialize a float32 blob to a float vector."""
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))

    def ingest(self, repo_map, run_id: str = "default",
               model: str = "bge-small-en-v1.5",
               batch_size: int = 32) -> int:
        """Ingest function/class-level defs from *repo_map* into the store.

        Returns the number of chunks written.  If sqlite-vec is not
        available, logs a warning and returns 0 (graceful disable)."""
        if not self._vec_available:
            logger.warning(
                "sqlite-vec not installed — embedding ingest disabled. "
                "Install with: pip install sqlite-vec")
            return 0

        # Collect chunk texts from function/class defs.
        defs = [d for d in repo_map.defs if d.kind in ("function", "class")]
        if not defs:
            return 0

        chunk_texts = []
        chunk_meta = []
        for d in defs:
            # Read source range from disk.
            try:
                with open(d.file, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                text = "".join(
                    lines[d.start_line - 1:d.end_line])
            except OSError:
                text = f"{d.kind} {d.name}"
            chunk_texts.append(text)
            chunk_meta.append(d)

        # Batch-embed via PrepStage.
        written = 0
        conn = sqlite3.connect(self._db_path)
        try:
            for i in range(0, len(chunk_texts), batch_size):
                batch_texts = chunk_texts[i:i + batch_size]
                batch_meta = chunk_meta[i:i + batch_size]
                embeddings = self._prep_stage.embed_batch(model, batch_texts)
                for emb, d, text in zip(embeddings, batch_meta, batch_texts):
                    self._get_dim(emb)
                    conn.execute(
                        """INSERT INTO embedding_chunks
                            (run_id, file_path, symbol_name, symbol_kind,
                             line_start, line_end, chunk_text, embedding,
                             embedding_model)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (run_id, d.file, d.name, d.kind,
                         d.start_line, d.end_line, text,
                         self._serialize(emb), model),
                    )
                    written += 1
            conn.commit()
        finally:
            conn.close()
        return written

    def query(self, question: str, top_k: int = 10,
              model: str = "bge-small-en-v1.5") -> list[Chunk]:
        """Return the top-k chunks most relevant to *question*.

        Embeds the question via PrepStage, then runs ANN search via
        sqlite-vec (joined back to embedding_chunks for metadata).
        If sqlite-vec is not available, returns []."""
        if not self._vec_available:
            logger.warning("sqlite-vec not installed — query disabled.")
            return []

        # Embed the question.
        question_embedding = self._prep_stage.embed_batch(model, [question])
        if not question_embedding:
            return []
        q_emb = question_embedding[0]
        self._get_dim(q_emb)

        conn = sqlite3.connect(self._db_path)
        try:
            # Try sqlite-vec ANN search first.
            try:
                q_blob = self._serialize(q_emb)
                cur = conn.execute(
                    """SELECT ec.id, ec.file_path, ec.symbol_name,
                              ec.symbol_kind, ec.chunk_text, ec.embedding
                       FROM embedding_chunks_vec v
                       JOIN embedding_chunks ec ON ec.id = v.rowid
                       WHERE v.embedding MATCH ?
                       ORDER BY v.distance
                       LIMIT ?""",
                    (q_blob, top_k),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # Fallback: brute-force cosine similarity.
                rows = self._brute_force_query(conn, q_emb, top_k)

            return [
                Chunk(
                    text=row[4] or "",
                    score=0.0,  # distance, not similarity
                    source_file=row[1] or "",
                    symbol_name=row[2] or "",
                )
                for row in rows
            ]
        finally:
            conn.close()

    def _brute_force_query(
        self, conn, q_emb: list[float], top_k: int
    ) -> list[tuple]:
        """Fallback: brute-force cosine similarity over all stored embeddings."""
        cur = conn.execute(
            "SELECT id, file_path, symbol_name, symbol_kind, chunk_text, "
            "embedding FROM embedding_chunks")
        scored: list[tuple[float, tuple]] = []
        for row in cur.fetchall():
            emb = self._deserialize(row[5])
            sim = _cosine_similarity(q_emb, emb)
            scored.append((sim, row))
        scored.sort(key=lambda x: -x[0])
        return [row for _, row in scored[:top_k]]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
