from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv
from openai import OpenAI

from service_club.core.conversation.characters import CharacterProfile
from service_club.core.conversation.emotion import EmotionResult
from service_club.core.conversation.prompts import build_system_prompt
from service_club.core.runtime.agent_task_store import (
    AgentTaskBudgetExceeded,
    current_agent_task_id,
)
from service_club.core.runtime.model_resilience import (
    ModelCircuitOpen,
    ModelConcurrencyLimit,
    ModelEmptyResponse,
    ModelHealthMonitor,
    ModelRequestCancelled,
    ModelWaitTimeout,
    ProviderFailure,
    classify_provider_failure,
)
from service_club.core.runtime.tool_output_security import ToolOutputSecurity
from service_club.core.runtime.usage import UsageLedger
from service_club.core.types import (
    ChatMessage,
    ChatMode,
    ConversationMode,
    SafetyLevel,
    ToolAction,
    ToolExecutionResult,
)

if TYPE_CHECKING:
    from service_club.core.tooling.tool_registry import ServiceClubToolRegistry

load_dotenv()


# 作用：保存模型最终文本、降级信息、工具证据和本轮运行摘要。
# 参数 content：最终展示给用户的模型文本。
# 参数 degraded：本轮是否使用了降级或兜底路径。
# 参数 degradation_reason：稳定的降级原因代码。
# 参数 tool_results：模型循环中已经执行并保留的工具结果。
# 参数 model_runtime：实际模型、备用切换和尝试记录等运行摘要。
@dataclass(frozen=True)
# 作用：定义“ModelReply”相关的数据结构、异常类型或服务组件。
# 字段：content：该对象中的结构化字段。、degraded：该对象中的结构化字段。、degradation_reason：该对象中的结构化字段。、tool_results：该对象中的结构化字段。、model_runtime：该对象中的结构化字段。
class ModelReply:
    content: str
    degraded: bool = False
    degradation_reason: str = ""
    tool_results: tuple[ToolExecutionResult, ...] = field(default_factory=tuple)
    model_runtime: dict[str, Any] = field(default_factory=dict)


# 作用：表示模型轮次在已有工具结果后中断，使上层能够保留证据并禁止整轮重放。
class ModelTurnInterrupted(RuntimeError):
    # 作用：保存原始异常、已经提交的工具结果以及中断来源。
    # 参数 cause：触发当前轮次中断的底层模型或工具异常。
    # 参数 tool_results：中断前已经执行完成、不能丢失或盲目重放的工具结果。
    # 参数 source：异常来源，使用 model 或 tool 区分容错策略。
    def __init__(
        self,
        cause: Exception,
        tool_results: tuple[ToolExecutionResult, ...],
        *,
        source: str = "model",
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.tool_results = tool_results
        self.source = source


# 作用：封装 OpenAI 兼容模型调用、模型容错和有界的“模型—工具—观察”循环。
# 构造参数：usage 可注入模型 token 用量账本。
class ServiceClubModel:
    # 作用：从环境配置模型客户端、超时、并发限制、循环步数与健康监控器。
    # 参数 usage：可选用量账本；用于记录每次模型调用的输入与输出 token。
    def __init__(self, usage: UsageLedger | None = None) -> None:
        self.usage = usage
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        self.base_url = base_url or ""
        if api_key:
            kwargs = {}
            if base_url:
                kwargs["base_url"] = base_url
            self.client = OpenAI(api_key=api_key, **kwargs)
        else:
            self.client = None
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        self.fallback_model = os.environ.get("OPENAI_FALLBACK_MODEL", "")
        self.timeout_seconds = self._bounded_env_float(
            "OPENAI_TIMEOUT_SECONDS",
            12,
            1,
            120,
        )
        self.max_agent_steps = self._bounded_env_int(
            "YUKINO_AGENT_MAX_STEPS",
            4,
            1,
            8,
        )
        self.max_concurrent_calls = self._bounded_env_int(
            "YUKINO_MODEL_MAX_CONCURRENT",
            4,
            1,
            16,
        )
        self.health = ModelHealthMonitor(
            failure_threshold=self._bounded_env_int(
                "YUKINO_MODEL_CIRCUIT_FAILURES",
                2,
                1,
                10,
            ),
            cooldown_seconds=self._bounded_env_float(
                "YUKINO_MODEL_CIRCUIT_COOLDOWN_SECONDS",
                30,
                5,
                600,
            ),
        )
        self._model_slots = threading.BoundedSemaphore(self.max_concurrent_calls)
        self._active_calls = 0
        self._active_lock = threading.Lock()
        self.tool_registry: ServiceClubToolRegistry | None = None
        self.output_security = ToolOutputSecurity()

    # 作用：绑定模型可调用的统一工具注册表。
    # 参数 registry：保存工具 schema、handler、权限和任务账本的注册表。
    def bind_tool_registry(self, registry: ServiceClubToolRegistry) -> None:
        self.tool_registry = registry

    # 作用：绑定工具输出隔离与模型动作授权组件。
    # 参数 security：负责提示注入检测、不可信输出包装和副作用授权的安全器。
    def bind_tool_output_security(self, security: ToolOutputSecurity) -> None:
        self.output_security = security

    # 作用：构造角色系统提示，并在主模型与备用模型之间执行带容错的一轮回复。
    # 参数 messages：已经裁剪好的用户与助手历史消息。
    # 参数 conversation_mode：日常、委托、安静等对话策略模式。
    # 参数 safety_level：正常、高风险或危机安全等级。
    # 参数 chat_mode：单角色私聊或多角色群像模式。
    # 参数 character：本轮主角色的人设和语言风格配置。
    # 参数 emotion：当前用户情绪标签、强度和倾向。
    # 参数 memories：允许注入提示的相关记忆文本。
    # 参数 tool_context：模型前工具结果和其他受控观察文本。
    # 参数 session_id：工具调用和任务记录归属的会话标识。
    # 参数 allowed_tools：当前角色允许暴露和执行的工具白名单。
    # 参数 enable_agent_tools：是否向模型提供原生工具定义。
    # 参数 authorization_requirements：用户本轮执行契约中的授权和证据要求。
    # 参数 untrusted_context：上下文中是否已经包含不可信外部数据。
    # 参数 injection_detected_context：上下文中是否已经发现提示注入信号。
    # 返回：包含最终文本、降级信息和工具证据的 ModelReply。
    def reply(
        self,
        *,
        messages: list[ChatMessage],
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        chat_mode: ChatMode,
        character: CharacterProfile,
        emotion: EmotionResult,
        memories: list[str],
        tool_context: list[str],
        session_id: str = "default",
        allowed_tools: tuple[ToolAction, ...] | None = None,
        enable_agent_tools: bool = True,
        authorization_requirements: tuple[dict[str, Any], ...] | None = None,
        untrusted_context: bool = False,
        injection_detected_context: bool = False,
    ) -> ModelReply:
        if self.client is None:
            self._task_event(
                "model_unavailable",
                status="degraded",
                phase="model_waiting",
                detail="模型 API Key 尚未配置",
            )
            return ModelReply(
                content=character.fallback_for(emotion.label),
                degraded=True,
                degradation_reason="missing_api_key",
                model_runtime=self._turn_runtime(
                    model_used="",
                    fallback_used=False,
                    attempts=[],
                ),
            )

        system_prompt = build_system_prompt(
            conversation_mode=conversation_mode,
            safety_level=safety_level,
            chat_mode=chat_mode,
            character=character,
            emotion=emotion,
            memories=memories,
            tool_context=tool_context,
        )
        models = list(dict.fromkeys(name for name in (self.model, self.fallback_model) if name))
        attempts: list[dict[str, Any]] = []
        failures: list[ProviderFailure] = []
        for index, model_name in enumerate(models):
            fallback_used = index > 0
            started = time.monotonic()
            try:
                self.health.begin(model_name)
            except ModelCircuitOpen as exc:
                self._task_event(
                    "model_skipped",
                    status="circuit_open",
                    phase="model_waiting",
                    action=model_name,
                    detail=f"{model_name} 当前熔断，跳过本次调用",
                    payload={"retry_after_seconds": round(exc.retry_after, 3)},
                )
                attempts.append(
                    {
                        "model": model_name,
                        "status": "circuit_open",
                        "retry_after_seconds": round(exc.retry_after, 3),
                        "fallback": fallback_used,
                    }
                )
                continue
            self._task_event(
                "model_started",
                status="running",
                phase="model_waiting",
                action=model_name,
                detail=f"正在请求 {model_name}",
                payload={"fallback": fallback_used},
            )
            try:
                reply = self._reply_with_model(
                    model_name=model_name,
                    system_prompt=system_prompt,
                    messages=messages,
                    safety_level=safety_level,
                    character=character,
                    session_id=session_id,
                    allowed_tools=allowed_tools,
                    enable_agent_tools=enable_agent_tools,
                    authorization_requirements=authorization_requirements,
                    untrusted_context=untrusted_context,
                    injection_detected_context=injection_detected_context,
                )
            except ModelRequestCancelled:
                self.health.abort(model_name)
                self._task_event(
                    "model_cancelled",
                    status="cancelled",
                    phase="cancelling",
                    action=model_name,
                    detail="已停止等待模型返回",
                )
                attempts.append(
                    {
                        "model": model_name,
                        "status": "cancelled",
                        "fallback": fallback_used,
                    }
                )
                return ModelReply(
                    content="这次委托已经停止，不再等待模型服务返回。",
                    degraded=True,
                    degradation_reason="task_cancelled",
                    model_runtime=self._turn_runtime(
                        model_used=model_name,
                        fallback_used=fallback_used,
                        attempts=attempts,
                    ),
                )
            except ModelConcurrencyLimit:
                self.health.abort(model_name)
                self._task_event(
                    "model_capacity_wait_failed",
                    status="degraded",
                    phase="model_waiting",
                    action=model_name,
                    detail="等待模型并发槽位超时",
                )
                attempts.append(
                    {
                        "model": model_name,
                        "status": "local_capacity",
                        "fallback": fallback_used,
                    }
                )
                return ModelReply(
                    content=character.fallback_for(emotion.label),
                    degraded=True,
                    degradation_reason="model_concurrency_limit",
                    model_runtime=self._turn_runtime(
                        model_used="",
                        fallback_used=False,
                        attempts=attempts,
                    ),
                )
            except AgentTaskBudgetExceeded as exc:
                self.health.abort(model_name)
                self._task_event(
                    "budget_exceeded",
                    status="degraded",
                    phase="budget_exceeded",
                    action=model_name,
                    detail=str(exc),
                    payload={
                        "resource": exc.resource,
                        "limit": exc.limit,
                        "used": exc.used,
                    },
                )
                attempts.append(
                    {
                        "model": model_name,
                        "status": "budget_exceeded",
                        "resource": exc.resource,
                        "fallback": fallback_used,
                    }
                )
                return ModelReply(
                    content=f"{exc} 请在设置中提高额度后恢复原任务。",
                    degraded=True,
                    degradation_reason="task_budget_exceeded",
                    model_runtime=self._turn_runtime(
                        model_used=model_name,
                        fallback_used=fallback_used,
                        attempts=attempts,
                    ),
                )
            except ModelTurnInterrupted as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                if isinstance(exc.cause, AgentTaskBudgetExceeded):
                    self.health.abort(model_name)
                    budget_error = exc.cause
                    self._task_event(
                        "budget_exceeded",
                        status="degraded",
                        phase="budget_exceeded",
                        action=model_name,
                        detail=str(budget_error),
                        payload={
                            "resource": budget_error.resource,
                            "limit": budget_error.limit,
                            "used": budget_error.used,
                        },
                    )
                    attempts.append(
                        {
                            "model": model_name,
                            "status": "budget_exceeded",
                            "resource": budget_error.resource,
                            "fallback": fallback_used,
                            "after_tools": True,
                        }
                    )
                    return ModelReply(
                        content=f"{budget_error} 已完成的工具步骤仍保留；提高额度后可恢复原任务。",
                        degraded=True,
                        degradation_reason="task_budget_exceeded",
                        tool_results=exc.tool_results,
                        model_runtime=self._turn_runtime(
                            model_used=model_name,
                            fallback_used=fallback_used,
                            attempts=attempts,
                        ),
                    )
                if isinstance(exc.cause, ModelRequestCancelled):
                    self.health.abort(model_name)
                    self._task_event(
                        "model_cancelled",
                        status="cancelled",
                        phase="cancelling",
                        action=model_name,
                        detail="工具执行记录已保留，已停止等待模型",
                    )
                    attempts.append(
                        {
                            "model": model_name,
                            "status": "cancelled",
                            "fallback": fallback_used,
                            "after_tools": True,
                        }
                    )
                    return ModelReply(
                        content="这次委托已经停止；停止前完成的工具步骤保留在执行记录中。",
                        degraded=True,
                        degradation_reason="task_cancelled",
                        tool_results=exc.tool_results,
                        model_runtime=self._turn_runtime(
                            model_used=model_name,
                            fallback_used=fallback_used,
                            attempts=attempts,
                        ),
                    )
                if isinstance(exc.cause, ModelConcurrencyLimit):
                    self.health.abort(model_name)
                    self._task_event(
                        "model_capacity_wait_failed",
                        status="degraded",
                        phase="model_waiting",
                        action=model_name,
                        detail="工具记录已保留，但没有可用模型并发槽位",
                    )
                    attempts.append(
                        {
                            "model": model_name,
                            "status": "local_capacity",
                            "fallback": fallback_used,
                            "after_tools": True,
                        }
                    )
                    return ModelReply(
                        content="工具步骤已经保留，但暂时没有可用的模型并发槽位来整理结果。",
                        degraded=True,
                        degradation_reason="model_concurrency_limit",
                        tool_results=exc.tool_results,
                        model_runtime=self._turn_runtime(
                            model_used=model_name,
                            fallback_used=fallback_used,
                            attempts=attempts,
                        ),
                    )
                if exc.source == "tool":
                    self.health.success(
                        model_name,
                        latency_ms=latency_ms,
                        fallback=fallback_used,
                    )
                    self._task_event(
                        "tool_execution_failed",
                        status="failed",
                        phase="executing",
                        action=model_name,
                        detail="模型请求成功，但工具执行发生内部错误",
                    )
                    attempts.append(
                        {
                            "model": model_name,
                            "status": "tool_execution_failed",
                            "latency_ms": latency_ms,
                            "fallback": fallback_used,
                            "after_tools": True,
                        }
                    )
                    return ModelReply(
                        content="工具执行发生内部错误；为避免重复副作用，这一轮没有切换模型重试。",
                        degraded=True,
                        degradation_reason="tool_execution_error",
                        tool_results=exc.tool_results,
                        model_runtime=self._turn_runtime(
                            model_used=model_name,
                            fallback_used=fallback_used,
                            attempts=attempts,
                        ),
                    )
                failure = classify_provider_failure(exc.cause)
                self.health.failure(model_name, failure, latency_ms=latency_ms)
                self._task_event(
                    "model_failed",
                    status="failed",
                    phase="model_waiting",
                    action=model_name,
                    detail=f"{model_name} 调用失败，正在评估是否切换备用模型",
                    payload={"failure": failure.code, "fallback": fallback_used},
                )
                attempts.append(
                    self._failed_attempt(
                        model_name=model_name,
                        failure=failure,
                        latency_ms=latency_ms,
                        fallback=fallback_used,
                        after_tools=True,
                    )
                )
                return ModelReply(
                    content=character.fallback_for(emotion.label),
                    degraded=True,
                    degradation_reason=self._degradation_reason(failure),
                    tool_results=exc.tool_results,
                    model_runtime=self._turn_runtime(
                        model_used=model_name,
                        fallback_used=fallback_used,
                        attempts=attempts,
                    ),
                )
            except Exception as first_error:
                error: Exception = first_error
                if enable_agent_tools and self._is_tool_compatibility_error(first_error):
                    try:
                        plain_reply = self._reply_with_model(
                            model_name=model_name,
                            system_prompt=system_prompt,
                            messages=messages,
                            safety_level=safety_level,
                            character=character,
                            session_id=session_id,
                            allowed_tools=allowed_tools,
                            enable_agent_tools=False,
                            authorization_requirements=authorization_requirements,
                            untrusted_context=untrusted_context,
                            injection_detected_context=injection_detected_context,
                        )
                    except ModelRequestCancelled:
                        self.health.abort(model_name)
                        self._task_event(
                            "model_cancelled",
                            status="cancelled",
                            phase="cancelling",
                            action=model_name,
                            detail="已停止等待模型兼容模式返回",
                        )
                        attempts.append(
                            {
                                "model": model_name,
                                "status": "cancelled",
                                "fallback": fallback_used,
                            }
                        )
                        return ModelReply(
                            content="这次委托已经停止，不再等待模型服务返回。",
                            degraded=True,
                            degradation_reason="task_cancelled",
                            model_runtime=self._turn_runtime(
                                model_used=model_name,
                                fallback_used=fallback_used,
                                attempts=attempts,
                            ),
                        )
                    except ModelConcurrencyLimit:
                        self.health.abort(model_name)
                        self._task_event(
                            "model_capacity_wait_failed",
                            status="degraded",
                            phase="model_waiting",
                            action=model_name,
                            detail="模型兼容模式没有取得可用并发槽位",
                        )
                        return ModelReply(
                            content=character.fallback_for(emotion.label),
                            degraded=True,
                            degradation_reason="model_concurrency_limit",
                            model_runtime=self._turn_runtime(
                                model_used="",
                                fallback_used=False,
                                attempts=attempts,
                            ),
                        )
                    except AgentTaskBudgetExceeded as exc:
                        self.health.abort(model_name)
                        self._task_event(
                            "budget_exceeded",
                            status="degraded",
                            phase="budget_exceeded",
                            action=model_name,
                            detail=str(exc),
                            payload={
                                "resource": exc.resource,
                                "limit": exc.limit,
                                "used": exc.used,
                            },
                        )
                        attempts.append(
                            {
                                "model": model_name,
                                "status": "budget_exceeded",
                                "resource": exc.resource,
                                "fallback": fallback_used,
                                "compatibility_retry": True,
                            }
                        )
                        return ModelReply(
                            content=f"{exc} 请提高额度后恢复原任务。",
                            degraded=True,
                            degradation_reason="task_budget_exceeded",
                            model_runtime=self._turn_runtime(
                                model_used=model_name,
                                fallback_used=fallback_used,
                                attempts=attempts,
                            ),
                        )
                    except ModelTurnInterrupted as exc:
                        error = exc.cause
                    except Exception as plain_error:
                        error = plain_error
                    else:
                        latency_ms = int((time.monotonic() - started) * 1000)
                        self.health.success(
                            model_name,
                            latency_ms=latency_ms,
                            fallback=fallback_used,
                        )
                        self._task_event(
                            "model_succeeded",
                            status="completed",
                            phase="model_waiting",
                            action=model_name,
                            detail=f"{model_name} 已通过无工具兼容模式返回",
                            payload={
                                "latency_ms": latency_ms,
                                "fallback": fallback_used,
                                "tool_compatibility_mode": True,
                            },
                        )
                        attempts.append(
                            {
                                "model": model_name,
                                "status": "tool_compatibility_fallback",
                                "latency_ms": latency_ms,
                                "fallback": fallback_used,
                            }
                        )
                        return replace(
                            plain_reply,
                            degraded=True,
                            degradation_reason="tool_calling_unavailable",
                            model_runtime=self._turn_runtime(
                                model_used=model_name,
                                fallback_used=fallback_used,
                                attempts=attempts,
                            ),
                        )
                failure = classify_provider_failure(error)
                failures.append(failure)
                latency_ms = int((time.monotonic() - started) * 1000)
                self.health.failure(model_name, failure, latency_ms=latency_ms)
                self._task_event(
                    "model_failed",
                    status="failed",
                    phase="model_waiting",
                    action=model_name,
                    detail=f"{model_name} 调用失败，正在评估是否切换备用模型",
                    payload={"failure": failure.code, "fallback": fallback_used},
                )
                attempts.append(
                    self._failed_attempt(
                        model_name=model_name,
                        failure=failure,
                        latency_ms=latency_ms,
                        fallback=fallback_used,
                    )
                )
                continue
            if reply.degradation_reason == "task_cancelled":
                self.health.abort(model_name)
                attempts.append(
                    {
                        "model": model_name,
                        "status": "cancelled",
                        "fallback": fallback_used,
                    }
                )
                return replace(
                    reply,
                    model_runtime=self._turn_runtime(
                        model_used=model_name,
                        fallback_used=fallback_used,
                        attempts=attempts,
                    ),
                )
            latency_ms = int((time.monotonic() - started) * 1000)
            self.health.success(
                model_name,
                latency_ms=latency_ms,
                fallback=fallback_used,
            )
            self._task_event(
                "model_succeeded",
                status="completed",
                phase="model_waiting",
                action=model_name,
                detail=(
                    f"备用模型 {model_name} 已返回结果"
                    if fallback_used
                    else f"主模型 {model_name} 已返回结果"
                ),
                payload={"latency_ms": latency_ms, "fallback": fallback_used},
            )
            attempts.append(
                {
                    "model": model_name,
                    "status": "success",
                    "latency_ms": latency_ms,
                    "fallback": fallback_used,
                }
            )
            return replace(
                reply,
                model_runtime=self._turn_runtime(
                    model_used=model_name,
                    fallback_used=fallback_used,
                    attempts=attempts,
                ),
            )
        failure = failures[-1] if failures else None
        reason = self._degradation_reason(failure) if failure else "model_circuit_open"
        return ModelReply(
            content=character.fallback_for(emotion.label),
            degraded=True,
            degradation_reason=reason,
            model_runtime=self._turn_runtime(
                model_used="",
                fallback_used=False,
                attempts=attempts,
            ),
        )

    # 作用：使用指定模型反复执行“生成工具调用—执行—回填观察”，直到得到最终文本或达到上限。
    # 参数 model_name：本次实际调用的主模型或备用模型名称。
    # 参数 system_prompt：已经汇总角色、记忆、工具和安全规则的系统提示。
    # 参数 messages：进入模型循环的历史消息。
    # 参数 safety_level：决定无工具模式温度和安全处理的等级。
    # 参数 character：用于最终兜底回复的角色配置。
    # 参数 session_id：工具执行归属的会话标识。
    # 参数 allowed_tools：当前角色可调用的工具白名单。
    # 参数 enable_agent_tools：是否启用原生 Tool Calling。
    # 参数 authorization_requirements：限制模型副作用动作的用户契约。
    # 参数 untrusted_context：进入循环前是否已观察到不可信数据。
    # 参数 injection_detected_context：进入循环前是否已发现提示注入。
    # 返回：循环终止时的模型文本和累计工具结果。
    def _reply_with_model(
        self,
        *,
        model_name: str,
        system_prompt: str,
        messages: list[ChatMessage],
        safety_level: SafetyLevel,
        character: CharacterProfile,
        session_id: str,
        allowed_tools: tuple[ToolAction, ...] | None,
        enable_agent_tools: bool,
        authorization_requirements: tuple[dict[str, Any], ...] | None,
        untrusted_context: bool,
        injection_detected_context: bool,
    ) -> ModelReply:
        conversation: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *[{"role": item.role, "content": item.content} for item in messages],
        ]
        native_tools = (
            self.tool_registry.native_tools(allowed_tools)
            if (
                enable_agent_tools
                and os.environ.get("YUKINO_AGENT_ENABLED", "true").lower() not in {"0", "false", "off", "no"}
                and self.tool_registry is not None
            )
            else []
        )
        results: list[ToolExecutionResult] = []
        committed_tools: list[dict[str, Any]] = []
        untrusted_observed = untrusted_context
        injection_detected = injection_detected_context
        for loop_index in range(self.max_agent_steps):
            if self.tool_registry is not None and self.tool_registry.task_cancel_requested():
                self._task_checkpoint(
                    state="cancelled",
                    loop_index=loop_index,
                    model=model_name,
                    conversation=conversation,
                    tools=committed_tools,
                    detail="模型循环已在下一次调用工具前停止",
                )
                return ModelReply(
                    content="这次委托已经停止，不会继续调用工具。",
                    degraded=True,
                    degradation_reason="task_cancelled",
                    tool_results=tuple(results),
                )
            kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": conversation,
                "temperature": 0.45 if native_tools else (0.85 if safety_level == "normal" else 0.35),
                "timeout": self.timeout_seconds,
            }
            kwargs.update(self.provider_request_options(model_name))
            if native_tools:
                kwargs.update({"tools": native_tools, "tool_choice": "auto"})
            self._task_checkpoint(
                state="model_waiting",
                loop_index=loop_index,
                model=model_name,
                conversation=conversation,
                tools=committed_tools,
                detail=f"已保存第 {loop_index + 1} 轮模型请求边界",
            )
            completion = self._completion_for_turn(kwargs, results)
            self._record_usage(model_name, completion)
            message = completion.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            content = str(getattr(message, "content", "") or "").strip()
            if tool_calls and not native_tools:
                self._task_event(
                    "tool_authorization_blocked",
                    status="blocked",
                    phase="security_review",
                    detail="模型返回了未提供给它的工具调用，已阻止执行",
                    payload={"reason": "tools_not_offered"},
                )
                self._task_checkpoint(
                    state="unexpected_tool_call_blocked",
                    loop_index=loop_index,
                    model=model_name,
                    conversation=conversation,
                    tools=committed_tools,
                    detail="模型返回未授权工具调用，未执行任何工具",
                )
                return ModelReply(
                    content=content or "模型返回了未授权的工具调用，已经阻止执行。",
                    degraded=True,
                    degradation_reason="unexpected_tool_call",
                    tool_results=tuple(results),
                )
            if not tool_calls:
                if content:
                    self._task_checkpoint(
                        state="response_ready",
                        loop_index=loop_index,
                        model=model_name,
                        conversation=conversation,
                        tools=committed_tools,
                        detail=f"第 {loop_index + 1} 轮已取得最终回答",
                    )
                    return ModelReply(content=content, tool_results=tuple(results))
                empty = ModelEmptyResponse("模型返回了空内容。")
                if results:
                    raise ModelTurnInterrupted(empty, tuple(results))
                raise empty
            conversation.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
                }
            )
            decisions = [
                {
                    "action": str(
                        getattr(getattr(call, "function", None), "name", "")
                    ),
                    "status": "planned",
                    "fingerprint": self._tool_call_fingerprint(call),
                }
                for call in tool_calls
            ]
            self._task_checkpoint(
                state="tool_decision",
                loop_index=loop_index,
                model=model_name,
                conversation=conversation,
                tools=[*committed_tools, *decisions],
                detail=f"第 {loop_index + 1} 轮模型决定调用 {len(tool_calls)} 个工具",
            )
            for call in tool_calls:
                if self.tool_registry is not None and self.tool_registry.task_cancel_requested():
                    results.append(
                        ToolExecutionResult(
                            action=str(getattr(getattr(call, "function", None), "name", "none")),  # type: ignore[arg-type]
                            success=False,
                            error="任务已经停止，未执行后续工具。",
                            audit={"task_cancelled": True},
                        )
                    )
                    self._task_checkpoint(
                        state="cancelled",
                        loop_index=loop_index,
                        model=model_name,
                        conversation=conversation,
                        tools=committed_tools,
                        detail="工具批次已在下一项执行前停止",
                    )
                    return ModelReply(
                        content="这次委托已经停止，不会继续调用工具。",
                        degraded=True,
                        degradation_reason="task_cancelled",
                        tool_results=tuple(results),
                    )
                function = getattr(call, "function", None)
                name = str(getattr(function, "name", ""))
                fingerprint = self._tool_call_fingerprint(call)
                arguments = self._parse_tool_arguments(str(getattr(function, "arguments", "{}")))
                authorized, authorization_reason = self.output_security.authorize_model_action(
                    name,
                    arguments,
                    authorization_requirements,
                    injection_detected=injection_detected,
                )
                try:
                    if authorized:
                        result = self.tool_registry.execute(  # type: ignore[union-attr,arg-type]
                            name,
                            arguments,
                            session_id=session_id,
                            allowed_tools=allowed_tools,
                        )
                    else:
                        result = ToolExecutionResult(
                            action=name,  # type: ignore[arg-type]
                            success=False,
                            error=(
                                "检测到不可信内容中的提示注入，本轮后续副作用已经冻结；"
                                "请由用户在新的消息中明确重新授权。"
                                if authorization_reason == "prompt_injection_freeze"
                                else "这个副作用不在用户本轮明确委托的执行契约中，已阻止执行。"
                            ),
                            audit={
                                "tool_output_security_blocked": True,
                                "authorization_reason": authorization_reason,
                                "requires_user_reauthorization": True,
                                "untrusted_context_observed": untrusted_observed,
                                "prompt_injection_detected": injection_detected,
                            },
                        )
                        self._task_event(
                            "tool_authorization_blocked",
                            status="blocked",
                            phase="security_review",
                            action=name,
                            detail=result.error,
                            payload={"reason": authorization_reason},
                        )
                except Exception as exc:
                    results.append(
                        ToolExecutionResult(
                            action=name,  # type: ignore[arg-type]
                            success=False,
                            error="工具执行时发生内部错误，未自动重试。",
                            audit={
                                "source": "native_tool_call",
                                "tool_call_id": str(getattr(call, "id", "")),
                                "internal_error": type(exc).__name__,
                            },
                        )
                    )
                    committed_tools.append(
                        {
                            "action": name,
                            "status": "failed",
                            "fingerprint": fingerprint,
                            "success": False,
                        }
                    )
                    self._task_checkpoint(
                        state="tool_failed",
                        loop_index=loop_index,
                        model=model_name,
                        conversation=conversation,
                        tools=committed_tools,
                        detail=f"工具 {name} 发生内部错误，已保留停止边界",
                    )
                    raise ModelTurnInterrupted(
                        exc,
                        tuple(results),
                        source="tool",
                    ) from exc
                result.audit.update(
                    {
                        "source": "native_tool_call",
                        "tool_call_id": str(getattr(call, "id", "")),
                    }
                )
                self.output_security.assess(result, source="native_tool")
                if result.audit.get("output_trust") == "untrusted":
                    untrusted_observed = True
                if result.audit.get("prompt_injection_detected"):
                    injection_detected = True
                    self._task_event(
                        "untrusted_tool_output_isolated",
                        status="blocked",
                        phase="security_review",
                        action=name,
                        detail="工具结果含疑似提示注入；正文已作为数据隔离，后续副作用已冻结",
                        payload={
                            "signals": list(result.audit.get("security_signals", [])),
                            "source": result.audit.get("output_source", ""),
                        },
                    )
                results.append(result)
                committed_tools.append(
                    {
                        "action": name,
                        "status": (
                            "waiting_confirmation"
                            if result.audit.get("requires_confirmation")
                            else (
                                "waiting_background"
                                if (
                                    result.audit.get("background_pending")
                                    or result.audit.get("background_job_pending")
                                )
                                else ("completed" if result.success else "failed")
                            )
                        ),
                        "step_id": int(result.audit.get("task_step_id", 0) or 0),
                        "fingerprint": fingerprint,
                        "success": result.success,
                        "requires_confirmation": bool(
                            result.audit.get("requires_confirmation")
                        ),
                        "background_pending": bool(
                            result.audit.get("background_pending")
                            or result.audit.get("background_job_pending")
                        ),
                    }
                )
                if result.audit.get("budget_exceeded"):
                    self._task_checkpoint(
                        state="budget_exceeded",
                        loop_index=loop_index,
                        model=model_name,
                        conversation=conversation,
                        tools=committed_tools,
                        detail="工具预算已到上限，已保存已提交结果",
                    )
                    return ModelReply(
                        content=result.error or "任务资源预算已经用完。",
                        degraded=True,
                        degradation_reason="task_budget_exceeded",
                        tool_results=tuple(results),
                    )
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(getattr(call, "id", "")),
                        "content": json.dumps(
                            {
                                "success": result.success,
                                "content": self.output_security.render_for_model(result),
                                "error": (
                                    ""
                                    if result.audit.get("output_trust") == "untrusted"
                                    else result.error
                                ),
                                "requires_confirmation": bool(result.audit.get("requires_confirmation")),
                                "operation_id": result.audit.get("operation_id", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            self._task_checkpoint(
                state="tool_results_committed",
                loop_index=loop_index,
                model=model_name,
                conversation=conversation,
                tools=committed_tools,
                detail=f"第 {loop_index + 1} 轮工具结果已写入模型上下文",
            )
        conversation.append(
            {
                "role": "system",
                "content": "工具步骤已达到上限。请基于已经取得的结果直接回答，不要再调用工具，也不要声称未完成的操作已经完成。",
            }
        )
        self._task_checkpoint(
            state="step_limit_response_waiting",
            loop_index=self.max_agent_steps,
            model=model_name,
            conversation=conversation,
            tools=committed_tools,
            detail="工具轮数达到上限，正在请求最终整理回答",
        )
        completion = self._completion_for_turn(
            {
                "model": model_name,
                "messages": conversation,
                "temperature": 0.35,
                "timeout": self.timeout_seconds,
                **self.provider_request_options(model_name),
            },
            results,
        )
        self._record_usage(model_name, completion)
        content = str(completion.choices[0].message.content or "").strip()
        self._task_checkpoint(
            state="response_ready",
            loop_index=self.max_agent_steps,
            model=model_name,
            conversation=conversation,
            tools=committed_tools,
            detail="工具轮数上限后的最终回答已就绪",
        )
        return ModelReply(
            content=content or character.fallback_for("neutral"),
            degraded=True,
            degradation_reason="agent_step_limit",
            tool_results=tuple(results),
        )

    # 作用：预留任务级模型预算，完成一次请求后按实际 token 用量结算。
    # 参数 kwargs：传给模型 Provider 的完整请求参数。
    # 参数 results：当前循环已执行的工具结果；模型失败时用于判断是否禁止重放。
    # 返回：Provider 返回的原始 completion 对象。
    def _completion_for_turn(
        self,
        kwargs: dict[str, Any],
        results: list[ToolExecutionResult],
    ) -> Any:
        request_kwargs = dict(kwargs)
        reservation: dict[str, Any] = {}
        task_id = current_agent_task_id()
        if task_id and self.tool_registry is not None:
            requested_output = int(
                request_kwargs.get(
                    "max_tokens",
                    os.getenv("YUKINO_MODEL_MAX_OUTPUT_TOKENS", "2048"),
                )
            )
            reservation = self.tool_registry.tasks.reserve_model_call(
                task_id,
                estimated_input_tokens=self._estimate_request_tokens(request_kwargs),
                requested_output_tokens=requested_output,
                model=str(request_kwargs.get("model", "")),
            )
            request_kwargs["max_tokens"] = int(reservation["max_output_tokens"])
            request_kwargs["_task_timeout_seconds"] = float(
                reservation["wall_remaining_seconds"]
            )
        try:
            completion = self._create_completion(request_kwargs)
        except Exception as exc:
            if reservation:
                if isinstance(exc, ModelConcurrencyLimit) or not bool(
                    getattr(exc, "_yukino_request_started", False)
                ):
                    self.tool_registry.tasks.release_model_call(reservation)  # type: ignore[union-attr]
                elif bool(getattr(exc, "_yukino_request_completed", False)):
                    self.tool_registry.tasks.settle_model_call(reservation)  # type: ignore[union-attr]
                else:
                    self.tool_registry.tasks.settle_model_call(  # type: ignore[union-attr]
                        reservation,
                        uncertain=True,
                    )
            if results:
                raise ModelTurnInterrupted(exc, tuple(results)) from exc
            raise
        if reservation:
            input_tokens, output_tokens = self._completion_tokens(
                completion,
                request_kwargs,
            )
            budget_status = self.tool_registry.tasks.settle_model_call(  # type: ignore[union-attr]
                reservation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            if budget_status.get("exceeded"):
                self.tool_registry.tasks.ensure_within_budget(task_id)  # type: ignore[union-attr]
        return completion

    # 作用：在并发槽位、任务取消和等待超时约束下调用 OpenAI 兼容接口。
    # 参数 kwargs：模型名称、消息、工具、温度与超时等请求参数。
    # 返回：OpenAI 兼容客户端返回的 completion 对象。
    def _create_completion(self, kwargs: dict[str, Any]) -> Any:
        if self.client is None:
            raise RuntimeError("模型客户端尚未配置。")
        request_kwargs = dict(kwargs)
        task_timeout = max(
            0.05,
            float(request_kwargs.pop("_task_timeout_seconds", self.timeout_seconds)),
        )
        effective_timeout = min(self.timeout_seconds, task_timeout)
        request_kwargs["timeout"] = min(
            float(request_kwargs.get("timeout", self.timeout_seconds)),
            effective_timeout,
        )
        slot_deadline = time.monotonic() + effective_timeout
        while not self._model_slots.acquire(timeout=0.05):
            if self._task_cancel_requested():
                raise ModelRequestCancelled("任务已经停止。")
            if time.monotonic() >= slot_deadline:
                raise ModelConcurrencyLimit("模型并发槽位已满。")
        with self._active_lock:
            self._active_calls += 1
        deadline = time.monotonic() + effective_timeout
        completed: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        # 作用：在线程中发起可能阻塞的模型请求，并把成功或异常传回等待队列。
        # 参数：无；通过闭包读取 request_kwargs、completed 和并发状态。
        def worker() -> None:
            try:
                completed.put((True, self.client.chat.completions.create(**request_kwargs)))
            except Exception as exc:
                completed.put((False, exc))
            finally:
                with self._active_lock:
                    self._active_calls -= 1
                self._model_slots.release()

        threading.Thread(target=worker, name="yukino-model-call", daemon=True).start()
        while True:
            try:
                succeeded, value = completed.get(timeout=0.05)
            except queue.Empty:
                if self._task_cancel_requested():
                    error = ModelRequestCancelled("任务已经停止等待模型。")
                    error._yukino_request_started = True  # type: ignore[attr-defined]
                    raise error
                if time.monotonic() >= deadline:
                    error = ModelWaitTimeout("模型调用超过配置等待时间。")
                    error._yukino_request_started = True  # type: ignore[attr-defined]
                    raise error
                continue
            if succeeded:
                return value
            try:
                value._yukino_request_started = True
                value._yukino_request_completed = True
            except Exception:
                pass
            raise value

    # 作用：对指定模型执行最小探测请求，并更新健康状态与延迟指标。
    # 参数 model_name：可选待探测模型；为空时探测当前主模型。
    # 返回：探测成功内容或分类后的失败信息及运行状态。
    def probe(self, model_name: str | None = None) -> dict[str, Any]:
        selected = (model_name or self.model).strip()
        if self.client is None:
            return {
                "ok": False,
                "model": selected,
                "failure": {
                    "code": "missing_api_key",
                    "category": "configuration",
                    "retryable": False,
                    "fatal": True,
                    "status_code": None,
                },
                "latency_ms": 0,
                "runtime": self.status(),
            }
        started = time.monotonic()
        try:
            self.health.begin(selected, force=True)
        except ModelCircuitOpen as exc:
            return {
                "ok": False,
                "model": selected,
                "failure": {
                    "code": "circuit_probe_in_flight",
                    "category": "circuit",
                    "retryable": True,
                    "fatal": False,
                    "status_code": None,
                },
                "retry_after_seconds": round(exc.retry_after, 3),
                "latency_ms": 0,
                "runtime": self.status(),
            }
        try:
            completion = self._create_completion(
                {
                    "model": selected,
                    "messages": [{"role": "user", "content": "只回复 OK"}],
                    "temperature": 0,
                    "max_tokens": 4,
                    "timeout": self.timeout_seconds,
                    **self.provider_request_options(selected),
                }
            )
            content = str(completion.choices[0].message.content or "").strip()
            if not content:
                raise ModelEmptyResponse("模型没有返回文本。")
        except ModelRequestCancelled:
            self.health.abort(selected)
            failure = ProviderFailure("cancelled", "local", True)
        except ModelConcurrencyLimit:
            self.health.abort(selected)
            failure = ProviderFailure("local_capacity", "local", True)
        except Exception as exc:
            failure = classify_provider_failure(exc)
            self.health.failure(
                selected,
                failure,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        else:
            latency_ms = int((time.monotonic() - started) * 1000)
            self.health.success(selected, latency_ms=latency_ms)
            self._record_usage(selected, completion)
            return {
                "ok": True,
                "model": selected,
                "content": content,
                "latency_ms": latency_ms,
                "runtime": self.status(),
            }
        return {
            "ok": False,
            "model": selected,
            "failure": failure.as_dict(),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "runtime": self.status(),
        }

    # 作用：查询当前持久任务是否已收到取消请求。
    # 参数：无。
    # 返回：存在当前任务且任务已请求取消时为 True。
    def _task_cancel_requested(self) -> bool:
        return bool(
            self.tool_registry is not None
            and self.tool_registry.task_cancel_requested()
        )

    # 作用：以尽力而为方式写入任务进度事件，遥测失败不影响真实 Agent 流程。
    # 参数 event：稳定的事件类型名称。
    # 参数 status：事件对应的运行状态。
    # 参数 phase：事件发生时的任务阶段。
    # 参数 action：相关模型或工具动作名称。
    # 参数 detail：适合展示和排障的简短说明。
    # 参数 payload：不含敏感正文的附加结构数据。
    def _task_event(
        self,
        event: str,
        *,
        status: str,
        phase: str,
        action: str = "",
        detail: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        task_id = current_agent_task_id()
        if not task_id or self.tool_registry is None:
            return
        try:
            self.tool_registry.tasks.append_event(
                task_id,
                event,
                status=status,
                phase=phase,
                action=action,
                detail=detail,
                payload=payload,
            )
        except Exception:
            # Progress telemetry must never make the actual Agent turn fail.
            return

    # 作用：保存不含原始提示和工具正文的结构检查点，供恢复和审计使用。
    # 参数 state：检查点对应的模型循环状态。
    # 参数 loop_index：当前模型—工具循环轮次。
    # 参数 model：本轮实际使用的模型名称。
    # 参数 conversation：用于计算摘要、但不会原样持久化的消息列表。
    # 参数 tools：已计划或提交工具的结构化摘要。
    # 参数 detail：本检查点的人类可读说明。
    def _task_checkpoint(
        self,
        *,
        state: str,
        loop_index: int,
        model: str,
        conversation: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        detail: str,
    ) -> None:
        """Persist only structural recovery evidence, never prompt/tool payload text."""
        task_id = current_agent_task_id()
        if not task_id or self.tool_registry is None:
            return
        try:
            encoded = json.dumps(
                conversation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            self.tool_registry.tasks.save_checkpoint(
                task_id,
                state=state,
                loop_index=loop_index,
                model=model,
                conversation_digest=hashlib.sha256(encoded).hexdigest(),
                tools=tools,
                detail=detail,
            )
        except Exception as exc:
            self._task_event(
                "checkpoint_failed",
                status="degraded",
                phase="checkpointing",
                action=model,
                detail="结构检查点保存失败，但本轮仍继续执行",
                payload={"error_type": type(exc).__name__},
            )

    # 作用：对工具名称和原始参数生成不可逆摘要，避免检查点保存敏感参数。
    # 参数 call：模型 SDK 返回的单个工具调用对象。
    # 返回：工具决策的 SHA-256 十六进制摘要。
    @staticmethod
    # 作用：执行“tool_call_fingerprint”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 call：调用方传入的call，用于本次处理。
    def _tool_call_fingerprint(call: Any) -> str:
        """Hash one tool decision so checkpoints never retain raw arguments."""
        function = getattr(call, "function", None)
        payload = {
            "name": str(getattr(function, "name", "")),
            "arguments": str(getattr(function, "arguments", "{}")),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # 作用：返回模型可用性、并发占用、健康状态和工具输出安全统计。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        with self._active_lock:
            active_calls = self._active_calls
        return {
            "ok": self.client is not None,
            "available": self.client is not None,
            "primary_model": self.model,
            "fallback_model": self.fallback_model,
            "timeout_seconds": self.timeout_seconds,
            "max_concurrent_calls": self.max_concurrent_calls,
            "active_calls": active_calls,
            "soft_cancellation": True,
            "provider_request_may_finish_in_background": True,
            "tool_output_security": self.output_security.status(),
            "health": self.health.status((self.model, self.fallback_model)),
        }

    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> str:
        """Run a bounded, tool-free auxiliary completion for retrieval helpers."""
        if self.client is None or not self.model:
            return ""
        completion = self._completion_for_turn(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": max(0.0, min(float(temperature), 1.0)),
                "max_tokens": max(64, min(int(max_tokens), 2048)),
                "timeout": self.timeout_seconds,
                **self.provider_request_options(self.model),
            },
            [],
        )
        self._record_usage(self.model, completion)
        return str(completion.choices[0].message.content or "").strip()

    # 作用：组装本轮实际模型、备用切换和各次尝试的可观测摘要。
    # 参数 model_used：最终实际产出回复的模型名称。
    # 参数 fallback_used：是否切换到了备用模型。
    # 参数 attempts：每次模型调用的状态、延迟和失败分类记录。
    # 返回：本轮模型运行摘要。
    def _turn_runtime(
        self,
        *,
        model_used: str,
        fallback_used: bool,
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "requested_model": self.model,
            "configured_fallback": self.fallback_model,
            "model_used": model_used,
            "fallback_used": fallback_used,
            "attempts": list(attempts),
            "health": self.health.status((self.model, self.fallback_model)),
        }

    # 作用：将一次失败的模型尝试转换成统一运行记录。
    # 参数 model_name：失败的模型名称。
    # 参数 failure：分类后的 ProviderFailure。
    # 参数 latency_ms：失败前等待的毫秒数。
    # 参数 fallback：该尝试是否属于备用模型。
    # 参数 after_tools：失败前是否已经产生工具结果。
    @staticmethod
    # 作用：执行“failed_attempt”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 model_name：调用方传入的model_name，用于本次处理。
    # 参数 failure：调用方传入的failure，用于本次处理。
    # 参数 latency_ms：调用方传入的latency_ms，用于本次处理。
    # 参数 fallback：解析失败时返回的兜底对象。
    # 参数 after_tools：调用方传入的after_tools，用于本次处理。
    def _failed_attempt(
        *,
        model_name: str,
        failure: ProviderFailure,
        latency_ms: int,
        fallback: bool,
        after_tools: bool = False,
    ) -> dict[str, Any]:
        return {
            "model": model_name,
            "status": "failed",
            "failure": failure.as_dict(),
            "latency_ms": latency_ms,
            "fallback": fallback,
            "after_tools": after_tools,
        }

    # 作用：把底层 Provider 故障类型映射为稳定的业务降级原因。
    # 参数 failure：可选的 Provider 故障分类；为空时使用通用模型错误。
    # 返回：对外稳定的降级原因代码。
    @staticmethod
    # 作用：执行“degradation_reason”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 failure：调用方传入的failure，用于本次处理。
    def _degradation_reason(failure: ProviderFailure | None) -> str:
        if failure is None:
            return "model_error"
        return {
            "timeout": "model_wait_timeout",
            "empty_response": "empty_response",
            "authentication_error": "model_authentication_error",
            "model_not_found": "model_not_found",
            "rate_limited": "model_rate_limited",
            "provider_unavailable": "model_provider_unavailable",
            "network_error": "model_network_error",
            "invalid_request": "model_invalid_request",
            "provider_error": "model_error",
        }.get(failure.code, "model_error")

    # 作用：判断模型错误是否来自不支持原生工具协议，以便降级到纯文本模式。
    # 参数 exc：模型 Provider 抛出的原始异常。
    @staticmethod
    # 作用：执行“is_tool_compatibility_error”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 exc：调用方传入的exc，用于本次处理。
    def _is_tool_compatibility_error(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        message = str(exc).lower()
        return bool(
            (status in {400, 404, 422} or isinstance(exc, RuntimeError))
            and any(
                token in message
                for token in ("tool", "function call", "function_call")
            )
        )

    # 作用：读取整数环境变量并限制在安全区间内。
    # 参数 name：环境变量名称。
    # 参数 default：缺失或格式错误时使用的默认值。
    # 参数 minimum：允许返回的最小值。
    # 参数 maximum：允许返回的最大值。
    @staticmethod
    # 作用：执行“bounded_env_int”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 name：调用方传入的name，用于本次处理。
    # 参数 default：输入缺失或无效时使用的默认值。
    # 参数 minimum：调用方传入的minimum，用于本次处理。
    # 参数 maximum：调用方传入的maximum，用于本次处理。
    def _bounded_env_int(
        name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    # 作用：读取浮点环境变量并限制在安全区间内。
    # 参数 name：环境变量名称。
    # 参数 default：缺失或格式错误时使用的默认值。
    # 参数 minimum：允许返回的最小值。
    # 参数 maximum：允许返回的最大值。
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

    # 作用：将 SDK 工具调用对象转换为下一轮消息可接受的标准字典。
    # 参数 call：Provider SDK 返回的工具调用对象。
    @staticmethod
    # 作用：执行“serialize_tool_call”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 call：调用方传入的call，用于本次处理。
    def _serialize_tool_call(call: Any) -> dict[str, Any]:
        function = getattr(call, "function", None)
        return {
            "id": str(getattr(call, "id", "")),
            "type": "function",
            "function": {
                "name": str(getattr(function, "name", "")),
                "arguments": str(getattr(function, "arguments", "{}")),
            },
        }

    # 作用：将模型返回的 JSON 参数解析成字典，非法或非对象参数按空字典处理。
    # 参数 raw：模型 function.arguments 返回的原始 JSON 字符串。
    @staticmethod
    # 作用：执行“parse_tool_arguments”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 raw：尚未解析或规范化的原始输入。
    def _parse_tool_arguments(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    # 作用：估算消息和工具定义占用的输入 token 数，用于缺失用量时的预算结算。
    # 参数 kwargs：包含 messages 和 tools 的模型请求参数。
    @classmethod
    # 作用：执行“estimate_request_tokens”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 kwargs：调用方传入的kwargs，用于本次处理。
    def _estimate_request_tokens(cls, kwargs: dict[str, Any]) -> int:
        payload = {
            "messages": kwargs.get("messages", []),
            "tools": kwargs.get("tools", []),
        }
        try:
            raw = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = str(payload)
        return cls._estimate_text_tokens(raw)

    # 作用：优先读取 Provider 用量，缺失时估算本次输入与输出 token。
    # 参数 completion：Provider 返回的完成对象。
    # 参数 request_kwargs：用于补算输入 token 的原始请求参数。
    @classmethod
    # 作用：执行“completion_tokens”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 completion：调用方传入的completion，用于本次处理。
    # 参数 request_kwargs：调用方传入的request_kwargs，用于本次处理。
    def _completion_tokens(
        cls,
        completion: Any,
        request_kwargs: dict[str, Any],
    ) -> tuple[int, int]:
        completion_usage = getattr(completion, "usage", None)
        input_tokens = int(getattr(completion_usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(completion_usage, "completion_tokens", 0) or 0)
        if input_tokens <= 0:
            input_tokens = cls._estimate_request_tokens(request_kwargs)
        if output_tokens <= 0:
            try:
                choices = getattr(completion, "choices", [])
                raw = json.dumps(
                    [getattr(choice, "message", choice) for choice in choices],
                    ensure_ascii=False,
                    default=str,
                )
            except (TypeError, ValueError):
                raw = str(completion)
            output_tokens = cls._estimate_text_tokens(raw)
        return input_tokens, output_tokens

    # 作用：使用与分词器无关的保守规则估算中英文混合文本 token 数。
    # 参数 value：需要估算 token 数的文本。
    @staticmethod
    # 作用：执行“estimate_text_tokens”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _estimate_text_tokens(value: str) -> int:
        # Conservative tokenizer-independent estimate for OpenAI-compatible providers.
        latin = sum(1 for char in value if ord(char) < 128)
        non_latin = max(0, len(value) - latin)
        return max(1, (latin + 3) // 4 + non_latin)

    # 作用：把 Provider 返回的 token 用量写入运行用量账本。
    # 参数 model_name：产生本次用量的模型名称。
    # 参数 completion：包含 usage 字段的 Provider 完成对象。
    def _record_usage(self, model_name: str, completion: Any) -> None:
        if self.usage is None:
            return
        completion_usage = getattr(completion, "usage", None)
        self.usage.record(
            model=model_name,
            input_tokens=int(getattr(completion_usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(completion_usage, "completion_tokens", 0) or 0),
        )

    # 作用：返回特定 OpenAI 兼容 Provider 所需的附加请求参数。
    # 参数 model_name：可选实际模型名称；为空时使用当前主模型。
    def provider_request_options(self, model_name: str | None = None) -> dict[str, Any]:
        """Provider-specific compatibility that preserves the OpenAI-compatible surface."""
        selected = model_name or self.model
        if "api.deepseek.com" in self.base_url and selected.startswith("deepseek-"):
            # V4 enables thinking by default. Non-thinking mode avoids empty short replies and
            # does not require forwarding provider-specific reasoning_content between tool steps.
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {}
