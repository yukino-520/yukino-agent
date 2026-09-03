from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from service_club.storage.contracts import WorkflowRunRepository
from service_club.storage.relational import (
    RelationalBackend,
    RelationalConnection,
    configured_relational_backend,
)


# 作用：表示工作流定义、依赖关系或恢复操作不合法。
# 参数：无。
class WorkflowError(ValueError):
    pass


WorkflowExecutor = Callable[[str, str, dict[str, Any], bool], dict[str, Any]]


# 作用：以事务账本持久化可恢复工作流的步骤和状态。
# 参数：无。
class WorkflowRunStore:
    """Small transactional ledger for resumable capability workflows."""

    # 作用：选择关系存储后端并确保工作流表结构存在。
    # 参数 path：目标文件、数据库或状态存储路径。
    # 参数 backend：所使用的存储、执行后端或后端标识。
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        if backend is None:
            backend = configured_relational_backend()
        self.backend = backend
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._ensure_schema()

    # 作用：创建工作流运行表及按更新时间查询的索引。
    # 参数：无。
    def _ensure_schema(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at {self.backend.float_type} NOT NULL,
                    updated_at {self.backend.float_type} NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_updated
                ON workflow_runs(updated_at DESC)
                """
            )

    # 作用：创建一条工作流运行记录，并将所有步骤初始化为待执行。
    # 参数 steps：要创建、校验或排序的工作流步骤列表。
    def create(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        now = time.time()
        run_id = f"workflow-{uuid.uuid4().hex[:16]}"
        state = {
            "id": run_id,
            "status": "created",
            "steps": [
                {
                    **step,
                    "status": "pending",
                    "attempts": 0,
                    "output": {},
                    "error": "",
                    "started_at": None,
                    "completed_at": None,
                }
                for step in steps
            ],
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self.backend.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs(id, status, state_json, created_at, updated_at)
                VALUES (?, 'created', ?, ?, ?)
                """,
                (run_id, json.dumps(state, ensure_ascii=False), now, now),
            )
            self._trim(conn)
        return state

    # 作用：按运行 ID 读取并解析持久化工作流状态。
    # 参数 run_id：工作流运行记录的唯一标识。
    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self.backend.connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(str(row["state_json"]))
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    # 作用：按最近更新时间列出限定数量的工作流状态。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self.backend.connect() as conn:
            rows = conn.execute(
                """
                SELECT state_json FROM workflow_runs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(str(row["state_json"]))
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict):
                result.append(state)
        return result

    # 作用：锁定工作流记录，应用状态变更后在同一事务中保存。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 mutator：在事务锁内修改持久状态的回调。
    def update(
        self,
        run_id: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        with self._lock, self.backend.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT state_json FROM workflow_runs WHERE id = ?"
                + self.backend.for_update(),
                (run_id,),
            ).fetchone()
            if row is None:
                raise WorkflowError("没有找到指定的工作流运行记录。")
            try:
                state = json.loads(str(row["state_json"]))
            except json.JSONDecodeError as exc:
                raise WorkflowError("工作流运行记录已经损坏。") from exc
            if not isinstance(state, dict):
                raise WorkflowError("工作流运行记录格式错误。")
            mutator(state)
            now = time.time()
            state["updated_at"] = now
            conn.execute(
                """
                UPDATE workflow_runs
                SET status = ?, state_json = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(state.get("status", "failed")),
                    json.dumps(state, ensure_ascii=False),
                    str(state.get("error", ""))[:2000],
                    now,
                    run_id,
                ),
            )
        return state

    # 作用：把重启时仍在运行的流程和步骤标记为结果不确定。
    # 参数：无。
    def recover_interrupted(self) -> dict[str, int]:
        recovered_runs = 0
        recovered_steps = 0
        for item in self.list(limit=100):
            if item.get("status") != "running":
                continue

            # 作用：将单条运行记录及其运行中步骤转换为中断状态。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def recover(state: dict[str, Any]) -> None:
                nonlocal recovered_steps
                state["status"] = "interrupted"
                state["error"] = "服务重启时工作流尚未完成；已完成步骤不会自动重放。"
                for step in state.get("steps", []):
                    if isinstance(step, dict) and step.get("status") == "running":
                        step["status"] = "interrupted"
                        step["error"] = "步骤执行结果不确定，需要人工确认后恢复。"
                        step["completed_at"] = time.time()
                        recovered_steps += 1

            self.update(str(item.get("id", "")), recover)
            recovered_runs += 1
        return {"interrupted_runs": recovered_runs, "interrupted_steps": recovered_steps}

    # 作用：只保留最近一百条工作流记录，防止账本无限增长。
    # 参数 conn：执行工作流账本维护操作的关系数据库连接。
    @staticmethod
    # 作用：执行“trim”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 conn：调用方传入的conn，用于本次处理。
    def _trim(conn: RelationalConnection) -> None:
        rows = conn.execute(
            """
            SELECT id FROM workflow_runs
            ORDER BY updated_at DESC LIMIT 9223372036854775807 OFFSET 100
            """
        ).fetchall()
        if rows:
            conn.executemany(
                "DELETE FROM workflow_runs WHERE id = ?",
                [(row["id"],) for row in rows],
            )


# 作用：校验并按依赖顺序执行可暂停、恢复的能力工作流。
# 参数：无。
class DurableWorkflowRunner:
    MAX_STEPS = 8
    STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

    # 作用：初始化工作流账本，并立即收敛上次重启留下的运行状态。
    # 参数 path：目标文件、数据库或状态存储路径。
    # 参数 backend：所使用的存储、执行后端或后端标识。
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
    ) -> None:
        self.store: WorkflowRunRepository = WorkflowRunStore(path, backend=backend)
        self.recovery = self.store.recover_interrupted()

    # 作用：确认后创建并同步执行一份新工作流。
    # 参数 raw_steps：调用方提交、尚未规范化的工作流步骤列表。
    # 参数 executor：实际执行单个工作流能力步骤的回调。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def run(
        self,
        raw_steps: object,
        *,
        executor: WorkflowExecutor,
        confirmed: bool,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise WorkflowError("启动工作流需要 confirmed=true。")
        steps = self._normalize(raw_steps)
        state = self.store.create(steps)
        return self._execute(
            str(state["id"]),
            executor=executor,
            confirmed=confirmed,
            cancel_check=cancel_check,
        )

    # 作用：校验并持久化待后台执行的工作流，不占用当前请求。
    # 参数 raw_steps：调用方提交、尚未规范化的工作流步骤列表。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def prepare(self, raw_steps: object, *, confirmed: bool) -> dict[str, Any]:
        """Validate and persist a workflow without occupying the caller's request."""
        if not confirmed:
            raise WorkflowError("后台排队工作流需要 confirmed=true。")
        state = self.store.create(self._normalize(raw_steps))
        return self._public(state)

    # 作用：仅执行新建状态的预备流程，避免自动重放不确定运行。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 executor：实际执行单个工作流能力步骤的回调。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def execute_prepared(
        self,
        run_id: str,
        *,
        executor: WorkflowExecutor,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Execute only a newly prepared run; uncertain runs are never auto-replayed."""
        state = self.store.get(run_id)
        if state is None:
            raise WorkflowError("没有找到指定的工作流运行记录。")
        if state.get("status") != "created":
            return self._public(state)
        return self._execute(
            run_id,
            executor=executor,
            confirmed=True,
            cancel_check=cancel_check,
        )

    # 作用：经再次确认后重置未完成步骤并恢复执行。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 executor：实际执行单个工作流能力步骤的回调。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def resume(
        self,
        run_id: str,
        *,
        executor: WorkflowExecutor,
        confirmed: bool,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise WorkflowError("恢复工作流需要再次明确确认。")
        state = self.store.get(run_id)
        if state is None:
            raise WorkflowError("没有找到指定的工作流运行记录。")
        if state.get("status") == "completed":
            return self._public(state)

        # 作用：保留已完成结果，仅把其余步骤重置为待执行。
        # 参数 current：事务中正在修改的当前持久状态。
        def reset_incomplete(current: dict[str, Any]) -> None:
            current["status"] = "resuming"
            current["error"] = ""
            for step in current.get("steps", []):
                if isinstance(step, dict) and step.get("status") != "completed":
                    step["status"] = "pending"
                    step["error"] = ""
                    step["started_at"] = None
                    step["completed_at"] = None

        self.store.update(run_id, reset_incomplete)
        return self._execute(
            run_id,
            executor=executor,
            confirmed=confirmed,
            cancel_check=cancel_check,
        )

    # 作用：读取指定工作流并转换为公开视图。
    # 参数 run_id：工作流运行记录的唯一标识。
    def get(self, run_id: str) -> dict[str, Any]:
        state = self.store.get(run_id)
        if state is None:
            raise WorkflowError("没有找到指定的工作流运行记录。")
        return self._public(state)

    # 作用：列出工作流摘要，并隐藏可能敏感的步骤参数。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return [self._public(item, include_arguments=False) for item in self.store.list(limit=limit)]

    # 作用：汇总各状态工作流数量和启动恢复结果。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        runs = self.store.list(limit=100)
        counts: dict[str, int] = {}
        for run in runs:
            status = str(run.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "ok": True,
            "run_count": len(runs),
            "status_counts": counts,
            "recovery": dict(self.recovery),
        }

    # 作用：按拓扑顺序解析依赖输出、调用能力并逐步持久化结果。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 executor：实际执行单个工作流能力步骤的回调。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _execute(
        self,
        run_id: str,
        *,
        executor: WorkflowExecutor,
        confirmed: bool,
        cancel_check: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        self.store.update(run_id, lambda state: state.update({"status": "running", "error": ""}))
        state = self.store.get(run_id)
        if state is None:
            raise WorkflowError("工作流运行记录丢失。")
        stop = False
        for current in state.get("steps", []):
            if not isinstance(current, dict) or current.get("status") == "completed":
                continue
            step_id = str(current.get("id", ""))
            state = self.store.get(run_id) or state
            step = self._find_step(state, step_id)
            if stop:
                self._finish_step(run_id, step_id, "skipped", error="前序步骤要求失败后停止。")
                continue
            if cancel_check is not None and cancel_check():
                self._cancel_remaining(run_id)
                return self._public(self.store.get(run_id) or state)
            dependency_states = {
                dependency: str(self._find_step(state, dependency).get("status", "missing"))
                for dependency in step.get("depends_on", [])
            }
            if any(status != "completed" for status in dependency_states.values()):
                self._finish_step(
                    run_id,
                    step_id,
                    "skipped",
                    error="依赖步骤未完成："
                    + "、".join(
                        f"{key}={value}" for key, value in dependency_states.items()
                    ),
                )
                continue
            try:
                arguments = self._resolve(
                    step.get("arguments", {}),
                    state,
                    allowed_sources=set(step.get("depends_on", [])),
                )
            except WorkflowError as exc:
                self._finish_step(run_id, step_id, "failed", error=str(exc))
                stop = bool(step.get("stop_on_error", True))
                continue
            self._start_step(run_id, step_id, arguments)
            execution_arguments = dict(arguments)
            execution_arguments["_agent_workflow_context"] = {
                "run_id": run_id,
                "step_id": step_id,
            }
            try:
                output = executor(
                    str(step.get("capability", "")),
                    str(step.get("action", "")),
                    execution_arguments,
                    confirmed and bool(step.get("confirmed", False)),
                )
            except Exception as exc:  # executor boundaries must remain auditable
                output = {"ok": False, "error": str(exc)}
            if bool(output.get("ok")):
                self._finish_step(run_id, step_id, "completed", output=output)
            else:
                self._finish_step(
                    run_id,
                    step_id,
                    "failed",
                    output=output,
                    error=str(output.get("error", "工作流步骤执行失败。")),
                )
                stop = bool(step.get("stop_on_error", True))
        return self._finalize(run_id)

    # 作用：根据全部步骤状态计算流程终态和错误摘要。
    # 参数 run_id：工作流运行记录的唯一标识。
    def _finalize(self, run_id: str) -> dict[str, Any]:
        state = self.store.get(run_id)
        if state is None:
            raise WorkflowError("工作流运行记录丢失。")
        statuses = [str(step.get("status", "failed")) for step in state.get("steps", [])]
        if statuses and all(status == "completed" for status in statuses):
            final_status = "completed"
            error = ""
        elif "cancelled" in statuses:
            final_status = "cancelled"
            error = "工作流已经停止。"
        elif "interrupted" in statuses:
            final_status = "interrupted"
            error = "工作流包含执行结果不确定的中断步骤。"
        else:
            final_status = "failed"
            failed = [
                str(step.get("id", ""))
                for step in state.get("steps", [])
                if step.get("status") in {"failed", "skipped"}
            ]
            error = "未完成步骤：" + "、".join(failed)

        # 作用：把计算出的终态和错误写入工作流记录。
        # 参数 current：事务中正在修改的当前持久状态。
        def finish(current: dict[str, Any]) -> None:
            current["status"] = final_status
            current["error"] = error

        return self._public(self.store.update(run_id, finish))

    # 作用：在真正调用能力前记录步骤参数、尝试次数和开始时间。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 step_id：工作流步骤的唯一标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    def _start_step(self, run_id: str, step_id: str, arguments: dict[str, Any]) -> None:
        # 作用：把指定步骤原子转换为运行中状态。
        # 参数 state：当前读取、修改或公开的持久状态对象。
        def start(state: dict[str, Any]) -> None:
            step = self._find_step(state, step_id)
            step["status"] = "running"
            step["attempts"] = int(step.get("attempts", 0)) + 1
            step["resolved_arguments"] = arguments
            step["started_at"] = time.time()
            step["completed_at"] = None

        self.store.update(run_id, start)

    # 作用：记录指定步骤的结果、错误和完成时间。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 step_id：工作流步骤的唯一标识。
    # 参数 status：用于写入或筛选的业务状态值。
    # 参数 output：工作流步骤产生并需要持久化的结构化结果。
    # 参数 error：需要持久化的失败原因或错误摘要。
    def _finish_step(
        self,
        run_id: str,
        step_id: str,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        # 作用：把执行结果写入对应步骤的持久状态。
        # 参数 state：当前读取、修改或公开的持久状态对象。
        def finish(state: dict[str, Any]) -> None:
            step = self._find_step(state, step_id)
            step["status"] = status
            step["output"] = output or {}
            step["error"] = error[:2000]
            step["completed_at"] = time.time()

        self.store.update(run_id, finish)

    # 作用：将工作流和所有未完成步骤统一标记为已取消。
    # 参数 run_id：工作流运行记录的唯一标识。
    def _cancel_remaining(self, run_id: str) -> None:
        # 作用：在一条状态变更中取消剩余步骤并记录原因。
        # 参数 state：当前读取、修改或公开的持久状态对象。
        def cancel(state: dict[str, Any]) -> None:
            state["status"] = "cancelled"
            state["error"] = "工作流已按用户要求停止。"
            for step in state.get("steps", []):
                if isinstance(step, dict) and step.get("status") != "completed":
                    step["status"] = "cancelled"
                    step["error"] = "工作流已停止。"
                    step["completed_at"] = time.time()

        self.store.update(run_id, cancel)

    # 作用：规范化步骤字段，限制数量并拒绝递归工作流。
    # 参数 raw_steps：调用方提交、尚未规范化的工作流步骤列表。
    def _normalize(self, raw_steps: object) -> list[dict[str, Any]]:
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > self.MAX_STEPS:
            raise WorkflowError(f"工作流需要 1-{self.MAX_STEPS} 个步骤。")
        normalized: list[dict[str, Any]] = []
        ids: set[str] = set()
        for index, raw in enumerate(raw_steps, 1):
            if not isinstance(raw, dict):
                raise WorkflowError("工作流步骤必须是对象。")
            step_id = str(raw.get("id") or f"step_{index}").strip()
            if not self.STEP_ID.fullmatch(step_id) or step_id in ids:
                raise WorkflowError(f"工作流步骤 id 无效或重复：{step_id}")
            capability = str(raw.get("capability", "")).strip()
            action = str(raw.get("action", "")).strip()
            if not capability or not action:
                raise WorkflowError(f"步骤 {step_id} 缺少 capability 或 action。")
            if capability == "workflows":
                raise WorkflowError("工作流不能递归调用工作流。")
            dependencies = raw.get("depends_on", [])
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if not isinstance(dependencies, list):
                raise WorkflowError(f"步骤 {step_id} 的 depends_on 必须是数组。")
            depends_on = [str(item).strip() for item in dependencies if str(item).strip()]
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, dict):
                raise WorkflowError(f"步骤 {step_id} 的 arguments 必须是对象。")
            normalized.append(
                {
                    "id": step_id,
                    "sequence": index,
                    "capability": capability,
                    "action": action,
                    "arguments": arguments,
                    "depends_on": list(dict.fromkeys(depends_on)),
                    "confirmed": bool(raw.get("confirmed", False)),
                    "stop_on_error": bool(raw.get("stop_on_error", True)),
                }
            )
            ids.add(step_id)
        self._validate_graph(normalized)
        return self._topological(normalized)

    # 作用：校验依赖步骤存在且不存在自依赖。
    # 参数 steps：要创建、校验或排序的工作流步骤列表。
    @staticmethod
    # 作用：执行“validate_graph”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 steps：调用方传入的steps，用于本次处理。
    def _validate_graph(steps: list[dict[str, Any]]) -> None:
        ids = {str(step["id"]) for step in steps}
        for step in steps:
            unknown = set(step["depends_on"]) - ids
            if unknown:
                raise WorkflowError(
                    f"步骤 {step['id']} 引用了不存在的依赖：{'、'.join(sorted(unknown))}"
                )
            if step["id"] in step["depends_on"]:
                raise WorkflowError(f"步骤 {step['id']} 不能依赖自身。")

    # 作用：按依赖关系稳定拓扑排序，并拒绝循环依赖。
    # 参数 steps：要创建、校验或排序的工作流步骤列表。
    @staticmethod
    # 作用：执行“topological”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 steps：调用方传入的steps，用于本次处理。
    def _topological(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remaining = {str(step["id"]): step for step in steps}
        completed: set[str] = set()
        ordered: list[dict[str, Any]] = []
        while remaining:
            ready = [
                step
                for step in remaining.values()
                if set(step["depends_on"]) <= completed
            ]
            if not ready:
                raise WorkflowError("工作流依赖形成了循环。")
            ready.sort(key=lambda item: int(item["sequence"]))
            for step in ready:
                step_id = str(step["id"])
                ordered.append(step)
                completed.add(step_id)
                remaining.pop(step_id)
        return ordered

    # 作用：递归解析参数中的前序步骤输出引用。
    # 参数 value：待解析、清洗、转义或递归处理的输入值。
    # 参数 state：当前读取、修改或公开的持久状态对象。
    # 参数 allowed_sources：允许被当前步骤引用输出的前序步骤 ID 集合。
    def _resolve(
        self,
        value: Any,
        state: dict[str, Any],
        *,
        allowed_sources: set[str],
    ) -> Any:
        if isinstance(value, list):
            return [self._resolve(item, state, allowed_sources=allowed_sources) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) <= {"$from", "path"} and "$from" in value:
            source_id = str(value.get("$from", ""))
            if source_id not in allowed_sources:
                raise WorkflowError(f"输出引用 {source_id} 必须同时声明为 depends_on。")
            source = self._find_step(state, source_id)
            if source.get("status") != "completed":
                raise WorkflowError(f"输出引用 {source_id} 尚未完成。")
            current: Any = source.get("output", {})
            path = str(value.get("path", "result")).strip()
            for part in [item for item in path.split(".") if item]:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                    current = current[int(part)]
                else:
                    raise WorkflowError(f"输出引用 {source_id}.{path} 不存在。")
            return current
        return {
            key: self._resolve(item, state, allowed_sources=allowed_sources)
            for key, item in value.items()
        }

    # 作用：从运行状态中查找指定步骤，不存在时立即报错。
    # 参数 state：当前读取、修改或公开的持久状态对象。
    # 参数 step_id：工作流步骤的唯一标识。
    @staticmethod
    # 作用：执行“find_step”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 state：调用方传入的state，用于本次处理。
    # 参数 step_id：调用方传入的step_id，用于本次处理。
    def _find_step(state: dict[str, Any], step_id: str) -> dict[str, Any]:
        for step in state.get("steps", []):
            if isinstance(step, dict) and step.get("id") == step_id:
                return step
        raise WorkflowError(f"工作流步骤不存在：{step_id}")

    # 作用：生成可返回给调用方的工作流视图，并可选择隐藏参数。
    # 参数 state：当前读取、修改或公开的持久状态对象。
    # 参数 include_arguments：公开工作流状态时是否包含原始和解析后参数。
    @staticmethod
    # 作用：执行“public”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 state：调用方传入的state，用于本次处理。
    # 参数 include_arguments：调用方传入的include_arguments，用于本次处理。
    def _public(
        state: dict[str, Any],
        *,
        include_arguments: bool = True,
    ) -> dict[str, Any]:
        result = {
            "id": str(state.get("id", "")),
            "status": str(state.get("status", "unknown")),
            "error": str(state.get("error", "")),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "steps": [],
        }
        for raw in state.get("steps", []):
            if not isinstance(raw, dict):
                continue
            step = {
                key: raw.get(key)
                for key in (
                    "id",
                    "sequence",
                    "capability",
                    "action",
                    "depends_on",
                    "confirmed",
                    "stop_on_error",
                    "status",
                    "attempts",
                    "output",
                    "error",
                    "started_at",
                    "completed_at",
                )
            }
            if include_arguments:
                step["arguments"] = raw.get("arguments", {})
                step["resolved_arguments"] = raw.get("resolved_arguments", {})
            result["steps"].append(step)
        return result
