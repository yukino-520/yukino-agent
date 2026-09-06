from collections.abc import Callable
from pathlib import Path

from service_club.capabilities import CapabilityHub
from service_club.core.companion.affective_state import AffectiveStateTracker
from service_club.core.companion.behavior_instinct import BehaviorInstinctManager
from service_club.core.companion.interaction_outcome import InteractionOutcomeLearner
from service_club.core.companion.mental_state import MentalStateTracker
from service_club.core.companion.preference_learning import PreferenceLearner
from service_club.core.companion.proactive_care import ProactiveCarePlanner
from service_club.core.companion.proactive_nudge import ProactiveNudgeEngine
from service_club.core.companion.relationship import RelationshipTracker
from service_club.core.companion.self_model import SelfModelManager
from service_club.core.conversation.character_agent import CharacterAgentRegistry
from service_club.core.conversation.characters import get_character
from service_club.core.conversation.club_orchestrator import ClubOrchestrator
from service_club.core.conversation.emotion import detect_emotion
from service_club.core.conversation.intent import IntentDecomposer
from service_club.core.conversation.llm import ServiceClubModel
from service_club.core.conversation.modes import infer_mode
from service_club.core.conversation.postprocess import clean_reply, pick_sticker, synthesize_tts_optional
from service_club.core.conversation.response_policy import ResponsePolicyEnforcer, ResponsePolicyPlanner
from service_club.core.conversation.response_quality import ResponseQualityGuard
from service_club.core.conversation.route_feedback import RouteFeedbackTracker
from service_club.core.conversation.routing import ServiceClubRouter
from service_club.core.conversation.safety import detect_safety_level
from service_club.core.memory import MemoryManager
from service_club.core.memory.context_window import ContextWindowManager
from service_club.core.memory.conversations import ConversationStore
from service_club.core.memory.embeddings import OpenAIEmbeddingProvider
from service_club.core.memory.hybrid_retrieval import HybridMemoryRetriever
from service_club.core.memory.knowledge_base import KnowledgeBaseStore
from service_club.core.memory.memory_consolidation import MemoryConsolidator
from service_club.core.memory.permanent_memory import PermanentMemoryManager
from service_club.core.memory.profile_facts import ProfileFactLearner
from service_club.core.memory.reflection import TurnReflectionEngine
from service_club.core.memory.reunion import ReunionPlanner
from service_club.core.memory.search_index import configured_search_index
from service_club.core.memory.spontaneous_recall import SpontaneousRecallEngine
from service_club.core.memory.user_profile import UserProfileSynthesizer
from service_club.core.memory.vector_store import configured_vector_store
from service_club.core.runtime.agent_request_store import AgentRequestStore
from service_club.core.runtime.agent_task_store import (
    AgentTaskStore,
    agent_task_scope,
    current_agent_task_id,
)
from service_club.core.runtime.attachment_store import AttachmentStore
from service_club.core.runtime.background_jobs import BackgroundJobStore
from service_club.core.runtime.concurrency_gate import RuntimeConcurrencyGate
from service_club.core.runtime.event_stream import KafkaOutboxDispatcher
from service_club.core.runtime.execution_contract import AgentExecutionPlanner
from service_club.core.runtime.outcome_verifier import AgentOutcomeVerifier
from service_club.core.runtime.runtime_degradation import RuntimeDegradationMonitor
from service_club.core.runtime.runtime_trace import RuntimeTraceStore
from service_club.core.runtime.tool_output_security import ToolOutputSecurity
from service_club.core.runtime.usage import UsageLedger
from service_club.core.tooling.executor import ToolExecutor
from service_club.core.tooling.result_presenter import present_degraded_tool_results
from service_club.core.tooling.tool_calling import ToolCallExtractor
from service_club.core.types import ChatRequest, ChatResponse, ToolExecutionResult
from service_club.settings import settings
from service_club.storage.contracts import AgentRequestRepository
from service_club.storage.relational import configured_relational_backend


# 作用：表示持久任务不满足恢复条件，例如会话、目标或附件与原任务不一致。
class AgentTaskResumeError(RuntimeError):
    pass


# 作用：作为系统核心编排器，串联角色路由、记忆检索、工具执行、证据验收和任务持久化。
# 构造参数：各项可替换依赖由 __init__ 说明；未传入时使用项目默认实现。
class ServiceClubCore:
    # 作用：初始化一次服务运行所需的模型、存储、Agent、能力中心与故障恢复组件。
    # 参数 model：可注入的模型适配器；为空时创建 ServiceClubModel。
    # 参数 memory：关系事实存储管理器；为空时使用项目配置的数据库。
    # 参数 permanent_memory：长期记忆管理器；为空时创建默认实例。
    # 参数 trace_store：运行链路追踪存储；用于替换默认追踪实现。
    # 参数 degradation_monitor：降级监控器；记录外部依赖的降级情况。
    # 参数 route_feedback：角色路由反馈器；保存成功率和延迟反馈。
    # 参数 mental_state：心理状态追踪器；维护会话级情绪演变。
    # 参数 preference_learner：用户偏好学习器；从对话提取表达偏好。
    # 参数 sticker_base：角色表情资源根目录；为空时读取全局设置。
    def __init__(
        self,
        model: object | None = None,
        memory: MemoryManager | None = None,
        permanent_memory: PermanentMemoryManager | None = None,
        trace_store: RuntimeTraceStore | None = None,
        degradation_monitor: RuntimeDegradationMonitor | None = None,
        route_feedback: RouteFeedbackTracker | None = None,
        mental_state: MentalStateTracker | None = None,
        preference_learner: PreferenceLearner | None = None,
        sticker_base: str | Path | None = None,
    ) -> None:
        self._uses_default_storage = memory is None
        self.usage = UsageLedger(settings.data_dir / "usage.jsonl")
        self.runtime_gate = RuntimeConcurrencyGate()
        self.model = model or ServiceClubModel(self.usage)
        self.output_security = ToolOutputSecurity()
        self.memory = memory or MemoryManager(
            backend=configured_relational_backend(),
        )
        self.memory.init()
        self.search_index = configured_search_index()
        self.memory.bind_search_index(self.search_index)
        self.knowledge_base = KnowledgeBaseStore(
            self.memory.backend,
            self.search_index,
            generate_fn=(
                self.model.generate_text
                if callable(getattr(self.model, "generate_text", None))
                else None
            ),
        )
        self.vector_store = configured_vector_store(settings.data_dir)
        self.memory.bind_vector_store(self.vector_store)
        self.memory_lifecycle = self.memory.consolidate_memories()
        self.agent_requests: AgentRequestRepository = AgentRequestStore(
            backend=self.memory.backend
        )
        self.agent_tasks = AgentTaskStore(backend=self.memory.backend)
        self.background_jobs = BackgroundJobStore(backend=self.memory.backend)
        self.agent_recovery = self.agent_tasks.recover_interrupted()
        self.agent_recovery["interrupted_requests"] = self.agent_requests.recover_interrupted()
        self.agent_recovery["orphaned_background_jobs"] = (
            self.background_jobs.expire_orphaned_running()
        )
        self.outcome_verifier = AgentOutcomeVerifier()
        self.execution_planner = AgentExecutionPlanner()
        self.conversations = ConversationStore(self.memory)
        self.permanent_memory = permanent_memory or PermanentMemoryManager(
            settings.data_dir,
            backend=self.memory.backend,
            migrate_legacy=self._uses_default_storage,
        )
        self.traces = trace_store or RuntimeTraceStore()
        self.degradation_monitor = degradation_monitor or RuntimeDegradationMonitor()
        self.route_feedback = route_feedback or RouteFeedbackTracker(store=self.memory)
        self.mental_state = mental_state or MentalStateTracker(store=self.memory)
        self.preference_learner = preference_learner or PreferenceLearner()
        self.affective_state = AffectiveStateTracker(self.memory)
        self.behavior_instincts = BehaviorInstinctManager(self.memory)
        self.context_window = ContextWindowManager(self.memory)
        self.embedding_provider = OpenAIEmbeddingProvider.from_env()
        self.knowledge_base.embedding_provider = self.embedding_provider
        self.knowledge_base.vector_store = self.vector_store
        self.memory_retriever = HybridMemoryRetriever(
            self.memory,
            embedding_provider=self.embedding_provider,
            vector_store=self.vector_store,
            search_index=self.search_index,
            generate_fn=(
                self.model.generate_text
                if callable(getattr(self.model, "generate_text", None))
                else None
            ),
        )
        self.event_stream = KafkaOutboxDispatcher(
            self.memory.backend, projector=self.search_index.project_event
        )
        self.profile_fact_learner = ProfileFactLearner(self.memory)
        self.intent_decomposer = IntentDecomposer()
        self.spontaneous_recall = SpontaneousRecallEngine()
        self.profile_synthesizer = UserProfileSynthesizer()
        self.reflection_engine = TurnReflectionEngine()
        self.reunion_planner = ReunionPlanner()
        self.proactive_care_planner = ProactiveCarePlanner()
        self.proactive_nudges = ProactiveNudgeEngine(self.memory)
        self.memory_consolidator = MemoryConsolidator()
        self.relationship_tracker = RelationshipTracker(self.memory)
        self.response_policy_planner = ResponsePolicyPlanner()
        self.response_policy_enforcer = ResponsePolicyEnforcer()
        self.response_quality_guard = ResponseQualityGuard(self.memory)
        self.interaction_outcomes = InteractionOutcomeLearner(self.memory)
        self.self_model = SelfModelManager(self.memory)
        self.router = ServiceClubRouter(self.route_feedback)
        self.capabilities = CapabilityHub(
            settings.data_dir,
            background_jobs=self.background_jobs,
            database_path=self.memory.db_path,
            backend=self.memory.backend,
            migrate_legacy=self._uses_default_storage,
        )
        self.memory.bind_knowledge_graph(self.capabilities.knowledge_graph)
        self.memory_retriever.knowledge_graph = self.capabilities.knowledge_graph
        self.attachments = AttachmentStore(
            root=self.capabilities.workspace.root / "attachments",
            backend=self.memory.backend,
        )
        self.tools = ToolExecutor(self.memory, self.permanent_memory, self.capabilities)
        self.agent_recovery["uncertain_external_dispatches"] = (
            self.tools.registry.external_dispatches.recover_interrupted()
        )
        operation_recovery = self.tools.registry.operations.recover_interrupted()
        self.agent_recovery["interrupted_operations"] = operation_recovery[
            "interrupted_operations"
        ]
        for task_id in operation_recovery["task_ids"]:
            self.agent_tasks.resolve_waiting_step(
                task_id,
                status="interrupted",
                error="确认后的操作在服务重启时处于执行中，结果需要人工检查。",
            )
            self.agent_tasks.mark(
                task_id,
                status="interrupted",
                phase="operation_interrupted",
                outcome={
                    "status": "interrupted",
                    "verified": False,
                    "summary": "确认后的操作在服务重启时中断，结果可能不确定，未自动重放。",
                },
                error="操作结果不确定，请检查外部状态或文件内容。",
            )
        self.agent_recovery["completed_external_dispatches_reconciled"] = (
            self.tools.registry.reconcile_completed_external_dispatches()
        )
        self.agent_recovery["effect_reconciliation"] = (
            self.tools.registry.reconcile_interrupted_effects()
        )
        self._bind_model_tools()
        self.character_agents = CharacterAgentRegistry(self.model)
        self.club_orchestrator = ClubOrchestrator(self.character_agents)
        self.sticker_base = Path(sticker_base or settings.sticker_dir)

    def start_data_plane(self) -> None:
        """Fail closed unless required search and event services are reachable."""
        self.search_index.ensure_indices()
        kafka = self.event_stream.status(probe=True)
        if not kafka.get("ok"):
            raise RuntimeError(f"Kafka 不可用：{kafka.get('last_error', 'unknown error')}")
        self.event_stream.start()

    def stop_data_plane(self) -> None:
        self.event_stream.stop()
        self.search_index.close()

    # 作用：在并发安全边界内重建依赖环境配置的模型与外部能力适配器。
    # 参数 before_reload：可选回调，在替换外部适配器前执行准备或校验逻辑。
    def reload_external_configuration(
        self,
        before_reload: Callable[[], None] | None = None,
    ) -> None:
        """Refresh adapters whose clients are constructed from environment settings."""
        with self.runtime_gate.reconfiguration():
            if before_reload is not None:
                before_reload()
            model = ServiceClubModel(self.usage)
            capabilities = CapabilityHub(
                settings.data_dir,
                background_jobs=self.background_jobs,
                database_path=self.memory.db_path,
                backend=self.memory.backend,
                migrate_legacy=self._uses_default_storage,
            )
            tools = ToolExecutor(self.memory, self.permanent_memory, capabilities)
            binder = getattr(model, "bind_tool_registry", None)
            if callable(binder):
                binder(tools.registry)
            security_binder = getattr(model, "bind_tool_output_security", None)
            if callable(security_binder):
                security_binder(self.output_security)
            character_agents = CharacterAgentRegistry(model)
            club_orchestrator = ClubOrchestrator(character_agents)
            embedding_provider = OpenAIEmbeddingProvider.from_env()
            vector_store = configured_vector_store(settings.data_dir)
            self.knowledge_base.generate_fn = (
                model.generate_text
                if callable(getattr(model, "generate_text", None))
                else None
            )
            memory_retriever = HybridMemoryRetriever(
                self.memory,
                embedding_provider=embedding_provider,
                vector_store=vector_store,
                search_index=self.search_index,
                knowledge_graph=capabilities.knowledge_graph,
                generate_fn=(
                    model.generate_text
                    if callable(getattr(model, "generate_text", None))
                    else None
                ),
            )
            previous_vector_store = self.vector_store
            previous_capabilities = self.capabilities
            self.memory.bind_vector_store(vector_store)
            self.memory.bind_knowledge_graph(capabilities.knowledge_graph)
            self.model = model
            self.capabilities = capabilities
            self.tools = tools
            self.character_agents = character_agents
            self.club_orchestrator = club_orchestrator
            self.embedding_provider = embedding_provider
            self.vector_store = vector_store
            self.knowledge_base.embedding_provider = embedding_provider
            self.knowledge_base.vector_store = vector_store
            self.memory_retriever = memory_retriever
            if previous_vector_store is not None:
                previous_vector_store.close()
            previous_capabilities.knowledge_graph.close()

    # 作用：在运行时并发门控保护下处理一个聊天请求。
    # 参数 request：聊天请求，包含消息、会话、路由、附件和任务恢复信息。
    # 返回：包含回复文本、工具证据和运行摘要的 ChatResponse。
    def process(self, request: ChatRequest) -> ChatResponse:
        with self.runtime_gate.turn():
            return self._process(request)

    # 作用：创建或认领持久任务，处理请求幂等，并把本轮交给核心编排流程。
    # 参数 request：已经通过入口门控的聊天请求。
    # 返回：新执行、缓存复用或恢复执行产生的 ChatResponse。
    def _process(self, request: ChatRequest) -> ChatResponse:
        if request.resume_task_id:
            return self._resume_task(request)
        request_id = request.request_id.strip()
        latest_user = next(
            (message for message in reversed(request.messages) if message.role == "user"),
            None,
        )
        task = self.agent_tasks.create(
            session_id=request.session_id,
            request_id=request_id,
            goal=latest_user.content if latest_user is not None else "继续会话",
            request=request.model_dump(mode="json"),
        )
        if request_id:
            request_hash = self.agent_requests.fingerprint(
                request.model_dump(exclude={"request_id"})
            )
            claim = self.agent_requests.claim(request.session_id, request_id, request_hash)
            if claim.state == "cached" and claim.response is not None:
                return ChatResponse.model_validate(claim.response)
        self.agent_tasks.mark(str(task["id"]), status="running", phase="planning")
        try:
            with agent_task_scope(str(task["id"])):
                response = self._process_once(request)
        except Exception as exc:
            try:
                self.agent_tasks.save_checkpoint(
                    str(task["id"]),
                    state="task_failed",
                    detail="任务在最终验收前异常停止；既有模型边界与工具证据已保留",
                )
            except Exception:
                pass
            self.agent_tasks.mark(
                str(task["id"]),
                status="failed",
                phase="failed",
                error=str(exc),
            )
            if request_id:
                self.agent_requests.fail(request.session_id, request_id, str(exc))
            raise
        if request_id:
            self.agent_requests.complete(
                request.session_id,
                request_id,
                response.model_dump(mode="json"),
            )
        return response

    # 作用：校验原任务边界并从已保存的任务账本和工具证据中恢复执行。
    # 参数 request：携带 resume_task_id、新 request_id 和原始委托内容的恢复请求。
    # 返回：恢复执行后的聊天响应；不满足安全条件时抛出 AgentTaskResumeError。
    def _resume_task(self, request: ChatRequest) -> ChatResponse:
        task_id = request.resume_task_id.strip()
        task = self.agent_tasks.get(task_id)
        if task is None or task.get("session_id") != request.session_id:
            raise AgentTaskResumeError("要恢复的 Agent 任务不存在或不属于当前会话。")
        request_id = request.request_id.strip()
        if task.get("status") == "completed" and request_id == task.get("request_id"):
            request_hash = self.agent_requests.fingerprint(
                request.model_dump(exclude={"request_id"})
            )
            claim = self.agent_requests.claim(request.session_id, request_id, request_hash)
            if claim.state == "cached" and claim.response is not None:
                return ChatResponse.model_validate(claim.response)
            self.agent_requests.fail(
                request.session_id,
                request_id,
                "任务已完成但响应缓存缺失。",
            )
            raise AgentTaskResumeError("任务已经完成，不能重新执行。")
        if task.get("status") not in {"failed", "degraded", "cancelled", "interrupted"}:
            raise AgentTaskResumeError("这个任务当前不能恢复；它可能已经完成或仍在等待确认。")
        self.tools.registry.reconcile_interrupted_effects(task_id=task_id)
        task = self.agent_tasks.get(task_id) or task
        latest_user = next(
            (message for message in reversed(request.messages) if message.role == "user"),
            None,
        )
        if latest_user is None or latest_user.content.strip() != str(task.get("goal", "")).strip():
            raise AgentTaskResumeError("恢复时委托内容必须保持不变；如果要修改目标，请新建委托。")
        source_request = task.get("request") if isinstance(task.get("request"), dict) else {}
        expected_attachments = list(source_request.get("attachment_ids", []))
        if request.attachment_ids != expected_attachments:
            raise AgentTaskResumeError("恢复任务必须带回原来的全部附件，且不能增加新的附件。")
        if (
            request.chat_mode != source_request.get("chat_mode", request.chat_mode)
            or request.character != source_request.get("character", request.character)
        ):
            raise AgentTaskResumeError("恢复任务必须留在原来的会话模式和角色中。")
        uncertain_steps = [
            step
            for step in task.get("steps", [])
            if isinstance(step, dict)
            and step.get("status") in {"running", "interrupted"}
            and (
                step.get("action") in AgentExecutionPlanner.SIDE_EFFECT_ACTIONS
                or step.get("action") == "capability_call"
            )
        ]
        if uncertain_steps and not request.allow_uncertain_replay:
            labels = "、".join(str(step.get("action", "操作")) for step in uncertain_steps)
            raise AgentTaskResumeError(
                f"任务包含结果不确定的副作用步骤（{labels}）。请先检查外部状态；"
                "只有明确允许重放后才能恢复。"
            )
        if not request_id:
            raise AgentTaskResumeError("恢复任务需要新的 request_id。")
        request_hash = self.agent_requests.fingerprint(
            request.model_dump(exclude={"request_id"})
        )
        claim = self.agent_requests.claim(request.session_id, request_id, request_hash)
        if claim.state == "cached" and claim.response is not None:
            return ChatResponse.model_validate(claim.response)
        resumed = self.agent_tasks.resume(
            task_id,
            session_id=request.session_id,
            request_id=request_id,
            request=request.model_dump(mode="json"),
        )
        if resumed is None:
            self.agent_requests.fail(request.session_id, request_id, "任务状态已变化，不能恢复。")
            raise AgentTaskResumeError("任务状态已经变化，未开始重复执行。")
        try:
            with agent_task_scope(task_id):
                response = self._process_once(request)
        except Exception as exc:
            try:
                self.agent_tasks.save_checkpoint(
                    task_id,
                    state="task_failed",
                    detail="恢复任务再次异常停止；既有模型边界与工具证据仍保留",
                )
            except Exception:
                pass
            self.agent_tasks.mark(
                task_id,
                status="failed",
                phase="failed",
                error=str(exc),
            )
            self.agent_requests.fail(request.session_id, request_id, str(exc))
            raise
        self.agent_requests.complete(
            request.session_id,
            request_id,
            response.model_dump(mode="json"),
        )
        return response

    # 作用：执行一轮完整 Agent 流程，准备上下文、路由、调用工具和模型，并验收最终状态。
    # 参数 request：本轮实际执行的聊天请求；外层已完成任务创建或恢复。
    # 返回：带最终状态、模型回复和全部执行证据的 ChatResponse。
    def _process_once(self, request: ChatRequest) -> ChatResponse:
        latest_user = next(
            (message for message in reversed(request.messages) if message.role == "user"),
            None,
        )
        latest_text = latest_user.content if latest_user else ""
        attachment_records = self.attachments.get_many(
            request.session_id,
            request.attachment_ids,
        )
        public_attachments = [self._public_attachment(item) for item in attachment_records]
        if attachment_records and not latest_text.strip():
            latest_text = "请查看我发送的附件。"
        cancelled_nudges = self.proactive_nudges.on_user_return(request.session_id)
        relationship_observation = self.relationship_tracker.observe(
            request.session_id, latest_text
        )
        instinct_observation = self.behavior_instincts.observe(
            request.session_id,
            latest_text,
        )
        trace = self.traces.start(session_id=request.session_id, user_text=latest_text)
        context_plan = self.context_window.prepare(request.session_id, request.messages)
        route = self.router.decide(
            text=latest_text,
            chat_mode=request.chat_mode,
            selected_character=request.character,
            routing_mode=request.routing_mode,
        )
        if self.character_agents.model is not self.model:
            self.character_agents = CharacterAgentRegistry(self.model)
            self.club_orchestrator = ClubOrchestrator(self.character_agents)
        if self.tools.registry.permanent_memory is not self.permanent_memory:
            self.tools = ToolExecutor(self.memory, self.permanent_memory, self.capabilities)
            self._bind_model_tools()
        character = get_character(route.primary_agent)
        conversation_mode = infer_mode(latest_text)
        safety_level = detect_safety_level(latest_text)
        interaction_observation = self.interaction_outcomes.observe(
            request.session_id,
            latest_text,
            safety_level=safety_level,
        )
        emotion = detect_emotion(latest_text)
        previous_affective_state = self.affective_state.status(request.session_id)
        affective_state = self.affective_state.update(request.session_id, emotion)
        effective_emotion_label = (
            affective_state.label
            if emotion.label == "neutral" and affective_state.needs_support
            else emotion.label
        )
        primary_agent = self.character_agents.get(route.primary_agent)
        intent_interpretation = self.intent_decomposer.interpret(latest_text)
        intent_steps = list(intent_interpretation.steps)
        execution_contract = self.execution_planner.build(
            text=latest_text,
            intent_steps=intent_steps,
            attachments=attachment_records,
        )
        task_id = current_agent_task_id()
        if task_id:
            self.agent_tasks.set_plan(
                task_id,
                plan=list(execution_contract.plan),
                contract=execution_contract.as_dict(),
            )
            self.agent_tasks.set_phase(
                task_id,
                "preparing_context",
                detail="正在读取附件、记忆与可用工具",
            )
        attachment_tool_results = self._process_attachments(
            request.session_id,
            latest_text,
            attachment_records,
            allowed_tools=primary_agent.allowed_tools,
        )
        tool_results = [*attachment_tool_results]
        if intent_interpretation.needs_clarification:
            tool_results.append(
                ToolExecutionResult(
                    action="none",
                    success=False,
                    error=intent_interpretation.clarification_question,
                    audit={
                        "intent_clarification_required": True,
                        "intent_rewrite": intent_interpretation.rewritten_text,
                    },
                )
            )
        tool_results.extend(
            self.tools.execute_parsed(
                request.session_id,
                step,
                allowed_tools=primary_agent.allowed_tools,
            )
            for step in intent_steps
            if not intent_interpretation.needs_clarification
        )
        if not intent_steps and not intent_interpretation.needs_clarification:
            tool_result = self.tools.execute(
                request.session_id,
                latest_text,
                allowed_tools=primary_agent.allowed_tools,
            )
            if tool_result.action != "none":
                tool_results.append(tool_result)
        if attachment_records:
            tool_context = [
                "本轮附件已经由受控工具读取；不要声称看过工具未成功读取的内容。附件："
                + "、".join(
                    f"{item['original_name']}（{item['kind']}，{item['size']} 字节）"
                    for item in attachment_records
                )
            ]
        else:
            tool_context = []
        if intent_interpretation.corrections:
            tool_context.append(
                "用户意图已做轻量规范化（仅供内部使用）："
                + "、".join(intent_interpretation.corrections)
            )
        if intent_interpretation.needs_clarification:
            tool_context.append(
                "用户请求缺少必要参数，必须先澄清，不得猜测："
                + intent_interpretation.clarification_question
            )
        untrusted_context = False
        injection_detected_context = False
        for result in tool_results:
            self.output_security.assess(result, source="pre_model_tool")
            untrusted_context = untrusted_context or (
                result.audit.get("output_trust") == "untrusted"
            )
            injection_detected_context = injection_detected_context or bool(
                result.audit.get("prompt_injection_detected")
            )
            if result.content:
                tool_context.append(self.output_security.render_for_model(result))
            elif result.error:
                confirmation = (
                    f"；operation_id={result.audit.get('operation_id')}"
                    if result.audit.get("requires_confirmation")
                    else ""
                )
                tool_context.append(
                    f"工具 {result.action} 尚未完成："
                    f"{self.output_security.render_for_model(result)}{confirmation}"
                )
        historical_text = "\n".join(
            message.content for message in context_plan.messages[:-1]
        )
        if self.output_security.detect_signals(historical_text):
            untrusted_context = True
            injection_detected_context = True
        contract_requirements = [
            item.as_dict() for item in execution_contract.requirements
        ]
        pre_model_side_effects_complete = (
            self.outcome_verifier.side_effect_requirements_completed(
                contract_requirements,
                tool_results,
            )
        )
        enable_agent_followup = (
            not intent_interpretation.needs_clarification
            and not pre_model_side_effects_complete
            and not any(
            result.audit.get("requires_confirmation")
            or result.action
            in {
                "confirm_file_operation",
                "cancel_file_operation",
                "confirm_capability_operation",
                "cancel_capability_operation",
            }
            for result in tool_results
            )
        )
        self.permanent_memory.record_key_event(
            request.session_id,
            "first_chat",
            "第一次来到侍奉部活动室",
        )
        learned_preferences = self.preference_learner.extract(latest_text)
        persistence_allowed = self.profile_fact_learner.allows_persistence(
            latest_text, safety_level=safety_level
        )
        for preference in learned_preferences if persistence_allowed else []:
            if preference.key == "avoid_address":
                preferred_name = self.permanent_memory.preferences(
                    request.session_id
                ).get("preferred_name", "")
                if preferred_name and preferred_name in preference.value:
                    self.permanent_memory.forget(request.session_id, "preferred_name")
            self.permanent_memory.set_preference(
                request.session_id,
                preference.key,
                preference.value,
            )
        profile_observation = self.profile_fact_learner.observe(
            request.session_id,
            latest_text,
            safety_level=safety_level,
            learned_preferences=learned_preferences,
        )
        knowledge_observation = (
            self.capabilities.execute(
                "knowledge_graph",
                "observe",
                {"session_id": request.session_id, "text": latest_text},
                session_id=request.session_id,
            )
            if persistence_allowed
            else {"ok": True, "result": {"observed": []}}
        )
        reunion_plan = self.reunion_planner.plan(
            self.memory.latest_conversation(request.session_id),
            safety_level=safety_level,
            previous_affective_state=previous_affective_state.as_dict(),
            relationship=relationship_observation,
        )
        permanent_segment = self.permanent_memory.get_prompt_segment(request.session_id)
        memory_hits = self.memory_retriever.search(
            request.session_id,
            latest_text,
            limit=5,
            history=[
                {"role": message.role, "content": message.content}
                for message in request.messages[-6:]
            ],
        )
        memories = [hit.content for hit in memory_hits]
        if permanent_segment != "- 无跨会话永久记忆":
            memories = [permanent_segment, *memories]

        # Knowledge-base retrieval is part of the normal chat path, but only
        # for informational turns. Tool/delegation requests remain governed by
        # the existing execution contract and never inherit document context.
        knowledge_hits: list[dict] = []
        if (
            not intent_steps
            and not intent_interpretation.needs_clarification
            and self.knowledge_base.status().get("documents", 0) > 0
        ):
            knowledge_hits = self.knowledge_base.search_all(latest_text, limit=5)
            for item in knowledge_hits:
                title = str(item.get("title", "")).strip()
                base = str(item.get("knowledge_base", "")).strip()
                source = str(item.get("source", "")).strip()
                label = "知识库资料"
                if base:
                    label += f"[{base}]"
                if title:
                    label += f"《{title}》"
                if source and source != base:
                    label += f" 来源:{source}"
                result = ToolExecutionResult(
                    action="knowledge_search",
                    success=True,
                    content=f"{label}\n{str(item.get('content', '')).strip()}",
                    audit={
                        "output_trust": "untrusted",
                        "knowledge_base": base,
                        "document_id": str(item.get("document_id", "")),
                        "chunk_id": str(item.get("id", "")),
                    },
                )
                self.output_security.assess(result, source="knowledge_base")
                tool_context.append(self.output_security.render_for_model(result))
                untrusted_context = True
                injection_detected_context = injection_detected_context or bool(
                    result.audit.get("prompt_injection_detected")
                )
        spontaneous_recalls = self.spontaneous_recall.collect(
            session_id=request.session_id,
            text=latest_text,
            emotion_label=emotion.label,
            memory=self.memory,
            permanent_memory=self.permanent_memory,
        )
        recall_context = self.spontaneous_recall.format_for_prompt(spontaneous_recalls)
        for recall_text in recall_context:
            recall_result = ToolExecutionResult(
                action="recall",
                success=True,
                content=recall_text,
            )
            self.output_security.assess(recall_result, source="spontaneous_memory")
            tool_context.append(self.output_security.render_for_model(recall_result))
            untrusted_context = True
            injection_detected_context = injection_detected_context or bool(
                recall_result.audit.get("prompt_injection_detected")
            )
        for recall in spontaneous_recalls:
            if recall.content not in memories:
                memories.append(recall.content)
        secured_memories: list[str] = []
        for memory_text in memories:
            memory_result = ToolExecutionResult(
                action="recall",
                success=True,
                content=memory_text,
            )
            self.output_security.assess(memory_result, source="memory_retrieval")
            secured_memories.append(self.output_security.render_for_model(memory_result))
            untrusted_context = True
            injection_detected_context = injection_detected_context or bool(
                memory_result.audit.get("prompt_injection_detected")
            )
        self.mental_state.record(
            request.session_id,
            emotion.label,
            intensity=emotion.intensity,
        )
        mental_state = self.mental_state_status(request.session_id)
        proactive_care = self.proactive_care_planner.plan(
            mental_state=mental_state,
            safety_level=safety_level,
            latest_text=latest_text,
        )
        relationship = self.relationship_tracker.status(
            session_id=request.session_id,
            mental_state=mental_state,
        )
        response_policy = self.response_policy_planner.plan(
            emotion_label=effective_emotion_label,
            safety_level=safety_level,
            conversation_mode=conversation_mode,
            relationship=relationship,
            proactive_care=[item.as_dict() for item in proactive_care],
        )
        tool_context.extend(self.proactive_care_planner.format_for_prompt(proactive_care))
        tool_context.append(self.affective_state.format_for_prompt(affective_state))
        tool_context.append(self.relationship_tracker.format_for_prompt(relationship))
        profile_prompt = self.profile_fact_learner.format_for_prompt(request.session_id)
        if profile_prompt:
            profile_result = ToolExecutionResult(
                action="recall",
                success=True,
                content=profile_prompt,
            )
            self.output_security.assess(profile_result, source="profile_memory")
            tool_context.append(self.output_security.render_for_model(profile_result))
            untrusted_context = True
            injection_detected_context = injection_detected_context or bool(
                profile_result.audit.get("prompt_injection_detected")
            )
        quality_lessons = self.response_quality_guard.lessons_for_prompt(
            request.session_id, route.primary_agent
        )
        if quality_lessons:
            tool_context.append(quality_lessons)
        outcome_prompt, active_outcome_lessons = self.interaction_outcomes.build_prompt(
            request.session_id,
            route.primary_agent,
        )
        if outcome_prompt:
            tool_context.append(outcome_prompt)
        tool_context.append(self.response_policy_planner.format_for_prompt(response_policy))
        instinct_prompt, active_instincts = self.behavior_instincts.build_prompt(
            request.session_id
        )
        if instinct_prompt:
            tool_context.append(instinct_prompt)
        reunion_context = self.reunion_planner.format_for_prompt(reunion_plan)
        if reunion_context:
            tool_context.append(reunion_context)
        self_model_prompt_segments = [
            self.self_model.format_for_prompt(request.session_id, agent_id)
            for agent_id in dict.fromkeys(route.active_agents)
        ]
        tool_context.extend(segment for segment in self_model_prompt_segments if segment)
        recent_reflections = self.memory.list_reflections(request.session_id, limit=3)
        for item in reversed(recent_reflections):
            reflection_result = ToolExecutionResult(
                action="recall",
                success=True,
                content=f"近期对话反思：{item['summary']}",
            )
            self.output_security.assess(
                reflection_result,
                source="conversation_reflection",
            )
            tool_context.append(
                self.output_security.render_for_model(reflection_result)
            )
            untrusted_context = True
            injection_detected_context = injection_detected_context or bool(
                reflection_result.audit.get("prompt_injection_detected")
            )
        compressed_context = self.context_window.format_summary(context_plan)
        if compressed_context:
            summary_result = ToolExecutionResult(
                action="recall",
                success=True,
                content=compressed_context,
            )
            self.output_security.assess(summary_result, source="history_summary")
            tool_context.append(self.output_security.render_for_model(summary_result))
            untrusted_context = True
            injection_detected_context = injection_detected_context or bool(
                summary_result.audit.get("prompt_injection_detected")
            )
        if task_id and request.resume_task_id:
            recovery_context = self.agent_tasks.resume_context(task_id)
            if recovery_context:
                tool_context.append(recovery_context)
                self.agent_tasks.append_event(
                    task_id,
                    "checkpoint_context_prepared",
                    status="completed",
                    phase="resuming",
                    detail="已根据结构检查点与工具账本生成安全恢复上下文",
                )
        file_access = self.tools.registry.file_access_status()
        if file_access:
            tool_context.append(
                "Agent 文件范围（内部信息，不要逐字复述）："
                f"内部工作区={file_access.get('workspace_root')}（可读写）；"
                f"项目源码={file_access.get('project_root')}（默认只读）；"
                f"已授权写入目录={','.join(file_access.get('write_roots', []))}。"
                "相对路径以内部工作区为基准；访问项目或桌面时使用这里列出的绝对路径。"
            )
        effective_active_agents = [route.primary_agent]
        club_orchestration: dict[str, object] = {}
        turn_model_runtime: dict[str, object] = {}
        if task_id and injection_detected_context:
            self.agent_tasks.append_event(
                task_id,
                "untrusted_context_isolated",
                status="blocked",
                phase="security_review",
                detail=(
                    "外部工具或记忆内容含疑似提示注入；正文已作为数据隔离，"
                    "本轮模型发起的副作用已冻结"
                ),
                payload={"side_effects_frozen": True},
            )
        if task_id:
            self.agent_tasks.set_phase(
                task_id,
                "model_waiting",
                detail="上下文已准备完成，正在等待模型分析与决策",
            )
        if request.chat_mode == "club" and len(route.active_agents) > 1:
            club_reply = self.club_orchestrator.reply(
                primary_agent=route.primary_agent,
                active_agents=route.active_agents,
                latest_text=latest_text,
                messages=context_plan.messages,
                conversation_mode=conversation_mode,
                safety_level=safety_level,
                chat_mode=request.chat_mode,
                emotion_label=effective_emotion_label,
                memories=secured_memories,
                tool_context=tool_context,
                session_id=request.session_id,
                enable_agent_tools=enable_agent_followup,
                authorization_requirements=tuple(
                    item.as_dict() for item in execution_contract.requirements
                ),
                untrusted_context=untrusted_context,
                injection_detected_context=injection_detected_context,
            )
            raw_content = club_reply.content
            degraded = club_reply.degraded
            degradation_reason = club_reply.degradation_reason
            effective_active_agents = [reply.agent_id for reply in club_reply.replies]
            club_orchestration = club_reply.orchestration_trace()
            turn_model_runtime = {
                "mode": "club",
                "speakers": list(club_orchestration.get("model_runtime", [])),
            }
            primary_reply = next(
                (reply for reply in club_reply.replies if reply.agent_id == route.primary_agent),
                None,
            )
            primary_degraded = primary_reply.degraded if primary_reply else degraded
        else:
            agent_reply = self.character_agents.reply(
                agent_id=route.primary_agent,
                messages=context_plan.messages,
                conversation_mode=conversation_mode,
                safety_level=safety_level,
                chat_mode=request.chat_mode,
                emotion_label=effective_emotion_label,
                memories=secured_memories,
                tool_context=tool_context,
                session_id=request.session_id,
                enable_agent_tools=enable_agent_followup,
                authorization_requirements=tuple(
                    item.as_dict() for item in execution_contract.requirements
                ),
                untrusted_context=untrusted_context,
                injection_detected_context=injection_detected_context,
            )
            raw_content = agent_reply.content
            degraded = agent_reply.degraded
            degradation_reason = agent_reply.degradation_reason
            primary_degraded = agent_reply.degraded
            turn_model_runtime = dict(agent_reply.model_runtime)
        native_tool_results = (
            [result for reply in club_reply.replies for result in reply.tool_results]
            if request.chat_mode == "club" and len(route.active_agents) > 1
            else list(agent_reply.tool_results)
        )
        pending_operation = next(
            (
                result
                for result in [*tool_results, *native_tool_results]
                if result.audit.get("requires_confirmation")
            ),
            None,
        )
        if pending_operation is not None:
            operation_id = pending_operation.audit.get("operation_id", "")
            if pending_operation.audit.get("capability"):
                capability = pending_operation.audit.get("capability", "外部能力")
                operation = pending_operation.audit.get("operation", "call")
                raw_content = (
                    f"已经准备好 `{capability}.{operation}`，但还没有执行。"
                    "这项操作会访问外部服务或改变数据，需要你点击下面的“确认执行”。"
                    f"\n\n确认编号：`{operation_id}`"
                )
            else:
                pending_path = pending_operation.audit.get("path", "这个文件")
                raw_content = (
                    f"已经准备好对 `{pending_path}` 的修改，但还没有写入。"
                    "这是已有文件，需要你点击下面的“确认写入”后才会真正落盘。"
                    f"\n\n确认编号：`{operation_id}`"
                )
        if context_plan.degraded:
            degraded = True
            degradation_reason = ";".join(
                dict.fromkeys(
                    reason
                    for reason in (
                        degradation_reason,
                        context_plan.degradation_reason,
                    )
                    if reason
                )
            )
        budget_result = next(
            (
                result
                for result in [*tool_results, *native_tool_results]
                if result.audit.get("budget_exceeded")
            ),
            None,
        )
        if budget_result is not None:
            degraded = True
            primary_degraded = True
            degradation_reason = ";".join(
                dict.fromkeys(
                    reason
                    for reason in (degradation_reason, "task_budget_exceeded")
                    if reason
                )
            )
            raw_content = budget_result.error or "任务资源预算已经用完。"
            dsml_results = []
        else:
            native_injection_detected = any(
                result.audit.get("prompt_injection_detected")
                for result in native_tool_results
            )
            dsml_results = self._execute_model_tool_calls(
                raw_content,
                request.session_id,
                primary_agent.allowed_tools,
                authorization_requirements=tuple(
                    item.as_dict() for item in execution_contract.requirements
                ),
                injection_detected=(
                    injection_detected_context or native_injection_detected
                ),
                block_side_effects=pre_model_side_effects_complete,
            )
        all_tool_results = [*tool_results, *native_tool_results, *dsml_results]
        if task_id:
            self.agent_tasks.set_phase(
                task_id,
                "verifying",
                detail="正在核对回答、工具结果与完成条件",
            )
        successful_local_tools = [
            result
            for result in all_tool_results
            if result.success and result.content.strip()
        ]
        if primary_degraded and successful_local_tools:
            raw_content = present_degraded_tool_results(
                character.short_name,
                successful_local_tools,
                degradation_reason=degradation_reason,
            )
        content = clean_reply(ToolCallExtractor().strip_text(raw_content))
        content, quality_audit = self.response_quality_guard.enforce(
            content,
            session_id=request.session_id,
            character=character,
            policy=response_policy,
            chat_mode=request.chat_mode,
            active_agents=list(effective_active_agents),
            relationship=relationship,
            preferences=self.permanent_memory.preferences(request.session_id),
            emotion_label=emotion.label,
        )
        content, policy_audit = self.response_policy_enforcer.enforce(
            content,
            response_policy,
        )
        task_id = current_agent_task_id()
        cancelled = bool(task_id and self.agent_tasks.cancel_requested(task_id))
        outcome = self.outcome_verifier.verify(
            content=content,
            tool_results=all_tool_results,
            degraded=degraded,
            degradation_reason=degradation_reason,
            cancelled=cancelled,
            requirements=[
                item.as_dict() for item in execution_contract.requirements
            ],
        )
        cancelled_content = (
            "这次委托已经停止，我不会继续调用后续工具。尚未执行的排队操作已经取消；"
            "停止前已经完成的操作不会自动撤销，请根据下方步骤记录检查，文件修改可使用回滚。"
        )
        if outcome.status == "cancelled":
            content = cancelled_content
            degraded = True
            degradation_reason = ";".join(
                dict.fromkeys(reason for reason in (degradation_reason, "task_cancelled") if reason)
            )
            self.tools.registry.operations.cancel_by_task(task_id)
        elif outcome.checks.get("missing_evidence"):
            missing_labels = "、".join(
                str(item.get("label", "必要工具证据"))
                for item in outcome.checks["missing_evidence"]
                if isinstance(item, dict)
            )
            content = (
                "这次委托尚未完成。Agent 验收没有取得"
                f"{missing_labels or '完成委托所需的工具证据'}，"
                "因此不会把缺少真实执行证据的回答标记为完成。请检查下方工具状态后重试。"
            )
        elif bool(outcome.checks.get("completion_claim_guarded")):
            content += "\n\n执行验收没有通过，以上操作不能视为已经完成。"
        task_phase = {
            "completed": "verified",
            "waiting_confirmation": "waiting_confirmation",
            "waiting_background": "background_queued",
            "failed": "verification_failed",
            "degraded": "degraded",
            "cancelled": "cancelled",
        }[outcome.status]
        if "task_budget_exceeded" in degradation_reason:
            task_phase = "budget_exceeded"
        if task_id:
            task = self.agent_tasks.mark(
                task_id,
                status=outcome.status,
                phase=task_phase,
                outcome=outcome.as_dict(),
                error=(outcome.summary if outcome.status == "failed" else ""),
            )
            if (
                task is not None
                and task.get("cancel_requested")
                and outcome.status != "cancelled"
            ):
                outcome = self.outcome_verifier.verify(
                    content=content,
                    tool_results=all_tool_results,
                    degraded=True,
                    degradation_reason="task_cancelled",
                    cancelled=True,
                    requirements=[
                        item.as_dict() for item in execution_contract.requirements
                    ],
                )
                content = cancelled_content
                degraded = True
                degradation_reason = ";".join(
                    dict.fromkeys(
                        reason
                        for reason in (degradation_reason, "task_cancelled")
                        if reason
                    )
                )
                task_phase = "cancelled"
                self.tools.registry.operations.cancel_by_task(task_id)
                task = self.agent_tasks.mark(
                    task_id,
                    status="cancelled",
                    phase=task_phase,
                    outcome=outcome.as_dict(),
                )
            final_checkpoint = self.agent_tasks.save_checkpoint(
                task_id,
                state=f"outcome_{task_phase}",
                detail=f"任务已完成最终验收：{task_phase}",
            )
            self.agent_tasks.append_event(
                task_id,
                "task_checkpoint_committed",
                status=outcome.status,
                phase=task_phase,
                detail=(
                    f"任务状态与恢复点 #{final_checkpoint.get('revision', 0)} 已持久化"
                ),
                payload={
                    "checkpoint_revision": int(final_checkpoint.get("revision", 0)),
                    "checkpoint_state": str(final_checkpoint.get("state", "")),
                },
            )
            task = self.agent_tasks.get(task_id) or task
        else:
            task = None
            final_checkpoint = {}
        execution = {
            **outcome.as_dict(),
            "task_id": task_id,
            "phase": task_phase,
            "plan": list(task.get("plan", [])) if task else [],
            "contract": dict(task.get("contract", {})) if task else {},
            "steps": list(task.get("steps", [])) if task else [],
            "events": self.agent_tasks.events(task_id, limit=100) if task_id else [],
            "budget": self.agent_tasks.budget_status(task_id) if task_id else {},
            "checkpoint": final_checkpoint,
            "cancel_requested": bool(task.get("cancel_requested")) if task else False,
            "retryable": outcome.status in {"failed", "degraded", "cancelled", "interrupted"},
            "knowledge_retrieval": [
                {
                    "knowledge_base": str(item.get("knowledge_base", "")),
                    "document_id": str(item.get("document_id", "")),
                    "chunk_id": str(item.get("id", "")),
                    "title": str(item.get("title", "")),
                    "source": str(item.get("source", item.get("source_uri", ""))),
                    "score": float(item.get("score", 0.0)),
                }
                for item in knowledge_hits
            ],
        }
        sticker_url = (
            pick_sticker(self.sticker_base, character, emotion.label)
            if request.enable_sticker
            else None
        )
        audio_url = (
            synthesize_tts_optional(content, character, emotion.label)
            if request.enable_voice
            else None
        )
        self.memory.save_conversation(
            session_id=request.session_id,
            character=route.primary_agent,
            chat_mode=request.chat_mode,
            user_message=latest_text,
            assistant_reply=content,
            emotion=emotion.label,
            tool_results=[result.model_dump(mode="json") for result in all_tool_results],
            attachments=public_attachments,
            trace_id=trace.trace_id,
            degraded=degraded,
            degradation_reason=degradation_reason,
            execution=execution,
        )
        self.conversations.record_turn(
            conversation_id=request.session_id,
            chat_mode=request.chat_mode,
            character=route.primary_agent,
            user_message=latest_text,
            assistant_reply=content,
        )
        strategy_recorded = self.interaction_outcomes.record_strategy(
            session_id=request.session_id,
            character=route.primary_agent,
            content=content,
            policy=response_policy,
        )
        reflection = self.reflection_engine.create(
            session_id=request.session_id,
            user_text=latest_text,
            assistant_reply=content,
            emotion=emotion.label,
            character=route.primary_agent,
            chat_mode=request.chat_mode,
        )
        self.memory.save_reflection(
            session_id=request.session_id,
            summary=reflection.summary,
            emotion=reflection.emotion,
            character=reflection.character,
            chat_mode=reflection.chat_mode,
            salience=reflection.salience,
        )
        memory_consolidation = self.memory_consolidator.consolidate(
            reflection=reflection,
            permanent_memory=self.permanent_memory,
            safety_level=safety_level,
        )
        scheduled_nudge = self.proactive_nudges.schedule_after_turn(
            session_id=request.session_id,
            mental_state=mental_state,
            safety_level=safety_level,
            character_name=character.short_name,
            relationship=relationship,
        )
        self_model_participants = [
            self.self_model.refresh(
                session_id=request.session_id,
                character=agent_id,
                relationship=relationship,
                active_instincts=active_instincts,
                safety_level=safety_level,
            )
            for agent_id in dict.fromkeys(effective_active_agents)
        ]
        primary_self_model = next(
            (
                item
                for item in self_model_participants
                if item["character"] == route.primary_agent
            ),
            self.self_model.current(request.session_id, route.primary_agent),
        )
        self_model_state = {
            "primary": primary_self_model,
            "participants": self_model_participants,
        }
        companion_state = {
            "session_id": request.session_id,
            "mental_state": mental_state,
            "relationship": relationship,
            "relationship_observation": relationship_observation,
            "last_reflection": reflection.as_dict(),
            "memory_consolidation": memory_consolidation.as_dict(),
            "reunion": reunion_plan.as_dict(),
            "proactive_nudge": {
                "cancelled_on_return": cancelled_nudges,
                "scheduled": scheduled_nudge,
            },
            "behavior_instincts": {
                "observation": instinct_observation,
                "active": active_instincts,
            },
            "profile_observation": profile_observation,
            "knowledge_observation": knowledge_observation,
            "profile_facts": self.profile_fact_learner.current(request.session_id),
            "quality_audit": quality_audit.as_dict(),
            "interaction_learning": {
                "observation": interaction_observation,
                "active_lessons": active_outcome_lessons,
                "strategy_recorded": strategy_recorded,
            },
            "self_model": self_model_state,
            "club_orchestration": club_orchestration,
            "context_window": context_plan.as_dict(),
            "execution": execution,
        }
        trace.route = {
            "primary_agent": route.primary_agent,
            "active_agents": list(effective_active_agents),
            "candidate_agents": list(route.active_agents),
            "mode": route.mode,
            "reasoning": route.reasoning,
            "scorecard": route.scorecard,
        }
        trace.emotion = emotion.label
        trace.safety_level = safety_level
        trace.tool_results = [result.model_dump() for result in all_tool_results]
        trace.memories_used = list(memories)
        trace.memory_retrieval = [hit.as_dict() for hit in memory_hits]
        trace.knowledge_retrieval = [
            {
                "knowledge_base": str(item.get("knowledge_base", "")),
                "document_id": str(item.get("document_id", "")),
                "chunk_id": str(item.get("id", "")),
                "title": str(item.get("title", "")),
                "source": str(item.get("source", item.get("source_uri", ""))),
                "score": float(item.get("score", 0.0)),
            }
            for item in knowledge_hits
        ]
        trace.intent_plan = [step.as_trace() for step in intent_steps]
        trace.intent_plan.append(
            {
                "action": "interpretation",
                "argument": intent_interpretation.rewritten_text,
                "source_text": intent_interpretation.original_text,
            }
        )
        trace.spontaneous_recalls = [recall.as_dict() for recall in spontaneous_recalls]
        trace.proactive_care = [item.as_dict() for item in proactive_care]
        trace.companion_state = companion_state
        trace.response_policy = response_policy.as_dict()
        trace.policy_audit = policy_audit
        trace.quality_audit = quality_audit.as_dict()
        trace.interaction_learning = companion_state["interaction_learning"]
        trace.self_model = self_model_state
        trace.club_orchestration = club_orchestration
        trace.context_window = context_plan.as_dict()
        trace.model_runtime = turn_model_runtime
        finished_trace = self.traces.finish(
            trace.trace_id,
            degraded=degraded,
            degradation_reason=degradation_reason,
        )
        self.degradation_monitor.record_turn(
            success=not degraded,
            latency_ms=finished_trace.latency_ms if finished_trace else 0,
        )
        self.route_feedback.record(
            route.primary_agent,
            success=not primary_degraded,
            latency_ms=finished_trace.latency_ms if finished_trace else 0,
        )
        return ChatResponse(
            content=content,
            conversation_mode=conversation_mode,
            chat_mode=request.chat_mode,
            character=route.primary_agent,
            emotion=emotion.label,
            safety_level=safety_level,
            degraded=degraded,
            degradation_reason=degradation_reason,
            tool_results=all_tool_results,
            memories_used=memories,
            memory_retrieval=[hit.as_dict() for hit in memory_hits],
            knowledge_retrieval=knowledge_hits,
            trace_id=trace.trace_id,
            route_reasoning=route.reasoning,
            route_scorecard=route.scorecard,
            active_agents=effective_active_agents,
            sticker_url=sticker_url,
            audio_url=audio_url,
            intent_plan=[
                *[step.as_trace() for step in intent_steps],
                {
                    "action": "interpretation",
                    "argument": intent_interpretation.rewritten_text,
                    "source_text": intent_interpretation.original_text,
                },
            ],
            spontaneous_recalls=[recall.as_dict() for recall in spontaneous_recalls],
            proactive_care=[item.as_dict() for item in proactive_care],
            companion_state=companion_state,
            response_policy=response_policy.as_dict(),
            policy_audit=policy_audit,
            quality_audit=quality_audit.as_dict(),
            interaction_learning=companion_state["interaction_learning"],
            self_model=self_model_state,
            club_orchestration=club_orchestration,
            context_window=context_plan.as_dict(),
            model_runtime=turn_model_runtime,
            execution=execution,
        )

    # 作用：解析兼容模型输出中的 DSML 工具调用，并在契约授权后执行这些调用。
    # 参数 content：可能包含 DSML 工具标记的模型文本。
    # 参数 session_id：工具执行归属的会话标识。
    # 参数 allowed_tools：当前角色允许使用的工具名称集合。
    # 参数 authorization_requirements：由用户委托生成的动作授权与证据要求。
    # 参数 injection_detected：是否已经在不可信上下文中检测到提示注入。
    # 参数 block_side_effects：是否因前置步骤已完成而禁止重复副作用。
    # 返回：本次兼容工具调用产生的 ToolExecutionResult 列表。
    def _execute_model_tool_calls(
        self,
        content: str,
        session_id: str,
        allowed_tools: tuple,
        *,
        authorization_requirements: tuple[dict[str, object], ...] | None = None,
        injection_detected: bool = False,
        block_side_effects: bool = False,
    ) -> list:
        extractor = ToolCallExtractor(allowed_tools=set(allowed_tools))
        results = []
        for call in extractor.extract_text(content):
            if block_side_effects and self.output_security.is_side_effecting(
                call.name,
                call.arguments,
            ):
                result = ToolExecutionResult(
                    action=call.name,  # type: ignore[arg-type]
                    success=True,
                    content="本轮副作用已经由确定性步骤完成，未重复执行。",
                    audit={
                        "deduplicated": True,
                        "side_effect_already_satisfied": True,
                        "authorization_reason": "pre_model_evidence_complete",
                    },
                )
                result.audit.update(
                    {
                        "source": "model_dsml",
                        "tool_call_id": call.id,
                        "repaired": call.repaired,
                    }
                )
                self.output_security.assess(result, source="model_dsml")
                results.append(result)
                continue
            authorized, reason = self.output_security.authorize_model_action(
                call.name,
                call.arguments,
                authorization_requirements,
                injection_detected=injection_detected,
            )
            if authorized:
                result = self.tools.registry.execute(
                    call.name,  # type: ignore[arg-type]
                    call.arguments,
                    session_id=session_id,
                    allowed_tools=allowed_tools,
                )
            else:
                result = ToolExecutionResult(
                    action=call.name,  # type: ignore[arg-type]
                    success=False,
                    error=(
                        "检测到不可信内容中的提示注入，本轮后续副作用已经冻结；"
                        "请由用户在新的消息中明确重新授权。"
                        if reason == "prompt_injection_freeze"
                        else "这个副作用不在用户本轮明确委托的执行契约中，已阻止执行。"
                    ),
                    audit={
                        "tool_output_security_blocked": True,
                        "authorization_reason": reason,
                        "requires_user_reauthorization": True,
                        "prompt_injection_detected": injection_detected,
                    },
                )
            result.audit.update(
                {
                    "source": "model_dsml",
                    "tool_call_id": call.id,
                    "repaired": call.repaired,
                }
            )
            self.output_security.assess(result, source="model_dsml")
            results.append(result)
        return results

    # 作用：将统一工具注册表和输出安全器注入当前模型运行时。
    # 参数：无。
    def _bind_model_tools(self) -> None:
        binder = getattr(self.model, "bind_tool_registry", None)
        if callable(binder):
            binder(self.tools.registry)
        security_binder = getattr(self.model, "bind_tool_output_security", None)
        if callable(security_binder):
            security_binder(self.output_security)

    # 作用：在并发门控保护下返回整个 Agent 系统的运行状态。
    # 参数：无。
    # 返回：所有核心组件的健康状态与运行统计。
    def status(self) -> dict:
        with self.runtime_gate.turn():
            return self._status()

    # 作用：返回关键执行安全约束是否启用，供健康检查和运维页面展示。
    # 参数：无。
    # 返回：执行守卫能力及其启用状态。
    @staticmethod
    # 作用：执行“execution_guard_status”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def execution_guard_status() -> dict[str, object]:
        return {
            "ok": True,
            "pre_model_side_effect_completion_closes_tool_mode": True,
            "unsolicited_model_tool_calls_blocked": True,
            "verified_effects_survive_response_degradation": True,
            "grounded_code_analysis_fallback": True,
        }

    # 作用：汇总模型、记忆、任务、能力与安全组件的内部状态快照。
    # 参数：无。
    # 返回：不经过外层并发门控的内部状态字典。
    def _status(self) -> dict:
        return {
            "agents": self.character_agents.manifest(),
            "tools": self.tools.registry.manifest(),
            "traces": self.traces.status(),
            "degradation": self.degradation_monitor.status(),
            "route_feedback": self.route_feedback.status(),
            "mental_state": self.mental_state.global_status(),
            "permanent_memory": self.permanent_memory.status(),
            "proactive_nudges": self.proactive_nudges.status(),
            "behavior_instincts": self.behavior_instincts.status(),
            "context_window": self.context_window.status(),
            "memory_retrieval": self.memory_retriever.status(),
            "event_stream": self.event_stream.status(),
            "knowledge_base": self.knowledge_base.status(),
            "profile_facts": self.profile_fact_learner.status(),
            "response_quality": self.response_quality_guard.status(),
            "interaction_learning": self.interaction_outcomes.status(),
            "self_model": self.self_model.status(),
            "club_orchestration": self.club_orchestrator.status(),
            "capabilities": {
                "count": len(self.capabilities.manifest()),
                "ready": sum(
                    item["state"] == "ready" for item in self.capabilities.manifest()
                ),
            },
            "usage": self.usage.summary(days=1),
            "agent_recovery": self.agent_recovery,
            "network_resilience": self.capabilities.network.status(),
            "model_runtime": (
                self.model.status()
                if callable(getattr(self.model, "status", None))
                else {"ok": True, "available": False}
            ),
            "runtime_concurrency": self.runtime_gate.status(),
            "background_jobs": self.background_jobs.status(),
            "task_events": self.agent_tasks.event_status(),
            "task_budget": self.agent_tasks.budget_configuration_status(),
            "task_checkpoints": self.agent_tasks.checkpoint_status(),
            "effect_receipts": self.tools.registry.effect_receipts.status(),
            "external_dispatches": self.tools.registry.external_dispatches.status(),
            "python_sandbox": self.capabilities.python.status(),
            "capability_boundaries": self.capabilities.boundary_security_status(),
            "tool_output_security": self.output_security.status(),
            "agent_execution_guards": self.execution_guard_status(),
        }

    # 作用：综合多层记忆和行为偏好，生成指定会话的用户画像。
    # 参数 session_id：需要生成画像的会话标识。
    # 返回：事实、偏好和行为倾向组成的用户画像。
    def user_profile(self, session_id: str) -> dict[str, object]:
        return self.profile_synthesizer.synthesize(
            session_id=session_id,
            memory=self.memory,
            permanent_memory=self.permanent_memory,
            behavior_instincts=self.behavior_instincts.active(session_id),
        )

    # 作用：清除指定会话关联的记忆、任务、附件、授权和外部执行记录。
    # 参数 session_id：需要彻底清理数据的会话标识。
    # 返回：各类数据实际删除数量的统计字典。
    def clear_memory(self, session_id: str) -> dict[str, int]:
        attachment_count = self.attachments.delete_session(session_id)
        local_count = self.memory.clear(session_id)
        permanent_count = self.permanent_memory.clear(session_id)
        agent_task_count = self.agent_tasks.delete_session(session_id)
        agent_operation_count = self.tools.registry.operations.delete_session(session_id)
        effect_receipt_count = self.tools.registry.effect_receipts.delete_session(session_id)
        external_dispatch_count = self.tools.registry.external_dispatches.delete_session(
            session_id
        )
        agent_request_count = self.agent_requests.delete_session(session_id)
        background_job_count = self.background_jobs.delete_session(session_id)
        capability_grant_count = self.capabilities.permission_policy.delete_session(
            session_id
        )
        knowledge_graph_count = self.capabilities.knowledge_graph.delete_session(session_id)
        return {
            "local_count": local_count,
            "permanent_count": permanent_count,
            "agent_task_count": agent_task_count,
            "agent_operation_count": agent_operation_count,
            "effect_receipt_count": effect_receipt_count,
            "external_dispatch_count": external_dispatch_count,
            "agent_request_count": agent_request_count,
            "background_job_count": background_job_count,
            "capability_grant_count": capability_grant_count,
            "knowledge_graph_count": knowledge_graph_count,
            "attachment_count": attachment_count,
        }

    # 作用：从内部附件记录中挑选可以安全返回给客户端的公开字段。
    # 参数 item：包含存储路径等内部信息的完整附件记录。
    # 返回：仅包含附件标识、名称、类型、大小和摘要等安全字段的字典。
    @staticmethod
    # 作用：执行“public_attachment”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 item：调用方传入的item，用于本次处理。
    def _public_attachment(item: dict[str, object]) -> dict[str, object]:
        return {
            key: item[key]
            for key in (
                "id",
                "original_name",
                "mime_type",
                "kind",
                "size",
                "sha256",
                "created_at",
            )
            if key in item
        }

    # 作用：按附件类型选择受控读取或分析工具，并把附件身份写入执行审计。
    # 参数 session_id：附件及工具调用归属的会话标识。
    # 参数 prompt：用户针对附件提出的问题，媒体分析时作为提示使用。
    # 参数 attachments：已经通过附件存储校验的内部附件记录列表。
    # 参数 allowed_tools：当前主角色允许使用的工具集合。
    # 返回：每个附件对应的受控工具执行结果列表。
    def _process_attachments(
        self,
        session_id: str,
        prompt: str,
        attachments: list[dict[str, object]],
        *,
        allowed_tools: tuple,
    ) -> list:
        results = []
        for item in attachments:
            path = str(item["relative_path"])
            kind = str(item["kind"])
            if kind == "code":
                result = self.tools.registry.execute(
                    "analyze_file",
                    {"path": path},
                    session_id=session_id,
                    allowed_tools=allowed_tools,
                )
            elif kind == "text":
                result = self.tools.registry.execute(
                    "read_file",
                    {"path": path},
                    session_id=session_id,
                    allowed_tools=allowed_tools,
                )
            elif kind == "document":
                result = self.tools.registry.execute(
                    "read_document",
                    {"path": path},
                    session_id=session_id,
                    allowed_tools=allowed_tools,
                )
            else:
                result = self.tools.registry.execute(
                    "capability_call",
                    {
                        "capability": "media",
                        "action": "analyze",
                        "arguments": {
                            "path": path,
                            "prompt": prompt or "请客观描述这张图片，并回答用户的附件问题。",
                        },
                    },
                    session_id=session_id,
                    allowed_tools=allowed_tools,
                )
            result.audit.update(
                {
                    "attachment_id": item["id"],
                    "attachment_name": item["original_name"],
                    "attachment_kind": kind,
                }
            )
            results.append(result)
        return results

    # 作用：汇总指定会话的检索结果、长期记忆、画像及相关派生状态。
    # 参数 session_id：需要查看记忆状态的会话标识。
    # 参数 query：可选检索词；为空时主要返回近期和全局记忆状态。
    # 返回：供调试和展示使用的完整记忆视图。
    def memory_state(self, session_id: str, query: str = "") -> dict[str, object]:
        recall_query = query.strip()
        memory_hits = self.memory_retriever.search(session_id, recall_query, limit=20)
        explicit_memories = [hit.content for hit in memory_hits]
        permanent_entries = self.permanent_memory.entries_for_prompt(session_id)
        return {
            "session_id": session_id,
            "explicit_memories": explicit_memories,
            "memory_retrieval": [hit.as_dict() for hit in memory_hits],
            "recent_conversations": self.memory.recent_conversations(session_id, limit=10),
            "reminders": self.memory.list_reminders(session_id, limit=20),
            "reflections": self.memory.list_reflections(session_id, limit=10),
            "permanent_memory": {
                "entries": permanent_entries,
                "items": self.permanent_memory.list_entries(session_id),
                "preferences": self.permanent_memory.preferences(session_id),
                "prompt_segment": self.permanent_memory.get_prompt_segment(session_id),
            },
            "user_profile": self.user_profile(session_id),
            "emotion_turns": self.memory.list_emotion_turns(session_id, limit=20),
            "proactive_nudges": self.memory.list_nudges(session_id, limit=20),
            "affective_state": self.affective_state.status(session_id).as_dict(),
            "behavior_instincts": self.behavior_instincts.list_all(session_id),
            "profile_facts": self.profile_fact_learner.list_all(session_id),
            "profile_stats": self.memory.get_profile_interaction_stats(session_id),
            "quality_cases": self.memory.list_quality_cases(session_id, limit=20),
            "interaction_learning": self.interaction_outcomes.list_all(session_id),
            "self_model": self.self_model.history(session_id),
            "context_checkpoint": self.context_window.checkpoint(session_id),
            "reunion": self.reunion_planner.plan(
                self.memory.latest_conversation(session_id),
                safety_level="normal",
                relationship=self.relationship_tracker.status(session_id=session_id),
            ).as_dict(),
        }

    # 作用：汇总情绪、关系、响应策略和主动关怀信息，形成长期陪伴状态视图。
    # 参数 session_id：需要查看陪伴状态的会话标识。
    # 返回：心理、关系、策略、画像和主动关怀组成的状态字典。
    def companion_state(self, session_id: str) -> dict[str, object]:
        mental_state = self.mental_state_status(session_id)
        affective_state = mental_state["affective_state"]
        relationship = self.relationship_tracker.status(
            session_id=session_id,
            mental_state=mental_state,
        )
        policy = self.response_policy_planner.plan(
            emotion_label=(
                str(affective_state["label"])
                if affective_state["needs_support"] is True
                else str(mental_state["latest_emotion"])
            ),
            safety_level="normal",
            conversation_mode="daily",
            relationship=relationship,
            proactive_care=[],
        )
        return {
            "session_id": session_id,
            "mental_state": mental_state,
            "relationship": relationship,
            "response_policy": policy.as_dict(),
            "recent_reflections": self.memory.list_reflections(session_id, limit=5),
            "user_profile": self.user_profile(session_id),
            "reunion": self.reunion_planner.plan(
                self.memory.latest_conversation(session_id),
                safety_level="normal",
                relationship=relationship,
            ).as_dict(),
            "proactive_nudges": self.proactive_nudges.pending(session_id),
            "behavior_instincts": self.behavior_instincts.active(session_id),
            "profile_facts": self.profile_fact_learner.current(session_id),
            "profile_stats": self.memory.get_profile_interaction_stats(session_id),
            "quality_cases": self.memory.list_quality_cases(session_id, limit=20),
            "interaction_learning": self.interaction_outcomes.list_all(session_id),
            "self_model": self.self_model.history(session_id),
            "context_checkpoint": self.context_window.checkpoint(session_id),
        }

    # 作用：返回会话级心理状态，并补充由近期交互计算出的情感状态。
    # 参数 session_id：需要读取心理状态的会话标识。
    # 返回：包含长期心理状态和近期 affective_state 的字典。
    def mental_state_status(self, session_id: str) -> dict[str, object]:
        state = self.mental_state.status(session_id)
        state["affective_state"] = self.affective_state.status(session_id).as_dict()
        return state
