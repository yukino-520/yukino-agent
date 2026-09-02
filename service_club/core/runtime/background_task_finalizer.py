"""Bridge durable background results back into the Agent task evidence ledger."""

from __future__ import annotations

import json
from typing import Any

from service_club.core.runtime.agent_task_store import AgentTaskStore
from service_club.core.runtime.audit_redaction import redact_for_audit
from service_club.core.runtime.outcome_verifier import AgentOutcomeVerifier
from service_club.core.types import ToolExecutionResult


# 作用：将持久后台工作流结果回写 Agent 步骤，并重新执行证据验收。
# 参数：无。
class BackgroundTaskFinalizer:
    # 作用：绑定任务账本和统一结果验收器。
    # 参数 tasks：持久 Agent 任务与步骤账本。
    # 参数 verifier：把工具回执转换为确定性任务终态的验收器。
    def __init__(
        self,
        tasks: AgentTaskStore,
        verifier: AgentOutcomeVerifier,
    ) -> None:
        self.tasks = tasks
        self.verifier = verifier

    # 作用：将一个后台工作流终态同步到作业关联的所有 Agent 任务。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    # 参数 workflow：后台工作流的最终状态、步骤与结果记录。
    def finalize_workflow(
        self,
        job: dict[str, Any],
        workflow: dict[str, Any],
    ) -> None:
        workflow_status = str(workflow.get("status") or "failed")
        for task_id in self.task_ids(job):
            self._finalize_task(
                task_id,
                job_id=str(job.get("id") or ""),
                workflow=workflow,
                workflow_status=workflow_status,
            )

    # 作用：把耗尽重试的后台作业转换为失败工作流并完成任务收尾。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    def dead_letter(self, job: dict[str, Any]) -> None:
        error = str(job.get("error") or "后台工作进程重试后仍未完成。")
        workflow = {
            "id": str((job.get("payload") or {}).get("workflow_run_id") or ""),
            "status": "failed",
            "steps": [],
            "error": error,
        }
        self.finalize_workflow(job, workflow)

    # 作用：从作业及其载荷中提取并去重直接和关联任务 ID。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    @staticmethod
    # 作用：执行“task_ids”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 job：调用方传入的job，用于本次处理。
    def task_ids(job: dict[str, Any]) -> list[str]:
        payload = job.get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        return list(
            dict.fromkeys(
                task_id
                for task_id in (
                    str(job.get("task_id") or payload.get("source_task_id") or ""),
                    str(
                        job.get("related_task_id")
                        or payload.get("request_task_id")
                        or ""
                    ),
                )
                if task_id
            )
        )

    # 作用：解析工作流证据、关闭等待步骤，并据契约更新任务最终状态。
    # 参数 task_id：Agent 持久任务的唯一标识。
    # 参数 job_id：后台作业的唯一标识。
    # 参数 workflow：后台工作流的最终状态、步骤与结果记录。
    # 参数 workflow_status：后台工作流的终态名称。
    def _finalize_task(
        self,
        task_id: str,
        *,
        job_id: str,
        workflow: dict[str, Any],
        workflow_status: str,
    ) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        waiting_step = next(
            (
                step
                for step in reversed(task.get("steps", []))
                if isinstance(step, dict) and step.get("status") == "waiting_background"
            ),
            None,
        )
        if waiting_step is None:
            return
        action = str(waiting_step.get("action") or "capability_call")
        success = workflow_status == "completed"
        error = "" if success else str(workflow.get("error") or "后台工作流没有完成。")
        result = ToolExecutionResult(
            action=action,  # type: ignore[arg-type]
            success=success,
            content=(
                json.dumps(workflow, ensure_ascii=False, indent=2)[:24_000]
                if success
                else ""
            ),
            error=error[:4000],
            audit={
                "capability": "workflows",
                "operation": "enqueue",
                "background_job_id": job_id,
                "background_job_pending": False,
                "background_job_status": (
                    "completed" if success else workflow_status
                ),
                "workflow_run_id": workflow.get("id", ""),
                "workflow_status": workflow_status,
                "workflow_steps": [
                    {
                        "id": step.get("id", ""),
                        "capability": step.get("capability", ""),
                        "operation": step.get("action", ""),
                        "status": step.get("status", "unknown"),
                        "attempts": step.get("attempts", 0),
                    }
                    for step in workflow.get("steps", [])
                    if isinstance(step, dict)
                ],
            },
        )
        step_status = (
            "completed"
            if success
            else "cancelled"
            if workflow_status == "cancelled"
            else "interrupted"
            if workflow_status == "interrupted"
            else "failed"
        )
        self.tasks.resolve_background_step(
            task_id,
            status=step_status,
            output=redact_for_audit(result.model_dump(mode="json")),
            error=error,
        )
        refreshed = self.tasks.get(task_id)
        if refreshed is None:
            return
        if workflow_status in {"cancelled", "interrupted"}:
            outcome = {
                "status": workflow_status,
                "verified": False,
                "summary": (
                    "后台工作流已经停止。"
                    if workflow_status == "cancelled"
                    else "服务中断了后台工作流；不确定的副作用没有自动重放。"
                ),
                "checks": {"background_job_id": job_id},
            }
            self.tasks.mark(
                task_id,
                status=workflow_status,
                phase=workflow_status,
                outcome=outcome,
                error=error,
            )
            return

        results: list[ToolExecutionResult] = []
        for step in refreshed.get("steps", []):
            output = step.get("output") if isinstance(step, dict) else None
            if not isinstance(output, dict) or not output.get("action"):
                continue
            try:
                results.append(ToolExecutionResult.model_validate(output))
            except (TypeError, ValueError):
                continue
        contract = (
            refreshed.get("contract")
            if isinstance(refreshed.get("contract"), dict)
            else {}
        )
        requirements = (
            contract.get("requirements", [])
            if isinstance(contract.get("requirements"), list)
            else []
        )
        outcome = self.verifier.verify(
            content="",
            tool_results=results,
            degraded=False,
            degradation_reason="",
            requirements=requirements,
        )
        self.tasks.mark(
            task_id,
            status=outcome.status,
            phase="verified" if outcome.status == "completed" else "verification_failed",
            outcome=outcome.as_dict(),
            error="" if outcome.status == "completed" else error or outcome.summary,
        )
