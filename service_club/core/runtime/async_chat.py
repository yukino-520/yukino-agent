"""Durable asynchronous execution for complete Agent chat turns."""

from __future__ import annotations

import os
from typing import Any

from service_club.core.runtime.agent_request_store import AgentRequestStore
from service_club.core.runtime.background_jobs import (
    BackgroundJobCapacityError,
    BackgroundJobError,
    BackgroundJobStore,
)
from service_club.core.types import ChatRequest


# 作用：表示异步聊天委托无法安全入队、恢复或执行。
# 参数：无。
class AsyncChatError(RuntimeError):
    pass


# 作用：表示全局或单会话后台队列已达到接纳上限。
# 参数：无。
class AsyncChatOverloaded(AsyncChatError):
    # 作用：保存触发容量限制的范围和阈值，供接口返回明确的过载信息。
    # 参数 message：异常向调用方说明的可读错误信息。
    # 参数 scope：触发容量限制的全局类型或单会话范围。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def __init__(self, message: str, *, scope: str, limit: int) -> None:
        super().__init__(message)
        self.scope = scope
        self.limit = limit


# 作用：将完整聊天回合映射为持久后台作业，支持查询、取消和重启安全恢复。
# 参数：无。
class AsyncChatRuntime:
    job_kind = "agent.chat"

    # 作用：绑定 Agent 核心与共享后台作业账本。
    # 参数 core：共享的 Agent 核心运行时实例。
    # 参数 jobs：持久后台作业仓库。
    def __init__(self, core: Any, jobs: BackgroundJobStore) -> None:
        self.core = core
        self.jobs = jobs

    # 作用：从环境变量读取并夹紧队列容量配置。
    # 参数 name：要读取的环境变量名称。
    # 参数 default：配置缺失或格式无效时采用的默认值。
    # 参数 minimum：环境配置允许的最小值。
    # 参数 maximum：环境配置允许的最大值。
    @staticmethod
    # 作用：执行“bounded_env”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 name：调用方传入的name，用于本次处理。
    # 参数 default：输入缺失或无效时使用的默认值。
    # 参数 minimum：调用方传入的minimum，用于本次处理。
    # 参数 maximum：调用方传入的maximum，用于本次处理。
    def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    # 作用：返回全局 Agent 队列和单会话并发排队上限。
    # 参数：无。
    def limits(self) -> dict[str, int]:
        return {
            "capacity": self._bounded_env(
                "YUKINO_AGENT_QUEUE_CAPACITY",
                64,
                4,
                1000,
            ),
            "per_session": self._bounded_env(
                "YUKINO_AGENT_SESSION_QUEUE_LIMIT",
                3,
                1,
                20,
            ),
        }

    # 作用：汇总当前活跃作业数量和剩余接纳容量。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        limits = self.limits()
        active = self.jobs.active_count(kind=self.job_kind)
        return {
            **limits,
            "active": active,
            "available": max(0, limits["capacity"] - active),
        }

    # 作用：校验请求幂等性与容量后，创建或恢复任务并原子加入持久队列。
    # 参数 request：准备进入持久队列的完整聊天请求对象。
    def enqueue(self, request: ChatRequest) -> dict[str, Any]:
        request_id = request.request_id.strip()
        if not request_id:
            raise AsyncChatError("异步 Agent 委托必须提供 request_id。")
        request_payload = request.model_dump(mode="json")
        request_hash = AgentRequestStore.fingerprint(
            request.model_dump(mode="json", exclude={"request_id"})
        )
        dedupe_key = f"{request.session_id}:{request_id}"
        existing_job = self.jobs.get_by_dedupe(self.job_kind, dedupe_key)
        if existing_job is not None:
            self._ensure_same_request(existing_job, request_hash)
            return self.public_job(existing_job)
        limits = self.limits()
        active = self.jobs.active_count(kind=self.job_kind)
        if active >= limits["capacity"]:
            raise AsyncChatOverloaded(
                "Agent 队列暂时已满，请稍后再提交。",
                scope="kind",
                limit=limits["capacity"],
            )
        session_active = self.jobs.active_count(
            kind=self.job_kind,
            session_id=request.session_id,
        )
        if session_active >= limits["per_session"]:
            raise AsyncChatOverloaded(
                "当前会话已有多条委托等待或执行，请等待前面的委托完成。",
                scope="session",
                limit=limits["per_session"],
            )

        if request.resume_task_id:
            task = self.core.agent_tasks.get(request.resume_task_id)
            if task is None or task.get("session_id") != request.session_id:
                raise AsyncChatError("要恢复的 Agent 任务不存在或不属于当前会话。")
            task_id = str(task["id"])
        else:
            latest_user = next(
                (message for message in reversed(request.messages) if message.role == "user"),
                None,
            )
            task = self.core.agent_tasks.create(
                session_id=request.session_id,
                request_id=request_id,
                goal=latest_user.content if latest_user is not None else "继续会话",
                request=request_payload,
            )
            stored_request = task.get("request")
            if isinstance(stored_request, dict):
                stored_hash = AgentRequestStore.fingerprint(
                    {
                        key: value
                        for key, value in stored_request.items()
                        if key != "request_id"
                    }
                )
                if stored_hash != request_hash:
                    raise AsyncChatError(
                        "同一个 request_id 已经属于另一条 Agent 委托。"
                    )
            task_id = str(task["id"])
            if task.get("status") in {"created", "queued"}:
                self.core.agent_tasks.mark(
                    task_id,
                    status="queued",
                    phase="background_queued",
                    outcome={
                        "status": "queued",
                        "verified": False,
                        "summary": "委托已经进入持久 Agent 队列。",
                        "checks": {},
                    },
                )
        try:
            job = self.jobs.enqueue(
                self.job_kind,
                {
                    "request": request_payload,
                    "request_hash": request_hash,
                    "request_id": request_id,
                },
                session_id=request.session_id,
                task_id=task_id,
                dedupe_key=dedupe_key,
                max_attempts=1,
                max_active_kind=limits["capacity"],
                max_active_session=limits["per_session"],
            )
        except BackgroundJobCapacityError as exc:
            if not request.resume_task_id:
                self.core.agent_tasks.mark(
                    task_id,
                    status="failed",
                    phase="queue_rejected",
                    error=str(exc),
                )
            raise AsyncChatOverloaded(
                str(exc),
                scope=exc.scope,
                limit=exc.limit,
            ) from exc
        except BackgroundJobError as exc:
            if not request.resume_task_id:
                self.core.agent_tasks.mark(
                    task_id,
                    status="failed",
                    phase="queue_failed",
                    error=str(exc),
                )
            raise AsyncChatError(str(exc)) from exc
        self._ensure_same_request(job, request_hash)
        return self.public_job(job)

    # 作用：核对去重作业的请求指纹，阻止同 request_id 承载不同委托。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    # 参数 request_hash：排除 request_id 后计算的请求内容指纹。
    @staticmethod
    # 作用：执行“ensure_same_request”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 job：调用方传入的job，用于本次处理。
    # 参数 request_hash：调用方传入的request_hash，用于本次处理。
    def _ensure_same_request(job: dict[str, Any], request_hash: str) -> None:
        payload = job.get("payload")
        stored_hash = str(payload.get("request_hash") or "") if isinstance(payload, dict) else ""
        if not stored_hash or stored_hash != request_hash:
            raise AsyncChatError("同一个 request_id 不能用于不同的聊天内容。")

    # 作用：从作业载荷恢复聊天请求并执行；中断过的副作用任务不自动重放。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        if int(job.get("attempts") or 0) > 1:
            raise AsyncChatError(
                "异步 Agent 曾在运行中中断；为避免重复副作用，没有自动重放。"
            )
        payload = job.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
            raise AsyncChatError("异步 Agent 委托参数损坏。")
        current = self.jobs.get(str(job.get("id") or ""))
        if current is None or current.get("status") == "cancelled":
            raise AsyncChatError("异步 Agent 委托已经停止。")
        request = ChatRequest.model_validate(payload["request"])
        response = self.core.process(request)
        return response.model_dump(mode="json")

    # 作用：将未自行进入安全终态的死信作业回写为任务失败。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    def dead_letter(self, job: dict[str, Any]) -> None:
        task_id = str(job.get("task_id") or "")
        task = self.core.agent_tasks.get(task_id) if task_id else None
        if task is None or task.get("status") in {
            "failed",
            "cancelled",
            "interrupted",
            "completed",
            "degraded",
            "waiting_confirmation",
            "waiting_background",
        }:
            return
        error = str(job.get("error") or "异步 Agent 作业未能完成。")
        self.core.agent_tasks.mark(
            task_id,
            status="failed",
            phase="background_failed",
            outcome={
                "status": "failed",
                "verified": False,
                "summary": "异步 Agent 作业未能完成，未自动重放。",
                "checks": {"background_job_id": str(job.get("id") or "")},
            },
            error=error,
        )

    # 作用：取消会话所属异步作业，并级联停止任务、确认单和等待步骤。
    # 参数 job_id：后台作业的唯一标识。
    # 参数 session_id：当前用户会话的唯一标识。
    def cancel(self, job_id: str, *, session_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.get("kind") != self.job_kind
            or job.get("session_id") != session_id
        ):
            return None
        cancelled = self.jobs.cancel(job_id, session_id=session_id)
        if cancelled is None or cancelled.get("status") != "cancelled":
            return cancelled
        task_id = str(job.get("task_id") or "")
        task = self.core.agent_tasks.cancel(task_id, session_id=session_id) if task_id else None
        if task is not None:
            self.core.tools.registry.operations.cancel_by_task(task_id)
            self.core.agent_tasks.resolve_waiting_step(
                task_id,
                status="cancelled",
                error="用户停止了异步 Agent 委托。",
            )
            self.core.agent_tasks.mark(
                task_id,
                status="cancelled",
                phase="cancelled",
                outcome={
                    "status": "cancelled",
                    "verified": False,
                    "summary": "异步 Agent 委托已经停止。",
                    "checks": {"background_job_id": job_id},
                },
            )
        return self.jobs.get(job_id)

    # 作用：过滤内部载荷并生成面向 REST/WebSocket 客户端的作业视图。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    def public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(job["id"]),
            "status": str(job["status"]),
            "task_id": str(job.get("task_id") or ""),
            "request_id": str((job.get("payload") or {}).get("request_id") or ""),
            "attempts": int(job.get("attempts") or 0),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "completed_at": job.get("completed_at"),
            "queue_position": self.jobs.queue_position(str(job["id"])),
            "result": job.get("result") if job.get("status") == "completed" else {},
            "error": str(job.get("error") or ""),
        }
