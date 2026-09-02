from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from service_club.storage.relational import (
    RelationalBackend,
    RelationalConnection,
    SQLiteRelationalBackend,
)

_CURRENT_TASK_ID: ContextVar[str] = ContextVar("agi_yukino_agent_task_id", default="")


# 作用：表示任务的时长、模型、费用或工具调用额度已经耗尽。
# 参数：无。
class AgentTaskBudgetExceeded(RuntimeError):
    # 作用：保存超限资源、阈值和当时的预算用量快照。
    # 参数 resource：发生预算超限的资源类型。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    # 参数 used：触发异常时对应资源已经使用的数量。
    # 参数 budget：任务预算策略或预算快照。
    # 参数 usage：任务累计的时长、模型与工具资源用量。
    def __init__(
        self,
        resource: str,
        *,
        limit: float,
        used: float,
        budget: dict[str, Any],
        usage: dict[str, Any],
    ) -> None:
        labels = {
            "wall_time": "总执行时长",
            "model_tokens": "模型 Token",
            "model_cost": "模型费用",
            "model_calls": "模型调用次数",
            "tool_calls": "工具调用次数",
        }
        super().__init__(f"任务已达到{labels.get(resource, resource)}预算。")
        self.resource = resource
        self.limit = limit
        self.used = used
        self.budget = budget
        self.usage = usage

    # 作用：将预算异常转换成便于事件记录和接口返回的结构。
    # 参数：无。
    def as_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "limit": self.limit,
            "used": self.used,
            "budget": dict(self.budget),
            "usage": dict(self.usage),
        }


# 作用：读取当前执行上下文所绑定的 Agent 任务 ID。
# 参数：无。
def current_agent_task_id() -> str:
    return _CURRENT_TASK_ID.get()


# 作用：在当前上下文临时绑定任务 ID，使模型和工具调用可自动归入同一账本。
# 参数 task_id：Agent 持久任务的唯一标识。
@contextmanager
# 作用：执行“agent_task_scope”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 task_id：持久任务的稳定标识。
def agent_task_scope(task_id: str) -> Iterator[None]:
    token = _CURRENT_TASK_ID.set(task_id)
    try:
        yield
    finally:
        _CURRENT_TASK_ID.reset(token)


# 作用：持久化 Agent 任务、步骤、预算、检查点和事件，支撑取消与安全恢复。
# 参数：无。
class AgentTaskStore:
    """Durable task and step ledger for observable, cancellable Agent work."""

    SIDE_EFFECT_ACTIONS = {
        "remember",
        "forget",
        "clear_memory",
        "set_reminder",
        "cancel_reminder",
        "write_file",
        "edit_file",
        "confirm_file_operation",
        "rollback_file_operation",
        "add_task",
        "confirm_capability_operation",
    }

    # 作用：绑定关系型后端并初始化完整任务账本结构。
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
                raise ValueError("SQLite Agent 任务账本需要数据库路径。")
            backend = SQLiteRelationalBackend(db_path)
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self._ensure_schema()

    # 作用：创建或迁移任务、工具步骤和可游标重放的事件表。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL DEFAULT '[]',
                    contract_json TEXT NOT NULL DEFAULT '{{}}',
                    budget_json TEXT NOT NULL DEFAULT '{{}}',
                    usage_json TEXT NOT NULL DEFAULT '{{}}',
                    checkpoint_json TEXT NOT NULL DEFAULT '{{}}',
                    checkpoint_revision INTEGER NOT NULL DEFAULT 0,
                    run_started_at {self.backend.float_type},
                    status TEXT NOT NULL DEFAULT 'created',
                    phase TEXT NOT NULL DEFAULT 'planning',
                    outcome_json TEXT NOT NULL DEFAULT '{{}}',
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL,
                    UNIQUE(session_id, request_id)
                )
                """
            )
            task_additions = {
                "contract_json": "TEXT NOT NULL DEFAULT '{}'",
                "budget_json": "TEXT NOT NULL DEFAULT '{}'",
                "usage_json": "TEXT NOT NULL DEFAULT '{}'",
                "run_started_at": self.backend.float_type,
                "checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
                "checkpoint_revision": "INTEGER NOT NULL DEFAULT 0",
            }
            self._add_missing_columns(conn, "agent_tasks", task_additions)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_task_steps (
                    id {self.backend.identity_type},
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{{}}',
                    output_json TEXT NOT NULL DEFAULT '{{}}',
                    error TEXT NOT NULL DEFAULT '',
                    started_at {self.backend.float_type} NOT NULL,
                    completed_at {self.backend.float_type},
                    updated_at {self.backend.float_type} NOT NULL,
                    UNIQUE(task_id, sequence)
                )
                """
            )
            self._add_missing_columns(
                conn,
                "agent_task_steps",
                {
                    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                    "updated_at": (
                        f"{self.backend.float_type} NOT NULL DEFAULT 0"
                    ),
                },
            )
            conn.execute(
                """
                UPDATE agent_task_steps
                SET updated_at = COALESCE(completed_at, started_at)
                WHERE updated_at <= 0
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_updated
                ON agent_tasks(session_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_updated
                ON agent_tasks(status, updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_task_steps_idempotency
                ON agent_task_steps(task_id, action, idempotency_key, status)
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_task_events (
                    id {self.backend.identity_type},
                    task_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL DEFAULT '',
                    step_id INTEGER,
                    action TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at {self.backend.float_type} NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_task_events_task_cursor
                ON agent_task_events(task_id, id)
                """
            )

    # 作用：兼容 SQLite 与其他关系库，为旧表补齐新增字段。
    # 参数 connection：用于执行表结构迁移的数据库连接。
    # 参数 table：需要检查并迁移的数据表名称。
    # 参数 additions：待补充字段名与数据库类型定义的映射。
    def _add_missing_columns(
        self,
        connection: RelationalConnection,
        table: str,
        additions: dict[str, str],
    ) -> None:
        if self.backend.name == "sqlite":
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, definition in additions.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
            return
        for column, definition in additions.items():
            connection.execute(
                f"""
                ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS {column} {definition}
                """
            )

    # 作用：以会话和请求 ID 幂等创建任务，并写入默认计划与预算快照。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    # 参数 goal：用户委托的目标摘要。
    # 参数 request：创建或恢复任务时保存的请求结构快照。
    def create(
        self,
        *,
        session_id: str,
        request_id: str,
        goal: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        stable_request_id = request_id or f"direct-{uuid.uuid4().hex}"
        now = time.time()
        task_id = f"task-{uuid.uuid4().hex[:16]}"
        plan = ["理解委托", "调用必要工具", "验证执行结果", "整理最终回应"]
        budget = self.default_budget()
        usage = self.empty_usage()
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO agent_tasks(
                    id, session_id, request_id, goal, request_json, plan_json,
                    budget_json, usage_json, status, phase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', 'planning', ?, ?)
                ON CONFLICT(session_id, request_id) DO NOTHING
                """,
                (
                    task_id,
                    session_id,
                    stable_request_id,
                    goal[:4000],
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(budget, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE session_id = ? AND request_id = ?",
                (session_id, stable_request_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Agent 任务创建失败。")
        task = self._decode_task(row, include_steps=True)
        if cursor.rowcount:
            self.append_event(
                str(task["id"]),
                "task_created",
                status=str(task["status"]),
                phase=str(task["phase"]),
                detail="已建立持久任务账本",
            )
        return task

    # 作用：保存确定性执行计划和证据契约，并发布计划就绪事件。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 plan：面向用户展示的确定性执行计划步骤。
    # 参数 contract：本任务必须遵守的动作授权与证据验收契约。
    def set_plan(
        self,
        task_id: str,
        *,
        plan: list[str],
        contract: dict[str, Any],
    ) -> dict[str, Any] | None:
        clean_plan = [str(item).strip()[:300] for item in plan if str(item).strip()][:12]
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET plan_json = ?, contract_json = ?, phase = 'planned', updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(clean_plan, ensure_ascii=False),
                    json.dumps(contract, ensure_ascii=False),
                    time.time(),
                    task_id,
                ),
            )
        if cursor.rowcount:
            self.append_event(
                task_id,
                "plan_ready",
                status="running",
                phase="planned",
                detail=f"已生成 {len(clean_plan)} 个执行计划步骤",
                payload={"plan_count": len(clean_plan)},
            )
        return self.get(task_id)

    # 作用：更新任务状态、阶段和验收结论，同时结算本轮运行时长。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 phase：任务当前所处的可观察执行阶段。
    # 参数 outcome：任务经过证据验收得到的结论。
    # 参数 error：需要持久化或返回的错误说明。
    def mark(
        self,
        task_id: str,
        *,
        status: str,
        phase: str,
        outcome: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        now = time.time()
        cancelled = False
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                "SELECT usage_json, run_started_at, cancel_requested FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if bool(row["cancel_requested"]) and status != "cancelled":
                cancelled = True
                cursor = None
            else:
                usage = self._normalize_usage(self._json(row["usage_json"], {}))
                run_started_at = (
                    float(row["run_started_at"])
                    if row["run_started_at"] is not None
                    else None
                )
                if status == "running" and run_started_at is None:
                    run_started_at = now
                elif status in {
                    "completed",
                    "failed",
                    "degraded",
                    "cancelled",
                    "interrupted",
                    "waiting_confirmation",
                    "waiting_background",
                } and run_started_at is not None:
                    usage["wall_seconds"] = round(
                        float(usage["wall_seconds"]) + max(0.0, now - run_started_at),
                        3,
                    )
                    run_started_at = None
                cursor = conn.execute(
                    """
                    UPDATE agent_tasks SET status = ?, phase = ?, outcome_json = ?,
                        error = ?, usage_json = ?, run_started_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        phase,
                        json.dumps(outcome or {}, ensure_ascii=False),
                        error[:2000],
                        json.dumps(usage, ensure_ascii=False),
                        run_started_at,
                        now,
                        task_id,
                    ),
                )
        if cancelled:
            return self.get(task_id)
        if cursor is not None and cursor.rowcount:
            self.append_event(
                task_id,
                "task_status",
                status=status,
                phase=phase,
                detail=self._phase_detail(status, phase),
            )
        return self.get(task_id)

    # 作用：推进可观察执行阶段，但保留已有验收结果并尊重取消和终态。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 phase：任务当前所处的可观察执行阶段。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 detail：写入任务事件或检查点的简短可读说明。
    def set_phase(
        self,
        task_id: str,
        phase: str,
        *,
        status: str = "running",
        detail: str = "",
    ) -> dict[str, Any] | None:
        """Advance observable progress without discarding the existing outcome."""
        now = time.time()
        run_started_at = now if status == "running" else None
        with self.backend.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?, phase = ?,
                    run_started_at = CASE
                        WHEN ? IS NULL THEN run_started_at
                        ELSE COALESCE(run_started_at, ?)
                    END,
                    updated_at = ?
                WHERE id = ? AND cancel_requested = 0
                  AND status NOT IN ('completed', 'failed', 'degraded', 'cancelled', 'interrupted')
                """,
                (status, phase[:80], run_started_at, run_started_at, now, task_id),
            )
        if cursor.rowcount:
            self.append_event(
                task_id,
                "phase_changed",
                status=status,
                phase=phase,
                detail=detail or self._phase_detail(status, phase),
            )
        return self.get(task_id)

    # 作用：在保留步骤证据和累计用量的前提下，显式重开可恢复终态任务。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    # 参数 request：创建或恢复任务时保存的请求结构快照。
    def resume(
        self,
        task_id: str,
        *,
        session_id: str,
        request_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reopen one terminal task without discarding completed step evidence."""
        stable_request_id = request_id or f"resume-{uuid.uuid4().hex}"
        with self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT budget_json, usage_json FROM agent_tasks
                WHERE id = ? AND session_id = ?
                """
                + self.backend.for_update(),
                (task_id, session_id),
            ).fetchone()
            if row is None:
                return None
            old_budget = self._normalize_budget(self._json(row["budget_json"], {}))
            current_budget = self.default_budget()
            limit_keys = (
                "wall_time_seconds",
                "model_token_limit",
                "model_cost_limit",
                "tool_call_limit",
                "model_call_limit",
                "max_output_tokens",
            )
            merged_budget = dict(old_budget)
            for key in limit_keys:
                merged_budget[key] = max(
                    float(old_budget[key]),
                    float(current_budget[key]),
                )
            for key in (
                "model_token_limit",
                "tool_call_limit",
                "model_call_limit",
                "max_output_tokens",
            ):
                merged_budget[key] = int(merged_budget[key])
            for key in ("input_cost_per_million", "output_cost_per_million"):
                if float(current_budget[key]) > 0:
                    merged_budget[key] = float(current_budget[key])
            merged_budget["currency"] = str(current_budget["currency"])
            usage = self._normalize_usage(self._json(row["usage_json"], {}))
            usage["budget_exceeded"] = ""
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET request_id = ?, request_json = ?, status = 'running',
                    phase = 'resuming', error = '', cancel_requested = 0,
                    budget_json = ?, usage_json = ?, run_started_at = ?, updated_at = ?
                WHERE id = ? AND session_id = ?
                  AND status IN ('failed', 'degraded', 'cancelled', 'interrupted')
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks AS conflicting
                    WHERE conflicting.session_id = ? AND conflicting.request_id = ?
                      AND conflicting.id != ?
                  )
                """,
                (
                    stable_request_id,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(merged_budget, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    time.time(),
                    time.time(),
                    task_id,
                    session_id,
                    session_id,
                    stable_request_id,
                    task_id,
                ),
            )
        if cursor.rowcount:
            checkpoint = self.checkpoint(task_id)
            self.append_event(
                task_id,
                "task_resumed",
                status="running",
                phase="resuming",
                detail=(
                    f"正在从检查点 #{checkpoint['revision']} 恢复未完成委托"
                    if checkpoint
                    else "正在从持久账本恢复未完成委托"
                ),
                payload={
                    "checkpoint_revision": int(checkpoint.get("revision", 0)),
                    "checkpoint_state": str(checkpoint.get("state", "")),
                },
            )
            if checkpoint:
                self.append_event(
                    task_id,
                    "checkpoint_loaded",
                    status="running",
                    phase="resuming",
                    detail=f"已加载安全结构检查点 #{checkpoint['revision']}",
                    payload={
                        "revision": checkpoint["revision"],
                        "state": checkpoint["state"],
                        "loop_index": checkpoint["loop_index"],
                    },
                )
        return self.get(task_id) if cursor.rowcount else None

    # 作用：计算任务实时预算用量、剩余额度和已超限资源。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def budget_status(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            return {}
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT budget_json, usage_json, run_started_at FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return {}
        budget = self._normalize_budget(self._json(row["budget_json"], {}))
        usage = self._normalize_usage(self._json(row["usage_json"], {}))
        wall_seconds = float(usage["wall_seconds"])
        if row["run_started_at"] is not None:
            wall_seconds += max(0.0, time.time() - float(row["run_started_at"]))
        usage["wall_seconds"] = round(wall_seconds, 3)
        used = self._budget_used(usage)
        remaining = {
            "wall_time": max(0.0, float(budget["wall_time_seconds"]) - used["wall_time"]),
            "model_tokens": max(0, int(budget["model_token_limit"]) - int(used["model_tokens"])),
            "model_cost": max(0.0, float(budget["model_cost_limit"]) - used["model_cost"]),
            "model_calls": max(0, int(budget["model_call_limit"]) - int(used["model_calls"])),
            "tool_calls": max(0, int(budget["tool_call_limit"]) - int(used["tool_calls"])),
        }
        return {
            "policy": budget,
            "usage": usage,
            "used": used,
            "remaining": remaining,
            "pricing_active": self._pricing_active(budget),
            "exceeded": str(usage.get("budget_exceeded", "")),
        }

    # 作用：在继续执行前检查所有预算，并持久化后抛出首个超限异常。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def ensure_within_budget(self, task_id: str) -> dict[str, Any]:
        status = self.budget_status(task_id)
        if not status:
            return status
        resource = self._first_exceeded(status)
        if resource:
            self._persist_budget_exceeded(task_id, resource)
            refreshed = self.budget_status(task_id)
            raise self._budget_exception(resource, refreshed)
        return status

    # 作用：原子消耗一次工具调用额度，超限时拒绝执行并记录事件。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    def consume_tool_call(self, task_id: str, action: str = "") -> dict[str, Any]:
        if not task_id:
            return {}
        failure = ""
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                "SELECT budget_json, usage_json, run_started_at FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return {}
            budget = self._normalize_budget(self._json(row["budget_json"], {}))
            usage = self._usage_with_elapsed(row)
            failure = self._first_exceeded_for(budget, usage)
            if not failure and int(usage["tool_calls"]) + 1 > int(budget["tool_call_limit"]):
                failure = "tool_calls"
            if failure:
                usage["budget_exceeded"] = failure
            else:
                usage["tool_calls"] = int(usage["tool_calls"]) + 1
                usage["budget_exceeded"] = ""
            conn.execute(
                """
                UPDATE agent_tasks SET usage_json = ?,
                    run_started_at = CASE WHEN run_started_at IS NULL THEN NULL ELSE ? END,
                    updated_at = ? WHERE id = ?
                """,
                (json.dumps(usage, ensure_ascii=False), now, now, task_id),
            )
        if failure:
            self._append_budget_exceeded(task_id, failure, action=action)
            raise self._budget_exception(failure, self.budget_status(task_id))
        status = self.budget_status(task_id)
        self.append_event(
            task_id,
            "budget_usage",
            status="running",
            phase="executing",
            action=action,
            detail=f"工具调用 {status['used']['tool_calls']}/{status['policy']['tool_call_limit']}",
            payload={"resource": "tool_calls", "used": status["used"]["tool_calls"]},
        )
        return status

    # 作用：在请求模型前原子预留调用次数、Token、费用和最大输出额度。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 estimated_input_tokens：发起模型调用前估算的输入 Token 数。
    # 参数 requested_output_tokens：调用方期望为模型输出预留的 Token 数。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    def reserve_model_call(
        self,
        task_id: str,
        *,
        estimated_input_tokens: int,
        requested_output_tokens: int,
        model: str = "",
    ) -> dict[str, Any]:
        if not task_id:
            return {
                "task_id": "",
                "reserved_tokens": 0,
                "reserved_cost": 0.0,
                "estimated_input_tokens": max(0, int(estimated_input_tokens)),
                "max_output_tokens": max(1, int(requested_output_tokens)),
                "wall_remaining_seconds": 0.0,
            }
        failure = ""
        reservation: dict[str, Any] = {}
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                "SELECT budget_json, usage_json, run_started_at FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return {}
            budget = self._normalize_budget(self._json(row["budget_json"], {}))
            usage = self._usage_with_elapsed(row)
            failure = self._first_exceeded_for(budget, usage)
            if not failure and int(usage["model_calls"]) + 1 > int(budget["model_call_limit"]):
                failure = "model_calls"
            token_remaining = int(budget["model_token_limit"]) - int(
                self._budget_used(usage)["model_tokens"]
            )
            input_tokens = max(1, int(estimated_input_tokens))
            output_tokens = min(
                max(1, int(requested_output_tokens)),
                int(budget["max_output_tokens"]),
                max(0, token_remaining - input_tokens),
            )
            if not failure and (token_remaining <= input_tokens or output_tokens < 1):
                failure = "model_tokens"
            reserved_tokens = input_tokens + max(0, output_tokens)
            reserved_cost = self._estimated_cost(
                budget,
                input_tokens=input_tokens,
                output_tokens=max(0, output_tokens),
            )
            if (
                not failure
                and self._pricing_active(budget)
                and self._budget_used(usage)["model_cost"] + reserved_cost
                > float(budget["model_cost_limit"]) + 1e-12
            ):
                failure = "model_cost"
            if failure:
                usage["budget_exceeded"] = failure
            else:
                usage["model_calls"] = int(usage["model_calls"]) + 1
                usage["reserved_tokens"] = int(usage["reserved_tokens"]) + reserved_tokens
                usage["reserved_cost"] = round(
                    float(usage["reserved_cost"]) + reserved_cost,
                    8,
                )
                usage["budget_exceeded"] = ""
                reservation = {
                    "task_id": task_id,
                    "model": model,
                    "reserved_tokens": reserved_tokens,
                    "reserved_cost": reserved_cost,
                    "estimated_input_tokens": input_tokens,
                    "max_output_tokens": output_tokens,
                    "wall_remaining_seconds": max(
                        0.0,
                        float(budget["wall_time_seconds"])
                        - float(self._budget_used(usage)["wall_time"]),
                    ),
                }
            conn.execute(
                """
                UPDATE agent_tasks SET usage_json = ?,
                    run_started_at = CASE WHEN run_started_at IS NULL THEN NULL ELSE ? END,
                    updated_at = ? WHERE id = ?
                """,
                (json.dumps(usage, ensure_ascii=False), now, now, task_id),
            )
        if failure:
            self._append_budget_exceeded(task_id, failure, action=model)
            raise self._budget_exception(failure, self.budget_status(task_id))
        self.append_event(
            task_id,
            "budget_reserved",
            status="running",
            phase="model_waiting",
            action=model,
            detail=f"已为模型调用预留 {reservation['reserved_tokens']} Token",
            payload={
                "resource": "model_tokens",
                "reserved_tokens": reservation["reserved_tokens"],
                "max_output_tokens": reservation["max_output_tokens"],
            },
        )
        return reservation

    # 作用：模型调用结束后用实际用量结算预留；终态未知时保守计入不确定用量。
    # 参数 reservation：模型调用前原子创建的预算预留记录。
    # 参数 input_tokens：模型调用实际使用或待估价的输入 Token 数。
    # 参数 output_tokens：模型调用实际使用或待估价的输出 Token 数。
    # 参数 uncertain：供应商请求终态是否未知，需按预留量保守计费。
    def settle_model_call(
        self,
        reservation: dict[str, Any],
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        uncertain: bool = False,
    ) -> dict[str, Any]:
        task_id = str(reservation.get("task_id", ""))
        if not task_id:
            return {}
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                "SELECT budget_json, usage_json FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return {}
            budget = self._normalize_budget(self._json(row["budget_json"], {}))
            usage = self._normalize_usage(self._json(row["usage_json"], {}))
            reserved_tokens = max(0, int(reservation.get("reserved_tokens", 0)))
            reserved_cost = max(0.0, float(reservation.get("reserved_cost", 0.0)))
            usage["reserved_tokens"] = max(
                0,
                int(usage["reserved_tokens"]) - reserved_tokens,
            )
            usage["reserved_cost"] = round(
                max(0.0, float(usage["reserved_cost"]) - reserved_cost),
                8,
            )
            if uncertain:
                usage["uncertain_tokens"] = int(usage["uncertain_tokens"]) + reserved_tokens
                usage["uncertain_cost"] = round(
                    float(usage["uncertain_cost"]) + reserved_cost,
                    8,
                )
            else:
                actual_input = max(0, int(input_tokens))
                actual_output = max(0, int(output_tokens))
                usage["input_tokens"] = int(usage["input_tokens"]) + actual_input
                usage["output_tokens"] = int(usage["output_tokens"]) + actual_output
                usage["estimated_cost"] = round(
                    float(usage["estimated_cost"])
                    + self._estimated_cost(
                        budget,
                        input_tokens=actual_input,
                        output_tokens=actual_output,
                    ),
                    8,
                )
            conn.execute(
                "UPDATE agent_tasks SET usage_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(usage, ensure_ascii=False), time.time(), task_id),
            )
        status = self.budget_status(task_id)
        resource = self._first_exceeded(status)
        if resource:
            self._persist_budget_exceeded(task_id, resource)
            status = self.budget_status(task_id)
        self.append_event(
            task_id,
            "budget_usage_uncertain" if uncertain else "budget_usage",
            status="uncertain" if uncertain else "completed",
            phase="model_waiting",
            action=str(reservation.get("model", "")),
            detail=(
                "模型请求结束状态不确定，已保守计入预留额度"
                if uncertain
                else f"模型累计使用 {status['used']['model_tokens']} Token"
            ),
            payload={
                "resource": "model_tokens",
                "used": status["used"]["model_tokens"],
                "uncertain": uncertain,
            },
        )
        return status

    # 作用：当供应商请求尚未发出时释放预留额度并撤回调用次数。
    # 参数 reservation：模型调用前原子创建的预算预留记录。
    def release_model_call(self, reservation: dict[str, Any]) -> dict[str, Any]:
        """Release a reservation when no provider request was started."""
        task_id = str(reservation.get("task_id", ""))
        if not task_id:
            return {}
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                "SELECT usage_json FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return {}
            usage = self._normalize_usage(self._json(row["usage_json"], {}))
            usage["reserved_tokens"] = max(
                0,
                int(usage["reserved_tokens"]) - max(0, int(reservation.get("reserved_tokens", 0))),
            )
            usage["reserved_cost"] = round(
                max(0.0, float(usage["reserved_cost"]) - max(0.0, float(reservation.get("reserved_cost", 0.0)))),
                8,
            )
            usage["model_calls"] = max(0, int(usage["model_calls"]) - 1)
            conn.execute(
                "UPDATE agent_tasks SET usage_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(usage, ensure_ascii=False), time.time(), task_id),
            )
        return self.budget_status(task_id)

    # 作用：保存不含原始提示词和工具正文的模型循环结构检查点。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 state：待保存的检查点状态或待展示的熔断状态。
    # 参数 loop_index：模型—工具循环当前所在的轮次。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    # 参数 conversation_digest：对话结构的安全摘要，不包含原始提示词。
    # 参数 tools：写入检查点的工具结构摘要列表。
    # 参数 detail：写入任务事件或检查点的简短可读说明。
    def save_checkpoint(
        self,
        task_id: str,
        *,
        state: str,
        loop_index: int | None = None,
        model: str | None = None,
        conversation_digest: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        """Persist a structural model-loop boundary without prompt or tool payload text."""
        if not task_id:
            return {}
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                """
                SELECT checkpoint_json, checkpoint_revision
                FROM agent_tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return {}
            previous = self._normalize_checkpoint(
                self._json(row["checkpoint_json"], {})
            )
            revision = int(row["checkpoint_revision"] or 0) + 1
            checkpoint = {
                "version": 1,
                "revision": revision,
                "state": str(state).strip()[:80] or "unknown",
                "loop_index": (
                    max(0, int(loop_index))
                    if loop_index is not None
                    else int(previous.get("loop_index", 0))
                ),
                "model": (
                    str(model).strip()[:120]
                    if model is not None
                    else str(previous.get("model", ""))
                ),
                "conversation_digest": (
                    self._safe_digest(conversation_digest)
                    if conversation_digest is not None
                    else str(previous.get("conversation_digest", ""))
                ),
                "tools": (
                    self._safe_checkpoint_tools(tools)
                    if tools is not None
                    else list(previous.get("tools", []))
                ),
                "updated_at": now,
            }
            conn.execute(
                """
                UPDATE agent_tasks SET checkpoint_json = ?, checkpoint_revision = ?,
                    updated_at = ? WHERE id = ?
                """,
                (
                    json.dumps(checkpoint, ensure_ascii=False),
                    revision,
                    now,
                    task_id,
                ),
            )
        self.append_event(
            task_id,
            "checkpoint_saved",
            status="completed",
            phase="checkpointing",
            action=str(model or checkpoint.get("model", "")),
            detail=detail or f"已保存模型循环检查点 #{revision} · {checkpoint['state']}",
            payload={
                "revision": revision,
                "state": checkpoint["state"],
                "loop_index": checkpoint["loop_index"],
                "tool_count": len(checkpoint["tools"]),
            },
        )
        return checkpoint

    # 作用：读取并规范化任务最近一次结构检查点。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def checkpoint(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            return {}
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT checkpoint_json FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return (
            self._normalize_checkpoint(self._json(row["checkpoint_json"], {}))
            if row is not None
            else {}
        )

    # 作用：从检查点与步骤证据生成有限恢复上下文，并标明副作用不可自行重放。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def resume_context(self, task_id: str) -> str:
        """Build bounded evidence context; never restore raw prompts or tool bodies."""
        task = self.get(task_id)
        if task is None:
            return ""
        checkpoint = task.get("checkpoint")
        steps = task.get("steps") if isinstance(task.get("steps"), list) else []
        if not checkpoint and not steps:
            return ""
        lines = [
            "这是同一 Agent 任务的安全恢复。只能依据下列持久证据继续；"
            "不要声称恢复了未保存的模型思考或原始工具正文。"
        ]
        if isinstance(checkpoint, dict) and checkpoint:
            lines.append(
                "最近检查点："
                f"revision={checkpoint.get('revision', 0)}，"
                f"state={checkpoint.get('state', 'unknown')}，"
                f"loop={checkpoint.get('loop_index', 0)}。"
            )
            checkpoint_tools = (
                checkpoint.get("tools")
                if isinstance(checkpoint.get("tools"), list)
                else []
            )
            if checkpoint_tools:
                lines.append("检查点工具摘要（仅结构记录，不等同于执行证据）：")
                for item in checkpoint_tools[-20:]:
                    if not isinstance(item, dict):
                        continue
                    action = str(item.get("action", "tool"))[:100]
                    status = str(item.get("status", "unknown"))[:80]
                    step_id = int(item.get("step_id", 0) or 0)
                    suffix = f"，step_id={step_id}" if step_id else ""
                    rule = (
                        "；副作用已有提交状态，不得仅凭此摘要重复"
                        if action in self.SIDE_EFFECT_ACTIONS
                        and status
                        in {
                            "completed",
                            "waiting_confirmation",
                            "waiting_background",
                            "failed",
                        }
                        else "；需要时重新评估，并以步骤账本为准"
                    )
                    lines.append(f"- {action}: {status}{suffix}{rule}")
        evidence_lines: list[str] = []
        for step in steps[-20:]:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action", "tool"))[:100]
            status = str(step.get("status", "unknown"))[:80]
            output = step.get("output") if isinstance(step.get("output"), dict) else {}
            audit = output.get("audit") if isinstance(output.get("audit"), dict) else {}
            facts = []
            for key in ("path", "capability", "operation"):
                if audit.get(key):
                    facts.append(f"{key}={str(audit[key])[:180]}")
            for key in ("verified", "created", "deduplicated"):
                if key in audit:
                    facts.append(f"{key}={bool(audit[key])}")
            suffix = f"（{'，'.join(facts)}）" if facts else ""
            replay_rule = (
                "；这是副作用步骤，已完成或结果不确定时不得自行重复"
                if action in self.SIDE_EFFECT_ACTIONS
                else "；如仍需要只读正文，可重新读取"
            )
            evidence_lines.append(f"- {action}: {status}{suffix}{replay_rule}")
        if evidence_lines:
            lines.append("既有工具步骤：")
            lines.extend(evidence_lines)
        lines.append("从未完成的目标继续，并以新的工具证据完成最终验收。")
        return "\n".join(lines)[:4000]

    # 作用：汇总检查点持久化能力、隐私边界和最新版本号。
    # 参数：无。
    def checkpoint_status(self) -> dict[str, Any]:
        try:
            with self.backend.connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count,
                           COALESCE(MAX(checkpoint_revision), 0) AS latest_revision
                    FROM agent_tasks WHERE checkpoint_revision > 0
                    """
                ).fetchone()
        except Exception as exc:
            return {"ok": False, "persistent": True, "error": str(exc)[:300]}
        return {
            "ok": True,
            "persistent": True,
            "structural_only": True,
            "stores_raw_prompts": False,
            "stores_raw_tool_payloads": False,
            "resume_evidence_bounded": True,
            "checkpointed_tasks": int(row["count"]) if row else 0,
            "latest_revision": int(row["latest_revision"]) if row else 0,
        }

    # 作用：为一次工具动作创建有序运行中步骤，并把任务推进到执行阶段。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 arguments：工具或步骤执行所需的结构化参数。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    def start_step(
        self,
        task_id: str,
        action: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
    ) -> int | None:
        if not task_id or self.cancel_requested(task_id):
            return None
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM agent_task_steps WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            sequence = int(row["sequence"] if row is not None else 1)
            cursor = conn.execute(
                """
                INSERT INTO agent_task_steps(
                    task_id, sequence, action, status, idempotency_key,
                    input_json, started_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    task_id,
                    sequence,
                    action,
                    idempotency_key[:128],
                    json.dumps(arguments, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            inserted = cursor.fetchone()
            conn.execute(
                """
                UPDATE agent_tasks SET status = 'running', phase = 'executing',
                    run_started_at = COALESCE(run_started_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            if inserted is None:
                raise RuntimeError("Agent 步骤创建失败。")
            step_id = int(inserted["id"])
        self.append_event(
            task_id,
            "tool_started",
            status="running",
            phase="executing",
            step_id=step_id,
            action=action,
            detail=f"开始调用 {action}",
        )
        return step_id

    # 作用：按幂等键查找已完成或等待中的步骤，避免重复执行同一动作。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    def reusable_step(
        self,
        task_id: str,
        action: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if not task_id or not idempotency_key:
            return None
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_task_steps
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                  AND status IN ('completed', 'waiting_confirmation', 'waiting_background')
                ORDER BY sequence ASC LIMIT 1
                """,
                (task_id, action, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["input"] = self._json(item.pop("input_json"), {})
        item["output"] = self._json(item.pop("output_json"), {})
        return item

    # 作用：依据持久副作用回执关闭中断步骤，而不是重新调用工具。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 idempotency_key：标识同一次副作用或工具步骤的幂等键。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 output：工具步骤需要持久化的结构化输出证据。
    # 参数 error：需要持久化或返回的错误说明。
    def reconcile_step(
        self,
        task_id: str,
        action: str,
        idempotency_key: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> bool:
        """Resolve an interrupted step from durable domain evidence."""
        if not task_id or not idempotency_key:
            return False
        now = time.time()
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            row = conn.execute(
                """
                SELECT id FROM agent_task_steps
                WHERE task_id = ? AND action = ? AND idempotency_key = ?
                  AND status IN ('running', 'interrupted')
                ORDER BY sequence DESC LIMIT 1
                """,
                (task_id, action, idempotency_key[:128]),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                """
                UPDATE agent_task_steps
                SET status = ?, output_json = ?, error = ?, completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND status IN ('running', 'interrupted')
                """,
                (
                    status[:80],
                    json.dumps(output or {}, ensure_ascii=False),
                    error[:2000],
                    now,
                    now,
                    int(row["id"]),
                ),
            )
        if cursor.rowcount:
            self.append_event(
                task_id,
                "tool_reconciled",
                status=status,
                phase="reconciling",
                step_id=int(row["id"]),
                action=action,
                detail=f"已根据持久副作用证据对账 {action}",
                payload={"evidence": "durable_effect_receipt"},
            )
        return bool(cursor.rowcount)

    # 作用：列出近期中断任务及其步骤，供启动恢复和副作用对账扫描。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def interrupted_tasks(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE status = 'interrupted'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._decode_task(row, include_steps=True) for row in rows]

    # 作用：写入工具步骤终态、精简输出和错误，并发布对应进度事件。
    # 参数 step_id：工具步骤账本中的唯一编号。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 output：工具步骤需要持久化的结构化输出证据。
    # 参数 error：需要持久化或返回的错误说明。
    def finish_step(
        self,
        step_id: int,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        now = time.time()
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT task_id, action FROM agent_task_steps WHERE id = ?",
                (step_id,),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE agent_task_steps
                SET status = ?, output_json = ?, error = ?, completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output or {}, ensure_ascii=False),
                    error[:2000],
                    now,
                    now,
                    step_id,
                ),
            )
        if row is not None and cursor.rowcount:
            self.append_event(
                str(row["task_id"]),
                self._step_event(status),
                status=status,
                phase=self._step_phase(status),
                step_id=step_id,
                action=str(row["action"]),
                detail=self._step_detail(str(row["action"]), status),
            )

    # 作用：根据确认结果关闭最近的 waiting_confirmation 步骤。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 error：需要持久化或返回的错误说明。
    # 参数 output：工具步骤需要持久化的结构化输出证据。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    def resolve_waiting_step(
        self,
        task_id: str,
        *,
        status: str,
        error: str = "",
        output: dict[str, Any] | None = None,
        action: str = "",
    ) -> None:
        if not task_id:
            return
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id, action FROM agent_task_steps
                WHERE task_id = ? AND status = 'waiting_confirmation'
                ORDER BY sequence DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is not None:
                now = time.time()
                assignments = "status = ?, error = ?, completed_at = ?, updated_at = ?"
                values: list[Any] = [status, error[:2000], now, now]
                if output is not None:
                    assignments += ", output_json = ?"
                    values.append(json.dumps(output, ensure_ascii=False))
                if action:
                    assignments += ", action = ?"
                    values.append(action)
                values.append(int(row["id"]))
                conn.execute(
                    f"UPDATE agent_task_steps SET {assignments} WHERE id = ?",
                    values,
                )
        if row is not None:
            self.append_event(
                task_id,
                self._step_event(status),
                status=status,
                phase=self._step_phase(status),
                step_id=int(row["id"]),
                action=str(action or row["action"]),
                detail=self._step_detail(str(action or row["action"]), status),
            )

    # 作用：将匹配的不确定外发步骤重新置为待确认，供用户显式核对或重试。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 operation_id：用户确认单对应的原始操作唯一标识。
    # 参数 dispatch_id：外发账本记录的公开唯一标识。
    # 参数 decision：用户对未知外发所作的重试、确认成功或放弃决定。
    def prepare_external_dispatch_review(
        self,
        task_id: str,
        operation_id: str,
        *,
        dispatch_id: str,
        decision: str,
    ) -> bool:
        """Reopen the matching interrupted capability step for an explicit review."""
        if not task_id or not operation_id:
            return False
        chosen: Mapping[str, Any] | None = None
        with self.backend.connect(immediate=True) as conn:
            self.backend.lock_scope(conn, f"agent-task:{task_id}")
            rows = conn.execute(
                """
                SELECT id, status, output_json FROM agent_task_steps
                WHERE task_id = ? AND action = 'capability_call'
                  AND status IN ('interrupted', 'waiting_confirmation')
                ORDER BY sequence DESC
                """,
                (task_id,),
            ).fetchall()
            for row in rows:
                output = self._json(str(row["output_json"] or "{}"), {})
                audit = output.get("audit", {}) if isinstance(output, dict) else {}
                if isinstance(audit, dict) and str(
                    audit.get("source_operation_id") or audit.get("operation_id") or ""
                ) == operation_id:
                    chosen = row
                    break
            if chosen is None and rows:
                chosen = rows[0]
            if chosen is None:
                return False
            if str(chosen["status"]) == "interrupted":
                cursor = conn.execute(
                    """
                    UPDATE agent_task_steps
                    SET status = 'waiting_confirmation', completed_at = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'interrupted'
                    """,
                    (time.time(), int(chosen["id"])),
                )
                if not cursor.rowcount:
                    return False
        self.append_event(
            task_id,
            "external_dispatch_review",
            status="waiting_confirmation",
            phase="external_dispatch_review",
            step_id=int(chosen["id"]),
            action="capability_call",
            detail={
                "retry": "用户已明确要求使用原外发标识重试",
                "confirm_succeeded": "用户已确认外部系统中操作成功",
                "abandon": "用户已放弃这次不确定外发",
                "reconcile_completed": "已根据持久外发完成回执恢复任务证据",
            }.get(decision, "用户正在核对不确定外发"),
            payload={"dispatch_id": dispatch_id, "decision": decision},
        )
        return True

    # 作用：关闭最近的后台托管步骤并返回其原始工具动作。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 output：工具步骤需要持久化的结构化输出证据。
    # 参数 error：需要持久化或返回的错误说明。
    def resolve_background_step(
        self,
        task_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> str:
        """Finish the latest background-owned step and return its original action."""
        if not task_id:
            return ""
        now = time.time()
        with self.backend.connect() as conn:
            row = conn.execute(
                """
                SELECT id, action FROM agent_task_steps
                WHERE task_id = ? AND status = 'waiting_background'
                ORDER BY sequence DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return ""
            cursor = conn.execute(
                """
                UPDATE agent_task_steps
                SET status = ?, output_json = ?, error = ?, completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'waiting_background'
                """,
                (
                    status,
                    json.dumps(output or {}, ensure_ascii=False),
                    error[:2000],
                    now,
                    now,
                    int(row["id"]),
                ),
            )
        original_action = str(row["action"])
        if cursor.rowcount:
            self.append_event(
                task_id,
                self._step_event(status),
                status=status,
                phase=self._step_phase(status),
                step_id=int(row["id"]),
                action=original_action,
                detail=self._step_detail(original_action, status),
            )
        return original_action

    # 作用：标记非终态任务正在取消，使后续步骤停止接纳新工作。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    def cancel(self, task_id: str, *, session_id: str = "") -> dict[str, Any] | None:
        conditions = "id = ?"
        conditions_values: list[Any] = [task_id]
        if session_id:
            conditions += " AND session_id = ?"
            conditions_values.append(session_id)
        with self.backend.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE agent_tasks
                SET cancel_requested = 1, status = 'cancel_requested', phase = 'cancelling', updated_at = ?
                WHERE {conditions} AND status NOT IN (
                    'completed', 'failed', 'degraded', 'cancelled', 'interrupted'
                )
                """,
                [time.time(), *conditions_values],
            )
        if cursor.rowcount:
            self.append_event(
                task_id,
                "task_cancel_requested",
                status="cancel_requested",
                phase="cancelling",
                detail="用户已请求停止，正在阻止后续步骤",
            )
        return self.get(task_id) if cursor.rowcount else None

    # 作用：通过会话请求 ID 定位并取消对应任务。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    def cancel_by_request(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        task = self.get_by_request(session_id, request_id)
        return self.cancel(str(task["id"]), session_id=session_id) if task is not None else None

    # 作用：查询任务是否已经收到取消请求，供模型和工具边界快速中止。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def cancel_requested(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM agent_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    # 作用：按任务 ID 读取任务及其全部步骤。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._decode_task(row, include_steps=True) if row is not None else None

    # 作用：按会话和请求 ID 查询幂等任务及其步骤。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 request_id：调用方提供的请求幂等标识。
    def get_by_request(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE session_id = ? AND request_id = ?",
                (session_id, request_id),
            ).fetchone()
        return self._decode_task(row, include_steps=True) if row is not None else None

    # 作用：按更新时间倒序列出会话任务摘要，不加载步骤明细。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def list(self, session_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_tasks WHERE session_id = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (session_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._decode_task(row, include_steps=False) for row in rows]

    # 作用：追加有序且限长的进度事件，供断线客户端按游标补偿重放。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 event：要追加到任务事件流的事件类型。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 phase：任务当前所处的可观察执行阶段。
    # 参数 step_id：工具步骤账本中的唯一编号。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 detail：写入任务事件或检查点的简短可读说明。
    # 参数 payload：请求、作业或事件携带的结构化载荷。
    def append_event(
        self,
        task_id: str,
        event: str,
        *,
        status: str = "",
        phase: str = "",
        step_id: int | None = None,
        action: str = "",
        detail: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Append a redacted progress event that can be replayed after reconnect."""
        if not task_id or not event:
            return None
        safe_payload = payload or {}
        encoded = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 20_000:
            encoded = "{}"
        now = time.time()
        with self.backend.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if exists is None:
                return None
            cursor = conn.execute(
                """
                INSERT INTO agent_task_events(
                    task_id, event, status, phase, step_id, action,
                    detail, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    task_id,
                    event[:80],
                    status[:80],
                    phase[:80],
                    step_id,
                    action[:100],
                    detail[:500],
                    encoded,
                    now,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                raise RuntimeError("Agent 事件创建失败。")
            event_id = int(inserted["id"])
        return {
            "id": event_id,
            "task_id": task_id,
            "event": event[:80],
            "status": status[:80],
            "phase": phase[:80],
            "step_id": step_id,
            "action": action[:100],
            "detail": detail[:500],
            "payload": safe_payload if encoded != "{}" or not safe_payload else {},
            "created_at": now,
        }

    # 作用：从指定游标之后按顺序读取任务事件，用于 REST/WebSocket 续传。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 after_id：事件增量读取所使用的起始游标，不包含该游标本身。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def events(
        self,
        task_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_task_events
                WHERE task_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (task_id, max(0, int(after_id)), max(1, min(int(limit), 200))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._json(item.pop("payload_json", "{}"), {})
            result.append(item)
        return result

    # 作用：检查持久事件流并报告总量与最新全局游标。
    # 参数：无。
    def event_status(self) -> dict[str, Any]:
        try:
            with self.backend.connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count,
                           COALESCE(MAX(id), 0) AS latest_cursor
                    FROM agent_task_events
                    """
                ).fetchone()
        except Exception as exc:
            return {"ok": False, "persistent": True, "error": str(exc)[:300]}
        return {
            "ok": True,
            "persistent": True,
            "ordered_cursor": True,
            "redacted_payloads": True,
            "total_events": int(row["count"]) if row else 0,
            "latest_cursor": int(row["latest_cursor"]) if row else 0,
        }

    # 作用：报告当前默认预算、原子预留及不确定用量处理能力。
    # 参数：无。
    def budget_configuration_status(self) -> dict[str, Any]:
        budget = self.default_budget()
        return {
            "ok": True,
            "persistent_task_snapshot": True,
            "atomic_reservations": True,
            "uncertain_usage_accounting": True,
            "resume_preserves_usage": True,
            "pricing_active": self._pricing_active(budget),
            "currency": budget["currency"],
            "limits": {
                key: budget[key]
                for key in (
                    "wall_time_seconds",
                    "model_token_limit",
                    "model_cost_limit",
                    "tool_call_limit",
                    "model_call_limit",
                    "max_output_tokens",
                )
            },
        }

    # 作用：按依赖顺序删除会话的事件、步骤和任务记录。
    # 参数 session_id：当前用户会话的唯一标识。
    def delete_session(self, session_id: str) -> int:
        with self.backend.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM agent_tasks WHERE session_id = ?", (session_id,)
            ).fetchall()
            task_ids = [str(row["id"]) for row in rows]
            deleted_steps = 0
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                conn.execute(
                    f"DELETE FROM agent_task_events WHERE task_id IN ({placeholders})",
                    task_ids,
                )
                cursor = conn.execute(
                    f"DELETE FROM agent_task_steps WHERE task_id IN ({placeholders})",
                    task_ids,
                )
                deleted_steps = int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM agent_tasks WHERE session_id = ?", (session_id,)
            )
        return deleted_steps + int(cursor.rowcount or 0)

    # 作用：重启时把遗留任务和步骤关闭到 interrupted 或 cancelled，且不自动重放。
    # 参数：无。
    def recover_interrupted(self) -> dict[str, int]:
        """Close tasks left non-terminal by a previous service process."""
        now = time.time()
        interrupted_outcome = json.dumps(
            {
                "status": "interrupted",
                "verified": False,
                "summary": "服务在任务完成前停止；为避免重复副作用，任务没有自动重放。",
            },
            ensure_ascii=False,
        )
        cancelled_outcome = json.dumps(
            {
                "status": "cancelled",
                "verified": False,
                "summary": "服务重启时完成了此前已经请求的停止操作。",
            },
            ensure_ascii=False,
        )
        with self.backend.connect() as conn:
            interrupted_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM agent_tasks WHERE status IN ('created', 'running')"
                ).fetchall()
            ]
            cancelled_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM agent_tasks WHERE status = 'cancel_requested'"
                ).fetchall()
            ]
            self._close_run_timers(conn, [*interrupted_ids, *cancelled_ids], now)
            interrupted_steps = self._recover_steps(
                conn,
                interrupted_ids,
                status="interrupted",
                error="服务重启导致步骤中断。",
                now=now,
            )
            cancelled_steps = self._recover_steps(
                conn,
                cancelled_ids,
                status="cancelled",
                error="任务已经停止。",
                now=now,
            )
            interrupted = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'interrupted', phase = 'interrupted', outcome_json = ?,
                    error = '服务重启导致任务中断。', cancel_requested = 0, updated_at = ?
                WHERE status IN ('created', 'running')
                """,
                (interrupted_outcome, now),
            )
            cancelled = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'cancelled', phase = 'cancelled', outcome_json = ?,
                    error = '', updated_at = ?
                WHERE status = 'cancel_requested'
                """,
                (cancelled_outcome, now),
            )
        for task_id in interrupted_ids:
            self.append_event(
                task_id,
                "task_interrupted",
                status="interrupted",
                phase="interrupted",
                detail="服务重启前任务未完成，已停止自动重放",
            )
        for task_id in cancelled_ids:
            self.append_event(
                task_id,
                "task_cancelled",
                status="cancelled",
                phase="cancelled",
                detail="服务重启时完成了停止操作",
            )
        return {
            "interrupted_tasks": int(interrupted.rowcount or 0),
            "cancelled_tasks": int(cancelled.rowcount or 0),
            "interrupted_steps": interrupted_steps,
            "cancelled_steps": cancelled_steps,
        }

    # 作用：服务中断时结算仍在计时任务的累计运行时长。
    # 参数 conn：当前事务使用的关系型数据库连接。
    # 参数 task_ids：需要批量恢复或结算的 Agent 任务 ID 列表。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    @classmethod
    # 作用：执行“close_run_timers”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 conn：调用方传入的conn，用于本次处理。
    # 参数 task_ids：调用方传入的task_ids，用于本次处理。
    # 参数 now：调用方传入的now，用于本次处理。
    def _close_run_timers(
        cls,
        conn: RelationalConnection,
        task_ids: list[str],
        now: float,
    ) -> None:
        for task_id in task_ids:
            row = conn.execute(
                "SELECT usage_json, run_started_at FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["run_started_at"] is None:
                continue
            usage = cls._normalize_usage(cls._json(row["usage_json"], {}))
            usage["wall_seconds"] = round(
                float(usage["wall_seconds"])
                + max(0.0, now - float(row["run_started_at"])),
                3,
            )
            conn.execute(
                "UPDATE agent_tasks SET usage_json = ?, run_started_at = NULL WHERE id = ?",
                (json.dumps(usage, ensure_ascii=False), task_id),
            )

    # 作用：批量把任务中的运行中步骤转换为指定安全终态。
    # 参数 conn：当前事务使用的关系型数据库连接。
    # 参数 task_ids：需要批量恢复或结算的 Agent 任务 ID 列表。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 error：需要持久化或返回的错误说明。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    @staticmethod
    # 作用：执行“recover_steps”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 conn：调用方传入的conn，用于本次处理。
    # 参数 task_ids：调用方传入的task_ids，用于本次处理。
    # 参数 status：调用方传入的status，用于本次处理。
    # 参数 error：调用方传入的error，用于本次处理。
    # 参数 now：调用方传入的now，用于本次处理。
    def _recover_steps(
        conn: RelationalConnection,
        task_ids: list[str],
        *,
        status: str,
        error: str,
        now: float,
    ) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" for _ in task_ids)
        cursor = conn.execute(
            f"""
            UPDATE agent_task_steps
            SET status = ?, error = ?, completed_at = ?, updated_at = ?
            WHERE task_id IN ({placeholders}) AND status = 'running'
            """,
            [status, error, now, now, *task_ids],
        )
        return int(cursor.rowcount or 0)

    # 作用：读取任务的有序步骤并解码输入输出结构。
    # 参数 task_id：Agent 持久任务的唯一标识。
    def _steps(self, task_id: str) -> list[dict[str, Any]]:
        with self.backend.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_task_steps WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = self._json(item.pop("input_json"), {})
            item["output"] = self._json(item.pop("output_json"), {})
            result.append(item)
        return result

    # 作用：将任务数据库行还原为计划、契约、预算、检查点及可选步骤结构。
    # 参数 row：需要解码或计算的数据库查询行。
    # 参数 include_steps：解码任务时是否同时加载工具步骤明细。
    def _decode_task(
        self,
        row: Mapping[str, Any],
        *,
        include_steps: bool,
    ) -> dict[str, Any]:
        item = dict(row)
        item["request"] = self._json(item.pop("request_json"), {})
        item["plan"] = self._json(item.pop("plan_json"), [])
        item["contract"] = self._json(item.pop("contract_json", "{}"), {})
        item["budget"] = self._normalize_budget(item.pop("budget_json", {}))
        item["usage"] = self._normalize_usage(item.pop("usage_json", {}))
        item["checkpoint"] = self._normalize_checkpoint(
            item.pop("checkpoint_json", {})
        )
        item["outcome"] = self._json(item.pop("outcome_json"), {})
        item["cancel_requested"] = bool(item["cancel_requested"])
        item["steps"] = self._steps(str(item["id"])) if include_steps else []
        return item

    # 作用：从环境配置生成新任务的默认时长、模型、费用和工具预算。
    # 参数：无。
    @classmethod
    # 作用：执行“default_budget”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def default_budget(cls) -> dict[str, Any]:
        return {
            "wall_time_seconds": cls._bounded_env_float(
                "YUKINO_TASK_WALL_TIME_SECONDS", 180, 10, 1800
            ),
            "model_token_limit": cls._bounded_env_int(
                "YUKINO_TASK_MODEL_TOKEN_LIMIT", 32768, 1000, 1_000_000
            ),
            "model_cost_limit": cls._bounded_env_float(
                "YUKINO_TASK_MODEL_COST_LIMIT", 1.0, 0.01, 100
            ),
            "tool_call_limit": cls._bounded_env_int(
                "YUKINO_TASK_TOOL_CALL_LIMIT", 16, 1, 100
            ),
            "model_call_limit": cls._bounded_env_int(
                "YUKINO_TASK_MODEL_CALL_LIMIT", 8, 1, 50
            ),
            "max_output_tokens": cls._bounded_env_int(
                "YUKINO_MODEL_MAX_OUTPUT_TOKENS", 2048, 64, 8192
            ),
            "input_cost_per_million": cls._bounded_env_float(
                "YUKINO_INPUT_COST_PER_MILLION", 0, 0, 10_000
            ),
            "output_cost_per_million": cls._bounded_env_float(
                "YUKINO_OUTPUT_COST_PER_MILLION", 0, 0, 10_000
            ),
            "currency": (os.getenv("YUKINO_COST_CURRENCY", "USD").strip() or "USD")[:12],
        }

    # 作用：创建所有累计量为零的任务用量结构。
    # 参数：无。
    @staticmethod
    # 作用：执行“empty_usage”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def empty_usage() -> dict[str, Any]:
        return {
            "wall_seconds": 0.0,
            "model_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reserved_tokens": 0,
            "reserved_cost": 0.0,
            "estimated_cost": 0.0,
            "uncertain_tokens": 0,
            "uncertain_cost": 0.0,
            "budget_exceeded": "",
        }

    # 作用：将历史或外部预算快照补齐为类型安全的当前结构。
    # 参数 value：待清洗、规范化或编码的原始值。
    @classmethod
    # 作用：执行“normalize_budget”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _normalize_budget(cls, value: Any) -> dict[str, Any]:
        raw = cls._json(value, {}) if not isinstance(value, dict) else value
        defaults = cls.default_budget()
        normalized = dict(defaults)
        for key in (
            "wall_time_seconds",
            "model_cost_limit",
            "input_cost_per_million",
            "output_cost_per_million",
        ):
            try:
                normalized[key] = max(0.0, float(raw.get(key, defaults[key])))
            except (TypeError, ValueError):
                pass
        for key in (
            "model_token_limit",
            "tool_call_limit",
            "model_call_limit",
            "max_output_tokens",
        ):
            try:
                normalized[key] = max(1, int(raw.get(key, defaults[key])))
            except (TypeError, ValueError):
                pass
        normalized["currency"] = str(raw.get("currency", defaults["currency"]))[:12]
        return normalized

    # 作用：校验并清洗结构检查点，拒绝损坏字段和原始大载荷。
    # 参数 value：待清洗、规范化或编码的原始值。
    @classmethod
    # 作用：执行“normalize_checkpoint”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _normalize_checkpoint(cls, value: Any) -> dict[str, Any]:
        raw = cls._json(value, {}) if not isinstance(value, dict) else value
        if not isinstance(raw, dict) or not raw:
            return {}
        try:
            revision = max(0, int(raw.get("revision", 0)))
            loop_index = max(0, int(raw.get("loop_index", 0)))
            updated_at = max(0.0, float(raw.get("updated_at", 0.0)))
        except (TypeError, ValueError):
            return {}
        return {
            "version": 1,
            "revision": revision,
            "state": str(raw.get("state", "unknown"))[:80],
            "loop_index": loop_index,
            "model": str(raw.get("model", ""))[:120],
            "conversation_digest": cls._safe_digest(
                str(raw.get("conversation_digest", ""))
            ),
            "tools": cls._safe_checkpoint_tools(raw.get("tools", [])),
            "updated_at": updated_at,
        }

    # 作用：将摘要限制为最多 64 位十六进制字符。
    # 参数 value：待清洗、规范化或编码的原始值。
    @staticmethod
    # 作用：执行“safe_digest”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _safe_digest(value: str | None) -> str:
        return "".join(
            char for char in str(value or "").lower() if char in "0123456789abcdef"
        )[:64]

    # 作用：仅保留可恢复判断所需的工具动作、状态、步骤和指纹摘要。
    # 参数 value：待清洗、规范化或编码的原始值。
    @classmethod
    # 作用：执行“safe_checkpoint_tools”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _safe_checkpoint_tools(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in value[:20]:
            if not isinstance(raw, dict):
                continue
            try:
                step_id = max(0, int(raw.get("step_id", 0)))
            except (TypeError, ValueError):
                step_id = 0
            result.append(
                {
                    "action": str(raw.get("action", ""))[:100],
                    "status": str(raw.get("status", ""))[:80],
                    "step_id": step_id,
                    "fingerprint": cls._safe_digest(
                        str(raw.get("fingerprint", ""))
                    ),
                    "success": bool(raw.get("success", False)),
                    "requires_confirmation": bool(
                        raw.get("requires_confirmation", False)
                    ),
                    "background_pending": bool(raw.get("background_pending", False)),
                }
            )
        return result

    # 作用：将历史用量快照规范为非负、字段完整的计费结构。
    # 参数 value：待清洗、规范化或编码的原始值。
    @classmethod
    # 作用：执行“normalize_usage”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _normalize_usage(cls, value: Any) -> dict[str, Any]:
        raw = cls._json(value, {}) if not isinstance(value, dict) else value
        normalized = cls.empty_usage()
        for key in (
            "model_calls",
            "tool_calls",
            "input_tokens",
            "output_tokens",
            "reserved_tokens",
            "uncertain_tokens",
        ):
            try:
                normalized[key] = max(0, int(raw.get(key, 0)))
            except (TypeError, ValueError):
                pass
        for key in (
            "wall_seconds",
            "reserved_cost",
            "estimated_cost",
            "uncertain_cost",
        ):
            try:
                normalized[key] = max(0.0, float(raw.get(key, 0.0)))
            except (TypeError, ValueError):
                pass
        normalized["budget_exceeded"] = str(raw.get("budget_exceeded", ""))[:40]
        return normalized

    # 作用：在持久用量上叠加当前尚未结算的运行时长。
    # 参数 row：需要解码或计算的数据库查询行。
    @classmethod
    # 作用：执行“usage_with_elapsed”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 row：调用方传入的row，用于本次处理。
    def _usage_with_elapsed(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        usage = cls._normalize_usage(cls._json(row["usage_json"], {}))
        if row["run_started_at"] is not None:
            usage["wall_seconds"] = round(
                float(usage["wall_seconds"])
                + max(0.0, time.time() - float(row["run_started_at"])),
                3,
            )
        return usage

    # 作用：合并已用、预留和不确定额度，得到预算比较口径。
    # 参数 usage：任务累计的时长、模型与工具资源用量。
    @staticmethod
    # 作用：执行“budget_used”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 usage：调用方传入的usage，用于本次处理。
    def _budget_used(usage: dict[str, Any]) -> dict[str, float | int]:
        return {
            "wall_time": round(float(usage["wall_seconds"]), 3),
            "model_tokens": (
                int(usage["input_tokens"])
                + int(usage["output_tokens"])
                + int(usage["reserved_tokens"])
                + int(usage["uncertain_tokens"])
            ),
            "model_cost": round(
                float(usage["estimated_cost"])
                + float(usage["reserved_cost"])
                + float(usage["uncertain_cost"]),
                8,
            ),
            "model_calls": int(usage["model_calls"]),
            "tool_calls": int(usage["tool_calls"]),
        }

    # 作用：按固定优先级返回预算与用量之间首个超限资源。
    # 参数 budget：任务预算策略或预算快照。
    # 参数 usage：任务累计的时长、模型与工具资源用量。
    @classmethod
    # 作用：执行“first_exceeded_for”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 budget：调用方传入的budget，用于本次处理。
    # 参数 usage：调用方传入的usage，用于本次处理。
    def _first_exceeded_for(
        cls,
        budget: dict[str, Any],
        usage: dict[str, Any],
    ) -> str:
        used = cls._budget_used(usage)
        if float(used["wall_time"]) >= float(budget["wall_time_seconds"]):
            return "wall_time"
        if int(used["model_tokens"]) > int(budget["model_token_limit"]):
            return "model_tokens"
        if cls._pricing_active(budget) and float(used["model_cost"]) > float(
            budget["model_cost_limit"]
        ):
            return "model_cost"
        if int(used["model_calls"]) > int(budget["model_call_limit"]):
            return "model_calls"
        if int(used["tool_calls"]) > int(budget["tool_call_limit"]):
            return "tool_calls"
        return ""

    # 作用：优先采用已持久化超限标记，否则实时重新计算。
    # 参数 status：要写入、筛选或转换的执行状态。
    @classmethod
    # 作用：执行“first_exceeded”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 status：调用方传入的status，用于本次处理。
    def _first_exceeded(cls, status: dict[str, Any]) -> str:
        if not status:
            return ""
        stored = str(status.get("usage", {}).get("budget_exceeded", ""))
        if stored:
            return stored
        return cls._first_exceeded_for(status["policy"], status["usage"])

    # 作用：判断输入或输出单价是否已配置，从而启用费用预算。
    # 参数 budget：任务预算策略或预算快照。
    @staticmethod
    # 作用：执行“pricing_active”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 budget：调用方传入的budget，用于本次处理。
    def _pricing_active(budget: dict[str, Any]) -> bool:
        return bool(
            float(budget.get("input_cost_per_million", 0)) > 0
            or float(budget.get("output_cost_per_million", 0)) > 0
        )

    # 作用：根据输入输出 Token 和单价估算本次模型调用成本。
    # 参数 budget：任务预算策略或预算快照。
    # 参数 input_tokens：模型调用实际使用或待估价的输入 Token 数。
    # 参数 output_tokens：模型调用实际使用或待估价的输出 Token 数。
    @staticmethod
    # 作用：执行“estimated_cost”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 budget：调用方传入的budget，用于本次处理。
    # 参数 input_tokens：调用方传入的input_tokens，用于本次处理。
    # 参数 output_tokens：调用方传入的output_tokens，用于本次处理。
    def _estimated_cost(
        budget: dict[str, Any],
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        value = (
            max(0, input_tokens) * float(budget["input_cost_per_million"])
            + max(0, output_tokens) * float(budget["output_cost_per_million"])
        ) / 1_000_000
        return round(value, 8)

    # 作用：将首次预算超限原因写入任务用量并追加审计事件。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 resource：发生预算超限的资源类型。
    def _persist_budget_exceeded(self, task_id: str, resource: str) -> None:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT usage_json FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return
            usage = self._normalize_usage(self._json(row["usage_json"], {}))
            if usage["budget_exceeded"] == resource:
                return
            usage["budget_exceeded"] = resource
            conn.execute(
                "UPDATE agent_tasks SET usage_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(usage, ensure_ascii=False), time.time(), task_id),
            )
        self._append_budget_exceeded(task_id, resource)

    # 作用：生成包含额度与使用量的预算超限进度事件。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 resource：发生预算超限的资源类型。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    def _append_budget_exceeded(
        self,
        task_id: str,
        resource: str,
        *,
        action: str = "",
    ) -> None:
        status = self.budget_status(task_id)
        if not status:
            return
        exc = self._budget_exception(resource, status)
        self.append_event(
            task_id,
            "budget_exceeded",
            status="degraded",
            phase="budget_exceeded",
            action=action,
            detail=str(exc),
            payload={
                "resource": resource,
                "limit": exc.limit,
                "used": exc.used,
            },
        )

    # 作用：从预算状态构造带上下文的 AgentTaskBudgetExceeded 异常。
    # 参数 resource：发生预算超限的资源类型。
    # 参数 status：要写入、筛选或转换的执行状态。
    @staticmethod
    # 作用：执行“budget_exception”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 resource：调用方传入的resource，用于本次处理。
    # 参数 status：调用方传入的status，用于本次处理。
    def _budget_exception(
        resource: str,
        status: dict[str, Any],
    ) -> AgentTaskBudgetExceeded:
        budget = status["policy"]
        used = status["used"]
        limit_keys = {
            "wall_time": "wall_time_seconds",
            "model_tokens": "model_token_limit",
            "model_cost": "model_cost_limit",
            "model_calls": "model_call_limit",
            "tool_calls": "tool_call_limit",
        }
        return AgentTaskBudgetExceeded(
            resource,
            limit=float(budget[limit_keys[resource]]),
            used=float(used[resource]),
            budget=budget,
            usage=status["usage"],
        )

    # 作用：读取整数环境配置并限制在安全范围内。
    # 参数 name：要读取的环境变量名称。
    # 参数 default：配置缺失或格式无效时采用的默认值。
    # 参数 minimum：环境配置允许的最小值。
    # 参数 maximum：环境配置允许的最大值。
    @staticmethod
    # 作用：执行“bounded_env_int”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 name：调用方传入的name，用于本次处理。
    # 参数 default：输入缺失或无效时使用的默认值。
    # 参数 minimum：调用方传入的minimum，用于本次处理。
    # 参数 maximum：调用方传入的maximum，用于本次处理。
    def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    # 作用：读取浮点环境配置并限制在安全范围内。
    # 参数 name：要读取的环境变量名称。
    # 参数 default：配置缺失或格式无效时采用的默认值。
    # 参数 minimum：环境配置允许的最小值。
    # 参数 maximum：环境配置允许的最大值。
    @staticmethod
    # 作用：执行“bounded_env_float”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 name：调用方传入的name，用于本次处理。
    # 参数 default：输入缺失或无效时使用的默认值。
    # 参数 minimum：调用方传入的minimum，用于本次处理。
    # 参数 maximum：调用方传入的maximum，用于本次处理。
    def _bounded_env_float(
        name: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    # 作用：安全解析 JSON，格式损坏时返回指定默认值。
    # 参数 raw：待解析的 JSON 原始值。
    # 参数 fallback：是否采用了回退模型，或 JSON 解析失败时的默认值。
    @staticmethod
    # 作用：执行“json”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 raw：尚未解析或规范化的原始输入。
    # 参数 fallback：解析失败时返回的兜底对象。
    def _json(raw: Any, fallback: Any) -> Any:
        try:
            return json.loads(str(raw or ""))
        except (json.JSONDecodeError, TypeError):
            return fallback

    # 作用：将步骤状态映射为前端可订阅的事件名称。
    # 参数 status：工具步骤的持久状态。
    @staticmethod
    # 作用：执行“step_event”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 status：调用方传入的status，用于本次处理。
    def _step_event(status: str) -> str:
        return {
            "completed": "tool_completed",
            "waiting_confirmation": "confirmation_required",
            "waiting_background": "background_started",
            "failed": "tool_failed",
            "cancelled": "tool_cancelled",
            "interrupted": "tool_interrupted",
        }.get(status, "tool_updated")

    # 作用：将步骤状态映射为任务进度阶段。
    # 参数 status：工具步骤的持久状态。
    @staticmethod
    # 作用：执行“step_phase”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 status：调用方传入的status，用于本次处理。
    def _step_phase(status: str) -> str:
        return {
            "waiting_confirmation": "waiting_confirmation",
            "waiting_background": "waiting_background",
        }.get(status, "executing")

    # 作用：生成工具动作与步骤状态组合的用户可读说明。
    # 参数 action：当前步骤、工具或扩展能力的动作名称。
    # 参数 status：工具步骤的持久状态。
    @staticmethod
    # 作用：执行“step_detail”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 action：调用方传入的action，用于本次处理。
    # 参数 status：调用方传入的status，用于本次处理。
    def _step_detail(action: str, status: str) -> str:
        labels = {
            "completed": "执行完成",
            "waiting_confirmation": "等待你的确认",
            "waiting_background": "已转入后台执行",
            "failed": "执行失败",
            "cancelled": "已停止",
            "interrupted": "被服务中断",
        }
        return f"{action} · {labels.get(status, status)}"

    # 作用：将任务状态和阶段转换成面向事件流的进度说明。
    # 参数 status：要写入、筛选或转换的执行状态。
    # 参数 phase：任务当前所处的可观察执行阶段。
    @staticmethod
    # 作用：执行“phase_detail”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 status：调用方传入的status，用于本次处理。
    # 参数 phase：调用方传入的phase，用于本次处理。
    def _phase_detail(status: str, phase: str) -> str:
        if phase == "background_queued":
            return (
                "委托已进入持久队列"
                if status == "queued"
                else "任务已转入后台执行"
            )
        return {
            "planning": "正在理解委托并制定计划",
            "planned": "执行计划已经生成",
            "preparing_context": "正在读取记忆、附件与可用能力",
            "model_waiting": "正在等待模型分析与决策",
            "verifying": "正在核对工具证据与完成条件",
            "verified": "委托已完成并通过证据验收",
            "waiting_confirmation": "需要你的确认才能继续",
            "verification_failed": "执行证据未通过验收",
            "degraded": "任务以降级结果结束",
            "cancelled": "任务已经停止",
            "interrupted": "任务因服务中断而停止",
        }.get(phase, f"{status} · {phase}")
