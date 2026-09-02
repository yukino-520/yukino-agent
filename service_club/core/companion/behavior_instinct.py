import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# 作用：描述从用户明确表达中识别出的一个互动习惯候选。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“InstinctSignal”相关的数据结构、异常类型或服务组件。
# 字段：rule_key：该对象中的结构化字段。、content：该对象中的结构化字段。
class InstinctSignal:
    rule_key: str
    content: str


SIGNALS: tuple[tuple[InstinctSignal, tuple[str, ...]], ...] = (
    (
        InstinctSignal(
            "support_before_solutions",
            "用户低落时先倾听和陪伴，得到同意后再分析或给方案。",
        ),
        ("我只想说说", "听我说就好", "陪我就好", "不想解决", "先别分析"),
    ),
    (
        InstinctSignal(
            "structured_advice",
            "用户寻求办法时偏好具体分析、可执行步骤和明确选项。",
        ),
        ("帮我分析", "给我建议", "告诉我怎么做", "具体怎么做", "列个步骤"),
    ),
    (
        InstinctSignal(
            "low_question_pressure",
            "减少连续追问；一次最多问一个容易回答的问题。",
        ),
        ("别一直问", "不要一直问", "别追问", "不想回答问题"),
    ),
    (
        InstinctSignal(
            "direct_communication",
            "表达直接清楚，不绕弯，不用空泛铺垫。",
        ),
        ("直接一点", "直接说", "别绕弯", "不用铺垫"),
    ),
    (
        InstinctSignal(
            "gentle_tone",
            "保持温和克制的语气，避免尖锐、责备和居高临下。",
        ),
        ("温柔一点", "别太凶", "不要责备我", "语气柔和"),
    ),
)
POSITIVE_FEEDBACK = ("这样好多了", "就是这种感觉", "你这样说我舒服", "这样就好")
NEGATIVE_FEEDBACK = ("别这样说", "不是这种感觉", "这样让我不舒服", "你没懂我")


# 作用：从重复证据学习表达习惯，并仅把达到阈值的习惯注入提示词。
# 参数：无。
class BehaviorInstinctManager:
    ACTIVE_THRESHOLD = 0.7
    EFFECTIVE_THRESHOLD = 0.65
    DAILY_DECAY = 0.995

    # 作用：注入事实存储与时钟，用于记录证据、衰减置信度和归档旧假设。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(self, store: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._clock = clock

    # 作用：从当前原话提取互动偏好证据，并结合近期反馈更新既有习惯。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def observe(self, session_id: str, text: str) -> dict[str, object]:
        now = self._clock()
        archived = self.store.archive_stale_instincts(
            session_id,
            active_before=now - 30 * 86400,
            candidate_before=now - 14 * 86400,
        )
        feedback = self._apply_feedback(session_id, text, now)
        observations = []
        for signal, phrases in SIGNALS:
            if any(phrase in text for phrase in phrases):
                evidence_hash = hashlib.sha256(
                    f"{signal.rule_key}:{text.strip()}".encode()
                ).hexdigest()[:20]
                observations.append(
                    self.store.observe_instinct(
                        session_id=session_id,
                        rule_key=signal.rule_key,
                        content=signal.content,
                        evidence_hash=evidence_hash,
                        now=now,
                    )
                )
        return {
            "observations": observations,
            "feedback": feedback,
            "archived_stale": archived,
        }

    # 作用：读取活跃习惯并应用按天衰减，过滤掉有效置信度不足的记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def active(self, session_id: str, limit: int = 6) -> list[dict[str, Any]]:
        now = self._clock()
        active: list[dict[str, Any]] = []
        for item in self.store.list_instincts(
            session_id,
            statuses=("active",),
            limit=limit,
        ):
            age_days = max(0.0, now - float(item["last_evidence_at"])) / 86400
            effective_confidence = float(item["confidence"]) * self.DAILY_DECAY**age_days
            item["effective_confidence"] = round(effective_confidence, 4)
            if effective_confidence >= self.EFFECTIVE_THRESHOLD:
                active.append(item)
        return active

    # 作用：将有效习惯整理成内部提示词，并记录这些习惯已被本轮使用。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def build_prompt(self, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        instincts = self.active(session_id)
        if not instincts:
            return "", []
        self.store.mark_instincts_used(
            [int(item["id"]) for item in instincts],
            self._clock(),
        )
        lines = [
            f"- {item['content']}（有效置信度 {float(item['effective_confidence']):.2f}）"
            for item in instincts
        ]
        prompt = (
            "已学习的互动假设（只影响表达方式，不得复述给用户）：\n"
            + "\n".join(lines)
            + "\n当前用户原话、明确偏好和安全规则优先；有冲突时忽略这些假设。"
        )
        return prompt, instincts

    # 作用：返回会话中的全部习惯候选，包含未激活和已归档状态。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def list_all(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_instincts(session_id)

    # 作用：撤销指定习惯，使错误学习不再影响后续表达。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 instinct_id：目标行为习惯记录的数据库标识。
    def revoke(self, session_id: str, instinct_id: int) -> bool:
        return self.store.revoke_instinct(session_id, instinct_id)

    # 作用：暴露学习阈值、衰减与保留周期等运行配置。
    # 参数：无。
    def status(self) -> dict[str, object]:
        return {
            "active_threshold": self.ACTIVE_THRESHOLD,
            "effective_threshold": self.EFFECTIVE_THRESHOLD,
            "daily_decay": self.DAILY_DECAY,
            "candidate_ttl_days": 14,
            "active_ttl_days": 30,
            "signal_count": len(SIGNALS),
        }

    # 作用：将用户对上一轮风格的明确正负反馈作用到近期已使用习惯上。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def _apply_feedback(
        self,
        session_id: str,
        text: str,
        now: float,
    ) -> list[dict[str, Any]]:
        if any(phrase in text for phrase in POSITIVE_FEEDBACK):
            return self.store.adjust_recent_instincts(
                session_id,
                delta=0.08,
                used_after=now - 24 * 60 * 60,
            )
        if any(phrase in text for phrase in NEGATIVE_FEEDBACK):
            return self.store.adjust_recent_instincts(
                session_id,
                delta=-0.25,
                used_after=now - 24 * 60 * 60,
            )
        return []
