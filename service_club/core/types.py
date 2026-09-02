from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["user", "assistant"]
ConversationMode = Literal["daily", "quiet", "request", "banter", "serious", "comfort"]
SafetyLevel = Literal["normal", "high_risk", "crisis"]
ChatMode = Literal["solo", "club"]
RoutingMode = Literal["manual", "adaptive"]
CharacterId = Literal["yukino", "yui", "hachiman", "iroha", "shizuka"]
EmotionLabel = Literal[
    "happy", "sad", "anxious", "angry", "lonely", "shy", "thinking", "neutral"
]
ToolAction = Literal[
    "none",
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
]


# 作用：表示传入模型上下文的一条用户或助手消息。
# 参数 role：消息发送方，只允许 user 或 assistant。
# 参数 content：消息的自然语言正文。
class ChatMessage(BaseModel):
    role: Role
    content: str


# 作用：统一描述一次工具调用的结果、错误信息和可验收审计证据。
# 参数 action：实际尝试执行的工具动作名称。
# 参数 success：工具是否完成了声明的动作。
# 参数 content：成功结果或可展示的结果摘要。
# 参数 error：失败、等待确认或降级时的原因。
# 参数 audit：幂等、权限、回执和验收使用的结构化证据。
class ToolExecutionResult(BaseModel):
    action: ToolAction
    success: bool
    content: str = ""
    error: str = ""
    audit: dict = Field(default_factory=dict)


# 作用：定义聊天入口接收的请求参数，包括会话、路由、附件和任务恢复信息。
# 参数 messages：本轮携带的用户与助手消息序列。
# 参数 chat_mode：单角色 solo 或群像 club 模式。
# 参数 routing_mode：手动 manual 或自适应 adaptive 路由。
# 参数 character：用户当前选中的角色标识。
# 参数 session_id：隔离会话记忆、任务和权限的稳定标识。
# 参数 request_id：用于请求级幂等的可选标识。
# 参数 resume_task_id：需要恢复的持久任务标识；为空表示新任务。
# 参数 allow_uncertain_replay：用户是否明确允许重放结果不确定的副作用步骤。
# 参数 attachment_ids：本轮需要读取的已上传附件标识。
# 参数 enable_voice：是否尝试为最终文本生成语音。
# 参数 enable_sticker：是否为回复选择角色表情。
class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    chat_mode: ChatMode = "club"
    routing_mode: RoutingMode = "adaptive"
    character: CharacterId = "yukino"
    session_id: str = Field(
        default="default",
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    request_id: str = Field(
        default="",
        max_length=128,
        pattern=r"^(?:[A-Za-z0-9:_-]+)?$",
    )
    resume_task_id: str = Field(
        default="",
        max_length=80,
        pattern=r"^(?:task-[A-Za-z0-9_-]+)?$",
    )
    allow_uncertain_replay: bool = False
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    enable_voice: bool = False
    enable_sticker: bool = True


# 作用：定义返回客户端的完整响应，包含文本、工具证据和各模块运行摘要。
# 参数 content：最终展示给用户的回复文本。
# 参数 conversation_mode/chat_mode/character：本轮采用的对话策略、聊天方式和主角色。
# 参数 emotion/safety_level：识别出的情绪与安全等级。
# 参数 degraded/degradation_reason：是否降级及其稳定原因代码。
# 参数 tool_results/memories_used/memory_retrieval：工具证据和实际使用的记忆上下文。
# 参数 trace_id/route_reasoning/route_scorecard/active_agents：链路追踪与角色路由结果。
# 参数 sticker_url/audio_url：可选表情与语音资源地址。
# 参数 intent_plan/proactive_care/companion_state：意图计划和长期陪伴状态摘要。
# 参数 response_policy/policy_audit/quality_audit：回复策略及确定性验收记录。
# 参数 interaction_learning/self_model/club_orchestration：交互学习、自我模型和群像编排摘要。
# 参数 context_window/model_runtime/execution：上下文窗口、模型运行和任务执行明细。
class ChatResponse(BaseModel):
    content: str
    conversation_mode: ConversationMode
    chat_mode: ChatMode
    character: CharacterId
    emotion: EmotionLabel
    safety_level: SafetyLevel
    degraded: bool = False
    degradation_reason: str = ""
    tool_results: list[ToolExecutionResult] = Field(default_factory=list)
    memories_used: list[str] = Field(default_factory=list)
    memory_retrieval: list[dict] = Field(default_factory=list)
    trace_id: str = ""
    route_reasoning: str = ""
    route_scorecard: dict = Field(default_factory=dict)
    active_agents: list[CharacterId] = Field(default_factory=list)
    sticker_url: str | None = None
    audio_url: str | None = None
    intent_plan: list[dict[str, str]] = Field(default_factory=list)
    spontaneous_recalls: list[dict] = Field(default_factory=list)
    proactive_care: list[dict] = Field(default_factory=list)
    companion_state: dict = Field(default_factory=dict)
    response_policy: dict = Field(default_factory=dict)
    policy_audit: dict = Field(default_factory=dict)
    quality_audit: dict = Field(default_factory=dict)
    interaction_learning: dict = Field(default_factory=dict)
    self_model: dict = Field(default_factory=dict)
    club_orchestration: dict = Field(default_factory=dict)
    context_window: dict = Field(default_factory=dict)
    model_runtime: dict = Field(default_factory=dict)
    execution: dict = Field(default_factory=dict)
