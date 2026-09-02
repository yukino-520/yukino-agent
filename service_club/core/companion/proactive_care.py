from dataclasses import dataclass
from typing import Any


# 作用：描述一项仅供本轮决策使用的主动关怀建议及其紧急程度。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“ProactiveCareCandidate”相关的数据结构、异常类型或服务组件。
# 字段：type：该对象中的结构化字段。、channel：该对象中的结构化字段。、reason：该对象中的结构化字段。、suggestion：该对象中的结构化字段。、urgency：该对象中的结构化字段。
class ProactiveCareCandidate:
    type: str
    channel: str
    reason: str
    suggestion: str
    urgency: str = "low"

    # 作用：将关怀候选转换为便于追踪和接口输出的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "channel": self.channel,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "urgency": self.urgency,
        }


# 作用：根据安全级别与情绪状态生成克制的站内关怀候选，不直接发送消息。
# 参数：无。
class ProactiveCarePlanner:
    # 作用：按危机、连续负面和情绪惯性的优先级决定是否建议关怀。
    # 参数 mental_state：当前短期情绪趋势和长期情绪惯性的汇总状态。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    # 参数 latest_text：本轮最新用户原话。
    def plan(
        self,
        *,
        mental_state: dict[str, Any],
        safety_level: str,
        latest_text: str,
    ) -> list[ProactiveCareCandidate]:
        if safety_level == "crisis":
            return [
                ProactiveCareCandidate(
                    type="crisis_grounding",
                    channel="in_chat",
                    reason="检测到危机表达，需要优先现实安全和即时支持。",
                    suggestion="先确认用户身边是否有可信任的人，并建议联系当地紧急支持。",
                    urgency="high",
                )
            ]
        if mental_state.get("needs_gentle_followup") is True:
            streak = int(mental_state.get("negative_streak", 0))
            return [
                ProactiveCareCandidate(
                    type="gentle_followup",
                    channel="in_chat",
                    reason=f"连续 {streak} 轮负面情绪，需要更稳定的陪伴节奏。",
                    suggestion=self._suggestion(latest_text),
                    urgency="medium" if streak >= 3 else "low",
                )
            ]
        affective_state = mental_state.get("affective_state", {})
        if isinstance(affective_state, dict) and affective_state.get("needs_support") is True:
            return [
                ProactiveCareCandidate(
                    type="emotional_inertia",
                    channel="in_chat",
                    reason="当前文本较平静，但跨轮情绪状态仍明显低落。",
                    suggestion="自然承接之前的低落，不要假设用户已经完全恢复。",
                    urgency="low",
                )
            ]
        return []

    # 作用：把关怀候选整理为模型可参考的提示词片段。
    # 参数 candidates：待格式化、筛选或返回的候选集合。
    def format_for_prompt(self, candidates: list[ProactiveCareCandidate]) -> list[str]:
        return [
            f"主动关怀候选[{item.type}]：{item.suggestion}（{item.reason}）"
            for item in candidates
        ]

    # 作用：根据失眠、崩溃等显式主题选择低压力的回应建议。
    # 参数 latest_text：本轮最新用户原话。
    def _suggestion(self, latest_text: str) -> str:
        if "失眠" in latest_text or "睡" in latest_text:
            return "先承认睡眠压力，再给一个很小的今晚可执行步骤。"
        if "撑不住" in latest_text or "崩溃" in latest_text:
            return "先降低要求，陪用户把接下来十分钟拆成一个小动作。"
        return "先接住情绪，再温和询问是否要继续说一点。"
