from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from service_club.core.memory.hybrid_retrieval import HybridMemoryRetriever
from service_club.core.memory.knowledge_base import _split_text
from service_club.core.memory.search_index import SearchCandidate
from service_club.core.memory.vector_store import MilvusVectorStore
from service_club.core.runtime.event_stream import KafkaOutboxDispatcher, append_outbox_event
from service_club.storage.relational import PostgresRelationalBackend, configured_relational_backend


class _MemoryStore:
    def __init__(self) -> None:
        self.rows = {
            7: {"id": 7, "session_id": "s1", "content": "用户喜欢安静的咖啡馆", "source": "explicit", "importance": 0.8, "created_at": 1000.0},
            8: {"id": 8, "session_id": "s2", "content": "另一个会话的秘密", "source": "explicit", "importance": 1.0, "created_at": 1000.0},
        }
        self.backend = type("Backend", (), {"name": "postgresql"})()

    def memory_candidates_by_ids(self, session_id: str, memory_ids: list[int]):
        return [self.rows[value] for value in memory_ids if value in self.rows and self.rows[value]["session_id"] == session_id]

    def list_memory_candidates(self, session_id: str, limit: int):
        return [row for row in self.rows.values() if row["session_id"] == session_id][:limit]


class _SearchIndex:
    def search_memories(self, session_id: str, query: str, *, limit: int):
        del query, limit
        return [SearchCandidate(id=7, score=1.0, rank=1), SearchCandidate(id=8, score=0.9, rank=2)] if session_id == "s1" else []

    def status(self):
        return {"ok": True, "enabled": True, "backend": "elasticsearch"}

    def search_documents(self, knowledge_base: str, query: str, *, limit: int):
        del query, limit
        return [{
            "id": "doc-1:0",
            "document_id": "doc-1",
            "knowledge_base": knowledge_base,
            "title": "指南",
            "content": "安静环境有助于专注。",
            "source_uri": "guide.md",
            "score": 1.0,
        }]


class _RerankStore(_MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.rows[9] = {
            "id": 9,
            "session_id": "s1",
            "content": "用户正在准备考试，偏好安静环境",
            "source": "explicit",
            "importance": 0.7,
            "created_at": 1000.0,
        }


class _RerankIndex:
    def search_memories(self, session_id: str, query: str, *, limit: int):
        del query, limit
        if session_id != "s1":
            return []
        return [
            SearchCandidate(id=7, score=1.0, rank=1),
            SearchCandidate(id=9, score=0.9, rank=2),
        ]

    def status(self):
        return {"ok": True, "enabled": True, "backend": "elasticsearch"}


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))


class ScaledStorageTests(unittest.TestCase):
    def test_postgres_is_the_only_configured_runtime_backend(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "PostgreSQL"):
                configured_relational_backend()
        backend = configured_relational_backend(database_url="postgresql://u:p@db/yukino")
        self.assertIsInstance(backend, PostgresRelationalBackend)

    def test_elasticsearch_candidates_are_revalidated_by_session(self) -> None:
        retriever = HybridMemoryRetriever(_MemoryStore(), search_index=_SearchIndex(), clock=lambda: 1000.0)
        hits = retriever.search("s1", "安静咖啡馆", limit=5)
        self.assertEqual([hit.id for hit in hits], [7])
        self.assertGreater(hits[0].score_breakdown["rrf"], 0)

    def test_query_rewrite_and_llm_rerank_are_optional(self) -> None:
        responses = iter([
            '{"queries":["用户喜欢什么环境"]}',
            '{"scores":[{"idx":0,"score":9},{"idx":1,"score":2}]}',
        ])

        def generate(_system, _user, **_kwargs):
            return next(responses)

        retriever = HybridMemoryRetriever(
            _RerankStore(),
            search_index=_RerankIndex(),
            generate_fn=generate,
            clock=lambda: 1000.0,
        )
        hits = retriever.search("s1", "那个环境", limit=2)
        self.assertEqual([hit.id for hit in hits], [9, 7])
        self.assertIn("LLM 精排", hits[0].reasons)

    def test_query_rewrite_failure_keeps_original_retrieval(self) -> None:
        def generate(_system, _user, **_kwargs):
            raise RuntimeError("model unavailable")

        retriever = HybridMemoryRetriever(
            _MemoryStore(),
            search_index=_SearchIndex(),
            generate_fn=generate,
            clock=lambda: 1000.0,
        )
        hits = retriever.search("s1", "安静咖啡馆", limit=5)
        self.assertEqual([hit.id for hit in hits], [7])

    def test_knowledge_base_cross_base_search_keeps_provenance(self) -> None:
        from service_club.core.memory.knowledge_base import KnowledgeBaseStore

        class Backend:
            name = "postgresql"

            def connect(self):
                class Conn:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                    def execute(self, statement):
                        class Result:
                            def fetchall(self):
                                return [{"knowledge_base": "personal"}]

                        return Result()

                return Conn()

        store = KnowledgeBaseStore(Backend(), _SearchIndex())
        hits = store.search_all("专注", limit=2)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["knowledge_base"], "personal")
        self.assertEqual(hits[0]["source"], "guide.md")

    def test_document_chunking_has_overlap_and_no_empty_chunks(self) -> None:
        text = "甲" * 1200
        chunks = list(_split_text(text, chunk_chars=500, overlap_chars=50))
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(chunks))
        self.assertEqual(chunks[0][-50:], chunks[1][:50])

    def test_milvus_document_vectors_use_separate_collection_and_primary_key(self) -> None:
        class Client:
            def __init__(self):
                self.collections = set()
                self.created = []
                self.upserts = []

            def has_collection(self, *, collection_name):
                return collection_name in self.collections

            def create_collection(self, **kwargs):
                self.collections.add(kwargs["collection_name"])
                self.created.append(kwargs)

            def upsert(self, **kwargs):
                self.upserts.append(kwargs)

            def search(self, **_kwargs):
                return [[{"id": "doc-1:0", "distance": 0.91}]]

            def list_collections(self):
                return list(self.collections)

            def delete(self, **_kwargs):
                return None

        client = Client()
        store = MilvusVectorStore(
            "http://127.0.0.1:19530", client_factory=lambda **_kwargs: client
        )
        self.assertTrue(
            store.upsert_document_chunk(
                chunk_id="doc-1:0",
                knowledge_base="personal",
                document_id="doc-1",
                model="embed-v1",
                vector=[1.0, 0.0],
                updated_at=1.0,
            )
        )
        collection = client.created[0]
        self.assertIn("_document_", collection["collection_name"])
        self.assertEqual(collection["primary_field_name"], "chunk_id")
        self.assertEqual(collection["id_type"], "string")
        hits = store.search_document_chunks(
            knowledge_base="personal",
            model="embed-v1",
            query_vector=[1.0, 0.0],
            limit=3,
        )
        self.assertEqual(hits, {"doc-1:0": 0.91})

    def test_outbox_event_is_written_through_callers_transaction(self) -> None:
        connection = _Connection()
        append_outbox_event(
            connection,
            event_type="memory.created",
            aggregate_type="memory",
            aggregate_id="7",
            partition_key="s1",
            payload={"id": 7},
        )
        self.assertEqual(len(connection.calls), 1)
        self.assertIn("INSERT INTO event_outbox", connection.calls[0][0])
        self.assertEqual(connection.calls[0][1][1], "memory.created")


class KafkaNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_kafka_notification_wakes_local_websocket_queue(self) -> None:
        dispatcher = KafkaOutboxDispatcher(type("Backend", (), {})())
        queue = dispatcher.subscribe_local(__import__("asyncio").get_running_loop())
        dispatcher._notify_local("task-123")
        self.assertEqual(await __import__("asyncio").wait_for(queue.get(), 1), "task-123")
        dispatcher.unsubscribe_local(queue)


if __name__ == "__main__":
    unittest.main()
