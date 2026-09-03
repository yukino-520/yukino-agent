from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from service_club.storage.relational import RelationalBackend, configured_relational_backend


# 作用：持久保存需要用户确认的文件或外部能力操作，并控制其状态流转。
# 构造参数：db_path 和 backend 的含义由 __init__ 说明。
class AgentOperationStore:
    """Durable confirmation queue for side-effecting Agent operations."""

    DEFAULT_TTL_SECONDS = 30 * 60

    # 作用：初始化确认操作存储，并确保数据库结构可用。
    # 参数 db_path：旧版路径参数，运行时不使用。
    # 参数 backend：可选关系存储后端，用于 PostgreSQL 统一读写。
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

    # 作用：创建待确认操作表和所需索引，并兼容旧表字段迁移。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_operations (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at {self.backend.float_type} NOT NULL,
                    expires_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL
                )
                """
            )
            conn.execute(
                """
                ALTER TABLE agent_operations
                ADD COLUMN IF NOT EXISTS task_id TEXT NOT NULL DEFAULT ''
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_operations_session_status
                ON agent_operations(session_id, status, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_operations_dedupe
                ON agent_operations(session_id, payload_hash, status)
                """
            )

    # 作用：创建一条带有效期的待确认操作，供用户确认后再真正执行副作用。
    # 参数 session_id：操作归属的会话标识。
    # 参数 kind：操作类型，例如 file 或 capability。
    # 参数 action：确认后要执行的具体动作。
    # 参数 payload：确认后执行所需的完整参数。
    # 参数 summary：展示给用户的安全操作摘要。
    # 参数 task_id：可选来源任务标识，用于回写任务状态。
    # 参数 ttl_seconds：待确认操作的有效秒数。
    # 返回：创建后的操作记录。
    def queue(
        self,
        *,
        session_id: str,
        kind: str,
        action: str,
        payload: dict[str, Any],
        summary: str,
        task_id: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(f"{kind}:{action}:{encoded}".encode()).hexdigest()
        expires_at = now + max(60, min(int(ttl_seconds), 24 * 60 * 60))
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(
                conn,
                f"agent-operation:queue:{session_id}:{task_id}:{payload_hash}",
            )
            conn.execute(
                """
                UPDATE agent_operations
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now, now),
            )
            if task_id:
                existing = conn.execute(
                    """
                    SELECT * FROM agent_operations
                    WHERE session_id = ? AND task_id = ? AND payload_hash = ?
                      AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, task_id, payload_hash),
                ).fetchone()
            else:
                existing = conn.execute(
                    """
                    SELECT * FROM agent_operations
                    WHERE session_id = ? AND payload_hash = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, payload_hash),
                ).fetchone()
            if existing is not None:
                return self._decode(existing)
            interrupted = None
            if task_id:
                task_table = self.backend.table_exists(conn, "agent_tasks")
                task_running = (
                    conn.execute(
                        "SELECT 1 FROM agent_tasks WHERE id = ? AND status = 'running'",
                        (task_id,),
                    ).fetchone()
                    if task_table
                    else None
                )
                if task_running is not None:
                    interrupted = conn.execute(
                        """
                        SELECT * FROM agent_operations
                        WHERE session_id = ? AND task_id = ? AND payload_hash = ?
                          AND status = 'interrupted'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id, task_id, payload_hash),
                    ).fetchone()
            if interrupted is not None:
                conn.execute(
                    """
                    UPDATE agent_operations
                    SET status = 'pending', result_json = '', error = '',
                        expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'interrupted'
                    """,
                    (expires_at, now, str(interrupted["id"])),
                )
                refreshed = conn.execute(
                    "SELECT * FROM agent_operations WHERE id = ?",
                    (str(interrupted["id"]),),
                ).fetchone()
                if refreshed is not None:
                    return self._decode(refreshed)
            operation_id = uuid.uuid4().hex[:12]
            conn.execute(
                """
                INSERT INTO agent_operations(
                    id, session_id, task_id, kind, action, payload_json, payload_hash,
                    summary, status, created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    operation_id,
                    session_id,
                    task_id,
                    kind,
                    action,
                    encoded,
                    payload_hash,
                    summary,
                    now,
                    expires_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("待确认操作创建失败。")
        return self._decode(row)

    # 作用：原子领取一条待确认操作并把它标记为执行中，防止重复确认。
    # 参数 session_id：确认请求所属会话，避免跨会话领取。
    # 参数 operation_id：指定操作标识；为空时领取会话内最新待确认操作。
    # 返回：成功领取的操作记录；不存在或已过期时返回 None。
    def claim(self, session_id: str, operation_id: str = "") -> dict[str, Any] | None:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            if operation_id:
                row = conn.execute(
                    """
                    SELECT * FROM agent_operations
                    WHERE id = ? AND session_id = ? AND status = 'pending'
                    """
                    + self.backend.for_update(skip_locked=True),
                    (operation_id, session_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM agent_operations
                    WHERE session_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """
                    + self.backend.for_update(skip_locked=True),
                    (session_id,),
                ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                conn.execute(
                    "UPDATE agent_operations SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return None
            changed = conn.execute(
                """
                UPDATE agent_operations
                SET status = 'executing', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, row["id"]),
            )
            if changed.rowcount != 1:
                return None
        value = self._decode(row)
        value["status"] = "executing"
        return value

    # 作用：取消一条仍在等待确认的操作。
    # 参数 session_id：取消请求所属会话。
    # 参数 operation_id：指定操作标识；为空时取消会话内最新待确认操作。
    # 返回：被取消的操作记录；没有匹配项时返回 None。
    def cancel(self, session_id: str, operation_id: str = "") -> dict[str, Any] | None:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            if operation_id:
                row = conn.execute(
                    """
                    SELECT * FROM agent_operations
                    WHERE id = ? AND session_id = ? AND status = 'pending'
                    """
                    + self.backend.for_update(),
                    (operation_id, session_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM agent_operations
                    WHERE session_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """
                    + self.backend.for_update(),
                    (session_id,),
                ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE agent_operations SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        value = self._decode(row)
        value["status"] = "cancelled"
        return value

    # 作用：批量取消某个任务仍未确认的全部操作。
    # 参数 task_id：来源 Agent 任务标识。
    # 返回：实际取消的操作数量。
    def cancel_by_task(self, task_id: str) -> int:
        """Cancel every still-pending side effect created by one Agent task."""
        if not task_id:
            return 0
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_operations
                SET status = 'cancelled', updated_at = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (time.time(), task_id),
            )
        return int(cursor.rowcount)

    # 作用：按操作标识读取一条确认操作记录。
    # 参数 operation_id：待查询的操作标识。
    # 返回：解码后的操作记录；不存在时返回 None。
    def get(self, operation_id: str) -> dict[str, Any] | None:
        if not operation_id:
            return None
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    # 作用：列出某个 Agent 任务创建的全部确认操作。
    # 参数 task_id：来源任务标识。
    # 返回：按创建顺序排列的操作记录列表。
    def list_task(self, task_id: str) -> list[dict[str, Any]]:
        if not task_id:
            return []
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_operations
                WHERE task_id = ? ORDER BY created_at DESC
                """,
                (task_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    # 作用：删除指定会话的全部确认操作记录。
    # 参数 session_id：需要清理数据的会话标识。
    # 返回：实际删除的记录数量。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM agent_operations WHERE session_id = ?", (session_id,)
            )
        return int(cursor.rowcount or 0)

    # 作用：启动时把遗留 executing 操作标记为 interrupted，禁止将未知终态自动当作成功。
    # 参数：无。
    # 返回：中断操作数量和受影响任务标识。
    def recover_interrupted(self) -> dict[str, Any]:
        """Make operations claimed by a dead process explicit and non-replayable."""
        with self.backend.connect(immediate=True) as conn:
            rows = conn.execute(
                "SELECT task_id FROM agent_operations WHERE status = 'executing'"
            ).fetchall()
            task_ids = sorted(
                {str(row["task_id"]) for row in rows if str(row["task_id"])}
            )
            cursor = conn.execute(
                """
                UPDATE agent_operations
                SET status = 'interrupted',
                    error = '服务在操作执行期间停止；结果可能不确定，未自动重放。',
                    updated_at = ?
                WHERE status = 'executing'
                """,
                (time.time(),),
            )
        return {"interrupted_operations": int(cursor.rowcount or 0), "task_ids": task_ids}

    # 作用：经人工确认后把一条中断操作重新放回待确认状态。
    # 参数 session_id：操作所属会话。
    # 参数 operation_id：需要重新开放的中断操作标识。
    # 参数 ttl_seconds：重新开放后的有效秒数。
    # 返回：更新后的操作记录；状态不匹配时返回 None。
    def reopen_interrupted(
        self,
        session_id: str,
        operation_id: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any] | None:
        """Explicitly reopen one uncertain operation while preserving its identity."""
        now = time.time()
        expires_at = now + max(60, min(int(ttl_seconds), 24 * 60 * 60))
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operations
                WHERE id = ? AND session_id = ?
                """
                + self.backend.for_update(),
                (operation_id, session_id),
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) in {"interrupted", "pending"}:
                conn.execute(
                    """
                    UPDATE agent_operations
                    SET status = 'pending', result_json = '', error = '',
                        expires_at = ?, updated_at = ?
                    WHERE id = ? AND session_id = ?
                      AND status IN ('interrupted', 'pending')
                    """,
                    (expires_at, now, operation_id, session_id),
                )
            elif str(row["status"]) != "pending":
                return None
            refreshed = conn.execute(
                "SELECT * FROM agent_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(refreshed) if refreshed is not None else None

    # 作用：人工将中断操作结算为已完成、失败等明确终态。
    # 参数 session_id：操作所属会话。
    # 参数 operation_id：需要结算的中断操作标识。
    # 参数 status：人工确认的最终状态。
    # 参数 result：可选的结构化执行结果。
    # 参数 error：可选失败说明。
    # 返回：结算后的操作记录；无法更新时返回 None。
    def resolve_interrupted(
        self,
        session_id: str,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        if status not in {"completed", "cancelled"}:
            raise ValueError("不支持的确认单人工终态。")
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operations
                WHERE id = ? AND session_id = ?
                """
                + self.backend.for_update(),
                (operation_id, session_id),
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) in {"interrupted", "pending"}:
                conn.execute(
                    """
                    UPDATE agent_operations
                    SET status = ?, result_json = ?, error = ?, updated_at = ?
                    WHERE id = ? AND session_id = ?
                      AND status IN ('interrupted', 'pending')
                    """,
                    (
                        status,
                        json.dumps(result or {}, ensure_ascii=False),
                        error[:2000],
                        now,
                        operation_id,
                        session_id,
                    ),
                )
            elif str(row["status"]) != status:
                return None
            refreshed = conn.execute(
                "SELECT * FROM agent_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(refreshed) if refreshed is not None else None

    # 作用：把执行中的操作标记为完成并保存结果。
    # 参数 operation_id：需要完成的操作标识。
    # 参数 result：可审计的结构化执行结果。
    def complete(self, operation_id: str, result: dict[str, Any]) -> None:
        self._finish(operation_id, "completed", result=result)

    # 作用：把执行中的操作标记为失败。
    # 参数 operation_id：需要失败结算的操作标识。
    # 参数 error：失败原因。
    def fail(self, operation_id: str, error: str) -> None:
        self._finish(operation_id, "failed", error=error)

    # 作用：把结果不确定的执行中操作标记为中断，等待人工处理。
    # 参数 operation_id：需要中断结算的操作标识。
    # 参数 error：说明未知终态的错误信息。
    def interrupt(self, operation_id: str, error: str) -> None:
        self._finish(operation_id, "interrupted", error=error)

    # 作用：统一更新操作终态、结果、错误和更新时间。
    # 参数 operation_id：需要结算的操作标识。
    # 参数 status：目标终态。
    # 参数 result：可选结构化结果。
    # 参数 error：可选错误信息。
    def _finish(
        self,
        operation_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_operations
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE id = ? AND status = 'executing'
                """,
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    time.time(),
                    operation_id,
                ),
            )

    # 作用：把数据库行中的 JSON 字段和标量解码为业务操作字典。
    # 参数 row：关系后端返回的单行记录。
    # 返回：可供工具层使用的操作记录。
    @staticmethod
    # 作用：执行“decode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for source, target in (("payload_json", "payload"), ("result_json", "result")):
            try:
                value[target] = json.loads(str(value.pop(source) or "{}"))
            except (json.JSONDecodeError, TypeError):
                value[target] = {}
        return value
