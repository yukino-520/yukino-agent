from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


# 作用：表示模型请求在开始或等待过程中被主动取消。
# 参数：无。
class ModelRequestCancelled(RuntimeError):
    pass


# 作用：表示模型并发额度不足，当前调用无法被接纳。
# 参数：无。
class ModelConcurrencyLimit(RuntimeError):
    pass


# 作用：表示模型调用或并发排队超过允许等待时间。
# 参数：无。
class ModelWaitTimeout(RuntimeError):
    pass


# 作用：表示供应商返回了无法继续处理的空模型响应。
# 参数：无。
class ModelEmptyResponse(RuntimeError):
    pass


# 作用：表示指定模型处于熔断期，并告知可再次尝试的等待时间。
# 参数：无。
class ModelCircuitOpen(RuntimeError):
    # 作用：保存被熔断的模型名称和剩余冷却秒数。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    # 参数 retry_after：模型熔断器建议等待后再试的秒数。
    def __init__(self, model: str, retry_after: float) -> None:
        super().__init__(f"model circuit is open: {model}")
        self.model = model
        self.retry_after = max(0.0, float(retry_after))


# 作用：统一描述模型供应商失败类型、重试性及是否属于致命配置错误。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“ProviderFailure”相关的数据结构、异常类型或服务组件。
# 字段：code：该对象中的结构化字段。、category：该对象中的结构化字段。、retryable：该对象中的结构化字段。、fatal：该对象中的结构化字段。、status_code：该对象中的结构化字段。
class ProviderFailure:
    code: str
    category: str
    retryable: bool
    fatal: bool = False
    status_code: int | None = None

    # 作用：将分类结果转换成可记录和对外返回的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "fatal": self.fatal,
            "status_code": self.status_code,
        }


# 作用：将 SDK 异常和 HTTP 状态归一为稳定的供应商失败类别。
# 参数 exc：需要分类或记录的原始异常。
def classify_provider_failure(exc: Exception) -> ProviderFailure:
    if isinstance(exc, ModelWaitTimeout):
        return ProviderFailure("timeout", "timeout", True)
    if isinstance(exc, ModelEmptyResponse):
        return ProviderFailure("empty_response", "invalid_response", True)
    status = getattr(exc, "status_code", None)
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if status_code in {401, 403}:
        return ProviderFailure(
            "authentication_error",
            "configuration",
            False,
            fatal=True,
            status_code=status_code,
        )
    if status_code == 404:
        return ProviderFailure(
            "model_not_found",
            "configuration",
            False,
            fatal=True,
            status_code=status_code,
        )
    if status_code == 429:
        return ProviderFailure(
            "rate_limited",
            "capacity",
            True,
            status_code=status_code,
        )
    if status_code in {408, 504}:
        return ProviderFailure(
            "timeout",
            "timeout",
            True,
            status_code=status_code,
        )
    if status_code is not None and status_code >= 500:
        return ProviderFailure(
            "provider_unavailable",
            "provider",
            True,
            status_code=status_code,
        )
    if status_code in {400, 409, 422}:
        return ProviderFailure(
            "invalid_request",
            "request",
            False,
            status_code=status_code,
        )
    if "timeout" in name or "timed out" in message or "timeout" in message:
        return ProviderFailure("timeout", "timeout", True)
    if any(token in name for token in ("connection", "network")) or any(
        token in message
        for token in ("connection refused", "connection reset", "name resolution")
    ):
        return ProviderFailure("network_error", "network", True)
    return ProviderFailure("provider_error", "provider", True)


# 作用：保存单个模型的熔断、成功失败和回退使用统计。
# 参数：无。
@dataclass
# 作用：定义“_ModelHealth”相关的数据结构、异常类型或服务组件。
# 字段：circuit：该对象中的结构化字段。、consecutive_failures：该对象中的结构化字段。、total_failures：该对象中的结构化字段。、total_successes：该对象中的结构化字段。、fallback_uses：该对象中的结构化字段。、opened_until：该对象中的结构化字段。、probe_in_flight：该对象中的结构化字段。、last_failure_code：该对象中的结构化字段。、last_failure_at：该对象中的结构化字段。、last_success_at：该对象中的结构化字段。、last_latency_ms：该对象中的结构化字段。
class _ModelHealth:
    circuit: str = "closed"
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    fallback_uses: int = 0
    opened_until: float = 0.0
    probe_in_flight: bool = False
    last_failure_code: str = ""
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    last_latency_ms: int = 0


# 作用：对每个模型独立实施线程安全熔断，并限制半开恢复探针数量。
# 参数：无。
class ModelHealthMonitor:
    """Thread-safe per-model circuit breaker with a single half-open probe."""

    # 作用：配置熔断阈值和冷却期，并初始化模型健康状态表。
    # 参数 failure_threshold：触发熔断前允许的连续失败次数。
    # 参数 cooldown_seconds：模型熔断后进入恢复探测前的冷却秒数。
    # 参数 clock：可替换的时钟函数，便于控制过期和熔断时间。
    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.failure_threshold = max(1, min(int(failure_threshold), 20))
        self.cooldown_seconds = max(1.0, min(float(cooldown_seconds), 3600.0))
        self._clock = clock
        self._states: dict[str, _ModelHealth] = {}
        self._lock = threading.RLock()

    # 作用：调用前检查模型熔断状态，并在冷却后领取唯一恢复探针。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    # 参数 force：是否忽略尚未结束的冷却期强制尝试模型。
    def begin(self, model: str, *, force: bool = False) -> None:
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(model, _ModelHealth())
            if state.circuit == "open":
                if now < state.opened_until and not force:
                    raise ModelCircuitOpen(model, state.opened_until - now)
                if state.probe_in_flight:
                    raise ModelCircuitOpen(model, 1.0)
                state.circuit = "half_open"
                state.probe_in_flight = True
            elif state.circuit == "half_open":
                if state.probe_in_flight:
                    raise ModelCircuitOpen(model, 1.0)
                state.probe_in_flight = True

    # 作用：记录模型调用成功，关闭熔断器并更新延迟和回退统计。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    # 参数 latency_ms：本次模型或 Agent 回合的耗时毫秒数。
    # 参数 fallback：是否采用了回退模型，或 JSON 解析失败时的默认值。
    def success(self, model: str, *, latency_ms: int, fallback: bool = False) -> None:
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(model, _ModelHealth())
            state.circuit = "closed"
            state.consecutive_failures = 0
            state.total_successes += 1
            state.fallback_uses += 1 if fallback else 0
            state.opened_until = 0.0
            state.probe_in_flight = False
            state.last_success_at = now
            state.last_latency_ms = max(0, int(latency_ms))

    # 作用：记录已分类失败，并按阈值、致命性或探针失败打开熔断器。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    # 参数 failure：需要登记或判断是否已恢复的失败信息。
    # 参数 latency_ms：本次模型或 Agent 回合的耗时毫秒数。
    def failure(
        self,
        model: str,
        failure: ProviderFailure,
        *,
        latency_ms: int,
    ) -> None:
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(model, _ModelHealth())
            was_probe = state.circuit == "half_open"
            state.consecutive_failures += 1
            state.total_failures += 1
            state.probe_in_flight = False
            state.last_failure_code = failure.code
            state.last_failure_at = now
            state.last_latency_ms = max(0, int(latency_ms))
            if (
                failure.fatal
                or was_probe
                or state.consecutive_failures >= self.failure_threshold
            ):
                state.circuit = "open"
                state.opened_until = now + self.cooldown_seconds
            else:
                state.circuit = "closed"

    # 作用：释放未完成的恢复探针，并将半开模型退回打开状态。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    def abort(self, model: str) -> None:
        with self._lock:
            state = self._states.setdefault(model, _ModelHealth())
            state.probe_in_flight = False
            if state.circuit == "half_open":
                state.circuit = "open"

    # 作用：返回指定及已观测模型的公开健康与熔断信息。
    # 参数 models：需要纳入健康状态报告的模型名称列表。
    def status(self, models: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            for model in models:
                if model:
                    self._states.setdefault(model, _ModelHealth())
            items = {
                model: self._public_state(state, now)
                for model, state in sorted(self._states.items())
            }
        return {
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "models": items,
        }

    # 作用：将内部健康状态换算为可展示的熔断标签和剩余冷却时间。
    # 参数 state：待保存的检查点状态或待展示的熔断状态。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    @staticmethod
    # 作用：执行“public_state”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 state：调用方传入的state，用于本次处理。
    # 参数 now：调用方传入的now，用于本次处理。
    def _public_state(state: _ModelHealth, now: float) -> dict[str, Any]:
        circuit = state.circuit
        retry_after = max(0.0, state.opened_until - now)
        if circuit == "open" and retry_after <= 0 and not state.probe_in_flight:
            circuit = "half_open_ready"
        return {
            "circuit": circuit,
            "retry_after_seconds": round(retry_after, 3),
            "consecutive_failures": state.consecutive_failures,
            "total_failures": state.total_failures,
            "total_successes": state.total_successes,
            "fallback_uses": state.fallback_uses,
            "probe_in_flight": state.probe_in_flight,
            "last_failure_code": state.last_failure_code,
            "last_failure_at": state.last_failure_at or None,
            "last_success_at": state.last_success_at or None,
            "last_latency_ms": state.last_latency_ms,
        }
