from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from service_club.storage.relational import (
    RelationalBackend,
    SQLiteRelationalBackend,
)


# 作用：表示权限规则或临时授权参数无效。
# 参数：无。
class CapabilityPolicyError(ValueError):
    pass


# 作用：表示能力调用被持久权限策略拒绝。
# 参数：无。
class CapabilityPermissionDenied(PermissionError):
    pass


# 作用：在关系库中管理能力拒绝规则和会话级临时授权。
# 参数：无。
class CapabilityPolicyStore:
    """Persistent deny rules and atomic, session-scoped temporary grants."""

    # 作用：绑定关系存储后端，并初始化权限策略表结构。
    # 参数 path：目标文件、数据库或状态存储路径。
    # 参数 backend：所使用的存储、执行后端或后端标识。
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        if backend is None:
            if path is None:
                raise ValueError("SQLite 权限仓库需要数据库路径。")
            backend = SQLiteRelationalBackend(path)
        self.backend = backend
        self.path = Path(path) if path is not None else None
        self._ensure_schema()

    # 作用：创建权限拒绝规则与临时授权所需的数据表和索引。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS capability_policy_denials (
                    capability TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    PRIMARY KEY(capability, action)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS capability_temporary_grants (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    remaining_uses INTEGER NOT NULL,
                    expires_at {self.backend.float_type} NOT NULL,
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    revoked_at {self.backend.float_type}
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_capability_grants_session
                ON capability_temporary_grants(session_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_capability_grants_authorize
                ON capability_temporary_grants(
                    session_id, capability, action, status, expires_at, created_at
                )
                """
            )

    # 作用：设置或移除单个能力操作的全局拒绝规则。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 denied：是否启用这条全局拒绝规则。
    def set_denied(self, capability: str, action: str, *, denied: bool) -> None:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            if denied:
                conn.execute(
                    """
                    INSERT INTO capability_policy_denials(
                        capability, action, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(capability, action) DO UPDATE
                    SET updated_at = excluded.updated_at
                    """,
                    (capability, action, now, now),
                )
            else:
                conn.execute(
                    """
                    DELETE FROM capability_policy_denials
                    WHERE capability = ? AND action = ?
                    """,
                    (capability, action),
                )

    # 作用：在同一事务中批量替换指定权限规则。
    # 参数 rules：本次要批量应用的能力权限规则。
    def set_many(self, rules: list[dict[str, Any]]) -> None:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            for rule in rules:
                capability = str(rule["capability"])
                action = str(rule["action"])
                denied = not bool(rule["enabled"])
                if denied:
                    conn.execute(
                        """
                        INSERT INTO capability_policy_denials(
                            capability, action, created_at, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(capability, action) DO UPDATE
                        SET updated_at = excluded.updated_at
                        """,
                        (capability, action, now, now),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM capability_policy_denials
                        WHERE capability = ? AND action = ?
                        """,
                        (capability, action),
                    )

    # 作用：读取当前启用的全部拒绝规则。
    # 参数：无。
    def denied_rules(self) -> set[tuple[str, str]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                "SELECT capability, action FROM capability_policy_denials"
            ).fetchall()
        return {
            (str(row["capability"]), str(row["action"]))
            for row in rows
        }

    # 作用：判断能力或具体操作是否命中全局拒绝规则。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    def is_denied(self, capability: str, action: str) -> bool:
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM capability_policy_denials
                WHERE capability = ? AND action IN ('*', ?)
                LIMIT 1
                """,
                (capability, action),
            ).fetchone()
        return row is not None

    # 作用：创建带有效期和使用次数限制的会话临时授权。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 ttl_seconds：授权或待确认操作保持有效的秒数。
    # 参数 uses：临时授权允许被消费的最大次数。
    def create_grant(
        self,
        *,
        session_id: str,
        capability: str,
        action: str,
        ttl_seconds: int,
        uses: int,
    ) -> dict[str, Any]:
        if not session_id:
            raise CapabilityPolicyError("临时授权必须绑定当前会话。")
        bounded_ttl = max(60, min(int(ttl_seconds), 24 * 60 * 60))
        bounded_uses = max(1, min(int(uses), 50))
        now = time.time()
        grant_id = f"grant-{uuid.uuid4().hex[:20]}"
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"capability-grants:{session_id}")
            conn.execute(
                """
                UPDATE capability_temporary_grants
                SET status = 'expired', updated_at = ?
                WHERE session_id = ? AND status = 'active' AND expires_at <= ?
                """,
                (now, session_id, now),
            )
            active = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM capability_temporary_grants
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchone()["count"]
            )
            if active >= 100:
                raise CapabilityPolicyError("当前会话的有效临时授权已达到 100 条上限。")
            conn.execute(
                """
                INSERT INTO capability_temporary_grants(
                    id, session_id, capability, action, status, remaining_uses,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    session_id,
                    capability,
                    action,
                    bounded_uses,
                    now + bounded_ttl,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                DELETE FROM capability_temporary_grants
                WHERE status != 'active' AND updated_at < ?
                """,
                (now - 30 * 24 * 60 * 60,),
            )
            row = conn.execute(
                "SELECT * FROM capability_temporary_grants WHERE id = ?",
                (grant_id,),
            ).fetchone()
        return self._decode_grant(row)

    # 作用：综合拒绝规则与临时授权判断请求，必要时原子扣减次数。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 consume：授权成功时是否扣减一次临时授权额度。
    def authorize(
        self,
        capability: str,
        action: str,
        *,
        session_id: str = "",
        consume: bool,
    ) -> dict[str, Any]:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            denied = conn.execute(
                """
                SELECT action FROM capability_policy_denials
                WHERE capability = ? AND action IN ('*', ?)
                ORDER BY CASE action WHEN '*' THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (capability, action),
            ).fetchone()
            if denied is None:
                return {
                    "allowed": True,
                    "source": "default_allow",
                    "consumed": False,
                    "grant_id": "",
                }
            conn.execute(
                """
                UPDATE capability_temporary_grants
                SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <= ?
                """,
                (now, now),
            )
            grant = None
            if session_id:
                grant = conn.execute(
                    """
                    SELECT * FROM capability_temporary_grants
                    WHERE session_id = ? AND capability = ? AND action = ?
                      AND status = 'active' AND expires_at > ?
                      AND remaining_uses > 0
                    ORDER BY expires_at ASC, created_at ASC
                    LIMIT 1
                    """
                    + (
                        self.backend.for_update(skip_locked=True)
                        if consume
                        else ""
                    ),
                    (session_id, capability, action, now),
                ).fetchone()
            if grant is None:
                return {
                    "allowed": False,
                    "source": "policy_denied",
                    "denied_scope": str(denied["action"]),
                    "consumed": False,
                    "grant_id": "",
                }
            decoded = self._decode_grant(grant)
            if not consume:
                return {
                    "allowed": True,
                    "source": "temporary_grant",
                    "consumed": False,
                    "grant_id": decoded["id"],
                    "remaining_uses": decoded["remaining_uses"],
                    "expires_at": decoded["expires_at"],
                }
            remaining = int(decoded["remaining_uses"]) - 1
            status = "consumed" if remaining == 0 else "active"
            cursor = conn.execute(
                """
                UPDATE capability_temporary_grants
                SET remaining_uses = ?, status = ?, updated_at = ?
                WHERE id = ? AND status = 'active' AND expires_at > ?
                  AND remaining_uses = ?
                """,
                (
                    remaining,
                    status,
                    now,
                    decoded["id"],
                    now,
                    decoded["remaining_uses"],
                ),
            )
            if cursor.rowcount != 1:
                return {
                    "allowed": False,
                    "source": "policy_denied",
                    "denied_scope": str(denied["action"]),
                    "consumed": False,
                    "grant_id": "",
                }
            return {
                "allowed": True,
                "source": "temporary_grant",
                "consumed": True,
                "grant_id": decoded["id"],
                "remaining_uses": remaining,
                "expires_at": decoded["expires_at"],
            }

    # 作用：列出会话的临时授权及其剩余有效性。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def list_session(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not session_id:
            return []
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE capability_temporary_grants
                SET status = 'expired', updated_at = ?
                WHERE session_id = ? AND status = 'active' AND expires_at <= ?
                """,
                (now, session_id, now),
            )
            rows = conn.execute(
                """
                SELECT * FROM capability_temporary_grants
                WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (session_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._decode_grant(row) for row in rows]

    # 作用：撤销属于指定会话的一条临时授权。
    # 参数 grant_id：临时授权记录的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def revoke(self, grant_id: str, *, session_id: str) -> tuple[dict[str, Any], bool]:
        if not grant_id or not session_id:
            return {}, False
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE capability_temporary_grants
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE id = ? AND session_id = ? AND status = 'active'
                """,
                (now, now, grant_id, session_id),
            )
            row = conn.execute(
                """
                SELECT * FROM capability_temporary_grants
                WHERE id = ? AND session_id = ?
                """,
                (grant_id, session_id),
            ).fetchone()
        return (self._decode_grant(row) if row is not None else {}, bool(cursor.rowcount))

    # 作用：清理会话产生的全部临时授权。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int:
        if not session_id:
            return 0
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM capability_temporary_grants WHERE session_id = ?",
                (session_id,),
            )
        return int(cursor.rowcount or 0)

    # 作用：汇总权限规则和临时授权数量，供运行状态检查。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE capability_temporary_grants
                SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <= ?
                """,
                (now, now),
            )
            denied = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM capability_policy_denials"
                ).fetchone()["count"]
            )
            active = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM capability_temporary_grants
                    WHERE status = 'active'
                    """
                ).fetchone()["count"]
            )
        return {
            "ok": True,
            "persistent": True,
            "default": "allow",
            "denied_rules": denied,
            "active_temporary_grants": active,
            "session_scoped_grants": True,
            "atomic_consumption": True,
            "confirmation_independent": True,
            "workflow_steps_enforced": True,
            "backend": self.backend.name,
        }

    # 作用：把数据库行转换为带过期和剩余次数信息的授权对象。
    # 参数 row：待转换、取键或校验的数据库记录。
    @staticmethod
    # 作用：执行“decode_grant”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _decode_grant(row: Mapping[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        item["remaining_uses"] = int(item.get("remaining_uses", 0))
        return item
