from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Iterable

from service_club.core.runtime.event_stream import (
    append_outbox_event,
    ensure_event_outbox_schema,
)
from service_club.storage.relational import RelationalBackend


class KnowledgeBaseStore:
    """PostgreSQL-canonical documents with Elasticsearch chunk projection."""

    def __init__(self, backend: RelationalBackend, search_index: Any) -> None:
        self.backend = backend
        self.search_index = search_index
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.backend.connect() as conn:
            ensure_event_outbox_schema(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY,
                    knowledge_base TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_uri TEXT NOT NULL DEFAULT '',
                    checksum TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    UNIQUE(knowledge_base, checksum)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    knowledge_base TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    UNIQUE(document_id, chunk_index)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_base_document ON knowledge_chunks(knowledge_base, document_id, chunk_index)"
            )

    def ingest_text(
        self,
        knowledge_base: str,
        *,
        title: str,
        text: str,
        source_uri: str = "",
        metadata_json: str = "{}",
        chunk_chars: int = 1800,
        overlap_chars: int = 180,
    ) -> dict[str, Any]:
        base = knowledge_base.strip()
        content = text.strip()
        if not base or not content:
            raise ValueError("knowledge_base 和 text 不能为空。")
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chunks = list(_split_text(content, chunk_chars=chunk_chars, overlap_chars=overlap_chars))
        now = time.time()
        document_id = uuid.uuid4().hex
        indexed_rows: list[dict[str, Any]] = []
        with self.backend.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM knowledge_documents WHERE knowledge_base = ? AND checksum = ?",
                (base, checksum),
            ).fetchone()
            if existing is not None:
                return {"id": str(existing["id"]), "knowledge_base": base, "chunk_count": len(chunks), "deduplicated": True}
            conn.execute(
                """
                INSERT INTO knowledge_documents(
                    id, knowledge_base, title, source_uri, checksum,
                    metadata_json, chunk_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, base, title.strip(), source_uri.strip(), checksum, metadata_json, len(chunks), now, now),
            )
            for index, chunk in enumerate(chunks):
                chunk_id = f"{document_id}:{index}"
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks(
                        id, document_id, knowledge_base, chunk_index,
                        content, token_estimate, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chunk_id, document_id, base, index, chunk, max(1, len(chunk) // 3), now, now),
                )
                row = {
                    "id": chunk_id,
                    "document_id": document_id,
                    "knowledge_base": base,
                    "chunk_index": index,
                    "title": title.strip(),
                    "content": chunk,
                    "source_uri": source_uri.strip(),
                    "updated_at": now,
                }
                indexed_rows.append(row)
                append_outbox_event(
                    conn,
                    event_type="knowledge.chunk_created",
                    aggregate_type="knowledge_chunk",
                    aggregate_id=chunk_id,
                    partition_key=document_id,
                    payload=row,
                )
        for row in indexed_rows:
            try:
                self.search_index.upsert_document_chunk(row)
            except Exception:
                pass
        return {"id": document_id, "knowledge_base": base, "chunk_count": len(chunks), "deduplicated": False}

    def search(self, knowledge_base: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.search_index.search_documents(knowledge_base, query, limit=limit)

    def delete_document(self, knowledge_base: str, document_id: str) -> bool:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT id FROM knowledge_documents WHERE id = ? AND knowledge_base = ?",
                (document_id, knowledge_base),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
            append_outbox_event(
                conn,
                event_type="knowledge.document_deleted",
                aggregate_type="knowledge_document",
                aggregate_id=document_id,
                partition_key=document_id,
                payload={"document_id": document_id, "knowledge_base": knowledge_base},
            )
        try:
            self.search_index.delete_document(document_id)
        except Exception:
            pass
        return True

    def iter_chunks(self, *, batch_size: int = 500) -> Iterable[dict[str, Any]]:
        last_id = ""
        while True:
            with self.backend.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT c.id, c.document_id, c.knowledge_base, c.chunk_index,
                           c.content, c.updated_at, d.title, d.source_uri
                    FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.id = c.document_id
                    WHERE c.id > ? ORDER BY c.id LIMIT ?
                    """,
                    (last_id, max(1, min(batch_size, 5000))),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                item = dict(row)
                yield item
                last_id = str(item["id"])

    def status(self) -> dict[str, Any]:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS documents, COALESCE(SUM(chunk_count), 0) AS chunks FROM knowledge_documents"
            ).fetchone()
        return {
            "ok": True,
            "fact_backend": self.backend.name,
            "search_backend": "elasticsearch",
            "documents": int(row["documents"]) if row else 0,
            "chunks": int(row["chunks"]) if row else 0,
        }


def _split_text(text: str, *, chunk_chars: int, overlap_chars: int) -> Iterable[str]:
    size = max(400, min(int(chunk_chars), 8000))
    overlap = max(0, min(int(overlap_chars), size // 3))
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start + size // 2, end), text.rfind("。", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        if end >= len(text):
            return
        start = max(start + 1, end - overlap)
