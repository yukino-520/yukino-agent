from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


# 作用：协调普通 Agent 回合与运行时重配置，让回合可并发而配置切换保持独占。
# 参数：无。
class RuntimeConcurrencyGate:
    """Allow concurrent Agent turns while making runtime reloads exclusive."""

    # 作用：初始化条件锁、活跃回合计数以及线程内的重入深度。
    # 参数：无。
    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active_turns = 0
        self._waiting_reconfigurations = 0
        self._reconfiguring = False
        self._local = threading.local()

    # 作用：为一次 Agent 回合申请共享通行权，并支持同线程嵌套进入。
    # 参数：无。
    @contextmanager
    # 作用：执行“turn”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def turn(self) -> Iterator[None]:
        depth = int(getattr(self._local, "turn_depth", 0))
        if depth:
            self._local.turn_depth = depth + 1
            try:
                yield
            finally:
                self._local.turn_depth = depth
            return
        with self._condition:
            while self._reconfiguring or self._waiting_reconfigurations:
                self._condition.wait()
            self._active_turns += 1
            self._local.turn_depth = 1
        try:
            yield
        finally:
            with self._condition:
                self._local.turn_depth = 0
                self._active_turns -= 1
                if self._active_turns == 0:
                    self._condition.notify_all()

    # 作用：等待所有回合退出后独占运行时，供模型或配置热重载使用。
    # 参数：无。
    @contextmanager
    # 作用：执行“reconfiguration”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def reconfiguration(self) -> Iterator[None]:
        with self._condition:
            self._waiting_reconfigurations += 1
            try:
                while self._reconfiguring or self._active_turns:
                    self._condition.wait()
                self._reconfiguring = True
            finally:
                self._waiting_reconfigurations -= 1
        try:
            yield
        finally:
            with self._condition:
                self._reconfiguring = False
                self._condition.notify_all()

    # 作用：返回当前执行中回合、等待重配置者及独占状态的快照。
    # 参数：无。
    def status(self) -> dict[str, int | bool]:
        with self._condition:
            return {
                "active_turns": self._active_turns,
                "waiting_reconfigurations": self._waiting_reconfigurations,
                "reconfiguring": self._reconfiguring,
            }
