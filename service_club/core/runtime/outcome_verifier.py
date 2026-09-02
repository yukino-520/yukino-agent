from __future__ import annotations

import re
from dataclasses import dataclass

from service_club.core.types import ToolExecutionResult


# 作用：表示经过工具证据验收后的任务终态、摘要和检查明细。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“AgentOutcome”相关的数据结构、异常类型或服务组件。
# 字段：status：该对象中的结构化字段。、verified：该对象中的结构化字段。、summary：该对象中的结构化字段。、checks：该对象中的结构化字段。
class AgentOutcome:
    status: str
    verified: bool
    summary: str
    checks: dict[str, object]

    # 作用：将验收结论转换成可持久化和对外展示的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "verified": self.verified,
            "summary": self.summary,
            "checks": self.checks,
        }


# 作用：依据真实工具回执计算任务状态，防止模型文本冒充完成证据。
# 参数：无。
class AgentOutcomeVerifier:
    """Turns raw tool results into a deterministic, user-visible task outcome."""

    FILE_MUTATIONS = {
        "write_file",
        "edit_file",
        "confirm_file_operation",
        "rollback_file_operation",
    }
    COMPLETION_CLAIMS = re.compile(r"(?:已经|已|成功)(?:完成|创建|写入|修改|发送|执行)|修改完成|任务完成")

    # 作用：汇总确认、后台、失败、文件校验和契约证据，确定最终安全状态。
    # 参数 content：模型生成的最终回复文本，用于识别无证据完成声明。
    # 参数 tool_results：任务执行期间收集的真实工具回执列表。
    # 参数 degraded：本次回合是否以降级能力完成。
    # 参数 degradation_reason：本次回合发生能力降级的原因。
    # 参数 cancelled：任务是否已收到取消信号。
    # 参数 requirements：任务执行契约中的证据要求集合。
    def verify(
        self,
        *,
        content: str,
        tool_results: list[ToolExecutionResult],
        degraded: bool,
        degradation_reason: str,
        cancelled: bool = False,
        requirements: list[dict[str, object]] | None = None,
    ) -> AgentOutcome:
        requirements = requirements or []
        pending = [item for item in tool_results if item.audit.get("requires_confirmation")]
        background_pending = [
            item for item in tool_results if item.audit.get("background_job_pending")
        ]
        failed_indices = [
            index
            for index, item in enumerate(tool_results)
            if not item.success and not item.audit.get("requires_confirmation")
        ]
        failed_attempts = [tool_results[index] for index in failed_indices]
        successful = [item for item in tool_results if item.success]
        file_results = [
            item
            for item in successful
            if not item.audit.get("deduplicated")
            and (
                item.action in self.FILE_MUTATIONS
            or (
                item.audit.get("file_access")
                and (item.audit.get("created") or item.audit.get("confirmed"))
            )
            )
        ]
        unverified_files = [item for item in file_results if not item.audit.get("verified")]
        requirement_checks = [
            self._check_requirement(requirement, tool_results)
            for requirement in requirements
        ]
        missing_requirements = [
            item for item in requirement_checks if not item["satisfied"]
        ]
        required_side_effects_only = bool(requirements) and all(
            bool(requirement.get("side_effect"))
            for requirement in requirements
        )
        verified_effects_survive_response_degradation = bool(
            degraded
            and required_side_effects_only
            and not missing_requirements
            and all(
                int(check.get("successful_results", 0) or 0) > 0
                and not bool(check.get("waiting_confirmation"))
                for check in requirement_checks
            )
        )
        recovered_indices = {
            index
            for index in failed_indices
            if self._failure_recovered(
                index,
                tool_results[index],
                tool_results,
                requirements,
                requirement_checks,
            )
        }
        failed = [
            tool_results[index] for index in failed_indices if index not in recovered_indices
        ]
        if cancelled or any(item.audit.get("task_cancelled") for item in tool_results):
            status = "cancelled"
            summary = "任务已停止，不会继续调用后续工具。"
        elif pending:
            status = "waiting_confirmation"
            summary = "任务已暂停，正在等待你的确认。"
        elif background_pending:
            status = "waiting_background"
            summary = "任务已进入持久后台队列，完成后会回写验收结果。"
        elif failed or unverified_files or missing_requirements:
            status = "failed"
            summary = (
                "缺少完成委托所必需的工具证据，不能视为已经完成。"
                if missing_requirements
                else "执行验收没有通过，不能视为已经完成。"
            )
        elif degraded and not verified_effects_survive_response_degradation:
            status = "degraded"
            summary = "任务以降级模式结束，部分 Agent 能力不可用。"
        else:
            status = "completed"
            summary = (
                "要求的操作已经通过工具证据验收；模型回复阶段降级，不影响已完成结果。"
                if verified_effects_survive_response_degradation
                else "执行结果已经通过当前可用的自动检查。"
            )
        verified = status == "completed"
        return AgentOutcome(
            status=status,
            verified=verified,
            summary=summary,
            checks={
                "tool_count": len(tool_results),
                "successful_tools": len(successful),
                "failed_tools": len(failed),
                "failed_attempts": len(failed_attempts),
                "recovered_failures": len(recovered_indices),
                "pending_confirmations": len(pending),
                "pending_background_jobs": len(background_pending),
                "background_job_ids": list(
                    dict.fromkeys(
                        str(item.audit.get("background_job_id") or "")
                        for item in background_pending
                        if item.audit.get("background_job_id")
                    )
                ),
                "verified_file_writes": len(file_results) - len(unverified_files),
                "unverified_file_writes": len(unverified_files),
                "required_evidence": requirement_checks,
                "missing_evidence": [
                    {"key": item["key"], "label": item["label"]}
                    for item in missing_requirements
                ],
                "degradation_reason": degradation_reason,
                "response_degraded": degraded,
                "verified_effects_survive_response_degradation": (
                    verified_effects_survive_response_degradation
                ),
                "completion_claim_detected": bool(self.COMPLETION_CLAIMS.search(content)),
                "completion_claim_guarded": bool(
                    status not in {"completed", "waiting_confirmation"}
                    and self.COMPLETION_CLAIMS.search(content)
                ),
            },
        )

    # 作用：判断全部必需副作用是否已有成功回执，以避免模型重复执行。
    # 参数 requirements：任务执行契约中的证据要求集合。
    # 参数 tool_results：任务执行期间收集的真实工具回执列表。
    @classmethod
    # 作用：执行“side_effect_requirements_completed”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 requirements：调用方传入的requirements，用于本次处理。
    # 参数 tool_results：调用方传入的tool_results，用于本次处理。
    def side_effect_requirements_completed(
        cls,
        requirements: list[dict[str, object]],
        tool_results: list[ToolExecutionResult],
    ) -> bool:
        """Whether every required side effect already has successful evidence."""
        side_effects = [
            requirement
            for requirement in requirements
            if bool(requirement.get("side_effect"))
        ]
        if not side_effects:
            return False
        for requirement in side_effects:
            actions = {
                str(action)
                for action in requirement.get("actions", [])
                if str(action).strip()
            }
            audit_equals = (
                dict(requirement.get("audit_equals", {}))
                if isinstance(requirement.get("audit_equals"), dict)
                else {}
            )
            if not any(
                result.success
                and cls._result_matches(result, actions, audit_equals)
                for result in tool_results
            ):
                return False
        return True

    # 作用：将一项证据要求与工具回执匹配并生成可解释的验收明细。
    # 参数 requirement：当前需要核验的一项工具证据要求。
    # 参数 tool_results：任务执行期间收集的真实工具回执列表。
    @staticmethod
    # 作用：执行“check_requirement”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 requirement：调用方传入的requirement，用于本次处理。
    # 参数 tool_results：调用方传入的tool_results，用于本次处理。
    def _check_requirement(
        requirement: dict[str, object],
        tool_results: list[ToolExecutionResult],
    ) -> dict[str, object]:
        actions = {
            str(action)
            for action in requirement.get("actions", [])
            if str(action).strip()
        }
        audit_equals = (
            dict(requirement.get("audit_equals", {}))
            if isinstance(requirement.get("audit_equals"), dict)
            else {}
        )
        matching = [
            result
            for result in tool_results
            if AgentOutcomeVerifier._result_matches(result, actions, audit_equals)
        ]
        accepted = [
            result
            for result in matching
            if result.success or result.audit.get("requires_confirmation")
        ]
        return {
            "key": str(requirement.get("key", "required_evidence")),
            "label": str(requirement.get("label", "必要工具证据")),
            "actions": sorted(actions),
            "satisfied": bool(accepted),
            "matched_results": len(matching),
            "successful_results": sum(result.success for result in matching),
            "waiting_confirmation": any(
                result.audit.get("requires_confirmation") for result in matching
            ),
        }

    # 作用：判断早期失败是否已被后续同范围成功结果或等价证据修复。
    # 参数 index：失败工具结果在完整结果序列中的位置。
    # 参数 failure：需要登记或判断是否已恢复的失败信息。
    # 参数 tool_results：任务执行期间收集的真实工具回执列表。
    # 参数 requirements：任务执行契约中的证据要求集合。
    # 参数 requirement_checks：已经计算出的证据要求验收明细。
    @staticmethod
    # 作用：执行“failure_recovered”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 index：调用方传入的index，用于本次处理。
    # 参数 failure：调用方传入的failure，用于本次处理。
    # 参数 tool_results：调用方传入的tool_results，用于本次处理。
    # 参数 requirements：调用方传入的requirements，用于本次处理。
    # 参数 requirement_checks：调用方传入的requirement_checks，用于本次处理。
    def _failure_recovered(
        index: int,
        failure: ToolExecutionResult,
        tool_results: list[ToolExecutionResult],
        requirements: list[dict[str, object]],
        requirement_checks: list[dict[str, object]],
    ) -> bool:
        scope_keys = ("attachment_id", "capability", "operation", "path")
        failure_scope = {
            key: failure.audit[key]
            for key in scope_keys
            if key in failure.audit
        }
        if any(
            later.action == failure.action
            and (later.success or later.audit.get("requires_confirmation"))
            and all(later.audit.get(key) == value for key, value in failure_scope.items())
            for later in tool_results[index + 1 :]
        ):
            return True
        for requirement, check in zip(requirements, requirement_checks, strict=False):
            if not check.get("satisfied"):
                continue
            actions = {
                str(action)
                for action in requirement.get("actions", [])
                if str(action).strip()
            }
            audit_equals = (
                dict(requirement.get("audit_equals", {}))
                if isinstance(requirement.get("audit_equals"), dict)
                else {}
            )
            if AgentOutcomeVerifier._result_matches(failure, actions, audit_equals):
                return True
        return False

    # 作用：检查工具动作与指定审计字段是否共同满足证据约束。
    # 参数 result：需要评估、持久化或返回的执行结果。
    # 参数 actions：允许或需要匹配的动作集合。
    # 参数 audit_equals：工具回执必须满足的审计字段和值。
    @staticmethod
    # 作用：执行“result_matches”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 result：下游组件返回的原始结果对象。
    # 参数 actions：调用方传入的actions，用于本次处理。
    # 参数 audit_equals：调用方传入的audit_equals，用于本次处理。
    def _result_matches(
        result: ToolExecutionResult,
        actions: set[str],
        audit_equals: dict[str, object],
    ) -> bool:
        return result.action in actions and all(
            result.audit.get(key) == value for key, value in audit_equals.items()
        )
