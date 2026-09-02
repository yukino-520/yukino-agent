import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

NEGATIVE_EMOTIONS = {"sad", "lonely", "anxious", "angry"}


# 作用：描述用户离开后重返会话时是否需要承接，以及允许回忆的范围。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“ReunionPlan”相关的数据结构、异常类型或服务组件。
# 字段：active：该对象中的结构化字段。、idle_seconds：该对象中的结构化字段。、tier：该对象中的结构化字段。、previous_emotion：该对象中的结构化字段。、previous_topic：该对象中的结构化字段。、guidance：该对象中的结构化字段。
class ReunionPlan:
    active: bool
    idle_seconds: int = 0
    tier: str = "none"
    previous_emotion: str = "neutral"
    previous_topic: str = ""
    guidance: str = ""

    # 作用：将重聚计划转换为可追踪的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "idle_seconds": self.idle_seconds,
            "tier": self.tier,
            "previous_emotion": self.previous_emotion,
            "previous_topic": self.previous_topic,
            "guidance": self.guidance,
        }


# 作用：根据离线时长、历史情绪和关系边界恢复克制的对话连续性。
# 参数：无。
class ReunionPlanner:
    """Restores conversational continuity after a meaningful absence."""

    # 作用：注入时钟，便于稳定计算用户离线时长。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    # 作用：在非危机状态下生成重聚层级，并尊重不回提话题等关系边界。
    # 参数 latest_conversation：用户返回前最近一轮对话事实。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    # 参数 previous_affective_state：用户离开前经过衰减评估的长期情绪状态。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    def plan(
        self,
        latest_conversation: dict[str, Any] | None,
        *,
        safety_level: str,
        previous_affective_state: dict[str, Any] | None = None,
        relationship: dict[str, Any] | None = None,
    ) -> ReunionPlan:
        if not latest_conversation or safety_level == "crisis":
            return ReunionPlan(active=False)

        created_at = float(latest_conversation.get("created_at", 0))
        idle_seconds = max(0, int(self._clock() - created_at))
        if idle_seconds < 30 * 60:
            return ReunionPlan(active=False, idle_seconds=idle_seconds)

        previous_emotion = str(latest_conversation.get("emotion", "neutral"))
        if previous_affective_state and previous_affective_state.get("needs_support") is True:
            previous_emotion = str(previous_affective_state.get("label", previous_emotion))
        relationship = relationship or {}
        boundaries = relationship.get("boundaries", [])
        previous_topic = str(latest_conversation.get("user_message", "")).strip()
        if "no_topic_recall" in boundaries:
            previous_topic = ""
        if idle_seconds >= 4 * 60 * 60:
            tier = "long"
        else:
            tier = "medium"

        if relationship.get("rupture_state") in {"ruptured", "repairing"}:
            guidance = "保持克制，不假装关系已恢复；先兑现上次确认的边界，必要时简短承认影响。"
        elif relationship.get("needs_trust_rebuild") is True:
            guidance = (
                "关系已停止破裂，但信任仍在重建；克制欢迎，"
                "不使用高亲密称呼，也不主动翻旧账。"
            )
        elif previous_emotion in NEGATIVE_EMOTIONS:
            guidance = "先轻轻关心上一轮低落的感受有没有缓一点；不要假设问题已经解决。"
        elif tier == "long":
            guidance = "自然欢迎用户回来，关心这段时间过得怎样；只有合适时才轻提旧话题。"
        else:
            guidance = "自然承接用户回来这件事，可以轻轻问候或接回上一轮话题。"
        return ReunionPlan(
            active=True,
            idle_seconds=idle_seconds,
            tier=tier,
            previous_emotion=previous_emotion,
            previous_topic=previous_topic,
            guidance=guidance,
        )

    # 作用：把有效重聚计划转成受长度限制的内部连续性提示。
    # 参数 plan：待格式化或查询的上下文、重聚等规划结果。
    def format_for_prompt(self, plan: ReunionPlan) -> str | None:
        if not plan.active:
            return None
        topic = plan.previous_topic[:80] or "无明确旧话题"
        return (
            f"重聚上下文[{plan.tier}]：用户离开约{self._format_duration(plan.idle_seconds)}，"
            f"上次情绪为{plan.previous_emotion}，上次说“{topic}”。{plan.guidance}"
        )

    # 作用：将离线秒数转换为分钟、小时或天的可读表达。
    # 参数 seconds：待转换为可读时长的秒数。
    def _format_duration(self, seconds: int) -> str:
        if seconds < 60 * 60:
            return f"{max(1, seconds // 60)}分钟"
        if seconds < 24 * 60 * 60:
            return f"{seconds // 3600}小时"
        return f"{seconds // (24 * 60 * 60)}天"
