from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


# 作用：表示向量后端名称、地址或集合前缀不满足安全配置约束。
# 参数：无。
class VectorStoreConfigurationError(ValueError):
    pass


# 作用：把关系库中的向量事实投影到可重建的 Milvus 派生索引。
# 参数：无。
class MilvusVectorStore:
    """Best-effort Milvus index backed by PostgreSQL embedding facts."""

    # 作用：保存连接与集合命名配置，客户端按需创建且不改变关系库事实源地位。
    # 参数 uri：Milvus 或 Neo4j 服务的连接地址。
    # 参数 token：连接 Milvus 使用的认证令牌。
    # 参数 database：外部存储使用的数据库名称。
    # 参数 collection_prefix：Milvus 受管集合使用的安全名称前缀。
    # 参数 client_factory：用于创建外部后端客户端的可注入工厂。
    def __init__(
        self,
        uri: str,
        *,
        token: str = "",
        database: str = "default",
        collection_prefix: str = "agi_yukino_memory",
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.uri = uri.strip()
        self.token = token.strip()
        self.database = database.strip() or "default"
        self.collection_prefix = self._safe_prefix(collection_prefix)
        self._client_factory = client_factory
        self._client: Any | None = None
        self._collections: set[str] = set()
        self._lock = threading.RLock()
        self._last_error = ""
        self._writes = 0
        self._searches = 0
        self._failures = 0
        self._probed = False

    # 作用：返回该派生向量后端的稳定名称。
    # 参数：无。
    @property
    # 作用：执行“name”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def name(self) -> str:
        return "milvus"

    # 作用：将单条关系库向量事实幂等投影到按模型和维度划分的集合。
    # 参数 memory_id：目标记忆事实的数据库标识。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 vector：记忆文本对应的浮点向量。
    # 参数 updated_at：事实或检查点的最后更新时间戳。
    def upsert(
        self,
        *,
        memory_id: int,
        session_id: str,
        model: str,
        vector: list[float],
        updated_at: float,
    ) -> bool:
        if memory_id <= 0 or not session_id or not model or not vector:
            return False
        try:
            collection = self._ensure_collection(model, len(vector))
            self._get_client().upsert(
                collection_name=collection,
                data=[
                    {
                        "memory_id": int(memory_id),
                        "embedding": [float(value) for value in vector],
                        "session_id": session_id,
                        "model": model,
                        "updated_at": float(updated_at),
                    }
                ],
            )
            self._writes += 1
            self._mark_connected()
            return True
        except Exception as exc:
            self._record_failure(exc)
            return False

    # 作用：仅在关系库提供的会话候选 ID 范围内执行向量检索，隔离陈旧索引。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 query_vector：当前查询文本对应的向量。
    # 参数 candidate_ids：关系事实源限定的候选记忆 ID 列表。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def search(
        self,
        *,
        session_id: str,
        model: str,
        query_vector: list[float],
        candidate_ids: list[int] | None = None,
        limit: int,
    ) -> dict[int, float]:
        candidates = sorted({int(value) for value in (candidate_ids or []) if int(value) > 0})
        if not session_id or not model or not query_vector:
            return {}
        collection = self._collection_name(model, len(query_vector))
        try:
            client = self._get_client()
            if not client.has_collection(collection_name=collection):
                self._mark_connected()
                return {}
            expression = f'session_id == "{self._escape_string(session_id)}"'
            if candidates:
                expression += f" and memory_id in [{','.join(str(value) for value in candidates)}]"
            raw = client.search(
                collection_name=collection,
                data=[[float(value) for value in query_vector]],
                filter=expression,
                limit=max(1, min(int(limit), len(candidates) if candidates else 500)),
                output_fields=["session_id", "model"],
            )
            self._searches += 1
            self._mark_connected()
            hits = raw[0] if raw and isinstance(raw, list) else []
            candidate_set = set(candidates)
            scores: dict[int, float] = {}
            for hit in hits:
                hit_id = self._hit_value(hit, "id")
                distance = self._hit_value(hit, "distance")
                try:
                    memory_id = int(hit_id)
                    score = min(1.0, max(0.0, float(distance)))
                except (TypeError, ValueError):
                    continue
                if not candidate_set or memory_id in candidate_set:
                    scores[memory_id] = score
            return scores
        except Exception as exc:
            self._record_failure(exc)
            return {}

    # 作用：从所有受管集合删除指定记忆 ID 的派生向量记录。
    # 参数 memory_ids：待读取、删除或评分的记忆事实 ID 列表。
    def delete_memory_ids(self, memory_ids: list[int]) -> int:
        values = sorted({int(value) for value in memory_ids if int(value) > 0})
        if not values:
            return 0
        expression = f"memory_id in [{','.join(str(value) for value in values)}]"
        return self._delete_from_managed(filter_expression=expression)

    # 作用：从所有受管集合删除指定会话的派生向量记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def delete_session(self, session_id: str) -> int:
        if not session_id:
            return 0
        expression = f'session_id == "{self._escape_string(session_id)}"'
        return self._delete_from_managed(filter_expression=expression)

    # 作用：删除当前前缀下全部派生集合，关系事实仍可用于完整重建。
    # 参数：无。
    def drop_managed_collections(self) -> int:
        dropped = 0
        try:
            client = self._get_client()
            for collection in self._managed_collections(client):
                client.drop_collection(collection_name=collection)
                dropped += 1
            self._collections.clear()
            self._mark_connected()
        except Exception as exc:
            self._record_failure(exc)
        return dropped

    # 作用：返回连接、集合和读写统计；可选主动探测后端可用性。
    # 参数 probe：是否主动连接外部后端验证可用性。
    def status(self, *, probe: bool = False) -> dict[str, object]:
        ok = not self._last_error
        collections: list[str] = sorted(self._collections)
        if probe:
            try:
                client = self._get_client()
                collections = self._managed_collections(client)
                ok = True
                self._mark_connected()
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
            "collection_prefix": self.collection_prefix,
            "collections": collections,
            "writes": self._writes,
            "searches": self._searches,
            "failures": self._failures,
            "last_error": self._last_error,
            "source_of_truth": "relational",
        }

    # 作用：关闭懒加载客户端并重置连接探测状态。
    # 参数：无。
    def close(self) -> None:
        client = self._client
        self._client = None
        self._probed = False
        close = getattr(client, "close", None)
        if callable(close):
            close()

    # 作用：线程安全地按需创建 Milvus 客户端，缺少可选依赖时给出明确错误。
    # 参数：无。
    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            factory = self._client_factory
            if factory is None:
                try:
                    from pymilvus import MilvusClient
                except ImportError as exc:
                    raise RuntimeError(
                        "Milvus 后端需要安装 agi-yukino[milvus]。"
                    ) from exc
                factory = MilvusClient
            kwargs: dict[str, Any] = {"uri": self.uri}
            if self.token:
                kwargs["token"] = self.token
            if self.database:
                kwargs["db_name"] = self.database
            kwargs["timeout"] = self._timeout_seconds()
            self._client = factory(**kwargs)
            return self._client

    # 作用：确保指定模型与维度对应的余弦向量集合已经创建。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 dimensions：向量的维度数量。
    def _ensure_collection(self, model: str, dimensions: int) -> str:
        collection = self._collection_name(model, dimensions)
        if collection in self._collections:
            return collection
        with self._lock:
            client = self._get_client()
            if not client.has_collection(collection_name=collection):
                client.create_collection(
                    collection_name=collection,
                    dimension=dimensions,
                    primary_field_name="memory_id",
                    vector_field_name="embedding",
                    metric_type="COSINE",
                    auto_id=False,
                    enable_dynamic_field=True,
                    consistency_level="Strong",
                )
            self._collections.add(collection)
        return collection

    # 作用：在全部受管集合执行同一过滤删除，并记录触及集合数。
    # 参数 filter_expression：在所有受管向量集合执行的删除过滤表达式。
    def _delete_from_managed(self, *, filter_expression: str) -> int:
        touched = 0
        try:
            client = self._get_client()
            for collection in self._managed_collections(client):
                client.delete(collection_name=collection, filter=filter_expression)
                touched += 1
            self._mark_connected()
        except Exception as exc:
            self._record_failure(exc)
        return touched

    # 作用：枚举并缓存属于当前安全前缀的 Milvus 集合。
    # 参数 client：用于枚举集合的 Milvus 客户端实例。
    def _managed_collections(self, client: Any) -> list[str]:
        names = client.list_collections()
        managed = sorted(
            str(name) for name in names if str(name).startswith(self.collection_prefix + "_")
        )
        self._collections.update(managed)
        return managed

    # 作用：根据模型指纹和向量维度生成稳定、互不冲突的集合名。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 dimensions：向量的维度数量。
    def _collection_name(self, model: str, dimensions: int) -> str:
        fingerprint = hashlib.sha256(model.encode("utf-8")).hexdigest()[:12]
        return f"{self.collection_prefix}_{fingerprint}_{int(dimensions)}"

    # 作用：生成隐藏内嵌凭据的后端地址，供状态接口安全展示。
    # 参数：无。
    def _public_location(self) -> str:
        match = re.match(r"^(https?://[^/@]+)(?::[^/@]*)?@(.+)$", self.uri)
        return f"{match.group(1)}@{match.group(2)}" if match else self.uri

    # 作用：累计失败次数并截断记录最近异常，供降级诊断。
    # 参数 exc：需要记录为后端失败状态的异常对象。
    def _record_failure(self, exc: Exception) -> None:
        self._failures += 1
        self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"

    # 作用：标记最近操作成功并清除旧错误状态。
    # 参数：无。
    def _mark_connected(self) -> None:
        self._probed = True
        self._last_error = ""

    # 作用：从环境读取并约束 Milvus 请求超时。
    # 参数：无。
    @staticmethod
    # 作用：执行“timeout_seconds”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _timeout_seconds() -> int:
        try:
            value = int(os.getenv("YUKINO_MILVUS_TIMEOUT_SECONDS", "5"))
        except ValueError:
            value = 5
        return max(1, min(value, 30))

    # 作用：规范并校验集合前缀，避免非法标识符影响后端操作。
    # 参数 value：待规范化、持久化或解析的业务值。
    @staticmethod
    # 作用：执行“safe_prefix”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _safe_prefix(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())[:80]
        if not normalized or not re.match(r"^[A-Za-z_]", normalized):
            raise VectorStoreConfigurationError("Milvus collection prefix 格式无效。")
        return normalized

    # 作用：转义 Milvus 过滤表达式中的字符串特殊字符。
    # 参数 value：待规范化、持久化或解析的业务值。
    @staticmethod
    # 作用：执行“escape_string”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _escape_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    # 作用：兼容字典和对象两种 Milvus 命中结构读取字段。
    # 参数 hit：Milvus 返回的单条向量检索命中。
    # 参数 key：永久记忆或配置项使用的稳定业务键。
    @staticmethod
    # 作用：执行“hit_value”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 hit：调用方传入的hit，用于本次处理。
    # 参数 key：调用方传入的key，用于本次处理。
    def _hit_value(hit: Any, key: str) -> Any:
        if isinstance(hit, dict):
            return hit.get(key)
        return getattr(hit, key, None)


# 作用：根据环境与显式配置选择 Milvus 派生索引，关系库或 pgvector 模式返回空投影。
# 参数 data_dir：本地兼容数据文件所在目录。
# 参数 backend：关系、向量或图谱后端选择或后端实例。
# 参数 uri：Milvus 或 Neo4j 服务的连接地址。
# 参数 token：连接 Milvus 使用的认证令牌。
# 参数 database：外部存储使用的数据库名称。
# 参数 collection_prefix：Milvus 受管集合使用的安全名称前缀。
def configured_vector_store(
    data_dir: str | Path,
    *,
    backend: str | None = None,
    uri: str | None = None,
    token: str | None = None,
    database: str | None = None,
    collection_prefix: str | None = None,
) -> MilvusVectorStore | None:
    selected = (
        os.getenv("YUKINO_VECTOR_BACKEND", "relational")
        if backend is None
        else backend
    ).strip().lower()
    if selected in {"", "relational", "pgvector"}:
        return None
    if selected != "milvus":
        raise VectorStoreConfigurationError(
            "YUKINO_VECTOR_BACKEND 仅支持 relational 或 milvus。"
        )
    resolved_uri = (
        os.getenv("YUKINO_MILVUS_URI", "") if uri is None else uri
    ).strip()
    if not resolved_uri:
        raise VectorStoreConfigurationError(
            "启用 Milvus 时必须配置 YUKINO_MILVUS_URI；"
            "推荐使用 docker-compose.storage.yml 提供的 Standalone 服务。"
        )
    parsed_uri = urlsplit(resolved_uri)
    try:
        parsed_uri.port
    except ValueError as exc:
        raise VectorStoreConfigurationError("Milvus URI 端口无效。") from exc
    if (
        parsed_uri.scheme not in {"http", "https"}
        or not parsed_uri.hostname
        or parsed_uri.username
        or parsed_uri.password
        or parsed_uri.path not in {"", "/"}
        or parsed_uri.query
        or parsed_uri.fragment
    ):
        raise VectorStoreConfigurationError(
            "Milvus URI 必须是无内嵌凭据、无路径的 http:// 或 https:// 服务地址；"
            "不再隐式启用 Lite 文件模式。"
        )
    return MilvusVectorStore(
        resolved_uri,
        token=os.getenv("YUKINO_MILVUS_TOKEN", "") if token is None else token,
        database=(
            os.getenv("YUKINO_MILVUS_DATABASE", "default")
            if database is None
            else database
        ),
        collection_prefix=(
            os.getenv("YUKINO_MILVUS_COLLECTION_PREFIX", "agi_yukino_memory")
            if collection_prefix is None
            else collection_prefix
        ),
    )
