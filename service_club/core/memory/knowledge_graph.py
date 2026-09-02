from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from service_club.storage.relational import RelationalBackend


# 作用：表示图谱后端、地址或凭据不满足配置与安全约束。
# 参数：无。
class GraphConfigurationError(ValueError):
    pass


# 作用：将关系库图事实尽力投影到可清空、可重建的 Neo4j 派生图。
# 参数：无。
class Neo4jGraphProjector:
    """Optional, rebuildable property-graph projection of relational facts."""

    # 作用：保存 Neo4j 连接配置并延迟创建驱动，关系库仍是唯一事实源。
    # 参数 uri：Milvus 或 Neo4j 服务的连接地址。
    # 参数 username：连接 Neo4j 使用的用户名。
    # 参数 password：连接 Neo4j 使用的密码；仅用于创建驱动。
    # 参数 database：外部存储使用的数据库名称。
    # 参数 driver_factory：用于创建 Neo4j 驱动的可注入工厂。
    def __init__(
        self,
        uri: str,
        *,
        username: str,
        password: str,
        database: str = "neo4j",
        driver_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.uri = uri.strip()
        self.username = username.strip()
        self.password = password
        self.database = database.strip() or "neo4j"
        self._driver_factory = driver_factory
        self._driver: Any | None = None
        self._initialized = False
        self._last_error = ""
        self._writes = 0
        self._failures = 0
        self._probed = False

    # 作用：返回图谱投影后端的稳定名称。
    # 参数：无。
    @property
    # 作用：执行“name”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def name(self) -> str:
        return "neo4j"

    # 作用：幂等写入实体投影，失败只记录状态而不影响关系事实提交。
    # 参数 entity：待投影或转换的知识实体事实。
    def upsert_entity(self, entity: dict[str, Any]) -> bool:
        try:
            self._ensure_schema()
            self._execute(
                """
                MERGE (entity:YukinoEntity {uid: $uid})
                SET entity.session_id = $session_id,
                    entity.label = $label,
                    entity.entity_type = $entity_type,
                    entity.properties_json = $properties_json,
                    entity.confidence = $confidence,
                    entity.source = $source,
                    entity.updated_at = $updated_at
                """,
                uid=str(entity["id"]),
                session_id=str(entity["session_id"]),
                label=str(entity["label"]),
                entity_type=str(entity["type"]),
                properties_json=json.dumps(entity.get("properties", {}), ensure_ascii=False),
                confidence=float(entity.get("confidence", 1.0)),
                source=str(entity.get("source", "explicit")),
                updated_at=float(entity.get("updated_at", time.time())),
            )
            self._writes += 1
            self._mark_connected()
            return True
        except Exception as exc:
            self._record_failure(exc)
            return False

    # 作用：确保两端实体存在后幂等写入关系投影。
    # 参数 source：数据来源标识或源实体；具体形态由当前方法类型标注约定。
    # 参数 relation：知识关系类型文本或完整关系事实。
    # 参数 target：知识关系目标实体或当前计算的目标值。
    def upsert_relation(
        self,
        source: dict[str, Any],
        relation: dict[str, Any],
        target: dict[str, Any],
    ) -> bool:
        if not self.upsert_entity(source) or not self.upsert_entity(target):
            return False
        try:
            self._execute(
                """
                MATCH (source:YukinoEntity {uid: $source_id})
                MATCH (target:YukinoEntity {uid: $target_id})
                MERGE (source)-[edge:YUKINO_RELATION {uid: $uid}]->(target)
                SET edge.session_id = $session_id,
                    edge.relation = $relation,
                    edge.properties_json = $properties_json,
                    edge.confidence = $confidence,
                    edge.source = $source,
                    edge.updated_at = $updated_at
                """,
                uid=str(relation["id"]),
                source_id=str(source["id"]),
                target_id=str(target["id"]),
                session_id=str(relation["session_id"]),
                relation=str(relation["relation"]),
                properties_json=json.dumps(relation.get("properties", {}), ensure_ascii=False),
                confidence=float(relation.get("confidence", 1.0)),
                source=str(relation.get("source_kind", "explicit")),
                updated_at=float(relation.get("updated_at", time.time())),
            )
            self._writes += 1
            self._mark_connected()
            return True
        except Exception as exc:
            self._record_failure(exc)
            return False

    # 作用：从 Neo4j 派生图删除指定会话的全部实体和关系。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def delete_session(self, session_id: str) -> bool:
        try:
            self._execute(
                "MATCH (entity:YukinoEntity {session_id: $session_id}) DETACH DELETE entity",
                session_id=session_id,
            )
            self._mark_connected()
            return True
        except Exception as exc:
            self._record_failure(exc)
            return False

    # 作用：删除指定关系投影并清理该会话中失去连接的孤立实体。
    # 参数 relation_ids：待从图谱投影删除的关系 ID 列表。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def delete_relations(self, relation_ids: list[str], *, session_id: str) -> bool:
        if not relation_ids:
            return True
        try:
            self._execute(
                """
                MATCH ()-[edge:YUKINO_RELATION]->()
                WHERE edge.uid IN $relation_ids DELETE edge
                """,
                relation_ids=relation_ids,
            )
            self._execute(
                """
                MATCH (entity:YukinoEntity {session_id: $session_id})
                WHERE NOT (entity)--() DELETE entity
                """,
                session_id=session_id,
            )
            self._mark_connected()
            return True
        except Exception as exc:
            self._record_failure(exc)
            return False

    # 作用：清空所有 Yukino 图谱投影，供从关系事实源全量重建。
    # 参数：无。
    def clear_projection(self) -> bool:
        try:
            self._execute("MATCH (entity:YukinoEntity) DETACH DELETE entity")
            self._mark_connected()
            return True
        except Exception as exc:
            self._record_failure(exc)
            return False

    # 作用：返回连接和写入统计，可选验证连接并初始化唯一约束。
    # 参数 probe：是否主动连接外部后端验证可用性。
    def status(self, *, probe: bool = False) -> dict[str, object]:
        ok = not self._last_error
        if probe:
            try:
                self._get_driver().verify_connectivity()
                self._ensure_schema()
                self._mark_connected()
                ok = True
            except Exception as exc:
                self._record_failure(exc)
                ok = False
        return {
            "ok": ok,
            "enabled": True,
            "backend": self.name,
            "connection_state": (
                "error" if self._last_error else "connected" if self._probed else "unprobed"
            ),
            "location": self._public_location(),
            "database": self.database,
            "writes": self._writes,
            "failures": self._failures,
            "last_error": self._last_error,
            "source_of_truth": "relational",
        }

    # 作用：关闭 Neo4j 驱动并清除连接探测状态。
    # 参数：无。
    def close(self) -> None:
        driver = self._driver
        self._driver = None
        self._probed = False
        if driver is not None:
            driver.close()

    # 作用：按需创建 Neo4j 驱动，缺少可选依赖时给出明确错误。
    # 参数：无。
    def _get_driver(self) -> Any:
        if self._driver is not None:
            return self._driver
        factory = self._driver_factory
        if factory is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:
                raise RuntimeError(
                    "Neo4j 图谱投影需要安装 agi-yukino[neo4j]。"
                ) from exc
            factory = GraphDatabase.driver
        self._driver = factory(self.uri, auth=(self.username, self.password))
        return self._driver

    # 作用：幂等创建实体 UID 唯一约束，保证投影可重复写入。
    # 参数：无。
    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        self._execute(
            """
            CREATE CONSTRAINT yukino_entity_uid IF NOT EXISTS
            FOR (entity:YukinoEntity) REQUIRE entity.uid IS UNIQUE
            """
        )
        self._initialized = True

    # 作用：在配置数据库中执行参数化 Cypher 查询。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 parameters：传给参数化图数据库查询的命名参数集合。
    def _execute(self, query: str, **parameters: Any) -> Any:
        return self._get_driver().execute_query(
            query,
            parameters_=parameters,
            database_=self.database,
        )

    # 作用：隐藏 URI 中可能存在的用户信息后返回可展示地址。
    # 参数：无。
    def _public_location(self) -> str:
        return re.sub(r"//[^/@]+@", "//••••@", self.uri)

    # 作用：累计投影失败并保存截断后的最近异常信息。
    # 参数 exc：需要记录为后端失败状态的异常对象。
    def _record_failure(self, exc: Exception) -> None:
        self._failures += 1
        self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"

    # 作用：标记最近后端操作成功并清除旧错误。
    # 参数：无。
    def _mark_connected(self) -> None:
        self._probed = True
        self._last_error = ""


# 作用：以关系库保存会话级实体关系事实，并可同步到 Neo4j 派生投影。
# 参数：无。
class KnowledgeGraphStore:
    """Session-scoped property graph with a relational canonical store."""

    # 作用：注入关系事实后端与可选投影器，并初始化规范图表结构。
    # 参数 backend：关系、向量或图谱后端选择或后端实例。
    # 参数 projector：可选的 Neo4j 派生图投影器。
    def __init__(
        self,
        backend: RelationalBackend,
        *,
        projector: Neo4jGraphProjector | None = None,
    ) -> None:
        self.backend = backend
        self.projector = projector
        self._projection_failures = 0
        self.init()

    # 作用：创建实体、关系事实表及会话查询索引。
    # 参数：无。
    def init(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_entities (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    normalized_label TEXT NOT NULL,
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source TEXT NOT NULL DEFAULT 'explicit',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, entity_type, normalized_label)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_entities_session_label
                ON knowledge_entities(session_id, normalized_label, entity_type)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_relations (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source TEXT NOT NULL DEFAULT 'explicit',
                    evidence_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, source_id, relation_type, target_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_session_nodes
                ON knowledge_relations(session_id, source_id, target_id, relation_type)
                """
            )

    # 作用：规范化并幂等保存实体事实，再尽力更新派生图投影。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 label：情绪标签或知识实体显示名称。
    # 参数 entity_type：知识实体的类型标识。
    # 参数 properties：实体或关系附带的结构化属性。
    # 参数 confidence：实体、关系或学习事实的归一化置信度。
    # 参数 source：数据来源标识或源实体；具体形态由当前方法类型标注约定。
    def upsert_entity(
        self,
        session_id: str,
        label: str,
        entity_type: str = "concept",
        *,
        properties: dict[str, Any] | None = None,
        confidence: float = 1.0,
        source: str = "explicit",
    ) -> dict[str, Any]:
        session_id = session_id.strip() or "default"
        label = label.strip()
        entity_type = self._normalize_type(entity_type, fallback="concept")
        if not label:
            raise ValueError("实体名称不能为空。")
        normalized = self._normalize_label(label)
        entity_id = self._stable_id("entity", session_id, entity_type, normalized)
        now = time.time()
        encoded = json.dumps(properties or {}, ensure_ascii=False, separators=(",", ":"))
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_entities(
                    id, session_id, label, entity_type, normalized_label,
                    properties_json, confidence, source, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(session_id, entity_type, normalized_label) DO UPDATE SET
                    label = excluded.label,
                    properties_json = excluded.properties_json,
                    confidence = CASE
                        WHEN excluded.confidence > knowledge_entities.confidence
                        THEN excluded.confidence ELSE knowledge_entities.confidence END,
                    source = excluded.source,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    entity_id,
                    session_id,
                    label,
                    entity_type,
                    normalized,
                    encoded,
                    min(1.0, max(0.0, float(confidence))),
                    source[:80],
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM knowledge_entities
                WHERE session_id = ? AND entity_type = ? AND normalized_label = ?
                """,
                (session_id, entity_type, normalized),
            ).fetchone()
        entity = self._entity(row)
        self._project_entity(entity)
        return entity

    # 作用：确保两端实体并幂等保存带证据哈希的关系事实，再更新投影。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 source_label：知识关系起点实体的显示名称。
    # 参数 source_type：知识关系起点实体的类型。
    # 参数 relation：知识关系类型文本或完整关系事实。
    # 参数 target_label：知识关系终点实体的显示名称。
    # 参数 target_type：知识关系终点实体的类型。
    # 参数 properties：实体或关系附带的结构化属性。
    # 参数 confidence：实体、关系或学习事实的归一化置信度。
    # 参数 source_kind：知识关系或事实来源的类型标识。
    # 参数 evidence_hash：用于幂等去重和追溯来源的证据哈希。
    def upsert_relation(
        self,
        session_id: str,
        source_label: str,
        source_type: str,
        relation: str,
        target_label: str,
        target_type: str,
        *,
        properties: dict[str, Any] | None = None,
        confidence: float = 1.0,
        source_kind: str = "explicit",
        evidence_hash: str = "",
    ) -> dict[str, Any]:
        source = self.upsert_entity(
            session_id,
            source_label,
            source_type,
            confidence=confidence,
            source=source_kind,
        )
        target = self.upsert_entity(
            session_id,
            target_label,
            target_type,
            confidence=confidence,
            source=source_kind,
        )
        relation_type = self._normalize_type(relation, fallback="related_to")
        relation_id = self._stable_id(
            "relation", session_id, str(source["id"]), relation_type, str(target["id"])
        )
        now = time.time()
        encoded = json.dumps(properties or {}, ensure_ascii=False, separators=(",", ":"))
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_relations(
                    id, session_id, source_id, relation_type, target_id,
                    properties_json, confidence, source, evidence_hash,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(session_id, source_id, relation_type, target_id) DO UPDATE SET
                    properties_json = excluded.properties_json,
                    confidence = CASE
                        WHEN excluded.confidence > knowledge_relations.confidence
                        THEN excluded.confidence ELSE knowledge_relations.confidence END,
                    source = excluded.source,
                    evidence_hash = CASE
                        WHEN excluded.evidence_hash <> '' THEN excluded.evidence_hash
                        ELSE knowledge_relations.evidence_hash END,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    relation_id,
                    session_id,
                    source["id"],
                    relation_type,
                    target["id"],
                    encoded,
                    min(1.0, max(0.0, float(confidence))),
                    source_kind[:80],
                    evidence_hash[:128],
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM knowledge_relations
                WHERE session_id = ? AND source_id = ?
                  AND relation_type = ? AND target_id = ?
                """,
                (session_id, source["id"], relation_type, target["id"]),
            ).fetchone()
        edge = self._relation(row)
        self._project_relation(source, edge, target)
        return {"source": source, "edge": edge, "target": target}

    # 作用：从关系事实源列出会话中的活跃实体和关系。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list(self, session_id: str, *, limit: int = 1000) -> dict[str, list[dict[str, Any]]]:
        with self.backend.connect() as conn:
            nodes = conn.execute(
                """
                SELECT * FROM knowledge_entities
                WHERE session_id = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (session_id, max(1, min(limit, 5000))),
            ).fetchall()
            edges = conn.execute(
                """
                SELECT * FROM knowledge_relations
                WHERE session_id = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (session_id, max(1, min(limit * 2, 10000))),
            ).fetchall()
        return {
            "nodes": [self._entity(row) for row in nodes],
            "edges": [self._relation(row) for row in edges],
        }

    # 作用：判断规范关系图中是否不存在任何当前或历史事实。
    # 参数：无。
    def is_empty(self) -> bool:
        """Return whether the canonical graph contains no active or historical facts."""
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM knowledge_entities) AS entities,
                    (SELECT COUNT(*) FROM knowledge_relations) AS relations
                """
            ).fetchone()
        return int(row["entities"]) == 0 and int(row["relations"]) == 0

    # 作用：在关系事实源匹配种子实体，并扩展其一跳关系和邻居节点。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def search(self, session_id: str, query: str, *, limit: int = 100) -> dict[str, Any]:
        normalized = self._normalize_label(query)
        if not normalized:
            return self.list(session_id, limit=limit)
        like = f"%{normalized}%"
        with self.backend.connect() as conn:
            nodes = conn.execute(
                """
                SELECT * FROM knowledge_entities
                WHERE session_id = ? AND status = 'active'
                  AND (normalized_label LIKE ? OR entity_type LIKE ?)
                ORDER BY confidence DESC, updated_at DESC LIMIT ?
                """,
                (session_id, like, like, max(1, min(limit, 1000))),
            ).fetchall()
            node_ids = [str(row["id"]) for row in nodes]
            seed_node_ids = list(node_ids)
            edges: list[Any] = []
            if node_ids:
                placeholders = ",".join("?" for _ in node_ids)
                edges = conn.execute(
                    f"""
                    SELECT * FROM knowledge_relations
                    WHERE session_id = ? AND status = 'active'
                      AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (session_id, *node_ids, *node_ids, max(1, min(limit * 2, 2000))),
                ).fetchall()
                related_ids = sorted(
                    {
                        str(row[key])
                        for row in edges
                        for key in ("source_id", "target_id")
                    }
                    - set(node_ids)
                )
                if related_ids:
                    related_placeholders = ",".join("?" for _ in related_ids)
                    neighbors = conn.execute(
                        f"""
                        SELECT * FROM knowledge_entities
                        WHERE session_id = ? AND status = 'active'
                          AND id IN ({related_placeholders})
                        ORDER BY confidence DESC, updated_at DESC
                        """,
                        (session_id, *related_ids),
                    ).fetchall()
                    nodes = [*nodes, *neighbors]
        return {
            "nodes": [self._entity(row) for row in nodes],
            "edges": [self._relation(row) for row in edges],
            "seed_node_ids": seed_node_ids,
        }

    # 作用：从事实库删除会话全部图事实，并同步清理 Neo4j 投影。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect(immediate=True) as conn:
            edges = conn.execute(
                "DELETE FROM knowledge_relations WHERE session_id = ?", (session_id,)
            )
            nodes = conn.execute(
                "DELETE FROM knowledge_entities WHERE session_id = ?", (session_id,)
            )
        if self.projector is not None:
            self.projector.delete_session(session_id)
        return int(edges.rowcount or 0) + int(nodes.rowcount or 0)

    # 作用：删除源自指定证据的关系及其孤立实体，并同步派生图。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 evidence_hash：用于幂等去重和追溯来源的证据哈希。
    def delete_evidence(self, session_id: str, evidence_hash: str) -> int:
        if not evidence_hash:
            return 0
        with self.backend.connect(immediate=True) as conn:
            rows = conn.execute(
                """
                SELECT id FROM knowledge_relations
                WHERE session_id = ? AND evidence_hash = ?
                """,
                (session_id, evidence_hash),
            ).fetchall()
            relation_ids = [str(row["id"]) for row in rows]
            deleted = conn.execute(
                """
                DELETE FROM knowledge_relations
                WHERE session_id = ? AND evidence_hash = ?
                """,
                (session_id, evidence_hash),
            )
            orphans = conn.execute(
                """
                DELETE FROM knowledge_entities
                WHERE session_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM knowledge_relations relation
                    WHERE relation.source_id = knowledge_entities.id
                       OR relation.target_id = knowledge_entities.id
                  )
                """,
                (session_id,),
            )
        if self.projector is not None and relation_ids:
            self.projector.delete_relations(relation_ids, session_id=session_id)
        return int(deleted.rowcount or 0) + int(orphans.rowcount or 0)

    # 作用：从关系事实源全量重建 Neo4j 投影，可选先清空旧投影。
    # 参数 clear_existing：重建图谱投影前是否先清空旧投影。
    def rebuild_projection(self, *, clear_existing: bool = False) -> dict[str, int | bool]:
        if self.projector is None:
            return {"enabled": False, "entities": 0, "relations": 0, "failed": 0}
        if clear_existing and not self.projector.clear_projection():
            return {"enabled": True, "entities": 0, "relations": 0, "failed": 1}
        with self.backend.connect() as conn:
            nodes = conn.execute(
                "SELECT * FROM knowledge_entities WHERE status = 'active' ORDER BY id"
            ).fetchall()
            edges = conn.execute(
                "SELECT * FROM knowledge_relations WHERE status = 'active' ORDER BY id"
            ).fetchall()
        entities = {str(row["id"]): self._entity(row) for row in nodes}
        indexed_entities = 0
        indexed_relations = 0
        failed = 0
        for entity in entities.values():
            if self.projector.upsert_entity(entity):
                indexed_entities += 1
            else:
                failed += 1
        for row in edges:
            edge = self._relation(row)
            source = entities.get(str(edge["source"]))
            target = entities.get(str(edge["target"]))
            if source and target and self.projector.upsert_relation(source, edge, target):
                indexed_relations += 1
            else:
                failed += 1
        return {
            "enabled": True,
            "entities": indexed_entities,
            "relations": indexed_relations,
            "failed": failed,
        }

    # 作用：将旧 JSON 图节点和边迁移为规范关系事实。
    # 参数 graph：待迁移的旧版节点与边数据。
    def import_legacy(self, graph: dict[str, Any]) -> dict[str, int]:
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        node_map: dict[str, dict[str, Any]] = {}
        imported_nodes = 0
        imported_edges = 0
        for raw in nodes if isinstance(nodes, list) else []:
            if not isinstance(raw, dict):
                continue
            session_id = str(raw.get("session_id", "default"))
            label = str(raw.get("label", "")).strip()
            if not label:
                continue
            node = self.upsert_entity(
                session_id,
                label,
                str(raw.get("type", "concept")),
                source="legacy_json",
            )
            node_map[str(raw.get("id", ""))] = node
            imported_nodes += 1
        for raw in edges if isinstance(edges, list) else []:
            if not isinstance(raw, dict):
                continue
            source = node_map.get(str(raw.get("source", "")))
            target = node_map.get(str(raw.get("target", "")))
            if not source or not target:
                continue
            self.upsert_relation(
                str(raw.get("session_id", source["session_id"])),
                str(source["label"]),
                str(source["type"]),
                str(raw.get("relation", "related_to")),
                str(target["label"]),
                str(target["type"]),
                source_kind="legacy_json",
            )
            imported_edges += 1
        return {"nodes": imported_nodes, "edges": imported_edges}

    # 作用：汇总关系事实数量、投影状态和累计投影失败数。
    # 参数 probe：是否主动连接外部后端验证可用性。
    def status(self, *, probe: bool = False) -> dict[str, Any]:
        with self.backend.connect() as conn:
            entities = conn.execute("SELECT COUNT(*) AS count FROM knowledge_entities").fetchone()
            relations = conn.execute("SELECT COUNT(*) AS count FROM knowledge_relations").fetchone()
        projection = (
            self.projector.status(probe=probe)
            if self.projector is not None
            else {
                "ok": True,
                "enabled": False,
                "backend": "relational",
                "connection_state": "disabled",
                "source_of_truth": "relational",
            }
        )
        return {
            "ok": bool(projection.get("ok", True)),
            "fact_backend": self.backend.name,
            "entities": int(entities["count"]),
            "relations": int(relations["count"]),
            "projection": projection,
            "projection_failures": self._projection_failures,
        }

    # 作用：关闭可选 Neo4j 投影器连接。
    # 参数：无。
    def close(self) -> None:
        if self.projector is not None:
            self.projector.close()

    # 作用：尽力投影单个实体，失败只增加计数而不回滚事实库。
    # 参数 entity：待投影或转换的知识实体事实。
    def _project_entity(self, entity: dict[str, Any]) -> None:
        if self.projector is not None and not self.projector.upsert_entity(entity):
            self._projection_failures += 1

    # 作用：尽力投影完整关系，失败只增加计数而不影响规范事实。
    # 参数 source：数据来源标识或源实体；具体形态由当前方法类型标注约定。
    # 参数 edge：待投影或转换的知识关系事实。
    # 参数 target：知识关系目标实体或当前计算的目标值。
    def _project_relation(
        self,
        source: dict[str, Any],
        edge: dict[str, Any],
        target: dict[str, Any],
    ) -> None:
        if self.projector is not None and not self.projector.upsert_relation(source, edge, target):
            self._projection_failures += 1

    # 作用：将实体事实库行解码为统一领域字典。
    # 参数 row：待转换为领域对象的单条数据库记录。
    @staticmethod
    # 作用：执行“entity”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _entity(row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "label": str(row["label"]),
            "type": str(row["entity_type"]),
            "properties": KnowledgeGraphStore._json_object(row["properties_json"]),
            "confidence": float(row["confidence"]),
            "source": str(row["source"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    # 作用：将关系事实库行解码为统一领域字典。
    # 参数 row：待转换为领域对象的单条数据库记录。
    @staticmethod
    # 作用：执行“relation”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _relation(row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "source": str(row["source_id"]),
            "relation": str(row["relation_type"]),
            "target": str(row["target_id"]),
            "properties": KnowledgeGraphStore._json_object(row["properties_json"]),
            "confidence": float(row["confidence"]),
            "source_kind": str(row["source"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    # 作用：安全解码属性 JSON，格式异常时返回空对象。
    # 参数 value：待规范化、持久化或解析的业务值。
    @staticmethod
    # 作用：执行“json_object”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _json_object(value: Any) -> dict[str, Any]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    # 作用：归一化实体标签的大小写和空白，供稳定去重与搜索。
    # 参数 value：待规范化、持久化或解析的业务值。
    @staticmethod
    # 作用：执行“normalize_label”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _normalize_label(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().casefold())[:300]

    # 作用：将实体或关系类型规范为安全标识符，空值使用指定回退类型。
    # 参数 value：待规范化、持久化或解析的业务值。
    # 参数 fallback：类型规范化失败或为空时使用的默认值。
    @staticmethod
    # 作用：执行“normalize_type”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    # 参数 fallback：解析失败时返回的兜底对象。
    def _normalize_type(value: str, *, fallback: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_\u3400-\u9fff-]", "_", value.strip())[:80]
        return normalized or fallback

    # 作用：根据事实组成部分生成稳定短 ID，保证重复导入幂等。
    # 参数 parts：参与稳定哈希或标识生成的有序字符串片段。
    @staticmethod
    # 作用：执行“stable_id”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 parts：调用方传入的parts，用于本次处理。
    def _stable_id(*parts: str) -> str:
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        return digest[:24]


# 作用：根据环境与显式配置选择 Neo4j 投影器，关系库模式下保持无投影。
# 参数 backend：关系、向量或图谱后端选择或后端实例。
# 参数 uri：Milvus 或 Neo4j 服务的连接地址。
# 参数 username：连接 Neo4j 使用的用户名。
# 参数 password：连接 Neo4j 使用的密码；仅用于创建驱动。
# 参数 database：外部存储使用的数据库名称。
def configured_graph_projector(
    *,
    backend: str | None = None,
    uri: str | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> Neo4jGraphProjector | None:
    selected = (
        os.getenv("YUKINO_GRAPH_BACKEND", "relational")
        if backend is None
        else backend
    ).strip().lower()
    if selected in {"", "relational"}:
        return None
    if selected != "neo4j":
        raise GraphConfigurationError(
            "YUKINO_GRAPH_BACKEND 仅支持 relational 或 neo4j。"
        )
    resolved_uri = (os.getenv("YUKINO_NEO4J_URI", "") if uri is None else uri).strip()
    resolved_user = (
        os.getenv("YUKINO_NEO4J_USERNAME", "neo4j") if username is None else username
    ).strip()
    resolved_password = (
        os.getenv("YUKINO_NEO4J_PASSWORD", "") if password is None else password
    )
    parsed_uri = urlsplit(resolved_uri)
    try:
        parsed_uri.port
    except ValueError as exc:
        raise GraphConfigurationError("Neo4j URI 端口无效。") from exc
    if (
        parsed_uri.scheme
        not in {"neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc"}
        or not parsed_uri.hostname
        or parsed_uri.username
        or parsed_uri.password
        or parsed_uri.path not in {"", "/"}
        or parsed_uri.fragment
    ):
        raise GraphConfigurationError(
            "Neo4j URI 必须是无内嵌凭据、无嵌套路径的 neo4j:// 或 bolt:// 地址。"
        )
    if not resolved_user or not resolved_password:
        raise GraphConfigurationError("Neo4j 后端需要用户名和密码。")
    resolved_database = (
        os.getenv("YUKINO_NEO4J_DATABASE", "neo4j")
        if database is None
        else database
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", resolved_database):
        raise GraphConfigurationError("Neo4j database 名称格式无效。")
    return Neo4jGraphProjector(
        resolved_uri,
        username=resolved_user,
        password=resolved_password,
        database=resolved_database,
    )
