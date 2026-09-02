import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

SENSITIVE_TERMS = (
    "密码",
    "验证码",
    "身份证",
    "银行卡",
    "精确地址",
    "住址",
    "病历",
    "诊断",
    "自杀",
    "自残",
)


# 作用：表示从用户明确陈述中提取、尚待写入事实源的画像事实。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“ProfileFactCandidate”相关的数据结构、异常类型或服务组件。
# 字段：fact_key：该对象中的结构化字段。、category：该对象中的结构化字段。、value：该对象中的结构化字段。、polarity：该对象中的结构化字段。、source：该对象中的结构化字段。
class ProfileFactCandidate:
    fact_key: str
    category: str
    value: str
    polarity: str = "positive"
    source: str = "explicit_user_statement"


# 作用：仅学习可撤销、可归一化且非敏感的显式用户画像事实。
# 参数：无。
class ProfileFactLearner:
    """Learns only explicit, normalized and revocable user facts."""

    # 作用：注入画像事实存储和时钟，用于统计互动与记录证据时间。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(self, store: Any, *, clock=time.time) -> None:  # noqa: ANN001
        self.store = store
        self._clock = clock

    # 作用：在隐私门禁通过后抽取事实，以证据哈希持久化并处理称呼冲突撤销。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    # 参数 learned_preferences：已经从原话提取、可作为画像候选的偏好列表。
    def observe(
        self,
        session_id: str,
        text: str,
        *,
        safety_level: str,
        learned_preferences: list[Any] | None = None,
    ) -> dict[str, object]:
        now = self._clock()
        stats = self.store.record_profile_interaction(session_id, len(text), now=now)
        if not self.allows_persistence(text, safety_level=safety_level):
            return {
                "facts": [],
                "stats": stats,
                "skipped": True,
                "reason": "safety_or_privacy_guard",
            }
        candidates = self._extract(text, learned_preferences or [])
        revoked = []
        for preference in learned_preferences or []:
            if str(preference.key) != "avoid_address":
                continue
            avoided = str(preference.value)
            for fact in self.current(session_id):
                if fact["fact_key"] == "preferred_name" and str(fact["value"]) in avoided:
                    if self.store.revoke_profile_fact(session_id, int(fact["id"])):
                        revoked.append(int(fact["id"]))
        facts = []
        for candidate in candidates:
            evidence_hash = hashlib.sha256(
                (
                    f"{candidate.fact_key}:{candidate.value}:"
                    f"{candidate.polarity}:{text.strip()}"
                ).encode()
            ).hexdigest()[:24]
            facts.append(
                self.store.observe_profile_fact(
                    session_id=session_id,
                    fact_key=candidate.fact_key,
                    category=candidate.category,
                    value=candidate.value,
                    polarity=candidate.polarity,
                    source=candidate.source,
                    evidence_hash=evidence_hash,
                    now=now,
                )
            )
        return {
            "facts": facts,
            "revoked": revoked,
            "stats": stats,
            "skipped": False,
            "reason": "",
        }

    # 作用：返回会话当前生效、未被替代或撤销的画像事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def current(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_profile_facts(session_id, statuses=("current",))

    # 作用：返回会话画像事实的完整生命周期记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def list_all(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_profile_facts(session_id)

    # 作用：显式撤销指定画像事实，使其不再进入提示词。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 fact_id：目标画像事实的数据库标识。
    def revoke(self, session_id: str, fact_id: int) -> bool:
        return self.store.revoke_profile_fact(session_id, fact_id)

    # 作用：将当前事实压缩为提示词，并声明当前原话优先于历史画像。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def format_for_prompt(self, session_id: str) -> str:
        facts = self.current(session_id)
        if not facts:
            return ""
        lines = [
            f"- {item['category']}/{item['fact_key']}: "
            f"{item['polarity']} {item['value']}（置信度 {item['confidence']:.2f}）"
            for item in facts[:10]
        ]
        return (
            "可证实用户画像（只用于个性化，不得推测或复述置信度）：\n"
            + "\n".join(lines)
            + "\n若当前原话与画像冲突，以当前原话为准。"
        )

    # 作用：说明画像学习的证据、冲突处理、撤销和隐私保护能力。
    # 参数：无。
    def status(self) -> dict[str, object]:
        return {
            "mode": "explicit_evidence_only",
            "supports_conflict_supersession": True,
            "supports_revocation": True,
            "privacy_guard_terms": len(SENSITIVE_TERMS),
        }

    # 作用：仅允许正常安全级别且不含敏感词的文本进入持久画像事实源。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    def allows_persistence(self, text: str, *, safety_level: str) -> bool:
        return safety_level == "normal" and not self._contains_sensitive(text)

    # 作用：合并已学习偏好与显式兴趣、习惯规则，按事实键去重候选。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 learned_preferences：已经从原话提取、可作为画像候选的偏好列表。
    def _extract(
        self, text: str, learned_preferences: list[Any]
    ) -> list[ProfileFactCandidate]:
        candidates: list[ProfileFactCandidate] = []
        for preference in learned_preferences:
            key = str(preference.key)
            value = self._clean_value(str(preference.value))
            if not value or self._contains_sensitive(value):
                continue
            category = "identity" if key == "preferred_name" else "communication"
            candidates.append(ProfileFactCandidate(key, category, value))

        negative = re.search(r"我(?:不喜欢|讨厌)\s*([^，。！？,.!?]{1,24})", text)
        positive = re.search(r"我(?:很喜欢|喜欢)\s*([^，。！？,.!?]{1,24})", text)
        match = negative or positive
        if match:
            value = self._clean_value(match.group(1))
            if value and not self._contains_sensitive(value):
                candidates.append(
                    ProfileFactCandidate(
                        fact_key=f"interest:{value}",
                        category="interest",
                        value=value,
                        polarity="negative" if negative else "positive",
                    )
                )
        routine = re.search(r"我(?:通常|习惯)([^，。！？,.!?]{2,28})", text)
        if routine:
            value = self._clean_value(routine.group(1))
            if value and not self._contains_sensitive(value):
                key_hash = hashlib.sha256(value.encode()).hexdigest()[:10]
                candidates.append(
                    ProfileFactCandidate(f"routine:{key_hash}", "routine", value)
                )
        deduplicated = {candidate.fact_key: candidate for candidate in candidates}
        return list(deduplicated.values())

    # 作用：清理偏好值两端语气词和多余空白，并限制最大长度。
    # 参数 value：待规范化、持久化或解析的业务值。
    def _clean_value(self, value: str) -> str:
        value = value.strip(" ：:，。！？,.!?的了呢吧")
        value = re.sub(r"\s+", " ", value)
        return value[:40]

    # 作用：检查文本是否含密码、身份、医疗或自伤等禁止持久化信息。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _contains_sensitive(self, text: str) -> bool:
        return any(term in text for term in SENSITIVE_TERMS)
