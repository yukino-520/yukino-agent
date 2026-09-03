from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from service_club.core.runtime.event_stream import (
    append_outbox_event,
    ensure_event_outbox_schema,
)
from service_club.storage.relational import (
    RelationalBackend,
    RelationalConnection,
    configured_relational_backend,
)


# 作用：统一管理对话、记忆、提醒、关系和学习状态的关系事实，并同步可重建派生索引。
# 参数：无。
class MemoryManager:
    # 作用：选择关系事实后端并预留向量、知识图谱派生索引绑定点。
    # 参数 db_path：仅为旧调用方保留，不参与 PostgreSQL 连接选择。
    # 参数 backend：关系、向量或图谱后端选择或后端实例。
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self.backend = backend or configured_relational_backend()
        if self.backend.name != "postgresql":
            raise ValueError("MemoryManager 运行时仅允许 PostgreSQL 事实后端。")
        self.vector_store: Any | None = None
        self.knowledge_graph: Any | None = None
        self.search_index: Any | None = None

    # 作用：绑定可选向量派生索引；关系库中的记忆与向量缓存仍是规范事实。
    # 参数 vector_store：可选的 Milvus 等外部向量派生索引。
    def bind_vector_store(self, vector_store: Any | None) -> None:
        self.vector_store = vector_store

    # 作用：绑定可选知识图谱存储，用于在删除记忆时同步撤销相关证据关系。
    # 参数 knowledge_graph：可选的知识图谱事实存储或查询通道。
    def bind_knowledge_graph(self, knowledge_graph: Any | None) -> None:
        self.knowledge_graph = knowledge_graph

    def bind_search_index(self, search_index: Any) -> None:
        self.search_index = search_index

    # 作用：初始化全部 PostgreSQL 事实表；失败时直接终止初始化。
    # 参数：无。
    def init(self) -> None:
        self._init_at_path(self.db_path)

    # 作用：创建并迁移项目所需的关系事实表、约束和查询索引。
    # 参数 db_path：已废弃的本地路径参数。
    def _init_at_path(self, db_path: Path | None = None) -> None:
        del db_path
        with self.backend.connect(immediate=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character TEXT NOT NULL,
                    chat_mode TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_reply TEXT NOT NULL,
                    emotion TEXT NOT NULL,
                    tool_results_json TEXT NOT NULL DEFAULT '[]',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    execution_json TEXT NOT NULL DEFAULT '{}',
                    trace_id TEXT NOT NULL DEFAULT '',
                    degraded INTEGER NOT NULL DEFAULT 0,
                    degradation_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            self._add_missing_columns(
                conn,
                "conversation_logs",
                {
                    "tool_results_json": "TEXT NOT NULL DEFAULT '[]'",
                    "attachments_json": "TEXT NOT NULL DEFAULT '[]'",
                    "execution_json": "TEXT NOT NULL DEFAULT '{}'",
                    "trace_id": "TEXT NOT NULL DEFAULT ''",
                    "degraded": "INTEGER NOT NULL DEFAULT 0",
                    "degradation_reason": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_logs_session_cursor
                ON conversation_logs(session_id, id DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_threads (
                    id TEXT PRIMARY KEY,
                    chat_mode TEXT NOT NULL,
                    character TEXT NOT NULL,
                    title TEXT NOT NULL,
                    preview TEXT NOT NULL DEFAULT '',
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_threads_scope
                ON conversation_threads(chat_mode, character, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            ensure_event_outbox_schema(conn)
            self._add_missing_columns(
                conn,
                "memories",
                {"idempotency_key": "TEXT NOT NULL DEFAULT ''"},
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_idempotency
                ON memories(session_id, idempotency_key)
                WHERE idempotency_key <> ''
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_session_rank
                ON memories(session_id, importance DESC, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    due_at REAL,
                    timezone TEXT NOT NULL DEFAULT '',
                    recurrence TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unscheduled',
                    claim_token TEXT NOT NULL DEFAULT '',
                    claim_until REAL,
                    delivered_at REAL,
                    last_delivered_at REAL,
                    delivery_count INTEGER NOT NULL DEFAULT 0,
                    cancelled_at REAL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            reminder_migrations = {
                "due_at": self.backend.float_type,
                "timezone": "TEXT NOT NULL DEFAULT ''",
                "recurrence": "TEXT NOT NULL DEFAULT ''",
                "status": "TEXT NOT NULL DEFAULT 'unscheduled'",
                "claim_token": "TEXT NOT NULL DEFAULT ''",
                "claim_until": self.backend.float_type,
                "delivered_at": self.backend.float_type,
                "last_delivered_at": self.backend.float_type,
                "delivery_count": "INTEGER NOT NULL DEFAULT 0",
                "cancelled_at": self.backend.float_type,
                "updated_at": f"{self.backend.float_type} NOT NULL DEFAULT 0",
                "idempotency_key": "TEXT NOT NULL DEFAULT ''",
            }
            self._add_missing_columns(conn, "reminders", reminder_migrations)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(status, due_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminders_session_status_due
                ON reminders(session_id, status, due_at, id)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_idempotency
                ON reminders(session_id, idempotency_key)
                WHERE idempotency_key <> ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    emotion TEXT NOT NULL,
                    character TEXT NOT NULL,
                    chat_mode TEXT NOT NULL,
                    salience REAL NOT NULL DEFAULT 0.5,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emotion_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    intensity REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_emotion_turns_session_cursor
                ON emotion_turns(session_id, id DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_nudges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    emotion TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            self._add_missing_columns(
                conn,
                "proactive_nudges",
                {"updated_at": f"{self.backend.float_type} NOT NULL DEFAULT 0"},
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_proactive_nudges_session_due
                ON proactive_nudges(session_id, status, due_at, id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_route_stats (
                    agent_id TEXT PRIMARY KEY,
                    turns INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    total_latency_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS affective_states (
                    session_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    intensity REAL NOT NULL,
                    pleasure REAL NOT NULL,
                    arousal REAL NOT NULL,
                    dominance REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_instincts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    rule_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    source TEXT NOT NULL DEFAULT 'implicit',
                    last_evidence_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_evidence_at REAL NOT NULL,
                    last_used_at REAL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(session_id, rule_key)
                )
                """
            )
            self._add_missing_columns(
                conn,
                "behavior_instincts",
                {"updated_at": f"{self.backend.float_type} NOT NULL DEFAULT 0"},
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    covered_messages INTEGER NOT NULL,
                    original_tokens INTEGER NOT NULL,
                    final_tokens INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationship_states (
                    session_id TEXT PRIMARY KEY,
                    turns INTEGER NOT NULL DEFAULT 0,
                    trust_score REAL NOT NULL DEFAULT 0.2,
                    quality_score REAL NOT NULL DEFAULT 0.0,
                    positive_feedback INTEGER NOT NULL DEFAULT 0,
                    negative_feedback INTEGER NOT NULL DEFAULT 0,
                    support_moments INTEGER NOT NULL DEFAULT 0,
                    rupture_count INTEGER NOT NULL DEFAULT 0,
                    repair_count INTEGER NOT NULL DEFAULT 0,
                    repair_progress INTEGER NOT NULL DEFAULT 0,
                    rupture_state TEXT NOT NULL DEFAULT 'stable',
                    boundaries_json TEXT NOT NULL DEFAULT '[]',
                    last_gap_seconds INTEGER NOT NULL DEFAULT 0,
                    last_interaction_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationship_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    signal_key TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    trust_delta REAL NOT NULL DEFAULT 0.0,
                    quality_delta REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL,
                    UNIQUE(session_id, evidence_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_relationship_events_session_cursor
                ON relationship_events(session_id, id DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    polarity TEXT NOT NULL DEFAULT 'positive',
                    confidence REAL NOT NULL DEFAULT 0.65,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'current',
                    source TEXT NOT NULL,
                    superseded_by INTEGER,
                    created_at REAL NOT NULL,
                    last_evidence_at REAL NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            self._add_missing_columns(
                conn,
                "profile_facts",
                {"updated_at": f"{self.backend.float_type} NOT NULL DEFAULT 0"},
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_fact_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(fact_id, evidence_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_interaction_stats (
                    session_id TEXT PRIMARY KEY,
                    total_messages INTEGER NOT NULL DEFAULT 0,
                    deep_messages INTEGER NOT NULL DEFAULT 0,
                    total_chars INTEGER NOT NULL DEFAULT 0,
                    hour_distribution_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(memory_id, model)
                )
                """
            )
            if self.backend.name == "postgresql":
                conn.execute(
                    """
                    ALTER TABLE memory_embeddings
                    ADD COLUMN IF NOT EXISTS embedding vector
                    """
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
                ON memory_embeddings(model, dimensions, memory_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS response_quality_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character TEXT NOT NULL,
                    issue_key TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    repair_action TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(session_id, issue_key, output_hash)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_strategy_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    strategy_signature TEXT NOT NULL,
                    opening_move TEXT NOT NULL,
                    advice_mode TEXT NOT NULL,
                    question_budget INTEGER NOT NULL,
                    roleplay_intensity TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(session_id, response_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS interaction_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    signal_key TEXT NOT NULL,
                    strategy_signature TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(session_id, evidence_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_self_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character TEXT NOT NULL,
                    checkpoint_date TEXT NOT NULL,
                    relationship_stage TEXT NOT NULL,
                    rupture_state TEXT NOT NULL,
                    trust_band TEXT NOT NULL,
                    milestone_keys_json TEXT NOT NULL DEFAULT '[]',
                    adjustment_keys_json TEXT NOT NULL DEFAULT '[]',
                    lesson_keys_json TEXT NOT NULL DEFAULT '[]',
                    source_counts_json TEXT NOT NULL DEFAULT '{}',
                    evidence_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, character, checkpoint_date)
                )
                """
            )

    # 作用：为已有 PostgreSQL 部署补加缺失列。
    # 参数 connection：用于检查和迁移表结构的关系库连接。
    # 参数 table：需要检查或迁移结构的数据库表名。
    # 参数 additions：待补加的数据库列名与 SQL 类型定义映射。
    def _add_missing_columns(
        self,
        connection: RelationalConnection,
        table: str,
        additions: dict[str, str],
    ) -> None:
        for column, definition in additions.items():
            connection.execute(
                f"""
                ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS {column} {definition}
                """
            )

    # 作用：将显式记忆幂等写入关系事实表，空内容被忽略。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 content：待保存、评分、格式化或发送的业务正文。
    # 参数 source：数据来源标识或源实体；具体形态由当前方法类型标注约定。
    # 参数 importance：记忆事实参与召回排序的重要度。
    # 参数 idempotency_key：用于识别重复写入请求的幂等键。
    def remember(
        self,
        session_id: str,
        content: str,
        source: str = "explicit",
        importance: float = 0.7,
        idempotency_key: str = "",
    ) -> int | None:
        if not content.strip():
            return None
        normalized = content.strip()
        now = time.time()
        row: dict[str, Any] | None = None
        with self.backend.connect() as conn:
            inserted = conn.execute(
                    """
                    INSERT INTO memories(
                        session_id, content, source, importance,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    RETURNING id, session_id, content, source, importance, created_at
                    """,
                    (
                        session_id,
                        normalized,
                        source,
                        importance,
                        idempotency_key[:128],
                        now,
                    ),
                ).fetchone()
            if inserted is not None:
                row = dict(inserted)
                append_outbox_event(
                    conn,
                    event_type="memory.created",
                    aggregate_type="memory",
                    aggregate_id=str(row["id"]),
                    partition_key=session_id,
                    payload=row,
                )
        if row is None:
            existing = self.memory_by_idempotency(session_id, idempotency_key)
            return int(existing["id"]) if existing else None
        if self.search_index is not None:
            try:
                self.search_index.upsert_memory(row)
            except Exception:
                pass
        return int(row["id"])

    def memory_candidates_by_ids(
        self, session_id: str, memory_ids: list[int]
    ) -> list[dict[str, Any]]:
        ids = sorted({int(value) for value in memory_ids if int(value) > 0})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, session_id, content, source, importance, created_at
                FROM memories
                WHERE session_id = ? AND id IN ({placeholders})
                """,
                [session_id, *ids],
            ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        return [by_id[value] for value in memory_ids if value in by_id]

    def iter_memories(self, *, batch_size: int = 500):  # noqa: ANN201
        last_id = 0
        while True:
            with self.backend.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, session_id, content, source, importance, created_at
                    FROM memories WHERE id > ? ORDER BY id LIMIT ?
                    """,
                    (last_id, max(1, min(batch_size, 5000))),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                item = dict(row)
                yield item
                last_id = int(item["id"])

    # 作用：按会话和幂等键查找已存在的记忆事实，供重复请求复用。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 idempotency_key：用于识别重复写入请求的幂等键。
    def memory_by_idempotency(
        self,
        session_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_id, content, source, importance,
                       idempotency_key, created_at
                FROM memories
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key[:128]),
            ).fetchone()
        return dict(row) if row is not None else None

    # 作用：从关系事实源按重要度和时间取得混合检索的有界候选集。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_memory_candidates(
        self, session_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content, source, importance, created_at
                FROM memories
                WHERE session_id = ?
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：使用本地可降级混合检索器搜索关系事实候选。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def search_memories(self, session_id: str, query: str, limit: int = 5) -> list[Any]:
        from service_club.core.memory.hybrid_retrieval import HybridMemoryRetriever

        return HybridMemoryRetriever(self).search(session_id, query, limit)

    # 作用：从关系库向量缓存读取指定模型与记忆 ID 的规范向量记录。
    # 参数 memory_ids：待读取、删除或评分的记忆事实 ID 列表。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    def get_memory_embeddings(
        self, memory_ids: list[int], model: str
    ) -> dict[int, list[float]]:
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT memory_id, vector_json FROM memory_embeddings
                WHERE model = ? AND memory_id IN ({placeholders})
                """,
                [model, *memory_ids],
            ).fetchall()
        vectors = {}
        for row in rows:
            try:
                vectors[int(row["memory_id"])] = [
                    float(value) for value in json.loads(str(row["vector_json"]))
                ]
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return vectors

    # 作用：保存关系库向量缓存，并尽力同步到可重建的外部向量索引。
    # 参数 memory_id：目标记忆事实的数据库标识。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 vector：记忆文本对应的浮点向量。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def save_memory_embedding(
        self,
        memory_id: int,
        model: str,
        vector: list[float],
        *,
        now: float,
        session_id: str = "",
    ) -> None:
        encoded = json.dumps(vector, separators=(",", ":"))
        with self.backend.connect() as conn:
            if not session_id:
                row = conn.execute(
                    "SELECT session_id FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
                session_id = str(row["session_id"]) if row is not None else ""
            if self.backend.name == "postgresql":
                conn.execute(
                    """
                    INSERT INTO memory_embeddings(
                        memory_id, model, dimensions, vector_json, embedding, updated_at
                    ) VALUES (?, ?, ?, ?, CAST(? AS vector), ?)
                    ON CONFLICT(memory_id, model) DO UPDATE SET
                        dimensions = excluded.dimensions,
                        vector_json = excluded.vector_json,
                        embedding = excluded.embedding,
                        updated_at = excluded.updated_at
                    """,
                    (memory_id, model, len(vector), encoded, encoded, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO memory_embeddings(
                        memory_id, model, dimensions, vector_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id, model) DO UPDATE SET
                        dimensions = excluded.dimensions,
                        vector_json = excluded.vector_json,
                        updated_at = excluded.updated_at
                    """,
                    (memory_id, model, len(vector), encoded, now),
                )
        if self.vector_store is not None and session_id:
            self.vector_store.upsert(
                memory_id=memory_id,
                session_id=session_id,
                model=model,
                vector=vector,
                updated_at=now,
            )

    # 作用：分批遍历规范向量记录，供外部派生向量索引全量重建。
    # 参数 batch_size：每批读取规范向量事实的最大条数。
    def iter_memory_embeddings(self, *, batch_size: int = 200):  # noqa: ANN201
        """Yield canonical embedding rows for rebuilding a derived vector index."""
        last_memory_id = 0
        last_model = ""
        batch_size = max(1, min(int(batch_size), 2000))
        while True:
            with self.backend.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT e.memory_id, e.model, e.vector_json, e.updated_at,
                           m.session_id
                    FROM memory_embeddings e
                    JOIN memories m ON m.id = e.memory_id
                    WHERE e.memory_id > ? OR (e.memory_id = ? AND e.model > ?)
                    ORDER BY e.memory_id, e.model
                    LIMIT ?
                    """,
                    (last_memory_id, last_memory_id, last_model, batch_size),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                try:
                    vector = [float(value) for value in json.loads(str(row["vector_json"]))]
                except (TypeError, ValueError, json.JSONDecodeError):
                    vector = []
                yield {
                    "memory_id": int(row["memory_id"]),
                    "session_id": str(row["session_id"]),
                    "model": str(row["model"]),
                    "vector": vector,
                    "updated_at": float(row["updated_at"]),
                }
            last_memory_id = int(rows[-1]["memory_id"])
            last_model = str(rows[-1]["model"])

    # 作用：从关系库向量事实重建外部派生索引，并汇总扫描与失败数量。
    # 参数 drop_existing：重建向量索引前是否删除已有受管集合。
    def rebuild_vector_index(self, *, drop_existing: bool = False) -> dict[str, Any]:
        if self.vector_store is None:
            return {
                "enabled": False,
                "backend": "relational",
                "scanned": 0,
                "indexed": 0,
                "failed": 0,
                "dropped_collections": 0,
            }
        dropped = (
            int(self.vector_store.drop_managed_collections()) if drop_existing else 0
        )
        scanned = 0
        indexed = 0
        failed = 0
        for row in self.iter_memory_embeddings():
            scanned += 1
            if not row["vector"]:
                failed += 1
                continue
            ok = self.vector_store.upsert(
                memory_id=row["memory_id"],
                session_id=row["session_id"],
                model=row["model"],
                vector=row["vector"],
                updated_at=row["updated_at"],
            )
            if ok:
                indexed += 1
            else:
                failed += 1
        return {
            "enabled": True,
            "backend": self.vector_store.name,
            "scanned": scanned,
            "indexed": indexed,
            "failed": failed,
            "dropped_collections": dropped,
        }

    # 作用：在 PostgreSQL 内用 pgvector 为有界候选打分，避免导出全部存量向量。
    # 参数 memory_ids：待读取、删除或评分的记忆事实 ID 列表。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 query_vector：当前查询文本对应的向量。
    def memory_vector_scores(
        self,
        memory_ids: list[int],
        model: str,
        query_vector: list[float],
    ) -> dict[int, float]:
        """Score a bounded candidate set in pgvector without exporting stored vectors."""
        if self.backend.name != "postgresql" or not memory_ids or not query_vector:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        encoded = json.dumps(query_vector, separators=(",", ":"))
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT memory_id,
                       GREATEST(0.0, 1.0 - (embedding <=> CAST(? AS vector))) AS score
                FROM memory_embeddings
                WHERE model = ? AND dimensions = ? AND embedding IS NOT NULL
                  AND memory_id IN ({placeholders})
                """,
                (encoded, model, len(query_vector), *memory_ids),
            ).fetchall()
        return {
            int(row["memory_id"]): min(1.0, max(0.0, float(row["score"])))
            for row in rows
        }

    # 作用：将已验证 JSON 向量回填为 PostgreSQL vector 列，支持旧数据迁移。
    # 参数：无。
    def backfill_pgvector(self) -> int:
        """Populate pgvector values after importing verified JSON embeddings."""
        if self.backend.name != "postgresql":
            return 0
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE memory_embeddings
                SET embedding = CAST(vector_json AS vector)
                WHERE embedding IS NULL AND vector_json <> '' AND vector_json <> '[]'
                """
            )
        return int(cursor.rowcount or 0)

    # 作用：对混合检索结果做兼容封装，仅返回记忆正文。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def recall(self, session_id: str, query: str, limit: int = 5) -> list[str]:
        return [hit.content for hit in self.search_memories(session_id, query, limit)]

    # 作用：清除会话全部关系事实及对应向量派生记录，保留其他会话隔离。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def clear(self, session_id: str) -> int:
        try:
            with self.backend.connect() as conn:
                profile_rows = conn.execute(
                    "SELECT id FROM profile_facts WHERE session_id = ?", (session_id,)
                ).fetchall()
                profile_ids = [int(row["id"]) for row in profile_rows]
                if profile_ids:
                    placeholders = ",".join("?" for _ in profile_ids)
                    conn.execute(
                        f"DELETE FROM profile_fact_evidence "
                        f"WHERE fact_id IN ({placeholders})",
                        profile_ids,
                    )
                rows = conn.execute(
                    "SELECT id FROM memories WHERE session_id = ?", (session_id,)
                ).fetchall()
                memory_ids = [int(row["id"]) for row in rows]
                if memory_ids:
                    placeholders = ",".join("?" for _ in memory_ids)
                    conn.execute(
                        f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                        memory_ids,
                    )
                session_tables = (
                    "conversation_logs",
                    "memories",
                    "reminders",
                    "user_profile",
                    "turn_reflections",
                    "emotion_turns",
                    "proactive_nudges",
                    "affective_states",
                    "behavior_instincts",
                    "context_checkpoints",
                    "relationship_states",
                    "relationship_events",
                    "profile_facts",
                    "profile_interaction_stats",
                    "response_quality_cases",
                    "turn_strategy_snapshots",
                    "interaction_outcomes",
                    "agent_self_checkpoints",
                )
                deleted_total = 0
                for table in session_tables:
                    cursor = conn.execute(
                        f"DELETE FROM {table} WHERE session_id = ?", (session_id,)
                    )
                    deleted_total += int(cursor.rowcount or 0)
                append_outbox_event(
                    conn,
                    event_type="memory.session_cleared",
                    aggregate_type="memory_session",
                    aggregate_id=session_id,
                    partition_key=session_id,
                    payload={"session_id": session_id},
                )
            if self.vector_store is not None:
                self.vector_store.delete_session(session_id)
            if self.search_index is not None:
                try:
                    self.search_index.delete_session(session_id)
                except Exception:
                    pass
            return deleted_total
        except Exception:
            raise

    # 作用：按内容查询删除记忆及关联向量、图谱证据，并撤销匹配画像事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    def forget(self, session_id: str, query: str) -> int:
        query = query.strip()
        if not query:
            return 0
        try:
            with self.backend.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, content FROM memories
                    WHERE session_id = ? AND content LIKE ?
                    """,
                    (session_id, f"%{query}%"),
                ).fetchall()
                memory_ids = [int(row["id"]) for row in rows]
                memory_contents = [str(row["content"]) for row in rows]
                if memory_ids:
                    placeholders = ",".join("?" for _ in memory_ids)
                    conn.execute(
                        f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                        memory_ids,
                    )
                cursor = conn.execute(
                    """
                    DELETE FROM memories
                    WHERE session_id = ? AND content LIKE ?
                    """,
                    (session_id, f"%{query}%"),
                )
                deleted = cursor.rowcount if cursor.rowcount is not None else 0
                for memory_id in memory_ids:
                    append_outbox_event(
                        conn,
                        event_type="memory.deleted",
                        aggregate_type="memory",
                        aggregate_id=str(memory_id),
                        partition_key=session_id,
                        payload={"id": memory_id, "session_id": session_id},
                    )
            if self.vector_store is not None and memory_ids:
                self.vector_store.delete_memory_ids(memory_ids)
            if self.search_index is not None and memory_ids:
                try:
                    self.search_index.delete_memories(memory_ids)
                except Exception:
                    pass
            if self.knowledge_graph is not None:
                for content in memory_contents:
                    self.knowledge_graph.delete_evidence(
                        session_id,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    )
            return deleted + self.revoke_profile_facts_matching(session_id, query)
        except Exception:
            raise

    # 作用：按事实 ID 删除单条记忆，并同步清理向量与图谱派生数据。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 memory_id：目标记忆事实的数据库标识。
    def forget_by_id(self, session_id: str, memory_id: int) -> bool:
        try:
            with self.backend.connect() as conn:
                row = conn.execute(
                    "SELECT id, content FROM memories WHERE id = ? AND session_id = ?",
                    (memory_id, session_id),
                ).fetchone()
                if row is None:
                    return False
                content = str(row["content"])
                conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
                conn.execute(
                    "DELETE FROM memories WHERE id = ? AND session_id = ?",
                    (memory_id, session_id),
                )
                append_outbox_event(
                    conn,
                    event_type="memory.deleted",
                    aggregate_type="memory",
                    aggregate_id=str(memory_id),
                    partition_key=session_id,
                    payload={"id": memory_id, "session_id": session_id},
                )
            if self.vector_store is not None:
                self.vector_store.delete_memory_ids([memory_id])
            if self.search_index is not None:
                try:
                    self.search_index.delete_memories([memory_id])
                except Exception:
                    pass
            if self.knowledge_graph is not None:
                self.knowledge_graph.delete_evidence(
                    session_id,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            return True
        except Exception:
            raise

    # 作用：幂等创建已排期或待补充时间的提醒事实，并返回规范记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 content：待保存、评分、格式化或发送的业务正文。
    # 参数 due_at：提醒、关怀或周期计算使用的到期时间戳。
    # 参数 timezone：提醒或检查点计算采用的 IANA 时区名称。
    # 参数 recurrence：提醒的每日或每周重复规则。
    # 参数 idempotency_key：用于识别重复写入请求的幂等键。
    def add_reminder(
        self,
        session_id: str,
        content: str,
        *,
        due_at: float | None = None,
        timezone: str = "",
        recurrence: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any] | None:
        if not content.strip():
            return None
        now = time.time()
        status = "scheduled" if due_at is not None else "unscheduled"
        try:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO reminders(
                        session_id, content, due_at, timezone, recurrence,
                        idempotency_key, status, updated_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        session_id,
                        content.strip(),
                        due_at,
                        timezone,
                        recurrence,
                        idempotency_key[:128],
                        status,
                        now,
                        now,
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    reminder_id = int(inserted["id"])
                else:
                    row = conn.execute(
                        """
                        SELECT id FROM reminders
                        WHERE session_id = ? AND idempotency_key = ?
                        """,
                        (session_id, idempotency_key[:128]),
                    ).fetchone()
                    reminder_id = int(row["id"]) if row is not None else 0
        except Exception:
            raise
        return self.get_reminder(session_id, reminder_id) if reminder_id else None

    # 作用：按会话和幂等键读取已有提醒，避免重复创建同一副作用任务。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 idempotency_key：用于识别重复写入请求的幂等键。
    def reminder_by_idempotency(
        self,
        session_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM reminders
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key[:128]),
            ).fetchone()
        return self.get_reminder(session_id, int(row["id"])) if row is not None else None

    # 作用：按会话与 ID 读取提醒的排期、租约和投递状态事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 reminder_id：目标提醒记录的数据库标识。
    def get_reminder(self, session_id: str, reminder_id: int) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_id, content, due_at, timezone, recurrence, status,
                       claim_token, claim_until, delivered_at, last_delivered_at,
                       delivery_count, cancelled_at,
                       updated_at, created_at
                FROM reminders WHERE id = ? AND session_id = ?
                """,
                (reminder_id, session_id),
            ).fetchone()
        return dict(row) if row is not None else None

    # 作用：按创建顺序倒序列出会话提醒及其完整状态。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_reminders(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, content, due_at, timezone, recurrence, status,
                       claim_until, delivered_at, last_delivered_at, delivery_count,
                       cancelled_at, updated_at, created_at
                FROM reminders
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：在单会话内回收过期租约并原子领取到期提醒，防止并发重复投递。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    # 参数 lease_seconds：提醒领取权允许保持的租约秒数。
    def claim_due_reminders(
        self,
        session_id: str,
        *,
        now: float | None = None,
        limit: int = 10,
        lease_seconds: float = 60,
    ) -> list[dict[str, Any]]:
        current = float(now if now is not None else time.time())
        lease_until = current + max(10.0, min(float(lease_seconds), 300.0))
        claimed: list[dict[str, Any]] = []
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'scheduled', claim_token = '', claim_until = NULL,
                    updated_at = ?
                WHERE session_id = ? AND status = 'claimed' AND claim_until <= ?
                """,
                (current, session_id, current),
            )
            rows = conn.execute(
                """
                SELECT id FROM reminders
                WHERE session_id = ? AND status = 'scheduled'
                  AND due_at IS NOT NULL AND due_at <= ?
                ORDER BY due_at, id LIMIT ?
                """
                + self.backend.for_update(skip_locked=True),
                (session_id, current, max(1, min(int(limit), 20))),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                cursor = conn.execute(
                    """
                    UPDATE reminders
                    SET status = 'claimed', claim_token = ?, claim_until = ?,
                        updated_at = ?
                    WHERE id = ? AND session_id = ? AND status = 'scheduled'
                    """,
                    (token, lease_until, current, int(row["id"]), session_id),
                )
                if not cursor.rowcount:
                    continue
                item = conn.execute(
                    """
                    SELECT id, session_id, content, due_at, timezone, recurrence, status,
                           claim_token, claim_until, created_at
                    FROM reminders WHERE id = ?
                    """,
                    (int(row["id"]),),
                ).fetchone()
                if item is not None:
                    claimed.append(dict(item))
        return claimed

    # 作用：跨会话原子领取到期提醒，供服务端统一调度器并行消费。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    # 参数 lease_seconds：提醒领取权允许保持的租约秒数。
    def claim_due_reminders_any(
        self,
        *,
        now: float | None = None,
        limit: int = 20,
        lease_seconds: float = 300,
    ) -> list[dict[str, Any]]:
        """Claim due reminders across sessions for a server-side dispatcher."""
        current = float(now if now is not None else time.time())
        lease_until = current + max(10.0, min(float(lease_seconds), 3600.0))
        claimed: list[dict[str, Any]] = []
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'scheduled', claim_token = '', claim_until = NULL,
                    updated_at = ?
                WHERE status = 'claimed' AND claim_until <= ?
                """,
                (current, current),
            )
            rows = conn.execute(
                """
                SELECT id, session_id FROM reminders
                WHERE status = 'scheduled' AND due_at IS NOT NULL AND due_at <= ?
                ORDER BY due_at, id LIMIT ?
                """
                + self.backend.for_update(skip_locked=True),
                (current, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                cursor = conn.execute(
                    """
                    UPDATE reminders
                    SET status = 'claimed', claim_token = ?, claim_until = ?,
                        updated_at = ?
                    WHERE id = ? AND session_id = ? AND status = 'scheduled'
                    """,
                    (
                        token,
                        lease_until,
                        current,
                        int(row["id"]),
                        str(row["session_id"]),
                    ),
                )
                if not cursor.rowcount:
                    continue
                item = conn.execute(
                    """
                    SELECT id, session_id, content, due_at, timezone, recurrence, status,
                           claim_token, claim_until, created_at
                    FROM reminders WHERE id = ?
                    """,
                    (int(row["id"]),),
                ).fetchone()
                if item is not None:
                    claimed.append(dict(item))
        return claimed

    # 作用：仅凭匹配令牌续租已领取提醒，确保外部投递前仍拥有执行权。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 reminder_id：目标提醒记录的数据库标识。
    # 参数 claim_token：证明调用方拥有当前提醒租约的随机令牌。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    # 参数 lease_seconds：提醒领取权允许保持的租约秒数。
    def renew_reminder_claim(
        self,
        session_id: str,
        reminder_id: int,
        claim_token: str,
        *,
        now: float | None = None,
        lease_seconds: float = 300,
    ) -> bool:
        """Renew an owned lease immediately before an external delivery attempt."""
        if not claim_token:
            return False
        current = float(now if now is not None else time.time())
        lease_until = current + max(10.0, min(float(lease_seconds), 3600.0))
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET claim_until = ?, updated_at = ?
                WHERE id = ? AND session_id = ? AND status = 'claimed'
                  AND claim_token = ?
                """,
                (lease_until, current, reminder_id, session_id, claim_token),
            )
        return bool(cursor.rowcount)

    # 作用：仅释放调用方拥有的提醒租约，使失败投递可由后续消费者重试。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 reminder_id：目标提醒记录的数据库标识。
    # 参数 claim_token：证明调用方拥有当前提醒租约的随机令牌。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def release_reminder_claim(
        self,
        session_id: str,
        reminder_id: int,
        claim_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Release only the lease owned by the caller, never another consumer's lease."""
        if not claim_token:
            return False
        current = float(now if now is not None else time.time())
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'scheduled', claim_token = '', claim_until = NULL,
                    updated_at = ?
                WHERE id = ? AND session_id = ? AND status = 'claimed'
                  AND claim_token = ?
                """,
                (current, reminder_id, session_id, claim_token),
            )
        return bool(cursor.rowcount)

    # 作用：确认投递成功；一次性提醒终结，周期提醒推进到下一合法时间。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 reminder_id：目标提醒记录的数据库标识。
    # 参数 claim_token：证明调用方拥有当前提醒租约的随机令牌。
    # 参数 delivered_at：提醒或关怀实际投递完成的时间戳。
    def acknowledge_reminder(
        self,
        session_id: str,
        reminder_id: int,
        claim_token: str,
        *,
        delivered_at: float | None = None,
    ) -> bool:
        now = float(delivered_at if delivered_at is not None else time.time())
        if not claim_token:
            return False
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT due_at, timezone, recurrence FROM reminders
                WHERE id = ? AND session_id = ? AND status = 'claimed'
                  AND claim_token = ?
                """
                + self.backend.for_update(),
                (reminder_id, session_id, claim_token),
            ).fetchone()
            if row is None:
                return False
            recurrence = str(row["recurrence"] or "")
            if recurrence:
                next_due = self._next_recurrence_due(
                    float(row["due_at"] or now),
                    recurrence,
                    str(row["timezone"] or "Asia/Shanghai"),
                    now,
                )
                cursor = conn.execute(
                    """
                    UPDATE reminders
                    SET status = 'scheduled', due_at = ?, last_delivered_at = ?,
                        delivery_count = delivery_count + 1, claim_token = '',
                        claim_until = NULL, updated_at = ?
                    WHERE id = ? AND session_id = ? AND status = 'claimed'
                      AND claim_token = ?
                    """,
                    (next_due, now, now, reminder_id, session_id, claim_token),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE reminders
                    SET status = 'delivered', delivered_at = ?, last_delivered_at = ?,
                        delivery_count = delivery_count + 1, claim_token = '',
                        claim_until = NULL, updated_at = ?
                    WHERE id = ? AND session_id = ? AND status = 'claimed'
                      AND claim_token = ?
                    """,
                    (now, now, now, reminder_id, session_id, claim_token),
                )
        return bool(cursor.rowcount)

    # 作用：按提醒时区计算每日或每周规则在投递后的下一次到期时间。
    # 参数 due_at：提醒、关怀或周期计算使用的到期时间戳。
    # 参数 recurrence：提醒的每日或每周重复规则。
    # 参数 timezone：提醒或检查点计算采用的 IANA 时区名称。
    # 参数 delivered_at：提醒或关怀实际投递完成的时间戳。
    @staticmethod
    # 作用：执行“next_recurrence_due”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 due_at：调用方传入的due_at，用于本次处理。
    # 参数 recurrence：调用方传入的recurrence，用于本次处理。
    # 参数 timezone：调用方传入的timezone，用于本次处理。
    # 参数 delivered_at：调用方传入的delivered_at，用于本次处理。
    def _next_recurrence_due(
        due_at: float,
        recurrence: str,
        timezone: str,
        delivered_at: float,
    ) -> float:
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("Asia/Shanghai")
        current = dt.datetime.fromtimestamp(due_at, zone)
        days = 1 if recurrence == "daily" else 7
        candidate = current
        for _ in range(370):
            next_date = candidate.date() + dt.timedelta(days=days)
            candidate = dt.datetime.combine(
                next_date,
                candidate.timetz().replace(tzinfo=None),
                tzinfo=zone,
            )
            if candidate.timestamp() > delivered_at:
                return candidate.timestamp()
        return delivered_at + days * 86_400

    # 作用：取消尚未终结的提醒并清除可能存在的领取租约。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 reminder_id：目标提醒记录的数据库标识。
    def cancel_reminder(self, session_id: str, reminder_id: int) -> bool:
        now = time.time()
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'cancelled', cancelled_at = ?, claim_token = '',
                    claim_until = NULL, updated_at = ?
                WHERE id = ? AND session_id = ?
                  AND status IN ('scheduled', 'unscheduled', 'claimed')
                """,
                (now, now, reminder_id, session_id),
            )
        return bool(cursor.rowcount)

    # 作用：按状态汇总全局提醒数量和关系后端信息。
    # 参数：无。
    def reminder_status(self) -> dict[str, Any]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM reminders GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "ok": True,
            "counts": counts,
            "total": sum(counts.values()),
            "backend": self.backend.name,
        }

    # 作用：将一轮对话、工具回执、附件和执行状态写入规范事实日志。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 user_message：本轮用户消息原文。
    # 参数 assistant_reply：本轮助手最终回复文本。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    # 参数 tool_results：本轮真实工具执行回执列表。
    # 参数 attachments：本轮用户提交的结构化附件列表。
    # 参数 execution：本轮执行计划、验收与状态的结构化快照。
    # 参数 trace_id：串联本轮日志与执行事件的追踪标识。
    # 参数 degraded：本轮执行是否处于降级状态。
    # 参数 degradation_reason：本轮发生降级时记录的原因。
    def save_conversation(
        self,
        *,
        session_id: str,
        character: str,
        chat_mode: str,
        user_message: str,
        assistant_reply: str,
        emotion: str,
        tool_results: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        execution: dict[str, Any] | None = None,
        trace_id: str = "",
        degraded: bool = False,
        degradation_reason: str = "",
    ) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO conversation_logs(
                        session_id, character, chat_mode, user_message,
                        assistant_reply, emotion, tool_results_json, attachments_json,
                        execution_json, trace_id,
                        degraded, degradation_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        character,
                        chat_mode,
                        user_message,
                        assistant_reply,
                        emotion,
                        json.dumps(tool_results or [], ensure_ascii=False),
                        json.dumps(attachments or [], ensure_ascii=False),
                        json.dumps(execution or {}, ensure_ascii=False),
                        trace_id,
                        int(degraded),
                        degradation_reason,
                        time.time(),
                    ),
                )
        except Exception:
            raise

    # 作用：读取会话最近对话事实，并安全还原 JSON 执行元数据。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def recent_conversations(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT user_message, assistant_reply, character, chat_mode, emotion,
                       tool_results_json, attachments_json, execution_json, trace_id, degraded,
                       degradation_reason, created_at
                FROM conversation_logs
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["tool_results"] = json.loads(str(item.pop("tool_results_json") or "[]"))
            except (json.JSONDecodeError, TypeError):
                item["tool_results"] = []
            try:
                item["attachments"] = json.loads(str(item.pop("attachments_json") or "[]"))
            except (json.JSONDecodeError, TypeError):
                item["attachments"] = []
            try:
                item["execution"] = json.loads(str(item.pop("execution_json") or "{}"))
            except (json.JSONDecodeError, TypeError):
                item["execution"] = {}
            item["degraded"] = bool(item["degraded"])
            results.append(item)
        return results

    # 作用：返回会话最新一轮事实，供重聚和连续性判断使用。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def latest_conversation(self, session_id: str) -> dict[str, Any] | None:
        conversations = self.recent_conversations(session_id, limit=1)
        return conversations[0] if conversations else None

    # 作用：读取会话的持久关系状态，并解码明确边界列表。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def get_relationship_state(self, session_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM relationship_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        state = dict(row)
        try:
            state["boundaries"] = json.loads(str(state.pop("boundaries_json")))
        except (json.JSONDecodeError, TypeError):
            state["boundaries"] = []
        return state

    # 作用：幂等保存信任、质量、破裂修复和边界组成的关系事实快照。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    def save_relationship_state(self, state: dict[str, Any]) -> None:
        values = (
            state["session_id"],
            int(state["turns"]),
            float(state["trust_score"]),
            float(state["quality_score"]),
            int(state["positive_feedback"]),
            int(state["negative_feedback"]),
            int(state["support_moments"]),
            int(state["rupture_count"]),
            int(state["repair_count"]),
            int(state["repair_progress"]),
            str(state["rupture_state"]),
            json.dumps(state.get("boundaries", []), ensure_ascii=False),
            int(state.get("last_gap_seconds", 0)),
            state.get("last_interaction_at"),
            float(state["updated_at"]),
        )
        with self.backend.connect() as conn:
            conn.execute(
                """
                INSERT INTO relationship_states(
                    session_id, turns, trust_score, quality_score,
                    positive_feedback, negative_feedback, support_moments,
                    rupture_count, repair_count, repair_progress, rupture_state,
                    boundaries_json, last_gap_seconds, last_interaction_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    turns = excluded.turns,
                    trust_score = excluded.trust_score,
                    quality_score = excluded.quality_score,
                    positive_feedback = excluded.positive_feedback,
                    negative_feedback = excluded.negative_feedback,
                    support_moments = excluded.support_moments,
                    rupture_count = excluded.rupture_count,
                    repair_count = excluded.repair_count,
                    repair_progress = excluded.repair_progress,
                    rupture_state = excluded.rupture_state,
                    boundaries_json = excluded.boundaries_json,
                    last_gap_seconds = excluded.last_gap_seconds,
                    last_interaction_at = excluded.last_interaction_at,
                    updated_at = excluded.updated_at
                """,
                values,
            )

    # 作用：以证据哈希幂等记录关系事件及其分数增量。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 event_type：关系事件的业务类型。
    # 参数 signal_key：可聚合的反馈、关系或互动信号键。
    # 参数 evidence_hash：用于幂等去重和追溯来源的证据哈希。
    # 参数 trust_delta：关系事件对信任分数的增量。
    # 参数 quality_delta：关系事件对互动质量分数的增量。
    # 参数 created_at：业务事实或快照的创建时间戳。
    def record_relationship_event(
        self,
        *,
        session_id: str,
        event_type: str,
        signal_key: str,
        evidence_hash: str,
        trust_delta: float,
        quality_delta: float,
        created_at: float,
    ) -> bool:
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO relationship_events(
                    session_id, event_type, signal_key, evidence_hash,
                    trust_delta, quality_delta, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    session_id,
                    event_type,
                    signal_key,
                    evidence_hash,
                    trust_delta,
                    quality_delta,
                    created_at,
                ),
            )
        return int(cursor.rowcount or 0) == 1

    # 作用：按时间倒序读取会话最近的关系变化证据。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_relationship_events(
        self, session_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type, signal_key, trust_delta, quality_delta, created_at
                FROM relationship_events
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：持久化单轮离散情绪事实，供短期情绪窗口跨进程恢复。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 label：情绪标签或知识实体显示名称。
    # 参数 intensity：归一化到零至一的情绪强度。
    def save_emotion_turn(self, session_id: str, label: str, intensity: float) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO emotion_turns(session_id, label, intensity, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (session_id, label, intensity, time.time()),
                )
        except Exception:
            raise

    # 作用：按时间正序返回会话最近若干轮情绪事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_emotion_turns(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT label, intensity, created_at
                FROM emotion_turns
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # 作用：列出存在持久情绪记录的全部会话 ID。
    # 参数：无。
    def emotion_sessions(self) -> list[str]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM emotion_turns ORDER BY session_id"
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    # 作用：创建一条待投递的主动关怀事实，并记录触发原因和情绪。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 content：待保存、评分、格式化或发送的业务正文。
    # 参数 reason：主动关怀、存储结果或候选产生的业务原因。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    # 参数 due_at：提醒、关怀或周期计算使用的到期时间戳。
    def schedule_nudge(
        self,
        *,
        session_id: str,
        content: str,
        reason: str,
        emotion: str,
        due_at: float,
    ) -> dict[str, Any]:
        now = time.time()
        try:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO proactive_nudges(
                        session_id, content, reason, emotion, due_at, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    RETURNING id
                    """,
                    (session_id, content, reason, emotion, due_at, now, now),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    raise RuntimeError("主动关怀记录创建失败。")
                nudge_id = int(inserted["id"])
        except Exception:
            raise
        return {
            "id": nudge_id,
            "session_id": session_id,
            "content": content,
            "reason": reason,
            "emotion": emotion,
            "due_at": due_at,
            "status": "pending",
            "created_at": now,
            "delivered_at": None,
        }

    # 作用：按会话及可选状态查询主动关怀记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 status：用于筛选或写入的业务生命周期状态。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_nudges(
        self,
        session_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        where = "session_id = ?"
        params: list[Any] = [session_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        params.append(limit)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, session_id, content, reason, emotion, due_at,
                       status, created_at, delivered_at
                FROM proactive_nudges
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：批量取消会话中尚未投递的主动关怀并返回数量。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def cancel_pending_nudges(self, session_id: str) -> int:
        now = time.time()
        try:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE proactive_nudges
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'pending'
                    """,
                    (now, session_id),
                )
        except Exception:
            raise
        return int(cursor.rowcount or 0)

    # 作用：原子把一条待投递关怀标记为已投递，避免并发重复发送。
    # 参数 nudge_id：目标主动关怀记录的数据库标识。
    # 参数 delivered_at：提醒或关怀实际投递完成的时间戳。
    def claim_nudge(self, nudge_id: int, delivered_at: float) -> bool:
        try:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE proactive_nudges
                    SET status = 'delivered', delivered_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (delivered_at, delivered_at, nudge_id),
                )
        except Exception:
            raise
        return int(cursor.rowcount or 0) == 1

    # 作用：返回各角色路由累计轮数、成功数和总延迟事实。
    # 参数：无。
    def route_stats(self) -> dict[str, dict[str, int]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, turns, successes, total_latency_ms
                FROM agent_route_stats
                """
            ).fetchall()
        return {
            str(row["agent_id"]): {
                "turns": int(row["turns"]),
                "successes": int(row["successes"]),
                "total_latency_ms": int(row["total_latency_ms"]),
            }
            for row in rows
        }

    # 作用：原子累计一次角色路由的成功结果与延迟，供自适应路由评分。
    # 参数 agent_id：被统计或路由的角色唯一标识。
    # 参数 success：本次角色路由或执行是否成功。
    # 参数 latency_ms：本次角色执行耗时，单位为毫秒。
    def record_route_result(
        self,
        agent_id: str,
        *,
        success: bool,
        latency_ms: int,
    ) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_route_stats(
                        agent_id, turns, successes, total_latency_ms, updated_at
                    ) VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        turns = turns + 1,
                        successes = successes + excluded.successes,
                        total_latency_ms = total_latency_ms + excluded.total_latency_ms,
                        updated_at = excluded.updated_at
                    """,
                    (agent_id, 1 if success else 0, latency_ms, time.time()),
                )
        except Exception:
            raise

    # 作用：读取会话当前持久 PAD 情绪状态。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def get_affective_state(self, session_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, label, intensity, pleasure, arousal,
                       dominance, updated_at
                FROM affective_states
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    # 作用：幂等保存会话长期情绪的标签、强度和 PAD 连续坐标。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 label：情绪标签或知识实体显示名称。
    # 参数 intensity：归一化到零至一的情绪强度。
    # 参数 pleasure：PAD 模型中的愉悦度数值。
    # 参数 arousal：PAD 模型中的唤醒度数值。
    # 参数 dominance：PAD 模型中的支配感数值。
    # 参数 updated_at：事实或检查点的最后更新时间戳。
    def save_affective_state(
        self,
        *,
        session_id: str,
        label: str,
        intensity: float,
        pleasure: float,
        arousal: float,
        dominance: float,
        updated_at: float,
    ) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO affective_states(
                        session_id, label, intensity, pleasure,
                        arousal, dominance, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        label = excluded.label,
                        intensity = excluded.intensity,
                        pleasure = excluded.pleasure,
                        arousal = excluded.arousal,
                        dominance = excluded.dominance,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        label,
                        intensity,
                        pleasure,
                        arousal,
                        dominance,
                        updated_at,
                    ),
                )
        except Exception:
            raise

    # 作用：以证据哈希更新行为习惯候选，达到重复证据阈值后转为活跃。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 rule_key：行为习惯的稳定规则标识。
    # 参数 content：待保存、评分、格式化或发送的业务正文。
    # 参数 evidence_hash：用于幂等去重和追溯来源的证据哈希。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def observe_instinct(
        self,
        *,
        session_id: str,
        rule_key: str,
        content: str,
        evidence_hash: str,
        now: float,
    ) -> dict[str, Any]:
        try:
            with self.backend.connect(immediate=True) as conn:
                self.backend.lock_scope(
                    conn, f"memory-instinct:{session_id}:{rule_key}"
                )
                row = conn.execute(
                    """
                    SELECT * FROM behavior_instincts
                    WHERE session_id = ? AND rule_key = ?
                    """,
                    (session_id, rule_key),
                ).fetchone()
                if row and row["status"] == "revoked":
                    return {**dict(row), "changed": False, "reason": "revoked_by_user"}
                if row and row["last_evidence_hash"] == evidence_hash:
                    return {**dict(row), "changed": False, "reason": "duplicate_evidence"}
                if row:
                    evidence_count = int(row["evidence_count"]) + 1
                    confidence = min(0.95, float(row["confidence"]) + 0.2)
                    status = "active" if evidence_count >= 2 and confidence >= 0.7 else "candidate"
                    conn.execute(
                        """
                        UPDATE behavior_instincts
                        SET content = ?, confidence = ?, evidence_count = ?,
                            status = ?, last_evidence_hash = ?, last_evidence_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            content,
                            confidence,
                            evidence_count,
                            status,
                            evidence_hash,
                            now,
                            now,
                            row["id"],
                        ),
                    )
                    instinct_id = int(row["id"])
                else:
                    confidence = 0.5
                    evidence_count = 1
                    status = "candidate"
                    cursor = conn.execute(
                        """
                        INSERT INTO behavior_instincts(
                            session_id, rule_key, content, confidence,
                            evidence_count, status, source, last_evidence_hash,
                            created_at, last_evidence_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'implicit', ?, ?, ?, ?)
                        RETURNING id
                        """,
                        (
                            session_id,
                            rule_key,
                            content,
                            confidence,
                            evidence_count,
                            status,
                            evidence_hash,
                            now,
                            now,
                            now,
                        ),
                    )
                    inserted = cursor.fetchone()
                    if inserted is None:
                        raise RuntimeError("行为本能记录创建失败。")
                    instinct_id = int(inserted["id"])
            return {
                "id": instinct_id,
                "session_id": session_id,
                "rule_key": rule_key,
                "content": content,
                "confidence": confidence,
                "evidence_count": evidence_count,
                "status": status,
                "changed": True,
                "reason": "evidence_recorded",
            }
        except Exception:
            raise

    # 作用：按可选生命周期状态列出会话行为习惯及其置信度证据。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 statuses：用于筛选记录的多个生命周期状态。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_instincts(
        self,
        session_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [session_id]
        status_clause = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            status_clause = f" AND status IN ({placeholders})"
            params.extend(statuses)
        params.append(limit)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, session_id, rule_key, content, confidence,
                       evidence_count, use_count, status, source,
                       created_at, last_evidence_at, last_used_at
                FROM behavior_instincts
                WHERE session_id = ?{status_clause}
                ORDER BY confidence DESC, evidence_count DESC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：记录本轮实际注入提示词的活跃习惯及使用时间。
    # 参数 instinct_ids：本轮实际使用的行为习惯 ID 列表。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def mark_instincts_used(self, instinct_ids: list[int], now: float) -> None:
        if not instinct_ids:
            return
        placeholders = ",".join("?" for _ in instinct_ids)
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    f"""
                    UPDATE behavior_instincts
                    SET use_count = use_count + 1, last_used_at = ?, updated_at = ?
                    WHERE id IN ({placeholders}) AND status = 'active'
                    """,
                    [now, now, *instinct_ids],
                )
        except Exception:
            raise

    # 作用：根据明确反馈调整近期使用习惯的置信度和生命周期状态。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 delta：根据用户反馈施加到习惯置信度的增量。
    # 参数 used_after：只调整在该时间之后被使用过的行为习惯。
    def adjust_recent_instincts(
        self,
        session_id: str,
        *,
        delta: float,
        used_after: float,
    ) -> list[dict[str, Any]]:
        try:
            with self.backend.connect(immediate=True) as conn:
                self.backend.lock_scope(conn, f"memory-instinct-adjust:{session_id}")
                rows = conn.execute(
                    """
                    SELECT id, confidence FROM behavior_instincts
                    WHERE session_id = ? AND status = 'active'
                      AND last_used_at IS NOT NULL AND last_used_at >= ?
                    """,
                    (session_id, used_after),
                ).fetchall()
                adjusted: list[dict[str, Any]] = []
                for row in rows:
                    confidence = max(0.0, min(1.0, float(row["confidence"]) + delta))
                    status = "active" if confidence >= 0.7 else "candidate"
                    if confidence < 0.3:
                        status = "archived"
                    conn.execute(
                        """
                        UPDATE behavior_instincts
                        SET confidence = ?, status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (confidence, status, time.time(), row["id"]),
                    )
                    adjusted.append(
                        {"id": int(row["id"]), "confidence": confidence, "status": status}
                    )
            return adjusted
        except Exception:
            raise

    # 作用：按活跃与候选的不同截止时间归档缺少新证据的行为习惯。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 active_before：活跃习惯允许保留的最早证据时间戳。
    # 参数 candidate_before：候选习惯允许保留的最早证据时间戳。
    def archive_stale_instincts(
        self,
        session_id: str,
        *,
        active_before: float,
        candidate_before: float,
    ) -> int:
        now = time.time()
        try:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE behavior_instincts
                    SET status = 'archived', updated_at = ?
                    WHERE session_id = ? AND (
                        (status = 'active' AND last_evidence_at < ?)
                        OR (status = 'candidate' AND last_evidence_at < ?)
                    )
                    """,
                    (now, session_id, active_before, candidate_before),
                )
            return int(cursor.rowcount or 0)
        except Exception:
            raise

    # 作用：将指定行为习惯标记为用户撤销，阻止后续证据自动恢复。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 instinct_id：目标行为习惯记录的数据库标识。
    def revoke_instinct(self, session_id: str, instinct_id: int) -> bool:
        now = time.time()
        try:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE behavior_instincts
                    SET status = 'revoked', updated_at = ?
                    WHERE session_id = ? AND id = ?
                    """,
                    (now, session_id, instinct_id),
                )
            return int(cursor.rowcount or 0) == 1
        except Exception:
            raise

    # 作用：幂等保存上下文摘要、来源哈希和压缩预算统计。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 summary：待保存或格式化的摘要文本。
    # 参数 source_hash：被摘要历史消息的稳定哈希，用于复用检查点。
    # 参数 covered_messages：上下文摘要已经覆盖的历史消息数量。
    # 参数 original_tokens：上下文压缩前的估算令牌数量。
    # 参数 final_tokens：上下文压缩后的估算令牌数量。
    # 参数 updated_at：事实或检查点的最后更新时间戳。
    def save_context_checkpoint(
        self,
        *,
        session_id: str,
        summary: str,
        source_hash: str,
        covered_messages: int,
        original_tokens: int,
        final_tokens: int,
        updated_at: float,
    ) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO context_checkpoints(
                        session_id, summary, source_hash, covered_messages,
                        original_tokens, final_tokens, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        summary = excluded.summary,
                        source_hash = excluded.source_hash,
                        covered_messages = excluded.covered_messages,
                        original_tokens = excluded.original_tokens,
                        final_tokens = excluded.final_tokens,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        summary,
                        source_hash,
                        covered_messages,
                        original_tokens,
                        final_tokens,
                        updated_at,
                    ),
                )
        except Exception:
            raise

    # 作用：读取会话最近一次上下文压缩检查点。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def get_context_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, summary, source_hash, covered_messages,
                       original_tokens, final_tokens, updated_at
                FROM context_checkpoints
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    # 作用：保存可由底层事实重建的用户画像摘要视图。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 summary：待保存或格式化的摘要文本。
    def upsert_user_profile(self, session_id: str, summary: str) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_profile(session_id, summary, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        summary = excluded.summary,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, summary, time.time()),
                )
        except Exception:
            raise

    # 作用：读取会话当前用户画像派生摘要及更新时间。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def get_user_profile(self, session_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, summary, updated_at
                FROM user_profile
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    # 作用：原子累计画像互动的消息数、字符数、深度消息数和小时分布。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 message_length：当前用户消息的字符长度。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def record_profile_interaction(
        self, session_id: str, message_length: int, *, now: float
    ) -> dict[str, Any]:
        hour = f"{time.localtime(now).tm_hour:02d}"
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"memory-profile-stats:{session_id}")
            row = conn.execute(
                "SELECT * FROM profile_interaction_stats WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                try:
                    distribution = json.loads(str(row["hour_distribution_json"]))
                except (json.JSONDecodeError, TypeError):
                    distribution = {}
                distribution[hour] = int(distribution.get(hour, 0)) + 1
                conn.execute(
                    """
                    UPDATE profile_interaction_stats
                    SET total_messages = total_messages + 1,
                        deep_messages = deep_messages + ?,
                        total_chars = total_chars + ?,
                        hour_distribution_json = ?, last_active_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        1 if message_length >= 50 else 0,
                        message_length,
                        json.dumps(distribution, ensure_ascii=False),
                        now,
                        session_id,
                    ),
                )
            else:
                distribution = {hour: 1}
                conn.execute(
                    """
                    INSERT INTO profile_interaction_stats(
                        session_id, total_messages, deep_messages, total_chars,
                        hour_distribution_json, first_seen_at, last_active_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        1 if message_length >= 50 else 0,
                        message_length,
                        json.dumps(distribution, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        return self.get_profile_interaction_stats(session_id)

    # 作用：读取会话画像互动统计，缺失时返回零值结构。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def get_profile_interaction_stats(self, session_id: str) -> dict[str, Any]:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM profile_interaction_stats WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {
                "total_messages": 0,
                "deep_messages": 0,
                "total_chars": 0,
                "hour_distribution": {},
            }
        result = dict(row)
        try:
            result["hour_distribution"] = json.loads(
                str(result.pop("hour_distribution_json"))
            )
        except (json.JSONDecodeError, TypeError):
            result["hour_distribution"] = {}
        return result

    # 作用：以证据哈希强化相同画像事实，冲突时用新事实替代旧版本。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 fact_key：同类画像事实冲突与替代使用的稳定业务键。
    # 参数 category：记忆或画像事实所属的业务类别。
    # 参数 value：待规范化、持久化或解析的业务值。
    # 参数 polarity：画像兴趣事实的正向或负向极性。
    # 参数 source：数据来源标识或源实体；具体形态由当前方法类型标注约定。
    # 参数 evidence_hash：用于幂等去重和追溯来源的证据哈希。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def observe_profile_fact(
        self,
        *,
        session_id: str,
        fact_key: str,
        category: str,
        value: str,
        polarity: str,
        source: str,
        evidence_hash: str,
        now: float,
    ) -> dict[str, Any]:
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(
                conn, f"memory-profile-fact:{session_id}:{fact_key}"
            )
            current = conn.execute(
                """
                SELECT * FROM profile_facts
                WHERE session_id = ? AND fact_key = ? AND status = 'current'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id, fact_key),
            ).fetchone()
            if current and current["value"] == value and current["polarity"] == polarity:
                cursor = conn.execute(
                    """
                    INSERT INTO profile_fact_evidence(
                        fact_id, evidence_hash, created_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (current["id"], evidence_hash, now),
                )
                if int(cursor.rowcount or 0) == 0:
                    return {**dict(current), "changed": False, "reason": "duplicate_evidence"}
                confidence = min(0.95, float(current["confidence"]) + 0.1)
                conn.execute(
                    """
                    UPDATE profile_facts
                    SET confidence = ?, evidence_count = evidence_count + 1,
                        last_evidence_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (confidence, now, now, current["id"]),
                )
                return {
                    **dict(current),
                    "confidence": confidence,
                    "evidence_count": int(current["evidence_count"]) + 1,
                    "changed": True,
                    "reason": "evidence_reinforced",
                }

            cursor = conn.execute(
                """
                INSERT INTO profile_facts(
                    session_id, fact_key, category, value, polarity,
                    confidence, evidence_count, status, source,
                    created_at, last_evidence_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0.65, 1, 'current', ?, ?, ?, ?)
                RETURNING id
                """,
                (session_id, fact_key, category, value, polarity, source, now, now, now),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                raise RuntimeError("用户画像事实创建失败。")
            fact_id = int(inserted["id"])
            conn.execute(
                """
                INSERT INTO profile_fact_evidence(fact_id, evidence_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (fact_id, evidence_hash, now),
            )
            if current:
                conn.execute(
                    """
                    UPDATE profile_facts
                    SET status = 'superseded', superseded_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (fact_id, now, current["id"]),
                )
        return {
            "id": fact_id,
            "session_id": session_id,
            "fact_key": fact_key,
            "category": category,
            "value": value,
            "polarity": polarity,
            "confidence": 0.65,
            "evidence_count": 1,
            "status": "current",
            "source": source,
            "changed": True,
            "reason": "conflict_superseded" if current else "fact_created",
        }

    # 作用：按可选生命周期状态列出画像事实及证据、替代关系。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 statuses：用于筛选记录的多个生命周期状态。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_profile_facts(
        self,
        session_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [session_id]
        status_clause = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            status_clause = f" AND status IN ({placeholders})"
            params.extend(statuses)
        params.append(limit)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, session_id, fact_key, category, value, polarity,
                       confidence, evidence_count, status, source,
                       superseded_by, created_at, last_evidence_at
                FROM profile_facts
                WHERE session_id = ?{status_clause}
                ORDER BY status = 'current' DESC, last_evidence_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：按 ID 撤销会话画像事实并保留历史审计记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 fact_id：目标画像事实的数据库标识。
    def revoke_profile_fact(self, session_id: str, fact_id: int) -> bool:
        now = time.time()
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE profile_facts SET status = 'revoked', updated_at = ?
                WHERE session_id = ? AND id = ? AND status != 'revoked'
                """,
                (now, session_id, fact_id),
            )
        return int(cursor.rowcount or 0) == 1

    # 作用：按事实键撤销会话当前生效的画像事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 fact_key：同类画像事实冲突与替代使用的稳定业务键。
    def revoke_profile_fact_key(self, session_id: str, fact_key: str) -> int:
        now = time.time()
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE profile_facts SET status = 'revoked', updated_at = ?
                WHERE session_id = ? AND fact_key = ? AND status = 'current'
                """,
                (now, session_id, fact_key),
            )
        return int(cursor.rowcount or 0)

    # 作用：按键或值的文本匹配批量撤销当前画像事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    def revoke_profile_facts_matching(self, session_id: str, query: str) -> int:
        now = time.time()
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE profile_facts SET status = 'revoked', updated_at = ?
                WHERE session_id = ? AND status = 'current'
                  AND (fact_key LIKE ? OR value LIKE ?)
                """,
                (now, session_id, f"%{query}%", f"%{query}%"),
            )
        return int(cursor.rowcount or 0)

    # 作用：以输出哈希幂等记录回复质量问题及对应修复动作。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 issue_key：可聚合的回复质量问题类型键。
    # 参数 dimension：回复质量问题所属的评估维度。
    # 参数 severity：回复质量问题的严重程度。
    # 参数 output_hash：用于对同一回复质量案例幂等去重的输出哈希。
    # 参数 repair_action：检测到质量问题后采用的修复动作。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def record_quality_case(
        self,
        *,
        session_id: str,
        character: str,
        issue_key: str,
        dimension: str,
        severity: str,
        output_hash: str,
        repair_action: str,
        now: float,
    ) -> bool:
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO response_quality_cases(
                    session_id, character, issue_key, dimension, severity,
                    output_hash, repair_action, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    session_id,
                    character,
                    issue_key,
                    dimension,
                    severity,
                    output_hash,
                    repair_action,
                    now,
                ),
            )
        return int(cursor.rowcount or 0) == 1

    # 作用：按时间倒序列出会话最近的回复质量案例。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_quality_cases(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, character, issue_key, dimension,
                       severity, output_hash, repair_action, created_at
                FROM response_quality_cases
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：按角色聚合会话中的质量问题次数和最近发生时间。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def quality_issue_stats(
        self, session_id: str, character: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT issue_key, dimension, COUNT(*) AS occurrences,
                       MAX(created_at) AS last_seen
                FROM response_quality_cases
                WHERE session_id = ? AND character = ?
                GROUP BY issue_key, dimension
                ORDER BY occurrences DESC, last_seen DESC
                LIMIT ?
                """,
                (session_id, character, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：汇总全局质量案例和受影响会话数量。
    # 参数：无。
    def quality_global_stats(self) -> dict[str, Any]:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cases, COUNT(DISTINCT session_id) AS sessions
                FROM response_quality_cases
                """
            ).fetchone()
        return {
            "case_count": int(row["cases"]),
            "session_count": int(row["sessions"]),
        }

    # 作用：幂等保存一轮回复采用的策略快照，供下一轮显式反馈归因。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 response_hash：用于关联反馈但不保存回复原文的摘要哈希。
    # 参数 strategy_signature：由回复策略字段组成的稳定聚合签名。
    # 参数 opening_move：本轮回复开场采用的策略。
    # 参数 advice_mode：本轮回复采用的建议策略模式。
    # 参数 question_budget：本轮回复允许提出的问题数量。
    # 参数 roleplay_intensity：本轮角色化表达的强度档位。
    # 参数 created_at：业务事实或快照的创建时间戳。
    def save_strategy_snapshot(
        self,
        *,
        session_id: str,
        character: str,
        response_hash: str,
        strategy_signature: str,
        opening_move: str,
        advice_mode: str,
        question_budget: int,
        roleplay_intensity: str,
        created_at: float,
    ) -> bool:
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO turn_strategy_snapshots(
                    session_id, character, response_hash, strategy_signature,
                    opening_move, advice_mode, question_budget,
                    roleplay_intensity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    session_id,
                    character,
                    response_hash,
                    strategy_signature,
                    opening_move,
                    advice_mode,
                    question_budget,
                    roleplay_intensity,
                    created_at,
                ),
            )
        return int(cursor.rowcount or 0) == 1

    # 作用：读取会话最近一轮回复策略快照。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def latest_strategy_snapshot(self, session_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT character, response_hash, strategy_signature,
                       opening_move, advice_mode, question_budget,
                       roleplay_intensity, created_at
                FROM turn_strategy_snapshots
                WHERE session_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    # 作用：以反馈证据哈希幂等记录某回复策略的正负互动结果。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 sentiment：用户明确反馈的正向或负向分类。
    # 参数 signal_key：可聚合的反馈、关系或互动信号键。
    # 参数 strategy_signature：由回复策略字段组成的稳定聚合签名。
    # 参数 response_hash：用于关联反馈但不保存回复原文的摘要哈希。
    # 参数 evidence_hash：用于幂等去重和追溯来源的证据哈希。
    # 参数 created_at：业务事实或快照的创建时间戳。
    def record_interaction_outcome(
        self,
        *,
        session_id: str,
        character: str,
        sentiment: str,
        signal_key: str,
        strategy_signature: str,
        response_hash: str,
        evidence_hash: str,
        created_at: float,
    ) -> bool:
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO interaction_outcomes(
                    session_id, character, sentiment, signal_key,
                    strategy_signature, response_hash, evidence_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    session_id,
                    character,
                    sentiment,
                    signal_key,
                    strategy_signature,
                    response_hash,
                    evidence_hash,
                    created_at,
                ),
            )
        return int(cursor.rowcount or 0) == 1

    # 作用：按时间倒序列出会话中的互动结果证据。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_interaction_outcomes(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT character, sentiment, signal_key, strategy_signature,
                       response_hash, evidence_hash, created_at
                FROM interaction_outcomes
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：按角色、情感和信号聚合互动结果，并保留最近策略签名。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def interaction_outcome_stats(
        self, session_id: str, character: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT outcomes.sentiment, outcomes.signal_key,
                       COUNT(*) AS occurrences,
                       MAX(outcomes.created_at) AS last_seen,
                       (
                           SELECT latest.strategy_signature
                           FROM interaction_outcomes AS latest
                           WHERE latest.session_id = outcomes.session_id
                             AND latest.character = outcomes.character
                             AND latest.sentiment = outcomes.sentiment
                             AND latest.signal_key = outcomes.signal_key
                           ORDER BY latest.id DESC LIMIT 1
                       ) AS strategy_signature
                FROM interaction_outcomes AS outcomes
                WHERE outcomes.session_id = ? AND outcomes.character = ?
                GROUP BY outcomes.sentiment, outcomes.signal_key
                ORDER BY occurrences DESC, last_seen DESC
                LIMIT ?
                """,
                (session_id, character, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：汇总全局互动结果事件和会话数量。
    # 参数：无。
    def interaction_outcome_global_stats(self) -> dict[str, Any]:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS events, COUNT(DISTINCT session_id) AS sessions
                FROM interaction_outcomes
                """
            ).fetchone()
        return {
            "event_count": int(row["events"]),
            "session_count": int(row["sessions"]),
            "store_ok": True,
        }

    # 作用：按角色和自然日保存确定性连续性检查点，证据指纹未变时跳过写入。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 checkpoint_date：角色连续性检查点对应的本地自然日。
    # 参数 relationship_stage：连续性检查点记录的关系阶段。
    # 参数 rupture_state：关系当前的稳定、破裂或修复中状态。
    # 参数 trust_band：连续性检查点记录的低、中、高信任档位。
    # 参数 milestone_keys：由关系证据派生的角色历程里程碑键列表。
    # 参数 adjustment_keys：角色需要持续遵守的调整项键列表。
    # 参数 lesson_keys：从历史反馈与质量证据提炼的教训键列表。
    # 参数 source_counts：生成连续性检查点所依据的各类证据数量。
    # 参数 evidence_fingerprint：由规范证据集合生成的稳定指纹。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def save_self_model_checkpoint(
        self,
        *,
        session_id: str,
        character: str,
        checkpoint_date: str,
        relationship_stage: str,
        rupture_state: str,
        trust_band: str,
        milestone_keys: list[str],
        adjustment_keys: list[str],
        lesson_keys: list[str],
        source_counts: dict[str, int],
        evidence_fingerprint: str,
        now: float,
    ) -> bool:
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(
                conn,
                f"memory-self-model:{session_id}:{character}:{checkpoint_date}",
            )
            existing = conn.execute(
                """
                SELECT evidence_fingerprint FROM agent_self_checkpoints
                WHERE session_id = ? AND character = ? AND checkpoint_date = ?
                """,
                (session_id, character, checkpoint_date),
            ).fetchone()
            if (
                existing is not None
                and str(existing["evidence_fingerprint"]) == evidence_fingerprint
            ):
                return False
            conn.execute(
                """
                INSERT INTO agent_self_checkpoints(
                    session_id, character, checkpoint_date, relationship_stage,
                    rupture_state, trust_band, milestone_keys_json,
                    adjustment_keys_json, lesson_keys_json, source_counts_json,
                    evidence_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, character, checkpoint_date) DO UPDATE SET
                    relationship_stage = excluded.relationship_stage,
                    rupture_state = excluded.rupture_state,
                    trust_band = excluded.trust_band,
                    milestone_keys_json = excluded.milestone_keys_json,
                    adjustment_keys_json = excluded.adjustment_keys_json,
                    lesson_keys_json = excluded.lesson_keys_json,
                    source_counts_json = excluded.source_counts_json,
                    evidence_fingerprint = excluded.evidence_fingerprint,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    character,
                    checkpoint_date,
                    relationship_stage,
                    rupture_state,
                    trust_band,
                    json.dumps(milestone_keys),
                    json.dumps(adjustment_keys),
                    json.dumps(lesson_keys),
                    json.dumps(source_counts, sort_keys=True),
                    evidence_fingerprint,
                    now,
                    now,
                ),
            )
        return True

    # 作用：查询会话的角色连续性检查点，并安全解码里程碑与来源统计。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_self_model_checkpoints(
        self,
        session_id: str,
        *,
        character: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [session_id]
        character_filter = ""
        if character is not None:
            character_filter = " AND character = ?"
            params.append(character)
        params.append(limit)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT character, checkpoint_date, relationship_stage,
                       rupture_state, trust_band, milestone_keys_json,
                       adjustment_keys_json, lesson_keys_json,
                       source_counts_json, evidence_fingerprint,
                       created_at, updated_at
                FROM agent_self_checkpoints
                WHERE session_id = ?{character_filter}
                ORDER BY checkpoint_date DESC, updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        checkpoints = []
        for row in rows:
            item = dict(row)
            for source_key, target_key, fallback in (
                ("milestone_keys_json", "milestone_keys", []),
                ("adjustment_keys_json", "adjustment_keys", []),
                ("lesson_keys_json", "lesson_keys", []),
                ("source_counts_json", "source_counts", {}),
            ):
                try:
                    item[target_key] = json.loads(str(item.pop(source_key)))
                except (json.JSONDecodeError, TypeError):
                    item[target_key] = fallback
            checkpoints.append(item)
        return checkpoints

    # 作用：汇总角色连续性检查点、会话和会话角色组合数量。
    # 参数：无。
    def self_model_global_stats(self) -> dict[str, int]:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS checkpoints,
                       COUNT(DISTINCT session_id) AS sessions,
                       COUNT(DISTINCT session_id || ':' || character)
                           AS session_characters
                FROM agent_self_checkpoints
                """
            ).fetchone()
        return {
            "checkpoint_count": int(row["checkpoints"]),
            "session_count": int(row["sessions"]),
            "session_character_count": int(row["session_characters"]),
        }

    # 作用：保存单轮对话反思及其情绪、角色、模式和显著性事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 summary：待保存或格式化的摘要文本。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 salience：轮次反思是否值得长期保存的显著性分数。
    def save_reflection(
        self,
        *,
        session_id: str,
        summary: str,
        emotion: str,
        character: str,
        chat_mode: str,
        salience: float,
    ) -> None:
        try:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO turn_reflections(
                        session_id, summary, emotion, character,
                        chat_mode, salience, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        summary,
                        emotion,
                        character,
                        chat_mode,
                        salience,
                        time.time(),
                    ),
                )
        except Exception:
            raise

    # 作用：按时间倒序读取会话最近的轮次反思事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_reflections(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, summary, emotion, character, chat_mode, salience, created_at
                FROM turn_reflections
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
