"""Conversation thread metadata and server-side chat history."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from service_club.core.memory import MemoryManager
from service_club.storage.relational import RelationalBackend

CHAT_MODES = {"solo", "club"}
CHARACTERS = {"yukino", "yui", "hachiman", "iroha", "shizuka"}
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")


# 作用：管理可见会话线程元数据，并从详细对话事实日志构造消息历史。
# 参数：无。
class ConversationStore:
    """Keeps the thread list separate from the detailed memory store."""

    # 作用：复用记忆关系库作为事实源，并确保线程元数据表可用。
    # 参数 memory：提供会话记忆事实与混合检索能力的管理器。
    # 参数 backend：关系、向量或图谱后端选择或后端实例。
    def __init__(
        self,
        memory: MemoryManager,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        self.memory = memory
        self.backend = backend or memory.backend
        self._ensure_schema()

    # 作用：创建会话线程表与按模式、角色和更新时间查询的索引。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS conversation_threads (
                    id TEXT PRIMARY KEY,
                    chat_mode TEXT NOT NULL,
                    character TEXT NOT NULL,
                    title TEXT NOT NULL,
                    preview TEXT NOT NULL DEFAULT '',
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_threads_scope
                ON conversation_threads(chat_mode, character, updated_at DESC)
                """
            )

    # 作用：校验会话范围并创建一条带默认或指定标题的可见线程。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 conversation_id：目标会话线程的唯一标识。
    # 参数 title：创建会话线程时提供的初始标题。
    def create(
        self,
        *,
        chat_mode: str,
        character: str,
        conversation_id: str | None = None,
        title: str = "",
    ) -> dict[str, Any]:
        self._validate_scope(chat_mode, character)
        thread_id = conversation_id or f"thread-{uuid.uuid4()}"
        if not CONVERSATION_ID.fullmatch(thread_id):
            raise ValueError("invalid conversation id")
        now = time.time()
        clean_title = self._compact(title, limit=36) or self._default_title(chat_mode, character)
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO conversation_threads(
                    id, chat_mode, character, title, preview,
                    turn_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', 0, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (thread_id, chat_mode, character, clean_title, now, now),
            )
        thread = self.get(thread_id, include_messages=False)
        if thread is None:
            raise RuntimeError("conversation could not be created")
        return thread

    # 作用：将已有浏览器会话注册为线程，并从事实日志同步元数据而不复制消息。
    # 参数 conversation_id：目标会话线程的唯一标识。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    def adopt(
        self,
        *,
        conversation_id: str,
        chat_mode: str,
        character: str,
    ) -> dict[str, Any]:
        """Register the one pre-thread browser session without copying its data."""
        existing = self.get(conversation_id, include_messages=False)
        if existing is not None:
            return existing
        first_turn = self._first_turn(conversation_id)
        title = str(first_turn.get("user_message", "")) if first_turn else ""
        thread = self.create(
            conversation_id=conversation_id,
            chat_mode=chat_mode,
            character=character,
            title=title,
        )
        self._sync_from_logs(conversation_id)
        return self.get(conversation_id, include_messages=False) or thread

    # 作用：原子取得当前范围最新线程；不存在时创建首条线程。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    def ensure(self, *, chat_mode: str, character: str) -> dict[str, Any]:
        """Return the latest thread, atomically creating the first one for a scope."""
        self._validate_scope(chat_mode, character)
        where = "chat_mode = ?"
        params: list[Any] = [chat_mode]
        if chat_mode == "solo":
            where += " AND character = ?"
            params.append(character)
        thread_id = f"thread-{uuid.uuid4()}"
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(
                conn,
                f"conversation-scope:{chat_mode}:{character if chat_mode == 'solo' else '*'}",
            )
            existing = conn.execute(
                f"""
                SELECT id, chat_mode, character, title, preview,
                       turn_count, created_at, updated_at
                FROM conversation_threads
                WHERE {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """
                INSERT INTO conversation_threads(
                    id, chat_mode, character, title, preview,
                    turn_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', 0, ?, ?)
                """,
                (
                    thread_id,
                    chat_mode,
                    character,
                    self._default_title(chat_mode, character),
                    now,
                    now,
                ),
            )
        thread = self.get(thread_id, include_messages=False)
        if thread is None:
            raise RuntimeError("conversation could not be ensured")
        return thread

    # 作用：按聊天模式和角色列出最近更新的线程元数据。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list(self, *, chat_mode: str, character: str, limit: int = 100) -> list[dict[str, Any]]:
        self._validate_scope(chat_mode, character)
        safe_limit = max(1, min(limit, 200))
        where = "chat_mode = ?"
        params: list[Any] = [chat_mode]
        if chat_mode == "solo":
            where += " AND character = ?"
            params.append(character)
        params.append(safe_limit)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, chat_mode, character, title, preview,
                       turn_count, created_at, updated_at
                FROM conversation_threads
                WHERE {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # 作用：读取指定线程，并可选地附加由事实日志重建的消息列表。
    # 参数 conversation_id：目标会话线程的唯一标识。
    # 参数 include_messages：读取线程时是否附加完整消息历史。
    def get(self, conversation_id: str, *, include_messages: bool = True) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id, chat_mode, character, title, preview,
                       turn_count, created_at, updated_at
                FROM conversation_threads
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        thread = dict(row)
        if include_messages:
            thread["messages"] = self.messages(conversation_id)
        return thread

    # 作用：将每条对话日志展开为用户和助手两条前端消息，并恢复执行元数据。
    # 参数 conversation_id：目标会话线程的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def messages(self, conversation_id: str, *, limit: int = 120) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 400))
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_message, assistant_reply, character,
                       chat_mode, emotion, tool_results_json, attachments_json, trace_id,
                       execution_json, degraded, degradation_reason, created_at
                FROM conversation_logs
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, safe_limit),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            messages.append(
                {
                    "id": f"{item['id']}:user",
                    "role": "user",
                    "content": item["user_message"],
                    "attachments": self._json_list(item.get("attachments_json", "[]")),
                    "created_at": item["created_at"],
                }
            )
            messages.append(
                {
                    "id": f"{item['id']}:assistant",
                    "role": "assistant",
                    "content": item["assistant_reply"],
                    "character": item["character"],
                    "emotion": item["emotion"],
                    "tool_results": self._tool_results(item.get("tool_results_json", "[]")),
                    "execution": self._json_object(item.get("execution_json", "{}")),
                    "trace_id": item.get("trace_id", ""),
                    "degraded": bool(item.get("degraded", False)),
                    "degradation_reason": item.get("degradation_reason", ""),
                    "created_at": item["created_at"],
                }
            )
        return messages

    # 作用：安全解析工具结果 JSON，只保留字典元素。
    # 参数 raw：待安全解析的原始 JSON 值。
    @staticmethod
    # 作用：执行“tool_results”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 raw：尚未解析或规范化的原始输入。
    def _tool_results(raw: Any) -> list[dict[str, Any]]:
        try:
            value = json.loads(str(raw or "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    # 作用：安全解析通用 JSON 列表，只保留字典元素。
    # 参数 raw：待安全解析的原始 JSON 值。
    @staticmethod
    # 作用：执行“json_list”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 raw：尚未解析或规范化的原始输入。
    def _json_list(raw: Any) -> list[dict[str, Any]]:
        try:
            value = json.loads(str(raw or "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    # 作用：安全解析 JSON 对象，格式异常时返回空字典。
    # 参数 raw：待安全解析的原始 JSON 值。
    @staticmethod
    # 作用：执行“json_object”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 raw：尚未解析或规范化的原始输入。
    def _json_object(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    # 作用：在详细日志已保存后更新可见线程的标题、预览、轮数和时间。
    # 参数 conversation_id：目标会话线程的唯一标识。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 user_message：本轮用户消息原文。
    # 参数 assistant_reply：本轮助手最终回复文本。
    def record_turn(
        self,
        *,
        conversation_id: str,
        chat_mode: str,
        character: str,
        user_message: str,
        assistant_reply: str,
    ) -> None:
        self._validate_scope(chat_mode, character)
        if self.get(conversation_id, include_messages=False) is None:
            # Only explicit Web threads belong in the visible history list.
            # CLI, channel adapters and low-level tests may also use session IDs.
            return
        preview = self._compact(assistant_reply or user_message, limit=70)
        generated_title = self._compact(user_message, limit=28)
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE conversation_threads
                SET character = ?,
                    title = CASE WHEN turn_count = 0 THEN ? ELSE title END,
                    preview = ?,
                    turn_count = turn_count + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (character, generated_title, preview, now, conversation_id),
            )

    # 作用：重置线程展示元数据；详细事实日志由上层负责清理。
    # 参数 conversation_id：目标会话线程的唯一标识。
    def reset(self, conversation_id: str) -> dict[str, Any] | None:
        thread = self.get(conversation_id, include_messages=False)
        if thread is None:
            return None
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE conversation_threads
                SET title = ?, preview = '', turn_count = 0, updated_at = ?
                WHERE id = ?
                """,
                (
                    self._default_title(str(thread["chat_mode"]), str(thread["character"])),
                    now,
                    conversation_id,
                ),
            )
        return self.get(conversation_id, include_messages=False)

    # 作用：删除指定线程元数据并返回是否实际删除。
    # 参数 conversation_id：目标会话线程的唯一标识。
    def delete(self, conversation_id: str) -> bool:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM conversation_threads WHERE id = ?", (conversation_id,)
            )
        return cursor.rowcount > 0

    # 作用：根据已有详细日志回填被接管线程的轮数、预览和时间范围。
    # 参数 conversation_id：目标会话线程的唯一标识。
    def _sync_from_logs(self, conversation_id: str) -> None:
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS turns, MIN(created_at) AS created_at,
                       MAX(created_at) AS updated_at
                FROM conversation_logs
                WHERE session_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT assistant_reply FROM conversation_logs
                WHERE session_id = ? ORDER BY id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if row is None or int(row["turns"] or 0) == 0:
                return
            conn.execute(
                """
                UPDATE conversation_threads
                SET preview = ?, turn_count = ?, created_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    self._compact(str(latest["assistant_reply"]), limit=70) if latest else "",
                    int(row["turns"]),
                    float(row["created_at"]),
                    float(row["updated_at"]),
                    conversation_id,
                ),
            )

    # 作用：读取会话最早的用户消息，用作接管线程的初始标题来源。
    # 参数 conversation_id：目标会话线程的唯一标识。
    def _first_turn(self, conversation_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT user_message FROM conversation_logs
                WHERE session_id = ? ORDER BY id ASC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    # 作用：归一化空白并按字符上限生成标题或预览文本。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    @staticmethod
    # 作用：执行“compact”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 text：待分析、记录或处理的自然语言文本。
    # 参数 limit：返回结果的最大数量。
    def _compact(text: str, *, limit: int) -> str:
        compacted = " ".join(text.strip().split())
        if len(compacted) <= limit:
            return compacted
        return compacted[: max(1, limit - 1)].rstrip() + "…"

    # 作用：根据群聊或单角色范围生成新线程的默认标题。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    @staticmethod
    # 作用：执行“default_title”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 chat_mode：调用方传入的chat_mode，用于本次处理。
    # 参数 character：角色标识，决定采用的角色配置或行为策略。
    def _default_title(chat_mode: str, character: str) -> str:
        if chat_mode == "club":
            return "新的部内会议"
        names = {
            "yukino": "与雪乃的新委托",
            "yui": "与结衣的新委托",
            "hachiman": "与八幡的新委托",
            "iroha": "与一色的新委托",
            "shizuka": "与平冢老师的新委托",
        }
        return names[character]

    # 作用：校验聊天模式与角色是否属于受支持枚举。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    @staticmethod
    # 作用：执行“validate_scope”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 chat_mode：调用方传入的chat_mode，用于本次处理。
    # 参数 character：角色标识，决定采用的角色配置或行为策略。
    def _validate_scope(chat_mode: str, character: str) -> None:
        if chat_mode not in CHAT_MODES:
            raise ValueError("invalid chat mode")
        if character not in CHARACTERS:
            raise ValueError("invalid character")
