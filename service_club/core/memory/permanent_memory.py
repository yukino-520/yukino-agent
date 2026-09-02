import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from service_club.storage.relational import RelationalBackend


# 作用：表示一条可跨会话读取、带来源和时间的永久记忆事实。
# 参数：无。
@dataclass
# 作用：定义“PermanentMemoryEntry”相关的数据结构、异常类型或服务组件。
# 字段：user_id：该对象中的结构化字段。、category：该对象中的结构化字段。、key：该对象中的结构化字段。、value：该对象中的结构化字段。、source：该对象中的结构化字段。、timestamp：该对象中的结构化字段。
class PermanentMemoryEntry:
    user_id: str
    category: str
    key: str
    value: str
    source: str = "system"
    timestamp: float = field(default_factory=time.time)


# 作用：管理跨会话永久记忆；关系库可用时作为事实源，JSON 仅作本地兼容后端。
# 参数：无。
class PermanentMemoryManager:
    # 作用：初始化关系库或 JSON 后端，并可将旧 JSON 条目一次性迁移到事实库。
    # 参数 data_dir：本地兼容数据文件所在目录。
    # 参数 backend：关系、向量或图谱后端选择或后端实例。
    # 参数 migrate_legacy：是否把旧 JSON 永久记忆迁移到关系事实库。
    def __init__(
        self,
        data_dir: str | Path = "data",
        *,
        backend: RelationalBackend | None = None,
        migrate_legacy: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "permanent_memories.json"
        self.backend = backend
        self._entries: dict[str, dict[str, PermanentMemoryEntry]] = {}
        self._lock = threading.RLock()
        self._load()
        if self.backend is not None:
            self._ensure_schema()
            if migrate_legacy:
                self._import_legacy_entries()
            self._entries = {}

    # 作用：以用户来源写入或更新一项跨会话偏好事实。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    # 参数 key：永久记忆或配置项使用的稳定业务键。
    # 参数 value：待规范化、持久化或解析的业务值。
    def set_preference(self, user_id: str, key: str, value: str) -> PermanentMemoryEntry:
        return self.store(user_id, "preference", key, value, source="user")

    # 作用：原子记录只写一次的关键事件，同键已存在时返回原事实。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    # 参数 key：永久记忆或配置项使用的稳定业务键。
    # 参数 value：待规范化、持久化或解析的业务值。
    def record_key_event(self, user_id: str, key: str, value: str) -> PermanentMemoryEntry:
        if self.backend is not None:
            with self.backend.connect(immediate=True) as conn:
                self.backend.lock_scope(conn, f"permanent-memory:{user_id}:{key}")
                existing = conn.execute(
                    """
                    SELECT user_id, category, key, value, source, timestamp
                    FROM permanent_memories WHERE user_id = ? AND key = ?
                    """,
                    (user_id, key),
                ).fetchone()
                if existing is not None:
                    return self._row_to_entry(existing)
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO permanent_memories(
                        user_id, category, key, value, source, timestamp, updated_at
                    ) VALUES (?, 'key_event', ?, ?, 'system', ?, ?)
                    """,
                    (user_id, key, value, now, now),
                )
            return PermanentMemoryEntry(
                user_id=user_id,
                category="key_event",
                key=key,
                value=value,
                timestamp=now,
            )
        with self._lock:
            existing = self._entries.get(user_id, {}).get(key)
            if existing is not None:
                return existing
            return self.store(user_id, "key_event", key, value)

    # 作用：按用户和键新增或覆盖一条永久事实，并持久化到当前后端。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    # 参数 category：记忆或画像事实所属的业务类别。
    # 参数 key：永久记忆或配置项使用的稳定业务键。
    # 参数 value：待规范化、持久化或解析的业务值。
    # 参数 source：数据来源标识或源实体；具体形态由当前方法类型标注约定。
    def store(
        self,
        user_id: str,
        category: str,
        key: str,
        value: str,
        *,
        source: str = "system",
    ) -> PermanentMemoryEntry:
        entry = PermanentMemoryEntry(
            user_id=user_id,
            category=category,
            key=key,
            value=value,
            source=source,
        )
        if self.backend is not None:
            with self.backend.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO permanent_memories(
                        user_id, category, key, value, source, timestamp, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, key) DO UPDATE SET
                        category = excluded.category,
                        value = excluded.value,
                        source = excluded.source,
                        timestamp = excluded.timestamp,
                        updated_at = excluded.updated_at
                    """,
                    (
                        entry.user_id,
                        entry.category,
                        entry.key,
                        entry.value,
                        entry.source,
                        entry.timestamp,
                        entry.timestamp,
                    ),
                )
            return entry
        with self._lock:
            self._entries.setdefault(user_id, {})[key] = entry
            self._save()
        return entry

    # 作用：将用户全部永久事实格式化为可直接注入模型的文本段。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    def get_prompt_segment(self, user_id: str) -> str:
        if self.backend is not None:
            entries = self._database_entries(user_id)
            if not entries:
                return "- 无跨会话永久记忆"
            return "\n".join(
                f"- {entry.category}/{entry.key}: {entry.value}"
                for entry in entries
            )
        with self._lock:
            entries = self._entries.get(user_id, {})
            if not entries:
                return "- 无跨会话永久记忆"
            return "\n".join(
                f"- {entry.category}/{entry.key}: {entry.value}"
                for entry in entries.values()
            )

    # 作用：返回逐条格式化的永久事实，供检索或提示词调用方自行组合。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    def entries_for_prompt(self, user_id: str) -> list[str]:
        if self.backend is not None:
            return [
                f"{entry.category}/{entry.key}: {entry.value}"
                for entry in self._database_entries(user_id)
            ]
        with self._lock:
            return [
                f"{entry.category}/{entry.key}: {entry.value}"
                for entry in self._entries.get(user_id, {}).values()
            ]

    # 作用：返回带来源和时间的结构化永久记忆列表。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    def list_entries(self, user_id: str) -> list[dict[str, object]]:
        if self.backend is not None:
            return [
                {
                    "category": entry.category,
                    "key": entry.key,
                    "value": entry.value,
                    "source": entry.source,
                    "timestamp": entry.timestamp,
                }
                for entry in self._database_entries(user_id)
            ]
        with self._lock:
            return [
                {
                    "category": entry.category,
                    "key": entry.key,
                    "value": entry.value,
                    "source": entry.source,
                    "timestamp": entry.timestamp,
                }
                for entry in sorted(
                    self._entries.get(user_id, {}).values(),
                    key=lambda item: item.timestamp,
                    reverse=True,
                )
            ]

    # 作用：按精确键删除一条永久记忆事实。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    # 参数 key：永久记忆或配置项使用的稳定业务键。
    def forget_key(self, user_id: str, key: str) -> bool:
        if self.backend is not None:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM permanent_memories WHERE user_id = ? AND key = ?",
                    (user_id, key),
                )
            return int(cursor.rowcount or 0) == 1
        with self._lock:
            entries = self._entries.get(user_id, {})
            if key not in entries:
                return False
            del entries[key]
            if not entries:
                self._entries.pop(user_id, None)
            self._save()
            return True

    # 作用：从永久事实中过滤并返回当前用户偏好键值。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    def preferences(self, user_id: str) -> dict[str, str]:
        if self.backend is not None:
            return {
                entry.key: entry.value
                for entry in self._database_entries(user_id)
                if entry.category == "preference"
            }
        with self._lock:
            return {
                entry.key: entry.value
                for entry in self._entries.get(user_id, {}).values()
                if entry.category == "preference"
            }

    # 作用：按类别、键、值或来源的包含关系批量删除匹配永久事实。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    def forget(self, user_id: str, query: str) -> int:
        query = query.strip()
        if not query:
            return 0
        if self.backend is not None:
            matching_keys = [
                entry.key
                for entry in self._database_entries(user_id)
                if self._matches(entry, query)
            ]
            if not matching_keys:
                return 0
            placeholders = ",".join("?" for _ in matching_keys)
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    f"DELETE FROM permanent_memories "
                    f"WHERE user_id = ? AND key IN ({placeholders})",
                    [user_id, *matching_keys],
                )
            return int(cursor.rowcount or 0)
        with self._lock:
            entries = self._entries.get(user_id, {})
            matching_keys = [
                key
                for key, entry in entries.items()
                if self._matches(entry, query)
            ]
            for key in matching_keys:
                del entries[key]
            if matching_keys:
                self._save()
            return len(matching_keys)

    # 作用：清空指定用户的全部永久记忆并返回删除数量。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    def clear(self, user_id: str) -> int:
        if self.backend is not None:
            with self.backend.connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM permanent_memories WHERE user_id = ?",
                    (user_id,),
                )
            return int(cursor.rowcount or 0)
        with self._lock:
            entries = self._entries.pop(user_id, {})
            if entries:
                self._save()
            return len(entries)

    # 作用：汇总当前永久记忆后端及用户、条目数量。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        if self.backend is not None:
            with self.backend.connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT user_id) AS users, COUNT(*) AS entries
                    FROM permanent_memories
                    """
                ).fetchone()
            return {
                "backend": self.backend.name,
                "user_count": int(row["users"]),
                "entry_count": int(row["entries"]),
            }
        with self._lock:
            return {
                "user_count": len(self._entries),
                "entry_count": sum(len(items) for items in self._entries.values()),
            }

    # 作用：在关系事实库创建永久记忆表和用户时间索引。
    # 参数：无。
    def _ensure_schema(self) -> None:
        assert self.backend is not None
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS permanent_memories (
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    timestamp {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    PRIMARY KEY(user_id, key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_permanent_memories_user_time
                ON permanent_memories(user_id, timestamp DESC)
                """
            )

    # 作用：将内存中加载的旧 JSON 条目幂等迁移到关系事实库。
    # 参数：无。
    def _import_legacy_entries(self) -> None:
        if self.backend is None or not self._entries:
            return
        with self.backend.connect(immediate=True) as conn:
            for entries in self._entries.values():
                for entry in entries.values():
                    conn.execute(
                        """
                        INSERT INTO permanent_memories(
                            user_id, category, key, value, source, timestamp, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, key) DO NOTHING
                        """,
                        (
                            entry.user_id,
                            entry.category,
                            entry.key,
                            entry.value,
                            entry.source,
                            entry.timestamp,
                            entry.timestamp,
                        ),
                    )

    # 作用：从关系事实库按时间倒序读取指定用户的永久记忆。
    # 参数 user_id：永久记忆所属用户的唯一标识。
    def _database_entries(self, user_id: str) -> list[PermanentMemoryEntry]:
        assert self.backend is not None
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, category, key, value, source, timestamp
                FROM permanent_memories
                WHERE user_id = ?
                ORDER BY timestamp DESC, key
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    # 作用：将关系库行转换为领域对象并规范字段类型。
    # 参数 row：待转换为领域对象的单条数据库记录。
    @staticmethod
    # 作用：执行“row_to_entry”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _row_to_entry(row: Any) -> PermanentMemoryEntry:
        return PermanentMemoryEntry(
            user_id=str(row["user_id"]),
            category=str(row["category"]),
            key=str(row["key"]),
            value=str(row["value"]),
            source=str(row["source"]),
            timestamp=float(row["timestamp"]),
        )

    # 作用：从兼容 JSON 文件加载永久记忆，损坏时回退为空集合。
    # 参数：无。
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._entries = {}
            return
        self._entries = {
            user_id: {
                key: PermanentMemoryEntry(**entry)
                for key, entry in entries.items()
            }
            for user_id, entries in data.items()
        }

    # 作用：原子保存 JSON 后端；主目录不可写时回退到系统临时目录。
    # 参数：无。
    def _save(self) -> None:
        data = {
            user_id: {key: asdict(entry) for key, entry in entries.items()}
            for user_id, entries in self._entries.items()
        }
        try:
            self._write_json(data)
        except OSError:
            self.data_dir = Path(tempfile.gettempdir()) / "agi_yukino_memory"
            self.path = self.data_dir / "permanent_memories.json"
            self._write_json(data)

    # 作用：通过临时文件替换方式原子写入 JSON，避免中途失败损坏事实文件。
    # 参数 data：待原子写入 JSON 后端的完整数据对象。
    def _write_json(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # 作用：判断删除查询是否命中永久记忆的任一可搜索字段。
    # 参数 entry：待匹配或转换的永久记忆条目。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    def _matches(self, entry: PermanentMemoryEntry, query: str) -> bool:
        return any(
            query in value
            for value in (entry.category, entry.key, entry.value, entry.source)
        )
