from enum import IntEnum


# 作用：定义运行时从正常服务到仅保留应急响应的四级降级状态。
# 参数：无。
class DegradationLevel(IntEnum):
    L0_NORMAL = 0
    L1_DEGRADED = 1
    L2_MINIMAL = 2
    L3_EMERGENCY = 3


FEATURE_THRESHOLDS = {
    "tts": DegradationLevel.L0_NORMAL,
    "sticker": DegradationLevel.L0_NORMAL,
    "tools": DegradationLevel.L1_DEGRADED,
    "memory": DegradationLevel.L2_MINIMAL,
    "model": DegradationLevel.L2_MINIMAL,
    "basic_response": DegradationLevel.L3_EMERGENCY,
}


# 作用：保存当前降级状态，并集中判断各功能是否应当关闭或恢复。
# 参数：无。
class DegradationStrategy:
    # 作用：初始化为正常服务状态并清空触发原因。
    # 参数：无。
    def __init__(self) -> None:
        self.level = DegradationLevel.L0_NORMAL
        self.reason = ""

    # 作用：将运行时切换到指定降级等级并记录触发原因。
    # 参数 level：要切换到的运行时降级等级。
    # 参数 reason：触发降级或安全阻断的具体原因。
    def trigger(self, level: DegradationLevel, reason: str) -> None:
        self.level = level
        self.reason = reason

    # 作用：健康恢复时逐级降低降级等级，避免能力一次性全部放开。
    # 参数：无。
    def recover(self) -> None:
        if self.level > DegradationLevel.L0_NORMAL:
            self.level = DegradationLevel(self.level - 1)
            self.reason = "recover"

    # 作用：按当前等级与功能阈值判断某项能力是否仍可使用。
    # 参数 feature：要判断是否仍可用的运行时功能名称。
    def is_available(self, feature: str) -> bool:
        threshold = FEATURE_THRESHOLDS.get(feature)
        if threshold is None:
            return True
        return self.level <= threshold
