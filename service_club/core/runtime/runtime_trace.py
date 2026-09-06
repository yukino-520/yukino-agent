import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# 作用：汇集一次 Agent 回合从路由到模型、工具和策略审计的全链路诊断信息。
# 参数：无。
@dataclass
# 作用：定义“RuntimeTrace”相关的数据结构、异常类型或服务组件。
# 字段：trace_id：该对象中的结构化字段。、session_id：该对象中的结构化字段。、user_text：该对象中的结构化字段。、started_at：该对象中的结构化字段。、route：该对象中的结构化字段。、emotion：该对象中的结构化字段。、safety_level：该对象中的结构化字段。、tool_results：该对象中的结构化字段。、memories_used：该对象中的结构化字段。、memory_retrieval：该对象中的结构化字段。、intent_plan：该对象中的结构化字段。、spontaneous_recalls：该对象中的结构化字段。、proactive_care：该对象中的结构化字段。、companion_state：该对象中的结构化字段。、response_policy：该对象中的结构化字段。、policy_audit：该对象中的结构化字段。、quality_audit：该对象中的结构化字段。、interaction_learning：该对象中的结构化字段。、self_model：该对象中的结构化字段。、club_orchestration：该对象中的结构化字段。、context_window：该对象中的结构化字段。、model_runtime：该对象中的结构化字段。、degraded：该对象中的结构化字段。、degradation_reason：该对象中的结构化字段。、latency_ms：该对象中的结构化字段。
class RuntimeTrace:
    trace_id: str
    session_id: str
    user_text: str
    started_at: float = field(default_factory=time.time)
    route: dict[str, Any] = field(default_factory=dict)
    emotion: str = "neutral"
    safety_level: str = "normal"
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    memories_used: list[str] = field(default_factory=list)
    memory_retrieval: list[dict[str, Any]] = field(default_factory=list)
    knowledge_retrieval: list[dict[str, Any]] = field(default_factory=list)
    intent_plan: list[dict[str, Any]] = field(default_factory=list)
    spontaneous_recalls: list[dict[str, Any]] = field(default_factory=list)
    proactive_care: list[dict[str, Any]] = field(default_factory=list)
    companion_state: dict[str, Any] = field(default_factory=dict)
    response_policy: dict[str, Any] = field(default_factory=dict)
    policy_audit: dict[str, Any] = field(default_factory=dict)
    quality_audit: dict[str, Any] = field(default_factory=dict)
    interaction_learning: dict[str, Any] = field(default_factory=dict)
    self_model: dict[str, Any] = field(default_factory=dict)
    club_orchestration: dict[str, Any] = field(default_factory=dict)
    context_window: dict[str, Any] = field(default_factory=dict)
    model_runtime: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    degradation_reason: str = ""
    latency_ms: int = 0


# 作用：在线程安全的内存环形列表中保存近期运行时追踪记录。
# 参数：无。
class RuntimeTraceStore:
    # 作用：设置最多保留的追踪数量并初始化存储锁。
    # 参数 max_items：内存中最多保留的运行追踪数量。
    def __init__(self, max_items: int = 100) -> None:
        self.max_items = max_items
        self._items: list[RuntimeTrace] = []
        self._lock = threading.RLock()

    # 作用：为新回合创建追踪标识并加入近期记录头部。
    # 参数 session_id：当前用户会话的唯一标识。
    # 参数 user_text：用于建立追踪的本轮用户原始文本。
    def start(self, *, session_id: str, user_text: str) -> RuntimeTrace:
        trace = RuntimeTrace(
            trace_id=uuid.uuid4().hex[:12],
            session_id=session_id,
            user_text=user_text,
        )
        with self._lock:
            self._items.insert(0, trace)
            del self._items[self.max_items :]
        return trace

    # 作用：结束指定追踪，写入降级结论并计算整个回合耗时。
    # 参数 trace_id：一次运行时追踪的唯一标识。
    # 参数 degraded：本次回合是否以降级能力完成。
    # 参数 degradation_reason：本次回合发生能力降级的原因。
    def finish(
        self,
        trace_id: str,
        *,
        degraded: bool,
        degradation_reason: str,
    ) -> RuntimeTrace | None:
        with self._lock:
            trace = next(
                (item for item in self._items if item.trace_id == trace_id),
                None,
            )
            if trace is None:
                return None
            trace.degraded = degraded
            trace.degradation_reason = degradation_reason
            trace.latency_ms = int((time.time() - trace.started_at) * 1000)
            return trace

    # 作用：按追踪标识查询单次回合诊断记录。
    # 参数 trace_id：一次运行时追踪的唯一标识。
    def get(self, trace_id: str) -> RuntimeTrace | None:
        with self._lock:
            return next(
                (item for item in self._items if item.trace_id == trace_id),
                None,
            )

    # 作用：按时间倒序返回指定数量的近期追踪。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def recent(self, limit: int = 20) -> list[RuntimeTrace]:
        with self._lock:
            return self._items[:limit]

    # 作用：汇总已缓存追踪数量及发生降级的回合数量。
    # 参数：无。
    def status(self) -> dict[str, int]:
        with self._lock:
            degraded_count = sum(1 for item in self._items if item.degraded)
            return {
                "trace_count": len(self._items),
                "degraded_count": degraded_count,
            }
