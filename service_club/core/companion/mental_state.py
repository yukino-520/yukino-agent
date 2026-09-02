import threading
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from service_club.core.types import EmotionLabel

NEGATIVE_EMOTIONS = {"sad", "lonely", "anxious", "angry"}


# 作用：保存一轮离散情绪标签与强度，作为短期情绪窗口的最小记录。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“EmotionTurn”相关的数据结构、异常类型或服务组件。
# 字段：label：该对象中的结构化字段。、intensity：该对象中的结构化字段。
class EmotionTurn:
    label: EmotionLabel
    intensity: float


# 作用：维护会话最近若干轮情绪，用于识别主导情绪和连续负面趋势。
# 参数：无。
class MentalStateTracker:
    # 作用：配置情绪窗口大小；有事实存储时从持久层读取，否则使用进程内缓存。
    # 参数 max_turns：短期情绪窗口最多保留的轮数。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    def __init__(self, max_turns: int = 20, store: Any | None = None) -> None:
        self.max_turns = max_turns
        self.store = store
        self._sessions: dict[str, deque[EmotionTurn]] = {}
        self._lock = threading.RLock()

    # 作用：把当前轮情绪同时写入内存窗口和可选的持久事实存储。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 label：情绪标签或知识实体显示名称。
    # 参数 intensity：归一化到零至一的情绪强度。
    def record(self, session_id: str, label: EmotionLabel, *, intensity: float) -> None:
        with self._lock:
            turns = self._sessions.setdefault(session_id, deque(maxlen=self.max_turns))
            turns.append(EmotionTurn(label=label, intensity=intensity))
        if self.store is not None:
            self.store.save_emotion_turn(session_id, label, intensity)

    # 作用：汇总单个会话的主导情绪、平均强度与连续负面轮数。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def status(self, session_id: str) -> dict[str, object]:
        turns = self._turns(session_id)
        if not turns:
            return {
                "turns": 0,
                "dominant_emotion": "neutral",
                "latest_emotion": "neutral",
                "negative_streak": 0,
                "avg_intensity": 0.0,
                "needs_gentle_followup": False,
            }
        counts = Counter(turn.label for turn in turns)
        dominant = counts.most_common(1)[0][0]
        negative_streak = self._negative_streak(turns)
        avg_intensity = sum(turn.intensity for turn in turns) / len(turns)
        return {
            "turns": len(turns),
            "dominant_emotion": dominant,
            "latest_emotion": turns[-1].label,
            "negative_streak": negative_streak,
            "avg_intensity": round(avg_intensity, 3),
            "needs_gentle_followup": negative_streak >= 2,
        }

    # 作用：汇总内存和持久层中所有已知会话的情绪状态。
    # 参数：无。
    def global_status(self) -> dict[str, object]:
        with self._lock:
            session_ids = set(self._sessions)
        if self.store is not None:
            session_ids.update(self.store.emotion_sessions())
        return {
            "session_count": len(session_ids),
            "sessions": {
                session_id: self.status(session_id)
                for session_id in sorted(session_ids)
            },
        }

    # 作用：按当前存储模式取得会话的最近情绪记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def _turns(self, session_id: str) -> list[EmotionTurn]:
        if self.store is None:
            with self._lock:
                return list(self._sessions.get(session_id, []))
        return [
            EmotionTurn(label=item["label"], intensity=float(item["intensity"]))
            for item in self.store.list_emotion_turns(session_id, limit=self.max_turns)
        ]

    # 作用：从最新一轮向前计算连续负面情绪的长度。
    # 参数 turns：用于计算关系阶段或负面连续轮数的情绪记录集合。
    def _negative_streak(self, turns: list[EmotionTurn]) -> int:
        streak = 0
        for turn in reversed(turns):
            if turn.label in NEGATIVE_EMOTIONS:
                streak += 1
            else:
                break
        return streak
