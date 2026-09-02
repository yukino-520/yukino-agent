import hashlib
from dataclasses import dataclass

from service_club.core.memory.permanent_memory import PermanentMemoryManager
from service_club.core.memory.reflection import TurnReflection


# 作用：描述一条轮次反思是否被提升为跨会话永久记忆及其原因。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“MemoryConsolidationResult”相关的数据结构、异常类型或服务组件。
# 字段：stored：该对象中的结构化字段。、key：该对象中的结构化字段。、reason：该对象中的结构化字段。
class MemoryConsolidationResult:
    stored: bool
    key: str = ""
    reason: str = ""

    # 作用：将巩固结果转换为可追踪的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "stored": self.stored,
            "key": self.key,
            "reason": self.reason,
        }


# 作用：只把安全且高显著性的轮次反思提升到永久事实源。
# 参数：无。
class MemoryConsolidator:
    THRESHOLD = 0.9

    # 作用：校验安全级别和显著性阈值后，写入可跨会话使用的反思记忆。
    # 参数 reflection：待判断是否提升为永久记忆的轮次反思。
    # 参数 permanent_memory：提供跨会话永久事实读写能力的管理器。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    def consolidate(
        self,
        *,
        reflection: TurnReflection,
        permanent_memory: PermanentMemoryManager,
        safety_level: str = "normal",
    ) -> MemoryConsolidationResult:
        if safety_level != "normal":
            return MemoryConsolidationResult(
                stored=False,
                reason="blocked_by_safety_privacy_guard",
            )
        if reflection.salience < self.THRESHOLD:
            return MemoryConsolidationResult(
                stored=False,
                reason="reflection_salience_below_threshold",
            )
        key = self._key(reflection.summary)
        permanent_memory.store(
            reflection.session_id,
            "reflection",
            key,
            reflection.summary,
            source="reflection_engine",
        )
        return MemoryConsolidationResult(
            stored=True,
            key=key,
            reason="high_salience_reflection",
        )

    # 作用：根据反思摘要生成稳定短键，避免同一内容被重复巩固。
    # 参数 summary：待保存或格式化的摘要文本。
    def _key(self, summary: str) -> str:
        digest = hashlib.sha1(summary.encode("utf-8")).hexdigest()[:10]
        return f"turn_{digest}"
