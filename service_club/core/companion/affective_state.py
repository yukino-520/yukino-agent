import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from service_club.core.conversation.emotion import EmotionResult
from service_club.core.types import EmotionLabel


# 作用：表示愉悦度、唤醒度和支配感三个维度的连续情绪坐标。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“PADVector”相关的数据结构、异常类型或服务组件。
# 字段：pleasure：该对象中的结构化字段。、arousal：该对象中的结构化字段。、dominance：该对象中的结构化字段。
class PADVector:
    pleasure: float
    arousal: float
    dominance: float

    # 作用：将三个情绪维度限制在各自合法区间，避免状态累积后越界。
    # 参数：无。
    def clamp(self) -> "PADVector":
        return PADVector(
            pleasure=max(-1.0, min(1.0, self.pleasure)),
            arousal=max(0.0, min(1.0, self.arousal)),
            dominance=max(0.0, min(1.0, self.dominance)),
        )

    # 作用：按权重融合新旧 PAD 状态，并返回经过边界约束的结果。
    # 参数 other：待与当前值融合的另一份同类状态。
    # 参数 weight：新状态在融合结果中占用的归一化权重。
    def blend(self, other: "PADVector", weight: float) -> "PADVector":
        weight = max(0.0, min(1.0, weight))
        return PADVector(
            pleasure=self.pleasure * (1 - weight) + other.pleasure * weight,
            arousal=self.arousal * (1 - weight) + other.arousal * weight,
            dominance=self.dominance * (1 - weight) + other.dominance * weight,
        ).clamp()

    # 作用：将 PAD 坐标转成适合持久化和接口返回的精简字典。
    # 参数：无。
    def as_dict(self) -> dict[str, float]:
        return {
            "pleasure": round(self.pleasure, 4),
            "arousal": round(self.arousal, 4),
            "dominance": round(self.dominance, 4),
        }


NEUTRAL_PAD = PADVector(0.0, 0.0, 0.5)
PAD_REFERENCES: dict[EmotionLabel, PADVector] = {
    "happy": PADVector(0.8, 0.5, 0.65),
    "sad": PADVector(-0.7, 0.3, 0.2),
    "lonely": PADVector(-0.65, 0.25, 0.15),
    "anxious": PADVector(-0.5, 0.75, 0.25),
    "angry": PADVector(-0.65, 0.85, 0.75),
    "shy": PADVector(0.15, 0.45, 0.2),
    "thinking": PADVector(0.0, 0.2, 0.5),
    "neutral": NEUTRAL_PAD,
}
NEGATIVE_LABELS = {"sad", "lonely", "anxious", "angry"}


# 作用：描述某个会话经过时间衰减后的长期情绪状态，而非单轮情绪识别结果。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“AffectiveState”相关的数据结构、异常类型或服务组件。
# 字段：label：该对象中的结构化字段。、intensity：该对象中的结构化字段。、pad：该对象中的结构化字段。、updated_at：该对象中的结构化字段。、decayed：该对象中的结构化字段。
class AffectiveState:
    label: EmotionLabel
    intensity: float
    pad: PADVector
    updated_at: float
    decayed: bool = False

    # 作用：判断当前负面情绪惯性是否已经达到需要温和支持的程度。
    # 参数：无。
    @property
    # 作用：执行“needs_support”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def needs_support(self) -> bool:
        return self.label in NEGATIVE_LABELS and (
            self.intensity >= 0.2 or self.pad.pleasure <= -0.2
        )

    # 作用：将长期情绪状态及其支持信号转换为对外可用的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "intensity": round(self.intensity, 4),
            "pad": self.pad.as_dict(),
            "updated_at": self.updated_at,
            "decayed": self.decayed,
            "needs_support": self.needs_support,
        }


# 作用：跨轮次融合、衰减并持久化会话的 PAD 情绪惯性。
# 参数：无。
class AffectiveStateTracker:
    HALF_LIFE_SECONDS = 60 * 60
    NEUTRAL_THRESHOLD = 0.1

    # 作用：注入事实存储与时钟，便于保存状态并在测试或回放中控制时间。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(self, store: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._clock = clock

    # 作用：将当前单轮情绪融入会话状态，并把新的长期状态写回事实存储。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    def update(self, session_id: str, emotion: EmotionResult) -> AffectiveState:
        now = self._clock()
        previous = self.status(session_id, now=now)
        if emotion.label == "neutral":
            pad = previous.pad.blend(NEUTRAL_PAD, 0.1)
            label = previous.label
        else:
            target = self.from_emotion(emotion.label, emotion.intensity)
            weight = 0.35 + emotion.intensity * 0.45
            pad = previous.pad.blend(target, weight)
            label = emotion.label
        intensity = self._intensity(pad)
        if intensity < self.NEUTRAL_THRESHOLD:
            label = "neutral"
            intensity = 0.0
            pad = NEUTRAL_PAD
        state = AffectiveState(label=label, intensity=intensity, pad=pad, updated_at=now)
        self._save(session_id, state)
        return state

    # 作用：读取持久状态并按半衰期衰减，缺失时返回中性状态。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def status(self, session_id: str, *, now: float | None = None) -> AffectiveState:
        current_time = self._clock() if now is None else now
        stored = self.store.get_affective_state(session_id)
        if not stored:
            return AffectiveState(
                label="neutral",
                intensity=0.0,
                pad=NEUTRAL_PAD,
                updated_at=current_time,
            )
        updated_at = float(stored["updated_at"])
        elapsed = max(0.0, current_time - updated_at)
        decay = math.pow(0.5, elapsed / self.HALF_LIFE_SECONDS)
        original = PADVector(
            pleasure=float(stored["pleasure"]),
            arousal=float(stored["arousal"]),
            dominance=float(stored["dominance"]),
        )
        pad = NEUTRAL_PAD.blend(original, decay)
        intensity = self._intensity(pad)
        label: EmotionLabel = stored["label"]
        if intensity < self.NEUTRAL_THRESHOLD:
            label = "neutral"
            intensity = 0.0
            pad = NEUTRAL_PAD
        return AffectiveState(
            label=label,
            intensity=intensity,
            pad=pad,
            updated_at=updated_at,
            decayed=elapsed > 0,
        )

    # 作用：将情绪惯性压缩为提示词片段，同时声明原话优先和禁止擅自诊断。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    def format_for_prompt(self, state: AffectiveState) -> str:
        return (
            "持续情绪状态："
            f"label={state.label}，intensity={state.intensity:.2f}，"
            f"P={state.pad.pleasure:.2f}，A={state.pad.arousal:.2f}，"
            f"D={state.pad.dominance:.2f}。"
            "它表示用户跨轮次的情绪惯性；当前原话仍然优先，不要擅自诊断。"
        )

    # 作用：把离散情绪标签和强度映射为连续 PAD 目标向量。
    # 参数 label：情绪标签或知识实体显示名称。
    # 参数 intensity：归一化到零至一的情绪强度。
    def from_emotion(self, label: EmotionLabel, intensity: float) -> PADVector:
        target = PAD_REFERENCES[label]
        intensity = max(0.0, min(1.0, intensity))
        return NEUTRAL_PAD.blend(target, intensity)

    # 作用：根据 PAD 各维度偏离中性的最大幅度估算综合情绪强度。
    # 参数 pad：待计算或格式化的 PAD 连续情绪向量。
    def _intensity(self, pad: PADVector) -> float:
        return min(
            1.0,
            max(abs(pad.pleasure), pad.arousal, abs(pad.dominance - 0.5) * 2),
        )

    # 作用：将计算后的情绪状态交给关系事实库持久化。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    def _save(self, session_id: str, state: AffectiveState) -> None:
        self.store.save_affective_state(
            session_id=session_id,
            label=state.label,
            intensity=state.intensity,
            pleasure=state.pad.pleasure,
            arousal=state.pad.arousal,
            dominance=state.pad.dominance,
            updated_at=state.updated_at,
        )
