import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from service_club.capabilities.mcp_stdio import (
    McpStdioConfigurationError,
    parse_stdio_servers,
)
from service_club.capabilities.policy import CapabilityPolicyError
from service_club.capabilities.system_tasks import (
    SystemTaskConfigurationError,
    parse_system_tasks,
)
from service_club.core.agent import AgentTaskResumeError, ServiceClubCore
from service_club.core.conversation.characters import CHARACTERS
from service_club.core.runtime.agent_request_store import AgentRequestConflict
from service_club.core.runtime.agent_task_store import AgentTaskStore
from service_club.core.runtime.async_chat import (
    AsyncChatError,
    AsyncChatOverloaded,
    AsyncChatRuntime,
)
from service_club.core.runtime.attachment_store import AttachmentError
from service_club.core.runtime.background_jobs import BackgroundJobWorker
from service_club.core.runtime.background_task_finalizer import BackgroundTaskFinalizer
from service_club.core.runtime.channel_security import (
    ChannelSecurityError,
    InboundChannelSecurity,
)
from service_club.core.runtime.doctor import ServiceClubDoctor
from service_club.core.runtime.reminder_delivery import ReminderWebhookDispatcher
from service_club.core.tooling.tool_registry import ExternalDispatchReviewError
from service_club.core.types import CharacterId, ChatMessage, ChatMode, ChatRequest, ChatResponse
from service_club.runtime_config import RuntimeConfigStore, masked_secret, parse_json_object
from service_club.settings import settings

BASE_DIR = Path(__file__).parent
ASSET_DIR = settings.sticker_dir.parent
MEDIA_DIR = settings.data_dir / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_CONFIG = RuntimeConfigStore(settings.data_dir / "runtime_config.json")
RUNTIME_CONFIG.apply_saved()
CORE = ServiceClubCore(sticker_base=settings.sticker_dir)
REMINDER_DISPATCHER = ReminderWebhookDispatcher(CORE.memory, CORE.background_jobs)
BACKGROUND_FINALIZER = BackgroundTaskFinalizer(CORE.agent_tasks, CORE.outcome_verifier)
ASYNC_CHAT = AsyncChatRuntime(CORE, CORE.background_jobs)
CHANNEL_SECURITY = InboundChannelSecurity(backend=CORE.memory.backend)


# 作用：执行“bounded_env_int”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 name：调用方传入的name，用于本次处理。
# 参数 default：输入缺失或无效时使用的默认值。
# 参数 minimum：调用方传入的minimum，用于本次处理。
# 参数 maximum：调用方传入的maximum，用于本次处理。
def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


# 作用：执行“run_background_workflow”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 job：调用方传入的job，用于本次处理。
def _run_background_workflow(job: dict) -> dict:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    run_id = str(payload.get("workflow_run_id") or "")

    # 作用：执行“cancelled”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def cancelled() -> bool:
        current = CORE.background_jobs.get(str(job.get("id") or ""))
        return bool(
            current is None
            or current.get("status") == "cancelled"
            or any(
                CORE.agent_tasks.cancel_requested(task_id)
                for task_id in BACKGROUND_FINALIZER.task_ids(job)
            )
        )

    workflow = CORE.capabilities.execute_prepared_workflow(
        run_id,
        session_id=str(job.get("session_id") or ""),
        cancel_check=cancelled,
    )
    BACKGROUND_FINALIZER.finalize_workflow(job, workflow)
    return workflow


BACKGROUND_WORKER = BackgroundJobWorker(
    CORE.background_jobs,
    {
        REMINDER_DISPATCHER.job_kind: REMINDER_DISPATCHER.deliver,
        "workflow.run": _run_background_workflow,
    },
    producers=(REMINDER_DISPATCHER.scan_due,),
    dead_letter_handlers={
        REMINDER_DISPATCHER.job_kind: REMINDER_DISPATCHER.release_dead_letter,
        "workflow.run": BACKGROUND_FINALIZER.dead_letter,
    },
    name="yukino-system-worker",
)
AGENT_WORKER = BackgroundJobWorker(
    CORE.background_jobs,
    {ASYNC_CHAT.job_kind: ASYNC_CHAT.run},
    dead_letter_handlers={ASYNC_CHAT.job_kind: ASYNC_CHAT.dead_letter},
    poll_seconds=0.15,
    lease_seconds=600,
    concurrency=_bounded_env_int("YUKINO_AGENT_WORKERS", 2, 1, 8),
    serialize_by_session=True,
    name="yukino-agent-worker",
)


@asynccontextmanager
# 作用：执行“lifespan”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 _：调用方传入的_，用于本次处理。
async def lifespan(_: FastAPI):
    BACKGROUND_WORKER.start()
    AGENT_WORKER.start()
    try:
        yield
    finally:
        AGENT_WORKER.stop()
        BACKGROUND_WORKER.stop()

app = FastAPI(
    title="AGI Yukino",
    description="原生侍奉部多角色情感陪伴运行时",
    version="1.0.0-native",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.state.core = CORE
app.state.runtime_config = RUNTIME_CONFIG
app.state.background_worker = BACKGROUND_WORKER
app.state.reminder_dispatcher = REMINDER_DISPATCHER
app.state.background_finalizer = BACKGROUND_FINALIZER
app.state.async_chat = ASYNC_CHAT
app.state.agent_worker = AGENT_WORKER
app.state.channel_security = CHANNEL_SECURITY


# 作用：执行“public_attachment”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 item：调用方传入的item，用于本次处理。
def public_attachment(item: dict) -> dict:
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


# 作用：执行“public_background_job”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 item：调用方传入的item，用于本次处理。
def public_background_job(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "id",
            "kind",
            "session_id",
            "task_id",
            "related_task_id",
            "status",
            "attempts",
            "max_attempts",
            "available_at",
            "cancel_requested",
            "error",
            "created_at",
            "updated_at",
            "completed_at",
        )
    }


# 作用：执行“public_external_dispatch”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 item：调用方传入的item，用于本次处理。
def public_external_dispatch(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "id",
            "operation_id",
            "task_id",
            "capability",
            "action",
            "provider_idempotency_key",
            "status",
            "attempts",
            "receipt",
            "error",
            "provider_started_at",
            "completed_at",
            "created_at",
            "updated_at",
        )
    }


@app.middleware("http")
# 作用：执行“security_headers”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 request：调用方传入的request，用于本次处理。
# 参数 call_next：调用方传入的call_next，用于本次处理。
async def security_headers(request: Request, call_next):  # noqa: ANN001
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin", "").strip()
        host = request.headers.get("host", "").strip()
        if origin and urlsplit(origin).netloc and not hmac.compare_digest(urlsplit(origin).netloc, host):
            return HTMLResponse("Cross-origin state change rejected.", status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:"
    )
    return response


# 作用：定义“ClearMemoryRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class ClearMemoryRequest(BaseModel):
    session_id: str = "default"
    confirmed: bool = False


# 作用：定义“ConversationCreateRequest”相关的数据结构、异常类型或服务组件。
# 字段：chat_mode：该对象中的结构化字段。、character：该对象中的结构化字段。
class ConversationCreateRequest(BaseModel):
    chat_mode: ChatMode
    character: CharacterId


# 作用：定义“ConversationAdoptRequest”相关的数据结构、异常类型或服务组件。
# 字段：conversation_id：该对象中的结构化字段。
class ConversationAdoptRequest(ConversationCreateRequest):
    conversation_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )


# 作用：定义“ConversationDeleteRequest”相关的数据结构、异常类型或服务组件。
# 字段：confirmed：该对象中的结构化字段。
class ConversationDeleteRequest(BaseModel):
    confirmed: bool = False


# 作用：定义“ForgetMemoryRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、kind：该对象中的结构化字段。、memory_id：该对象中的结构化字段。、key：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class ForgetMemoryRequest(BaseModel):
    session_id: str = "default"
    kind: Literal["local", "permanent"]
    memory_id: int | None = Field(default=None, ge=1)
    key: str = Field(default="", max_length=200)
    confirmed: bool = False


# 作用：定义“NudgeClaimRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。
class NudgeClaimRequest(BaseModel):
    session_id: str = "default"


# 作用：定义“ReminderClaimRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。
class ReminderClaimRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9:_-]+$")


# 作用：定义“ReminderAckRequest”相关的数据结构、异常类型或服务组件。
# 字段：reminder_id：该对象中的结构化字段。、claim_token：该对象中的结构化字段。
class ReminderAckRequest(ReminderClaimRequest):
    reminder_id: int = Field(ge=1)
    claim_token: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


# 作用：定义“ReminderCancelRequest”相关的数据结构、异常类型或服务组件。
# 字段：reminder_id：该对象中的结构化字段。
class ReminderCancelRequest(ReminderClaimRequest):
    reminder_id: int = Field(ge=1)


# 作用：定义“BackgroundJobActionRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class BackgroundJobActionRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    confirmed: bool = False


# 作用：定义“InstinctRevokeRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。
class InstinctRevokeRequest(BaseModel):
    session_id: str = "default"


# 作用：定义“ProfileFactRevokeRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。
class ProfileFactRevokeRequest(BaseModel):
    session_id: str = "default"


# 作用：定义“CapabilityExecuteRequest”相关的数据结构、异常类型或服务组件。
# 字段：capability：该对象中的结构化字段。、action：该对象中的结构化字段。、arguments：该对象中的结构化字段。、confirmed：该对象中的结构化字段。、session_id：该对象中的结构化字段。
class CapabilityExecuteRequest(BaseModel):
    capability: str
    action: str
    arguments: dict = Field(default_factory=dict)
    confirmed: bool = False
    session_id: str = Field(
        default="",
        max_length=160,
        pattern=r"^(?:[A-Za-z0-9:_-]+)?$",
    )


# 作用：定义“CapabilityPolicyRuleRequest”相关的数据结构、异常类型或服务组件。
# 字段：capability：该对象中的结构化字段。、action：该对象中的结构化字段。、enabled：该对象中的结构化字段。
class CapabilityPolicyRuleRequest(BaseModel):
    capability: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    action: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^(?:\*|[A-Za-z0-9_-]+)$",
    )
    enabled: bool


# 作用：定义“CapabilityPolicyUpdateRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、rules：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class CapabilityPolicyUpdateRequest(BaseModel):
    session_id: str = Field(
        default="",
        max_length=160,
        pattern=r"^(?:[A-Za-z0-9:_-]+)?$",
    )
    rules: list[CapabilityPolicyRuleRequest] = Field(min_length=1, max_length=100)
    confirmed: bool = False


# 作用：定义“CapabilityGrantCreateRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、capability：该对象中的结构化字段。、action：该对象中的结构化字段。、ttl_seconds：该对象中的结构化字段。、uses：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class CapabilityGrantCreateRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    capability: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    action: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    uses: int = Field(default=1, ge=1, le=50)
    confirmed: bool = False


# 作用：定义“CapabilityGrantRevokeRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class CapabilityGrantRevokeRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    confirmed: bool = False


# 作用：定义“ChannelMessageRequest”相关的数据结构、异常类型或服务组件。
# 字段：user_id：该对象中的结构化字段。、text：该对象中的结构化字段。、session_id：该对象中的结构化字段。、character：该对象中的结构化字段。、chat_mode：该对象中的结构化字段。、request_id：该对象中的结构化字段。
class ChannelMessageRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")
    text: str = Field(min_length=1, max_length=100_000)
    session_id: str = Field(
        default="",
        max_length=160,
        pattern=r"^(?:[A-Za-z0-9:_-]+)?$",
    )
    character: CharacterId = "yukino"
    chat_mode: ChatMode = "club"
    request_id: str = Field(
        default="",
        max_length=128,
        pattern=r"^(?:[A-Za-z0-9:_-]+)?$",
    )


# 作用：定义“AgentTaskCancelRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、request_id：该对象中的结构化字段。
class AgentTaskCancelRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )


# 作用：定义“ExternalDispatchReviewRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、decision：该对象中的结构化字段。、confirmed：该对象中的结构化字段。
class ExternalDispatchReviewRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    decision: Literal["confirm_succeeded", "retry", "abandon"]
    confirmed: bool = False


# 作用：定义“AttachmentUploadRequest”相关的数据结构、异常类型或服务组件。
# 字段：session_id：该对象中的结构化字段。、filename：该对象中的结构化字段。、mime_type：该对象中的结构化字段。、base64：该对象中的结构化字段。
class AttachmentUploadRequest(BaseModel):
    session_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=160)
    base64: str = Field(min_length=1, max_length=14_000_000)


# 作用：定义“RuntimeSettingsUpdate”相关的数据结构、异常类型或服务组件。
# 字段：openai_api_key：该对象中的结构化字段。、clear_openai_api_key：该对象中的结构化字段。、openai_base_url：该对象中的结构化字段。、openai_model：该对象中的结构化字段。、openai_fallback_model：该对象中的结构化字段。、openai_embedding_api_key：该对象中的结构化字段。、clear_openai_embedding_api_key：该对象中的结构化字段。、openai_embedding_base_url：该对象中的结构化字段。、openai_embedding_model：该对象中的结构化字段。、openai_embedding_timeout_seconds：该对象中的结构化字段。、openai_timeout_seconds：该对象中的结构化字段。、model_max_concurrent：该对象中的结构化字段。、model_circuit_failures：该对象中的结构化字段。、model_circuit_cooldown_seconds：该对象中的结构化字段。、tts_enabled：该对象中的结构化字段。、openai_tts_model：该对象中的结构化字段。、tts_voices：该对象中的结构化字段。、mcp_endpoints：该对象中的结构化字段。、mcp_headers：该对象中的结构化字段。、clear_mcp_headers：该对象中的结构化字段。、mcp_stdio_enabled：该对象中的结构化字段。、mcp_stdio_servers：该对象中的结构化字段。、mcp_stdio_env：该对象中的结构化字段。、clear_mcp_stdio_env：该对象中的结构化字段。、mcp_allow_private_hostnames：该对象中的结构化字段。、outbound_webhook：该对象中的结构化字段。、outbound_webhook_secret：该对象中的结构化字段。、clear_outbound_webhook_secret：该对象中的结构化字段。、inbound_webhook_secret：该对象中的结构化字段。、clear_inbound_webhook_secret：该对象中的结构化字段。、background_reminder_delivery：该对象中的结构化字段。、smtp_host：该对象中的结构化字段。、smtp_port：该对象中的结构化字段。、smtp_username：该对象中的结构化字段。、smtp_password：该对象中的结构化字段。、clear_smtp_password：该对象中的结构化字段。、smtp_sender：该对象中的结构化字段。、agent_enabled：该对象中的结构化字段。、agent_max_steps：该对象中的结构化字段。、agent_workers：该对象中的结构化字段。、agent_queue_capacity：该对象中的结构化字段。、agent_session_queue_limit：该对象中的结构化字段。、task_wall_time_seconds：该对象中的结构化字段。、task_model_token_limit：该对象中的结构化字段。、task_model_cost_limit：该对象中的结构化字段。、task_tool_call_limit：该对象中的结构化字段。、task_model_call_limit：该对象中的结构化字段。、model_max_output_tokens：该对象中的结构化字段。、input_cost_per_million：该对象中的结构化字段。、output_cost_per_million：该对象中的结构化字段。、cost_currency：该对象中的结构化字段。、allow_desktop：该对象中的结构化字段。、allowed_write_dirs：该对象中的结构化字段。、system_tasks：该对象中的结构化字段。、timezone：该对象中的结构化字段。、vector_backend：该对象中的结构化字段。、milvus_uri：该对象中的结构化字段。、milvus_token：该对象中的结构化字段。、clear_milvus_token：该对象中的结构化字段。、milvus_database：该对象中的结构化字段。、milvus_collection_prefix：该对象中的结构化字段。、graph_backend：该对象中的结构化字段。、neo4j_uri：该对象中的结构化字段。、neo4j_username：该对象中的结构化字段。、neo4j_password：该对象中的结构化字段。、clear_neo4j_password：该对象中的结构化字段。、neo4j_database：该对象中的结构化字段。
class RuntimeSettingsUpdate(BaseModel):
    openai_api_key: str | None = Field(default=None, max_length=4096)
    clear_openai_api_key: bool = False
    openai_base_url: str = Field(default="", max_length=2048)
    openai_model: str = Field(default="gpt-4.1-mini", max_length=200)
    openai_fallback_model: str = Field(default="", max_length=200)
    openai_embedding_api_key: str | None = Field(default=None, max_length=4096)
    clear_openai_embedding_api_key: bool = False
    openai_embedding_base_url: str = Field(default="", max_length=2048)
    openai_embedding_model: str = Field(default="", max_length=200)
    openai_embedding_timeout_seconds: float = Field(default=8, ge=1, le=120)
    openai_timeout_seconds: float = Field(default=12, ge=1, le=120)
    model_max_concurrent: int = Field(default=4, ge=1, le=16)
    model_circuit_failures: int = Field(default=2, ge=1, le=10)
    model_circuit_cooldown_seconds: int = Field(default=30, ge=5, le=600)
    tts_enabled: bool = False
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", max_length=200)
    tts_voices: dict[str, str] = Field(default_factory=dict)
    mcp_endpoints: dict[str, str] = Field(default_factory=dict)
    mcp_headers: dict[str, dict[str, str]] | None = None
    clear_mcp_headers: bool = False
    mcp_stdio_enabled: bool = False
    mcp_stdio_servers: dict[str, dict] = Field(default_factory=dict)
    mcp_stdio_env: dict[str, dict[str, str]] | None = None
    clear_mcp_stdio_env: bool = False
    mcp_allow_private_hostnames: bool = False
    outbound_webhook: str = Field(default="", max_length=2048)
    outbound_webhook_secret: str | None = Field(default=None, max_length=4096)
    clear_outbound_webhook_secret: bool = False
    inbound_webhook_secret: str | None = Field(default=None, max_length=4096)
    clear_inbound_webhook_secret: bool = False
    background_reminder_delivery: bool = False
    smtp_host: str = Field(default="", max_length=512)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str = Field(default="", max_length=512)
    smtp_password: str | None = Field(default=None, max_length=4096)
    clear_smtp_password: bool = False
    smtp_sender: str = Field(default="", max_length=512)
    agent_enabled: bool = True
    agent_max_steps: int = Field(default=4, ge=1, le=8)
    agent_workers: int = Field(default=2, ge=1, le=8)
    agent_queue_capacity: int = Field(default=64, ge=4, le=1000)
    agent_session_queue_limit: int = Field(default=3, ge=1, le=20)
    task_wall_time_seconds: float = Field(default=180, ge=10, le=1800)
    task_model_token_limit: int = Field(default=32768, ge=1000, le=1_000_000)
    task_model_cost_limit: float = Field(default=1.0, ge=0.01, le=100)
    task_tool_call_limit: int = Field(default=16, ge=1, le=100)
    task_model_call_limit: int = Field(default=8, ge=1, le=50)
    model_max_output_tokens: int = Field(default=2048, ge=64, le=8192)
    input_cost_per_million: float = Field(default=0, ge=0, le=10_000)
    output_cost_per_million: float = Field(default=0, ge=0, le=10_000)
    cost_currency: str = Field(default="USD", min_length=1, max_length=12)
    allow_desktop: bool = False
    allowed_write_dirs: list[str] = Field(default_factory=list, max_length=12)
    system_tasks: dict[str, list[str] | dict[str, object]] = Field(
        default_factory=dict
    )
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    vector_backend: Literal["relational", "milvus"] = "relational"
    milvus_uri: str = Field(default="", max_length=2048)
    milvus_token: str | None = Field(default=None, max_length=4096)
    clear_milvus_token: bool = False
    milvus_database: str = Field(default="default", min_length=1, max_length=100)
    milvus_collection_prefix: str = Field(
        default="agi_yukino_memory",
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    graph_backend: Literal["relational", "neo4j"] = "relational"
    neo4j_uri: str = Field(default="", max_length=2048)
    neo4j_username: str = Field(default="neo4j", max_length=200)
    neo4j_password: str | None = Field(default=None, max_length=4096)
    clear_neo4j_password: bool = False
    neo4j_database: str = Field(default="neo4j", min_length=1, max_length=100)


# 作用：定义“SettingsTestRequest”相关的数据结构、异常类型或服务组件。
# 字段：area：该对象中的结构化字段。、model_role：该对象中的结构化字段。、character：该对象中的结构化字段。、server：该对象中的结构化字段。
class SettingsTestRequest(BaseModel):
    area: Literal["model", "voice", "mcp", "mail", "storage"]
    model_role: Literal["primary", "fallback"] = "primary"
    character: CharacterId = "yukino"
    server: str = Field(default="", max_length=200)


# 作用：定义“StickerUploadRequest”相关的数据结构、异常类型或服务组件。
# 字段：character：该对象中的结构化字段。、emotion：该对象中的结构化字段。、filename：该对象中的结构化字段。、base64：该对象中的结构化字段。
class StickerUploadRequest(BaseModel):
    character: CharacterId
    emotion: Literal["happy", "sad", "anxious", "angry", "lonely", "shy", "thinking", "neutral"]
    filename: str = Field(max_length=255)
    base64: str = Field(max_length=8_000_000)


# 作用：执行“remote_url”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 value：需要转换、校验或保存的输入值。
# 参数 allow_empty：调用方传入的allow_empty，用于本次处理。
def _remote_url(value: str, *, allow_empty: bool = True) -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    if any(character in value for character in "\r\n\0"):
        raise HTTPException(status_code=422, detail="地址包含不允许的控制字符。")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=422, detail="地址必须是有效的 HTTP 或 HTTPS URL。")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="服务端地址不能包含用户名或密码。")
    if parsed.fragment:
        raise HTTPException(status_code=422, detail="服务端地址不能包含片段标识。")
    try:
        parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="服务端地址端口无效。") from exc
    return value


# 作用：执行“external_error_message”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 exc：调用方传入的exc，用于本次处理。
def _external_error_message(exc: Exception) -> str:
    """Return useful provider feedback without leaking credentials or full responses."""
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        elif isinstance(error, str):
            detail = error
    detail = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-••••", detail).strip()
    detail = detail[:240]
    status = f"HTTP {status_code}" if status_code else type(exc).__name__
    return f"连接失败（{status}）：{detail}" if detail else f"连接失败（{status}）。"


# 作用：执行“model_probe_message”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 result：下游组件返回的原始结果对象。
def _model_probe_message(result: dict) -> str:
    model = str(result.get("model") or "主模型")
    latency_ms = int(result.get("latency_ms") or 0)
    if result.get("ok"):
        return f"{model} 连接成功，真实调用耗时 {latency_ms}ms。"
    failure = result.get("failure") if isinstance(result.get("failure"), dict) else {}
    code = str(failure.get("code") or "provider_error")
    status_code = failure.get("status_code")
    messages = {
        "missing_api_key": "尚未配置 API Key。",
        "authentication_error": "API Key 被拒绝，请核对密钥与 Base URL。",
        "model_not_found": "模型不存在或当前账户无权访问，请核对模型名称。",
        "timeout": "模型在配置的超时时间内没有返回；请检查供应商延迟或适当提高超时。",
        "rate_limited": "供应商正在限流，请稍后再试或配置备用模型。",
        "provider_unavailable": "供应商服务暂时不可用，请稍后再试或配置备用模型。",
        "network_error": "无法连接模型供应商，请检查 Base URL、网络或代理。",
        "empty_response": "模型返回了空内容，当前响应格式可能不兼容。",
        "invalid_request": "供应商拒绝了请求，请检查模型名称和兼容参数。",
        "local_capacity": "本机模型并发槽位已满，请等待正在运行的请求结束。",
        "cancelled": "连接探测已经停止。",
        "circuit_probe_in_flight": "已有一个模型恢复探测正在执行，请等待它结束。",
        "provider_error": "模型供应商返回未知错误。",
    }
    status = f" HTTP {status_code}" if status_code else ""
    return f"{model} 连接失败（{code}{status}）：{messages.get(code, messages['provider_error'])}"


# 作用：执行“sticker_status”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def _sticker_status() -> dict:
    emotions = ("happy", "sad", "anxious", "angry", "lonely", "shy", "thinking", "neutral")
    coverage: dict[str, dict[str, int]] = {}
    total = 0
    for character in CHARACTERS.values():
        character_counts: dict[str, int] = {}
        for emotion in emotions:
            directory = settings.sticker_dir / character.sticker_pack / emotion
            count = len([path for path in directory.iterdir() if path.is_file()]) if directory.is_dir() else 0
            character_counts[emotion] = count
            total += count
        coverage[character.id] = character_counts
    return {"total": total, "coverage": coverage, "emotions": list(emotions)}


# 作用：执行“settings_snapshot”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def _settings_snapshot() -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "")
    embedding_api_key = os.getenv("OPENAI_EMBEDDING_API_KEY", "")
    smtp_password = os.getenv("YUKINO_SMTP_PASSWORD", "")
    webhook_secret = os.getenv("YUKINO_OUTBOUND_WEBHOOK_SECRET", "")
    inbound_webhook_secret = os.getenv("YUKINO_INBOUND_WEBHOOK_SECRET", "")
    milvus_token = os.getenv("YUKINO_MILVUS_TOKEN", "")
    neo4j_password = os.getenv("YUKINO_NEO4J_PASSWORD", "")
    mcp_endpoints = parse_json_object(os.getenv("YUKINO_MCP_ENDPOINTS", "{}"))
    mcp_headers = parse_json_object(os.getenv("YUKINO_MCP_HEADERS", "{}"))
    raw_stdio_servers = parse_json_object(
        os.getenv("YUKINO_MCP_STDIO_SERVERS", "{}")
    )
    raw_stdio_environment = parse_json_object(
        os.getenv("YUKINO_MCP_STDIO_ENV", "{}")
    )
    stdio_config_error = ""
    try:
        parsed_stdio_servers = parse_stdio_servers(
            raw_stdio_servers,
            raw_stdio_environment,
        )
    except McpStdioConfigurationError as exc:
        parsed_stdio_servers = {}
        stdio_config_error = str(exc)[:240]
    public_stdio_servers = {
        name: {
            "command": server.command,
            "args": list(server.args),
            "cwd": server.cwd,
            "timeout_seconds": server.timeout_seconds,
        }
        for name, server in parsed_stdio_servers.items()
    }
    configured_mcp_servers = set(mcp_endpoints) | set(parsed_stdio_servers)
    mcp_catalog = app.state.core.capabilities.mcp_catalog.snapshot(
        configured_mcp_servers
    )
    voices = {
        character.id: os.getenv(f"YUKINO_TTS_VOICE_{character.id.upper()}", "alloy")
        for character in CHARACTERS.values()
    }
    try:
        allowed_write_dirs_value = json.loads(os.getenv("YUKINO_ALLOWED_WRITE_DIRS", "[]"))
    except json.JSONDecodeError:
        allowed_write_dirs_value = []
    allowed_write_dirs = (
        [str(item) for item in allowed_write_dirs_value if isinstance(item, str)]
        if isinstance(allowed_write_dirs_value, list)
        else []
    )
    try:
        system_tasks = {
            name: spec.public()
            for name, spec in parse_system_tasks(
                parse_json_object(os.getenv("YUKINO_SYSTEM_TASKS", "{}"))
            ).items()
        }
        system_tasks_error = ""
    except SystemTaskConfigurationError as exc:
        system_tasks = {}
        system_tasks_error = str(exc)[:240]
    task_budget = AgentTaskStore.default_budget()
    return {
        "storage": {
            "path": str(app.state.runtime_config.path),
            "mode": "local-0600",
            "secrets_returned": False,
        },
        "model": {
            "provider": settings.model_provider,
            "configured": bool(api_key),
            "api_key_set": bool(api_key),
            "api_key_masked": masked_secret(api_key),
            "base_url": os.getenv("OPENAI_BASE_URL", ""),
            "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            "fallback_model": os.getenv("OPENAI_FALLBACK_MODEL", ""),
            "embedding_api_key_set": bool(embedding_api_key),
            "embedding_api_key_masked": masked_secret(embedding_api_key),
            "embedding_uses_chat_credentials": not bool(embedding_api_key),
            "embedding_base_url": os.getenv("OPENAI_EMBEDDING_BASE_URL", ""),
            "embedding_model": os.getenv("OPENAI_EMBEDDING_MODEL", ""),
            "embedding_timeout_seconds": float(
                os.getenv("OPENAI_EMBEDDING_TIMEOUT_SECONDS", "8")
            ),
            "timeout_seconds": float(os.getenv("OPENAI_TIMEOUT_SECONDS", "12")),
            "max_concurrent": int(os.getenv("YUKINO_MODEL_MAX_CONCURRENT", "4")),
            "circuit_failures": _bounded_env_int(
                "YUKINO_MODEL_CIRCUIT_FAILURES",
                2,
                1,
                10,
            ),
            "circuit_cooldown_seconds": _bounded_env_int(
                "YUKINO_MODEL_CIRCUIT_COOLDOWN_SECONDS",
                30,
                5,
                600,
            ),
            "runtime": (
                app.state.core.model.status()
                if callable(getattr(app.state.core.model, "status", None))
                else {"ok": False, "available": False}
            ),
        },
        "voice": {
            "configured": bool(api_key) and os.getenv("TTS_ENABLED", "").lower() in {"1", "true", "yes"},
            "enabled": os.getenv("TTS_ENABLED", "").lower() in {"1", "true", "yes"},
            "model": os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            "voices": voices,
        },
        "stickers": _sticker_status(),
        "mcp": {
            "configured": bool(
                mcp_endpoints
                or (
                    os.getenv("YUKINO_MCP_STDIO_ENABLED", "false").lower()
                    in {"1", "true", "yes", "on"}
                    and parsed_stdio_servers
                )
            ),
            "endpoints": mcp_endpoints,
            "headers_set": bool(mcp_headers),
            "header_servers": sorted(mcp_headers),
            "stdio_enabled": os.getenv(
                "YUKINO_MCP_STDIO_ENABLED", "false"
            ).lower()
            in {"1", "true", "yes", "on"},
            "stdio_servers": public_stdio_servers,
            "stdio_config_error": stdio_config_error,
            "stdio_env_set": bool(raw_stdio_environment),
            "stdio_env_servers": sorted(raw_stdio_environment),
            "stdio_env_keys": {
                name: sorted(server.env)
                for name, server in parsed_stdio_servers.items()
                if server.env
            },
            "catalog": mcp_catalog,
            "allow_private_hostnames": os.getenv(
                "YUKINO_MCP_ALLOW_PRIVATE_HOSTNAMES", "false"
            ).lower()
            in {"1", "true", "yes", "on"},
            "transport_security": {
                "dns_pinned": True,
                "redirects_forbidden": True,
                "jsonrpc_response_validated": True,
                "stdio_shell_disabled": True,
                "stdio_environment_allowlisted": True,
                "stdio_application_secrets_inherited": False,
                "stdio_launch_confirmation": True,
            },
        },
        "channels": {
            "configured": bool(os.getenv("YUKINO_OUTBOUND_WEBHOOK", "").strip()),
            "outbound_webhook": os.getenv("YUKINO_OUTBOUND_WEBHOOK", ""),
            "webhook_secret_set": bool(webhook_secret),
            "webhook_secret_masked": masked_secret(webhook_secret),
            "inbound_secret_set": bool(inbound_webhook_secret),
            "inbound_secret_masked": masked_secret(inbound_webhook_secret),
            "inbound_security": app.state.channel_security.status(),
            "background_reminder_delivery": os.getenv(
                "YUKINO_BACKGROUND_REMINDER_DELIVERY", "false"
            ).lower()
            in {"1", "true", "yes", "on"},
            "delivery": app.state.reminder_dispatcher.status(),
            "jobs": app.state.background_worker.status(),
        },
        "mail": {
            "configured": bool(os.getenv("YUKINO_SMTP_HOST", "") and smtp_password),
            "host": os.getenv("YUKINO_SMTP_HOST", ""),
            "port": int(os.getenv("YUKINO_SMTP_PORT", "465")),
            "username": os.getenv("YUKINO_SMTP_USERNAME", ""),
            "password_set": bool(smtp_password),
            "password_masked": masked_secret(smtp_password),
            "sender": os.getenv("YUKINO_SMTP_SENDER", ""),
        },
        "agent": {
            "enabled": os.getenv("YUKINO_AGENT_ENABLED", "true").lower() not in {"0", "false", "off", "no"},
            "max_steps": int(os.getenv("YUKINO_AGENT_MAX_STEPS", "4")),
            "workers": _bounded_env_int("YUKINO_AGENT_WORKERS", 2, 1, 8),
            "queue_capacity": _bounded_env_int(
                "YUKINO_AGENT_QUEUE_CAPACITY",
                64,
                4,
                1000,
            ),
            "session_queue_limit": _bounded_env_int(
                "YUKINO_AGENT_SESSION_QUEUE_LIMIT",
                3,
                1,
                20,
            ),
            "budget": task_budget,
            "queue": {
                **app.state.async_chat.status(),
                "worker": app.state.agent_worker.status(),
            },
            "allow_desktop": os.getenv("YUKINO_ALLOW_DESKTOP", "").lower() in {"1", "true", "yes"},
            "allowed_write_dirs": allowed_write_dirs,
            "system_tasks": system_tasks,
            "system_tasks_error": system_tasks_error,
            "timezone": os.getenv("YUKINO_TIMEZONE", "Asia/Shanghai"),
            "file_access": app.state.core.tools.registry.file_access_status(),
        },
        "data": {
            "fact_store": {
                "backend": app.state.core.memory.backend.name,
                "location": app.state.core.memory.backend.location,
            },
            "vector": {
                "backend": os.getenv("YUKINO_VECTOR_BACKEND", "relational"),
                "uri": os.getenv("YUKINO_MILVUS_URI", ""),
                "token_set": bool(milvus_token),
                "token_masked": masked_secret(milvus_token),
                "database": os.getenv("YUKINO_MILVUS_DATABASE", "default"),
                "collection_prefix": os.getenv(
                    "YUKINO_MILVUS_COLLECTION_PREFIX", "agi_yukino_memory"
                ),
                "runtime": (
                    app.state.core.vector_store.status()
                    if app.state.core.vector_store is not None
                    else {
                        "ok": True,
                        "enabled": False,
                        "backend": "relational",
                        "connection_state": "disabled",
                        "source_of_truth": "relational",
                    }
                ),
            },
            "graph": {
                "backend": os.getenv("YUKINO_GRAPH_BACKEND", "relational"),
                "uri": os.getenv("YUKINO_NEO4J_URI", ""),
                "username": os.getenv("YUKINO_NEO4J_USERNAME", "neo4j"),
                "password_set": bool(neo4j_password),
                "password_masked": masked_secret(neo4j_password),
                "database": os.getenv("YUKINO_NEO4J_DATABASE", "neo4j"),
                "runtime": app.state.core.capabilities.knowledge_graph.status(),
            },
        },
        "capabilities": app.state.core.capabilities.manifest(),
    }


@app.get("/", response_class=HTMLResponse)
# 作用：执行“index”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/characters")
# 作用：执行“characters”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def characters() -> list[dict]:
    return [
        {
            "id": item.id,
            "display_name": item.display_name,
            "short_name": item.short_name,
            "accent_color": item.accent_color,
            "role_summary": item.role_summary,
        }
        for item in CHARACTERS.values()
    ]


@app.get("/api/conversations")
# 作用：执行“conversations”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 chat_mode：调用方传入的chat_mode，用于本次处理。
# 参数 character：角色标识，决定采用的角色配置或行为策略。
def conversations(chat_mode: ChatMode, character: CharacterId) -> list[dict]:
    return app.state.core.conversations.list(
        chat_mode=chat_mode,
        character=character,
    )


@app.post("/api/conversations")
# 作用：执行“create_conversation”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def create_conversation(payload: ConversationCreateRequest) -> dict:
    return app.state.core.conversations.create(
        chat_mode=payload.chat_mode,
        character=payload.character,
    )


@app.post("/api/conversations/ensure")
# 作用：执行“ensure_conversation”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def ensure_conversation(payload: ConversationCreateRequest) -> dict:
    return app.state.core.conversations.ensure(
        chat_mode=payload.chat_mode,
        character=payload.character,
    )


@app.post("/api/conversations/adopt")
# 作用：执行“adopt_conversation”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def adopt_conversation(payload: ConversationAdoptRequest) -> dict:
    return app.state.core.conversations.adopt(
        conversation_id=payload.conversation_id,
        chat_mode=payload.chat_mode,
        character=payload.character,
    )


@app.get("/api/conversations/{conversation_id}")
# 作用：执行“conversation”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 conversation_id：调用方传入的conversation_id，用于本次处理。
def conversation(conversation_id: str) -> dict:
    thread = app.state.core.conversations.get(conversation_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return thread


@app.post("/api/conversations/{conversation_id}/delete")
# 作用：执行“delete_conversation”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 conversation_id：调用方传入的conversation_id，用于本次处理。
# 参数 payload：调用方传入的payload，用于本次处理。
def delete_conversation(
    conversation_id: str,
    payload: ConversationDeleteRequest,
) -> dict:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="delete conversation requires confirmation")
    if app.state.core.conversations.get(conversation_id, include_messages=False) is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    cleared = app.state.core.clear_memory(conversation_id)
    deleted = app.state.core.conversations.delete(conversation_id)
    return {"ok": deleted, **cleared}


@app.get("/api/agents")
# 作用：执行“agents”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def agents() -> list[dict]:
    return app.state.core.character_agents.manifest()


@app.get("/api/tools")
# 作用：执行“tools”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def tools() -> list[dict]:
    return app.state.core.tools.registry.manifest()


@app.get("/api/capabilities")
# 作用：执行“capabilities”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def capabilities() -> list[dict]:
    return app.state.core.capabilities.manifest()


@app.get("/api/capabilities/search")
# 作用：执行“capability_search”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 query：用于筛选、检索或匹配目标数据的查询词。
# 参数 limit：返回结果的最大数量。
def capability_search(query: str, limit: int = 6) -> list[dict]:
    return app.state.core.capabilities.search(query, limit=max(1, min(limit, 20)))


@app.post("/api/capabilities/execute")
# 作用：执行“capability_execute”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def capability_execute(payload: CapabilityExecuteRequest) -> dict:
    return app.state.core.capabilities.execute(
        payload.capability,
        payload.action,
        payload.arguments,
        confirmed=payload.confirmed,
        session_id=payload.session_id,
    )


@app.get("/api/agent/capability-policy")
# 作用：执行“capability_policy”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def capability_policy(session_id: str = "") -> dict:
    if session_id and not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", session_id):
        raise HTTPException(status_code=422, detail="会话标识格式无效。")
    return app.state.core.capabilities.permission_snapshot(session_id)


@app.put("/api/agent/capability-policy")
# 作用：执行“update_capability_policy”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def update_capability_policy(payload: CapabilityPolicyUpdateRequest) -> dict:
    try:
        return app.state.core.capabilities.update_permission_policy(
            [item.model_dump() for item in payload.rules],
            confirmed=payload.confirmed,
            session_id=payload.session_id,
        )
    except CapabilityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/agent/capability-grants")
# 作用：执行“create_capability_grant”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def create_capability_grant(payload: CapabilityGrantCreateRequest) -> dict:
    try:
        grant = app.state.core.capabilities.create_temporary_grant(
            session_id=payload.session_id,
            capability=payload.capability,
            action=payload.action,
            ttl_seconds=payload.ttl_seconds,
            uses=payload.uses,
            confirmed=payload.confirmed,
        )
    except CapabilityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "grant": grant}


@app.post("/api/agent/capability-grants/{grant_id}/revoke")
# 作用：执行“revoke_capability_grant”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 grant_id：调用方传入的grant_id，用于本次处理。
# 参数 payload：调用方传入的payload，用于本次处理。
def revoke_capability_grant(
    grant_id: str,
    payload: CapabilityGrantRevokeRequest,
) -> dict:
    if not re.fullmatch(r"grant-[a-f0-9]{20}", grant_id):
        raise HTTPException(status_code=404, detail="临时授权不存在。")
    try:
        return {
            "ok": True,
            **app.state.core.capabilities.revoke_temporary_grant(
                grant_id,
                session_id=payload.session_id,
                confirmed=payload.confirmed,
            ),
        }
    except CapabilityPolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/settings")
# 作用：执行“runtime_settings”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def runtime_settings() -> dict:
    return _settings_snapshot()


@app.put("/api/settings")
# 作用：执行“update_runtime_settings”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def update_runtime_settings(payload: RuntimeSettingsUpdate) -> dict:
    base_url = _remote_url(payload.openai_base_url)
    embedding_base_url = _remote_url(payload.openai_embedding_base_url)
    outbound_webhook = _remote_url(payload.outbound_webhook)
    embedding_model = payload.openai_embedding_model.strip()
    milvus_uri = payload.milvus_uri.strip()
    if milvus_uri:
        milvus_uri = _remote_url(milvus_uri, allow_empty=False)
        parsed_milvus = urlsplit(milvus_uri)
        if parsed_milvus.path not in {"", "/"} or parsed_milvus.query:
            raise HTTPException(
                status_code=422,
                detail="Milvus 地址不能包含嵌套路径或查询参数。",
            )
    effective_api_key = (
        payload.openai_api_key.strip()
        if payload.openai_api_key is not None and payload.openai_api_key.strip()
        else os.getenv("OPENAI_API_KEY", "").strip()
    )
    if payload.clear_openai_api_key:
        effective_api_key = ""
    effective_embedding_api_key = (
        payload.openai_embedding_api_key.strip()
        if payload.openai_embedding_api_key is not None
        and payload.openai_embedding_api_key.strip()
        else os.getenv("OPENAI_EMBEDDING_API_KEY", "").strip()
    )
    if payload.clear_openai_embedding_api_key:
        effective_embedding_api_key = ""
    effective_embedding_api_key = effective_embedding_api_key or effective_api_key
    if payload.vector_backend == "milvus" and not milvus_uri:
        raise HTTPException(
            status_code=422,
            detail="启用 Milvus 前必须填写 Standalone 或 Distributed 服务 URI。",
        )
    if payload.vector_backend == "milvus" and (
        not embedding_model or not effective_embedding_api_key
    ):
        raise HTTPException(
            status_code=422,
            detail="启用 Milvus 前必须配置 Embedding 模型和可用的 Embedding API Key。",
        )
    neo4j_uri = payload.neo4j_uri.strip()
    if neo4j_uri:
        parsed_neo4j = urlsplit(neo4j_uri)
        if (
            parsed_neo4j.scheme
            not in {"neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc"}
            or not parsed_neo4j.hostname
            or parsed_neo4j.username
            or parsed_neo4j.password
            or parsed_neo4j.path not in {"", "/"}
            or parsed_neo4j.fragment
        ):
            raise HTTPException(
                status_code=422,
                detail="Neo4j 地址必须使用 neo4j:// 或 bolt://，且不能内嵌账号密码。",
            )
    effective_neo4j_password = (
        payload.neo4j_password
        if payload.neo4j_password is not None and payload.neo4j_password
        else os.getenv("YUKINO_NEO4J_PASSWORD", "")
    )
    if payload.clear_neo4j_password:
        effective_neo4j_password = ""
    if payload.graph_backend == "neo4j" and (
        not neo4j_uri or not payload.neo4j_username.strip() or not effective_neo4j_password
    ):
        raise HTTPException(
            status_code=422,
            detail="启用 Neo4j 前必须填写地址、用户名和密码。",
        )
    if payload.inbound_webhook_secret and len(payload.inbound_webhook_secret) < 16:
        raise HTTPException(status_code=422, detail="入站通道签名密钥至少需要 16 个字符。")
    if payload.background_reminder_delivery and not outbound_webhook:
        raise HTTPException(
            status_code=422,
            detail="开启后台提醒派送前必须先配置出站 Webhook。",
        )
    if len(payload.mcp_endpoints) > 20:
        raise HTTPException(status_code=422, detail="MCP 服务最多配置 20 个。")
    endpoints: dict[str, str] = {}
    for raw_name, raw_url in payload.mcp_endpoints.items():
        name = raw_name.strip()
        if not name or len(name) > 100 or not re.fullmatch(r"[\w.-]+", name):
            raise HTTPException(status_code=422, detail="MCP 名称只能包含字母、数字、点、横线和下划线。")
        endpoints[name] = _remote_url(raw_url, allow_empty=False)
    effective_stdio_environment: dict[str, dict[str, str]] = (
        payload.mcp_stdio_env
        if payload.mcp_stdio_env is not None
        else {
            str(name): {
                str(key): str(value)
                for key, value in values.items()
            }
            for name, values in parse_json_object(
                os.getenv("YUKINO_MCP_STDIO_ENV", "{}")
            ).items()
            if isinstance(values, dict)
        }
    )
    if payload.clear_mcp_stdio_env:
        effective_stdio_environment = {}
    try:
        parsed_stdio_servers = parse_stdio_servers(
            payload.mcp_stdio_servers,
            effective_stdio_environment,
        )
    except McpStdioConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    collisions = sorted(set(endpoints) & set(parsed_stdio_servers))
    if collisions:
        raise HTTPException(
            status_code=422,
            detail=f"HTTP 与 stdio MCP 服务名不能重复：{', '.join(collisions[:5])}",
        )
    if len(endpoints) + len(parsed_stdio_servers) > 20:
        raise HTTPException(status_code=422, detail="HTTP 与 stdio MCP 服务合计最多 20 个。")
    normalized_stdio_servers = {
        name: {
            "command": server.command,
            "args": list(server.args),
            **({"cwd": server.cwd} if server.cwd else {}),
            "timeout_seconds": server.timeout_seconds,
        }
        for name, server in parsed_stdio_servers.items()
    }
    if set(payload.tts_voices) - set(CHARACTERS):
        raise HTTPException(status_code=422, detail="语音角色配置包含未知角色。")
    voices = {
        character_id: voice.strip()
        for character_id, voice in payload.tts_voices.items()
        if voice.strip()
    }
    if any(len(voice) > 100 or not re.fullmatch(r"[\w.-]+", voice) for voice in voices.values()):
        raise HTTPException(status_code=422, detail="语音名称格式无效。")
    headers_value: str | None = None
    if payload.mcp_headers is not None:
        clean_headers: dict[str, dict[str, str]] = {}
        for server, headers in payload.mcp_headers.items():
            if server not in endpoints or len(headers) > 30:
                raise HTTPException(status_code=422, detail="MCP 请求头必须属于已配置服务且每项不超过 30 个。")
            clean_headers[server] = {}
            for name, value in headers.items():
                name = name.strip()
                value = value.strip()
                if (
                    not name
                    or name.lower()
                    in app.state.core.capabilities.http.FORBIDDEN_REQUEST_HEADERS
                    or len(name) > 100
                    or len(value) > 4096
                    or "\n" in name + value
                    or "\r" in name + value
                    or "\0" in name + value
                ):
                    raise HTTPException(status_code=422, detail="MCP 请求头格式无效。")
                clean_headers[server][name] = value
        headers_value = json.dumps(clean_headers, ensure_ascii=False)

    allowed_write_dirs: list[str] = []
    home = Path.home().resolve()
    for raw_path in payload.allowed_write_dirs:
        if not raw_path.strip():
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise HTTPException(status_code=422, detail="Agent 授权目录必须使用绝对路径。")
        resolved = path.resolve()
        if resolved in {Path("/").resolve(), home, home.parent}:
            raise HTTPException(status_code=422, detail="不能把系统根目录、用户主目录或用户目录根节点整体授权给 Agent。")
        if not resolved.is_dir():
            raise HTTPException(status_code=422, detail=f"Agent 授权目录不存在：{resolved}")
        normalized = str(resolved)
        if normalized not in allowed_write_dirs:
            allowed_write_dirs.append(normalized)

    try:
        system_tasks = {
            name: spec.public()
            for name, spec in parse_system_tasks(payload.system_tasks).items()
        }
    except SystemTaskConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        ZoneInfo(payload.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="提醒时区不是有效的 IANA 时区。") from exc
    cost_currency = payload.cost_currency.strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,12}", cost_currency):
        raise HTTPException(status_code=422, detail="费用币种只能包含字母、数字、点、横线和下划线。")

    values: dict[str, str | None] = {
        "OPENAI_BASE_URL": base_url,
        "OPENAI_MODEL": payload.openai_model,
        "OPENAI_FALLBACK_MODEL": payload.openai_fallback_model,
        "OPENAI_EMBEDDING_BASE_URL": embedding_base_url,
        "OPENAI_EMBEDDING_MODEL": embedding_model,
        "OPENAI_EMBEDDING_TIMEOUT_SECONDS": str(
            payload.openai_embedding_timeout_seconds
        ),
        "OPENAI_TIMEOUT_SECONDS": str(payload.openai_timeout_seconds),
        "YUKINO_MODEL_MAX_CONCURRENT": str(payload.model_max_concurrent),
        "YUKINO_MODEL_CIRCUIT_FAILURES": str(payload.model_circuit_failures),
        "YUKINO_MODEL_CIRCUIT_COOLDOWN_SECONDS": str(
            payload.model_circuit_cooldown_seconds
        ),
        "TTS_ENABLED": "true" if payload.tts_enabled else "false",
        "OPENAI_TTS_MODEL": payload.openai_tts_model,
        "YUKINO_MCP_ENDPOINTS": json.dumps(endpoints, ensure_ascii=False),
        "YUKINO_MCP_STDIO_ENABLED": (
            "true" if payload.mcp_stdio_enabled else "false"
        ),
        "YUKINO_MCP_STDIO_SERVERS": json.dumps(
            normalized_stdio_servers,
            ensure_ascii=False,
        ),
        "YUKINO_MCP_ALLOW_PRIVATE_HOSTNAMES": (
            "true" if payload.mcp_allow_private_hostnames else "false"
        ),
        "YUKINO_OUTBOUND_WEBHOOK": outbound_webhook,
        "YUKINO_BACKGROUND_REMINDER_DELIVERY": (
            "true" if payload.background_reminder_delivery else "false"
        ),
        "YUKINO_SMTP_HOST": payload.smtp_host,
        "YUKINO_SMTP_PORT": str(payload.smtp_port),
        "YUKINO_SMTP_USERNAME": payload.smtp_username,
        "YUKINO_SMTP_SENDER": payload.smtp_sender,
        "YUKINO_AGENT_ENABLED": "true" if payload.agent_enabled else "false",
        "YUKINO_AGENT_MAX_STEPS": str(payload.agent_max_steps),
        "YUKINO_AGENT_WORKERS": str(payload.agent_workers),
        "YUKINO_AGENT_QUEUE_CAPACITY": str(payload.agent_queue_capacity),
        "YUKINO_AGENT_SESSION_QUEUE_LIMIT": str(payload.agent_session_queue_limit),
        "YUKINO_TASK_WALL_TIME_SECONDS": str(payload.task_wall_time_seconds),
        "YUKINO_TASK_MODEL_TOKEN_LIMIT": str(payload.task_model_token_limit),
        "YUKINO_TASK_MODEL_COST_LIMIT": str(payload.task_model_cost_limit),
        "YUKINO_TASK_TOOL_CALL_LIMIT": str(payload.task_tool_call_limit),
        "YUKINO_TASK_MODEL_CALL_LIMIT": str(payload.task_model_call_limit),
        "YUKINO_MODEL_MAX_OUTPUT_TOKENS": str(payload.model_max_output_tokens),
        "YUKINO_INPUT_COST_PER_MILLION": str(payload.input_cost_per_million),
        "YUKINO_OUTPUT_COST_PER_MILLION": str(payload.output_cost_per_million),
        "YUKINO_COST_CURRENCY": cost_currency,
        "YUKINO_ALLOW_DESKTOP": "true" if payload.allow_desktop else "false",
        "YUKINO_ALLOWED_WRITE_DIRS": json.dumps(allowed_write_dirs, ensure_ascii=False),
        "YUKINO_SYSTEM_TASKS": json.dumps(system_tasks, ensure_ascii=False),
        "YUKINO_TIMEZONE": payload.timezone,
        "YUKINO_VECTOR_BACKEND": payload.vector_backend,
        "YUKINO_MILVUS_URI": milvus_uri,
        "YUKINO_MILVUS_DATABASE": payload.milvus_database,
        "YUKINO_MILVUS_COLLECTION_PREFIX": payload.milvus_collection_prefix,
        "YUKINO_GRAPH_BACKEND": payload.graph_backend,
        "YUKINO_NEO4J_URI": neo4j_uri,
        "YUKINO_NEO4J_USERNAME": payload.neo4j_username,
        "YUKINO_NEO4J_DATABASE": payload.neo4j_database,
    }
    for character_id, character in CHARACTERS.items():
        values[f"YUKINO_TTS_VOICE_{character_id.upper()}"] = voices.get(character_id, "alloy")
    if payload.openai_api_key is not None and payload.openai_api_key.strip():
        values["OPENAI_API_KEY"] = payload.openai_api_key
    if (
        payload.openai_embedding_api_key is not None
        and payload.openai_embedding_api_key.strip()
    ):
        values["OPENAI_EMBEDDING_API_KEY"] = payload.openai_embedding_api_key
    if payload.smtp_password is not None and payload.smtp_password.strip():
        values["YUKINO_SMTP_PASSWORD"] = payload.smtp_password
    if payload.outbound_webhook_secret is not None and payload.outbound_webhook_secret:
        values["YUKINO_OUTBOUND_WEBHOOK_SECRET"] = payload.outbound_webhook_secret
    if payload.inbound_webhook_secret is not None and payload.inbound_webhook_secret:
        values["YUKINO_INBOUND_WEBHOOK_SECRET"] = payload.inbound_webhook_secret
    if headers_value is not None:
        values["YUKINO_MCP_HEADERS"] = headers_value
    if payload.mcp_stdio_env is not None:
        values["YUKINO_MCP_STDIO_ENV"] = json.dumps(
            effective_stdio_environment,
            ensure_ascii=False,
        )
    if payload.milvus_token is not None and payload.milvus_token:
        values["YUKINO_MILVUS_TOKEN"] = payload.milvus_token
    if payload.neo4j_password is not None and payload.neo4j_password:
        values["YUKINO_NEO4J_PASSWORD"] = payload.neo4j_password
    clear: set[str] = set()
    if payload.clear_openai_api_key:
        clear.add("OPENAI_API_KEY")
    if payload.clear_openai_embedding_api_key:
        clear.add("OPENAI_EMBEDDING_API_KEY")
    if payload.clear_smtp_password:
        clear.add("YUKINO_SMTP_PASSWORD")
    if payload.clear_outbound_webhook_secret:
        clear.add("YUKINO_OUTBOUND_WEBHOOK_SECRET")
    if payload.clear_inbound_webhook_secret:
        clear.add("YUKINO_INBOUND_WEBHOOK_SECRET")
    if payload.clear_mcp_headers:
        clear.add("YUKINO_MCP_HEADERS")
    if payload.clear_mcp_stdio_env:
        clear.add("YUKINO_MCP_STDIO_ENV")
    if payload.clear_milvus_token:
        clear.add("YUKINO_MILVUS_TOKEN")
    if payload.clear_neo4j_password:
        clear.add("YUKINO_NEO4J_PASSWORD")
    app.state.core.reload_external_configuration(
        before_reload=lambda: app.state.runtime_config.update(values, clear=clear)
    )
    app.state.agent_worker.resize(payload.agent_workers)
    return {"ok": True, "settings": _settings_snapshot()}


@app.post("/api/settings/test")
# 作用：执行“test_runtime_settings”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def test_runtime_settings(payload: SettingsTestRequest) -> dict:
    if payload.area == "model":
        model = app.state.core.model
        probe = getattr(model, "probe", None)
        if not callable(probe):
            return {
                "ok": False,
                "area": "model",
                "message": "当前模型适配器不支持统一连接探测。",
            }
        selected = (
            str(getattr(model, "fallback_model", "") or "").strip()
            if payload.model_role == "fallback"
            else str(getattr(model, "model", "") or "").strip()
        )
        if not selected:
            return {
                "ok": False,
                "area": "model",
                "model_role": payload.model_role,
                "message": "尚未配置备用模型。",
            }
        result = probe(selected)
        return {
            **result,
            "area": "model",
            "model_role": payload.model_role,
            "message": _model_probe_message(result),
        }
    if payload.area == "voice":
        from service_club.core.conversation.characters import get_character
        from service_club.core.conversation.postprocess import synthesize_tts_optional

        audio_url = synthesize_tts_optional(
            "这里是侍奉部语音设置测试。",
            get_character(payload.character),
            "neutral",
            force=True,
        )
        return {
            "ok": bool(audio_url),
            "area": "voice",
            "message": "语音生成成功。" if audio_url else "语音生成失败，请检查 API Key、Base URL、模型和声音名称。",
            "audio_url": audio_url,
        }
    if payload.area == "mcp":
        configured_servers = {
            **app.state.core.capabilities._mcp_endpoints(),
            **app.state.core.capabilities._mcp_stdio_servers(),
        }
        server = payload.server.strip() or next(iter(configured_servers), "")
        if not server:
            return {"ok": False, "area": "mcp", "message": "尚未配置 MCP 服务。"}
        result = app.state.core.capabilities.execute(
            "mcp",
            "call",
            {"server": server, "method": "tools/list", "params": {}},
            confirmed=True,
        )
        return {"area": "mcp", "message": "MCP tools/list 调用成功。" if result["ok"] else result.get("error", "调用失败"), **result}
    if payload.area == "storage":
        vector = (
            app.state.core.vector_store.status(probe=True)
            if app.state.core.vector_store is not None
            else {
                "ok": True,
                "enabled": False,
                "backend": "relational",
                "connection_state": "disabled",
            }
        )
        graph = app.state.core.capabilities.knowledge_graph.status(probe=True)
        ok = bool(vector.get("ok")) and bool(graph.get("ok"))
        return {
            "ok": ok,
            "area": "storage",
            "message": (
                "关系事实库、向量索引与知识图谱检查通过。"
                if ok
                else "数据后端连接失败，请检查 Milvus 或 Neo4j 配置。"
            ),
            "vector": vector,
            "graph": graph,
        }
    result = app.state.core.capabilities.execute("mail", "status", {})
    configured = bool(result.get("result", {}).get("configured")) if result.get("ok") else False
    return {"ok": configured, "area": "mail", "message": "SMTP 配置完整。" if configured else "SMTP 配置不完整。", "result": result}


@app.post("/api/settings/stickers")
# 作用：执行“upload_sticker”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def upload_sticker(payload: StickerUploadRequest) -> dict:
    filename = Path(payload.filename).name
    extension = Path(filename).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(status_code=422, detail="贴纸只支持 PNG、JPEG、WebP 或 GIF。")
    try:
        raw = base64.b64decode(payload.base64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="贴纸内容不是有效 Base64。") from exc
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="贴纸必须在 1 字节到 5 MiB 之间。")
    valid_magic = (
        raw.startswith(b"\x89PNG\r\n\x1a\n")
        or raw.startswith(b"\xff\xd8\xff")
        or raw.startswith((b"GIF87a", b"GIF89a"))
        or (len(raw) > 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
    )
    if not valid_magic:
        raise HTTPException(status_code=422, detail="文件内容不是受支持的图片格式。")
    profile = CHARACTERS[payload.character]
    directory = settings.sticker_dir / profile.sticker_pack / payload.emotion
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^\w.-]+", "-", Path(filename).stem).strip(".-")[:80] or "sticker"
    digest = hashlib.sha256(raw).hexdigest()[:10]
    target = directory / f"{safe_stem}-{digest}{extension}"
    target.write_bytes(raw)
    return {
        "ok": True,
        "path": str(target.relative_to(settings.sticker_dir)),
        "url": f"/assets/stickers/{target.relative_to(settings.sticker_dir).as_posix()}",
        "stickers": _sticker_status(),
    }


@app.get("/api/status")
# 作用：汇总当前组件的运行状态、配置和可观测信息。
def status() -> dict:
    return {
        **app.state.core.status(),
        "background_runtime": app.state.background_worker.status(),
        "agent_queue_runtime": app.state.agent_worker.status(),
        "agent_queue_admission": app.state.async_chat.status(),
        "reminder_delivery": app.state.reminder_dispatcher.status(),
        "inbound_channel_security": app.state.channel_security.status(),
    }


@app.get("/api/usage")
# 作用：执行“usage”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 days：调用方传入的days，用于本次处理。
def usage(days: int = 1) -> dict:
    return app.state.core.usage.summary(days=max(1, min(days, 365)))


@app.get("/api/app-manifest")
# 作用：执行“app_manifest”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def app_manifest() -> dict:
    """Expose AGI Yukino's own product contract to the frontend."""
    return {
        "product": "AGI Yukino",
        "runtime": "service_club",
        "api_version": "1",
        "modes": ["solo", "club"],
        "characters": list(CHARACTERS),
        "features": {
            "emotion": True,
            "memory": True,
            "conversation_history": True,
            "adaptive_routing": True,
            "proactive_care": True,
            "tools": True,
            "capability_hub": True,
            "capability_permission_policy": True,
            "session_scoped_temporary_grants": True,
            "atomic_permission_consumption": True,
            "workflow_permission_enforcement": True,
            "tool_output_provenance": True,
            "untrusted_tool_output_isolation": True,
            "taint_aware_side_effect_authorization": True,
            "prompt_injection_side_effect_freeze": True,
            "workspace": True,
            "python_lab": True,
            "strict_python_process_sandbox": True,
            "python_network_file_process_denial": True,
            "bounded_python_output": True,
            "pre_model_side_effect_completion_closes_tool_mode": True,
            "unsolicited_model_tool_calls_blocked": True,
            "verified_effects_survive_response_degradation": True,
            "grounded_code_analysis_fallback": True,
            "web_research": True,
            "dns_pinned_http_transport": True,
            "server_side_redirects_forbidden": True,
            "document_reader": True,
            "bounded_document_parsing": True,
            "office_archive_bomb_preflight": True,
            "mcp": True,
            "mcp_jsonrpc_response_validation": True,
            "plugins": True,
            "channels": ["web", "qq", "wechat", "webhook"],
            "signed_inbound_channels": True,
            "inbound_replay_protection": True,
            "websocket": True,
            "voice": True,
            "agent_loop": True,
            "code_analysis": True,
            "authorized_file_write": True,
            "durable_agent_tasks": True,
            "task_cancellation": True,
            "verified_atomic_writes": True,
            "file_rollback": True,
            "interrupted_task_recovery": True,
            "process_tree_cancellation": True,
            "redacted_task_audit": True,
            "network_retry_and_circuit_breaker": True,
            "model_wait_cancellation": True,
            "model_concurrency_limit": True,
            "durable_dependency_workflows": True,
            "workflow_resume_without_replay": True,
            "confirmation_contract_recheck": True,
            "scheduled_reminders": True,
            "leased_reminder_delivery": True,
            "recurring_reminders": True,
            "same_ledger_task_resume": True,
            "uncertain_side_effect_replay_guard": True,
            "durable_background_jobs": True,
            "background_reminder_webhook": True,
            "background_job_dead_letters": True,
            "durable_async_chat": True,
            "refresh_safe_agent_jobs": True,
            "concurrent_agent_workers": True,
            "per_session_agent_ordering": True,
            "agent_queue_backpressure": True,
            "job_lease_heartbeat": True,
            "durable_task_event_stream": True,
            "reconnectable_task_timeline": True,
            "persistent_task_resource_budget": True,
            "atomic_model_budget_reservation": True,
            "uncertain_provider_usage_accounting": True,
            "durable_model_loop_checkpoint": True,
            "structural_checkpoint_without_raw_prompts": True,
            "checkpoint_resume_context": True,
            "durable_side_effect_receipts": True,
            "automatic_local_effect_reconciliation": True,
            "crash_window_duplicate_prevention": True,
            "durable_external_dispatch_ledger": True,
            "stable_provider_idempotency_keys": True,
            "uncertain_external_dispatch_guard": True,
            "external_dispatch_review_ui": True,
            "manual_external_resolution": True,
            "completed_dispatch_reconciliation": True,
        },
    }


@app.get("/api/traces")
# 作用：执行“traces”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def traces() -> list[dict]:
    return [asdict(item) for item in app.state.core.traces.recent()]


@app.get("/api/reminders")
# 作用：执行“reminders”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def reminders(session_id: str = "default") -> list[dict]:
    return app.state.core.memory.list_reminders(session_id)


@app.post("/api/reminders/claim")
# 作用：执行“claim_reminders”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def claim_reminders(payload: ReminderClaimRequest) -> list[dict]:
    return app.state.core.memory.claim_due_reminders(payload.session_id)


@app.post("/api/reminders/ack")
# 作用：执行“acknowledge_reminder”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def acknowledge_reminder(payload: ReminderAckRequest) -> dict:
    acknowledged = app.state.core.memory.acknowledge_reminder(
        payload.session_id,
        payload.reminder_id,
        payload.claim_token,
    )
    return {"ok": acknowledged}


@app.post("/api/reminders/cancel")
# 作用：执行“cancel_reminder”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def cancel_reminder(payload: ReminderCancelRequest) -> dict:
    return {
        "ok": app.state.core.memory.cancel_reminder(
            payload.session_id,
            payload.reminder_id,
        )
    }


@app.get("/api/background/jobs")
# 作用：执行“background_jobs”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 status：调用方传入的status，用于本次处理。
# 参数 limit：返回结果的最大数量。
def background_jobs(
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    return [
        public_background_job(item)
        for item in app.state.core.background_jobs.list(
            session_id=session_id,
            status=status,
            limit=limit,
        )
    ]


@app.post("/api/background/jobs/{job_id}/cancel")
# 作用：执行“cancel_background_job”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 job_id：后台作业的稳定标识。
# 参数 payload：调用方传入的payload，用于本次处理。
def cancel_background_job(job_id: str, payload: BackgroundJobActionRequest) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="后台作业不存在。")
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="取消后台作业需要明确确认。")
    existing = app.state.core.background_jobs.get(job_id)
    if existing is None or existing.get("session_id") != payload.session_id:
        raise HTTPException(status_code=404, detail="后台作业不存在。")
    if existing.get("kind") == app.state.async_chat.job_kind:
        job = app.state.async_chat.cancel(job_id, session_id=payload.session_id)
    else:
        job = app.state.core.background_jobs.cancel(job_id, session_id=payload.session_id)
    if job and job.get("kind") == app.state.reminder_dispatcher.job_kind:
        app.state.reminder_dispatcher.release_dead_letter(job)
    elif job and job.get("kind") == "workflow.run":
        app.state.background_finalizer.finalize_workflow(
            job,
            {
                "id": str(job.get("payload", {}).get("workflow_run_id") or ""),
                "status": "cancelled",
                "steps": [],
                "error": "用户取消了后台工作流。",
            },
        )
    return {
        "ok": bool(job and job.get("status") == "cancelled"),
        "job": public_background_job(job) if job else None,
    }


@app.post("/api/background/jobs/{job_id}/retry")
# 作用：执行“retry_background_job”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 job_id：后台作业的稳定标识。
# 参数 payload：调用方传入的payload，用于本次处理。
def retry_background_job(job_id: str, payload: BackgroundJobActionRequest) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="后台作业不存在。")
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="重试死信作业需要明确确认。")
    existing = app.state.core.background_jobs.get(job_id)
    if existing is None or existing.get("session_id") != payload.session_id:
        raise HTTPException(status_code=404, detail="后台作业不存在。")
    if existing.get("kind") in {
        app.state.reminder_dispatcher.job_kind,
        app.state.async_chat.job_kind,
        "workflow.run",
    }:
        raise HTTPException(
            status_code=409,
            detail="这个作业可能包含副作用，死信不能自动重放；请从原 Agent 任务恢复。",
        )
    job = app.state.core.background_jobs.retry_dead_letter(job_id)
    return {
        "ok": bool(job and job.get("status") == "queued"),
        "job": public_background_job(job) if job else None,
    }


@app.get("/api/mental-state")
# 作用：执行“mental_state”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def mental_state(session_id: str = "default") -> dict:
    return app.state.core.mental_state_status(session_id)


@app.get("/api/permanent-memory")
# 作用：执行“permanent_memory”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def permanent_memory(session_id: str = "default") -> dict:
    return {
        "prompt_segment": app.state.core.permanent_memory.get_prompt_segment(session_id)
    }


@app.get("/api/memory-state")
# 作用：执行“memory_state”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 query：用于筛选、检索或匹配目标数据的查询词。
def memory_state(session_id: str = "default", query: str = "") -> dict:
    return app.state.core.memory_state(session_id, query)


@app.get("/api/memory-search")
# 作用：执行“memory_search”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 query：用于筛选、检索或匹配目标数据的查询词。
def memory_search(session_id: str = "default", query: str = "") -> list[dict]:
    return [
        hit.as_dict()
        for hit in app.state.core.memory_retriever.search(session_id, query, limit=20)
    ]


@app.post("/api/memory/forget")
# 作用：执行“forget_memory”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def forget_memory(payload: ForgetMemoryRequest) -> dict:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="forget_memory requires confirmation")
    if payload.kind == "local":
        if payload.memory_id is None:
            raise HTTPException(status_code=422, detail="删除对话记忆需要 memory_id。")
        deleted = app.state.core.memory.forget_by_id(payload.session_id, payload.memory_id)
    else:
        key = payload.key.strip()
        if not key:
            raise HTTPException(status_code=422, detail="删除长期记忆需要 key。")
        deleted = app.state.core.permanent_memory.forget_key(payload.session_id, key)
    if not deleted:
        raise HTTPException(status_code=404, detail="记忆不存在或已经删除。")
    return {"ok": True, "kind": payload.kind}


@app.get("/api/user-profile")
# 作用：执行“user_profile”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def user_profile(session_id: str = "default") -> dict:
    return app.state.core.user_profile(session_id)


@app.get("/api/profile-facts")
# 作用：执行“profile_facts”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def profile_facts(session_id: str = "default") -> list[dict]:
    return app.state.core.profile_fact_learner.list_all(session_id)


@app.get("/api/quality-cases")
# 作用：执行“quality_cases”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def quality_cases(session_id: str = "default") -> list[dict]:
    return app.state.core.memory.list_quality_cases(session_id)


@app.get("/api/interaction-learning")
# 作用：执行“interaction_learning”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def interaction_learning(session_id: str = "default") -> dict:
    return app.state.core.interaction_outcomes.list_all(session_id)


@app.get("/api/self-model")
# 作用：执行“self_model”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 character：角色标识，决定采用的角色配置或行为策略。
def self_model(
    session_id: str = "default", character: str | None = None
) -> dict:
    return app.state.core.self_model.history(session_id, character=character)


@app.post("/api/profile-facts/{fact_id}/revoke")
# 作用：执行“revoke_profile_fact”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 fact_id：调用方传入的fact_id，用于本次处理。
# 参数 payload：调用方传入的payload，用于本次处理。
def revoke_profile_fact(fact_id: int, payload: ProfileFactRevokeRequest) -> dict:
    return {"ok": app.state.core.profile_fact_learner.revoke(payload.session_id, fact_id)}


@app.get("/api/reflections")
# 作用：执行“reflections”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def reflections(session_id: str = "default") -> list[dict]:
    return app.state.core.memory.list_reflections(session_id)


@app.get("/api/companion-state")
# 作用：执行“companion_state”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def companion_state(session_id: str = "default") -> dict:
    return app.state.core.companion_state(session_id)


@app.get("/api/relationship-state")
# 作用：执行“relationship_state”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def relationship_state(session_id: str = "default") -> dict:
    return app.state.core.relationship_tracker.status(session_id=session_id)


@app.get("/api/nudges")
# 作用：执行“nudges”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def nudges(session_id: str = "default") -> list[dict]:
    return app.state.core.proactive_nudges.pending(session_id)


@app.post("/api/nudges/claim")
# 作用：执行“claim_nudges”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def claim_nudges(payload: NudgeClaimRequest) -> list[dict]:
    return app.state.core.proactive_nudges.claim_due(payload.session_id)


@app.get("/api/instincts")
# 作用：执行“instincts”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def instincts(session_id: str = "default") -> list[dict]:
    return app.state.core.behavior_instincts.list_all(session_id)


@app.get("/api/context-state")
# 作用：执行“context_state”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def context_state(session_id: str = "default") -> dict:
    return {
        "runtime": app.state.core.context_window.status(),
        "checkpoint": app.state.core.context_window.checkpoint(session_id),
    }


@app.post("/api/instincts/{instinct_id}/revoke")
# 作用：执行“revoke_instinct”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 instinct_id：调用方传入的instinct_id，用于本次处理。
# 参数 payload：调用方传入的payload，用于本次处理。
def revoke_instinct(instinct_id: int, payload: InstinctRevokeRequest) -> dict:
    return {
        "ok": app.state.core.behavior_instincts.revoke(payload.session_id, instinct_id)
    }


@app.get("/api/doctor")
# 作用：执行“doctor”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
def doctor() -> dict:
    return ServiceClubDoctor(app.state.core).run()


@app.post("/api/attachments")
# 作用：执行“upload_attachment”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def upload_attachment(payload: AttachmentUploadRequest) -> dict:
    try:
        encoded = payload.base64.split(",", 1)[-1]
        content = base64.b64decode(encoded, validate=True)
        return public_attachment(
            app.state.core.attachments.save(
                session_id=payload.session_id,
                filename=payload.filename,
                mime_type=payload.mime_type,
                content=content,
            )
        )
    except (AttachmentError, binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/attachments")
# 作用：执行“attachments”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def attachments(session_id: str) -> list[dict]:
    try:
        return [public_attachment(item) for item in app.state.core.attachments.list(session_id)]
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/attachments/{attachment_id}")
# 作用：执行“delete_attachment”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 attachment_id：调用方传入的attachment_id，用于本次处理。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def delete_attachment(attachment_id: str, session_id: str) -> dict:
    try:
        deleted = app.state.core.attachments.delete(session_id, attachment_id)
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="附件不存在。")
    return {"ok": True}


@app.post("/api/chat", response_model=ChatResponse)
# 作用：执行“chat”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def chat(payload: ChatRequest) -> ChatResponse:
    try:
        return app.state.core.process(payload)
    except AgentRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentTaskResumeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/chat/jobs", status_code=202)
# 作用：执行“enqueue_chat_job”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def enqueue_chat_job(payload: ChatRequest) -> dict:
    try:
        return app.state.async_chat.enqueue(payload)
    except AsyncChatOverloaded as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={
                "Retry-After": "2",
                "X-Agent-Queue-Scope": exc.scope,
            },
        ) from exc
    except AsyncChatError as exc:
        message = str(exc)
        status_code = 409 if "request_id" in message or "恢复" in message else 422
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.get("/api/chat/jobs/{job_id}")
# 作用：执行“chat_job”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 job_id：后台作业的稳定标识。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def chat_job(job_id: str, session_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="异步 Agent 作业不存在。")
    job = app.state.core.background_jobs.get(job_id)
    if (
        job is None
        or job.get("kind") != app.state.async_chat.job_kind
        or job.get("session_id") != session_id
    ):
        raise HTTPException(status_code=404, detail="异步 Agent 作业不存在。")
    return app.state.async_chat.public_job(job)


@app.post("/api/chat/jobs/{job_id}/cancel")
# 作用：执行“cancel_chat_job”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 job_id：后台作业的稳定标识。
# 参数 payload：调用方传入的payload，用于本次处理。
def cancel_chat_job(job_id: str, payload: BackgroundJobActionRequest) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="异步 Agent 作业不存在。")
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="停止异步 Agent 作业需要明确确认。")
    job = app.state.async_chat.cancel(job_id, session_id=payload.session_id)
    if job is None:
        raise HTTPException(status_code=404, detail="异步 Agent 作业不存在。")
    return {
        "ok": job.get("status") == "cancelled",
        "job": app.state.async_chat.public_job(job),
    }


@app.get("/api/agent/tasks")
# 作用：执行“agent_tasks”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 limit：返回结果的最大数量。
def agent_tasks(session_id: str, limit: int = 30) -> list[dict]:
    tasks = app.state.core.agent_tasks.list(session_id, limit=limit)
    return [
        {
            **task,
            "budget_status": app.state.core.agent_tasks.budget_status(str(task["id"])),
        }
        for task in tasks
    ]


@app.get("/api/agent/external-dispatches")
# 作用：执行“external_dispatches”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 status：调用方传入的status，用于本次处理。
# 参数 limit：返回结果的最大数量。
def external_dispatches(
    session_id: str,
    status: Literal[
        "prepared",
        "dispatching",
        "completed",
        "failed",
        "uncertain",
        "abandoned",
    ]
    | None = None,
    limit: int = 50,
) -> dict:
    items = app.state.core.tools.registry.external_dispatches.list_session(
        session_id,
        status=status or "",
        limit=limit,
    )
    public_items = [public_external_dispatch(item) for item in items]
    return {
        "items": public_items,
        "count": len(public_items),
        "uncertain": sum(item.get("status") == "uncertain" for item in public_items),
        "automatic_replay": False,
    }


@app.post("/api/agent/external-dispatches/{dispatch_id}/review")
# 作用：执行“review_external_dispatch”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 dispatch_id：调用方传入的dispatch_id，用于本次处理。
# 参数 payload：调用方传入的payload，用于本次处理。
def review_external_dispatch(
    dispatch_id: str,
    payload: ExternalDispatchReviewRequest,
) -> dict:
    if not re.fullmatch(r"dispatch-[a-f0-9]{20}", dispatch_id):
        raise HTTPException(status_code=404, detail="外发记录不存在。")
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="处理外发记录需要明确确认。")
    try:
        result = app.state.core.tools.registry.review_external_dispatch(
            session_id=payload.session_id,
            dispatch_id=dispatch_id,
            decision=payload.decision,
        )
    except ExternalDispatchReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    result["dispatch"] = public_external_dispatch(result["dispatch"])
    return result


@app.get("/api/agent/tasks/{task_id}")
# 作用：执行“agent_task”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 task_id：持久任务的稳定标识。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
def agent_task(task_id: str, session_id: str) -> dict:
    task = app.state.core.agent_tasks.get(task_id)
    if task is None or task.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="Agent 任务不存在。")
    return {
        **task,
        "budget_status": app.state.core.agent_tasks.budget_status(task_id),
        "events": app.state.core.agent_tasks.events(task_id, limit=100),
    }


@app.get("/api/agent/tasks/{task_id}/events")
# 作用：执行“agent_task_events”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 task_id：持久任务的稳定标识。
# 参数 session_id：会话的稳定标识，用于隔离记忆、权限和任务数据。
# 参数 after_id：调用方传入的after_id，用于本次处理。
# 参数 limit：返回结果的最大数量。
def agent_task_events(
    task_id: str,
    session_id: str,
    after_id: int = 0,
    limit: int = 100,
) -> dict:
    task = app.state.core.agent_tasks.get(task_id)
    if task is None or task.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="Agent 任务不存在。")
    events = app.state.core.agent_tasks.events(
        task_id,
        after_id=max(0, after_id),
        limit=max(1, min(limit, 200)),
    )
    return {
        "task_id": task_id,
        "events": events,
        "cursor": int(events[-1]["id"]) if events else max(0, after_id),
        "task": {
            **task,
            "budget_status": app.state.core.agent_tasks.budget_status(task_id),
        },
    }


@app.post("/api/agent/tasks/cancel")
# 作用：执行“cancel_agent_task”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def cancel_agent_task(payload: AgentTaskCancelRequest) -> dict:
    existing = app.state.core.agent_tasks.get_by_request(
        payload.session_id,
        payload.request_id,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Agent 任务尚未建立。")
    cancelled = app.state.core.agent_tasks.cancel(
        str(existing["id"]),
        session_id=payload.session_id,
    )
    if cancelled is not None:
        cancelled_operations = app.state.core.tools.registry.operations.cancel_by_task(
            str(existing["id"])
        )
        cancelled_jobs = app.state.core.background_jobs.cancel_by_task(
            str(existing["id"])
        )
        for job in cancelled_jobs:
            if job.get("kind") == "workflow.run":
                app.state.background_finalizer.finalize_workflow(
                    job,
                    {
                        "id": str(job.get("payload", {}).get("workflow_run_id") or ""),
                        "status": "cancelled",
                        "steps": [],
                        "error": "用户停止了后台任务。",
                    },
                )
        app.state.core.agent_tasks.resolve_waiting_step(
            str(existing["id"]),
            status="cancelled",
            error="用户停止了任务。",
        )
        cancelled = app.state.core.agent_tasks.mark(
            str(existing["id"]),
            status="cancelled",
            phase="cancelled",
            outcome={
                "status": "cancelled",
                "verified": False,
                "summary": "用户停止了任务，未执行的后续步骤已经取消。",
            },
        )
        return {
            "ok": True,
            "task": cancelled,
            "cancelled_operations": cancelled_operations,
            "cancelled_background_jobs": [job["id"] for job in cancelled_jobs],
        }
    return {"ok": False, "task": existing, "reason": "task_already_finished"}


@app.post("/api/channels/{channel}/messages", response_model=ChatResponse)
# 作用：执行“channel_message”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 channel：调用方传入的channel，用于本次处理。
# 参数 payload：调用方传入的payload，用于本次处理。
# 参数 request：调用方传入的request，用于本次处理。
async def channel_message(
    channel: str,
    payload: ChannelMessageRequest,
    request: Request,
) -> ChatResponse:
    if channel not in {"web", "qq", "wechat", "webhook"}:
        raise HTTPException(status_code=404, detail="unknown channel")
    event_id = ""
    if channel != "web":
        try:
            event_id = app.state.channel_security.verify(
                channel=channel,
                body=await request.body(),
                timestamp=request.headers.get(
                    app.state.channel_security.timestamp_header,
                    "",
                ),
                event_id=request.headers.get(
                    app.state.channel_security.event_header,
                    "",
                ),
                signature=request.headers.get(
                    app.state.channel_security.signature_header,
                    "",
                ),
            )
        except ChannelSecurityError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    session_id = payload.session_id.strip() or f"{channel}:{payload.user_id}"
    try:
        return app.state.core.process(
            ChatRequest(
                messages=[ChatMessage(role="user", content=payload.text)],
                chat_mode=payload.chat_mode,
                character=payload.character,
                session_id=session_id,
                request_id=payload.request_id or event_id,
            )
        )
    except AgentRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.websocket("/ws/runtime")
# 作用：执行“runtime_socket”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 websocket：调用方传入的websocket，用于本次处理。
async def runtime_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    subscriptions: dict[str, int] = {}
    try:
        await websocket.send_json({"event": "ready", "status": app.state.core.status()})
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=0.35,
                )
            except TimeoutError:
                message = ""
            if message == "ping":
                await websocket.send_json({"event": "pong"})
            elif message == "status":
                await websocket.send_json({"event": "status", "status": app.state.core.status()})
            elif message:
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict) and payload.get("type") == "subscribe_task":
                    task_id = str(payload.get("task_id") or "")
                    session_id = str(payload.get("session_id") or "")
                    task = app.state.core.agent_tasks.get(task_id)
                    if (
                        not re.fullmatch(r"task-[a-f0-9]{16}", task_id)
                        or task is None
                        or task.get("session_id") != session_id
                        or (task_id not in subscriptions and len(subscriptions) >= 8)
                    ):
                        await websocket.send_json(
                            {"event": "task_subscription_denied", "task_id": task_id}
                        )
                    else:
                        try:
                            cursor = max(0, int(payload.get("after_id") or 0))
                        except (TypeError, ValueError):
                            cursor = 0
                        subscriptions[task_id] = cursor
                        await websocket.send_json(
                            {
                                "event": "task_subscribed",
                                "task_id": task_id,
                                "cursor": cursor,
                                "task": task,
                            }
                        )
                elif isinstance(payload, dict) and payload.get("type") == "unsubscribe_task":
                    subscriptions.pop(str(payload.get("task_id") or ""), None)
                else:
                    await websocket.send_json(
                        {"event": "ignored", "message": message[:80]}
                    )
            for task_id, cursor in list(subscriptions.items()):
                events = app.state.core.agent_tasks.events(
                    task_id,
                    after_id=cursor,
                    limit=100,
                )
                if not events:
                    continue
                next_cursor = int(events[-1]["id"])
                subscriptions[task_id] = next_cursor
                task = app.state.core.agent_tasks.get(task_id)
                await websocket.send_json(
                    {
                        "event": "task_events",
                        "task_id": task_id,
                        "cursor": next_cursor,
                        "events": events,
                        "task": task,
                    }
                )
    except WebSocketDisconnect:
        return


@app.post("/api/memory/clear")
# 作用：执行“clear_memory”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 payload：调用方传入的payload，用于本次处理。
def clear_memory(payload: ClearMemoryRequest) -> dict:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="clear_memory requires confirmation")
    cleared = app.state.core.clear_memory(payload.session_id)
    app.state.core.conversations.reset(payload.session_id)
    return {"ok": True, **cleared}
