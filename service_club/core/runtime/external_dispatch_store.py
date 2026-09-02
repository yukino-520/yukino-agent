from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from service_club.storage.relational import RelationalBackend, SQLiteRelationalBackend


# 作用：记录已确认外部调用的发送阶段与供应商幂等键，禁止未知终态自动重放。
# 参数：无。
class ExternalDispatchStore:
    """Structural ledger for confirmed calls that can leave this process."""

    # 作用：绑定关系型后端并初始化外发结构化账本。
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
                raise ValueError("SQLite 外发账本需要数据库路径。")
            backend = SQLiteRelationalBackend(db_path)
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self._ensure_schema()

    # 作用：创建外发记录表及按状态、会话查询所需索引。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_external_dispatches (
                    id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    capability TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    provider_idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'prepared',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    receipt_json TEXT NOT NULL DEFAULT '{{}}',
                    error TEXT NOT NULL DEFAULT '',
                    provider_started_at {self.backend.float_type},
                    completed_at {self.backend.float_type},
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_external_dispatches_status
                ON agent_external_dispatches(status, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_external_dispatches_session
                ON agent_external_dispatches(session_id, updated_at DESC)
                """
            )

    # 作用：为确认单准备唯一外发记录，并派生稳定的供应商幂等键。
    # 参数 operation_id：用户确认单对应的原始操作唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 capability：被调用的扩展能力标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 payload_fingerprint：外发请求载荷的稳定摘要，用于核对内容一致性。
    def prepare(
        self,
        *,
        operation_id: str,
        session_id: str,
        task_id: str,
        capability: str,
        action: str,
        payload_fingerprint: str,
    ) -> dict[str, Any]:
        if not operation_id:
            return {}
        digest = hashlib.sha256(f"operation:{operation_id}".encode("utf-8")).hexdigest()
        dispatch_id = f"dispatch-{digest[:20]}"
        provider_key = f"agi-yukino-{digest[:32]}"
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO agent_external_dispatches(
                    id, operation_id, session_id, task_id, capability, action,
                    payload_fingerprint, provider_idempotency_key,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (
                    dispatch_id,
                    operation_id,
                    session_id,
                    task_id,
                    capability[:100],
                    action[:100],
                    payload_fingerprint[:128],
                    provider_key,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：在实际请求供应商前将外发置为 dispatching 并累计尝试次数。
    # 参数 operation_id：用户确认单对应的原始操作唯一标识。
    def start(self, operation_id: str) -> dict[str, Any]:
        if not operation_id:
            return {}
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_external_dispatches
                SET status = 'dispatching', attempts = attempts + 1,
                    receipt_json = '{}', error = '', provider_started_at = ?,
                    completed_at = NULL, updated_at = ?
                WHERE operation_id = ?
                  AND status IN ('prepared', 'failed', 'uncertain')
                """,
                (now, now, operation_id),
            )
            row = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：持久化供应商调用终态、精简回执和错误信息。
    # 参数 operation_id：用户确认单对应的原始操作唯一标识。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 receipt：外部供应商返回的精简结构化回执。
    # 参数 error：需要持久化或返回的错误说明。
    def finish(
        self,
        operation_id: str,
        *,
        status: str,
        receipt: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        if not operation_id:
            return {}
        now = time.time()
        completed_at = now if status == "completed" else None
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_external_dispatches
                SET status = ?, receipt_json = ?, error = ?, completed_at = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    status[:80],
                    self._encode(receipt or {}),
                    error[:1000],
                    completed_at,
                    now,
                    operation_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：按原始确认操作 ID 查询外发记录。
    # 参数 operation_id：用户确认单对应的原始操作唯一标识。
    def get_operation(self, operation_id: str) -> dict[str, Any]:
        if not operation_id:
            return {}
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：按公开外发 ID 查询结构化记录。
    # 参数 dispatch_id：外发账本记录的公开唯一标识。
    def get(self, dispatch_id: str) -> dict[str, Any]:
        if not dispatch_id:
            return {}
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE id = ?",
                (dispatch_id,),
            ).fetchone()
        return self._decode(row) if row is not None else {}

    # 作用：按会话和可选状态倒序列出外发历史。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def list_session(
        self,
        session_id: str,
        *,
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        bounded_limit = max(1, min(int(limit), 200))
        with self.backend.connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_external_dispatches
                    WHERE session_id = ? AND status = ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (session_id, status[:80], bounded_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_external_dispatches
                    WHERE session_id = ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (session_id, bounded_limit),
                ).fetchall()
        return [self._decode(row) for row in rows]

    # 作用：查询指定状态的近期外发记录，供恢复扫描和运维处理。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def list_status(self, status: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if not status:
            return []
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_external_dispatches
                WHERE status = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (status[:80], max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._decode(row) for row in rows]

    # 作用：列出一个 Agent 任务关联的全部外发记录。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def list_task(self, task_id: str) -> list[dict[str, Any]]:
        if not task_id:
            return []
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_external_dispatches
                WHERE task_id = ? ORDER BY created_at DESC
                """,
                (task_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    # 作用：根据用户决定把未知终态外发确认为成功或放弃，并保证重复处理幂等。
    # 参数 dispatch_id：外发账本记录的公开唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 decision：用户对未知外发所作的重试、确认成功或放弃决定。
    def resolve_uncertain(
        self,
        dispatch_id: str,
        *,
        session_id: str,
        decision: str,
    ) -> tuple[dict[str, Any], bool]:
        target_status = {
            "confirm_succeeded": "completed",
            "abandon": "abandoned",
        }.get(decision, "")
        if not target_status:
            raise ValueError("不支持的外发处理决定。")
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_external_dispatches
                WHERE id = ? AND session_id = ?
                """,
                (dispatch_id, session_id),
            ).fetchone()
            if row is None:
                return {}, False
            current = self._decode(row)
            manual = (
                current.get("receipt", {}).get("manual_resolution", {})
                if isinstance(current.get("receipt"), dict)
                else {}
            )
            if (
                current.get("status") == target_status
                and isinstance(manual, dict)
                and manual.get("decision") == decision
            ):
                return current, False
            if current.get("status") != "uncertain":
                return current, False
            receipt = dict(current.get("receipt", {}))
            receipt["manual_resolution"] = {
                "decision": decision,
                "recorded_at": now,
            }
            cursor = conn.execute(
                """
                UPDATE agent_external_dispatches
                SET status = ?, receipt_json = ?, error = '', completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND session_id = ? AND status = 'uncertain'
                """,
                (
                    target_status,
                    self._encode(receipt),
                    now,
                    now,
                    dispatch_id,
                    session_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE id = ?",
                (dispatch_id,),
            ).fetchone()
        return (
            self._decode(refreshed) if refreshed is not None else {},
            bool(cursor.rowcount),
        )

    # 作用：用户明确要求重试时，原子重开对应确认操作的领取状态。
    # 参数 dispatch_id：外发账本记录的公开唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 ttl_seconds：显式重试确认单重新开放的有效秒数。
    def prepare_retry_operation(
        self,
        dispatch_id: str,
        *,
        session_id: str,
        ttl_seconds: int = 30 * 60,
    ) -> tuple[dict[str, Any], str, bool]:
        """Atomically make the original operation claimable for an explicit retry."""
        now = time.time()
        expires_at = now + max(60, min(int(ttl_seconds), 24 * 60 * 60))
        with self.backend.connect(immediate=True) as conn:
            dispatch_row = conn.execute(
                """
                SELECT * FROM agent_external_dispatches
                WHERE id = ? AND session_id = ?
                """,
                (dispatch_id, session_id),
            ).fetchone()
            if dispatch_row is None:
                return {}, "", False
            operation_row = conn.execute(
                """
                SELECT status FROM agent_operations
                WHERE id = ? AND session_id = ? AND kind = 'capability'
                """,
                (str(dispatch_row["operation_id"]), session_id),
            ).fetchone()
            operation_status = str(operation_row["status"]) if operation_row else ""
            if str(dispatch_row["status"]) != "uncertain" or operation_status not in {
                "interrupted",
                "pending",
            }:
                return self._decode(dispatch_row), operation_status, False
            conn.execute(
                """
                UPDATE agent_operations
                SET status = 'pending', result_json = '', error = '',
                    expires_at = ?, updated_at = ?
                WHERE id = ? AND session_id = ?
                  AND status IN ('interrupted', 'pending')
                """,
                (expires_at, now, str(dispatch_row["operation_id"]), session_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE id = ?",
                (dispatch_id,),
            ).fetchone()
        return (
            self._decode(refreshed) if refreshed is not None else {},
            "pending",
            True,
        )

    # 作用：原子关闭未知外发及其原始确认单，防止两份状态出现分叉。
    # 参数 dispatch_id：外发账本记录的公开唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 decision：用户对未知外发所作的重试、确认成功或放弃决定。
    # 参数 operation_result：人工处理未知外发后写回原确认单的结果。
    def resolve_uncertain_operation(
        self,
        dispatch_id: str,
        *,
        session_id: str,
        decision: str,
        operation_result: dict[str, Any],
    ) -> tuple[dict[str, Any], str, bool]:
        """Atomically close an uncertain dispatch and its original operation."""
        target_dispatch_status = {
            "confirm_succeeded": "completed",
            "abandon": "abandoned",
        }.get(decision, "")
        target_operation_status = {
            "confirm_succeeded": "completed",
            "abandon": "cancelled",
        }.get(decision, "")
        if not target_dispatch_status:
            raise ValueError("不支持的外发处理决定。")
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            dispatch_row = conn.execute(
                """
                SELECT * FROM agent_external_dispatches
                WHERE id = ? AND session_id = ?
                """,
                (dispatch_id, session_id),
            ).fetchone()
            if dispatch_row is None:
                return {}, "", False
            current = self._decode(dispatch_row)
            operation_row = conn.execute(
                """
                SELECT status FROM agent_operations
                WHERE id = ? AND session_id = ? AND kind = 'capability'
                """,
                (str(dispatch_row["operation_id"]), session_id),
            ).fetchone()
            operation_status = str(operation_row["status"]) if operation_row else ""
            manual = (
                current.get("receipt", {}).get("manual_resolution", {})
                if isinstance(current.get("receipt"), dict)
                else {}
            )
            if (
                current.get("status") == target_dispatch_status
                and operation_status == target_operation_status
                and isinstance(manual, dict)
                and manual.get("decision") == decision
            ):
                return current, operation_status, True
            if current.get("status") != "uncertain" or operation_status not in {
                "interrupted",
                "pending",
            }:
                return current, operation_status, False
            receipt = dict(current.get("receipt", {}))
            receipt["manual_resolution"] = {
                "decision": decision,
                "recorded_at": now,
            }
            dispatch_cursor = conn.execute(
                """
                UPDATE agent_external_dispatches
                SET status = ?, receipt_json = ?, error = '', completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND session_id = ? AND status = 'uncertain'
                """,
                (
                    target_dispatch_status,
                    self._encode(receipt),
                    now,
                    now,
                    dispatch_id,
                    session_id,
                ),
            )
            operation_cursor = conn.execute(
                """
                UPDATE agent_operations
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE id = ? AND session_id = ?
                  AND status IN ('interrupted', 'pending')
                """,
                (
                    target_operation_status,
                    json.dumps(operation_result, ensure_ascii=False),
                    "" if decision == "confirm_succeeded" else "用户已放弃这次不确定外发。",
                    now,
                    str(dispatch_row["operation_id"]),
                    session_id,
                ),
            )
            if dispatch_cursor.rowcount != 1 or operation_cursor.rowcount != 1:
                raise RuntimeError("外发记录与确认单未能原子更新。")
            refreshed = conn.execute(
                "SELECT * FROM agent_external_dispatches WHERE id = ?",
                (dispatch_id,),
            ).fetchone()
        return (
            self._decode(refreshed) if refreshed is not None else {},
            target_operation_status,
            True,
        )

    # 作用：重启时把未提交终态的 dispatching 记录改为 uncertain，且不自动重发。
    # 参数：无。
    def recover_interrupted(self) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_external_dispatches
                SET status = 'uncertain',
                    error = '服务在供应商调用终态提交前停止；未自动重发。',
                    updated_at = ?
                WHERE status = 'dispatching'
                """,
                (time.time(),),
            )
        return int(cursor.rowcount or 0)

    # 作用：删除指定会话的外发账本记录并返回数量。
    # 参数 session_id：当前用户会话的唯一标识。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM agent_external_dispatches WHERE session_id = ?",
                (session_id,),
            )
        return int(cursor.rowcount or 0)

    # 作用：按状态汇总外发账本，并报告未知终态与自动重放策略。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        try:
            with self.backend.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM agent_external_dispatches
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
            "stable_provider_idempotency_key": True,
            "automatic_replay": False,
            "backend": self.backend.name,
            "counts": counts,
            "uncertain": counts.get("uncertain", 0),
            "total": sum(counts.values()),
        }

    # 作用：将精简外发回执编码为有大小上限的 JSON。
    # 参数 value：待清洗、规范化或编码的原始值。
    @staticmethod
    # 作用：执行“encode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _encode(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return encoded if len(encoded.encode("utf-8")) <= 20_000 else "{}"

    # 作用：将数据库行解码为带结构化 receipt 的外发记录。
    # 参数 row：需要解码或计算的数据库查询行。
    @staticmethod
    # 作用：执行“decode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try:
            receipt = json.loads(str(item.pop("receipt_json") or "{}"))
        except (json.JSONDecodeError, TypeError):
            receipt = {}
        item["receipt"] = receipt if isinstance(receipt, dict) else {}
        return item
