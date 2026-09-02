from dataclasses import dataclass

from service_club.core.memory import MemoryManager
from service_club.core.memory.permanent_memory import PermanentMemoryManager


# 作用：描述一条与当前话题相关、可主动注入的回忆及其来源和分数。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“SpontaneousRecall”相关的数据结构、异常类型或服务组件。
# 字段：content：该对象中的结构化字段。、source：该对象中的结构化字段。、reason：该对象中的结构化字段。、score：该对象中的结构化字段。
class SpontaneousRecall:
    content: str
    source: str
    reason: str
    score: float

    # 作用：将主动回忆候选转换为便于观测的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "content": self.content,
            "source": self.source,
            "reason": self.reason,
            "score": self.score,
        }


# 作用：融合会话记忆和永久记忆，按当前话题与情绪筛选主动回忆。
# 参数：无。
class SpontaneousRecallEngine:
    KEYWORD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("sleep", ("失眠", "睡不着", "睡眠", "夜里", "熬夜", "醒")),
        ("pressure", ("累", "压力", "撑不住", "崩溃", "难受")),
        ("lonely", ("孤独", "一个人", "没人", "陪我")),
        ("preference", ("喜欢", "讨厌", "习惯", "以后叫我")),
    )
    EMOTION_BOOST = {"sad", "anxious", "lonely", "angry"}

    # 作用：扩展查询词、搜索两类事实源并统一打分，返回最高相关候选。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 emotion_label：当前情绪标签，用于调整回忆相关度。
    # 参数 memory：提供会话记忆事实与混合检索能力的管理器。
    # 参数 permanent_memory：提供跨会话永久事实读写能力的管理器。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def collect(
        self,
        *,
        session_id: str,
        text: str,
        emotion_label: str,
        memory: MemoryManager,
        permanent_memory: PermanentMemoryManager,
        limit: int = 3,
    ) -> list[SpontaneousRecall]:
        candidates: list[SpontaneousRecall] = []
        terms = self._expanded_terms(text)
        for hit in memory.search_memories(session_id, " ".join(terms), limit=20):
            item = hit.content
            topic_score = self._score(text, item, terms, emotion_label)
            score = round(hit.score * 4 + topic_score, 4)
            if score >= 1:
                candidates.append(
                    SpontaneousRecall(
                        content=item,
                        source="session_memory",
                        reason="；".join(hit.reasons) or "和当前话题或情绪相关",
                        score=score,
                    )
                )
        for item in permanent_memory.entries_for_prompt(session_id):
            score = self._score(text, item, terms, emotion_label)
            if score >= 1:
                candidates.append(
                    SpontaneousRecall(
                        content=item,
                        source="permanent_memory",
                        reason="跨会话偏好与当前表达相关",
                        score=score,
                    )
                )
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates[:limit]

    # 作用：将回忆候选标注来源与理由后整理为提示词片段。
    # 参数 recalls：待注入提示词的主动回忆候选列表。
    def format_for_prompt(self, recalls: list[SpontaneousRecall]) -> list[str]:
        return [
            f"主动回忆[{item.source}]：{item.content}（{item.reason}）"
            for item in recalls
        ]

    # 作用：根据睡眠、压力、孤独和偏好主题扩展并去重查询词。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _expanded_terms(self, text: str) -> list[str]:
        terms = [term for term in text.replace("，", " ").replace("。", " ").split() if term]
        for _, words in self.KEYWORD_GROUPS:
            if any(word in text for word in words):
                terms.extend(words)
        return list(dict.fromkeys(terms))

    # 作用：结合词项重合、主题组命中和负面情绪加权计算回忆相关度。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 candidate：待评分或处理的候选内容。
    # 参数 terms：用于匹配候选记忆的扩展查询词列表。
    # 参数 emotion_label：当前情绪标签，用于调整回忆相关度。
    def _score(
        self,
        text: str,
        candidate: str,
        terms: list[str],
        emotion_label: str,
    ) -> float:
        score = float(sum(1 for term in terms if term and term in candidate))
        for _, words in self.KEYWORD_GROUPS:
            if any(word in text for word in words) and any(word in candidate for word in words):
                score += 1.5
        if emotion_label in self.EMOTION_BOOST and score > 0:
            score += 0.5
        return score
