from __future__ import annotations

import hashlib
import json
import math
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

    def __init__(
        self,
        backend: RelationalBackend,
        search_index: Any,
        generate_fn: Any | None = None,
        embedding_provider: Any | None = None,
        vector_store: Any | None = None,
    ) -> None:
        self.backend = backend
        self.search_index = search_index
        self.generate_fn = generate_fn
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
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
            for column, definition in (
                ("embedding_model", "TEXT NOT NULL DEFAULT ''"),
                ("embedding_dimensions", "INTEGER NOT NULL DEFAULT 0"),
                ("embedding_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("embedding", "vector"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS {column} {definition}"
                    )
                except Exception:
                    # Older PostgreSQL/compatibility backends can still use
                    # Elasticsearch-only retrieval when vector columns fail.
                    pass

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
        self._embed_and_project(indexed_rows)
        return {"id": document_id, "knowledge_base": base, "chunk_count": len(chunks), "deduplicated": False}

    def search(self, knowledge_base: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        queries = self._rewrite_queries(query)
        merged: dict[str, tuple[dict[str, Any], float]] = {}
        for rewritten in queries:
            for item, score in self._search_candidates(
                knowledge_base, rewritten, max(limit * 4, 20)
            ):
                key = str(item.get("id", item.get("chunk_id", item.get("content", ""))))
                previous = merged.get(key)
                merged[key] = (item, score if previous is None else previous[1] + score)
        hits = [
            item
            for item, _score in sorted(
                merged.values(), key=lambda pair: pair[1], reverse=True
            )[: max(limit * 4, limit)]
        ]
        return self._rerank(query, hits, limit)

    def _search_candidates(
        self, knowledge_base: str, query: str, limit: int
    ) -> list[tuple[dict[str, Any], float]]:
        lexical = self.search_index.search_documents(knowledge_base, query, limit=limit)
        lexical_by_id = {
            str(item.get("id", item.get("chunk_id", ""))): (rank, item)
            for rank, item in enumerate(lexical, start=1)
        }
        semantic_scores = self._semantic_candidates(knowledge_base, query, limit=limit)
        semantic_rows = self._load_chunks_by_ids(list(semantic_scores))
        semantic_by_id = {
            str(item["id"]): (rank, item)
            for rank, item in enumerate(
                sorted(semantic_rows, key=lambda row: semantic_scores.get(str(row["id"]), 0.0), reverse=True),
                start=1,
            )
        }
        ids = list(dict.fromkeys([*lexical_by_id, *semantic_by_id]))
        results: list[tuple[dict[str, Any], float]] = []
        for chunk_id in ids:
            lexical_rank, lexical_item = lexical_by_id.get(chunk_id, (0, None))
            semantic_rank, semantic_item = semantic_by_id.get(chunk_id, (0, None))
            item = lexical_item or semantic_item
            if item is None:
                continue
            score = (1.0 / (60 + lexical_rank) if lexical_rank else 0.0) + (
                1.0 / (60 + semantic_rank) if semantic_rank else 0.0
            )
            enriched = dict(item)
            enriched["semantic_score"] = semantic_scores.get(chunk_id, 0.0)
            enriched["score"] = score
            results.append((enriched, score))
        results.sort(key=lambda pair: pair[1], reverse=True)
        return results[:limit]

    def _semantic_candidates(
        self, knowledge_base: str, query: str, *, limit: int
    ) -> dict[str, float]:
        provider = self.embedding_provider
        if provider is None or not getattr(provider, "enabled", False):
            return {}
        try:
            query_vector = provider.embed([query])[0]
            if self.vector_store is not None:
                return {
                    str(key): float(value)
                    for key, value in self.vector_store.search_document_chunks(
                        knowledge_base=knowledge_base,
                        model=provider.model,
                        query_vector=query_vector,
                        limit=limit,
                    ).items()
                }
            with self.backend.connect() as conn:
                rows = conn.execute(
                    "SELECT id, embedding_json FROM knowledge_chunks "
                    "WHERE knowledge_base = ? AND embedding_model = ? AND embedding_json <> '[]' "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (knowledge_base, provider.model, max(limit * 20, 100)),
                ).fetchall()
            return {
                str(row["id"]): self._cosine(
                    query_vector, [float(value) for value in json.loads(str(row["embedding_json"]))]
                )
                for row in rows
            }
        except Exception:
            return {}

    def _load_chunks_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        clean = [str(value) for value in ids if value]
        if not clean:
            return []
        placeholders = ",".join("?" for _ in clean)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"SELECT c.id, c.document_id, c.knowledge_base, c.chunk_index, c.content, "
                f"d.title, d.source_uri FROM knowledge_chunks c "
                f"JOIN knowledge_documents d ON d.id = c.document_id WHERE c.id IN ({placeholders})",
                clean,
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "document_id": str(row["document_id"]),
                "knowledge_base": str(row["knowledge_base"]),
                "chunk_index": int(row["chunk_index"]),
                "content": str(row["content"]),
                "title": str(row["title"]),
                "source_uri": str(row["source_uri"]),
            }
            for row in rows
        ]

    def _embed_and_project(self, rows: list[dict[str, Any]]) -> None:
        provider = self.embedding_provider
        if provider is None or not getattr(provider, "enabled", False) or not rows:
            return
        try:
            vectors = provider.embed([str(row["content"]) for row in rows])
        except Exception:
            return
        for row, vector in zip(rows, vectors, strict=False):
            if not vector:
                continue
            encoded = json.dumps([float(value) for value in vector], separators=(",", ":"))
            try:
                with self.backend.connect() as conn:
                    conn.execute(
                        "UPDATE knowledge_chunks SET embedding_model = ?, embedding_dimensions = ?, "
                        "embedding_json = ? WHERE id = ?",
                        (provider.model, len(vector), encoded, row["id"]),
                    )
                    try:
                        conn.execute(
                            "UPDATE knowledge_chunks SET embedding = CAST(? AS vector) WHERE id = ?",
                            (encoded, row["id"]),
                        )
                    except Exception:
                        pass
            except Exception:
                continue
            if self.vector_store is not None:
                self.vector_store.upsert_document_chunk(
                    chunk_id=str(row["id"]),
                    knowledge_base=str(row["knowledge_base"]),
                    document_id=str(row["document_id"]),
                    model=provider.model,
                    vector=[float(value) for value in vector],
                    updated_at=float(row["updated_at"]),
                )

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        norm_left = math.sqrt(sum(value * value for value in left))
        norm_right = math.sqrt(sum(value * value for value in right))
        if not norm_left or not norm_right:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_left * norm_right)))

    def search_all(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Search every loaded knowledge base for the chat path.

        The public API keeps explicit knowledge-base isolation. Chat does not
        carry a base name, so it uses a bounded cross-base search and returns
        provenance for each chunk. Index failures are treated as a retrieval
        miss; they must not make ordinary chat unavailable.
        """
        if not query.strip():
            return []
        try:
            with self.backend.connect() as conn:
                bases = [
                    str(row["knowledge_base"])
                    for row in conn.execute(
                        "SELECT DISTINCT knowledge_base FROM knowledge_documents"
                    ).fetchall()
                ]
            merged: dict[str, dict[str, Any]] = {}
            for base in bases:
                for item in self.search(base, query, limit=max(limit * 2, 10)):
                    key = str(item.get("id", item.get("chunk_id", item.get("content", ""))))
                    merged.setdefault(
                        key,
                        {
                            **item,
                            "knowledge_base": base,
                            "source": item.get("source_uri", "") or base,
                        },
                    )
            ranked = sorted(
                merged.values(),
                key=lambda item: float(item.get("score", 0.0)),
                reverse=True,
            )
            return ranked[: max(1, min(int(limit), 20))]
        except Exception:
            return []

    def _rewrite_queries(self, query: str) -> list[str]:
        if self.generate_fn is None or not query.strip():
            return [query]
        try:
            raw = self.generate_fn(
                "你是知识库检索查询改写器，只输出合法 JSON。",
                "将问题改写成最多 3 条独立中文检索查询，不要回答。"
                '只输出 {"queries":["..."]}。\n问题：' + query,
                temperature=0.0,
                max_tokens=256,
            )
            values = json.loads(str(raw)).get("queries", [])
            return list(dict.fromkeys([query, *[str(v).strip() for v in values if str(v).strip()]]))[:3]
        except Exception:
            return [query]

    def _rerank(self, query: str, hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if self.generate_fn is None or len(hits) <= 1:
            return hits[:limit]
        candidates = hits[: max(limit * 4, limit)]
        prompt = "用户问题：" + query + "\n候选：\n" + "\n".join(
            f"[{index}] {str(item.get('content', ''))[:600]}"
            for index, item in enumerate(candidates)
        ) + '\n只输出 {"scores":[{"idx":0,"score":0}]}，score 为 0 到 10。'
        try:
            raw = self.generate_fn(
                "你是知识库检索精排器，只输出合法 JSON。",
                prompt,
                temperature=0.0,
                max_tokens=512,
            )
            scores = {
                int(item["idx"]): float(item["score"])
                for item in json.loads(str(raw)).get("scores", [])
                if isinstance(item, dict) and "idx" in item and "score" in item
            }
            if not scores:
                return hits[:limit]
            return [
                item
                for index, item in sorted(
                    enumerate(candidates),
                    key=lambda pair: (scores.get(pair[0], -1.0), float(pair[1].get("score", 0.0))),
                    reverse=True,
                )[:limit]
            ]
        except Exception:
            return hits[:limit]

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
        if self.vector_store is not None:
            try:
                self.vector_store.delete_document_chunks(document_id)
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

    def rebuild_vector_index(self) -> dict[str, Any]:
        """Rebuild document vectors from PostgreSQL's canonical embeddings."""
        if self.vector_store is None:
            return {"enabled": False, "backend": "relational", "indexed": 0, "failed": 0}
        indexed = 0
        failed = 0
        provider_model = (
            str(getattr(self.embedding_provider, "model", ""))
            if self.embedding_provider is not None
            else ""
        )
        with self.backend.connect() as conn:
            rows = conn.execute(
                "SELECT c.id, c.document_id, c.knowledge_base, c.embedding_model, "
                "c.embedding_json, c.updated_at FROM knowledge_chunks c "
                "WHERE c.embedding_json <> '[]'"
            ).fetchall()
        for row in rows:
            model = str(row["embedding_model"] or provider_model)
            if not model:
                failed += 1
                continue
            try:
                vector = [float(value) for value in json.loads(str(row["embedding_json"]))]
                ok = self.vector_store.upsert_document_chunk(
                    chunk_id=str(row["id"]),
                    knowledge_base=str(row["knowledge_base"]),
                    document_id=str(row["document_id"]),
                    model=model,
                    vector=vector,
                    updated_at=float(row["updated_at"]),
                )
                indexed += int(bool(ok))
                failed += int(not ok)
            except Exception:
                failed += 1
        return {
            "enabled": True,
            "backend": str(getattr(self.vector_store, "name", "milvus")),
            "indexed": indexed,
            "failed": failed,
        }

    def status(self) -> dict[str, Any]:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS documents, COALESCE(SUM(chunk_count), 0) AS chunks FROM knowledge_documents"
            ).fetchone()
        return {
            "ok": True,
            "fact_backend": self.backend.name,
            "search_backend": "elasticsearch",
            "embedding_enabled": bool(
                self.embedding_provider is not None
                and getattr(self.embedding_provider, "enabled", False)
            ),
            "embedding_model": (
                str(getattr(self.embedding_provider, "model", ""))
                if self.embedding_provider is not None
                else ""
            ),
            "vector_backend": (
                str(getattr(self.vector_store, "name", ""))
                if self.vector_store is not None
                else "relational"
            ),
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
