import hashlib
import os
import time
from collections.abc import Callable
from typing import Any

from service_club.core.conversation.response_policy import ResponsePolicy

NEGATIVE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "question_pressure",
        ("别一直问", "不要一直问", "问太多了", "别再问了", "一直追问"),
    ),
    (
        "advice_too_early",
        ("别急着给建议", "我不是要建议", "先别分析", "不想听建议", "别说教"),
    ),
    (
        "tone_too_cold",
        ("太冷漠", "太敷衍", "你在敷衍", "像客服", "太生硬"),
    ),
    (
        "misunderstood",
        ("你没懂我", "你理解错了", "不是这个意思", "答非所问", "完全没听懂"),
    ),
    (
        "boundary_violation",
        ("你越界了", "别这么叫我", "这个称呼不舒服", "别装得太亲密"),
    ),
)
POSITIVE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "felt_supported",
        ("好多了", "舒服多了", "被你接住了", "谢谢你陪我", "这样陪着就好"),
    ),
    (
        "advice_helped",
        ("这个建议有用", "这个办法不错", "我愿意试试看", "这样做有帮助"),
    ),
    (
        "felt_understood",
        ("你真的懂我", "就是这个意思", "你说到点上了", "你听懂我了"),
    ),
)
LESSONS = {
    "question_pressure": "减少追问压力；一轮最多问一个容易回答的问题，也允许不回答。",
    "advice_too_early": "先承接和确认感受，得到用户同意后再分析或给建议。",
    "tone_too_cold": "保持自然、具体的陪伴语气，避免客服腔、模板化安慰和生硬结论。",
    "misunderstood": "先复述自己理解到的重点并留出修正空间，不要急着替用户下结论。",
    "boundary_violation": "降低亲密假设，遵守称呼与互动边界，不用关系进度压迫用户。",
    "felt_supported": "此前低压力、先陪伴的节奏得到多次明确肯定，可以优先沿用。",
    "advice_helped": "此前的小步、可执行建议得到多次明确肯定，可以在用户需要时沿用。",
    "felt_understood": "此前先理解再回应的方式得到多次明确肯定，继续保持具体而克制的共情。",
}


# 作用：从用户对上一轮回复的明确反馈中学习有效策略，不保存原始对话文本。
# 参数：无。
class InteractionOutcomeLearner:
    """Learns whether the previous companion response helped, without storing text."""

    MAX_FEEDBACK_AGE_SECONDS = 24 * 60 * 60
    EVIDENCE_THRESHOLD = 2
    LESSON_TTL_SECONDS = 30 * 24 * 60 * 60

    # 作用：注入事实存储与时钟，用于关联上一轮策略快照和当前反馈。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._clock = clock

    # 作用：根据环境开关判断互动结果学习是否启用。
    # 参数：无。
    @property
    # 作用：执行“enabled”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def enabled(self) -> bool:
        value = os.getenv("YUKINO_INTERACTION_LEARNING_ENABLED", "true").lower()
        return value not in {"0", "false", "off", "no"}

    # 作用：将当前明确反馈归因到最近策略快照，并以哈希证据去重后持久化。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    def observe(self, session_id: str, text: str, *, safety_level: str) -> dict[str, Any]:
        if not self.enabled:
            return {"observed": False, "reason": "disabled"}
        if safety_level != "normal":
            return {"observed": False, "reason": "safety_blocked"}
        signal = self._detect_signal(text)
        if signal is None:
            return {"observed": False, "reason": "no_explicit_feedback"}
        try:
            snapshot = self.store.latest_strategy_snapshot(session_id)
        except Exception:
            return {"observed": False, "reason": "store_unavailable"}
        if snapshot is None:
            return {"observed": False, "reason": "no_previous_strategy"}
        age_seconds = max(0.0, self._clock() - float(snapshot["created_at"]))
        if age_seconds > self.MAX_FEEDBACK_AGE_SECONDS:
            return {"observed": False, "reason": "previous_strategy_stale"}

        sentiment, signal_key = signal
        normalized = " ".join(text.strip().split())
        evidence_hash = hashlib.sha256(
            f"{signal_key}:{normalized}".encode()
        ).hexdigest()[:24]
        try:
            changed = self.store.record_interaction_outcome(
                session_id=session_id,
                character=str(snapshot["character"]),
                sentiment=sentiment,
                signal_key=signal_key,
                strategy_signature=str(snapshot["strategy_signature"]),
                response_hash=str(snapshot["response_hash"]),
                evidence_hash=evidence_hash,
                created_at=self._clock(),
            )
        except Exception:
            return {"observed": False, "reason": "store_unavailable"}
        return {
            "observed": changed,
            "reason": "recorded" if changed else "duplicate_evidence",
            "sentiment": sentiment,
            "signal_key": signal_key,
            "character": snapshot["character"],
            "strategy_signature": snapshot["strategy_signature"],
        }

    # 作用：保存本轮回复采用的策略签名和响应哈希，供下一轮反馈归因。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 content：待保存、评分、格式化或发送的业务正文。
    # 参数 policy：本轮实际采用的回复策略对象。
    def record_strategy(
        self,
        *,
        session_id: str,
        character: str,
        content: str,
        policy: ResponsePolicy,
    ) -> bool:
        if not self.enabled:
            return False
        response_hash = hashlib.sha256(content.encode()).hexdigest()[:24]
        signature = self._strategy_signature(policy)
        try:
            return self.store.save_strategy_snapshot(
                session_id=session_id,
                character=character,
                response_hash=response_hash,
                strategy_signature=signature,
                opening_move=policy.opening_move,
                advice_mode=policy.advice_mode,
                question_budget=policy.question_budget,
                roleplay_intensity=policy.roleplay_intensity,
                created_at=self._clock(),
            )
        except Exception:
            return False

    # 作用：汇总达到次数和时效阈值的历史反馈，生成内部策略提示词。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    def build_prompt(
        self,
        session_id: str,
        character: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        try:
            stats = self.store.interaction_outcome_stats(session_id, character)
        except Exception:
            return "", []
        active = [
            item
            for item in stats
            if int(item["occurrences"]) >= self.EVIDENCE_THRESHOLD
            and self._clock() - float(item["last_seen"]) <= self.LESSON_TTL_SECONDS
        ][:5]
        if not active:
            return "", []
        lines = [
            f"- {LESSONS[item['signal_key']]}"
            f"（{item['sentiment']} 证据 {item['occurrences']} 次）"
            for item in active
        ]
        return (
            "互动结果学习（内部策略，不得复述统计或假装记得原话）：\n"
            + "\n".join(lines)
            + "\n当前原话、明确偏好、关系边界和安全规则优先。",
            active,
        )

    # 作用：返回会话的互动结果事件及最近一次策略快照。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def list_all(self, session_id: str, limit: int = 50) -> dict[str, Any]:
        return {
            "events": self.store.list_interaction_outcomes(session_id, limit=limit),
            "latest_strategy": self.store.latest_strategy_snapshot(session_id),
        }

    # 作用：汇总学习开关、阈值和全局存储统计，存储异常时安全降级。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        try:
            stats = self.store.interaction_outcome_global_stats()
        except Exception:
            stats = {"event_count": 0, "session_count": 0, "store_ok": False}
        return {
            "enabled": self.enabled,
            "evidence_threshold": self.EVIDENCE_THRESHOLD,
            "feedback_max_age_seconds": self.MAX_FEEDBACK_AGE_SECONDS,
            "lesson_ttl_seconds": self.LESSON_TTL_SECONDS,
            "signal_count": len(NEGATIVE_SIGNALS) + len(POSITIVE_SIGNALS),
            **stats,
        }

    # 作用：仅从预定义的明确措辞中识别正负反馈，避免猜测用户意图。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _detect_signal(self, text: str) -> tuple[str, str] | None:
        for signal_key, phrases in NEGATIVE_SIGNALS:
            if any(phrase in text for phrase in phrases):
                return "negative", signal_key
        for signal_key, phrases in POSITIVE_SIGNALS:
            if any(phrase in text for phrase in phrases):
                return "positive", signal_key
        return None

    # 作用：把响应策略字段编码为稳定签名，用于按策略聚合反馈证据。
    # 参数 policy：本轮实际采用的回复策略对象。
    def _strategy_signature(self, policy: ResponsePolicy) -> str:
        return (
            f"opening={policy.opening_move}|advice={policy.advice_mode}|"
            f"questions={policy.question_budget}|roleplay={policy.roleplay_intensity}"
        )
