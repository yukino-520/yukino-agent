from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


# 作用：表示目标网络熔断器未允许本次调用进入。
# 参数：无。
class CircuitOpenError(RuntimeError):
    pass


# 作用：表示网络调用或重试等待被用户取消。
# 参数：无。
class NetworkCallCancelled(RuntimeError):
    pass


# 作用：保存单个网络目标的失败计数、熔断时间和恢复探测状态。
# 参数：无。
@dataclass
# 作用：定义“_CircuitState”相关的数据结构、异常类型或服务组件。
# 字段：consecutive_failures：该对象中的结构化字段。、opened_at：该对象中的结构化字段。、probe_in_flight：该对象中的结构化字段。、total_calls：该对象中的结构化字段。、total_failures：该对象中的结构化字段。、total_retries：该对象中的结构化字段。、last_error：该对象中的结构化字段。
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False
    total_calls: int = 0
    total_failures: int = 0
    total_retries: int = 0
    last_error: str = ""


# 作用：为同步外部适配器提供按目标隔离的重试、取消和熔断保护。
# 参数：无。
class NetworkResilience:
    """Thread-safe retry and per-target circuit breaking for synchronous adapters."""

    # 作用：配置失败阈值、冷却时间、退避延迟以及可测试的时钟函数。
    # 参数 failure_threshold：触发熔断前允许的连续失败次数。
    # 参数 recovery_seconds：网络熔断器打开后的冷却秒数。
    # 参数 retry_delay_seconds：网络重试的基础退避秒数。
    # 参数 clock：可替换的时钟函数，便于控制过期和熔断时间。
    # 参数 sleeper：可替换的等待函数，用于分片退避和测试。
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        retry_delay_seconds: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_seconds = max(0.1, float(recovery_seconds))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.clock = clock
        self.sleeper = sleeper
        self._states: dict[str, _CircuitState] = {}
        self._lock = threading.RLock()

    # 作用：在取消检查、条件重试和熔断保护下执行一次网络操作。
    # 参数 key：用于隔离熔断状态的网络目标标识。
    # 参数 operation：实际发起一次同步网络请求的无参回调。
    # 参数 retry_if：判断某类网络异常是否允许重试的回调。
    # 参数 max_attempts：一次网络操作或后台作业允许的最大尝试次数。
    # 参数 cancel_check：用于判断当前操作是否已被用户取消的回调。
    def call(
        self,
        key: str,
        operation: Callable[[], T],
        *,
        retry_if: Callable[[Exception], bool],
        max_attempts: int = 3,
        cancel_check: Callable[[], bool] | None = None,
    ) -> T:
        attempts = max(1, min(int(max_attempts), 5))
        for attempt in range(1, attempts + 1):
            if cancel_check is not None and cancel_check():
                raise NetworkCallCancelled("网络操作已按用户要求停止。")
            self._before_call(key)
            try:
                result = operation()
            except Exception as exc:
                opened = self._record_failure(key, exc)
                should_retry = attempt < attempts and retry_if(exc) and not opened
                if not should_retry:
                    raise
                with self._lock:
                    self._state(key).total_retries += 1
                self._wait_before_retry(attempt, cancel_check)
                continue
            self._record_success(key)
            return result
        raise RuntimeError("网络重试循环异常结束。")

    # 作用：汇总各网络目标的熔断状态、调用失败和重试次数。
    # 参数：无。
    def status(self) -> dict[str, object]:
        now = self.clock()
        with self._lock:
            targets = {
                key: {
                    "state": self._state_label(state, now),
                    "consecutive_failures": state.consecutive_failures,
                    "total_calls": state.total_calls,
                    "total_failures": state.total_failures,
                    "total_retries": state.total_retries,
                    "last_error": state.last_error,
                }
                for key, state in self._states.items()
            }
        open_count = sum(item["state"] != "closed" for item in targets.values())
        return {
            "ok": True,
            "failure_threshold": self.failure_threshold,
            "recovery_seconds": self.recovery_seconds,
            "target_count": len(targets),
            "open_circuits": open_count,
            "targets": targets,
        }

    # 作用：调用前检查熔断冷却期，并确保半开状态只有一个恢复探针。
    # 参数 key：用于隔离熔断状态的网络目标标识。
    def _before_call(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            state = self._state(key)
            if not state.opened_at:
                state.total_calls += 1
                return
            if now - state.opened_at < self.recovery_seconds:
                raise CircuitOpenError(f"{key} 的网络熔断器仍在冷却。")
            if state.probe_in_flight:
                raise CircuitOpenError(f"{key} 正在进行恢复探测。")
            state.probe_in_flight = True
            state.total_calls += 1

    # 作用：成功后关闭熔断器并清零连续失败与探针标记。
    # 参数 key：用于隔离熔断状态的网络目标标识。
    def _record_success(self, key: str) -> None:
        with self._lock:
            state = self._state(key)
            state.consecutive_failures = 0
            state.opened_at = 0.0
            state.probe_in_flight = False
            state.last_error = ""

    # 作用：累计目标失败并在达到阈值时打开熔断器。
    # 参数 key：用于隔离熔断状态的网络目标标识。
    # 参数 exc：需要分类或记录的原始异常。
    def _record_failure(self, key: str, exc: Exception) -> bool:
        with self._lock:
            state = self._state(key)
            state.consecutive_failures += 1
            state.total_failures += 1
            state.last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            state.probe_in_flight = False
            if state.consecutive_failures >= self.failure_threshold:
                state.opened_at = self.clock()
            return bool(state.opened_at)

    # 作用：分片执行指数退避，使等待期间仍能及时响应取消请求。
    # 参数 attempt：当前重试轮次，用于计算退避时间。
    # 参数 cancel_check：用于判断当前操作是否已被用户取消的回调。
    def _wait_before_retry(
        self,
        attempt: int,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        remaining = self.retry_delay_seconds * (2 ** (attempt - 1))
        while remaining > 0:
            if cancel_check is not None and cancel_check():
                raise NetworkCallCancelled("网络重试已按用户要求停止。")
            delay = min(0.05, remaining)
            self.sleeper(delay)
            remaining -= delay

    # 作用：获取或创建指定网络目标的独立熔断状态。
    # 参数 key：用于隔离熔断状态的网络目标标识。
    def _state(self, key: str) -> _CircuitState:
        return self._states.setdefault(key, _CircuitState())

    # 作用：根据打开时间和冷却期计算 closed、open 或 half_open 标签。
    # 参数 state：待保存的检查点状态或待展示的熔断状态。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    def _state_label(self, state: _CircuitState, now: float) -> str:
        if not state.opened_at:
            return "closed"
        if now - state.opened_at >= self.recovery_seconds:
            return "half_open"
        return "open"
