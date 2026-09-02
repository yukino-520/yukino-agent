import threading
from collections import deque
from dataclasses import dataclass

from service_club.core.runtime.degradation import DegradationLevel, DegradationStrategy


# 作用：保存单个 Agent 回合是否成功及其响应耗时。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“TurnHealth”相关的数据结构、异常类型或服务组件。
# 字段：success：该对象中的结构化字段。、latency_ms：该对象中的结构化字段。
class TurnHealth:
    success: bool
    latency_ms: int


# 作用：根据近期失败次数和延迟动态提升或恢复运行时降级等级。
# 参数：无。
class RuntimeDegradationMonitor:
    # 作用：配置延迟阈值和滑动窗口，并创建线程安全的降级策略。
    # 参数 latency_threshold_ms：触发运行时延迟降级的毫秒阈值。
    # 参数 window_size：用于判断降级的近期回合滑动窗口大小。
    def __init__(
        self,
        *,
        latency_threshold_ms: int = 4000,
        window_size: int = 6,
    ) -> None:
        self.latency_threshold_ms = latency_threshold_ms
        self.turns: deque[TurnHealth] = deque(maxlen=window_size)
        self.strategy = DegradationStrategy()
        self._lock = threading.RLock()

    # 作用：记录回合健康度，并按失败和慢请求信号调整可用能力层级。
    # 参数 success：本次 Agent 回合是否成功。
    # 参数 latency_ms：本次模型或 Agent 回合的耗时毫秒数。
    def record_turn(self, *, success: bool, latency_ms: int) -> None:
        with self._lock:
            self.turns.append(TurnHealth(success=success, latency_ms=latency_ms))
            recent_failures = sum(1 for item in self.turns if not item.success)
            if recent_failures >= 2:
                self.strategy.trigger(
                    DegradationLevel.L2_MINIMAL,
                    f"recent_failures:{recent_failures}",
                )
                return
            if latency_ms >= self.latency_threshold_ms:
                self.strategy.trigger(
                    DegradationLevel.L1_DEGRADED,
                    f"latency:{latency_ms}ms",
                )
                return
            if success and self.strategy.level > DegradationLevel.L0_NORMAL:
                self.strategy.recover()

    # 作用：输出当前降级原因以及窗口内的近期回合健康记录。
    # 参数：无。
    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "level": self.strategy.level.name,
                "reason": self.strategy.reason,
                "recent_turns": [
                    {"success": item.success, "latency_ms": item.latency_ms}
                    for item in self.turns
                ],
            }
