from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from service_club.storage.relational import (
    RelationalBackend,
    configured_relational_backend,
)


# 作用：表示同一请求标识被重复占用、内容冲突或执行结果已无法安全复用。
# 参数：无。
class AgentRequestConflict(RuntimeError):
    pass


# 作用：描述一次请求幂等领取的结果：首次执行或直接复用已完成响应。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“RequestClaim”相关的数据结构、异常类型或服务组件。
# 字段：state：该对象中的结构化字段。、response：该对象中的结构化字段。
class RequestClaim:
    state: Literal["new", "cached"]
    response: dict[str, Any] | None = None


# 作用：持久化聊天请求的幂等状态，阻止重复提交造成重复副作用。
# 参数：无。
class AgentRequestStore:
    """Persistent request idempotency for chat turns."""

    STALE_AFTER_SECONDS = 5 * 60

    # 作用：绑定关系型存储并初始化请求幂等表。
    # 参数 db_path：旧版路径参数，运行时不使用。
    # 参数 backend：可选的关系型存储后端；未提供时读取 PostgreSQL 配置。
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        if backend is None:
            backend = configured_relational_backend()
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self._ensure_schema()

    # 作用：创建保存请求指纹、执行状态与缓存响应的数据表。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_requests (
                    session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    PRIMARY KEY(session_id, request_id)
                )
                """
            )

    # 作用：将请求载荷稳定序列化并计算指纹，用于识别同 ID 下的内容变化。
    # 参数 payload：请求、作业或事件携带的结构化载荷。
    @staticmethod
    # 作用：根据关键字段生成稳定摘要，用于幂等、去重或完整性校验。
    # 参数 payload：调用方传入的payload，用于本次处理。
    def fingerprint(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    # 作用：原子领取请求执行权；已完成请求返回缓存，其余冲突进入安全失败。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    # 参数 request_hash：排除 request_id 后计算的请求内容指纹。
    def claim(self, session_id: str, request_id: str, request_hash: str) -> RequestClaim:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            inserted = conn.execute(
                """
                INSERT INTO agent_requests(
                    session_id, request_id, request_hash, status, started_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                ON CONFLICT(session_id, request_id) DO NOTHING
                """,
                (session_id, request_id, request_hash, now, now),
            )
            if inserted.rowcount == 1:
                return RequestClaim("new")
            row = conn.execute(
                "SELECT * FROM agent_requests WHERE session_id = ? AND request_id = ?"
                + self.backend.for_update(),
                (session_id, request_id),
            ).fetchone()
            if row is None:
                raise AgentRequestConflict("请求幂等记录在并发领取时丢失。")
            if str(row["request_hash"]) != request_hash:
                raise AgentRequestConflict("同一个 request_id 不能用于不同的聊天内容。")
            if row["status"] == "completed":
                try:
                    response = json.loads(str(row["response_json"]))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise AgentRequestConflict("缓存的 Agent 响应已经损坏。") from exc
                return RequestClaim("cached", response)
            if row["status"] == "running" and now - float(row["updated_at"]) < self.STALE_AFTER_SECONDS:
                raise AgentRequestConflict("这条委托仍在执行，请不要重复提交。")
            if row["status"] == "running":
                raise AgentRequestConflict(
                    "这条委托曾被中断，执行结果不确定。为避免重复操作，请先检查结果，再使用新的 request_id 重试。"
                )
            raise AgentRequestConflict(
                "这条委托执行失败过。为避免重复副作用，请使用新的 request_id 明确重试。"
            )

    # 作用：将请求标记为完成并保存可供重复请求复用的响应。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    # 参数 response：成功请求需要缓存并供幂等复用的响应。
    def complete(self, session_id: str, request_id: str, response: dict[str, Any]) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_requests
                SET status = 'completed', response_json = ?, error = '', updated_at = ?
                WHERE session_id = ? AND request_id = ?
                """,
                (json.dumps(response, ensure_ascii=False), time.time(), session_id, request_id),
            )

    # 作用：记录失败终态，避免调用方在结果不明时自动重放原请求。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    # 参数 error：需要持久化或返回的错误说明。
    def fail(self, session_id: str, request_id: str, error: str) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_requests
                SET status = 'failed', error = ?, updated_at = ?
                WHERE session_id = ? AND request_id = ?
                """,
                (error[:1000], time.time(), session_id, request_id),
            )

    # 作用：删除指定会话的全部请求幂等记录并返回删除数量。
    # 参数 session_id：当前用户会话的唯一标识。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM agent_requests WHERE session_id = ?", (session_id,)
            )
        return int(cursor.rowcount or 0)

    # 作用：服务启动时将遗留的运行中请求转为失败，强制人工确认后再重试。
    # 参数：无。
    def recover_interrupted(self) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_requests
                SET status = 'failed', error = '服务重启导致请求中断；请检查结果后使用新的 request_id。',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (time.time(),),
            )
        return int(cursor.rowcount or 0)
