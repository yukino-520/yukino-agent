import datetime as dt
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from service_club.capabilities import CapabilityHub
from service_club.capabilities.agent_files import AgentFileAccess, AgentFileError
from service_club.capabilities.catalog import CAPABILITIES
from service_club.capabilities.policy import CapabilityPermissionDenied
from service_club.core.memory import MemoryManager
from service_club.core.memory.permanent_memory import PermanentMemoryManager
from service_club.core.memory.reminders import ReminderScheduleParser
from service_club.core.runtime.agent_task_store import (
    AgentTaskBudgetExceeded,
    AgentTaskStore,
    agent_task_scope,
    current_agent_task_id,
)
from service_club.core.runtime.audit_redaction import redact_for_audit
from service_club.core.runtime.effect_receipts import EffectReceiptStore
from service_club.core.runtime.external_dispatch_store import ExternalDispatchStore
from service_club.core.runtime.outcome_verifier import AgentOutcomeVerifier
from service_club.core.tooling.code_analysis import inspect_code
from service_club.core.tooling.operation_store import AgentOperationStore
from service_club.core.types import ToolAction, ToolExecutionResult

ToolHandler = Callable[[dict[str, Any], str], ToolExecutionResult]
_CURRENT_TOOL_IDEMPOTENCY_KEY: ContextVar[str] = ContextVar(
    "agi_yukino_tool_idempotency_key",
    default="",
)


# 作用：描述一个已注册工具的模型接口、真实处理器、风险级别和可调用范围。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“RegisteredTool”相关的数据结构、异常类型或服务组件。
# 字段：name：该对象中的结构化字段。、description：该对象中的结构化字段。、handler：该对象中的结构化字段。、risk：该对象中的结构化字段。、parameters：该对象中的结构化字段。、model_callable：该对象中的结构化字段。
class RegisteredTool:
    name: ToolAction
    description: str
    handler: ToolHandler
    risk: str = "low"
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    model_callable: bool = True


# 作用：表示用户核对未知外发时请求无效、记录缺失或状态发生冲突。
# 参数：无。
class ExternalDispatchReviewError(RuntimeError):
    # 作用：保存外发核对错误及其应返回的 HTTP 状态码。
    # 参数 message：向用户说明核对失败原因的错误信息。
    # 参数 status_code：接口应返回的 HTTP 状态码。
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


# 作用：集中注册、授权和执行 Agent 工具，并衔接幂等步骤、确认单与副作用回执。
# 参数：无。
class ServiceClubToolRegistry:
    TASK_DEDUPLICATED_ACTIONS = {
        "remember",
        "forget",
        "clear_memory",
        "set_reminder",
        "cancel_reminder",
        "write_file",
        "edit_file",
        "confirm_file_operation",
        "cancel_file_operation",
        "rollback_file_operation",
        "run_python",
        "add_task",
        "confirm_capability_operation",
        "cancel_capability_operation",
    }

    # 作用：初始化记忆、能力、文件访问及任务和副作用账本，并注册内置工具。
    # 参数 memory：提供会话记忆、提醒和关系型存储后端的管理器。
    # 参数 permanent_memory：可选的永久记忆管理器，用于跨会话删除和清理。
    # 参数 capabilities：可选的统一能力中心，用于文件、网络、MCP 和外部调用。
    def __init__(
        self,
        memory: MemoryManager,
        permanent_memory: PermanentMemoryManager | None = None,
        capabilities: CapabilityHub | None = None,
    ) -> None:
        self.memory = memory
        self.permanent_memory = permanent_memory
        self.capabilities = capabilities
        self.file_access = (
            AgentFileAccess(capabilities.workspace.root, capabilities.project_root)
            if capabilities is not None
            else None
        )
        self.operations = AgentOperationStore(backend=memory.backend)
        self.effect_receipts = EffectReceiptStore(backend=memory.backend)
        self.external_dispatches = ExternalDispatchStore(backend=memory.backend)
        self.tasks = AgentTaskStore(backend=memory.backend)
        self._external_dispatch_review_lock = threading.RLock()
        self._tools: dict[ToolAction, RegisteredTool] = {}
        self._register_builtin_tools()

    # 作用：按工具名登记或替换一项可执行工具定义。
    # 参数 tool：包含模型 schema、风险和 Python 处理器的工具定义。
    def register(self, tool: RegisteredTool) -> None:
        self._tools[tool.name] = tool

    # 作用：生成不暴露处理器实现的工具名称、描述和风险清单。
    # 参数：无。
    # 返回：所有已注册工具的公开摘要列表。
    def manifest(self) -> list[dict[str, str]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk,
            }
            for tool in self._tools.values()
        ]

    # 作用：筛选模型可调用且角色获准的工具，并转换为 OpenAI Tool Calling schema。
    # 参数 allowed_tools：当前角色允许暴露给模型的工具白名单；空值表示不过滤。
    # 返回：可直接传给模型 API 的 function tool 定义列表。
    def native_tools(
        self,
        allowed_tools: tuple[ToolAction, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Return OpenAI-compatible tool definitions for the model agent loop."""
        definitions: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if not tool.model_callable or (
                allowed_tools is not None and tool.name not in allowed_tools
            ):
                continue
            description = tool.description
            if tool.name == "capability_call" and self.capabilities is not None:
                mcp_hint = self.capabilities.mcp_catalog.model_hint()
                if mcp_hint:
                    description = f"{description}\n{mcp_hint}"
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return definitions

    # 作用：返回 Agent 文件访问边界和授权目录的当前状态。
    # 参数：无。
    def file_access_status(self) -> dict[str, Any]:
        return self.file_access.status() if self.file_access is not None else {}

    # 作用：再次校验角色权限和任务状态，在预算及幂等保护下执行工具并落账步骤。
    # 参数 name：本次请求执行的工具动作名称。
    # 参数 arguments：传给工具处理器的结构化参数。
    # 参数 session_id：工具操作归属的用户会话标识。
    # 参数 allowed_tools：当前角色允许执行的工具白名单；空值表示不过滤。
    # 返回：包含真实结果、错误和审计证据的统一工具回执。
    def execute(
        self,
        name: ToolAction,
        arguments: dict[str, Any],
        *,
        session_id: str,
        allowed_tools: tuple[ToolAction, ...] | None = None,
    ) -> ToolExecutionResult:
        allowed = allowed_tools is None or name in allowed_tools
        audit_arguments = redact_for_audit(arguments)
        task_id = current_agent_task_id()
        audit = {
            "tool": name,
            "allowed": allowed,
            "arguments": audit_arguments,
            "task_id": task_id,
        }
        if task_id and self.tasks.cancel_requested(task_id):
            return ToolExecutionResult(
                action=name,
                success=False,
                error="任务已经停止，未再调用这个工具。",
                audit={**audit, "task_cancelled": True},
            )
        if not allowed:
            return ToolExecutionResult(
                action=name,
                success=False,
                error="当前角色没有权限使用这个工具。",
                audit=audit,
            )
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                action=name,
                success=False,
                error="未知工具。",
                audit=audit,
            )
        idempotency_key = self._idempotency_key(name, arguments)
        reusable = (
            self.tasks.reusable_step(task_id, name, idempotency_key)
            if task_id and idempotency_key
            else None
        )
        if reusable is not None:
            return self._reuse_task_result(name, reusable, audit)
        if task_id:
            try:
                self.tasks.consume_tool_call(task_id, name)
            except AgentTaskBudgetExceeded as exc:
                return ToolExecutionResult(
                    action=name,
                    success=False,
                    error=f"{exc} 可在设置中提高额度后恢复原任务。",
                    audit={
                        **audit,
                        "budget_exceeded": True,
                        "budget": exc.as_dict(),
                    },
                )
        step_id = (
            self.tasks.start_step(
                task_id,
                name,
                audit_arguments,
                idempotency_key=idempotency_key,
            )
            if task_id
            else None
        )
        try:
            idempotency_token = _CURRENT_TOOL_IDEMPOTENCY_KEY.set(idempotency_key)
            try:
                result = tool.handler(arguments, session_id)
            finally:
                _CURRENT_TOOL_IDEMPOTENCY_KEY.reset(idempotency_token)
            result.audit.update(audit)
            if step_id is not None:
                status = (
                    "waiting_confirmation"
                    if result.audit.get("requires_confirmation")
                    else "waiting_background"
                    if result.audit.get("background_job_pending")
                    else "completed"
                    if result.success
                    else "failed"
                )
                self.tasks.finish_step(
                    step_id,
                    status=status,
                    output=redact_for_audit(result.model_dump(mode="json")),
                    error=result.error,
                )
                result.audit["task_step_id"] = step_id
            return result
        except Exception as exc:
            if step_id is not None:
                self.tasks.finish_step(step_id, status="failed", error=str(exc))
            raise

    # 作用：判断当前上下文绑定的 Agent 任务是否已经请求取消。
    # 参数：无。
    def task_cancel_requested(self) -> bool:
        task_id = current_agent_task_id()
        return bool(task_id and self.tasks.cancel_requested(task_id))

    # 作用：启动恢复时依据领域持久证据对账中断步骤，不确定副作用保持未决且不重放。
    # 参数 task_id：可选的目标任务 ID；为空时扫描全部中断任务。
    # 返回：已检查任务、已确认步骤、确定未生效步骤和未决步骤的数量。
    def reconcile_interrupted_effects(self, *, task_id: str = "") -> dict[str, int]:
        """Resolve crash-window steps only when durable domain evidence is decisive."""
        counts = {
            "receipts_interrupted": self.effect_receipts.recover_interrupted(
                task_id=task_id
            ),
            "tasks_checked": 0,
            "steps_reconciled": 0,
            "effects_not_applied": 0,
            "steps_unresolved": 0,
        }
        selected_task = self.tasks.get(task_id) if task_id else None
        tasks = [selected_task] if selected_task is not None else (
            [] if task_id else self.tasks.interrupted_tasks()
        )
        for task in tasks:
            interrupted_steps = [
                step
                for step in task.get("steps", [])
                if isinstance(step, dict) and step.get("status") == "interrupted"
            ]
            if not interrupted_steps:
                continue
            counts["tasks_checked"] += 1
            current_task_id = str(task.get("id", ""))
            session_id = str(task.get("session_id", ""))
            task_operations = self.operations.list_task(current_task_id)
            task_counts = {
                "steps_reconciled": 0,
                "effects_not_applied": 0,
                "steps_unresolved": 0,
            }
            for step in interrupted_steps:
                outcome = self._reconcile_interrupted_step(
                    task_id=current_task_id,
                    session_id=session_id,
                    step=step,
                    task_operations=task_operations,
                )
                if outcome == "reconciled":
                    counts["steps_reconciled"] += 1
                    task_counts["steps_reconciled"] += 1
                elif outcome == "not_applied":
                    counts["effects_not_applied"] += 1
                    task_counts["effects_not_applied"] += 1
                else:
                    counts["steps_unresolved"] += 1
                    task_counts["steps_unresolved"] += 1
            self.tasks.append_event(
                current_task_id,
                "task_reconciliation",
                status=(
                    "interrupted"
                    if task_counts["steps_unresolved"]
                    else "completed"
                ),
                phase="reconciling",
                detail=(
                    "启动对账已完成："
                    f"确认 {task_counts['steps_reconciled']}，"
                    f"未发生 {task_counts['effects_not_applied']}，"
                    f"仍不确定 {task_counts['steps_unresolved']}"
                ),
                payload=task_counts,
            )
        return counts

    # 作用：用记忆、提醒、待办、文件回执或确认单判断单个中断步骤的真实结果。
    # 参数 task_id：中断步骤所属的 Agent 任务 ID。
    # 参数 session_id：中断步骤所属的用户会话 ID。
    # 参数 step：任务账本中待对账的步骤记录。
    # 参数 task_operations：该任务关联的持久确认单列表。
    # 返回：reconciled、not_applied 或 unresolved 对账结论。
    def _reconcile_interrupted_step(
        self,
        *,
        task_id: str,
        session_id: str,
        step: dict[str, Any],
        task_operations: list[dict[str, Any]],
    ) -> str:
        action = str(step.get("action", ""))
        idempotency_key = str(step.get("idempotency_key", ""))
        if not idempotency_key:
            return "unresolved"
        if action == "remember":
            effect_key = self._effect_idempotency_key(task_id, idempotency_key)
            memory = self.memory.memory_by_idempotency(session_id, effect_key)
            if memory is not None:
                result = ToolExecutionResult(
                    action="remember",
                    success=True,
                    content="显式记忆已在中断前持久化，恢复时没有重复写入。",
                    audit={
                        "memory_id": int(memory.get("id", 0)),
                        "reconciled_after_restart": True,
                        "deduplicated": True,
                    },
                )
                return self._commit_reconciled_step(
                    task_id,
                    action,
                    idempotency_key,
                    result,
                )
            return self._commit_not_applied_step(task_id, action, idempotency_key)
        if action == "set_reminder":
            effect_key = self._effect_idempotency_key(task_id, idempotency_key)
            reminder = self.memory.reminder_by_idempotency(
                session_id,
                effect_key,
            )
            if reminder is not None:
                result = ToolExecutionResult(
                    action="set_reminder",
                    success=True,
                    content="提醒已在中断前持久化，恢复时没有重复创建。",
                    audit={
                        "reminder_id": int(reminder.get("id", 0)),
                        "scheduled": reminder.get("due_at") is not None,
                        "due_at": reminder.get("due_at"),
                        "timezone": reminder.get("timezone", ""),
                        "recurrence": reminder.get("recurrence", ""),
                        "reconciled_after_restart": True,
                        "deduplicated": True,
                    },
                )
                return self._commit_reconciled_step(
                    task_id,
                    action,
                    idempotency_key,
                    result,
                )
            return self._commit_not_applied_step(task_id, action, idempotency_key)
        if action == "add_task" and self.capabilities is not None:
            effect_key = self._effect_idempotency_key(task_id, idempotency_key)
            item = self.capabilities.organizer_effect_by_idempotency(effect_key)
            if item is not None:
                result = ToolExecutionResult(
                    action="add_task",
                    success=True,
                    content="待办已在中断前持久化，恢复时没有重复创建。",
                    audit={
                        "item_id": str(item.get("id", "")),
                        "reconciled_after_restart": True,
                        "deduplicated": True,
                    },
                )
                return self._commit_reconciled_step(
                    task_id,
                    action,
                    idempotency_key,
                    result,
                )
            return self._commit_not_applied_step(task_id, action, idempotency_key)
        if action == "write_file":
            receipt = self.effect_receipts.get(
                task_id=task_id,
                action=action,
                idempotency_key=idempotency_key,
            )
            if receipt:
                evidence = (
                    receipt.get("evidence")
                    if isinstance(receipt.get("evidence"), dict)
                    else {}
                )
                try:
                    target = self._require_file_access().resolve(
                        str(evidence.get("path", "")),
                        write=True,
                    )
                except (AgentFileError, OSError):
                    return "unresolved"
                expected_sha256 = str(evidence.get("expected_sha256", ""))
                if target.exists() and expected_sha256:
                    result = self._reconcile_file_receipt(
                        receipt,
                        target=target,
                        expected_sha256=expected_sha256,
                    )
                    if result is not None and result.success:
                        return self._commit_reconciled_step(
                            task_id,
                            action,
                            idempotency_key,
                            result,
                        )
                    return "unresolved"
                if not target.exists() and receipt.get("status") in {
                    "completed",
                    "conflict",
                    "external_state_changed",
                }:
                    self.effect_receipts.finish(
                        task_id=task_id,
                        action=action,
                        idempotency_key=idempotency_key,
                        status="external_state_changed",
                        error=(
                            "副作用回执显示文件创建已完成，但恢复时目标已不存在；"
                            "外部状态可能已变化。"
                        ),
                    )
                    return "unresolved"
                if not target.exists():
                    self.effect_receipts.finish(
                        task_id=task_id,
                        action=action,
                        idempotency_key=idempotency_key,
                        status="not_applied",
                        error="目标文件不存在，证明写入未发生。",
                    )
                    return self._commit_not_applied_step(
                        task_id,
                        action,
                        idempotency_key,
                    )
        pending = self._pending_operation_for_step(action, task_operations)
        if pending is not None:
            result = ToolExecutionResult(
                action=action,  # type: ignore[arg-type]
                success=False,
                error=str(pending.get("summary", "操作仍在等待确认。")),
                audit={
                    "requires_confirmation": True,
                    "operation_id": str(pending.get("id", "")),
                    "reconciled_after_restart": True,
                    "deduplicated": True,
                },
            )
            self.tasks.reconcile_step(
                task_id,
                action,
                idempotency_key,
                status="waiting_confirmation",
                output=redact_for_audit(result.model_dump(mode="json")),
                error=result.error,
            )
            return "reconciled"
        if action in {
            "confirm_file_operation",
            "confirm_capability_operation",
            "cancel_file_operation",
            "cancel_capability_operation",
            "rollback_file_operation",
        }:
            arguments = step.get("input") if isinstance(step.get("input"), dict) else {}
            operation = self.operations.get(str(arguments.get("operation_id", "")))
            if operation is not None and operation.get("status") in {
                "completed",
                "cancelled",
            }:
                succeeded = operation.get("status") == "completed" or action.startswith(
                    "cancel_"
                )
                result = ToolExecutionResult(
                    action=action,  # type: ignore[arg-type]
                    success=succeeded,
                    content="操作终态已从持久确认账本恢复。" if succeeded else "",
                    error="操作没有完成。" if not succeeded else "",
                    audit={
                        "operation_id": str(operation.get("id", "")),
                        "operation_status": str(operation.get("status", "")),
                        "reconciled_after_restart": True,
                        "deduplicated": True,
                    },
                )
                return self._commit_reconciled_step(
                    task_id,
                    action,
                    idempotency_key,
                    result,
                )
        return "unresolved"

    # 作用：把已由持久证据确认的工具结果提交回中断步骤。
    # 参数 task_id：待更新步骤所属的任务 ID。
    # 参数 action：原工具步骤的动作名称。
    # 参数 idempotency_key：定位原步骤和副作用的幂等键。
    # 参数 result：从领域证据恢复出的工具执行结果。
    # 返回：步骤是否成功完成对账的状态字符串。
    def _commit_reconciled_step(
        self,
        task_id: str,
        action: str,
        idempotency_key: str,
        result: ToolExecutionResult,
    ) -> str:
        changed = self.tasks.reconcile_step(
            task_id,
            action,
            idempotency_key,
            status="completed" if result.success else "failed",
            output=redact_for_audit(result.model_dump(mode="json")),
            error=result.error,
        )
        return "reconciled" if changed else "unresolved"

    # 作用：把有证据证明未产生副作用的中断步骤关闭为可安全重试的失败。
    # 参数 task_id：待更新步骤所属的任务 ID。
    # 参数 action：原工具步骤的动作名称。
    # 参数 idempotency_key：定位原步骤的幂等键。
    # 返回：not_applied 或并发更新导致的 unresolved。
    def _commit_not_applied_step(
        self,
        task_id: str,
        action: str,
        idempotency_key: str,
    ) -> str:
        result = ToolExecutionResult(
            action=action,  # type: ignore[arg-type]
            success=False,
            error="服务停止前没有留下业务结果；该步骤可以在恢复时安全重试。",
            audit={
                "effect_not_applied": True,
                "reconciled_after_restart": True,
            },
        )
        changed = self.tasks.reconcile_step(
            task_id,
            action,
            idempotency_key,
            status="failed",
            output=redact_for_audit(result.model_dump(mode="json")),
            error=result.error,
        )
        return "not_applied" if changed else "unresolved"

    # 作用：为中断步骤查找仍在等待用户确认的同类持久操作。
    # 参数 action：中断工具步骤的动作名称。
    # 参数 operations：任务关联的确认单记录列表。
    # 返回：匹配的待确认操作；找不到时返回空值。
    @staticmethod
    # 作用：执行“pending_operation_for_step”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 action：调用方传入的action，用于本次处理。
    # 参数 operations：调用方传入的operations，用于本次处理。
    def _pending_operation_for_step(
        action: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for operation in operations:
            if operation.get("status") != "pending":
                continue
            if action == "capability_call" and operation.get("kind") == "capability":
                return operation
            if (
                action in {"write_file", "edit_file"}
                and operation.get("kind") == "file"
                and operation.get("action") == action
            ):
                return operation
        return None

    # 作用：为需要防重的工具参数生成稳定指纹，并规范提醒时间表达后再计算。
    # 参数 name：工具动作名称。
    # 参数 arguments：参与操作指纹计算的原始工具参数。
    # 返回：需要幂等保护时返回 SHA-256；只读或无需防重时返回空串。
    def _idempotency_key(self, name: ToolAction, arguments: dict[str, Any]) -> str:
        should_deduplicate = name in self.TASK_DEDUPLICATED_ACTIONS
        if name == "capability_call":
            capability = str(arguments.get("capability", "")).strip()
            action = str(arguments.get("action", "")).strip()
            payload = arguments.get("arguments", {})
            payload = dict(payload) if isinstance(payload, dict) else {}
            should_deduplicate = self._capability_needs_confirmation(
                capability,
                action,
                payload,
            )
        if not should_deduplicate:
            return ""
        fingerprint_arguments: dict[str, Any] = arguments
        if name == "set_reminder":
            content = str(arguments.get("content", "")).strip()
            when = str(arguments.get("when", "")).strip()
            raw = f"{when} {content}".strip() if when else content
            timezone = os.getenv(
                "YUKINO_TIMEZONE",
                ReminderScheduleParser.DEFAULT_TIMEZONE,
            )
            parsed = ReminderScheduleParser(timezone).parse(raw)
            fingerprint_arguments = {
                "content": parsed.content,
                "schedule_expression": re.sub(r"\s+", "", parsed.expression),
                "recurrence": parsed.recurrence,
                "timezone": parsed.timezone,
            }
        encoded = json.dumps(
            {"action": name, "arguments": fingerprint_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # 作用：将工具参数指纹限定在单个任务内，生成领域写入使用的幂等键。
    # 参数 task_id：当前 Agent 任务 ID。
    # 参数 idempotency_key：工具参数的稳定指纹。
    # 返回：任务级 SHA-256 幂等键；信息不全时返回空串。
    @staticmethod
    # 作用：执行“effect_idempotency_key”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 task_id：持久任务的稳定标识。
    # 参数 idempotency_key：调用方传入的idempotency_key，用于本次处理。
    def _effect_idempotency_key(task_id: str, idempotency_key: str) -> str:
        """Scope a domain write key to one task instead of all user turns."""
        if not task_id or not idempotency_key:
            return ""
        raw = f"{task_id}:{idempotency_key}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # 作用：将同任务已完成或待确认步骤还原为工具回执，避免再次产生副作用。
    # 参数 name：当前请求的工具动作名称。
    # 参数 step：由幂等键命中的历史任务步骤。
    # 参数 audit：本次调用应附加的权限和任务审计字段。
    # 返回：标记 deduplicated 的复用结果或原待确认状态。
    @staticmethod
    # 作用：执行“reuse_task_result”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 name：调用方传入的name，用于本次处理。
    # 参数 step：调用方传入的step，用于本次处理。
    # 参数 audit：调用方传入的audit，用于本次处理。
    def _reuse_task_result(
        name: ToolAction,
        step: dict[str, Any],
        audit: dict[str, Any],
    ) -> ToolExecutionResult:
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        previous_audit = (
            dict(output.get("audit", {}))
            if isinstance(output.get("audit"), dict)
            else {}
        )
        waiting = bool(previous_audit.get("requires_confirmation")) and step.get(
            "status"
        ) == "waiting_confirmation"
        reused_audit = {
            **previous_audit,
            **audit,
            "deduplicated": True,
            "original_task_step_id": step.get("id"),
        }
        if waiting:
            return ToolExecutionResult(
                action=name,
                success=False,
                error=str(
                    step.get("error")
                    or output.get("error")
                    or "相同操作已经在等待确认，未重复创建确认单。"
                ),
                audit=reused_audit,
            )
        return ToolExecutionResult(
            action=name,
            success=True,
            content="同一任务内的相同操作已经完成，已复用原结果而没有重复执行。",
            audit=reused_audit,
        )

    # 作用：注册时间、记忆、提醒及能力中心映射出的基础工具。
    # 参数：无。
    def _register_builtin_tools(self) -> None:
        self.register(
            RegisteredTool(
                name="get_time",
                description="获取当前日期和时间。",
                handler=self._get_time,
            )
        )
        if self.capabilities is not None:
            capability_tools = (
                ("search_capabilities", "按用户意图查找 AGI Yukino 可以使用的能力。", "tool_search", "search", "low", self._string_parameters("query", "要查找的能力")),
                ("run_python", "在 AST 审查后的隔离环境运行短 Python；只在用户要求执行时使用。", "python", "run", "medium", self._string_parameters("code", "要运行的 Python 代码")),
                ("web_search", "搜索公开网页。", "web", "search", "medium", self._string_parameters("query", "搜索词")),
                ("web_fetch", "读取经过 SSRF 防护的公开网页。", "web", "fetch", "medium", self._string_parameters("url", "HTTP 或 HTTPS 网页地址")),
                ("read_document", "读取内部工作区中的受支持文档。", "documents", "read", "low", self._string_parameters("path", "文档路径")),
                ("system_status", "读取本机资源状态。", "system", "snapshot", "low", self._empty_parameters()),
                ("add_task", "添加一条侍奉部待办。", "organizer", "add", "low", self._string_parameters("content", "待办内容")),
                ("list_tasks", "列出侍奉部待办。", "organizer", "list", "low", self._empty_parameters()),
            )
            for name, description, capability, action, risk, parameters in capability_tools:
                self.register(
                    RegisteredTool(
                        name=name,  # type: ignore[arg-type]
                        description=description,
                        handler=self._capability_handler(capability, action),
                        risk=risk,
                        parameters=parameters,
                    )
                )
            self._register_agent_file_tools()
            self._register_capability_gateway()
        self.register(
            RegisteredTool(
                name="remember",
                description="把用户明确要求记住的信息写入当前会话记忆。",
                handler=self._remember,
                parameters=self._string_parameters("content", "需要记住的信息"),
            )
        )
        self.register(
            RegisteredTool(
                name="recall",
                description="根据关键词召回当前会话记忆。",
                handler=self._recall,
                parameters=self._string_parameters("query", "记忆关键词"),
            )
        )
        self.register(
            RegisteredTool(
                name="forget",
                description="按关键词删除当前会话中的匹配显式记忆。",
                handler=self._forget,
                risk="medium",
                parameters=self._string_parameters("query", "要忘记的关键词"),
                model_callable=False,
            )
        )
        self.register(
            RegisteredTool(
                name="clear_memory",
                description="清空当前会话的显式记忆。",
                handler=self._clear_memory,
                risk="medium",
                model_callable=False,
            )
        )
        self.register(
            RegisteredTool(
                name="set_reminder",
                description="为当前会话创建持久提醒；when 可使用明天下午三点、30 分钟后或 ISO 时间。",
                handler=self._set_reminder,
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "提醒事项"},
                        "when": {"type": "string", "description": "提醒时间；不确定时留空"},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
            )
        )
        self.register(
            RegisteredTool(
                name="list_reminders",
                description="列出当前会话的提醒。",
                handler=self._list_reminders,
                parameters=self._empty_parameters(),
            )
        )
        self.register(
            RegisteredTool(
                name="cancel_reminder",
                description="按提醒编号取消一个尚未送达的提醒。",
                handler=self._cancel_reminder,
                risk="medium",
                parameters={
                    "type": "object",
                    "properties": {"reminder_id": {"type": "integer", "minimum": 1}},
                    "required": ["reminder_id"],
                    "additionalProperties": False,
                },
            )
        )

    # 作用：构造不接收业务字段的严格 JSON Object 工具参数 schema。
    # 参数：无。
    @staticmethod
    # 作用：执行“empty_parameters”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _empty_parameters() -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    # 作用：构造只接收一个必填字符串字段的严格工具参数 schema。
    # 参数 name：工具参数字段名。
    # 参数 description：该字段展示给模型的用途说明。
    @staticmethod
    # 作用：执行“string_parameters”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 name：调用方传入的name，用于本次处理。
    # 参数 description：调用方传入的description，用于本次处理。
    def _string_parameters(name: str, description: str) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {name: {"type": "string", "description": description}},
            "required": [name],
            "additionalProperties": False,
        }

    # 作用：注册受目录授权、确认保护和内容校验约束的文件工具。
    # 参数：无。
    def _register_agent_file_tools(self) -> None:
        path_parameter = {"type": "string", "description": "相对内部工作区或已授权目录中的绝对路径"}
        self.register(
            RegisteredTool(
                name="list_files",
                description="列出项目、内部工作区或用户已授权目录中的文件。",
                handler=self._list_files,
                parameters={"type": "object", "properties": {"path": path_parameter}, "additionalProperties": False},
            )
        )
        self.register(
            RegisteredTool(
                name="read_file",
                description="读取项目、内部工作区或用户已授权目录中的 UTF-8 文本文件。",
                handler=self._read_file,
                parameters={"type": "object", "properties": {"path": path_parameter}, "required": ["path"], "additionalProperties": False},
            )
        )
        self.register(
            RegisteredTool(
                name="search_files",
                description="在项目、内部工作区或用户已授权目录中搜索文本。",
                handler=self._search_files,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词"},
                        "path": path_parameter,
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        )
        self.register(
            RegisteredTool(
                name="analyze_code",
                description="对用户提供的代码做语言识别、语法检查和安全风险初检，再据此完成语义分析。",
                handler=self._analyze_code,
                parameters=self._string_parameters("code", "需要分析的完整代码"),
            )
        )
        self.register(
            RegisteredTool(
                name="analyze_file",
                description="读取已授权 UTF-8 代码文件并做语言识别、语法检查和安全风险初检。",
                handler=self._analyze_file,
                parameters={
                    "type": "object",
                    "properties": {"path": path_parameter},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
        )
        self.register(
            RegisteredTool(
                name="write_file",
                description="创建 UTF-8 文本文件。只能写入设置页已授权目录；若文件已存在会等待用户确认，绝不会自行覆盖。",
                handler=self._write_file,
                risk="medium",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": path_parameter,
                        "content": {"type": "string", "description": "文件的完整文本内容"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            )
        )
        self.register(
            RegisteredTool(
                name="edit_file",
                description="提出对已有 UTF-8 文本文件的精确替换。修改会先进入待确认状态，用户确认后才落盘。",
                handler=self._edit_file,
                risk="medium",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": path_parameter,
                        "old_text": {"type": "string", "description": "文件中要被替换的原文，必须精确匹配"},
                        "new_text": {"type": "string", "description": "替换后的文本"},
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            )
        )
        self.register(
            RegisteredTool(
                name="confirm_file_operation",
                description="确认上一条等待确认的覆盖或文件修改操作。",
                handler=self._confirm_file_operation,
                risk="medium",
                parameters={"type": "object", "properties": {"operation_id": {"type": "string"}}, "additionalProperties": False},
                model_callable=False,
            )
        )
        self.register(
            RegisteredTool(
                name="rollback_file_operation",
                description="回滚一次已经确认并完成的文件覆盖操作。",
                handler=self._rollback_file_operation,
                risk="medium",
                parameters={"type": "object", "properties": {"operation_id": {"type": "string"}}, "additionalProperties": False},
                model_callable=False,
            )
        )

    # 作用：注册统一能力网关以及外部操作确认、取消和文件确认工具。
    # 参数：无。
    def _register_capability_gateway(self) -> None:
        available = {
            item.id: list(item.operations)
            for item in CAPABILITIES
            if item.id not in {"companion", "memory", "retrieval", "channels", "usage"}
        }
        self.register(
            RegisteredTool(
                name="capability_call",
                description=(
                    "调用设置页中的统一能力中心，包括 MCP、邮件、媒体、插件、工作流、知识图谱和硬件状态。"
                    "先用 search_capabilities 查询能力和操作。产生外部副作用的操作只会创建待确认项。"
                ),
                handler=self._capability_call,
                risk="medium",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability": {
                            "type": "string",
                            "enum": sorted(available),
                            "description": "能力 id",
                        },
                        "action": {"type": "string", "description": "能力操作名"},
                        "arguments": {
                            "type": "object",
                            "description": "传给能力的参数",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["capability", "action"],
                    "additionalProperties": False,
                },
            )
        )
        self.register(
            RegisteredTool(
                name="confirm_capability_operation",
                description="确认上一条等待确认的外部能力操作。",
                handler=self._confirm_capability_operation,
                risk="high",
                parameters={
                    "type": "object",
                    "properties": {"operation_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                model_callable=False,
            )
        )
        self.register(
            RegisteredTool(
                name="cancel_capability_operation",
                description="取消上一条等待确认的外部能力操作。",
                handler=self._cancel_capability_operation,
                parameters={
                    "type": "object",
                    "properties": {"operation_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                model_callable=False,
            )
        )
        self.register(
            RegisteredTool(
                name="cancel_file_operation",
                description="取消上一条等待确认的文件操作。",
                handler=self._cancel_file_operation,
                parameters={"type": "object", "properties": {"operation_id": {"type": "string"}}, "additionalProperties": False},
                model_callable=False,
            )
        )

    # 作用：统一执行只读文件回调，并把正常值或受控异常转换为工具回执。
    # 参数 action：回执中记录的文件工具动作。
    # 参数 callback：实际读取、搜索或分析文件的无参函数。
    def _file_result(self, action: ToolAction, callback: Callable[[], Any]) -> ToolExecutionResult:
        try:
            value = callback()
            return ToolExecutionResult(
                action=action,
                success=True,
                content=json.dumps(value, ensure_ascii=False, indent=2)[:24_000],
                audit={"file_access": True},
            )
        except (AgentFileError, OSError, ValueError) as exc:
            return ToolExecutionResult(action=action, success=False, error=str(exc), audit={"file_access": True})

    # 作用：列出授权路径下的文件并返回统一文件访问回执。
    # 参数 arguments：包含可选目标 path 的工具参数。
    # 参数 session_id：当前会话 ID；该只读适配器不直接使用。
    def _list_files(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        return self._file_result("list_files", lambda: self._require_file_access().list(str(arguments.get("path", "."))))

    # 作用：读取授权范围内的 UTF-8 文件内容。
    # 参数 arguments：包含必填目标 path 的工具参数。
    # 参数 session_id：当前会话 ID；该只读适配器不直接使用。
    def _read_file(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        return self._file_result("read_file", lambda: self._require_file_access().read(str(arguments.get("path", ""))))

    # 作用：在授权路径内搜索指定文本。
    # 参数 arguments：包含搜索词 query 和可选 path 的工具参数。
    # 参数 session_id：当前会话 ID；该只读适配器不直接使用。
    def _search_files(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        return self._file_result(
            "search_files",
            lambda: self._require_file_access().search(str(arguments.get("query", "")), str(arguments.get("path", "."))),
        )

    # 作用：对用户直接提交的代码做语言、语法和风险检查。
    # 参数 arguments：包含完整代码字符串 code 的工具参数。
    # 参数 session_id：当前会话 ID；该纯分析适配器不直接使用。
    def _analyze_code(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        return self._file_result("analyze_code", lambda: inspect_code(str(arguments.get("code", ""))))

    # 作用：读取授权代码文件并返回文件元数据和静态检查结果。
    # 参数 arguments：包含待分析文件 path 的工具参数。
    # 参数 session_id：当前会话 ID；文件权限由目录授权边界控制。
    def _analyze_file(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        # 作用：封装本次文件读取和代码检查，交给统一错误转换器执行。
        # 参数：无。
        def analyze() -> dict[str, Any]:
            source = self._require_file_access().read(str(arguments.get("path", "")))
            return {
                "path": source["path"],
                "size": source["size"],
                "analysis": inspect_code(str(source["content"])),
            }

        return self._file_result("analyze_file", analyze)

    # 作用：安全创建文本文件；已有文件转入确认，崩溃后用回执和 SHA-256 对账。
    # 参数 arguments：包含目标 path 和完整文本 content 的工具参数。
    # 参数 session_id：文件操作及确认单归属的用户会话 ID。
    # 返回：已验证写入、等待确认、冲突或失败的工具回执。
    def _write_file(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        access = self._require_file_access()
        path = str(arguments.get("path", "")).strip()
        content = str(arguments.get("content", ""))
        task_id = current_agent_task_id()
        idempotency_key = _CURRENT_TOOL_IDEMPOTENCY_KEY.get()
        expected_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            target = access.resolve(path, write=True)
            receipt = self.effect_receipts.get(
                task_id=task_id,
                action="write_file",
                idempotency_key=idempotency_key,
            )
            if receipt:
                reconciled = self._reconcile_file_receipt(
                    receipt,
                    target=target,
                    expected_sha256=expected_sha256,
                )
                if reconciled is not None:
                    return reconciled
            if target.exists():
                previous = access.fingerprint(str(target))
                return self._queue_file_operation(
                    session_id,
                    action="write_file",
                    path=str(target),
                    content=content,
                    expected_previous_sha256=str(previous["sha256"]),
                    message=f"文件已存在：{target}。需要你确认后才能覆盖。",
                )
            receipt = self.effect_receipts.begin(
                task_id=task_id,
                session_id=session_id,
                action="write_file",
                idempotency_key=idempotency_key,
                evidence={
                    "path": str(target),
                    "expected_sha256": expected_sha256,
                },
            )
            result = access.write(path, content)
            if receipt:
                self.effect_receipts.finish(
                    task_id=task_id,
                    action="write_file",
                    idempotency_key=idempotency_key,
                    status="completed",
                    result={
                        "path": result["path"],
                        "sha256": result["sha256"],
                        "verified": result["verified"],
                    },
                )
            return ToolExecutionResult(
                action="write_file",
                success=True,
                content=f"文件已创建：{result['path']}（{result['size']} 字节）",
                audit={
                    "file_access": True,
                    "path": result["path"],
                    "created": True,
                    "verified": result["verified"],
                    "sha256": result["sha256"],
                    "effect_receipt_id": receipt.get("id", 0) if receipt else 0,
                },
            )
        except (AgentFileError, OSError) as exc:
            if task_id and idempotency_key:
                self.effect_receipts.finish(
                    task_id=task_id,
                    action="write_file",
                    idempotency_key=idempotency_key,
                    status="interrupted",
                    error=str(exc),
                )
            return ToolExecutionResult(action="write_file", success=False, error=str(exc), audit={"file_access": True})

    # 作用：比较目标文件与副作用回执中的预期摘要，决定复用、冲突或允许重试。
    # 参数 receipt：写文件前后持久化的结构化副作用回执。
    # 参数 target：经过授权边界解析的真实目标路径。
    # 参数 expected_sha256：本次委托期望文件内容的 SHA-256。
    # 返回：可确定时返回成功或安全失败回执；确认未执行时返回空值以继续写入。
    def _reconcile_file_receipt(
        self,
        receipt: dict[str, Any],
        *,
        target: Path,
        expected_sha256: str,
    ) -> ToolExecutionResult | None:
        task_id = str(receipt.get("task_id", ""))
        idempotency_key = str(receipt.get("idempotency_key", ""))
        status = str(receipt.get("status", ""))
        if target.exists():
            actual_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual_sha256 == expected_sha256:
                self.effect_receipts.finish(
                    task_id=task_id,
                    action="write_file",
                    idempotency_key=idempotency_key,
                    status="completed",
                    result={
                        "path": str(target),
                        "sha256": actual_sha256,
                        "verified": True,
                        "reconciled": True,
                    },
                )
                return ToolExecutionResult(
                    action="write_file",
                    success=True,
                    content=f"文件已存在并与崩溃前预期一致：{target}",
                    audit={
                        "file_access": True,
                        "path": str(target),
                        "created": True,
                        "verified": True,
                        "sha256": actual_sha256,
                        "reconciled_after_restart": True,
                        "deduplicated": True,
                        "effect_receipt_id": int(receipt.get("id", 0) or 0),
                    },
                )
            self.effect_receipts.finish(
                task_id=task_id,
                action="write_file",
                idempotency_key=idempotency_key,
                status="conflict",
                error="目标文件存在，但内容指纹与崩溃前预期不一致。",
            )
            return ToolExecutionResult(
                action="write_file",
                success=False,
                error=(
                    "文件在中断后存在，但内容与原委托不一致；为避免覆盖外部修改，"
                    "Agent 没有自动重放。"
                ),
                audit={
                    "file_access": True,
                    "path": str(target),
                    "uncertain_side_effect": True,
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                    "effect_receipt_id": int(receipt.get("id", 0) or 0),
                },
            )
        if status in {"completed", "conflict", "external_state_changed"}:
            receipt_status = (
                "external_state_changed" if status == "completed" else status
            )
            self.effect_receipts.finish(
                task_id=task_id,
                action="write_file",
                idempotency_key=idempotency_key,
                status=receipt_status,
                error="有完成或冲突证据的文件已不存在，外部状态发生了变化。",
            )
            return ToolExecutionResult(
                action="write_file",
                success=False,
                error="已完成的文件在恢复前被移除，Agent 不会静默重新创建。",
                audit={
                    "file_access": True,
                    "path": str(target),
                    "external_state_changed": True,
                    "receipt_status": receipt_status,
                    "effect_receipt_id": int(receipt.get("id", 0) or 0),
                },
            )
        self.effect_receipts.restart(
            task_id=task_id,
            action="write_file",
            idempotency_key=idempotency_key,
        )
        return None

    # 作用：校验精确单次文本替换并创建待确认文件操作，不直接修改磁盘。
    # 参数 arguments：包含 path、唯一匹配 old_text 和替换 new_text 的参数。
    # 参数 session_id：待确认文件操作归属的用户会话 ID。
    def _edit_file(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        access = self._require_file_access()
        path = str(arguments.get("path", "")).strip()
        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))
        try:
            current = access.read(path)
            if not old_text:
                raise AgentFileError("old_text 不能为空。")
            occurrences = str(current["content"]).count(old_text)
            if occurrences != 1:
                raise AgentFileError(f"要替换的原文必须精确出现一次，当前找到 {occurrences} 次。")
            updated = str(current["content"]).replace(old_text, new_text, 1)
            previous = access.fingerprint(str(current["path"]))
            return self._queue_file_operation(
                session_id,
                action="edit_file",
                path=str(current["path"]),
                content=updated,
                expected_previous_sha256=str(previous["sha256"]),
                message=f"已准备修改：{current['path']}。需要你确认后才会写入。",
            )
        except (AgentFileError, OSError) as exc:
            return ToolExecutionResult(action="edit_file", success=False, error=str(exc), audit={"file_access": True})

    # 作用：把覆盖或编辑内容写入持久确认单，并返回需要用户确认的工具状态。
    # 参数 session_id：确认单归属的用户会话 ID。
    # 参数 action：原始文件动作名称。
    # 参数 path：确认后将被写入的真实文件路径。
    # 参数 content：确认后应落盘的完整文本内容。
    # 参数 expected_previous_sha256：确认时必须仍匹配的旧文件版本摘要。
    # 参数 message：展示给用户的确认原因和操作摘要。
    def _queue_file_operation(
        self,
        session_id: str,
        *,
        action: ToolAction,
        path: str,
        content: str,
        expected_previous_sha256: str,
        message: str,
    ) -> ToolExecutionResult:
        operation = self.operations.queue(
            session_id=session_id,
            task_id=current_agent_task_id(),
            kind="file",
            action=action,
            payload={
                "path": path,
                "content": content,
                "expected_previous_sha256": expected_previous_sha256,
            },
            summary=message,
        )
        operation_id = str(operation["id"])
        return ToolExecutionResult(
            action=action,
            success=False,
            error=message,
            audit={
                "file_access": True,
                "requires_confirmation": True,
                "operation_id": operation_id,
                "path": path,
                "precondition_sha256": expected_previous_sha256,
            },
        )

    # 作用：原子领取用户指定的确认单，并按实际类型执行文件或能力操作。
    # 参数 arguments：包含待确认 operation_id 的工具参数。
    # 参数 session_id：用于校验确认单归属的用户会话 ID。
    def _confirm_file_operation(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        operation = self.operations.claim(session_id, str(arguments.get("operation_id", "")))
        if operation is None:
            return ToolExecutionResult(action="confirm_file_operation", success=False, error="没有找到等待确认的文件操作。")
        if operation["kind"] != "file":
            return self._execute_claimed_capability(operation, "confirm_file_operation")
        return self._execute_claimed_file_operation(operation, "confirm_file_operation")

    # 作用：取消当前会话的待确认文件操作，并同步关闭来源任务。
    # 参数 arguments：包含待取消 operation_id 的工具参数。
    # 参数 session_id：用于校验确认单归属的用户会话 ID。
    def _cancel_file_operation(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        operation = self.operations.cancel(session_id, str(arguments.get("operation_id", "")))
        if operation is not None:
            self._finish_source_task(operation, status="cancelled", phase="cancelled")
        return ToolExecutionResult(
            action="cancel_file_operation",
            success=operation is not None,
            content="已取消文件操作。" if operation is not None else "",
            error="没有找到等待取消的文件操作。" if operation is None else "",
        )

    # 作用：在旧文件指纹仍一致时执行已领取的写入，校验结果并生成可回滚确认单。
    # 参数 operation：已由当前请求原子领取的文件确认单。
    # 参数 result_action：最终工具回执应使用的确认动作名称。
    def _execute_claimed_file_operation(
        self,
        operation: dict[str, Any],
        result_action: ToolAction,
    ) -> ToolExecutionResult:
        try:
            payload = operation["payload"]
            expected_previous_sha256 = str(
                payload.get("expected_previous_sha256", "")
            )
            if not expected_previous_sha256:
                raise AgentFileError("确认单缺少目标文件版本指纹，请重新发起修改。")
            current_fingerprint = self._require_file_access().fingerprint(
                str(payload["path"])
            )
            if current_fingerprint["sha256"] != expected_previous_sha256:
                raise AgentFileError(
                    "文件在确认前已经发生变化；为避免覆盖新内容，本次操作已拒绝，"
                    "请重新读取后发起修改。"
                )
            previous = self._require_file_access().read(str(payload["path"]))
            result = self._require_file_access().write(
                str(payload["path"]),
                str(payload["content"]),
                overwrite=True,
            )
            rollback = self.operations.queue(
                session_id=str(operation["session_id"]),
                task_id="",
                kind="file_rollback",
                action="rollback_file_operation",
                payload={
                    "path": result["path"],
                    "content": previous["content"],
                    "expected_sha256": result["sha256"],
                },
                summary=f"把 {result['path']} 恢复到本次操作之前的内容。",
                ttl_seconds=24 * 60 * 60,
            )
            self.operations.complete(str(operation["id"]), result)
            tool_result = ToolExecutionResult(
                action=result_action,
                success=True,
                content=f"文件操作已完成并校验：{result['path']}（{result['size']} 字节）",
                audit={
                    "file_access": True,
                    "path": result["path"],
                    "confirmed": True,
                    "source_action": operation["action"],
                    "operation_id": operation["id"],
                    "rollback_operation_id": rollback["id"],
                    "verified": result["verified"],
                    "sha256": result["sha256"],
                    "precondition_sha256": expected_previous_sha256,
                },
            )
            self._finish_source_task(
                operation,
                status="completed",
                phase="verified",
                result=tool_result,
            )
            return tool_result
        except (AgentFileError, OSError, KeyError) as exc:
            self.operations.fail(str(operation["id"]), str(exc))
            tool_result = ToolExecutionResult(
                action=result_action,
                success=False,
                error=str(exc),
                audit={"file_access": True, "operation_id": operation["id"]},
            )
            self._finish_source_task(
                operation,
                status="failed",
                phase="verification_failed",
                error=str(exc),
                result=tool_result,
            )
            return tool_result

    # 作用：仅在当前文件仍匹配操作后摘要时执行已确认回滚，避免覆盖后续修改。
    # 参数 arguments：包含回滚确认单 operation_id 的工具参数。
    # 参数 session_id：用于领取并校验回滚单归属的会话 ID。
    def _rollback_file_operation(
        self,
        arguments: dict[str, Any],
        session_id: str,
    ) -> ToolExecutionResult:
        operation = self.operations.claim(session_id, str(arguments.get("operation_id", "")))
        if operation is None or operation.get("kind") != "file_rollback":
            if operation is not None:
                self.operations.fail(str(operation["id"]), "待回滚操作类型不匹配。")
            return ToolExecutionResult(
                action="rollback_file_operation",
                success=False,
                error="没有找到可回滚的文件操作，或回滚期限已经过期。",
            )
        try:
            payload = operation["payload"]
            current = self._require_file_access().read(str(payload["path"]))
            current_digest = hashlib.sha256(str(current["content"]).encode("utf-8")).hexdigest()
            if current_digest != str(payload["expected_sha256"]):
                raise AgentFileError("文件在操作后又发生了变化，为避免覆盖新内容，已拒绝回滚。")
            result = self._require_file_access().write(
                str(payload["path"]),
                str(payload["content"]),
                overwrite=True,
            )
            self.operations.complete(str(operation["id"]), result)
            return ToolExecutionResult(
                action="rollback_file_operation",
                success=True,
                content=f"文件已恢复并校验：{result['path']}",
                audit={
                    "file_access": True,
                    "path": result["path"],
                    "operation_id": operation["id"],
                    "verified": result["verified"],
                    "sha256": result["sha256"],
                },
            )
        except (AgentFileError, OSError, KeyError) as exc:
            self.operations.fail(str(operation["id"]), str(exc))
            return ToolExecutionResult(
                action="rollback_file_operation",
                success=False,
                error=str(exc),
                audit={"file_access": True, "operation_id": operation["id"]},
            )

    # 作用：校验能力、操作和权限；只读请求直接执行，副作用请求只创建绑定参数的确认单。
    # 参数 arguments：包含 capability、action 和下游 arguments 的统一网关参数。
    # 参数 session_id：权限策略和确认单归属的用户会话 ID。
    def _capability_call(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        if self.capabilities is None:
            return ToolExecutionResult(action="capability_call", success=False, error="能力中心尚未初始化。")
        capability = str(arguments.get("capability", "")).strip()
        action = str(arguments.get("action", "")).strip()
        payload = arguments.get("arguments", {})
        payload = dict(payload) if isinstance(payload, dict) else {}
        definitions = {item.id: set(item.operations) for item in CAPABILITIES}
        if capability not in definitions or action not in definitions[capability]:
            return ToolExecutionResult(action="capability_call", success=False, error="能力或操作不存在。")
        if capability in {"companion", "memory", "retrieval", "channels", "usage"}:
            return ToolExecutionResult(
                action="capability_call",
                success=False,
                error="这个能力不通过通用网关调用，请使用对应的原生聊天工具。",
            )
        if capability == "knowledge_graph":
            payload.setdefault("session_id", session_id)
        try:
            self.capabilities.check_permission(
                capability,
                action,
                session_id=session_id,
                consume=False,
            )
        except CapabilityPermissionDenied as exc:
            return ToolExecutionResult(
                action="capability_call",
                success=False,
                error=str(exc),
                audit={
                    "permission_denied": True,
                    "capability": capability,
                    "operation": action,
                },
            )
        if self._capability_needs_confirmation(capability, action, payload):
            try:
                confirmation = self.capabilities.confirmation_binding(
                    capability,
                    action,
                    payload,
                )
            except (ValueError, OSError) as exc:
                return ToolExecutionResult(
                    action="capability_call",
                    success=False,
                    error=str(exc),
                )
            payload["_agent_confirmation"] = {
                "version": confirmation["version"],
                "fingerprint": confirmation["fingerprint"],
            }
            operation = self.operations.queue(
                session_id=session_id,
                task_id=current_agent_task_id(),
                kind="capability",
                action=f"{capability}.{action}",
                payload={"capability": capability, "action": action, "arguments": payload},
                summary=str(confirmation["summary"]),
            )
            return ToolExecutionResult(
                action="capability_call",
                success=False,
                error=str(operation["summary"]),
                audit={
                    "requires_confirmation": True,
                    "confirmation_action": "confirm_capability_operation",
                    "cancel_action": "cancel_capability_operation",
                    "operation_id": operation["id"],
                    "capability": capability,
                    "operation": action,
                },
            )
        result = self.capabilities.execute(
            capability,
            action,
            payload,
            confirmed=False,
            session_id=session_id,
        )
        return self._capability_result("capability_call", result)

    # 作用：区分只读安全操作和可能改变外部状态、因此必须二次确认的能力调用。
    # 参数 capability：统一能力中心中的能力标识。
    # 参数 action：该能力下准备调用的操作名称。
    # 参数 arguments：用于判断 MCP 方法等风险细节的下游参数。
    # 返回：需要用户二次确认时返回真。
    def _capability_needs_confirmation(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any],
    ) -> bool:
        safe_operations = {
            ("tool_search", "search"),
            ("workspace", "list"),
            ("workspace", "read"),
            ("workspace", "search"),
            ("web", "search"),
            ("web", "fetch"),
            ("documents", "read"),
            ("system", "snapshot"),
            ("hardware", "status"),
            ("organizer", "list"),
            ("knowledge_graph", "list"),
            ("knowledge_graph", "search"),
            ("insights", "list"),
            ("plugins", "list"),
            ("mcp", "list"),
            ("media", "list"),
            ("mail", "status"),
            ("prompt_governance", "status"),
            ("prompt_governance", "analyze"),
            ("prompt_governance", "list_experiments"),
            ("observability", "status"),
            ("workflows", "status"),
            ("workflows", "list"),
        }
        if (capability, action) in safe_operations:
            return False
        if capability == "mcp" and action == "call":
            server = str(arguments.get("server", "")).strip()
            if (
                self.capabilities is not None
                and server in self.capabilities._mcp_stdio_servers()
            ):
                return True
            return str(arguments.get("method", "tools/list")) not in {
                "initialize",
                "tools/list",
                "resources/list",
            }
        return True

    # 作用：领取当前会话的能力确认单，并分派到文件或外部能力执行路径。
    # 参数 arguments：包含待确认 operation_id 的工具参数。
    # 参数 session_id：用于校验确认单归属的用户会话 ID。
    def _confirm_capability_operation(
        self,
        arguments: dict[str, Any],
        session_id: str,
    ) -> ToolExecutionResult:
        operation = self.operations.claim(session_id, str(arguments.get("operation_id", "")))
        if operation is None:
            return ToolExecutionResult(
                action="confirm_capability_operation",
                success=False,
                error="没有找到等待确认的能力操作，或确认已经过期。",
            )
        if operation["kind"] == "file":
            return self._execute_claimed_file_operation(
                operation,
                "confirm_capability_operation",
            )
        return self._execute_claimed_capability(operation, "confirm_capability_operation")

    # 作用：处理未知终态外发的人工核对，支持确认成功、原键重试或明确放弃。
    # 参数 session_id：用于校验外发、确认单和任务归属的会话 ID。
    # 参数 dispatch_id：待核对的持久外发记录 ID。
    # 参数 decision：用户选择的 confirm_succeeded、retry 或 abandon。
    # 返回：更新后的外发、确认单、任务和工具结果摘要。
    def review_external_dispatch(
        self,
        *,
        session_id: str,
        dispatch_id: str,
        decision: str,
    ) -> dict[str, Any]:
        if decision not in {"confirm_succeeded", "retry", "abandon"}:
            raise ExternalDispatchReviewError("不支持的外发处理决定。", status_code=400)
        with self._external_dispatch_review_lock:
            dispatch = self.external_dispatches.get(dispatch_id)
            if not dispatch or dispatch.get("session_id") != session_id:
                raise ExternalDispatchReviewError("外发记录不存在。", status_code=404)
            operation_id = str(dispatch.get("operation_id", ""))
            operation = self.operations.get(operation_id)
            if (
                operation is None
                or operation.get("session_id") != session_id
                or operation.get("kind") != "capability"
            ):
                raise ExternalDispatchReviewError("外发记录缺少可核对的原确认单。")
            task_id = str(operation.get("task_id", ""))
            task = self.tasks.get(task_id) if task_id else None
            if task is not None and task.get("session_id") != session_id:
                raise ExternalDispatchReviewError("外发记录与原任务会话不一致。")
            if decision == "retry":
                claimed, task_id = self._prepare_external_dispatch_retry(
                    dispatch,
                    operation,
                    task,
                )
            else:
                return self._resolve_external_dispatch_manually(
                    dispatch,
                    operation,
                    task,
                    decision=decision,
                )
        with agent_task_scope(task_id):
            tool_result = self._execute_claimed_capability(
                claimed,
                "confirm_capability_operation",
            )
        refreshed = self.external_dispatches.get(str(dispatch.get("id", "")))
        return self._external_dispatch_review_response(
            decision="retry",
            dispatch=refreshed,
            operation=self.operations.get(str(operation.get("id", ""))),
            task=self.tasks.get(task_id) if task_id else None,
            tool_result=tool_result,
        )

    # 作用：启动时用供应商完成回执补齐本地提交窗口中断的确认单和任务证据。
    # 参数：无。
    # 返回：成功恢复的确定完成外发数量。
    def reconcile_completed_external_dispatches(self) -> int:
        """Claim definite provider completions left between two local commits."""
        reconciled = 0
        with self._external_dispatch_review_lock:
            for dispatch in self.external_dispatches.list_status("completed"):
                operation = self.operations.get(str(dispatch.get("operation_id", "")))
                if operation is None or operation.get("kind") != "capability":
                    continue
                task_id = str(operation.get("task_id", ""))
                task = self.tasks.get(task_id) if task_id else None
                operation_needs_reconciliation = operation.get("status") == "interrupted"
                task_needs_reconciliation = bool(
                    task is not None and task.get("status") == "interrupted"
                )
                if not operation_needs_reconciliation and not task_needs_reconciliation:
                    continue
                if task_needs_reconciliation:
                    self.tasks.prepare_external_dispatch_review(
                        task_id,
                        str(operation.get("id", "")),
                        dispatch_id=str(dispatch.get("id", "")),
                        decision="reconcile_completed",
                    )
                result_payload = self._reconciled_external_result(dispatch)
                if operation_needs_reconciliation:
                    resolved = self.operations.resolve_interrupted(
                        str(operation.get("session_id", "")),
                        str(operation.get("id", "")),
                        status="completed",
                        result=result_payload,
                    )
                    if resolved is None:
                        continue
                if task_needs_reconciliation:
                    tool_result = self._reconciled_external_tool_result(
                        dispatch,
                        manual=False,
                    )
                    self._finish_source_task(
                        operation,
                        status="completed",
                        phase="verified",
                        result=tool_result,
                    )
                reconciled += 1
        return reconciled

    # 作用：在状态允许且用户明确要求后，原子重开原确认单并领取同一外发重试权。
    # 参数 dispatch：处于 uncertain 的外发账本记录。
    # 参数 operation：与外发绑定的原始能力确认单。
    # 参数 task：确认单来源任务；无来源任务时允许为空。
    # 返回：已领取确认单以及需要绑定执行上下文的任务 ID。
    def _prepare_external_dispatch_retry(
        self,
        dispatch: dict[str, Any],
        operation: dict[str, Any],
        task: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        if dispatch.get("status") != "uncertain":
            raise ExternalDispatchReviewError("只有结果待核对的外发可以明确重试。")
        if operation.get("status") not in {"interrupted", "pending"}:
            raise ExternalDispatchReviewError("原确认单正在处理或已经结束，不能重复重试。")
        if task is not None and (
            task.get("status") not in {"interrupted", "waiting_confirmation", "running"}
            or bool(task.get("cancel_requested"))
        ):
            raise ExternalDispatchReviewError("原任务当前状态不允许重试。")
        _, operation_status, prepared = self.external_dispatches.prepare_retry_operation(
            str(dispatch.get("id", "")),
            session_id=str(operation.get("session_id", "")),
        )
        if not prepared or operation_status != "pending":
            raise ExternalDispatchReviewError("原确认单没有进入可重试状态。")
        task_id = str(operation.get("task_id", ""))
        if task is not None and task_id:
            self.tasks.prepare_external_dispatch_review(
                task_id,
                str(operation.get("id", "")),
                dispatch_id=str(dispatch.get("id", "")),
                decision="retry",
            )
            running = self.tasks.mark(
                task_id,
                status="running",
                phase="external_dispatch_retry",
            )
            if running is None or running.get("status") != "running":
                raise ExternalDispatchReviewError("原任务没有进入重试状态。")
        claimed = self.operations.claim(
            str(operation.get("session_id", "")),
            str(operation.get("id", "")),
        )
        if claimed is None:
            raise ExternalDispatchReviewError("原确认单已经被其他请求领取。")
        return claimed, task_id

    # 作用：按用户核对结果原子关闭未知外发、确认单和来源任务，且保持重复处理幂等。
    # 参数 dispatch：待处理的未知终态外发记录。
    # 参数 operation：与外发绑定的原始确认单。
    # 参数 task：可能需要同步验收的来源 Agent 任务。
    # 参数 decision：confirm_succeeded 或 abandon 人工结论。
    # 返回：外发核对完成后的统一公开响应。
    def _resolve_external_dispatch_manually(
        self,
        dispatch: dict[str, Any],
        operation: dict[str, Any],
        task: dict[str, Any] | None,
        *,
        decision: str,
    ) -> dict[str, Any]:
        target_dispatch_status = "completed" if decision == "confirm_succeeded" else "abandoned"
        target_operation_status = "completed" if decision == "confirm_succeeded" else "cancelled"
        manual = (
            dispatch.get("receipt", {}).get("manual_resolution", {})
            if isinstance(dispatch.get("receipt"), dict)
            else {}
        )
        already_resolved = bool(
            dispatch.get("status") == target_dispatch_status
            and isinstance(manual, dict)
            and manual.get("decision") == decision
        )
        if dispatch.get("status") != "uncertain" and not already_resolved:
            raise ExternalDispatchReviewError("这条外发已经进入其他终态，不能覆盖。")
        if operation.get("status") not in {
            "interrupted",
            "pending",
            target_operation_status,
        }:
            raise ExternalDispatchReviewError("原确认单正在处理或已经进入其他终态。")
        task_id = str(operation.get("task_id", ""))
        if task is not None and task_id and task.get("status") == "interrupted":
            self.tasks.prepare_external_dispatch_review(
                task_id,
                str(operation.get("id", "")),
                dispatch_id=str(dispatch.get("id", "")),
                decision=decision,
            )
        operation_result = self._reconciled_external_result(
            {**dispatch, "status": target_dispatch_status}
        )
        resolved_dispatch, operation_status, resolved = (
            self.external_dispatches.resolve_uncertain_operation(
                str(dispatch.get("id", "")),
                session_id=str(dispatch.get("session_id", "")),
                decision=decision,
                operation_result=operation_result,
            )
        )
        if (
            not resolved
            or resolved_dispatch.get("status") != target_dispatch_status
            or operation_status != target_operation_status
        ):
            raise ExternalDispatchReviewError("外发记录已被其他处理请求更新。")
        resolved_operation = self.operations.get(str(operation.get("id", "")))
        tool_result = self._reconciled_external_tool_result(
            resolved_dispatch,
            manual=True,
            abandoned=decision == "abandon",
        )
        current_task = self.tasks.get(task_id) if task_id else None
        if current_task is not None and current_task.get("status") in {
            "interrupted",
            "waiting_confirmation",
            "running",
        }:
            self._finish_source_task(
                operation,
                status="completed" if decision == "confirm_succeeded" else "cancelled",
                phase="verified" if decision == "confirm_succeeded" else "cancelled",
                error=tool_result.error,
                result=tool_result,
            )
        return self._external_dispatch_review_response(
            decision=decision,
            dispatch=resolved_dispatch,
            operation=resolved_operation,
            task=self.tasks.get(task_id) if task_id else None,
            tool_result=tool_result,
        )

    # 作用：把已完成或人工裁定的外发回执恢复为原确认单可保存的能力结果。
    # 参数 dispatch：包含能力、操作和供应商回执的外发记录。
    @staticmethod
    # 作用：执行“reconciled_external_result”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 dispatch：调用方传入的dispatch，用于本次处理。
    def _reconciled_external_result(dispatch: dict[str, Any]) -> dict[str, Any]:
        receipt = dispatch.get("receipt") if isinstance(dispatch.get("receipt"), dict) else {}
        return {
            "ok": dispatch.get("status") == "completed",
            "capability": str(dispatch.get("capability", "")),
            "action": str(dispatch.get("action", "")),
            "result": {
                "reconciled_dispatch": str(dispatch.get("id", "")),
                "provider_receipt": receipt.get("provider_receipt", {}),
                "manual_resolution": receipt.get("manual_resolution", {}),
            },
        }

    # 作用：将外发对账结论转换为可参与来源任务验收的工具回执。
    # 参数 dispatch：已完成或放弃的外发记录。
    # 参数 manual：结果是否来自用户人工核对。
    # 参数 abandoned：用户是否明确放弃该未知外发。
    @staticmethod
    # 作用：执行“reconciled_external_tool_result”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 dispatch：调用方传入的dispatch，用于本次处理。
    # 参数 manual：调用方传入的manual，用于本次处理。
    # 参数 abandoned：调用方传入的abandoned，用于本次处理。
    def _reconciled_external_tool_result(
        dispatch: dict[str, Any],
        *,
        manual: bool,
        abandoned: bool = False,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            action="confirm_capability_operation",
            success=not abandoned,
            content=(
                "已根据用户核对确认外部操作成功。"
                if manual
                else "已根据持久外发完成回执恢复原操作。"
            )
            if not abandoned
            else "",
            error="用户已放弃这次不确定外发。" if abandoned else "",
            audit={
                "confirmed": True,
                "external_dispatch_id": str(dispatch.get("id", "")),
                "external_dispatch_attempt": int(dispatch.get("attempts", 0) or 0),
                "provider_idempotency_key": str(
                    dispatch.get("provider_idempotency_key", "")
                ),
                "reconciled_after_restart": not manual,
                "manual_external_review": manual,
                "external_dispatch_abandoned": abandoned,
                "deduplicated": True,
            },
        )

    # 作用：组合外发核对后的记录、确认单、任务和工具结果供接口返回。
    # 参数 decision：本次采用的人工处理决定。
    # 参数 dispatch：处理后的外发账本记录。
    # 参数 operation：处理后的原始确认单；缺失时可为空。
    # 参数 task：处理后的来源任务；无来源任务时可为空。
    # 参数 tool_result：本次对账生成的工具执行回执。
    @staticmethod
    # 作用：执行“external_dispatch_review_response”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 decision：调用方传入的decision，用于本次处理。
    # 参数 dispatch：调用方传入的dispatch，用于本次处理。
    # 参数 operation：调用方传入的operation，用于本次处理。
    # 参数 task：调用方传入的task，用于本次处理。
    # 参数 tool_result：调用方传入的tool_result，用于本次处理。
    def _external_dispatch_review_response(
        *,
        decision: str,
        dispatch: dict[str, Any],
        operation: dict[str, Any] | None,
        task: dict[str, Any] | None,
        tool_result: ToolExecutionResult,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "decision": decision,
            "dispatch": dispatch,
            "operation_status": str(operation.get("status", "")) if operation else "",
            "task": (
                {
                    "id": str(task.get("id", "")),
                    "status": str(task.get("status", "")),
                    "phase": str(task.get("phase", "")),
                }
                if task is not None
                else None
            ),
            "result": {
                "success": tool_result.success,
                "error": tool_result.error,
                "audit": redact_for_audit(tool_result.audit),
            },
        }

    # 作用：执行已确认能力并维护外发 prepared/dispatching/终态、供应商幂等键和来源任务。
    # 参数 operation：当前请求已原子领取的能力确认单。
    # 参数 result_action：最终工具回执应使用的确认动作名称。
    # 返回：成功、确定失败或未知副作用终态的工具回执。
    def _execute_claimed_capability(
        self,
        operation: dict[str, Any],
        result_action: ToolAction,
    ) -> ToolExecutionResult:
        if operation["kind"] != "capability" or self.capabilities is None:
            self.operations.fail(str(operation["id"]), "待确认操作类型不匹配。")
            return ToolExecutionResult(action=result_action, success=False, error="待确认操作类型不匹配。")
        payload = operation["payload"]
        capability = str(payload.get("capability", ""))
        action = str(payload.get("action", ""))
        capability_arguments = (
            dict(payload.get("arguments", {}))
            if isinstance(payload.get("arguments"), dict)
            else {}
        )
        operation_id = str(operation["id"])
        dispatch = self.external_dispatches.prepare(
            operation_id=operation_id,
            session_id=str(operation.get("session_id", "")),
            task_id=str(operation.get("task_id", "")),
            capability=capability,
            action=action,
            payload_fingerprint=str(operation.get("payload_hash", "")),
        )
        if dispatch.get("status") == "completed":
            return self._complete_claimed_from_dispatch(
                operation,
                result_action=result_action,
                dispatch=dispatch,
            )
        dispatch = self.external_dispatches.start(operation_id)
        dispatch_metadata = {
            "dispatch_id": str(dispatch.get("id", "")),
            "idempotency_key": str(dispatch.get("provider_idempotency_key", "")),
            "attempt": int(dispatch.get("attempts", 0) or 0),
            "provider_call_started": False,
            "side_effecting": False,
        }
        capability_arguments["_agent_dispatch"] = dispatch_metadata
        if capability == "workflows" and action == "enqueue":
            capability_arguments["_background_context"] = {
                "source_task_id": str(operation.get("task_id") or ""),
                "request_task_id": current_agent_task_id(),
                "session_id": str(operation.get("session_id") or ""),
            }
        result = self.capabilities.execute(
            capability,
            action,
            capability_arguments,
            confirmed=True,
            session_id=str(operation.get("session_id") or ""),
            cancel_check=self.task_cancel_requested,
        )
        tool_result = self._capability_result(result_action, result)
        cancelled = self.task_cancel_requested() and not tool_result.success
        workflow_result = result.get("result") if isinstance(result, dict) else {}
        workflow_interrupted = (
            capability == "workflows"
            and isinstance(workflow_result, dict)
            and workflow_result.get("status") == "interrupted"
        )
        provider_started = bool(dispatch_metadata.get("provider_call_started"))
        provider_side_effecting = bool(dispatch_metadata.get("side_effecting"))
        uncertain = not tool_result.success and (
            (provider_started and provider_side_effecting) or workflow_interrupted
        )
        if cancelled:
            tool_result.audit["task_cancelled"] = True
        tool_result.audit.update(
            {
                "confirmed": True,
                "operation_id": operation_id,
                "source_action": operation["action"],
                "external_dispatch_id": dispatch_metadata["dispatch_id"],
                "external_dispatch_attempt": dispatch_metadata["attempt"],
                "provider_idempotency_key": dispatch_metadata["idempotency_key"],
                "provider_call_started": provider_started,
                "provider_side_effecting": provider_side_effecting,
            }
        )
        if tool_result.success:
            self.external_dispatches.finish(
                operation_id,
                status="completed",
                receipt=self._external_dispatch_receipt(result),
            )
            self.operations.complete(operation_id, result)
            self._finish_source_task(
                operation,
                status=(
                    "waiting_background"
                    if tool_result.audit.get("background_job_pending")
                    else "completed"
                ),
                phase=(
                    "background_queued"
                    if tool_result.audit.get("background_job_pending")
                    else "verified"
                ),
                result=tool_result,
            )
        elif uncertain:
            uncertain_error = (
                "供应商调用已经开始，但没有取得可证明的终态；"
                "为避免重复外发，系统不会自动重试。"
            )
            tool_result.error = uncertain_error
            tool_result.audit["uncertain_side_effect"] = True
            self.external_dispatches.finish(
                operation_id,
                status="uncertain",
                receipt=self._external_dispatch_receipt(result),
                error=uncertain_error,
            )
            self.operations.interrupt(operation_id, uncertain_error)
            self._finish_source_task(
                operation,
                status="interrupted",
                phase="operation_interrupted",
                error=uncertain_error,
                result=tool_result,
            )
        else:
            error = tool_result.error or str(result.get("error", "能力调用失败。"))
            self.external_dispatches.finish(
                operation_id,
                status="failed",
                receipt=self._external_dispatch_receipt(result),
                error=error,
            )
            self.operations.fail(operation_id, error)
            self._finish_source_task(
                operation,
                status="cancelled" if cancelled else "failed",
                phase="cancelled" if cancelled else "verification_failed",
                error=error,
                result=tool_result,
            )
        return tool_result

    # 作用：发现已有确定完成外发时复用其回执关闭确认单，不再次请求供应商。
    # 参数 operation：当前已领取的原始能力确认单。
    # 参数 result_action：复用结果应使用的工具动作名称。
    # 参数 dispatch：已有 completed 状态和供应商回执的外发记录。
    def _complete_claimed_from_dispatch(
        self,
        operation: dict[str, Any],
        *,
        result_action: ToolAction,
        dispatch: dict[str, Any],
    ) -> ToolExecutionResult:
        operation_id = str(operation.get("id", ""))
        receipt = dispatch.get("receipt") if isinstance(dispatch.get("receipt"), dict) else {}
        result = {
            "ok": True,
            "capability": str(dispatch.get("capability", "")),
            "action": str(dispatch.get("action", "")),
            "result": {
                "reconciled_dispatch": str(dispatch.get("id", "")),
                "provider_receipt": receipt.get("provider_receipt", {}),
            },
        }
        self.operations.complete(operation_id, result)
        tool_result = ToolExecutionResult(
            action=result_action,
            success=True,
            content="外部调用已有完成回执，本次确认只认领原结果，没有再次发送。",
            audit={
                "confirmed": True,
                "operation_id": operation_id,
                "source_action": operation.get("action", ""),
                "external_dispatch_id": str(dispatch.get("id", "")),
                "external_dispatch_attempt": int(dispatch.get("attempts", 0) or 0),
                "provider_idempotency_key": str(
                    dispatch.get("provider_idempotency_key", "")
                ),
                "reconciled_after_restart": True,
                "deduplicated": True,
            },
        )
        self._finish_source_task(
            operation,
            status="completed",
            phase="verified",
            result=tool_result,
        )
        return tool_result

    # 作用：从完整能力响应提取指纹、供应商业务 ID、延迟和成功标记作为精简回执。
    # 参数 result：能力中心返回的完整结构化执行结果。
    @staticmethod
    # 作用：执行“external_dispatch_receipt”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 result：下游组件返回的原始结果对象。
    def _external_dispatch_receipt(result: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        value = result.get("result") if isinstance(result.get("result"), dict) else {}
        provider_receipt = {
            key: str(value[key])[:300]
            for key in ("id", "message_id", "request_id", "event_id", "workflow_run_id")
            if value.get(key) is not None and value.get(key) != ""
        }
        return {
            "response_fingerprint": hashlib.sha256(encoded).hexdigest(),
            "provider_receipt": provider_receipt,
            "ok": bool(result.get("ok")),
            "latency_ms": int(result.get("latency_ms", 0) or 0),
        }

    # 作用：取消当前会话的待确认能力操作，并同步关闭来源任务。
    # 参数 arguments：包含待取消 operation_id 的工具参数。
    # 参数 session_id：用于校验确认单归属的用户会话 ID。
    def _cancel_capability_operation(
        self,
        arguments: dict[str, Any],
        session_id: str,
    ) -> ToolExecutionResult:
        operation = self.operations.cancel(session_id, str(arguments.get("operation_id", "")))
        if operation is not None:
            self._finish_source_task(operation, status="cancelled", phase="cancelled")
        return ToolExecutionResult(
            action="cancel_capability_operation",
            success=operation is not None,
            content="已取消待确认操作。" if operation is not None else "",
            error="没有找到等待取消的操作。" if operation is None else "",
            audit={"operation_id": operation["id"]} if operation is not None else {},
        )

    # 作用：将能力中心的统一响应转换成工具回执，并保留工作流后台状态等验收证据。
    # 参数 action：对模型和任务账本呈现的工具动作名称。
    # 参数 result：能力中心返回的结构化结果、错误和审计信息。
    @staticmethod
    # 作用：执行“capability_result”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 action：调用方传入的action，用于本次处理。
    # 参数 result：下游组件返回的原始结果对象。
    def _capability_result(action: ToolAction, result: dict[str, Any]) -> ToolExecutionResult:
        value = result.get("result")
        nested_execution_failed = (
            isinstance(value, dict)
            and (
                int(value.get("returncode", 0) or 0) != 0
                or (
                    result.get("capability") == "python"
                    and value.get("ok") is False
                )
            )
        )
        if nested_execution_failed:
            detail = str(value.get("stderr") or value.get("stdout") or "执行返回非零状态。")
            return ToolExecutionResult(
                action=action,
                success=False,
                error=detail[:4000],
                audit={
                    "capability": result.get("capability", ""),
                    "operation": result.get("action", ""),
                    "latency_ms": result.get("latency_ms", 0),
                    "returncode": value.get("returncode"),
                },
            )
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        capability_audit = {
            "capability": result.get("capability", ""),
            "operation": result.get("action", ""),
            "latency_ms": result.get("latency_ms", 0),
        }
        if result.get("capability") == "workflows" and isinstance(value, dict):
            capability_audit.update(
                {
                    "workflow_run_id": value.get("workflow_run_id", value.get("id", "")),
                    "workflow_status": value.get("status", "unknown"),
                    "workflow_steps": [
                        {
                            "id": item.get("id", ""),
                            "capability": item.get("capability", ""),
                            "operation": item.get("action", ""),
                            "status": item.get("status", "unknown"),
                            "attempts": item.get("attempts", 0),
                        }
                        for item in value.get("steps", [])
                        if isinstance(item, dict)
                    ],
                }
            )
            if value.get("background_job_id") and value.get("status") == "queued":
                capability_audit.update(
                    {
                        "background_job_pending": True,
                        "background_job_id": value["background_job_id"],
                        "background_job_status": value.get(
                            "background_job_status", "queued"
                        ),
                    }
                )
        return ToolExecutionResult(
            action=action,
            success=bool(result.get("ok")),
            content=content[:24_000] if result.get("ok") else "",
            error=str(result.get("error", "")),
            audit=capability_audit,
        )

    # 作用：把确认操作结果回填原等待步骤，并基于任务契约重新计算来源任务终态。
    # 参数 operation：携带 source task_id 的持久确认单。
    # 参数 status：确认操作需要写回来源步骤的状态。
    # 参数 phase：来源任务接下来展示的执行阶段。
    # 参数 error：确认、外发或验收失败的说明。
    # 参数 result：可写入步骤并参与证据验收的工具回执。
    def _finish_source_task(
        self,
        operation: dict[str, Any],
        *,
        status: str,
        phase: str,
        error: str = "",
        result: ToolExecutionResult | None = None,
    ) -> None:
        source_task_id = str(operation.get("task_id", ""))
        if source_task_id:
            source_action = (
                "capability_call"
                if operation.get("kind") == "capability"
                else str(operation.get("action", result.action if result else ""))
            )
            evidence_result = None
            if result is not None:
                evidence_result = result.model_copy(
                    update={
                        "action": source_action,
                        "audit": {
                            **result.audit,
                            "source_confirmation_action": result.action,
                            "source_operation_id": operation.get("id", ""),
                        },
                    }
                )
            self.tasks.resolve_waiting_step(
                source_task_id,
                status="completed" if status == "completed" else status,
                error=error,
                output=(
                    redact_for_audit(evidence_result.model_dump(mode="json"))
                    if evidence_result is not None
                    else None
                ),
                action=source_action,
            )
            if status == "waiting_background":
                self.tasks.mark(
                    source_task_id,
                    status="waiting_background",
                    phase=phase,
                    outcome={
                        "status": "waiting_background",
                        "verified": False,
                        "summary": "工作流已经进入持久后台队列，完成后会回写验收结果。",
                        "checks": {
                            "background_job_id": (
                                result.audit.get("background_job_id", "")
                                if result is not None
                                else ""
                            )
                        },
                    },
                )
                return
            task = self.tasks.get(source_task_id)
            if task is None:
                return
            if status == "cancelled":
                outcome = {
                    "status": "cancelled",
                    "verified": False,
                    "summary": "等待确认的操作已经取消。",
                    "checks": {},
                }
                final_status = "cancelled"
                final_phase = "cancelled"
            elif status == "interrupted":
                outcome = {
                    "status": "interrupted",
                    "verified": False,
                    "summary": (
                        "外部调用已经开始但终态无法证明；"
                        "为避免重复发送，任务没有自动重放。"
                    ),
                    "checks": {
                        "uncertain_external_dispatch": True,
                        "external_dispatch_id": (
                            result.audit.get("external_dispatch_id", "")
                            if result is not None
                            else ""
                        ),
                    },
                }
                final_status = "interrupted"
                final_phase = "operation_interrupted"
            else:
                stored_results: list[ToolExecutionResult] = []
                for step in task.get("steps", []):
                    raw_output = step.get("output") if isinstance(step, dict) else None
                    if not isinstance(raw_output, dict) or not raw_output.get("action"):
                        continue
                    try:
                        stored_results.append(ToolExecutionResult.model_validate(raw_output))
                    except (TypeError, ValueError):
                        continue
                contract = task.get("contract") if isinstance(task.get("contract"), dict) else {}
                requirements = (
                    contract.get("requirements", [])
                    if isinstance(contract.get("requirements"), list)
                    else []
                )
                verified = AgentOutcomeVerifier().verify(
                    content="",
                    tool_results=stored_results,
                    degraded=False,
                    degradation_reason="",
                    requirements=requirements,
                )
                outcome = verified.as_dict()
                final_status = verified.status
                final_phase = {
                    "completed": "verified",
                    "waiting_confirmation": "waiting_confirmation",
                    "cancelled": "cancelled",
                    "degraded": "degraded",
                }.get(verified.status, "verification_failed")
            self.tasks.mark(
                source_task_id,
                status=final_status,
                phase=final_phase,
                outcome=outcome,
                error=error,
            )

    # 作用：取得已初始化的文件访问边界；能力缺失时以受控异常终止文件工具。
    # 参数：无。
    def _require_file_access(self) -> AgentFileAccess:
        if self.file_access is None:
            raise AgentFileError("Agent 文件能力尚未初始化。")
        return self.file_access

    # 作用：读取本机当前日期时间并生成只读工具回执。
    # 参数 arguments：工具协议传入的空参数对象。
    # 参数 session_id：当前会话 ID；读取时间不使用会话状态。
    def _get_time(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return ToolExecutionResult(action="get_time", success=True, content=f"当前时间：{now}")

    # 作用：使用任务级幂等键持久化显式记忆，重复执行时复用同一记录。
    # 参数 arguments：包含用户明确要求保存的 content。
    # 参数 session_id：新记忆归属的用户会话 ID。
    def _remember(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        content = str(arguments.get("content", "")).strip()
        if not content:
            return ToolExecutionResult(action="remember", success=False, error="记忆内容为空。")
        idempotency_key = self._effect_idempotency_key(
            current_agent_task_id(),
            _CURRENT_TOOL_IDEMPOTENCY_KEY.get(),
        )
        existing = self.memory.memory_by_idempotency(session_id, idempotency_key)
        self.memory.remember(
            session_id,
            content,
            idempotency_key=idempotency_key,
        )
        memory = self.memory.memory_by_idempotency(session_id, idempotency_key)
        return ToolExecutionResult(
            action="remember",
            success=True,
            content=f"已记住：{content}",
            audit={
                "memory_id": int(memory.get("id", 0)) if memory else 0,
                "deduplicated": existing is not None,
            },
        )

    # 作用：按关键词召回当前会话的相关记忆并整理为文本回执。
    # 参数 arguments：包含记忆检索 query 的工具参数。
    # 参数 session_id：限定记忆检索范围的会话 ID。
    def _recall(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        query = str(arguments.get("query", "")).strip()
        memories = self.memory.recall(session_id, query)
        content = "\n".join(memories) if memories else "没有找到相关记忆。"
        return ToolExecutionResult(action="recall", success=True, content=content)

    # 作用：从会话记忆和永久记忆中删除匹配关键词的记录并报告数量。
    # 参数 arguments：包含待遗忘关键词 query 的工具参数。
    # 参数 session_id：限定删除范围的用户会话 ID。
    def _forget(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolExecutionResult(
                action="forget",
                success=False,
                error="要忘记的关键词为空。",
            )
        deleted_count = self.memory.forget(session_id, query)
        permanent_deleted_count = (
            self.permanent_memory.forget(session_id, query)
            if self.permanent_memory is not None
            else 0
        )
        total_deleted = deleted_count + permanent_deleted_count
        content = (
            f"已忘记 {total_deleted} 条与“{query}”相关的记忆。"
            if total_deleted
            else f"没有找到与“{query}”相关的记忆。"
        )
        return ToolExecutionResult(
            action="forget",
            success=True,
            content=content,
            audit={
                "deleted_count": deleted_count,
                "permanent_deleted_count": permanent_deleted_count,
            },
        )

    # 作用：仅在明确确认后清空会话记忆、永久记忆及能力授权记录。
    # 参数 arguments：包含 confirmed 明确确认标记的工具参数。
    # 参数 session_id：需要清理全部相关状态的用户会话 ID。
    def _clear_memory(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        confirmed = bool(arguments.get("confirmed"))
        if not confirmed:
            return ToolExecutionResult(
                action="clear_memory",
                success=False,
                error="清空记忆需要明确确认。请说“确认清空记忆”。",
                audit={"blocked_by_guardrail": True, "required_confirmation": True},
            )
        local_count = self.memory.clear(session_id)
        permanent_count = (
            self.permanent_memory.clear(session_id)
            if self.permanent_memory is not None
            else 0
        )
        capability_grant_count = (
            self.capabilities.permission_policy.delete_session(session_id)
            if self.capabilities is not None
            else 0
        )
        return ToolExecutionResult(
            action="clear_memory",
            success=True,
            content="已清空当前会话记忆。",
            audit={
                "local_count": local_count,
                "permanent_count": permanent_count,
                "capability_grant_count": capability_grant_count,
            },
        )

    # 作用：解析自然语言时间并以任务级幂等键创建持久提醒，不确定时间则明确标为未定时。
    # 参数 arguments：包含提醒 content 和可选 when 时间表达式。
    # 参数 session_id：提醒记录归属的用户会话 ID。
    def _set_reminder(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        content = str(arguments.get("content", "")).strip()
        if not content:
            return ToolExecutionResult(
                action="set_reminder",
                success=False,
                error="提醒内容为空。",
            )
        when = str(arguments.get("when", "")).strip()
        raw = f"{when} {content}".strip() if when else content
        timezone = os.getenv("YUKINO_TIMEZONE", ReminderScheduleParser.DEFAULT_TIMEZONE)
        parsed = ReminderScheduleParser(timezone).parse(raw)
        idempotency_key = self._effect_idempotency_key(
            current_agent_task_id(),
            _CURRENT_TOOL_IDEMPOTENCY_KEY.get(),
        )
        existing = self.memory.reminder_by_idempotency(
            session_id,
            idempotency_key,
        )
        reminder = self.memory.add_reminder(
            session_id,
            parsed.content,
            due_at=parsed.due_at,
            timezone=parsed.timezone,
            recurrence=parsed.recurrence,
            idempotency_key=idempotency_key,
        )
        if reminder is None:
            return ToolExecutionResult(
                action="set_reminder",
                success=False,
                error="提醒没有保存成功。",
            )
        if parsed.scheduled:
            due_text = dt.datetime.fromtimestamp(parsed.due_at, parsed.zone).strftime(
                "%Y-%m-%d %H:%M"
            )
            result_content = f"已设置提醒 #{reminder['id']}：{parsed.content}（{due_text}）"
            if parsed.recurrence == "daily":
                result_content += "，每天重复"
            elif parsed.recurrence.startswith("weekly:"):
                result_content += "，每周重复"
        else:
            result_content = (
                f"已记录未定时提醒 #{reminder['id']}：{parsed.content}。"
                "没有识别到明确时间，因此不会假装已经安排自动触发。"
            )
        return ToolExecutionResult(
            action="set_reminder",
            success=True,
            content=result_content,
            audit={
                "reminder_id": reminder["id"],
                "scheduled": parsed.scheduled,
                "due_at": parsed.due_at,
                "timezone": parsed.timezone,
                "schedule_expression": parsed.expression,
                "recurrence": parsed.recurrence,
                "deduplicated": existing is not None,
            },
        )

    # 作用：列出当前会话的提醒状态、触发时间和内容。
    # 参数 arguments：工具协议传入的空参数对象。
    # 参数 session_id：限定提醒列表范围的用户会话 ID。
    def _list_reminders(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        reminders = self.memory.list_reminders(session_id)
        if not reminders:
            return ToolExecutionResult(
                action="list_reminders",
                success=True,
                content="当前没有提醒。",
            )
        lines = []
        for item in reminders:
            due = (
                dt.datetime.fromtimestamp(float(item["due_at"])).strftime("%Y-%m-%d %H:%M")
                if item.get("due_at") is not None
                else "未定时"
            )
            lines.append(f"#{item['id']} [{item['status']}] {due} · {item['content']}")
        return ToolExecutionResult(
            action="list_reminders",
            success=True,
            content="\n".join(lines),
        )

    # 作用：按提醒编号取消当前会话中尚未送达的提醒。
    # 参数 arguments：包含 reminder_id 的工具参数。
    # 参数 session_id：用于校验提醒归属的用户会话 ID。
    def _cancel_reminder(self, arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
        try:
            reminder_id = int(arguments.get("reminder_id", 0))
        except (TypeError, ValueError):
            reminder_id = 0
        cancelled = reminder_id > 0 and self.memory.cancel_reminder(session_id, reminder_id)
        return ToolExecutionResult(
            action="cancel_reminder",
            success=cancelled,
            content=f"已取消提醒 #{reminder_id}。" if cancelled else "",
            error="没有找到可取消的提醒，或提醒已经送达。" if not cancelled else "",
            audit={"reminder_id": reminder_id, "cancelled": cancelled},
        )

    # 作用：为固定能力和操作创建符合工具注册表协议的处理器闭包。
    # 参数 capability：处理器绑定的能力中心标识。
    # 参数 action：处理器绑定的能力操作名称。
    # 返回：接收工具参数和会话 ID 的标准 ToolHandler。
    def _capability_handler(self, capability: str, action: str) -> ToolHandler:
        # 作用：补充会话、取消和幂等上下文后执行绑定能力，并转换为工具回执。
        # 参数 arguments：模型或确定性解析器传入的能力参数。
        # 参数 session_id：权限、数据和任务操作归属的用户会话 ID。
        def handle(arguments: dict[str, Any], session_id: str) -> ToolExecutionResult:
            if self.capabilities is None:
                return ToolExecutionResult(action="none", success=False, error="部务能力未初始化。")
            capability_arguments = dict(arguments)
            if capability == "organizer":
                capability_arguments["kind"] = "tasks"
                if action == "add":
                    capability_arguments["_agent_idempotency_key"] = self._effect_idempotency_key(
                        current_agent_task_id(),
                        _CURRENT_TOOL_IDEMPOTENCY_KEY.get(),
                    )
            result = self.capabilities.execute(
                capability,
                action,
                capability_arguments,
                confirmed=capability == "python",
                session_id=session_id,
                cancel_check=self.task_cancel_requested,
            )
            tool_action: ToolAction = {
                    ("tool_search", "search"): "search_capabilities",
                    ("workspace", "list"): "list_files",
                    ("workspace", "read"): "read_file",
                    ("workspace", "search"): "search_files",
                    ("python", "run"): "run_python",
                    ("web", "search"): "web_search",
                    ("web", "fetch"): "web_fetch",
                    ("documents", "read"): "read_document",
                    ("system", "snapshot"): "system_status",
                    ("organizer", "add"): "add_task",
                    ("organizer", "list"): "list_tasks",
                }[(capability, action)]  # type: ignore[assignment]
            tool_result = self._capability_result(tool_action, result)
            tool_result.audit["session_id"] = session_id
            return tool_result

        return handle
