import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any

from service_club.core.conversation.character_agent import CharacterAgentRegistry, CharacterAgentReply
from service_club.core.conversation.characters import CHARACTERS
from service_club.core.types import (
    CharacterId,
    ChatMessage,
    ChatMode,
    ConversationMode,
    EmotionLabel,
    SafetyLevel,
)


# 作用：描述群像会话中某位角色的发言身份和内部指令。
# 参数 agent_id：发言角色的稳定标识。
# 参数 role：辅助角色在本轮承担的发言职责。
# 参数 instruction：仅供模型使用的本轮发言职责指令。
@dataclass(frozen=True)
# 作用：定义“ClubSpeakerPlan”相关的数据结构、异常类型或服务组件。
# 字段：agent_id：该对象中的结构化字段。、role：该对象中的结构化字段。、instruction：该对象中的结构化字段。
class ClubSpeakerPlan:
    agent_id: CharacterId
    role: str
    instruction: str

    # 作用：输出不包含内部提示文本的安全发言计划摘要。
    # 参数：无。
    def as_dict(self) -> dict[str, str]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
        }


# 作用：保存一轮群像会话的发言者顺序、候选角色和人数预算。
# 参数 speakers：需要并行调用的辅助角色发言计划列表。
# 参数 candidate_agents：规划时仍可参与本轮的候选角色。
# 参数 max_speakers：本轮群像允许发言的最大角色数。
# 参数 reason：采用当前人数和角色组合的原因。
@dataclass(frozen=True)
# 作用：定义“ClubTurnPlan”相关的数据结构、异常类型或服务组件。
# 字段：speakers：该对象中的结构化字段。、candidate_agents：该对象中的结构化字段。、max_speakers：该对象中的结构化字段。、reason：该对象中的结构化字段。
class ClubTurnPlan:
    speakers: list[ClubSpeakerPlan]
    candidate_agents: list[CharacterId]
    max_speakers: int
    reason: str

    # 作用：将群像计划转换为可观测的字典结构。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "speakers": [speaker.as_dict() for speaker in self.speakers],
            "candidate_agents": list(self.candidate_agents),
            "max_speakers": self.max_speakers,
            "reason": self.reason,
        }


# 作用：保存群像合成回复、各角色原始结果及跳过和去重记录。
# 参数 content：待切句或执行重复内容清理的回复文本。
# 参数 replies：各角色实际产生的原始回复。
# 参数 degraded：群像回复是否包含模型降级结果。
# 参数 degradation_reason：群像进入降级路径的稳定原因代码。
# 参数 plan：本轮实际采用的群像发言计划。
# 参数 skipped_speakers：本轮因预算或去重而跳过的角色信息。
# 参数 reply_adjustments：对各角色回复执行去重和修正的记录。
@dataclass(frozen=True)
# 作用：定义“ClubReply”相关的数据结构、异常类型或服务组件。
# 字段：content：该对象中的结构化字段。、replies：该对象中的结构化字段。、degraded：该对象中的结构化字段。、degradation_reason：该对象中的结构化字段。、plan：该对象中的结构化字段。、skipped_speakers：该对象中的结构化字段。、reply_adjustments：该对象中的结构化字段。
class ClubReply:
    content: str
    replies: list[CharacterAgentReply]
    degraded: bool
    degradation_reason: str
    plan: ClubTurnPlan
    skipped_speakers: list[dict[str, str]] = field(default_factory=list)
    reply_adjustments: list[dict[str, object]] = field(default_factory=list)

    # 作用：生成群像执行轨迹，说明谁发言、谁被跳过以及模型运行情况。
    # 参数：无。
    def orchestration_trace(self) -> dict[str, object]:
        return {
            "execution_mode": "lead_then_parallel_support",
            "plan": self.plan.as_dict(),
            "spoken_agents": [reply.agent_id for reply in self.replies],
            "model_runtime": [
                {
                    "agent_id": reply.agent_id,
                    **reply.model_runtime,
                }
                for reply in self.replies
                if reply.model_runtime
            ],
            "skipped_speakers": list(self.skipped_speakers),
            "reply_adjustments": list(self.reply_adjustments),
        }


# 作用：根据安全、情绪和对话模式决定群像会话需要哪些发言角色。
# 参数：无。
class ClubTurnPlanner:
    NEGATIVE_EMOTIONS = {"sad", "lonely", "anxious", "angry"}
    GROUP_REQUESTS = ("大家", "你们", "每个人", "都说", "一起聊", "都来")
    ROLE_FIT: dict[str, dict[CharacterId, int]] = {
        "emotional_support": {
            "yui": 5,
            "yukino": 3,
            "hachiman": 3,
            "shizuka": 2,
            "iroha": 1,
        },
        "grounded_perspective": {
            "hachiman": 5,
            "yukino": 5,
            "shizuka": 4,
            "yui": 2,
            "iroha": 1,
        },
        "practical_support": {
            "shizuka": 5,
            "yukino": 5,
            "hachiman": 3,
            "iroha": 2,
            "yui": 2,
        },
        "social_bridge": {
            "iroha": 5,
            "yui": 5,
            "hachiman": 3,
            "yukino": 2,
            "shizuka": 1,
        },
        "supporting_view": {
            "yui": 4,
            "hachiman": 4,
            "yukino": 4,
            "iroha": 3,
            "shizuka": 3,
        },
    }

    # 作用：选择主发言者和辅助角色，并为每个角色分配不重复的发言职责。
    # 参数 primary_agent：本轮负责先回答并可使用工具的主角色 ID。
    # 参数 active_agents：本轮允许参与群像编排的角色 ID 列表。
    # 参数 latest_text：用于规划发言者的最新用户文本。
    # 参数 conversation_mode：日常、委托、安静等回应策略模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    def plan(
        self,
        *,
        primary_agent: CharacterId,
        active_agents: list[CharacterId],
        latest_text: str,
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        emotion_label: EmotionLabel,
    ) -> ClubTurnPlan:
        candidates = list(dict.fromkeys([primary_agent, *active_agents]))
        max_speakers, reason = self._speaker_budget(
            latest_text=latest_text,
            conversation_mode=conversation_mode,
            safety_level=safety_level,
            emotion_label=emotion_label,
        )
        max_speakers = min(max_speakers, len(candidates))
        speakers = [
            ClubSpeakerPlan(
                agent_id=primary_agent,
                role="lead",
                instruction=self._lead_instruction(safety_level),
            )
        ]
        remaining = [agent_id for agent_id in candidates if agent_id != primary_agent]
        if max_speakers >= 2 and remaining:
            role = self._secondary_role(
                primary_agent=primary_agent,
                conversation_mode=conversation_mode,
                emotion_label=emotion_label,
                candidates=remaining,
            )
            secondary = self._best_fit(remaining, role)
            speakers.append(
                ClubSpeakerPlan(
                    agent_id=secondary,
                    role=role,
                    instruction=self._support_instruction(role),
                )
            )
            remaining.remove(secondary)
        if max_speakers >= 3 and remaining:
            third = self._best_fit(remaining, "supporting_view")
            speakers.append(
                ClubSpeakerPlan(
                    agent_id=third,
                    role="brief_contrast",
                    instruction=(
                        "你是简短对照补充者。只补一个主发言者尚未提到的角度，"
                        "最多两句，不新增问题，不总结或重复主发言。"
                    ),
                )
            )
        return ClubTurnPlan(
            speakers=speakers,
            candidate_agents=candidates,
            max_speakers=max_speakers,
            reason=reason,
        )

    # 作用：根据高风险、安静模式、负面情绪和明确群聊请求计算发言人数上限。
    # 参数 latest_text：用于规划发言者的最新用户文本。
    # 参数 conversation_mode：日常、委托、安静等回应策略模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    def _speaker_budget(
        self,
        *,
        latest_text: str,
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        emotion_label: EmotionLabel,
    ) -> tuple[int, str]:
        if safety_level in {"high_risk", "crisis"}:
            return 1, "safety_single_voice"
        if conversation_mode == "quiet":
            return 1, "quiet_single_voice"
        if emotion_label in self.NEGATIVE_EMOTIONS:
            return 2, "emotional_low_stimulation"
        if any(phrase in latest_text for phrase in self.GROUP_REQUESTS):
            return 3, "explicit_group_request"
        return 2, "default_two_voice"

    # 作用：根据主角色和当前场景确定第二位角色应承担的补充职责。
    # 参数 primary_agent：本轮负责先回答并可使用工具的主角色 ID。
    # 参数 conversation_mode：日常、委托、安静等回应策略模式。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 candidates：当前可承担辅助职责的候选角色列表。
    def _secondary_role(
        self,
        *,
        primary_agent: CharacterId,
        conversation_mode: ConversationMode,
        emotion_label: EmotionLabel,
        candidates: list[CharacterId],
    ) -> str:
        if emotion_label in self.NEGATIVE_EMOTIONS:
            if primary_agent != "yui" and "yui" in candidates:
                return "emotional_support"
            return "grounded_perspective"
        if conversation_mode in {"request", "serious"}:
            return "practical_support"
        if conversation_mode == "banter":
            return "social_bridge"
        return "supporting_view"

    # 作用：按角色与职责的预设匹配度选出最合适的候选人。
    # 参数 candidates：当前可承担辅助职责的候选角色列表。
    # 参数 role：辅助角色在本轮承担的发言职责。
    def _best_fit(self, candidates: list[CharacterId], role: str) -> CharacterId:
        scores = self.ROLE_FIT.get(role, self.ROLE_FIT["supporting_view"])
        order = {agent_id: index for index, agent_id in enumerate(candidates)}
        return max(candidates, key=lambda item: (scores[item], -order[item]))

    # 作用：为主发言者生成普通或高风险场景下的内部发言约束。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    def _lead_instruction(self, safety_level: SafetyLevel) -> str:
        if safety_level in {"high_risk", "crisis"}:
            return (
                "你是本轮唯一发言者。保持角色底色但压低角色扮演，简短稳定地确认安全，"
                "推动现实求助；不要等待其他角色补充。回复中不要加角色姓名标签。"
            )
        return (
            "你是本轮主发言者。直接回应用户并定下情绪节奏，保留空间给后续角色，"
            "不要替其他角色发言，也不要在回复中加角色姓名标签。"
        )

    # 作用：按辅助角色职责生成控制长度、语气和重复度的内部指令。
    # 参数 role：辅助角色在本轮承担的发言职责。
    def _support_instruction(self, role: str) -> str:
        instructions = {
            "emotional_support": (
                "你负责轻柔承接情绪。只补主发言者尚未表达的陪伴感，最多三句；"
                "不重复建议，不新增问题，不加角色姓名标签。"
            ),
            "grounded_perspective": (
                "你负责补一个克制、现实的角度。不要推翻主发言者，不重复安慰，"
                "最多三句且不新增问题，不加角色姓名标签。"
            ),
            "practical_support": (
                "你负责补一个低成本、可执行的小步骤。不要重做完整分析，"
                "最多三句且不连续追问，不加角色姓名标签。"
            ),
            "social_bridge": (
                "你负责让气氛自然一点，只补一个轻松但不抢戏的角度，"
                "最多三句，不重复主发言者，不加角色姓名标签。"
            ),
            "supporting_view": (
                "你是补充发言者。只提供一个与主发言不同但兼容的角度，"
                "最多三句，不重复、不总结、不加角色姓名标签。"
            ),
        }
        return instructions[role]


# 作用：执行“主角色先答、辅助角色并行补充”的群像编排并过滤重复内容。
# 参数：无。
class ClubOrchestrator:
    DUPLICATE_THRESHOLD = 0.68
    SENTENCE_DUPLICATE_THRESHOLD = 0.58

    # 作用：注入角色注册表和发言规划器。
    # 参数 registry：统一管理和调用角色 Agent 的注册表。
    # 参数 planner：可选群像发言规划器；为空时使用默认实现。
    def __init__(
        self,
        registry: CharacterAgentRegistry,
        planner: ClubTurnPlanner | None = None,
    ) -> None:
        self.registry = registry
        self.planner = planner or ClubTurnPlanner()

    # 作用：汇总群像编排模式、发言预算和去重阈值配置。
    # 参数：无。
    def status(self) -> dict[str, object]:
        return {
            "mode": "planned_lead_then_parallel_support",
            "default_speaker_budget": 2,
            "explicit_group_speaker_budget": 3,
            "quiet_speaker_budget": 1,
            "safety_speaker_budget": 1,
            "duplicate_threshold": self.DUPLICATE_THRESHOLD,
            "sentence_duplicate_threshold": self.SENTENCE_DUPLICATE_THRESHOLD,
            "degradation_short_circuit": True,
            "parallel_support": True,
        }

    # 作用：执行一轮群像对话：主角色可用工具，辅助角色只补充并经过内容去重。
    # 参数 primary_agent：本轮负责先回答并可使用工具的主角色 ID。
    # 参数 active_agents：本轮允许参与群像编排的角色 ID 列表。
    # 参数 latest_text：用于规划发言者的最新用户文本。
    # 参数 messages：经过裁剪的用户与助手会话历史。
    # 参数 conversation_mode：日常、委托、安静等回应策略模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 chat_mode：当前是单角色私聊还是多角色群像模式。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 memories：允许注入角色提示的相关记忆文本。
    # 参数 tool_context：模型调用前已经获得的受控工具观察文本。
    # 参数 session_id：隔离工具、记忆和任务记录的会话标识。
    # 参数 enable_agent_tools：是否允许主角色进入原生模型工具循环。
    # 参数 authorization_requirements：用户执行契约中允许动作及必需证据的要求。
    # 参数 untrusted_context：上下文是否包含网页或外部工具的不可信数据。
    # 参数 injection_detected_context：进入编排前是否已经检测到提示注入。
    def reply(
        self,
        *,
        primary_agent: CharacterId,
        active_agents: list[CharacterId],
        latest_text: str,
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
    ) -> ClubReply:
        plan = self.planner.plan(
            primary_agent=primary_agent,
            active_agents=active_agents,
            latest_text=latest_text,
            conversation_mode=conversation_mode,
            safety_level=safety_level,
            emotion_label=emotion_label,
        )
        emitted: list[CharacterAgentReply] = []
        skipped: list[dict[str, str]] = []
        adjustments: list[dict[str, object]] = []
        reasons: list[str] = []
        degraded = False
        lead_reply = self._invoke_speaker(
            speaker=plan.speakers[0],
            prior_replies=[],
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
        emitted.append(lead_reply)
        if lead_reply.degraded:
            degraded = True
            if lead_reply.degradation_reason:
                reasons.append(lead_reply.degradation_reason)
            for remaining in plan.speakers[1:]:
                skipped.append(
                    {"agent_id": remaining.agent_id, "reason": "primary_degraded"}
                )
            return self._compose_reply(
                emitted=emitted,
                degraded=degraded,
                reasons=reasons,
                plan=plan,
                skipped=skipped,
                adjustments=adjustments,
            )

        support_replies = self._invoke_support_speakers(
            speakers=plan.speakers[1:],
            lead_reply=lead_reply,
            messages=messages,
            conversation_mode=conversation_mode,
            safety_level=safety_level,
            chat_mode=chat_mode,
            emotion_label=emotion_label,
            memories=memories,
            tool_context=tool_context,
            session_id=session_id,
            authorization_requirements=authorization_requirements,
            untrusted_context=untrusted_context,
            injection_detected_context=injection_detected_context,
        )
        for reply in support_replies:
            if reply.degraded:
                degraded = True
                if reply.degradation_reason:
                    reasons.append(reply.degradation_reason)
                skipped.append({"agent_id": reply.agent_id, "reason": "speaker_degraded"})
                continue
            deduplicated, removed_sentences = self._remove_repeated_sentences(
                reply.content,
                [previous.content for previous in emitted],
            )
            if removed_sentences:
                adjustments.append(
                    {
                        "agent_id": reply.agent_id,
                        "action": "remove_repeated_sentences",
                        "removed_count": removed_sentences,
                    }
                )
                reply = CharacterAgentReply(
                    agent_id=reply.agent_id,
                    content=deduplicated,
                    degraded=reply.degraded,
                    degradation_reason=reply.degradation_reason,
                    tool_results=reply.tool_results,
                    model_runtime=reply.model_runtime,
                )
            if not reply.content.strip():
                skipped.append({"agent_id": reply.agent_id, "reason": "duplicate_content"})
                continue
            if any(
                self._similarity(reply.content, previous.content)
                >= self.DUPLICATE_THRESHOLD
                for previous in emitted
            ):
                skipped.append({"agent_id": reply.agent_id, "reason": "duplicate_content"})
                continue
            emitted.append(reply)
        return self._compose_reply(
            emitted=emitted,
            degraded=degraded,
            reasons=reasons,
            plan=plan,
            skipped=skipped,
            adjustments=adjustments,
        )

    # 作用：在复制当前上下文后并行调用辅助角色，并明确关闭其工具权限。
    # 参数 speakers：需要并行调用的辅助角色发言计划列表。
    # 参数 lead_reply：主角色已经生成、供辅助角色避免重复的回答。
    # 参数 messages：经过裁剪的用户与助手会话历史。
    # 参数 conversation_mode：日常、委托、安静等回应策略模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 chat_mode：当前是单角色私聊还是多角色群像模式。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 memories：允许注入角色提示的相关记忆文本。
    # 参数 tool_context：模型调用前已经获得的受控工具观察文本。
    # 参数 session_id：隔离工具、记忆和任务记录的会话标识。
    # 参数 authorization_requirements：用户执行契约中允许动作及必需证据的要求。
    # 参数 untrusted_context：上下文是否包含网页或外部工具的不可信数据。
    # 参数 injection_detected_context：进入编排前是否已经检测到提示注入。
    def _invoke_support_speakers(
        self,
        *,
        speakers: list[ClubSpeakerPlan],
        lead_reply: CharacterAgentReply,
        messages: list[ChatMessage],
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        chat_mode: ChatMode,
        emotion_label: EmotionLabel,
        memories: list[str],
        tool_context: list[str],
        session_id: str,
        authorization_requirements: tuple[dict[str, Any], ...] | None,
        untrusted_context: bool,
        injection_detected_context: bool,
    ) -> list[CharacterAgentReply]:
        if not speakers:
            return []

        # 作用：用统一上下文调用一位辅助角色，并把主回答作为已知前文。
        # 参数 speaker：当前准备调用或格式化的发言计划。
        def invoke(speaker: ClubSpeakerPlan) -> CharacterAgentReply:
            return self._invoke_speaker(
                speaker=speaker,
                prior_replies=[lead_reply],
                messages=messages,
                conversation_mode=conversation_mode,
                safety_level=safety_level,
                chat_mode=chat_mode,
                emotion_label=emotion_label,
                memories=memories,
                tool_context=tool_context,
                session_id=session_id,
                enable_agent_tools=False,
                authorization_requirements=authorization_requirements,
                untrusted_context=untrusted_context,
                injection_detected_context=injection_detected_context,
            )

        if len(speakers) == 1:
            return [invoke(speakers[0])]
        with ThreadPoolExecutor(
            max_workers=len(speakers),
            thread_name_prefix="club-support",
        ) as executor:
            futures = [
                executor.submit(copy_context().run, invoke, speaker)
                for speaker in speakers
            ]
            return [future.result() for future in futures]

    # 作用：为单个发言者拼装角色和前序回答约束，并把异常转换为角色降级回复。
    # 参数 speaker：当前准备调用或格式化的发言计划。
    # 参数 prior_replies：当前发言者之前已经生成的角色回答。
    # 参数 messages：经过裁剪的用户与助手会话历史。
    # 参数 conversation_mode：日常、委托、安静等回应策略模式。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 chat_mode：当前是单角色私聊还是多角色群像模式。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 memories：允许注入角色提示的相关记忆文本。
    # 参数 tool_context：模型调用前已经获得的受控工具观察文本。
    # 参数 session_id：隔离工具、记忆和任务记录的会话标识。
    # 参数 enable_agent_tools：是否允许主角色进入原生模型工具循环。
    # 参数 authorization_requirements：用户执行契约中允许动作及必需证据的要求。
    # 参数 untrusted_context：上下文是否包含网页或外部工具的不可信数据。
    # 参数 injection_detected_context：进入编排前是否已经检测到提示注入。
    def _invoke_speaker(
        self,
        *,
        speaker: ClubSpeakerPlan,
        prior_replies: list[CharacterAgentReply],
        messages: list[ChatMessage],
        conversation_mode: ConversationMode,
        safety_level: SafetyLevel,
        chat_mode: ChatMode,
        emotion_label: EmotionLabel,
        memories: list[str],
        tool_context: list[str],
        session_id: str,
        enable_agent_tools: bool,
        authorization_requirements: tuple[dict[str, Any], ...] | None = None,
        untrusted_context: bool = False,
        injection_detected_context: bool = False,
    ) -> CharacterAgentReply:
        turn_context = [*tool_context, self._format_role_instruction(speaker)]
        if prior_replies:
            turn_context.append(self._format_prior_replies(prior_replies))
        try:
            return self.registry.reply(
                agent_id=speaker.agent_id,
                messages=messages,
                conversation_mode=conversation_mode,
                safety_level=safety_level,
                chat_mode=chat_mode,
                emotion_label=emotion_label,
                memories=memories,
                tool_context=turn_context,
                session_id=session_id,
                enable_agent_tools=enable_agent_tools,
                authorization_requirements=authorization_requirements,
                untrusted_context=untrusted_context,
                injection_detected_context=injection_detected_context,
            )
        except Exception as exc:
            return CharacterAgentReply(
                agent_id=speaker.agent_id,
                content=CHARACTERS[speaker.agent_id].fallback_for(emotion_label),
                degraded=True,
                degradation_reason=f"orchestrator_error:{type(exc).__name__}",
            )

    # 作用：将保留下来的角色回答合成为最终群像文本和完整编排结果。
    # 参数 emitted：通过异常处理和去重后准备输出的角色回复列表。
    # 参数 degraded：群像回复是否包含模型降级结果。
    # 参数 reasons：模型降级原因代码列表。
    # 参数 plan：本轮实际采用的群像发言计划。
    # 参数 skipped：因预算、去重或安全约束未发言的角色记录。
    # 参数 adjustments：群像去重或内容修正产生的调整记录。
    def _compose_reply(
        self,
        *,
        emitted: list[CharacterAgentReply],
        degraded: bool,
        reasons: list[str],
        plan: ClubTurnPlan,
        skipped: list[dict[str, str]],
        adjustments: list[dict[str, object]],
    ) -> ClubReply:
        lines = [
            f"{CHARACTERS[reply.agent_id].short_name}：{reply.content}"
            for reply in emitted
        ]
        return ClubReply(
            content="\n".join(lines),
            replies=emitted,
            degraded=degraded,
            degradation_reason=";".join(dict.fromkeys(reasons)),
            plan=plan,
            skipped_speakers=skipped,
            reply_adjustments=adjustments,
        )

    # 作用：把角色职责包装成不可向用户复述的内部提示片段。
    # 参数 speaker：当前准备调用或格式化的发言计划。
    def _format_role_instruction(self, speaker: ClubSpeakerPlan) -> str:
        return (
            "活动室发言编排（内部约束，不得复述）："
            f"本轮角色={speaker.role}；{speaker.instruction}"
        )

    # 作用：摘要前序角色回复，帮助后续角色接力且避免重复。
    # 参数 replies：各角色实际产生的原始回复。
    def _format_prior_replies(self, replies: list[CharacterAgentReply]) -> str:
        lines = [
            f"- {CHARACTERS[item.agent_id].short_name}: {item.content[:400]}"
            for item in replies
        ]
        return (
            "本轮活动室中已经说过的内容（仅用于接力和避免重复，不是用户原话）：\n"
            + "\n".join(lines)
        )

    # 作用：使用词元集合的 Jaccard 比例衡量两段完整回复的重复程度。
    # 参数 left：相似度计算左侧文本。
    # 参数 right：相似度计算右侧文本。
    def _similarity(self, left: str, right: str) -> float:
        left_tokens = self._tokens(left)
        right_tokens = self._tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    # 作用：删除与已有角色回答高度重合的句子，并返回删除数量。
    # 参数 content：待切句或执行重复内容清理的回复文本。
    # 参数 prior_contents：已经保留的角色回复文本列表。
    def _remove_repeated_sentences(
        self,
        content: str,
        prior_contents: list[str],
    ) -> tuple[str, int]:
        if not prior_contents:
            return content, 0
        prior_sentences = [
            sentence
            for prior in prior_contents
            for sentence in self._sentences(prior)
        ]
        kept: list[str] = []
        removed = 0
        for sentence in self._sentences(content):
            tokens = self._tokens(sentence)
            is_duplicate = len(tokens) >= 4 and any(
                self._containment_similarity(tokens, self._tokens(previous))
                >= self.SENTENCE_DUPLICATE_THRESHOLD
                for previous in prior_sentences
            )
            if is_duplicate:
                removed += 1
            else:
                kept.append(sentence)
        return "\n".join(kept).strip(), removed

    # 作用：按中英文句末标点和换行切分可比较的句子。
    # 参数 content：待切句或执行重复内容清理的回复文本。
    def _sentences(self, content: str) -> list[str]:
        return [
            part.strip()
            for part in re.split(r"(?<=[。！？!?])|\n+", content)
            if part.strip()
        ]

    # 作用：计算较短词元集合被另一集合覆盖的比例，用于句子级去重。
    # 参数 left：相似度计算左侧文本。
    # 参数 right：相似度计算右侧文本。
    def _containment_similarity(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    # 作用：将中文二元字符组和英文单词转换为轻量去重词元集合。
    # 参数 text：待转换成轻量去重词元的文本。
    def _tokens(self, text: str) -> set[str]:
        compact = re.sub(r"\s+", "", text.lower())
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
        tokens = {cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))}
        tokens.update(re.findall(r"[a-z0-9]{2,}", compact))
        return tokens
