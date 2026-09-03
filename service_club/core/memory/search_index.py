from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


class SearchIndexConfigurationError(ValueError):
    """Raised when the mandatory Elasticsearch endpoint is unsafe or missing."""


@dataclass(frozen=True)
class SearchCandidate:
    id: int
    score: float
    rank: int


class ElasticsearchSearchIndex:
    """Derived BM25 index for memories and independently managed document chunks."""

    def __init__(
        self,
        url: str,
        *,
        index_prefix: str = "agi_yukino",
        api_key: str = "",
        username: str = "",
        password: str = "",
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.url = _validated_url(url)
        self.index_prefix = _safe_prefix(index_prefix)
        self.api_key = api_key.strip()
        self.username = username.strip()
        self.password = password
        self._client_factory = client_factory
        self._client: Any | None = None
        self._lock = threading.RLock()
        self._last_error = ""
        self._searches = 0
        self._writes = 0

    @property
    def memory_index(self) -> str:
        return f"{self.index_prefix}_memories_v1"

    @property
    def document_index(self) -> str:
        return f"{self.index_prefix}_documents_v1"

    def ensure_indices(self) -> None:
        client = self._get_client()
        for index, properties in (
            (self.memory_index, self._memory_mapping()),
            (self.document_index, self._document_mapping()),
        ):
            if not client.indices.exists(index=index):
                try:
                    client.indices.create(
                        index=index,
                        settings=self._analysis_settings(),
                        mappings={"dynamic": "strict", "properties": properties},
                    )
                except Exception:
                    if not client.indices.exists(index=index):
                        raise
        self._last_error = ""

    def upsert_memory(self, row: dict[str, Any], *, refresh: bool = False) -> None:
        self.ensure_indices()
        self._get_client().index(
            index=self.memory_index,
            id=str(int(row["id"])),
            document={
                "session_id": str(row["session_id"]),
                "content": str(row["content"]),
                "source": str(row.get("source", "explicit")),
                "importance": float(row.get("importance", 0.5)),
                "created_at": float(row.get("created_at", 0.0)),
            },
            refresh="wait_for" if refresh else False,
        )
        self._writes += 1
        self._last_error = ""

    def delete_memories(self, memory_ids: Iterable[int], *, refresh: bool = False) -> int:
        ids = sorted({int(value) for value in memory_ids if int(value) > 0})
        if not ids:
            return 0
        self.ensure_indices()
        response = self._get_client().delete_by_query(
            index=self.memory_index,
            query={"terms": {"_id": [str(value) for value in ids]}},
            conflicts="proceed",
            refresh=refresh,
        )
        self._writes += len(ids)
        return int(response.get("deleted", 0))

    def delete_session(self, session_id: str, *, refresh: bool = False) -> int:
        if not session_id:
            return 0
        self.ensure_indices()
        response = self._get_client().delete_by_query(
            index=self.memory_index,
            query={"term": {"session_id": session_id}},
            conflicts="proceed",
            refresh=refresh,
        )
        return int(response.get("deleted", 0))

    def search_memories(
        self, session_id: str, query: str, *, limit: int = 100
    ) -> list[SearchCandidate]:
        if not session_id or not query.strip():
            return []
        return self._search(
            index=self.memory_index,
            query={
                "bool": {
                    "filter": [{"term": {"session_id": session_id}}],
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["content^2", "content.standard"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "should": [{"match_phrase": {"content": {"query": query, "boost": 2}}}],
                }
            },
            limit=limit,
        )

    def upsert_document_chunk(self, row: dict[str, Any], *, refresh: bool = False) -> None:
        self.ensure_indices()
        self._get_client().index(
            index=self.document_index,
            id=str(row["id"]),
            document={
                "knowledge_base": str(row["knowledge_base"]),
                "document_id": str(row["document_id"]),
                "chunk_index": int(row["chunk_index"]),
                "title": str(row.get("title", "")),
                "content": str(row["content"]),
                "source_uri": str(row.get("source_uri", "")),
                "updated_at": float(row.get("updated_at", 0.0)),
            },
            refresh="wait_for" if refresh else False,
        )
        self._writes += 1

    def search_documents(
        self, knowledge_base: str, query: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        self.ensure_indices()
        response = self._get_client().search(
            index=self.document_index,
            size=max(1, min(limit, 200)),
            query={
                "bool": {
                    "filter": [{"term": {"knowledge_base": knowledge_base}}],
                    "must": [{"multi_match": {"query": query, "fields": ["title^3", "content^2", "content.standard"]}}],
                }
            },
        )
        return [
            {**dict(hit.get("_source", {})), "id": str(hit.get("_id", "")), "score": float(hit.get("_score") or 0.0), "rank": rank}
            for rank, hit in enumerate(response.get("hits", {}).get("hits", []), start=1)
        ]

    def delete_document(self, document_id: str, *, refresh: bool = False) -> int:
        if not document_id:
            return 0
        self.ensure_indices()
        response = self._get_client().delete_by_query(
            index=self.document_index,
            query={"term": {"document_id": document_id}},
            conflicts="proceed",
            refresh=refresh,
        )
        return int(response.get("deleted", 0))

    def project_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Idempotently repair an Elasticsearch projection from a durable outbox event."""
        if event_type == "memory.created":
            self.upsert_memory(payload)
        elif event_type == "memory.deleted":
            self.delete_memories([int(payload["id"])])
        elif event_type == "memory.session_cleared":
            self.delete_session(str(payload["session_id"]))
        elif event_type == "knowledge.chunk_created":
            self.upsert_document_chunk(payload)
        elif event_type == "knowledge.document_deleted":
            self.delete_document(str(payload["document_id"]))

    def bulk_rebuild_memories(
        self, rows: Iterable[dict[str, Any]], *, replace: bool = False
    ) -> int:
        self.ensure_indices()
        if replace:
            self._get_client().delete_by_query(
                index=self.memory_index,
                query={"match_all": {}},
                conflicts="proceed",
                refresh=True,
            )
        try:
            from elasticsearch.helpers import streaming_bulk
        except ImportError as exc:
            raise RuntimeError("Elasticsearch 索引需要安装 elasticsearch。") from exc
        actions = (
            {
                "_op_type": "index",
                "_index": self.memory_index,
                "_id": str(int(row["id"])),
                "_source": {
                    "session_id": str(row["session_id"]),
                    "content": str(row["content"]),
                    "source": str(row["source"]),
                    "importance": float(row["importance"]),
                    "created_at": float(row["created_at"]),
                },
            }
            for row in rows
        )
        count = sum(1 for ok, _ in streaming_bulk(self._get_client(), actions) if ok)
        self._writes += count
        return count

    def bulk_rebuild_documents(
        self, rows: Iterable[dict[str, Any]], *, replace: bool = False
    ) -> int:
        self.ensure_indices()
        if replace:
            self._get_client().delete_by_query(
                index=self.document_index,
                query={"match_all": {}},
                conflicts="proceed",
                refresh=True,
            )
        try:
            from elasticsearch.helpers import streaming_bulk
        except ImportError as exc:
            raise RuntimeError("Elasticsearch 索引需要安装 elasticsearch。") from exc
        actions = (
            {
                "_op_type": "index",
                "_index": self.document_index,
                "_id": str(row["id"]),
                "_source": {
                    "knowledge_base": str(row["knowledge_base"]),
                    "document_id": str(row["document_id"]),
                    "chunk_index": int(row["chunk_index"]),
                    "title": str(row.get("title", "")),
                    "content": str(row["content"]),
                    "source_uri": str(row.get("source_uri", "")),
                    "updated_at": float(row.get("updated_at", 0.0)),
                },
            }
            for row in rows
        )
        count = sum(1 for ok, _ in streaming_bulk(self._get_client(), actions) if ok)
        self._writes += count
        return count

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        ok = not self._last_error
        if probe:
            try:
                self.ensure_indices()
                ok = bool(self._get_client().ping())
            except Exception as exc:
                self._record_failure(exc)
                ok = False
        return {
            "ok": ok,
            "enabled": True,
            "backend": "elasticsearch",
            "location": _public_url(self.url),
            "memory_index": self.memory_index,
            "document_index": self.document_index,
            "searches": self._searches,
            "writes": self._writes,
            "last_error": self._last_error,
            "source_of_truth": "postgresql",
        }

    def close(self) -> None:
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def _search(self, *, index: str, query: dict[str, Any], limit: int) -> list[SearchCandidate]:
        try:
            self.ensure_indices()
            response = self._get_client().search(index=index, size=max(1, min(limit, 500)), query=query)
            hits = response.get("hits", {}).get("hits", [])
            self._searches += 1
            maximum = max((float(hit.get("_score") or 0.0) for hit in hits), default=1.0) or 1.0
            self._last_error = ""
            return [SearchCandidate(id=int(hit["_id"]), score=float(hit.get("_score") or 0.0) / maximum, rank=rank) for rank, hit in enumerate(hits, start=1)]
        except Exception as exc:
            self._record_failure(exc)
            return []

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            factory = self._client_factory
            if factory is None:
                try:
                    from elasticsearch import Elasticsearch
                except ImportError as exc:
                    raise RuntimeError("Elasticsearch 索引需要安装 elasticsearch。") from exc
                factory = Elasticsearch
            kwargs: dict[str, Any] = {"request_timeout": _timeout_seconds()}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            elif self.username:
                kwargs["basic_auth"] = (self.username, self.password)
            self._client = factory(self.url, **kwargs)
            return self._client

    @staticmethod
    def _analysis_settings() -> dict[str, Any]:
        return {"analysis": {"analyzer": {"cjk_text": {"tokenizer": "standard", "filter": ["lowercase", "cjk_bigram"]}}}}

    @staticmethod
    def _memory_mapping() -> dict[str, Any]:
        return {
            "session_id": {"type": "keyword"}, "content": {"type": "text", "analyzer": "cjk_text", "fields": {"standard": {"type": "text", "analyzer": "standard"}}},
            "source": {"type": "keyword"}, "importance": {"type": "float"}, "created_at": {"type": "double"},
        }

    @staticmethod
    def _document_mapping() -> dict[str, Any]:
        return {
            "knowledge_base": {"type": "keyword"}, "document_id": {"type": "keyword"}, "chunk_index": {"type": "integer"},
            "title": {"type": "text", "analyzer": "cjk_text"}, "content": {"type": "text", "analyzer": "cjk_text", "fields": {"standard": {"type": "text", "analyzer": "standard"}}},
            "source_uri": {"type": "keyword", "ignore_above": 2048}, "updated_at": {"type": "double"},
        }

    def _record_failure(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"


def configured_search_index() -> ElasticsearchSearchIndex:
    url = os.getenv("YUKINO_ELASTICSEARCH_URL", "http://127.0.0.1:9200").strip()
    return ElasticsearchSearchIndex(
        url,
        index_prefix=os.getenv("YUKINO_ELASTICSEARCH_INDEX_PREFIX", "agi_yukino"),
        api_key=os.getenv("YUKINO_ELASTICSEARCH_API_KEY", ""),
        username=os.getenv("YUKINO_ELASTICSEARCH_USERNAME", ""),
        password=os.getenv("YUKINO_ELASTICSEARCH_PASSWORD", ""),
    )


def _validated_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.fragment or parsed.username or parsed.password:
        raise SearchIndexConfigurationError("YUKINO_ELASTICSEARCH_URL 必须是无内嵌凭据的 http(s) 地址。")
    return url.rstrip("/")


def _safe_prefix(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,48}", normalized):
        raise SearchIndexConfigurationError("Elasticsearch 索引前缀格式无效。")
    return normalized


def _timeout_seconds() -> float:
    try:
        return max(1.0, min(float(os.getenv("YUKINO_ELASTICSEARCH_TIMEOUT_SECONDS", "5")), 30.0))
    except ValueError:
        return 5.0


def _public_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{parsed.hostname}"
