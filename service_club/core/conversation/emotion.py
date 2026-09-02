from dataclasses import dataclass

from service_club.core.types import EmotionLabel


# 作用：保存规则识别出的情绪标签、强度和正负倾向。
# 参数 label：规则识别出的标准化情绪标签。
# 参数 intensity：情绪关键词命中形成的强度值。
# 参数 valence：情绪的正向、负向或中性倾向。
@dataclass(frozen=True)
# 作用：定义“EmotionResult”相关的数据结构、异常类型或服务组件。
# 字段：label：该对象中的结构化字段。、intensity：该对象中的结构化字段。、valence：该对象中的结构化字段。
class EmotionResult:
    label: EmotionLabel
    intensity: float
    valence: str


KEYWORDS: list[tuple[EmotionLabel, tuple[str, ...], str]] = [
    ("lonely", ("孤独", "没人懂", "一个人", "寂寞", "被丢下"), "negative"),
    (
        "anxious",
        (
            "焦虑",
            "紧张",
            "担心",
            "睡不着",
            "失眠",
            "难眠",
            "睡不好",
            "休息不好",
            "慌",
            "害怕失败",
        ),
        "negative",
    ),
    ("angry", ("生气", "气死", "火大", "烦死", "讨厌", "不公平", "好烦"), "negative"),
    ("sad", ("难过", "难受", "想哭", "崩溃", "失落", "好累", "撑不住"), "negative"),
    ("shy", ("害羞", "不好意思", "脸红", "尴尬"), "neutral"),
    ("thinking", ("想想", "怎么办", "分析", "选择", "纠结"), "neutral"),
    ("happy", ("开心", "高兴", "好耶", "喜欢", "太棒", "顺利", "很棒"), "positive"),
]


# 作用：根据中文情绪关键词识别当前最明显的情绪及其强度。
# 参数 text：待识别、切分、清理或合成语音的输入文本。
def detect_emotion(text: str) -> EmotionResult:
    if not text.strip():
        return EmotionResult(label="neutral", intensity=0.0, valence="neutral")
    best_label: EmotionLabel = "neutral"
    best_hits = 0
    best_valence = "neutral"
    for label, words, valence in KEYWORDS:
        hits = sum(1 for word in words if word in text)
        if hits > best_hits:
            best_label = label
            best_hits = hits
            best_valence = valence
    if best_hits == 0:
        return EmotionResult(label="neutral", intensity=0.0, valence="neutral")
    return EmotionResult(
        label=best_label,
        intensity=min(0.35 + best_hits * 0.2, 1.0),
        valence=best_valence,
    )


# 作用：把结构化情绪结果翻译为模型可执行的回应策略提示。
# 参数 emotion：本轮结构化情绪结果或用于媒体选择的情绪标签。
def build_emotion_hint(emotion: EmotionResult) -> str:
    if emotion.label in ("sad", "lonely"):
        return "用户现在情绪低落，先陪伴和接住感受，不要急着说教。"
    if emotion.label == "anxious":
        return "用户现在焦虑，先降低压力，再给一个很小的下一步。"
    if emotion.label == "angry":
        return "用户现在愤怒，先承认委屈和边界，再帮助整理事实。"
    if emotion.label == "happy":
        return "用户心情不错，可以轻快回应并记住这份好状态。"
    if emotion.label == "shy":
        return "用户有点害羞，语气轻柔，不要逼问。"
    if emotion.label == "thinking":
        return "用户在思考或纠结，可以帮他拆解选择。"
    return "用户没有明显强情绪，自然回应。"
