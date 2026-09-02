import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


# 作用：在关系边界、免打扰和频率限制内安排并领取离线主动关怀。
# 参数：无。
class ProactiveNudgeEngine:
    MIN_INTERVAL_SECONDS = 60 * 60
    MAX_PER_DAY = 3

    # 作用：注入持久存储、时钟和时区，保证调度规则可恢复且可复现。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    # 参数 timezone_name：主动关怀免打扰规则采用的 IANA 时区名称。
    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
        timezone_name: str | None = None,
    ) -> None:
        self.store = store
        self._clock = clock
        self.timezone_name = timezone_name or os.getenv("YUKINO_TIMEZONE", "Asia/Shanghai")

    # 作用：用户重新上线时取消尚未发送的关怀，避免过时消息随后到达。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def on_user_return(self, session_id: str) -> int:
        return self.store.cancel_pending_nudges(session_id)

    # 作用：在情绪需要且关系允许时创建一条延迟关怀记录，禁区内不调度。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 mental_state：当前短期情绪趋势和长期情绪惯性的汇总状态。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    # 参数 character_name：用于生成关怀文案的角色显示名称。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    def schedule_after_turn(
        self,
        *,
        session_id: str,
        mental_state: dict[str, object],
        safety_level: str,
        character_name: str,
        relationship: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        relationship = relationship or {}
        if relationship and relationship.get("rupture_state") != "stable":
            return None
        if relationship.get("needs_trust_rebuild") is True:
            return None
        if "no_proactive_contact" in relationship.get("boundaries", []):
            return None
        if safety_level != "normal" or mental_state.get("needs_gentle_followup") is not True:
            return None
        if self.store.list_nudges(session_id, status="pending", limit=1):
            return None

        streak = int(mental_state.get("negative_streak", 0))
        delay = 30 * 60 if streak >= 3 else 2 * 60 * 60
        emotion = str(
            mental_state.get("latest_emotion")
            or mental_state.get("dominant_emotion", "neutral")
        )
        content = self._message(character_name, emotion)
        return self.store.schedule_nudge(
            session_id=session_id,
            content=content,
            reason=f"negative_streak:{streak}",
            emotion=emotion,
            due_at=self._clock() + delay,
        )

    # 作用：查询会话中尚未投递的主动关怀记录。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def pending(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_nudges(session_id, status="pending")

    # 作用：在免打扰、每日上限和最小间隔校验后原子领取一条到期关怀。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def claim_due(self, session_id: str) -> list[dict[str, Any]]:
        now = self._clock()
        if self._is_dnd(now):
            return []
        history = self.store.list_nudges(session_id, status="delivered", limit=50)
        delivered_today = [item for item in history if item["delivered_at"] >= self._day_start(now)]
        if len(delivered_today) >= self.MAX_PER_DAY:
            return []
        if history and now - float(history[0]["delivered_at"]) < self.MIN_INTERVAL_SECONDS:
            return []

        due = [item for item in self.pending(session_id) if float(item["due_at"]) <= now]
        if not due:
            return []
        for selected in sorted(due, key=lambda item: float(item["due_at"])):
            if self.store.claim_nudge(int(selected["id"]), now):
                selected["status"] = "delivered"
                selected["delivered_at"] = now
                return [selected]
        return []

    # 作用：暴露主动关怀的时区、免打扰和频率限制配置。
    # 参数：无。
    def status(self) -> dict[str, object]:
        return {
            "timezone": self.timezone_name,
            "dnd": "23:00-08:00",
            "min_interval_seconds": self.MIN_INTERVAL_SECONDS,
            "max_per_day": self.MAX_PER_DAY,
        }

    # 作用：将时间戳转换为配置时区时间，非法时区回退到上海。
    # 参数 timestamp：待换算时区、自然日或状态时间的 Unix 时间戳。
    def _local_datetime(self, timestamp: float) -> datetime:
        try:
            timezone = ZoneInfo(self.timezone_name)
        except Exception:
            timezone = ZoneInfo("Asia/Shanghai")
        return datetime.fromtimestamp(timestamp, timezone)

    # 作用：判断指定时刻是否落在 23:00 至次日 08:00 的免打扰窗口。
    # 参数 timestamp：待换算时区、自然日或状态时间的 Unix 时间戳。
    def _is_dnd(self, timestamp: float) -> bool:
        hour = self._local_datetime(timestamp).hour
        return hour >= 23 or hour < 8

    # 作用：计算指定时刻所在本地自然日的起始时间戳。
    # 参数 timestamp：待换算时区、自然日或状态时间的 Unix 时间戳。
    def _day_start(self, timestamp: float) -> float:
        local = self._local_datetime(timestamp)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # 作用：根据最近情绪生成带角色名但不过度承诺的关怀文案。
    # 参数 character_name：用于生成关怀文案的角色显示名称。
    # 参数 emotion：本轮情绪标签、情绪结果或关怀触发情绪。
    def _message(self, character_name: str, emotion: str) -> str:
        concern = {
            "sad": "刚才那阵难受，现在有没有轻一点？",
            "lonely": "我还记得你刚才说的孤单。现在要不要让我陪你坐一会儿？",
            "anxious": "刚才那份焦虑还在吗？不用马上解决，告诉我现在怎样就好。",
            "angry": "刚才那股委屈和火气，现在有没有松一点？",
        }.get(emotion, "刚才感觉你有点累，现在还好吗？")
        return f"{character_name}：{concern}"
