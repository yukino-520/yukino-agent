from typing import Any


# 作用：汇总核心组件的自检结果，为健康检查接口提供统一诊断报告。
# 参数：无。
class ServiceClubDoctor:
    # 作用：保存待检查的 Agent 核心实例。
    # 参数 core：共享的 Agent 核心运行时实例。
    def __init__(self, core: Any) -> None:
        self.core = core

    # 作用：执行存储、模型、工具、记忆和安全能力的全量健康检查。
    # 参数：无。
    def run(self) -> dict[str, Any]:
        checks = {
            "memory": self._check_memory(),
            "permanent_memory": self._check_permanent_memory(),
            "agents": self._check_agents(),
            "tools": self._check_tools(),
            "attachments": self._check_attachments(),
            "capabilities": self._check_capabilities(),
            "router": self._check_router(),
            "mental_state": self.core.mental_state.global_status(),
            "proactive_nudges": self._check_proactive_nudges(),
            "scheduled_reminders": self.core.memory.reminder_status(),
            "background_jobs": self.core.background_jobs.status(),
            "task_events": self.core.agent_tasks.event_status(),
            "task_budget": self.core.agent_tasks.budget_configuration_status(),
            "task_checkpoints": self.core.agent_tasks.checkpoint_status(),
            "effect_receipts": self.core.tools.registry.effect_receipts.status(),
            "external_dispatches": self.core.tools.registry.external_dispatches.status(),
            "capability_policy": self.core.capabilities.permission_policy.status(),
            "python_sandbox": self.core.capabilities.python.status(),
            "capability_boundaries": self.core.capabilities.boundary_security_status(),
            "storage": self._check_storage(),
            "tool_output_security": self.core.output_security.status(),
            "agent_execution_guards": self.core.execution_guard_status(),
            "behavior_instincts": self._check_behavior_instincts(),
            "context_window": self._check_context_window(),
            "memory_retrieval": {
                "ok": True,
                **self.core.memory_retriever.status(),
            },
            "vector_index": self._check_vector_index(),
            "knowledge_graph": self.core.capabilities.knowledge_graph.status(
                probe=True
            ),
            "profile_facts": {
                "ok": True,
                **self.core.profile_fact_learner.status(),
            },
            "response_quality": {
                "ok": True,
                **self.core.response_quality_guard.status(),
            },
            "interaction_learning": {
                "ok": True,
                **self.core.interaction_outcomes.status(),
            },
            "self_model": self._check_self_model(),
            "club_orchestration": {
                "ok": True,
                **self.core.club_orchestrator.status(),
            },
            "traces": self.core.traces.status(),
            "degradation": self.core.degradation_monitor.status(),
            "network_resilience": self.core.capabilities.network.status(),
            "workflows": self.core.capabilities.workflows.status(),
            "model_runtime": (
                self.core.model.status()
                if callable(getattr(self.core.model, "status", None))
                else {"ok": True, "available": False}
            ),
        }
        return {
            "ok": all(
                item.get("ok", True)
                for item in checks.values()
                if isinstance(item, dict)
            ),
            "checks": checks,
        }

    # 作用：通过最小召回操作验证关系型记忆源是否可读。
    # 参数：无。
    def _check_memory(self) -> dict[str, Any]:
        try:
            self.core.memory.recall("__doctor__", "", limit=1)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "backend": self.core.memory.backend.name,
            "location": self.core.memory.backend.location,
        }

    # 作用：汇总存储能力及各持久化仓库实际使用的后端类型。
    # 参数：无。
    def _check_storage(self) -> dict[str, Any]:
        status = self.core.capabilities.storage_status()
        repositories = {
            "memory": self.core.memory,
            "conversations": self.core.conversations,
            "attachments": self.core.attachments,
            "agent_requests": self.core.agent_requests,
            "agent_tasks": self.core.agent_tasks,
            "background_jobs": self.core.background_jobs,
            "agent_operations": self.core.tools.registry.operations,
            "effect_receipts": self.core.tools.registry.effect_receipts,
            "external_dispatches": self.core.tools.registry.external_dispatches,
        }
        status["repository_backends"] = {
            **status.get("repository_backends", {}),
            **{
                name: getattr(getattr(repository, "backend", None), "name", "unknown")
                for name, repository in repositories.items()
            },
        }
        status["search"] = self.core.search_index.status(probe=True)
        status["events"] = self.core.event_stream.status(probe=True)
        status["knowledge_base"] = self.core.knowledge_base.status()
        status["ok"] = bool(
            status.get("ok", True)
            and status["search"].get("ok")
            and status["events"].get("ok")
        )
        return status

    # 作用：获取永久记忆服务的可用状态。
    # 参数：无。
    def _check_permanent_memory(self) -> dict[str, Any]:
        status = self.core.permanent_memory.status()
        return {"ok": True, **status}

    # 作用：探测向量索引；未启用时明确标示关系库仍为事实源。
    # 参数：无。
    def _check_vector_index(self) -> dict[str, Any]:
        if self.core.vector_store is None:
            return {
                "ok": True,
                "enabled": False,
                "backend": self.core.memory.backend.name,
                "connection_state": "disabled",
                "source_of_truth": "relational",
            }
        return self.core.vector_store.status(probe=True)

    # 作用：检查角色注册表是否至少包含一个可用 Agent。
    # 参数：无。
    def _check_agents(self) -> dict[str, Any]:
        manifest = self.core.character_agents.manifest()
        return {"ok": len(manifest) > 0, "count": len(manifest)}

    # 作用：检查工具注册表是否成功加载可调用工具。
    # 参数：无。
    def _check_tools(self) -> dict[str, Any]:
        manifest = self.core.tools.registry.manifest()
        return {"ok": len(manifest) > 0, "count": len(manifest)}

    # 作用：用无副作用查询验证附件仓库并报告上传限制。
    # 参数：无。
    def _check_attachments(self) -> dict[str, Any]:
        try:
            self.core.attachments.list("__doctor__")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "max_upload_bytes": self.core.attachments.MAX_UPLOAD_BYTES,
            "max_per_request": self.core.attachments.MAX_ATTACHMENTS_PER_REQUEST,
        }

    # 作用：统计能力清单中已就绪和降级的适配器数量。
    # 参数：无。
    def _check_capabilities(self) -> dict[str, Any]:
        manifest = self.core.capabilities.manifest()
        ready = sum(item["state"] == "ready" for item in manifest)
        return {
            "ok": len(manifest) >= 18,
            "count": len(manifest),
            "ready": ready,
            "degraded": len(manifest) - ready,
        }

    # 作用：返回路由反馈学习组件的运行状态。
    # 参数：无。
    def _check_router(self) -> dict[str, Any]:
        return {
            "ok": True,
            "feedback": self.core.route_feedback.status(),
        }

    # 作用：验证主动关怀队列可访问并返回其统计状态。
    # 参数：无。
    def _check_proactive_nudges(self) -> dict[str, Any]:
        try:
            self.core.proactive_nudges.pending("__doctor__")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **self.core.proactive_nudges.status()}

    # 作用：验证行为本能存储可访问并返回其状态。
    # 参数：无。
    def _check_behavior_instincts(self) -> dict[str, Any]:
        try:
            self.core.behavior_instincts.list_all("__doctor__")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **self.core.behavior_instincts.status()}

    # 作用：通过生成空会话检查点验证上下文窗口组件。
    # 参数：无。
    def _check_context_window(self) -> dict[str, Any]:
        try:
            self.core.context_window.checkpoint("__doctor__")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **self.core.context_window.status()}

    # 作用：检查自我模型仓库，并以底层存储健康度作为总结果。
    # 参数：无。
    def _check_self_model(self) -> dict[str, Any]:
        status = self.core.self_model.status()
        return {"ok": status["store_ok"], **status}
