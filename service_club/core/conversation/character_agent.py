from dataclasses import dataclass, field
from typing import Any

from service_club.core.conversation.characters import CHARACTERS, CharacterProfile
from service_club.core.conversation.emotion import EmotionResult
from service_club.core.conversation.llm import ModelReply, ServiceClubModel
from service_club.core.types import (
    CharacterId,
    ChatMessage,
    ChatMode,
    ConversationMode,
    EmotionLabel,
    SafetyLevel,
    ToolAction,
)

DEFAULT_ALLOWED_TOOLS: tuple[ToolAction, ...] = (
    "get_time",
    "remember",
    "recall",
    "forget",
    "clear_memory",
    "set_reminder",
    "list_reminders",
    "cancel_reminder",
    "search_capabilities",
    "list_files",
    "read_file",
    "search_files",
    "analyze_code",
    "analyze_file",
    "write_file",
    "edit_file",
    "confirm_file_operation",
    "cancel_file_operation",
    "rollback_file_operation",
    "run_python",
    "web_search",
    "web_fetch",
    "read_document",
    "system_status",
    "add_task",
    "list_tasks",
    "capability_call",
    "confirm_capability_operation",
    "cancel_capability_operation",
)


# 作用：保存某个角色 Agent 的回答、降级原因和工具执行结果。
# 参数 agent_id：要查找或调用的角色标识。
# 参数 content：最终展示或继续处理的回复文本。
# 参数 degraded：本轮是否使用了降级或兜底路径。
# 参数 degradation_reason：模型进入降级路径的稳定原因代码。
# 参数 tool_results：模型循环中已经执行并保留的工具结果。
# 参数 model_runtime：实际模型、备用切换和尝试记录组成的运行摘要。
@dataclass(frozen=True)
# 作用：定义“CharacterAgentReply”相关的数据结构、异常类型或服务组件。
# 字段：agent_id：该对象中的结构化字段。、content：该对象中的结构化字段。、degraded：该对象中的结构化字段。、degradation_reason：该对象中的结构化字段。、tool_results：该对象中的结构化字段。、model_runtime：该对象中的结构化字段。
class CharacterAgentReply:
    agent_id: CharacterId
    content: str
    degraded: bool
    degradation_reason: str
    tool_results: tuple = ()
    model_runtime: dict = field(default_factory=dict)


# 作用：将角色设定、模型实例和角色工具白名单封装成可调用的 Agent。
# 参数 profile：该 Agent 使用的完整角色设定。
# 参数 model：角色调用使用的模型实例。
# 参数 allowed_tools：当前角色允许暴露和执行的工具白名单。
@dataclass(frozen=True)
# 作用：定义“CharacterAgent”相关的数据结构、异常类型或服务组件。
# 字段：profile：该对象中的结构化字段。、model：该对象中的结构化字段。、allowed_tools：该对象中的结构化字段。
class CharacterAgent:
    profile: CharacterProfile
    model: object
    allowed_tools: tuple[ToolAction, ...] = DEFAULT_ALLOWED_TOOLS

    # 作用：把角色设定与会话上下文交给模型，并转换成统一的角色回答结构。
    # 参数 messages：经过裁剪、准备提供给角色模型的会话历史。
    # 参数 conversation_mode：由用户意图推断出的日常、委托或安慰等回应模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 chat_mode：当前是单角色私聊还是多角色群像模式。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 memories：允许注入本轮系统提示的相关记忆文本。
    # 参数 tool_context：模型调用前已经获得的受控工具观察文本。
    # 参数 session_id：隔离工具执行、记忆和质量记录的会话标识。
    # 参数 enable_agent_tools：是否允许该角色进入原生模型工具循环。
    # 参数 authorization_requirements：用户执行契约中允许动作及必需证据的要求。
    # 参数 untrusted_context：上下文是否包含网页或外部工具的不可信数据。
    # 参数 injection_detected_context：进入本层前是否已经检测到提示注入信号。
    def reply(
        self,
        *,
        messages: list[ChatMessage],
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        chat_mode: ChatMode,
        emotion_label: EmotionLabel,
        memories: list[str],
        tool_context: list[str],
        session_id: str = "default",
        enable_agent_tools: bool = True,
        authorization_requirements: tuple[dict[str, Any], ...] | None = None,
        untrusted_context: bool = False,
        injection_detected_context: bool = False,
    ) -> CharacterAgentReply:
        model_reply: ModelReply = self.model.reply(
            messages=messages,
            conversation_mode=conversation_mode,
            safety_level=safety_level,
            chat_mode=chat_mode,
            character=self.profile,
            emotion=EmotionResult(label=emotion_label, intensity=0.5, valence="neutral"),
            memories=memories,
            tool_context=tool_context,
            session_id=session_id,
            allowed_tools=self.allowed_tools,
            enable_agent_tools=enable_agent_tools,
            authorization_requirements=authorization_requirements,
            untrusted_context=untrusted_context,
            injection_detected_context=injection_detected_context,
        )
        return CharacterAgentReply(
            agent_id=self.profile.id,
            content=model_reply.content,
            degraded=model_reply.degraded,
            degradation_reason=model_reply.degradation_reason,
            tool_results=model_reply.tool_results,
            model_runtime=model_reply.model_runtime,
        )


# 作用：创建并管理所有可用角色 Agent，向编排层提供统一查找和调用入口。
# 参数：无。
class CharacterAgentRegistry:
    # 作用：使用同一个模型实例为配置中的每个角色创建 Agent。
    # 参数 model：角色调用使用的模型实例。
    def __init__(self, model: object | None = None) -> None:
        self.model = model or ServiceClubModel()
        self._agents = {
            character_id: CharacterAgent(profile=profile, model=self.model)
            for character_id, profile in CHARACTERS.items()
        }

    # 作用：按角色标识取得对应的 Agent 实例。
    # 参数 agent_id：要查找或调用的角色标识。
    def get(self, agent_id: CharacterId) -> CharacterAgent:
        return self._agents[agent_id]

    # 作用：将请求转发给指定角色，并保留工具授权和不可信上下文边界。
    # 参数 agent_id：要查找或调用的角色标识。
    # 参数 messages：经过裁剪、准备提供给角色模型的会话历史。
    # 参数 conversation_mode：由用户意图推断出的日常、委托或安慰等回应模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 chat_mode：当前是单角色私聊还是多角色群像模式。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 memories：允许注入本轮系统提示的相关记忆文本。
    # 参数 tool_context：模型调用前已经获得的受控工具观察文本。
    # 参数 session_id：隔离工具执行、记忆和质量记录的会话标识。
    # 参数 enable_agent_tools：是否允许该角色进入原生模型工具循环。
    # 参数 authorization_requirements：用户执行契约中允许动作及必需证据的要求。
    # 参数 untrusted_context：上下文是否包含网页或外部工具的不可信数据。
    # 参数 injection_detected_context：进入本层前是否已经检测到提示注入信号。
    def reply(
        self,
        *,
        agent_id: CharacterId,
        messages: list[ChatMessage],
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        chat_mode: ChatMode,
        emotion_label: EmotionLabel,
        memories: list[str],
        tool_context: list[str],
        session_id: str = "default",
        enable_agent_tools: bool = True,
        authorization_requirements: tuple[dict[str, Any], ...] | None = None,
        untrusted_context: bool = False,
        injection_detected_context: bool = False,
    ) -> CharacterAgentReply:
        return self.get(agent_id).reply(
            messages=messages,
            conversation_mode=conversation_mode,
            safety_level=safety_level,
            chat_mode=chat_mode,
            emotion_label=emotion_label,
            memories=memories,
            tool_context=tool_context,
            session_id=session_id,
            enable_agent_tools=enable_agent_tools,
            authorization_requirements=authorization_requirements,
            untrusted_context=untrusted_context,
            injection_detected_context=injection_detected_context,
        )

    # 作用：生成角色名称、职责和工具白名单，供状态页与调试界面展示。
    # 参数：无。
    def manifest(self) -> list[dict[str, object]]:
        return [
            {
                "id": agent.profile.id,
                "display_name": agent.profile.display_name,
                "allowed_tools": list(agent.allowed_tools),
                "role_summary": agent.profile.role_summary,
            }
            for agent in self._agents.values()
        ]
