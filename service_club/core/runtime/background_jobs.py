"""Durable background jobs with leases, retries, cancellation and dead letters."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from service_club.storage.contracts import BackgroundJobRepository
from service_club.storage.relational import (
    RelationalBackend,
    configured_relational_backend,
)

TERMINAL_JOB_STATUSES = {"completed", "cancelled", "dead_letter"}
ACTIVE_JOB_STATUSES = {"queued", "running", "retry_wait"}


# 作用：表示后台作业参数、状态转换或持久化操作无效。
# 参数：无。
class BackgroundJobError(RuntimeError):
    pass


# 作用：表示某类作业或单会话活跃作业已达到容量上限。
# 参数：无。
class BackgroundJobCapacityError(BackgroundJobError):
    # 作用：保存触发限制的范围和阈值，供上层返回过载原因。
    # 参数 message：异常向调用方说明的可读错误信息。
    # 参数 scope：触发容量限制的全局类型或单会话范围。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def __init__(self, message: str, *, scope: str, limit: int) -> None:
        super().__init__(message)
        self.scope = scope
        self.limit = limit


# 作用：实现带去重、租约、重试、取消和死信状态的持久工作队列。
# 参数：无。
class BackgroundJobStore:
    """Durable leased work queue shared by web and recovery workers."""

    # 作用：绑定关系型存储并初始化后台作业账本。
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
        self.init()

    # 作用：创建或迁移作业表，并建立去重、领取和会话查询索引。
    # 参数：无。
    def init(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    related_task_id TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{{}}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    available_at {self.backend.float_type} NOT NULL,
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until {self.backend.float_type},
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '{{}}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    completed_at {self.backend.float_type}
                )
                """
            )
            for column in ("task_id", "related_task_id"):
                conn.execute(
                    f"""
                    ALTER TABLE background_jobs
                    ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''
                    """
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_background_jobs_dedupe
                ON background_jobs(kind, dedupe_key)
                WHERE dedupe_key != ''
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_background_jobs_claim
                ON background_jobs(status, available_at, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_background_jobs_session
                ON background_jobs(session_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_background_jobs_tasks
                ON background_jobs(task_id, related_task_id, created_at DESC)
                """
            )

    # 作用：将数据库行解码为包含结构化 payload、result 和布尔取消标记的作业。
    # 参数 row：需要解码或计算的数据库查询行。
    @staticmethod
    # 作用：执行“decode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _decode(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target in (("payload_json", "payload"), ("result_json", "result")):
            try:
                decoded = json.loads(str(item.pop(source, "{}")))
            except json.JSONDecodeError:
                decoded = {}
            item[target] = decoded if isinstance(decoded, dict) else {}
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        return item

    # 作用：在去重与容量锁保护下原子创建排队作业，重复键直接复用原记录。
    # 参数 kind：附件分类或后台作业类型。
    # 参数 payload：请求、作业或事件携带的结构化载荷。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 related_task_id：与当前作业间接关联的另一个 Agent 任务 ID。
    # 参数 dedupe_key：保证同类作业或步骤幂等的去重键。
    # 参数 max_attempts：一次网络操作或后台作业允许的最大尝试次数。
    # 参数 available_at：作业最早允许被工作线程领取的时间戳。
    # 参数 max_active_kind：同一作业类型允许的最大活跃数量。
    # 参数 max_active_session：同一会话允许的最大活跃作业数量。
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        session_id: str = "",
        task_id: str = "",
        related_task_id: str = "",
        dedupe_key: str = "",
        max_attempts: int = 5,
        available_at: float | None = None,
        max_active_kind: int | None = None,
        max_active_session: int | None = None,
    ) -> dict[str, Any]:
        kind = kind.strip()
        if not kind or len(kind) > 100:
            raise BackgroundJobError("后台作业类型无效。")
        if (
            len(session_id) > 160
            or len(task_id) > 80
            or len(related_task_id) > 80
            or len(dedupe_key) > 512
        ):
            raise BackgroundJobError("后台作业标识过长。")
        now = time.time()
        job_id = uuid.uuid4().hex
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > 1_000_000:
            raise BackgroundJobError("后台作业参数超过 1MB。")
        with self.backend.connect(immediate=True) as conn:
            if dedupe_key:
                self.backend.lock_scope(
                    conn,
                    f"background-job:dedupe:{kind}:{dedupe_key}",
                )
            if max_active_kind is not None:
                self.backend.lock_scope(conn, f"background-job:kind:{kind}")
            if max_active_session is not None and session_id:
                self.backend.lock_scope(
                    conn,
                    f"background-job:session:{kind}:{session_id}",
                )
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM background_jobs WHERE kind = ? AND dedupe_key = ?",
                    (kind, dedupe_key),
                ).fetchone()
                if existing is not None:
                    decoded = self._decode(existing)
                    assert decoded is not None
                    decoded["deduplicated"] = True
                    return decoded
            if max_active_kind is not None:
                kind_limit = max(1, int(max_active_kind))
                active_kind = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS count FROM background_jobs
                        WHERE kind = ? AND status IN ('queued', 'running', 'retry_wait')
                        """,
                        (kind,),
                    ).fetchone()["count"]
                )
                if active_kind >= kind_limit:
                    raise BackgroundJobCapacityError(
                        f"后台作业类型 {kind} 已达到 {kind_limit} 条容量上限。",
                        scope="kind",
                        limit=kind_limit,
                    )
            if max_active_session is not None and session_id:
                session_limit = max(1, int(max_active_session))
                active_session = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS count FROM background_jobs
                        WHERE kind = ? AND session_id = ?
                          AND status IN ('queued', 'running', 'retry_wait')
                        """,
                        (kind, session_id),
                    ).fetchone()["count"]
                )
                if active_session >= session_limit:
                    raise BackgroundJobCapacityError(
                        f"当前会话已有 {session_limit} 条 Agent 委托等待或执行。",
                        scope="session",
                        limit=session_limit,
                    )
            conn.execute(
                """
                INSERT INTO background_jobs(
                    id, kind, session_id, task_id, related_task_id,
                    dedupe_key, payload_json, status,
                    attempts, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    session_id,
                    task_id,
                    related_task_id,
                    dedupe_key,
                    payload_json,
                    max(1, min(int(max_attempts), 20)),
                    float(available_at if available_at is not None else now),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM background_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        decoded = self._decode(row)
        assert decoded is not None
        decoded["deduplicated"] = False
        return decoded

    # 作用：按作业 ID 查询完整持久化状态。
    # 参数 job_id：后台作业的唯一标识。
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM background_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._decode(row)

    # 作用：按作业类型和去重键查询已有任务。
    # 参数 kind：附件分类或后台作业类型。
    # 参数 dedupe_key：保证同类作业或步骤幂等的去重键。
    def get_by_dedupe(self, kind: str, dedupe_key: str) -> dict[str, Any] | None:
        if not kind or not dedupe_key:
            return None
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM background_jobs WHERE kind = ? AND dedupe_key = ?",
                (kind, dedupe_key),
            ).fetchone()
        return self._decode(row)

    # 作用：服务重启时立即过期旧进程持有的运行中租约，使其进入恢复流程。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    def expire_orphaned_running(self, *, now: float | None = None) -> int:
        """Make leases owned by a previous service process immediately recoverable."""
        current = float(now if now is not None else time.time())
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE background_jobs
                SET lease_until = ?, updated_at = ?,
                    error = CASE WHEN error = ''
                                 THEN 'service restarted; recovering worker lease'
                                 ELSE error END
                WHERE status = 'running'
                """,
                (current, current),
            )
        return int(cursor.rowcount or 0)

    # 作用：回收过期租约后原子领取一个到期作业，可选择同会话严格串行。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    # 参数 lease_seconds：工作线程持有作业执行权的租约时长。
    # 参数 kinds：本次允许领取或统计的作业类型集合。
    # 参数 serialize_by_session：是否要求同类型作业在同一会话内严格串行。
    def claim(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = 60,
        kinds: set[str] | None = None,
        serialize_by_session: bool = False,
    ) -> dict[str, Any] | None:
        current = float(now if now is not None else time.time())
        lease_until = current + max(10.0, min(float(lease_seconds), 600.0))
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = CASE WHEN cancel_requested = 1 THEN 'cancelled'
                                  ELSE 'retry_wait' END,
                    lease_token = '', lease_until = NULL, available_at = ?,
                    error = CASE WHEN cancel_requested = 1 THEN error
                                 ELSE 'worker lease expired; retrying' END,
                    completed_at = CASE WHEN cancel_requested = 1 THEN ?
                                        ELSE completed_at END,
                    updated_at = ?
                WHERE status = 'running' AND lease_until <= ?
                """,
                (current, current, current, current),
            )
            parameters: list[Any] = [current]
            kind_sql = ""
            if kinds:
                normalized = sorted(item for item in kinds if item)
                if not normalized:
                    return None
                kind_sql = f" AND candidate.kind IN ({','.join('?' for _ in normalized)})"
                parameters.extend(normalized)
            session_sql = ""
            if serialize_by_session:
                session_sql = """
                  AND (
                    candidate.session_id = '' OR NOT EXISTS (
                      SELECT 1 FROM background_jobs AS predecessor
                      WHERE predecessor.kind = candidate.kind
                        AND predecessor.session_id = candidate.session_id
                        AND predecessor.id != candidate.id
                        AND predecessor.status IN ('queued', 'running', 'retry_wait')
                        AND (
                          predecessor.status = 'running'
                          OR predecessor.created_at < candidate.created_at
                          OR (
                            predecessor.created_at = candidate.created_at
                            AND predecessor.id < candidate.id
                          )
                        )
                    )
                  )
                """
            row = conn.execute(
                f"""
                SELECT candidate.id FROM background_jobs AS candidate
                WHERE candidate.status IN ('queued', 'retry_wait')
                  AND candidate.cancel_requested = 0
                  AND candidate.available_at <= ?{kind_sql}{session_sql}
                ORDER BY candidate.available_at, candidate.created_at, candidate.id LIMIT 1
                """
                + self.backend.for_update(skip_locked=True),
                parameters,
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            cursor = conn.execute(
                """
                UPDATE background_jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_token = ?, lease_until = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'retry_wait')
                  AND cancel_requested = 0
                """,
                (token, lease_until, current, str(row["id"])),
            )
            if not cursor.rowcount:
                return None
            claimed = conn.execute(
                "SELECT * FROM background_jobs WHERE id = ?", (str(row["id"]),)
            ).fetchone()
        return self._decode(claimed)

    # 作用：使用当前租约令牌续期运行中作业，令牌失效或取消时拒绝续租。
    # 参数 job_id：后台作业的唯一标识。
    # 参数 lease_token：证明当前工作线程拥有作业租约的随机令牌。
    # 参数 lease_seconds：工作线程持有作业执行权的租约时长。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    def renew_lease(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: float = 60,
        now: float | None = None,
    ) -> bool:
        current = float(now if now is not None else time.time())
        lease_until = current + max(10.0, min(float(lease_seconds), 600.0))
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE background_jobs
                SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                  AND cancel_requested = 0
                """,
                (lease_until, current, job_id, lease_token),
            )
        return bool(cursor.rowcount)

    # 作用：仅由合法租约持有者提交成功结果并关闭作业。
    # 参数 job_id：后台作业的唯一标识。
    # 参数 lease_token：证明当前工作线程拥有作业租约的随机令牌。
    # 参数 result：作业成功完成后要持久化的结构化结果。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    def complete(
        self,
        job_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        current = float(now if now is not None else time.time())
        result_json = json.dumps(result or {}, ensure_ascii=False, separators=(",", ":"))
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE background_jobs
                SET status = 'completed', result_json = ?, error = '',
                    lease_token = '', lease_until = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                  AND cancel_requested = 0
                """,
                (result_json, current, current, job_id, lease_token),
            )
        return self.get(job_id) if cursor.rowcount else None

    # 作用：记录执行失败，并按尝试次数转入退避重试、取消或死信状态。
    # 参数 job_id：后台作业的唯一标识。
    # 参数 lease_token：证明当前工作线程拥有作业租约的随机令牌。
    # 参数 error：需要持久化或返回的错误说明。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    # 参数 retry_delay：本次作业失败后可选的自定义重试延迟。
    def fail(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        now: float | None = None,
        retry_delay: float | None = None,
    ) -> dict[str, Any] | None:
        current = float(now if now is not None else time.time())
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT attempts, max_attempts, cancel_requested
                FROM background_jobs
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """
                + self.backend.for_update(),
                (job_id, lease_token),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"])
            cancelled = bool(row["cancel_requested"])
            dead = attempts >= int(row["max_attempts"])
            status = "cancelled" if cancelled else "dead_letter" if dead else "retry_wait"
            delay = (
                max(0.0, float(retry_delay))
                if retry_delay is not None
                else min(300.0, float(2 ** max(0, attempts - 1)))
            )
            completed_at = current if status in TERMINAL_JOB_STATUSES else None
            conn.execute(
                """
                UPDATE background_jobs
                SET status = ?, available_at = ?, lease_token = '', lease_until = NULL,
                    error = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (
                    status,
                    current + delay,
                    str(error)[:2000],
                    completed_at,
                    current,
                    job_id,
                    lease_token,
                ),
            )
        return self.get(job_id)

    # 作用：取消指定范围内的非终态作业并撤销其租约。
    # 参数 job_id：后台作业的唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    def cancel(self, job_id: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        current = time.time()
        with self.backend.connect(immediate=True) as conn:
            parameters: list[Any] = [current, current, job_id]
            scope = ""
            if session_id is not None:
                scope = " AND session_id = ?"
                parameters.append(session_id)
            conn.execute(
                f"""
                UPDATE background_jobs
                SET status = 'cancelled', cancel_requested = 1,
                    lease_token = '', lease_until = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ?{scope} AND status NOT IN ('completed', 'cancelled', 'dead_letter')
                """,
                parameters,
            )
        return self.get(job_id)

    # 作用：在显式操作下清空死信错误和尝试次数，将作业重新排队。
    # 参数 job_id：后台作业的唯一标识。
    def retry_dead_letter(self, job_id: str) -> dict[str, Any] | None:
        current = time.time()
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued', attempts = 0, available_at = ?,
                    cancel_requested = 0, error = '', completed_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'dead_letter'
                """,
                (current, current, job_id),
            )
        return self.get(job_id)

    # 作用：按会话和状态筛选近期后台作业。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def list(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        parameters: list[Any] = []
        if session_id is not None:
            where.append("session_id = ?")
            parameters.append(session_id)
        if status is not None:
            where.append("status = ?")
            parameters.append(status)
        sql_where = f" WHERE {' AND '.join(where)}" if where else ""
        parameters.append(max(1, min(int(limit), 200)))
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM background_jobs{sql_where} ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [item for row in rows if (item := self._decode(row)) is not None]

    # 作用：删除指定会话的全部后台作业并返回数量。
    # 参数 session_id：当前用户会话的唯一标识。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM background_jobs WHERE session_id = ?", (session_id,)
            )
        return int(cursor.rowcount)

    # 作用：级联取消直接或间接关联某个 Agent 任务的所有活跃作业。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def cancel_by_task(self, task_id: str) -> list[dict[str, Any]]:
        if not task_id:
            return []
        current = time.time()
        with self.backend.connect(immediate=True) as conn:
            rows = conn.execute(
                """
                SELECT id FROM background_jobs
                WHERE (task_id = ? OR related_task_id = ?)
                  AND status NOT IN ('completed', 'cancelled', 'dead_letter')
                """,
                (task_id, task_id),
            ).fetchall()
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'cancelled', cancel_requested = 1,
                    lease_token = '', lease_until = NULL,
                    completed_at = ?, updated_at = ?
                WHERE (task_id = ? OR related_task_id = ?)
                  AND status NOT IN ('completed', 'cancelled', 'dead_letter')
                """,
                (current, current, task_id, task_id),
            )
        return [
            item
            for row in rows
            if (item := self.get(str(row["id"]))) is not None
        ]

    # 作用：统计指定类型或会话下排队、执行和等待重试的作业数量。
    # 参数 kind：附件分类或后台作业类型。
    # 参数 session_id：当前用户会话的唯一标识。
    def active_count(self, *, kind: str = "", session_id: str = "") -> int:
        where = ["status IN ('queued', 'running', 'retry_wait')"]
        parameters: list[Any] = []
        if kind:
            where.append("kind = ?")
            parameters.append(kind)
        if session_id:
            where.append("session_id = ?")
            parameters.append(session_id)
        with self.backend.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM background_jobs WHERE {' AND '.join(where)}",
                parameters,
            ).fetchone()
        return int(row["count"]) if row else 0

    # 作用：根据可用时间、创建时间和 ID 计算待处理作业的稳定队列位置。
    # 参数 job_id：后台作业的唯一标识。
    def queue_position(self, job_id: str) -> int:
        with self.backend.connect() as conn:
            job = conn.execute(
                "SELECT * FROM background_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None or str(job["status"]) not in {"queued", "retry_wait"}:
                return 0
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM background_jobs
                WHERE kind = ? AND status IN ('queued', 'retry_wait')
                  AND cancel_requested = 0
                  AND (
                    available_at < ?
                    OR (available_at = ? AND created_at < ?)
                    OR (available_at = ? AND created_at = ? AND id < ?)
                  )
                """,
                (
                    str(job["kind"]),
                    float(job["available_at"]),
                    float(job["available_at"]),
                    float(job["created_at"]),
                    float(job["available_at"]),
                    float(job["created_at"]),
                    str(job["id"]),
                ),
            ).fetchone()
        return int(row["count"]) + 1 if row else 1

    # 作用：汇总目标作业类型的状态分布、待处理量和死信数量。
    # 参数 kinds：本次允许领取或统计的作业类型集合。
    def status(self, *, kinds: set[str] | None = None) -> dict[str, Any]:
        where = ""
        parameters: list[Any] = []
        if kinds:
            normalized = sorted(item for item in kinds if item)
            if normalized:
                where = f" WHERE kind IN ({','.join('?' for _ in normalized)})"
                parameters.extend(normalized)
        with self.backend.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM background_jobs{where} GROUP BY status
                """,
                parameters,
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "ok": True,
            "counts": counts,
            "pending": sum(counts.get(name, 0) for name in ("queued", "running", "retry_wait")),
            "dead_letters": counts.get("dead_letter", 0),
            "total": sum(counts.values()),
            "backend": self.backend.name,
        }


JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]
JobProducer = Callable[[], Any]
DeadLetterHandler = Callable[[dict[str, Any]], Any]


# 作用：管理持久队列工作线程、租约心跳和作业处理器生命周期。
# 参数：无。
class BackgroundJobWorker:
    """Lifecycle-owned worker pool backed by a durable relational repository."""

    # 作用：配置处理器、生产器、并发度、会话串行策略和租约时长。
    # 参数 store：实现租约队列协议的持久作业仓库。
    # 参数 handlers：作业类型到实际执行函数的映射。
    # 参数 producers：队列为空时可主动生成新作业的回调集合。
    # 参数 dead_letter_handlers：按作业类型注册的死信收尾回调。
    # 参数 poll_seconds：工作线程在队列空闲时的轮询间隔。
    # 参数 lease_seconds：工作线程持有作业执行权的租约时长。
    # 参数 concurrency：后台工作线程的目标并发数。
    # 参数 serialize_by_session：是否要求同类型作业在同一会话内严格串行。
    # 参数 name：环境配置项或后台工作线程的名称。
    def __init__(
        self,
        store: BackgroundJobRepository,
        handlers: dict[str, JobHandler],
        *,
        producers: tuple[JobProducer, ...] = (),
        dead_letter_handlers: dict[str, DeadLetterHandler] | None = None,
        poll_seconds: float = 1.0,
        lease_seconds: float = 60.0,
        concurrency: int = 1,
        serialize_by_session: bool = False,
        name: str = "yukino-background-worker",
    ) -> None:
        self.store = store
        self.handlers = handlers
        self.producers = producers
        self.dead_letter_handlers = dead_letter_handlers or {}
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.lease_seconds = max(10.0, float(lease_seconds))
        self.concurrency = max(1, min(int(concurrency), 32))
        self.serialize_by_session = bool(serialize_by_session)
        self.name = name
        self._stop = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._active_jobs: dict[int, dict[str, Any]] = {}
        self._started = False
        self._last_error = ""
        self._lock = threading.RLock()

    # 作用：在持锁状态下补齐目标并发度对应的存活工作线程。
    # 参数：无。
    def _ensure_threads_locked(self) -> None:
        for slot in range(self.concurrency):
            existing = self._threads.get(slot)
            if existing is not None and existing.is_alive():
                continue
            thread = threading.Thread(
                target=self._run,
                args=(slot,),
                name=f"{self.name}-{slot + 1}",
                daemon=True,
            )
            self._threads[slot] = thread
            thread.start()

    # 作用：启动或补齐工作线程；已经完整运行时返回未发生变化。
    # 参数：无。
    def start(self) -> bool:
        with self._lock:
            live = [
                thread
                for slot, thread in self._threads.items()
                if slot < self.concurrency and thread.is_alive()
            ]
            if self._started and len(live) >= self.concurrency:
                return False
            self._stop.clear()
            self._started = True
            self._ensure_threads_locked()
            return True

    # 作用：动态调整目标并发度，并在运行中即时扩容工作线程。
    # 参数 concurrency：后台工作线程的目标并发数。
    def resize(self, concurrency: int) -> int:
        desired = max(1, min(int(concurrency), 32))
        with self._lock:
            self.concurrency = desired
            if self._started and not self._stop.is_set():
                self._ensure_threads_locked()
        return desired

    # 作用：通知所有线程停止并在截止时间内等待其安全退出。
    # 参数 timeout：停止工作线程时允许等待的最长秒数。
    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            threads = list(self._threads.values())
            if not self._started and not any(thread.is_alive() for thread in threads):
                return False
            self._started = False
            self._stop.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            stopped = not any(thread.is_alive() for thread in self._threads.values())
            if stopped:
                self._threads.clear()
                self._active_jobs.clear()
        return stopped

    # 作用：单个工作槽持续领取作业，空闲或异常时按轮询间隔等待。
    # 参数 slot：工作线程在池中的槽位编号。
    def _run(self, slot: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self._started or slot >= self.concurrency:
                    break
            try:
                worked = self.pump_once(slot=slot)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)[:500]
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)

    # 作用：尝试领取并处理一个作业；队列为空时先运行生产器补充任务。
    # 参数 slot：工作线程在池中的槽位编号。
    def pump_once(self, *, slot: int = 0) -> bool:
        job = self.store.claim(
            lease_seconds=self.lease_seconds,
            kinds=set(self.handlers),
            serialize_by_session=self.serialize_by_session,
        )
        produced = False
        if job is None:
            for producer in self.producers:
                try:
                    produced = bool(producer()) or produced
                except Exception:
                    continue
            job = self.store.claim(
                lease_seconds=self.lease_seconds,
                kinds=set(self.handlers),
                serialize_by_session=self.serialize_by_session,
            )
        if job is None:
            return produced
        return self._execute(job, slot=slot)

    # 作用：在租约心跳保护下调用处理器，提交成功、重试或死信回调。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    # 参数 slot：工作线程在池中的槽位编号。
    def _execute(self, job: dict[str, Any], *, slot: int) -> bool:
        handler = self.handlers.get(str(job["kind"]))
        token = str(job.get("lease_token") or "")
        job_id = str(job["id"])
        heartbeat_stop = threading.Event()

        # 作用：后台定期续租当前作业，防止长任务被其他工作进程重复领取。
        # 参数：无。
        def renew_lease() -> None:
            interval = max(1.0, min(30.0, self.lease_seconds / 3))
            while not heartbeat_stop.wait(interval):
                if not self.store.renew_lease(
                    job_id,
                    token,
                    lease_seconds=self.lease_seconds,
                ):
                    break

        heartbeat = threading.Thread(
            target=renew_lease,
            name=f"{self.name}-lease-{slot + 1}",
            daemon=True,
        )
        with self._lock:
            self._active_jobs[slot] = {
                "id": job_id,
                "kind": str(job.get("kind") or ""),
                "session_id": str(job.get("session_id") or ""),
                "started_at": time.time(),
            }
        heartbeat.start()
        failed: dict[str, Any] | None = None
        try:
            if handler is None:
                failed = self.store.fail(job_id, token, "no handler registered")
            else:
                try:
                    result = handler(job) or {}
                except Exception as exc:
                    failed = self.store.fail(job_id, token, str(exc))
                else:
                    self.store.complete(job_id, token, result)
                    return True
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)
            with self._lock:
                self._active_jobs.pop(slot, None)
        if failed and failed.get("status") == "dead_letter":
            callback = self.dead_letter_handlers.get(str(failed.get("kind") or ""))
            if callback is not None:
                try:
                    callback(failed)
                except Exception:
                    pass
        return True

    # 作用：汇总队列、线程健康、活跃作业、会话串行和租约心跳状态。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        with self._lock:
            live_threads = [
                thread
                for slot, thread in self._threads.items()
                if slot < self.concurrency and thread.is_alive()
            ]
            active_jobs = list(self._active_jobs.values())
            started = self._started
            last_error = self._last_error
        return {
            **self.store.status(kinds=set(self.handlers)),
            "worker_running": bool(started and live_threads),
            "worker_healthy": bool(started and len(live_threads) == self.concurrency),
            "configured_workers": self.concurrency,
            "live_workers": len(live_threads),
            "active_workers": len(active_jobs),
            "active_jobs": active_jobs,
            "session_serialization": self.serialize_by_session,
            "lease_heartbeat": True,
            "last_error": last_error,
            "registered_handlers": sorted(self.handlers),
        }
