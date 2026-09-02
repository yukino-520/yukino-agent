import threading
from dataclasses import dataclass
from typing import Any

from service_club.core.types import CharacterId


@dataclass
# 作用：定义“AgentRouteStats”相关的数据结构、异常类型或服务组件。
# 字段：turns：该对象中的结构化字段。、successes：该对象中的结构化字段。、total_latency_ms：该对象中的结构化字段。
class AgentRouteStats:
    turns: int = 0
    successes: int = 0
    total_latency_ms: int = 0

    # 作用：记录一次经过规范化的事件或运行结果，供后续查询和审计使用。
    # 参数 success：调用方传入的success，用于本次处理。
    # 参数 latency_ms：调用方传入的latency_ms，用于本次处理。
    def record(self, *, success: bool, latency_ms: int) -> None:
        self.turns += 1
        self.successes += 1 if success else 0
        self.total_latency_ms += latency_ms

    # 作用：把对象转换为稳定的普通字典表示。
    def to_dict(self) -> dict[str, float | int]:
        success_rate = self.successes / self.turns if self.turns else 0.0
        avg_latency = self.total_latency_ms / self.turns if self.turns else 0
        return {
            "turns": self.turns,
            "successes": self.successes,
            "success_rate": round(success_rate, 3),
            "avg_latency_ms": int(avg_latency),
            "belief_alpha": self.successes + 1,
            "belief_beta": self.turns - self.successes + 1,
            "expected_success": round(
                (self.successes + 1) / (self.turns + 2),
                3,
            ),
        }


# 作用：定义“RouteFeedbackTracker”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class RouteFeedbackTracker:
    # 作用：执行“init__”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 store：提供持久化读写能力的存储适配器。
    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self._stats: dict[CharacterId, AgentRouteStats] = {}
        self._lock = threading.RLock()
        if self.store is not None:
            self._load()

    # 作用：记录一次经过规范化的事件或运行结果，供后续查询和审计使用。
    # 参数 agent_id：调用方传入的agent_id，用于本次处理。
    # 参数 success：调用方传入的success，用于本次处理。
    # 参数 latency_ms：调用方传入的latency_ms，用于本次处理。
    def record(self, agent_id: CharacterId, *, success: bool, latency_ms: int) -> None:
        with self._lock:
            self._stats.setdefault(agent_id, AgentRouteStats()).record(
                success=success,
                latency_ms=latency_ms,
            )
        if self.store is not None:
            self.store.record_route_result(
                agent_id,
                success=success,
                latency_ms=latency_ms,
            )

    # 作用：执行“belief_score”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 agent_id：调用方传入的agent_id，用于本次处理。
    def belief_score(self, agent_id: CharacterId) -> dict[str, float | int]:
        with self._lock:
            stats = self._stats.get(agent_id, AgentRouteStats())
            expected_success = (stats.successes + 1) / (stats.turns + 2)
            avg_latency_ms = stats.total_latency_ms / stats.turns if stats.turns else 0.0
            latency_score = 1.0 / (1.0 + avg_latency_ms / 5000.0)
            return {
                "turns": stats.turns,
                "expected_success": round(expected_success, 4),
                "latency_score": round(latency_score, 4),
            }

    # 作用：执行“recommend”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 candidates：调用方传入的candidates，用于本次处理。
    def recommend(self, candidates: list[CharacterId]) -> CharacterId | None:
        with self._lock:
            known = [
                (agent_id, self._stats[agent_id])
                for agent_id in candidates
                if agent_id in self._stats and self._stats[agent_id].turns > 0
            ]
            if not known:
                return candidates[0] if candidates else None
            known.sort(
                key=lambda item: (
                    -item[1].to_dict()["success_rate"],
                    item[1].to_dict()["avg_latency_ms"],
                )
            )
            return known[0][0]

    # 作用：汇总当前组件的运行状态、配置和可观测信息。
    def status(self) -> dict[str, dict[str, dict[str, float | int]]]:
        with self._lock:
            return {
                "agents": {
                    agent_id: stats.to_dict()
                    for agent_id, stats in self._stats.items()
                }
            }

    # 作用：执行“load”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _load(self) -> None:
        with self._lock:
            for agent_id, values in self.store.route_stats().items():
                self._stats[agent_id] = AgentRouteStats(  # type: ignore[index]
                    turns=int(values["turns"]),
                    successes=int(values["successes"]),
                    total_latency_ms=int(values["total_latency_ms"]),
                )
