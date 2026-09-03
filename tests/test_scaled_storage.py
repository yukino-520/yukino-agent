from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from service_club.core.memory.hybrid_retrieval import HybridMemoryRetriever
from service_club.core.memory.knowledge_base import _split_text
from service_club.core.memory.search_index import SearchCandidate
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

    def test_document_chunking_has_overlap_and_no_empty_chunks(self) -> None:
        text = "甲" * 1200
        chunks = list(_split_text(text, chunk_chars=500, overlap_chars=50))
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(chunks))
        self.assertEqual(chunks[0][-50:], chunks[1][:50])

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
