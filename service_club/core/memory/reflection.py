from dataclasses import dataclass

from service_club.core.types import CharacterId, ChatMode, EmotionLabel


# 作用：保存一轮对话的压缩反思及其情绪、角色、模式和显著性。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“TurnReflection”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、summary：该对象中的结构化字段。、emotion：该对象中的结构化字段。、character：该对象中的结构化字段。、chat_mode：该对象中的结构化字段。、salience：该对象中的结构化字段。
class TurnReflection:
    session_id: str
    summary: str
    emotion: EmotionLabel
    character: CharacterId
    chat_mode: ChatMode
    salience: float

    # 作用：将轮次反思转换为可持久化和审计的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "emotion": self.emotion,
            "character": self.character,
            "chat_mode": self.chat_mode,
            "salience": self.salience,
        }


# 作用：以确定性规则生成简短轮次反思，并评估是否值得长期巩固。
# 参数：无。
class TurnReflectionEngine:
    NEGATIVE_EMOTIONS = {"sad", "lonely", "anxious", "angry"}

    # 作用：压缩用户与助手原话，组合成带上下文元数据的轮次反思。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 user_text：用于生成反思摘要和显著性判断的用户原话。
    # 参数 assistant_reply：本轮助手最终回复文本。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 chat_mode：本轮采用的单角色或群像聊天模式。
    def create(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_reply: str,
        emotion: EmotionLabel,
        character: CharacterId,
        chat_mode: ChatMode,
    ) -> TurnReflection:
        user_part = self._compact(user_text)
        reply_part = self._compact(assistant_reply)
        summary = f"用户说：{user_part}；{character}回应：{reply_part}"
        return TurnReflection(
            session_id=session_id,
            summary=summary,
            emotion=emotion,
            character=character,
            chat_mode=chat_mode,
            salience=self._salience(user_text, emotion),
        )

    # 作用：归一化空白并限制单侧摘要长度，控制反思体积。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def _compact(self, text: str, limit: int = 60) -> str:
        compacted = " ".join(text.split())
        return compacted[:limit]

    # 作用：根据负面情绪、显式记忆和高压力表达计算反思显著性。
    # 参数 user_text：用于生成反思摘要和显著性判断的用户原话。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    def _salience(self, user_text: str, emotion: EmotionLabel) -> float:
        score = 0.55
        if emotion in self.NEGATIVE_EMOTIONS:
            score += 0.25
        if any(word in user_text for word in ("记住", "以后叫我", "喜欢", "讨厌")):
            score += 0.15
        if any(word in user_text for word in ("撑不住", "崩溃", "失眠", "难受")):
            score += 0.15
        return min(score, 1.0)
