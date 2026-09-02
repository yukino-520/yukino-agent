from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


# 作用：定义最终能力权限闸门所需的数据库无关仓储接口。
# 参数：无。
@runtime_checkable
# 作用：定义“CapabilityPolicyRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class CapabilityPolicyRepository(Protocol):
    """Database-independent contract for the final capability permission gate."""

    # 作用：声明设置或移除单项全局拒绝规则的接口。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 denied：是否启用这条全局拒绝规则。
    def set_denied(
        self,
        capability: str,
        action: str,
        *,
        denied: bool,
    ) -> None: ...

    # 作用：声明批量更新能力权限规则的接口。
    # 参数 rules：本次要批量应用的能力权限规则。
    def set_many(self, rules: list[dict[str, Any]]) -> None: ...

    # 作用：声明读取所有生效拒绝规则的接口。
    # 参数：无。
    def denied_rules(self) -> set[tuple[str, str]]: ...

    # 作用：声明判断能力操作是否被全局禁止的接口。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    def is_denied(self, capability: str, action: str) -> bool: ...

    # 作用：声明创建会话级限时限次授权的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 ttl_seconds：授权或待确认操作保持有效的秒数。
    # 参数 uses：临时授权允许被消费的最大次数。
    def create_grant(
        self,
        *,
        session_id: str,
        capability: str,
        action: str,
        ttl_seconds: int,
        uses: int,
    ) -> dict[str, Any]: ...

    # 作用：声明校验权限并可原子消费临时授权次数的接口。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 consume：授权成功时是否扣减一次临时授权额度。
    def authorize(
        self,
        capability: str,
        action: str,
        *,
        session_id: str = "",
        consume: bool,
    ) -> dict[str, Any]: ...

    # 作用：声明列出会话临时授权的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def list_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    # 作用：声明按会话撤销指定临时授权的接口。
    # 参数 grant_id：临时授权记录的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def revoke(
        self,
        grant_id: str,
        *,
        session_id: str,
    ) -> tuple[dict[str, Any], bool]: ...

    # 作用：声明清理会话权限状态的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明汇总权限仓储健康与数量信息的接口。
    # 参数：无。
    def status(self) -> dict[str, Any]: ...


# 作用：定义可恢复能力工作流的持久状态接口。
# 参数：无。
@runtime_checkable
# 作用：定义“WorkflowRunRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class WorkflowRunRepository(Protocol):
    """Persistent state contract for resumable capability workflows."""

    # 作用：声明创建包含步骤初始状态的工作流记录接口。
    # 参数 steps：要创建、校验或排序的工作流步骤列表。
    def create(self, steps: list[dict[str, Any]]) -> dict[str, Any]: ...

    # 作用：声明按 ID 读取工作流状态的接口。
    # 参数 run_id：工作流运行记录的唯一标识。
    def get(self, run_id: str) -> dict[str, Any] | None: ...

    # 作用：声明按时间列出最近工作流的接口。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def list(self, *, limit: int = 20) -> list[dict[str, Any]]: ...

    # 作用：声明在事务中修改一条工作流状态的接口。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 mutator：在事务锁内修改持久状态的回调。
    def update(
        self,
        run_id: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]: ...

    # 作用：声明收敛重启遗留运行状态的接口。
    # 参数：无。
    def recover_interrupted(self) -> dict[str, int]: ...


# 作用：定义用户可见 Agent 请求的幂等占位与结果接口。
# 参数：无。
@runtime_checkable
# 作用：定义“AgentRequestRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class AgentRequestRepository(Protocol):
    """Idempotency contract for user-visible Agent requests."""

    # 作用：声明按会话和请求 ID 抢占或复用幂等请求的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 request_hash：用于识别同一请求 ID 内容冲突的摘要。
    def claim(
        self,
        session_id: str,
        request_id: str,
        request_hash: str,
    ) -> Any: ...

    # 作用：声明保存幂等请求成功响应的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 response：需要持久化复用的 Agent 成功响应。
    def complete(
        self,
        session_id: str,
        request_id: str,
        response: dict[str, Any],
    ) -> None: ...

    # 作用：声明保存幂等请求失败原因的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 error：需要持久化的失败原因或错误摘要。
    def fail(self, session_id: str, request_id: str, error: str) -> None: ...

    # 作用：声明删除会话请求幂等记录的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明恢复进程中断请求状态的接口。
    # 参数：无。
    def recover_interrupted(self) -> int: ...


# 作用：定义生产者和工作池共用的持久租约队列接口。
# 参数：无。
@runtime_checkable
# 作用：定义“BackgroundJobRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class BackgroundJobRepository(Protocol):
    """Durable leased-queue contract used by producers and worker pools."""

    # 作用：声明带去重、限流和重试参数入队后台任务的接口。
    # 参数 kind：任务、操作或后台作业的业务类型。
    # 参数 payload：后台任务、确认操作或外部分发携带的结构化负载。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    # 参数 related_task_id：与后台作业相关联的另一个 Agent 任务 ID。
    # 参数 dedupe_key：用于复用同一逻辑任务或副作用的去重键。
    # 参数 max_attempts：后台任务失败后允许执行的最大尝试次数。
    # 参数 available_at：任务最早可被领取的时间戳。
    # 参数 max_active_kind：同类型后台任务允许同时活跃的上限。
    # 参数 max_active_session：同会话后台任务允许同时活跃的上限。
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        session_id: str = "",
        task_id: str = "",
        related_task_id: str = "",
        dedupe_key: str = "",
        max_attempts: int = 5,
        available_at: float | None = None,
        max_active_kind: int | None = None,
        max_active_session: int | None = None,
    ) -> dict[str, Any]: ...

    # 作用：声明按任务 ID 读取后台任务的接口。
    # 参数 job_id：后台任务的唯一标识。
    def get(self, job_id: str) -> dict[str, Any] | None: ...

    # 作用：声明按类型和去重键查找既有任务的接口。
    # 参数 kind：任务、操作或后台作业的业务类型。
    # 参数 dedupe_key：用于复用同一逻辑任务或副作用的去重键。
    def get_by_dedupe(
        self,
        kind: str,
        dedupe_key: str,
    ) -> dict[str, Any] | None: ...

    # 作用：声明回收租约已过期运行任务的接口。
    # 参数 now：用于租约或状态判断的当前时间覆盖值。
    def expire_orphaned_running(self, *, now: float | None = None) -> int: ...

    # 作用：声明按类型及会话串行约束租赁下一项任务的接口。
    # 参数 now：用于租约或状态判断的当前时间覆盖值。
    # 参数 lease_seconds：后台任务租约从当前时刻起的有效秒数。
    # 参数 kinds：本次领取或统计允许包含的后台任务类型。
    # 参数 serialize_by_session：领取任务时是否要求同一会话串行执行。
    def claim(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = 60,
        kinds: set[str] | None = None,
        serialize_by_session: bool = False,
    ) -> dict[str, Any] | None: ...

    # 作用：声明凭租约令牌续期正在运行任务的接口。
    # 参数 job_id：后台任务的唯一标识。
    # 参数 lease_token：证明当前工作进程持有任务租约的随机令牌。
    # 参数 lease_seconds：后台任务租约从当前时刻起的有效秒数。
    # 参数 now：用于租约或状态判断的当前时间覆盖值。
    def renew_lease(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: float = 60,
        now: float | None = None,
    ) -> bool: ...

    # 作用：声明凭租约令牌提交任务成功结果的接口。
    # 参数 job_id：后台任务的唯一标识。
    # 参数 lease_token：证明当前工作进程持有任务租约的随机令牌。
    # 参数 result：待保存、清洗或转换的执行结果。
    # 参数 now：用于租约或状态判断的当前时间覆盖值。
    def complete(
        self,
        job_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None: ...

    # 作用：声明记录失败并决定重试或进入死信状态的接口。
    # 参数 job_id：后台任务的唯一标识。
    # 参数 lease_token：证明当前工作进程持有任务租约的随机令牌。
    # 参数 error：需要持久化的失败原因或错误摘要。
    # 参数 now：用于租约或状态判断的当前时间覆盖值。
    # 参数 retry_delay：任务失败后再次可领取前的延迟秒数。
    def fail(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        now: float | None = None,
        retry_delay: float | None = None,
    ) -> dict[str, Any] | None: ...

    # 作用：声明在可取消状态下停止后台任务的接口。
    # 参数 job_id：后台任务的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def cancel(
        self,
        job_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    # 作用：声明把死信任务重新放回待执行队列的接口。
    # 参数 job_id：后台任务的唯一标识。
    def retry_dead_letter(self, job_id: str) -> dict[str, Any] | None: ...

    # 作用：声明按会话和状态筛选后台任务的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 status：用于写入或筛选的业务状态值。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def list(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    # 作用：声明清理会话后台任务的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明取消关联同一 Agent 任务的后台作业接口。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    def cancel_by_task(self, task_id: str) -> list[dict[str, Any]]: ...

    # 作用：声明统计类型或会话维度活跃任务数的接口。
    # 参数 kind：任务、操作或后台作业的业务类型。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def active_count(self, *, kind: str = "", session_id: str = "") -> int: ...

    # 作用：声明计算待执行任务队列位置的接口。
    # 参数 job_id：后台任务的唯一标识。
    def queue_position(self, job_id: str) -> int: ...

    # 作用：声明汇总后台队列及租约状态的接口。
    # 参数 kinds：本次领取或统计允许包含的后台任务类型。
    def status(self, *, kinds: set[str] | None = None) -> dict[str, Any]: ...


# 作用：定义可恢复 Agent 任务、步骤、事件与检查点账本接口。
# 参数：无。
@runtime_checkable
# 作用：定义“AgentTaskRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class AgentTaskRepository(Protocol):
    """Task, step and event ledger contract for resumable Agent execution."""

    # 作用：声明为请求创建 Agent 任务账本的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 goal：Agent 任务要完成的用户目标。
    # 参数 request：创建任务时保存的原始请求快照。
    def create(
        self,
        *,
        session_id: str,
        request_id: str,
        goal: str,
        request: dict[str, Any],
    ) -> dict[str, Any]: ...

    # 作用：声明读取任务及其执行明细的接口。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    def get(self, task_id: str) -> dict[str, Any] | None: ...

    # 作用：声明按会话请求定位任务的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    def get_by_request(
        self,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any] | None: ...

    # 作用：声明删除会话任务账本的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明把重启遗留任务收敛到安全状态的接口。
    # 参数：无。
    def recover_interrupted(self) -> dict[str, int]: ...

    # 作用：声明汇总任务事件流存储状态的接口。
    # 参数：无。
    def event_status(self) -> dict[str, Any]: ...

    # 作用：声明汇总模型自检查点存储状态的接口。
    # 参数：无。
    def checkpoint_status(self) -> dict[str, Any]: ...


# 作用：定义副作用操作二次确认账本接口。
# 参数：无。
@runtime_checkable
# 作用：定义“AgentOperationRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class AgentOperationRepository(Protocol):
    """Confirmation-ledger contract for side-effecting operations."""

    # 作用：声明登记待用户确认操作及有效期的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 kind：任务、操作或后台作业的业务类型。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 payload：后台任务、确认操作或外部分发携带的结构化负载。
    # 参数 summary：展示给用户确认的操作摘要。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    # 参数 ttl_seconds：授权或待确认操作保持有效的秒数。
    def queue(
        self,
        *,
        session_id: str,
        kind: str,
        action: str,
        payload: dict[str, Any],
        summary: str,
        task_id: str = "",
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]: ...

    # 作用：声明按会话原子领取已确认操作的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 operation_id：待确认或外部分发操作的唯一标识。
    def claim(
        self,
        session_id: str,
        operation_id: str = "",
    ) -> dict[str, Any] | None: ...

    # 作用：声明读取待确认操作详情的接口。
    # 参数 operation_id：待确认或外部分发操作的唯一标识。
    def get(self, operation_id: str) -> dict[str, Any] | None: ...

    # 作用：声明清理会话确认操作的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明恢复重启时确认账本状态的接口。
    # 参数：无。
    def recover_interrupted(self) -> dict[str, Any]: ...


# 作用：定义本地已提交副作用的对账回执接口。
# 参数：无。
@runtime_checkable
# 作用：定义“EffectReceiptRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class EffectReceiptRepository(Protocol):
    """Reconciliation contract for locally committed side effects."""

    # 作用：声明按幂等键登记副作用证据回执的接口。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 idempotency_key：确保重复请求复用同一结果的幂等键。
    # 参数 evidence：证明副作用已执行或可对账的结构化证据。
    def begin(
        self,
        *,
        task_id: str,
        session_id: str,
        action: str,
        idempotency_key: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]: ...

    # 作用：声明列出任务全部副作用回执的接口。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    def list_task(self, task_id: str) -> list[dict[str, Any]]: ...

    # 作用：声明把未确认回执收敛为安全状态的接口。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    def recover_interrupted(self, *, task_id: str = "") -> int: ...

    # 作用：声明清理会话副作用回执的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明汇总副作用回执账本状态的接口。
    # 参数：无。
    def status(self) -> dict[str, Any]: ...


# 作用：定义离开进程边界的外部副作用幂等账本接口。
# 参数：无。
@runtime_checkable
# 作用：定义“ExternalDispatchRepository”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class ExternalDispatchRepository(Protocol):
    """Idempotent ledger contract for effects that leave the process."""

    # 作用：声明在调用提供方前登记负载指纹和操作标识的接口。
    # 参数 operation_id：待确认或外部分发操作的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 task_id：Agent 任务或相关副作用所属任务的唯一标识。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 payload_fingerprint：不暴露原文的外部分发负载稳定指纹。
    def prepare(
        self,
        *,
        operation_id: str,
        session_id: str,
        task_id: str,
        capability: str,
        action: str,
        payload_fingerprint: str,
    ) -> dict[str, Any]: ...

    # 作用：声明按操作 ID 读取外部分发记录的接口。
    # 参数 operation_id：待确认或外部分发操作的唯一标识。
    def get_operation(self, operation_id: str) -> dict[str, Any]: ...

    # 作用：声明将终态未知的中断分发标记为不可自动重放的接口。
    # 参数：无。
    def recover_interrupted(self) -> int: ...

    # 作用：声明清理会话外部分发记录的接口。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def delete_session(self, session_id: str) -> int: ...

    # 作用：声明汇总外部分发账本状态的接口。
    # 参数：无。
    def status(self) -> dict[str, Any]: ...
