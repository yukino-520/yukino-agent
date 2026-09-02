from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from service_club.storage.relational import RelationalBackend, SQLiteRelationalBackend


# 作用：持久化本地副作用的结构化回执，供崩溃恢复时判断是否已真实生效。
# 参数：无。
class EffectReceiptStore:
    """Durable structural receipts for reconciling local side effects after a crash."""

    # 作用：绑定关系型后端并初始化副作用回执表。
    # 参数 db_path：使用 SQLite 时的数据库文件路径。
    # 参数 backend：可选的关系型存储后端；未提供时使用 SQLite。
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        if backend is None:
            if db_path is None:
                raise ValueError("SQLite 副作用回执需要数据库路径。")
            backend = SQLiteRelationalBackend(db_path)
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self._ensure_schema()

    # 作用：创建以任务、动作和幂等键唯一约束的回执账本。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_effect_receipts (
                    id {self.backend.identity_type},
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'executing',
                    evidence_json TEXT NOT NULL DEFAULT '{{}}',
                    result_json TEXT NOT NULL DEFAULT '{{}}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    UNIQUE(task_id, action, idempotency_key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_effect_receipts_task
                ON agent_effect_receipts(task_id, status, id)
                """
            )

    # 作用：在副作用执行前原子创建 executing 回执，重复调用复用原记录。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    # 参数 evidence：执行副作用前保存的结构化预期证据。
    def begin(
        self,
        *,
        task_id: str,
        session_id: str,
        action: str,
        idempotency_key: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if not task_id or not idempotency_key:
            return {}
        now = time.time()
        encoded = self._encode(evidence)
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO agent_effect_receipts(
                    task_id, session_id, action, idempotency_key, status,
                    evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'executing', ?, ?, ?)
                ON CONFLICT(task_id, action, idempotency_key) DO NOTHING
                """,
                (
                    task_id,
                    session_id,
                    action[:100],
                    idempotency_key[:128],
                    encoded,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM agent_effect_receipts
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                """,
                (task_id, action[:100], idempotency_key[:128]),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：仅将已确认未生效的回执重新置为 executing，允许安全重试。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    def restart(
        self,
        *,
        task_id: str,
        action: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_effect_receipts
                SET status = 'executing', error = '', updated_at = ?
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                  AND status = 'not_applied'
                """,
                (now, task_id, action[:100], idempotency_key[:128]),
            )
            row = conn.execute(
                """
                SELECT * FROM agent_effect_receipts
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                """,
                (task_id, action[:100], idempotency_key[:128]),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：按任务、动作和幂等键查询一条副作用回执。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    def get(
        self,
        *,
        task_id: str,
        action: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not task_id or not idempotency_key:
            return {}
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_effect_receipts
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                """,
                (task_id, action[:100], idempotency_key[:128]),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：写入副作用终态、结构化结果或错误并返回最新回执。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 result：需要评估、持久化或返回的执行结果。
    # 参数 error：需要持久化或返回的错误说明。
    def finish(
        self,
        *,
        task_id: str,
        action: str,
        idempotency_key: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_effect_receipts
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                """,
                (
                    status[:80],
                    self._encode(result or {}),
                    error[:1000],
                    now,
                    task_id,
                    action[:100],
                    idempotency_key[:128],
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM agent_effect_receipts
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                """,
                (task_id, action[:100], idempotency_key[:128]),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：按执行顺序列出一个任务产生的全部副作用回执。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def list_task(self, task_id: str) -> list[dict[str, Any]]:
        if not task_id:
            return []
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_effect_receipts
                WHERE task_id = ? ORDER BY id
                """,
                (task_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    # 作用：将崩溃时仍在执行的回执标为 interrupted，等待持久证据对账。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def recover_interrupted(self, *, task_id: str = "") -> int:
        with self.backend.connect(immediate=True) as conn:
            query = """
                UPDATE agent_effect_receipts
                SET status = 'interrupted',
                    error = '服务在副作用完成确认前停止，正在根据持久证据对账。',
                    updated_at = ?
                WHERE status = 'executing'
            """
            parameters: tuple[Any, ...] = (time.time(),)
            if task_id:
                query += " AND task_id = ?"
                parameters = (*parameters, task_id)
            cursor = conn.execute(query, parameters)
        return int(cursor.rowcount or 0)

    # 作用：删除指定会话的副作用回执并返回删除数量。
    # 参数 session_id：当前用户会话的唯一标识。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM agent_effect_receipts WHERE session_id = ?",
                (session_id,),
            )
        return int(cursor.rowcount or 0)

    # 作用：按状态统计回执，并突出需要人工或程序对账的未决记录。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        try:
            with self.backend.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM agent_effect_receipts
                    GROUP BY status
                    """
                ).fetchall()
        except Exception as exc:
            return {"ok": False, "persistent": True, "error": str(exc)[:300]}
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "ok": True,
            "persistent": True,
            "structural_only": True,
            "reconciles_local_side_effects": True,
            "backend": self.backend.name,
            "counts": counts,
            "unresolved": sum(
                counts.get(status, 0)
                for status in ("interrupted", "conflict", "external_state_changed")
            ),
            "total": sum(counts.values()),
        }

    # 作用：将有限大小的结构化证据编码为 JSON，避免账本存入大载荷。
    # 参数 value：待清洗、规范化或编码的原始值。
    @staticmethod
    # 作用：执行“encode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _encode(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 20_000:
            return "{}"
        return encoded

    # 作用：将数据库行还原为带 evidence 和 result 字典的回执对象。
    # 参数 row：需要解码或计算的数据库查询行。
    @staticmethod
    # 作用：执行“decode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for source, target in (
            ("evidence_json", "evidence"),
            ("result_json", "result"),
        ):
            try:
                value = json.loads(str(item.pop(source) or "{}"))
            except (json.JSONDecodeError, TypeError):
                value = {}
            item[target] = value if isinstance(value, dict) else {}
        return item
