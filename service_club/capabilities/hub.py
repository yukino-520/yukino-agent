from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import platform
import re
import shutil
import signal
import smtplib
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from service_club.capabilities.catalog import CAPABILITIES, search_capabilities
from service_club.capabilities.mcp_catalog import McpToolCatalog
from service_club.capabilities.mcp_stdio import (
    McpStdioClient,
    McpStdioConfigurationError,
    McpStdioServer,
    parse_stdio_servers,
)
from service_club.capabilities.policy import (
    CapabilityPermissionDenied,
    CapabilityPolicyError,
    CapabilityPolicyStore,
)
from service_club.capabilities.python_lab import PythonLab
from service_club.capabilities.research import SafeWebResearch
from service_club.capabilities.safe_http import PinnedHttpClient, SafeHttpError
from service_club.capabilities.system_tasks import (
    SystemTaskConfigurationError,
    SystemTaskSpec,
    parse_system_tasks,
)
from service_club.capabilities.workflows import DurableWorkflowRunner, WorkflowError
from service_club.capabilities.workspace import SafeWorkspace
from service_club.core.memory.knowledge_graph import (
    KnowledgeGraphStore,
    configured_graph_projector,
)
from service_club.core.runtime.background_jobs import BackgroundJobStore
from service_club.core.runtime.network_resilience import (
    CircuitOpenError,
    NetworkCallCancelled,
    NetworkResilience,
)
from service_club.settings import PROJECT_ROOT
from service_club.storage.contracts import CapabilityPolicyRepository
from service_club.storage.migrations import consolidate_legacy_databases
from service_club.storage.relational import RelationalBackend, SQLiteRelationalBackend
from service_club.storage.sqlite import sqlite_status


# 作用：表示能力参数、确认或外部调用不满足网关约束。
# 参数：无。
class CapabilityError(ValueError):
    pass


# 作用：用线程锁和原子替换保存轻量能力状态。
# 参数：无。
class JsonStateStore:
    # 作用：绑定 JSON 状态文件并创建父目录和可重入锁。
    # 参数 path：目标文件、数据库或状态存储路径。
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # 作用：容错读取对象型 JSON 状态，损坏或缺失时返回空对象。
    # 参数：无。
    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = {}
            return value if isinstance(value, dict) else {}

    # 作用：在锁内修改状态，并通过临时文件原子落盘。
    # 参数 mutator：在事务锁内修改持久状态的回调。
    def update(self, mutator: Any) -> dict[str, Any]:
        with self._lock:
            state = self.read()
            result = mutator(state)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            return result


# 作用：作为非对话能力的统一权限、确认、执行和审计网关。
# 参数：无。
class CapabilityHub:
    """One auditable gateway for non-conversation product capabilities."""

    # 作用：装配工作区、网络、MCP、权限、工作流及知识图谱等能力后端。
    # 参数 data_dir：能力状态、工作区和本地数据所在目录。
    # 参数 background_jobs：用于排队长耗时工作的持久后台任务仓储。
    # 参数 database_path：能力账本使用的本地数据库路径。
    # 参数 backend：所使用的存储、执行后端或后端标识。
    # 参数 project_root：允许读取或执行项目资源的根目录。
    # 参数 migrate_legacy：初始化时是否把旧版分散账本合并到主库。
    def __init__(
        self,
        data_dir: str | Path,
        *,
        background_jobs: BackgroundJobStore | None = None,
        database_path: str | Path | None = None,
        backend: RelationalBackend | None = None,
        project_root: str | Path | None = None,
        migrate_legacy: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.project_root = Path(project_root or PROJECT_ROOT).expanduser().resolve()
        self.workspace = SafeWorkspace(self.data_dir / "workspace")
        self.python = PythonLab(self.workspace.root / "lab")
        self.network = NetworkResilience()
        self.research = SafeWebResearch(self.network)
        self.http = PinnedHttpClient()
        self.mcp_catalog = McpToolCatalog(self.data_dir / "mcp_tool_catalog.json")
        self.mcp_stdio = McpStdioClient()
        self.state = JsonStateStore(self.data_dir / "capability_state.json")
        self.database_path = Path(
            database_path or self.data_dir / "service_club.sqlite3"
        ).resolve()
        self.backend = backend or SQLiteRelationalBackend(self.database_path)
        self.knowledge_graph = KnowledgeGraphStore(
            self.backend,
            projector=configured_graph_projector(),
        )
        legacy_graph = self.state.read().get("knowledge_graph", {})
        self.knowledge_graph_migration = (
            self.knowledge_graph.import_legacy(legacy_graph)
            if legacy_graph and self.knowledge_graph.is_empty()
            else {
                "nodes": 0,
                "edges": 0,
                "skipped": "canonical_graph_not_empty" if legacy_graph else "no_legacy_graph",
            }
        )
        self.workflows = DurableWorkflowRunner(backend=self.backend)
        self.permission_policy: CapabilityPolicyRepository = CapabilityPolicyStore(
            backend=self.backend
        )
        self.storage_migration = (
            consolidate_legacy_databases(self.data_dir, self.database_path)
            if migrate_legacy and self.backend.name == "sqlite"
            else {
                "target": self.backend.location,
                "sources": [],
                "copied_rows": 0,
                "legacy_files_preserved": True,
                "skipped": (
                    "non_sqlite_backend"
                    if self.backend.name != "sqlite"
                    else "custom_memory_store"
                ),
            }
        )
        gateway_ids = {
            "tool_search",
            "workspace",
            "python",
            "web",
            "documents",
            "system",
            "hardware",
            "organizer",
            "knowledge_graph",
            "insights",
            "plugins",
            "mcp",
            "workflows",
            "mail",
            "media",
            "prompt_governance",
            "observability",
        }
        self._permission_targets = {
            item.id: set(item.operations)
            for item in CAPABILITIES
            if item.id in gateway_ids
        }
        self.background_jobs = background_jobs

    # 作用：结合运行配置、沙箱和权限规则生成能力可用清单。
    # 参数：无。
    def manifest(self) -> list[dict[str, object]]:
        python_sandbox = self.python.status()
        configured = {
            "web": True,
            "documents": True,
            "mcp": bool(self._mcp_endpoints() or self._mcp_stdio_servers()),
            "channels": bool(os.getenv("YUKINO_OUTBOUND_WEBHOOK", "").strip()),
            "media": bool(os.getenv("OPENAI_API_KEY", "").strip()),
            "mail": bool(os.getenv("YUKINO_SMTP_HOST", "").strip()),
        }
        denied = self.permission_policy.denied_rules()
        result = []
        for capability in CAPABILITIES:
            item = capability.as_dict()
            if capability.id == "python":
                item["sandbox"] = python_sandbox
                if not python_sandbox["ok"]:
                    item["state"] = "degraded"
                    item["requires"] = "可用的进程级 Python 沙箱"
            if capability.state == "configured":
                item["state"] = "ready" if configured.get(capability.id, False) else "degraded"
            if capability.id in self._permission_targets:
                if (capability.id, "*") in denied:
                    item["permission"] = "blocked"
                    item["state"] = "blocked"
                elif any(
                    (capability.id, action) in denied
                    for action in self._permission_targets[capability.id]
                ):
                    item["permission"] = "restricted"
                else:
                    item["permission"] = "allowed"
            result.append(item)
        return result

    # 作用：合并内置能力与 MCP 工具目录，返回最相关的候选项。
    # 参数 query：能力、文件、网页或图谱检索使用的查询文本。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def search(self, query: str, limit: int = 6) -> list[dict[str, object]]:
        manifest = {str(item["id"]): item for item in self.manifest()}
        native = [
            {
                **item,
                "state": manifest.get(str(item["id"]), {}).get(
                    "state", item.get("state", "ready")
                ),
                **(
                    {
                        "permission": manifest[str(item["id"])]["permission"]
                    }
                    if "permission" in manifest.get(str(item["id"]), {})
                    else {}
                ),
            }
            for item in search_capabilities(query, limit=limit)
        ]
        discovered = self.mcp_catalog.search(query, limit=limit)
        combined = [*native, *discovered]
        combined.sort(
            key=lambda item: (-float(item.get("score", 0)), str(item.get("id", "")))
        )
        return combined[: max(1, min(int(limit), 20))]

    # 作用：汇总确认绑定、系统任务、HTTP、MCP 和文档解析的边界防护。
    # 参数：无。
    def boundary_security_status(self) -> dict[str, Any]:
        configured_mcp = set(self._mcp_endpoints()) | set(self._mcp_stdio_servers())
        mcp_catalog = self.mcp_catalog.snapshot(configured_mcp)
        try:
            system_tasks = self._system_task_specs()
            system_tasks_error = ""
        except SystemTaskConfigurationError as exc:
            system_tasks = {}
            system_tasks_error = str(exc)[:240]
        return {
            "ok": True,
            "confirmation": {
                "durable": True,
                "execution_target_bound": True,
                "secret_values_fingerprinted": False,
                "file_overwrite_precondition": "sha256",
            },
            "system_tasks": {
                "configured_count": len(system_tasks),
                "configuration_valid": not system_tasks_error,
                "configuration_error": system_tasks_error,
                "shell_used": False,
                "working_directories_allowlisted": True,
                "timeout_bounded": True,
            },
            "http_transport": {
                "dns_resolution_pinned": True,
                "redirects_followed": False,
                "response_bytes_bounded": True,
                "unsafe_transport_headers_blocked": True,
            },
            "web": {
                "public_addresses_only": True,
                "default_ports_only": True,
                "fake_ip_dns_mode": os.getenv("YUKINO_ALLOW_FAKE_IP_DNS", "auto"),
            },
            "mcp": {
                "jsonrpc_response_validated": True,
                "request_id_matched": True,
                "discovered_server_count": mcp_catalog["server_count"],
                "discovered_tool_count": mcp_catalog["tool_count"],
                "remote_metadata_authoritative": False,
                "stdio_enabled": self._mcp_stdio_enabled(),
                "stdio_shell_used": False,
                "stdio_parent_environment_allowlisted": True,
                "stdio_application_secrets_inherited": False,
                "stdio_launch_requires_confirmation": True,
                "private_hostnames_opt_in": os.getenv(
                    "YUKINO_MCP_ALLOW_PRIVATE_HOSTNAMES", "false"
                ).lower()
                in {"1", "true", "yes", "on"},
            },
            "documents": {
                "source_bytes_limit": self.workspace.MAX_DOCUMENT_BYTES,
                "output_chars_limit": self.workspace.MAX_DOCUMENT_OUTPUT_CHARS,
                "office_archive_preflight": True,
                "office_uncompressed_bytes_limit": (
                    self.workspace.MAX_ARCHIVE_UNCOMPRESSED_BYTES
                ),
            },
        }

    # 作用：报告事实库、派生存储及各能力仓储实际使用的后端。
    # 参数：无。
    def storage_status(self) -> dict[str, Any]:
        backend_status = (
            sqlite_status(self.database_path)
            if self.backend.name == "sqlite"
            else {
                "ok": True,
                "backend": self.backend.name,
                "location": self.backend.location,
            }
        )
        return {
            **backend_status,
            "migration": self.storage_migration,
            "repository_backends": {
                "capability_policy": self.permission_policy.status().get(
                    "backend", "unknown"
                ),
                "workflow_runs": self.workflows.store.backend.name,
                "background_jobs": (
                    self.background_jobs.backend.name
                    if self.background_jobs is not None
                    else "unbound"
                ),
            },
            "knowledge_graph": self.knowledge_graph.status(),
            "production_target": "postgresql+pgvector",
            "scale_out_profile": "postgresql+milvus+neo4j",
            "postgres_runtime_switch_ready": True,
            "redis_role": "ephemeral_coordination_only",
            "milvus_role": "derived_semantic_index",
            "neo4j_role": "derived_graph_projection",
        }

    # 作用：生成全局能力开关、操作权限和会话临时授权快照。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def permission_snapshot(self, session_id: str = "") -> dict[str, Any]:
        denied = self.permission_policy.denied_rules()
        catalog = {item.id: item for item in CAPABILITIES}
        capabilities: list[dict[str, Any]] = []
        for capability_id, actions in self._permission_targets.items():
            definition = catalog[capability_id]
            capability_denied = (capability_id, "*") in denied
            capabilities.append(
                {
                    "id": capability_id,
                    "label": definition.label,
                    "group": definition.group,
                    "description": definition.description,
                    "enabled": not capability_denied,
                    "actions": [
                        {
                            "id": action,
                            "enabled": not capability_denied
                            and (capability_id, action) not in denied,
                            "directly_denied": (capability_id, action) in denied,
                        }
                        for action in definition.operations
                        if action in actions
                    ],
                }
            )
        return {
            "default": "allow",
            "capabilities": capabilities,
            "grants": self.permission_policy.list_session(session_id),
            "status": self.permission_policy.status(),
            "session_id": session_id,
        }

    # 作用：经确认后校验并批量更新能力启停规则。
    # 参数 rules：本次要批量应用的能力权限规则。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def update_permission_policy(
        self,
        rules: list[dict[str, Any]],
        *,
        confirmed: bool,
        session_id: str = "",
    ) -> dict[str, Any]:
        if not confirmed:
            raise CapabilityPolicyError("更改能力权限需要 confirmed=true。")
        if not rules or len(rules) > 100:
            raise CapabilityPolicyError("每次需要提交 1-100 条能力权限规则。")
        normalized: list[dict[str, Any]] = []
        for rule in rules:
            capability = str(rule.get("capability", "")).strip()
            action = str(rule.get("action", "")).strip()
            self._validate_permission_target(capability, action, allow_wildcard=True)
            normalized.append(
                {
                    "capability": capability,
                    "action": action,
                    "enabled": bool(rule.get("enabled", False)),
                }
            )
        self.permission_policy.set_many(normalized)
        return self.permission_snapshot(session_id)

    # 作用：经确认后发放绑定会话、有效期和次数的临时权限。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 ttl_seconds：授权或待确认操作保持有效的秒数。
    # 参数 uses：临时授权允许被消费的最大次数。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def create_temporary_grant(
        self,
        *,
        session_id: str,
        capability: str,
        action: str,
        ttl_seconds: int,
        uses: int,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise CapabilityPolicyError("发放临时授权需要 confirmed=true。")
        self._validate_permission_target(capability, action)
        return self.permission_policy.create_grant(
            session_id=session_id,
            capability=capability,
            action=action,
            ttl_seconds=ttl_seconds,
            uses=uses,
        )

    # 作用：经确认后撤销属于当前会话的临时授权。
    # 参数 grant_id：临时授权记录的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def revoke_temporary_grant(
        self,
        grant_id: str,
        *,
        session_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise CapabilityPolicyError("撤销临时授权需要 confirmed=true。")
        grant, changed = self.permission_policy.revoke(
            grant_id,
            session_id=session_id,
        )
        if not grant:
            raise CapabilityPolicyError("临时授权不存在或不属于当前会话。")
        return {"grant": grant, "revoked": changed}

    # 作用：执行最终权限判断，并可原子消费一次临时授权。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 consume：授权成功时是否扣减一次临时授权额度。
    def check_permission(
        self,
        capability: str,
        action: str,
        *,
        session_id: str = "",
        consume: bool = False,
    ) -> dict[str, Any]:
        if capability not in self._permission_targets:
            return {
                "allowed": True,
                "source": "unmanaged_internal",
                "consumed": False,
                "grant_id": "",
            }
        self._validate_permission_target(capability, action)
        decision = self.permission_policy.authorize(
            capability,
            action,
            session_id=session_id,
            consume=consume,
        )
        if not decision.get("allowed"):
            session_hint = (
                "请在设置中为当前会话发放临时授权。"
                if session_id
                else "当前调用没有可用的会话授权。"
            )
            raise CapabilityPermissionDenied(
                f"能力权限已禁止 {capability}.{action}。{session_hint}"
            )
        return decision

    # 作用：确认能力和操作确实属于网关管理范围。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 allow_wildcard：是否允许使用星号表示能力下的全部操作。
    def _validate_permission_target(
        self,
        capability: str,
        action: str,
        *,
        allow_wildcard: bool = False,
    ) -> None:
        actions = self._permission_targets.get(capability)
        if actions is None or (
            action not in actions and not (allow_wildcard and action == "*")
        ):
            raise CapabilityPolicyError("能力权限目标不存在。")

    # 作用：根据副作用类型判断本次调用是否必须显式确认。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def _validate_execution_confirmation(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool,
    ) -> None:
        if confirmed:
            self._validate_confirmation_binding(capability, action, arguments)
            return
        required = False
        if capability == "python" and action == "run":
            required = True
        elif capability == "workspace" and action == "write":
            required = bool(arguments.get("overwrite", False))
        elif capability == "system" and action == "run_task":
            required = True
        elif capability == "organizer" and action == "delete":
            required = True
        elif capability == "plugins" and action in {"enable", "invoke"}:
            required = True
        elif capability == "mcp" and action == "call":
            server = str(arguments.get("server", ""))
            required = server in self._mcp_stdio_servers() or str(
                arguments.get("method", "tools/list")
            ) in {
                "tools/call",
                "resources/read",
            }
        elif capability == "workflows" and action in {"run", "enqueue", "resume"}:
            required = True
        elif capability == "mail" and action == "send":
            required = True
        elif capability == "media" and action in {
            "analyze",
            "speak",
            "generate_image",
            "generate_video",
        }:
            required = True
        if required:
            raise CapabilityError(f"{capability}.{action} 需要 confirmed=true。")

    # 作用：为当前非密钥执行目标生成稳定指纹和用户确认摘要。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    def confirmation_binding(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, str | int]:
        """Bind a durable confirmation to the current non-secret execution target."""
        target: dict[str, Any] = {"capability": capability, "action": action}
        summary = f"{capability}.{action} 将访问外部服务或改变数据，需要确认。"
        if capability == "system" and action == "run_task":
            task_name = str(arguments.get("task", "")).strip()
            spec = self._system_task_specs().get(task_name)
            if spec is None:
                raise CapabilityError("系统任务未在 YUKINO_SYSTEM_TASKS 中配置。")
            target["task"] = spec.public()
            summary = (
                f"运行本机任务 {task_name}（目录：{spec.cwd}，"
                f"最长 {spec.timeout_seconds} 秒），需要确认。"
            )
        elif capability == "mcp" and action == "call":
            server = str(arguments.get("server", "")).strip()
            method = str(arguments.get("method", "tools/list")).strip()
            endpoints = self._mcp_endpoints()
            stdio_servers = self._mcp_stdio_servers()
            if server in stdio_servers:
                target["mcp"] = {
                    "server": server,
                    "method": method,
                    "transport": "stdio",
                    **stdio_servers[server].public(),
                }
                summary = (
                    f"启动本机 stdio MCP 服务 {server} 并执行 {method}，需要确认。"
                )
            elif server in endpoints:
                target["mcp"] = {
                    "server": server,
                    "method": method,
                    "transport": "http-jsonrpc",
                    "url": endpoints[server],
                    "header_names": sorted(self._mcp_headers(server)),
                }
                summary = f"调用 MCP 服务 {server} 的 {method}，需要确认。"
            else:
                raise CapabilityError("MCP 服务未配置。")
        elif capability == "mail" and action == "send":
            smtp = {
                "host": os.getenv("YUKINO_SMTP_HOST", "").strip(),
                "port": os.getenv("YUKINO_SMTP_PORT", "465").strip(),
                "username": os.getenv("YUKINO_SMTP_USERNAME", "").strip(),
                "sender": os.getenv("YUKINO_SMTP_SENDER", "").strip(),
            }
            target["smtp"] = smtp
            summary = (
                f"通过 {smtp['host'] or '未配置 SMTP'} 向 "
                f"{str(arguments.get('to', '')).strip() or '指定收件人'} 发送邮件，需要确认。"
            )
        elif capability == "plugins" and action in {"enable", "invoke"}:
            plugin_id = str(arguments.get("id", "")).strip()
            plugin = next(
                (
                    item
                    for item in self.state.read().get("plugins", [])
                    if isinstance(item, dict) and item.get("id") == plugin_id
                ),
                {},
            )
            target["plugin"] = {
                key: plugin.get(key)
                for key in ("id", "version", "endpoint", "permissions", "enabled")
            }
            summary = f"{action} 扩展 {plugin_id or '未指定扩展'}，需要确认。"
        elif capability == "media" and action in {
            "analyze",
            "speak",
            "generate_image",
            "generate_video",
        }:
            target["media"] = {
                "base_url": os.getenv("OPENAI_BASE_URL", "").strip(),
                "chat_model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                "vision_model": os.getenv("OPENAI_VISION_MODEL", "").strip(),
                "tts_model": os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
                "image_model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"),
                "video_endpoint": os.getenv("YUKINO_VIDEO_ENDPOINT", "").strip(),
            }
            summary = f"执行媒体能力 {action} 并向已配置服务发送内容，需要确认。"
        elif capability == "workflows" and action in {"run", "enqueue", "resume"}:
            target["workflow_dependencies"] = {
                "system_tasks": {
                    name: spec.public()
                    for name, spec in self._system_task_specs().items()
                },
                "mcp_endpoints": self._mcp_endpoints(),
                "mcp_stdio": {
                    name: server.public()
                    for name, server in self._mcp_stdio_servers().items()
                },
                "smtp": {
                    "host": os.getenv("YUKINO_SMTP_HOST", "").strip(),
                    "port": os.getenv("YUKINO_SMTP_PORT", "465").strip(),
                    "sender": os.getenv("YUKINO_SMTP_SENDER", "").strip(),
                },
            }
            summary = f"执行包含多步骤能力的工作流 {action}，需要确认。"
        encoded = json.dumps(
            target,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "version": 1,
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
            "summary": summary,
        }

    # 作用：核对持久确认指纹，拒绝确认后发生变化的执行目标。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    def _validate_confirmation_binding(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any],
    ) -> None:
        supplied = arguments.get("_agent_confirmation", {})
        if not isinstance(supplied, dict) or not supplied:
            return
        fingerprint = str(supplied.get("fingerprint", ""))
        if int(supplied.get("version", 0) or 0) != 1 or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ):
            raise CapabilityError("确认单执行目标指纹无效，请重新发起操作。")
        current = self.confirmation_binding(capability, action, arguments)
        if fingerprint != current["fingerprint"]:
            raise CapabilityError(
                "能力配置在确认后发生了变化；为避免执行到未经确认的新目标，"
                "本次操作已拒绝，请重新发起并确认。"
            )

    # 作用：依次执行权限预检、确认校验、权限消费和能力分派，并统一封装结果。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 _workflow_depth：当前调用的工作流嵌套层数，用于阻止递归编排。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def execute(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any] | None = None,
        *,
        confirmed: bool = False,
        session_id: str = "",
        _workflow_depth: int = 0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        arguments = arguments or {}
        started = time.perf_counter()
        try:
            self.check_permission(
                capability,
                action,
                session_id=session_id,
                consume=False,
            )
            self._validate_execution_confirmation(
                capability,
                action,
                arguments,
                confirmed=confirmed,
            )
            permission = self.check_permission(
                capability,
                action,
                session_id=session_id,
                consume=True,
            )
            result = self._dispatch(
                capability,
                action,
                arguments,
                confirmed=confirmed,
                workflow_depth=_workflow_depth,
                cancel_check=cancel_check,
                session_id=session_id,
            )
            workflow_failed = (
                capability == "workflows"
                and action in {"run", "resume"}
                and isinstance(result, dict)
                and result.get("status") != "completed"
            )
            return {
                "ok": not workflow_failed,
                "capability": capability,
                "action": action,
                "result": result,
                **(
                    {"error": str(result.get("error") or "工作流没有完整执行。")}
                    if workflow_failed
                    else {}
                ),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "permission": permission,
            }
        except Exception as exc:
            return {
                "ok": False,
                "capability": capability,
                "action": action,
                "error": str(exc),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }

    # 作用：从能力参数中提取外部分发审计元数据。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    @staticmethod
    # 作用：执行“dispatch_metadata”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 arguments：调用方传入的arguments，用于本次处理。
    def _dispatch_metadata(arguments: dict[str, Any]) -> dict[str, Any]:
        value = arguments.get("_agent_dispatch", {})
        return value if isinstance(value, dict) else {}

    # 作用：把分发 ID 和幂等键转换为安全的外部请求头。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    @classmethod
    # 作用：执行“dispatch_headers”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 arguments：调用方传入的arguments，用于本次处理。
    def _dispatch_headers(cls, arguments: dict[str, Any]) -> dict[str, str]:
        metadata = cls._dispatch_metadata(arguments)
        dispatch_id = str(metadata.get("dispatch_id", "")).strip()
        idempotency_key = str(metadata.get("idempotency_key", "")).strip()
        headers: dict[str, str] = {}
        if dispatch_id:
            headers["X-AGI-Dispatch-ID"] = dispatch_id[:120]
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:200]
        return headers

    # 作用：在调用外部提供方前标记是否已越过副作用边界。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 side_effecting：外部提供方调用是否可能产生不可逆副作用。
    @classmethod
    # 作用：执行“mark_provider_call_started”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 arguments：调用方传入的arguments，用于本次处理。
    # 参数 side_effecting：调用方传入的side_effecting，用于本次处理。
    def _mark_provider_call_started(
        cls,
        arguments: dict[str, Any],
        *,
        side_effecting: bool,
    ) -> None:
        metadata = cls._dispatch_metadata(arguments)
        if metadata:
            metadata["provider_call_started"] = True
            metadata["side_effecting"] = bool(side_effecting)

    # 作用：由工作流运行和步骤 ID 派生稳定分发标识与幂等键。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 step_id：工作流步骤的唯一标识。
    @staticmethod
    # 作用：执行“workflow_dispatch_metadata”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 run_id：调用方传入的run_id，用于本次处理。
    # 参数 step_id：调用方传入的step_id，用于本次处理。
    def _workflow_dispatch_metadata(run_id: str, step_id: str) -> dict[str, Any]:
        digest = hashlib.sha256(
            f"workflow:{run_id}:{step_id}".encode("utf-8")
        ).hexdigest()
        return {
            "dispatch_id": f"dispatch-workflow-{digest[:20]}",
            "idempotency_key": f"agi-yukino-{digest[:32]}",
            "attempt": 0,
            "provider_call_started": False,
            "side_effecting": False,
        }

    # 作用：按能力名称把已授权请求路由到具体实现。
    # 参数 capability：要授权、调用或记录的能力标识。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 workflow_depth：当前能力分派所处的工作流嵌套深度。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def _dispatch(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool,
        workflow_depth: int,
        cancel_check: Callable[[], bool] | None,
        session_id: str,
    ) -> Any:
        if capability == "tool_search" and action == "search":
            return self.search(str(arguments.get("query", "")), int(arguments.get("limit", 6)))
        if capability == "workspace":
            return self._workspace(action, arguments, confirmed)
        if capability == "python" and action == "run":
            if not confirmed:
                raise CapabilityError("运行代码需要 confirmed=true。")
            return self.python.run(str(arguments.get("code", "")))
        if capability == "web":
            if action == "fetch":
                return self.research.fetch(str(arguments.get("url", "")))
            if action == "search":
                return self.research.search(str(arguments.get("query", "")))
        if capability == "documents" and action == "read":
            return self.workspace.read_document(str(arguments.get("path", "")))
        if capability == "system" and action == "snapshot":
            return self._system_snapshot()
        if capability == "system" and action == "run_task":
            return self._run_system_task(arguments, confirmed, cancel_check=cancel_check)
        if capability == "hardware" and action == "status":
            return self._hardware_status()
        if capability == "organizer":
            return self._organizer(action, arguments, confirmed)
        if capability == "knowledge_graph":
            return self._knowledge_graph(action, arguments, confirmed)
        if capability == "insights":
            return self._insights(action, arguments, confirmed)
        if capability == "plugins":
            return self._plugins(action, arguments, confirmed, cancel_check=cancel_check)
        if capability == "mcp":
            return self._mcp(action, arguments, confirmed, cancel_check=cancel_check)
        if capability == "workflows":
            return self._workflows(
                action,
                arguments,
                confirmed,
                workflow_depth,
                cancel_check=cancel_check,
                session_id=session_id,
            )
        if capability == "mail":
            return self._mail(action, arguments, confirmed, cancel_check=cancel_check)
        if capability == "media":
            return self._media(action, arguments, confirmed, cancel_check=cancel_check)
        if capability == "prompt_governance":
            return self._prompt_governance(action, arguments)
        if capability == "observability" and action == "status":
            return {
                "available": True,
                "source": "service_club",
                "message": "状态由原生 doctor、质量审计与网络熔断器提供。",
                "network": self.network.status(),
                "boundary_security": self.boundary_security_status(),
                "storage": self.storage_status(),
            }
        raise CapabilityError(f"未知能力操作：{capability}.{action}")

    # 作用：分派工作区读写操作，并对覆盖写入执行二次确认。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def _workspace(self, action: str, arguments: dict[str, Any], confirmed: bool) -> Any:
        path = str(arguments.get("path", "."))
        if action == "list":
            return self.workspace.list(path)
        if action == "read":
            return self.workspace.read(path)
        if action == "search":
            return self.workspace.search(str(arguments.get("query", "")), path)
        if action == "write":
            overwrite = bool(arguments.get("overwrite", False))
            if overwrite and not confirmed:
                raise CapabilityError("覆盖文件需要 confirmed=true。")
            return self.workspace.write(path, str(arguments.get("content", "")), overwrite=overwrite)
        raise CapabilityError(f"未知文件操作：{action}")

    # 作用：读取平台、负载、磁盘及已配置本机任务概况。
    # 参数：无。
    def _system_snapshot(self) -> dict[str, Any]:
        disk = shutil.disk_usage(self.data_dir)
        load_average: tuple[float, ...] | tuple[()] = ()
        try:
            load_average = os.getloadavg()
        except OSError:
            pass
        try:
            configured_tasks = self._system_task_specs()
        except CapabilityError:
            configured_tasks = {}
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "load_average": list(load_average),
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
            "data_dir": str(self.data_dir),
            "configured_tasks": sorted(configured_tasks),
            "task_profiles": {
                name: {
                    "cwd": spec.cwd,
                    "timeout_seconds": spec.timeout_seconds,
                    "description": spec.description,
                }
                for name, spec in configured_tasks.items()
            },
        }

    # 作用：解析环境变量中的本机任务白名单并统一转换配置错误。
    # 参数：无。
    @staticmethod
    # 作用：执行“system_task_specs”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _system_task_specs() -> dict[str, SystemTaskSpec]:
        try:
            raw = json.loads(os.getenv("YUKINO_SYSTEM_TASKS", "{}"))
            return parse_system_tasks(raw)
        except (json.JSONDecodeError, SystemTaskConfigurationError) as exc:
            raise CapabilityError(str(exc)) from exc

    # 作用：确认后无 Shell 启动白名单命令，支持超时、取消和输出限长。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _run_system_task(
        self,
        arguments: dict[str, Any],
        confirmed: bool,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise CapabilityError("运行系统任务需要 confirmed=true。")
        task_name = str(arguments.get("task", "")).strip()
        spec = self._system_task_specs().get(task_name)
        if spec is None:
            raise CapabilityError("系统任务未在 YUKINO_SYSTEM_TASKS 中配置。")
        requested_timeout = max(
            1,
            min(int(arguments.get("timeout", spec.timeout_seconds)), 120),
        )
        timeout_seconds = min(requested_timeout, spec.timeout_seconds)
        working_directory = spec.working_directory(
            workspace_root=self.workspace.root,
            project_root=self.project_root,
        )
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            self._mark_provider_call_started(arguments, side_effecting=True)
            process = subprocess.Popen(
                list(spec.command),
                cwd=working_directory,
                env={"PATH": os.getenv("PATH", "/usr/bin:/bin")},
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name != "nt",
            )
            while process.poll() is None:
                if cancel_check is not None and cancel_check():
                    self._stop_process_tree(process)
                    raise CapabilityError("系统任务已按用户要求停止。")
                if time.monotonic() - started >= timeout_seconds:
                    self._stop_process_tree(process)
                    raise CapabilityError(f"系统任务超过 {timeout_seconds} 秒，已终止整个子进程组。")
                time.sleep(0.05)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")[-20_000:]
            stderr = stderr_file.read().decode("utf-8", errors="replace")[-8_000:]
        return {
            "task": task_name,
            "task_fingerprint": spec.fingerprint(),
            "cwd": spec.cwd,
            "working_directory": str(working_directory),
            "timeout_seconds": timeout_seconds,
            "returncode": int(process.returncode or 0),
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # 作用：先温和后强制终止任务的整个子进程组。
    # 参数 process：需要终止并回收的子进程。
    @staticmethod
    # 作用：执行“stop_process_tree”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 process：调用方传入的process，用于本次处理。
    def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=1)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 作用：借助 psutil 读取 CPU、内存、磁盘、电池和传感器信息。
    # 参数：无。
    def _hardware_status(self) -> dict[str, Any]:
        try:
            import psutil
        except ImportError as exc:
            raise CapabilityError("硬件状态需要安装 psutil。") from exc
        battery = psutil.sensors_battery()
        temperatures = {}
        try:
            temperatures = {
                name: [{"label": item.label, "current": item.current, "high": item.high} for item in values]
                for name, values in psutil.sensors_temperatures().items()
            }
        except (AttributeError, OSError):
            pass
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory": psutil.virtual_memory()._asdict(),
            "disk": psutil.disk_usage(str(self.data_dir))._asdict(),
            "battery": battery._asdict() if battery else None,
            "temperatures": temperatures,
            "gpio": {"available": Path("/dev/gpiomem").exists()},
            "i2c": {"available": any(Path("/dev").glob("i2c-*")) if Path("/dev").exists() else False},
        }

    # 作用：管理任务、笔记和计划，并为增删改应用确认与幂等规则。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def _organizer(self, action: str, arguments: dict[str, Any], confirmed: bool) -> Any:
        kind = str(arguments.get("kind", "tasks"))
        if kind not in {"tasks", "notes", "schedules"}:
            raise CapabilityError("kind 只能是 tasks、notes 或 schedules。")
        if action == "list":
            return [
                self._public_organizer_item(item)
                for item in self.state.read().get(kind, [])
                if isinstance(item, dict)
            ]
        if action == "add":
            content = str(arguments.get("content", "")).strip()
            if not content:
                raise CapabilityError("内容不能为空。")
            idempotency_key = str(arguments.get("_agent_idempotency_key", ""))[:128]

            # 作用：按幂等键复用或新增一条整理项目。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def add(state: dict[str, Any]) -> dict[str, Any]:
                if idempotency_key:
                    existing = next(
                        (
                            item
                            for item in state.get(kind, [])
                            if item.get("idempotency_key") == idempotency_key
                        ),
                        None,
                    )
                    if isinstance(existing, dict):
                        return dict(existing)
                item = {
                    "id": uuid.uuid4().hex[:12],
                    "content": content,
                    "due_at": arguments.get("due_at"),
                    "status": "open",
                    "created_at": time.time(),
                }
                if idempotency_key:
                    item["idempotency_key"] = idempotency_key
                state.setdefault(kind, []).append(item)
                return item

            return self._public_organizer_item(self.state.update(add))
        item_id = str(arguments.get("id", ""))
        if action == "complete":
            # 作用：查找整理项目并记录完成状态和时间。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def complete(state: dict[str, Any]) -> dict[str, Any]:
                for item in state.get(kind, []):
                    if item.get("id") == item_id:
                        item["status"] = "done"
                        item["completed_at"] = time.time()
                        return item
                raise CapabilityError("未找到对应项目。")

            return self.state.update(complete)
        if action == "delete":
            if not confirmed:
                raise CapabilityError("删除项目需要 confirmed=true。")

            # 作用：删除指定整理项目并返回实际删除数量。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def delete(state: dict[str, Any]) -> dict[str, Any]:
                before = len(state.get(kind, []))
                state[kind] = [item for item in state.get(kind, []) if item.get("id") != item_id]
                return {"deleted": before - len(state[kind])}

            return self.state.update(delete)
        raise CapabilityError(f"未知整理操作：{action}")

    # 作用：按幂等键查找已创建的任务副作用结果。
    # 参数 idempotency_key：确保重复请求复用同一结果的幂等键。
    def organizer_effect_by_idempotency(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        state = self.state.read()
        for item in state.get("tasks", []):
            if (
                isinstance(item, dict)
                and item.get("idempotency_key") == idempotency_key[:128]
            ):
                return dict(item)
        return None

    # 作用：移除内部幂等键后返回可公开的整理项目。
    # 参数 item：待转换为公开视图的整理项目。
    @staticmethod
    # 作用：执行“public_organizer_item”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 item：调用方传入的item，用于本次处理。
    def _public_organizer_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if key != "idempotency_key"
        }

    # 作用：管理扩展清单、启用状态及经授权的网络调用。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _plugins(
        self,
        action: str,
        arguments: dict[str, Any],
        confirmed: bool,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Any:
        if action == "list":
            return list(self.state.read().get("plugins", []))
        if action == "register":
            manifest = arguments.get("manifest")
            if not isinstance(manifest, dict) or not str(manifest.get("id", "")).strip():
                raise CapabilityError("扩展清单必须包含 id。")
            allowed = {"id", "name", "version", "description", "permissions", "endpoint"}
            clean = {key: manifest[key] for key in allowed if key in manifest}
            endpoint = str(clean.get("endpoint", ""))
            if endpoint and not endpoint.startswith(("http://", "https://")):
                raise CapabilityError("扩展 endpoint 只允许 HTTP/HTTPS。")
            clean.update({"enabled": False, "registered_at": time.time()})

            # 作用：拒绝重复 ID 后把清洗过的扩展清单写入状态。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def register(state: dict[str, Any]) -> dict[str, Any]:
                plugins = state.setdefault("plugins", [])
                if any(item.get("id") == clean["id"] for item in plugins):
                    raise CapabilityError("扩展已经注册。")
                plugins.append(clean)
                return clean

            return self.state.update(register)
        if action == "enable":
            if not confirmed:
                raise CapabilityError("启用扩展需要 confirmed=true。")
            plugin_id = str(arguments.get("id", ""))

            # 作用：切换指定扩展启用状态。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def enable(state: dict[str, Any]) -> dict[str, Any]:
                for plugin in state.get("plugins", []):
                    if plugin.get("id") == plugin_id:
                        plugin["enabled"] = bool(arguments.get("enabled", True))
                        return plugin
                raise CapabilityError("未找到对应扩展。")

            return self.state.update(enable)
        if action == "invoke":
            if not confirmed:
                raise CapabilityError("调用外部扩展需要 confirmed=true。")
            plugin_id = str(arguments.get("id", ""))
            plugin = next(
                (item for item in self.state.read().get("plugins", []) if item.get("id") == plugin_id),
                None,
            )
            if not plugin or not plugin.get("enabled"):
                raise CapabilityError("扩展不存在或尚未启用。")
            if "network" not in plugin.get("permissions", []):
                raise CapabilityError("扩展未声明 network 权限。")
            endpoint = str(plugin.get("endpoint", ""))
            if not endpoint:
                raise CapabilityError("扩展没有调用 endpoint。")
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(arguments.get("input", {})).encode(),
                headers={
                    "Content-Type": "application/json",
                    **self._dispatch_headers(arguments),
                },
                method="POST",
            )
            # 作用：调用已启用且声明网络权限的外部扩展端点。
            # 参数：无。
            def invoke() -> bytes:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read(2 * 1024 * 1024 + 1)

            self._mark_provider_call_started(arguments, side_effecting=True)
            raw = self._network_call(
                f"plugin:{plugin_id}",
                invoke,
                safe_to_retry=False,
                cancel_check=cancel_check,
            )
            if len(raw) > 2 * 1024 * 1024:
                raise CapabilityError("扩展响应超过 2 MiB 限制。")
            return json.loads(raw.decode("utf-8"))
        raise CapabilityError(f"未知扩展操作：{action}")

    # 作用：按会话分派图谱查询、文本观察及实体关系写入。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def _knowledge_graph(self, action: str, arguments: dict[str, Any], confirmed: bool) -> Any:
        del confirmed
        session_id = str(arguments.get("session_id", "default"))
        if action == "list":
            return self.knowledge_graph.list(session_id)
        if action == "search":
            return self.knowledge_graph.search(
                session_id,
                str(arguments.get("query", "")),
            )
        if action == "observe":
            text = str(arguments.get("text", ""))
            patterns = (
                (r"我(?:很|最)?喜欢([^，。！？]{1,30})", "likes", "preference"),
                (r"我(?:不喜欢|讨厌)([^，。！？]{1,30})", "dislikes", "preference"),
                (r"我住在([^，。！？]{1,30})", "lives_in", "place"),
                (r"我在([^，。！？]{1,30}?)(?:工作|上班)", "works_at", "organization"),
                (r"我希望你?([^，。！？]{1,30})", "prefers_response", "preference"),
                (r"我是(?:一名|一个)?([^，。！？]{1,30})", "is", "identity"),
            )
            observed = []
            for pattern, relation, entity_type in patterns:
                for match in re.finditer(pattern, text):
                    target = match.group(1).strip()
                    if target:
                        observed.append(
                            self.knowledge_graph.upsert_relation(
                                session_id,
                                "用户",
                                "person",
                                relation,
                                target,
                                entity_type,
                                confidence=0.78,
                                source_kind="conversation_observation",
                                evidence_hash=hashlib.sha256(
                                    text.encode("utf-8")
                                ).hexdigest(),
                            )
                        )
            return {"observed": observed}
        if action == "add_entity":
            label = str(arguments.get("label", "")).strip()
            entity_type = str(arguments.get("type", "concept")).strip()
            if not label:
                raise CapabilityError("实体名称不能为空。")
            return self._upsert_entity(session_id, label, entity_type)
        if action == "add_relation":
            relation = str(arguments.get("relation", "related_to")).strip()
            source = str(arguments.get("source", "")).strip()
            target = str(arguments.get("target", "")).strip()
            if not all((source, relation, target)):
                raise CapabilityError("关系需要 source、relation 和 target。")
            return self._upsert_relation(
                session_id,
                source,
                str(arguments.get("source_type", "concept")),
                relation,
                target,
                str(arguments.get("target_type", "concept")),
            )
        raise CapabilityError(f"未知知识图谱操作：{action}")

    # 作用：向规范知识图谱事实库幂等写入实体。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 label：展示报告所用标签或知识图谱实体名称。
    # 参数 entity_type：知识图谱实体的类型。
    def _upsert_entity(self, session_id: str, label: str, entity_type: str) -> dict[str, Any]:
        return self.knowledge_graph.upsert_entity(session_id, label, entity_type)

    # 作用：向规范知识图谱事实库幂等写入实体关系。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 source_label：知识图谱关系起点实体名称。
    # 参数 source_type：知识图谱关系起点实体类型。
    # 参数 relation：连接源实体和目标实体的关系名称。
    # 参数 target_label：知识图谱关系终点实体名称。
    # 参数 target_type：知识图谱关系终点实体类型。
    def _upsert_relation(
        self,
        session_id: str,
        source_label: str,
        source_type: str,
        relation: str,
        target_label: str,
        target_type: str,
    ) -> dict[str, Any]:
        return self.knowledge_graph.upsert_relation(
            session_id,
            source_label,
            source_type,
            relation,
            target_label,
            target_type,
        )

    # 作用：管理错误、需求、模式和偏好等学习记录。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    def _insights(self, action: str, arguments: dict[str, Any], confirmed: bool) -> Any:
        if action == "list":
            values = list(self.state.read().get("insights", []))
            status = str(arguments.get("status", ""))
            return [item for item in values if not status or item.get("status") == status]
        if action == "record":
            content = str(arguments.get("content", "")).strip()
            category = str(arguments.get("category", "pattern"))
            if not content or category not in {"error", "feature_request", "pattern", "preference"}:
                raise CapabilityError("学习记录需要内容和有效 category。")

            # 作用：创建带分类、优先级和时间的学习记录。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def record(state: dict[str, Any]) -> dict[str, Any]:
                item = {
                    "id": uuid.uuid4().hex[:12],
                    "content": content,
                    "category": category,
                    "priority": int(arguments.get("priority", 2)),
                    "status": "open",
                    "created_at": time.time(),
                }
                state.setdefault("insights", []).append(item)
                return item

            return self.state.update(record)
        if action == "update":
            insight_id = str(arguments.get("id", ""))
            new_status = str(arguments.get("status", "resolved"))
            if new_status not in {"open", "planned", "resolved", "dismissed"}:
                raise CapabilityError("无效学习记录状态。")

            # 作用：更新指定学习记录的处理状态。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def update(state: dict[str, Any]) -> dict[str, Any]:
                for item in state.get("insights", []):
                    if item.get("id") == insight_id:
                        item["status"] = new_status
                        item["updated_at"] = time.time()
                        return item
                raise CapabilityError("未找到学习记录。")

            return self.state.update(update)
        raise CapabilityError(f"未知学习记录操作：{action}")

    # 作用：解析并过滤 HTTP MCP 服务端点配置。
    # 参数：无。
    def _mcp_endpoints(self) -> dict[str, str]:
        try:
            endpoints = json.loads(os.getenv("YUKINO_MCP_ENDPOINTS", "{}"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(endpoints, dict):
            return {}
        validated: dict[str, str] = {}
        for name, url in endpoints.items():
            value = str(url).strip()
            try:
                self.http.validate_url(value)
            except SafeHttpError:
                continue
            validated[str(name)] = value
        return validated

    # 作用：读取是否显式允许启动本地 MCP stdio 服务。
    # 参数：无。
    @staticmethod
    # 作用：执行“mcp_stdio_enabled”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _mcp_stdio_enabled() -> bool:
        return os.getenv("YUKINO_MCP_STDIO_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    # 作用：解析 stdio 服务白名单，并避免与 HTTP 服务重名。
    # 参数：无。
    def _mcp_stdio_servers(self) -> dict[str, McpStdioServer]:
        if not self._mcp_stdio_enabled():
            return {}
        try:
            raw_servers = json.loads(os.getenv("YUKINO_MCP_STDIO_SERVERS", "{}"))
            raw_environment = json.loads(os.getenv("YUKINO_MCP_STDIO_ENV", "{}"))
            servers = parse_stdio_servers(raw_servers, raw_environment)
        except (json.JSONDecodeError, McpStdioConfigurationError):
            return {}
        http_names = set(self._mcp_endpoints())
        return {name: server for name, server in servers.items() if name not in http_names}

    # 作用：读取指定 MCP 服务的安全请求头配置。
    # 参数 server：MCP 服务名称或其经过校验的配置。
    def _mcp_headers(self, server: str) -> dict[str, str]:
        try:
            configured = json.loads(os.getenv("YUKINO_MCP_HEADERS", "{}"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(configured, dict) or not isinstance(configured.get(server), dict):
            return {}
        return {
            str(name): str(value)
            for name, value in configured[server].items()
            if str(name).strip() and "\n" not in str(name) and "\n" not in str(value)
        }

    # 作用：列出或调用获准 MCP 服务，并严格校验 JSON-RPC 响应。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _mcp(
        self,
        action: str,
        arguments: dict[str, Any],
        confirmed: bool,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Any:
        endpoints = self._mcp_endpoints()
        stdio_servers = self._mcp_stdio_servers()
        configured_names = set(endpoints) | set(stdio_servers)
        if action == "list":
            discovered = self.mcp_catalog.snapshot(configured_names)["servers"]
            http_items = [
                {
                    "name": name,
                    "transport": "http-jsonrpc",
                    "url": url,
                    "discovered": discovered.get(
                        name,
                        {"count": 0, "tools": [], "discovered_at": 0},
                    ),
                }
                for name, url in endpoints.items()
            ]
            stdio_items = [
                {
                    "name": name,
                    "transport": "stdio",
                    **server.public(),
                    "discovered": discovered.get(
                        name,
                        {"count": 0, "tools": [], "discovered_at": 0},
                    ),
                }
                for name, server in stdio_servers.items()
            ]
            return [*http_items, *stdio_items]
        if action != "call":
            raise CapabilityError(f"未知 MCP 操作：{action}")
        server = str(arguments.get("server", ""))
        if server not in configured_names:
            raise CapabilityError("MCP 服务未配置。")
        method = str(arguments.get("method", "tools/list"))
        if method not in {"initialize", "tools/list", "tools/call", "resources/list", "resources/read"}:
            raise CapabilityError("MCP 方法不在允许列表。")
        if method in {"tools/call", "resources/read"} and not confirmed:
            raise CapabilityError("MCP 外部调用需要 confirmed=true。")
        if server in stdio_servers and not confirmed:
            raise CapabilityError("启动本机 stdio MCP 服务需要 confirmed=true。")
        dispatch = self._dispatch_metadata(arguments)
        request_id = str(dispatch.get("dispatch_id", "")).strip() or uuid.uuid4().hex[:8]
        params = arguments.get("params", {})
        if not isinstance(params, dict):
            raise CapabilityError("MCP params 必须是对象。")
        if server in stdio_servers:
            self._mark_provider_call_started(
                arguments,
                side_effecting=method == "tools/call",
            )
            result = self.mcp_stdio.invoke(
                stdio_servers[server],
                method=method,
                params=params,
                request_id=request_id,
                idempotency_key=str(dispatch.get("idempotency_key", "")),
                cancel_check=cancel_check,
            )
            if method == "tools/call" and bool(result.get("isError")):
                raise CapabilityError("MCP 工具返回 isError=true。")
            response_payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
                "transport": "stdio",
            }
            if method == "tools/list":
                response_payload["catalog"] = self.mcp_catalog.update(server, result)
            return response_payload
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            **self._mcp_headers(server),
            **self._dispatch_headers(arguments),
        }

        # 作用：对 HTTP MCP 端点发起一次固定地址、大小受限的请求。
        # 参数：无。
        def invoke() -> bytes:
            response = self.http.request(
                "POST",
                endpoints[server],
                headers=headers,
                body=payload,
                timeout_seconds=15,
                max_bytes=2 * 1024 * 1024,
                address_policy=self._mcp_address_allowed,
            )
            if 300 <= response.status < 400:
                raise CapabilityError("MCP 端点不允许 HTTP 重定向。")
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    endpoints[server],
                    response.status,
                    response.reason,
                    response.headers,
                    None,
                )
            return response.body

        self._mark_provider_call_started(
            arguments,
            side_effecting=method == "tools/call",
        )
        raw = self._network_call(
            f"mcp:{server}",
            invoke,
            safe_to_retry=method in {"tools/list", "resources/list", "resources/read"},
            cancel_check=cancel_check,
        )
        if len(raw) > 2 * 1024 * 1024:
            raise CapabilityError("MCP 响应超过 2 MiB 限制。")
        try:
            response_payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapabilityError("MCP 响应不是有效的 UTF-8 JSON。") from exc
        if not isinstance(response_payload, dict):
            raise CapabilityError("MCP 响应必须是 JSON-RPC 对象。")
        if response_payload.get("jsonrpc") != "2.0":
            raise CapabilityError("MCP 响应缺少 jsonrpc=2.0。")
        if response_payload.get("id") != request_id:
            raise CapabilityError("MCP 响应 ID 与请求不匹配。")
        has_result = "result" in response_payload
        has_error = "error" in response_payload
        if has_result == has_error:
            raise CapabilityError("MCP 响应必须且只能包含 result 或 error。")
        if has_error:
            error = response_payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code", "unknown"))[:40]
                message = str(error.get("message", "MCP 调用失败"))[:300]
            else:
                code = "unknown"
                message = "MCP 调用失败"
            raise CapabilityError(f"MCP 返回错误 {code}：{message}")
        result_payload = response_payload.get("result")
        if (
            method == "tools/call"
            and isinstance(result_payload, dict)
            and bool(result_payload.get("isError"))
        ):
            raise CapabilityError("MCP 工具返回 isError=true。")
        if method == "tools/list":
            response_payload["catalog"] = self.mcp_catalog.update(
                server,
                response_payload.get("result"),
            )
        return response_payload

    # 作用：按公网、Fake-IP、本机和显式私网策略审核 MCP 地址。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    # 参数 address：待审核或连接的目标 IP 地址。
    # 参数 is_literal：目标主机是否由调用方直接写成 IP 地址。
    def _mcp_address_allowed(
        self,
        hostname: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        is_literal: bool,
    ) -> bool:
        if address.is_unspecified or address.is_multicast:
            return False
        if address.is_global:
            return True
        if (
            address in SafeWebResearch.FAKE_IP_NETWORK
            and self.research.fake_ip_dns_active()
        ):
            return True
        if is_literal:
            return True
        if hostname in {"localhost", "localhost.localdomain"} and address.is_loopback:
            return True
        return os.getenv("YUKINO_MCP_ALLOW_PRIVATE_HOSTNAMES", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    # 作用：判断外部调用异常是否属于可安全重试的瞬时故障。
    # 参数 exc：待判断是否可重试的异常对象。
    @staticmethod
    # 作用：执行“retryable_network_error”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 exc：调用方传入的exc，用于本次处理。
    def _retryable_network_error(exc: Exception) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in {408, 425, 429, 500, 502, 503, 504}
        return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))

    # 作用：通过熔断器执行外部调用，并按副作用安全性限制重试次数。
    # 参数 key：网络熔断作用域或运行配置键。
    # 参数 operation：实际访问外部服务的可调用对象。
    # 参数 safe_to_retry：当前外部操作是否确认具备安全重试语义。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _network_call(
        self,
        key: str,
        operation: Callable[[], Any],
        *,
        safe_to_retry: bool,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Any:
        try:
            return self.network.call(
                key,
                operation,
                retry_if=(self._retryable_network_error if safe_to_retry else lambda exc: False),
                max_attempts=3 if safe_to_retry else 1,
                cancel_check=cancel_check,
            )
        except CircuitOpenError as exc:
            raise CapabilityError(f"外部服务暂时熔断：{exc}") from exc
        except NetworkCallCancelled as exc:
            raise CapabilityError(str(exc)) from exc

    # 作用：分派工作流查询、排队、运行与人工恢复操作。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 depth：当前递归深度，用于限制嵌套模式或结构。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    def _workflows(
        self,
        action: str,
        arguments: dict[str, Any],
        confirmed: bool,
        depth: int,
        *,
        cancel_check: Callable[[], bool] | None = None,
        session_id: str = "",
    ) -> Any:
        if depth > 0:
            raise CapabilityError("工作流不能递归调用工作流。")
        try:
            if action == "status":
                return self.workflows.get(str(arguments.get("run_id", "")))
            if action == "list":
                return self.workflows.list(limit=int(arguments.get("limit", 20)))

            if action == "enqueue":
                if not confirmed:
                    raise CapabilityError("后台排队工作流需要 confirmed=true。")
                if self.background_jobs is None:
                    raise CapabilityError("后台作业运行时尚未初始化。")
                context = arguments.get("_background_context", {})
                context = dict(context) if isinstance(context, dict) else {}
                source_task_id = str(context.get("source_task_id") or "")
                request_task_id = str(context.get("request_task_id") or "")
                session_id = str(context.get("session_id") or "")
                prepared = self.workflows.prepare(
                    arguments.get("steps", []),
                    confirmed=True,
                )
                run_id = str(prepared["id"])
                job = self.background_jobs.enqueue(
                    "workflow.run",
                    {
                        "workflow_run_id": run_id,
                        "source_task_id": source_task_id,
                        "request_task_id": request_task_id,
                    },
                    session_id=session_id,
                    task_id=source_task_id,
                    related_task_id=request_task_id,
                    dedupe_key=f"{source_task_id}:{request_task_id}:{run_id}",
                    max_attempts=2,
                )
                return {
                    **prepared,
                    "status": "queued",
                    "workflow_run_id": run_id,
                    "background_job_id": job["id"],
                    "background_job_status": job["status"],
                }

            # 作用：把工作流步骤转换为受同一能力网关约束的嵌套调用。
            # 参数 capability：要授权、调用或记录的能力标识。
            # 参数 step_action：工作流步骤要执行的能力操作。
            # 参数 step_arguments：工作流步骤解析后的能力参数。
            # 参数 step_confirmed：该步骤是否携带有效的副作用确认。
            def execute_step(
                capability: str,
                step_action: str,
                step_arguments: dict[str, Any],
                step_confirmed: bool,
            ) -> dict[str, Any]:
                step_arguments = self._prepare_workflow_step_arguments(
                    step_arguments
                )
                return self.execute(
                    capability,
                    step_action,
                    step_arguments,
                    confirmed=step_confirmed,
                    session_id=session_id,
                    _workflow_depth=depth + 1,
                    cancel_check=cancel_check,
                )

            if action == "run":
                return self.workflows.run(
                    arguments.get("steps", []),
                    executor=execute_step,
                    confirmed=confirmed,
                    cancel_check=cancel_check,
                )
            if action == "resume":
                return self.workflows.resume(
                    str(arguments.get("run_id", "")),
                    executor=execute_step,
                    confirmed=confirmed,
                    cancel_check=cancel_check,
                )
        except WorkflowError as exc:
            raise CapabilityError(str(exc)) from exc
        raise CapabilityError(f"未知工作流操作：{action}")

    # 作用：执行后台已持久化的新工作流，并沿用会话权限和取消信号。
    # 参数 run_id：工作流运行记录的唯一标识。
    # 参数 session_id：隔离会话数据、权限和任务的会话标识。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def execute_prepared_workflow(
        self,
        run_id: str,
        *,
        session_id: str = "",
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        # 作用：为后台流程步骤注入分发元数据后调用能力网关。
        # 参数 capability：要授权、调用或记录的能力标识。
        # 参数 step_action：工作流步骤要执行的能力操作。
        # 参数 step_arguments：工作流步骤解析后的能力参数。
        # 参数 step_confirmed：该步骤是否携带有效的副作用确认。
        def execute_step(
            capability: str,
            step_action: str,
            step_arguments: dict[str, Any],
            step_confirmed: bool,
        ) -> dict[str, Any]:
            step_arguments = self._prepare_workflow_step_arguments(step_arguments)
            return self.execute(
                capability,
                step_action,
                step_arguments,
                confirmed=step_confirmed,
                session_id=session_id,
                _workflow_depth=1,
                cancel_check=cancel_check,
            )

        try:
            return self.workflows.execute_prepared(
                run_id,
                executor=execute_step,
                cancel_check=cancel_check,
            )
        except WorkflowError as exc:
            raise CapabilityError(str(exc)) from exc

    # 作用：移除内部工作流上下文并注入稳定外部分发元数据。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    def _prepare_workflow_step_arguments(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        prepared = dict(arguments)
        context = prepared.pop("_agent_workflow_context", {})
        if isinstance(context, dict):
            run_id = str(context.get("run_id", ""))
            step_id = str(context.get("step_id", ""))
            if run_id and step_id:
                prepared["_agent_dispatch"] = self._workflow_dispatch_metadata(
                    run_id,
                    step_id,
                )
        return prepared

    # 作用：检查 SMTP 配置或经确认发送不可自动重试的邮件。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _mail(
        self,
        action: str,
        arguments: dict[str, Any],
        confirmed: bool,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Any:
        host = os.getenv("YUKINO_SMTP_HOST", "").strip()
        port = int(os.getenv("YUKINO_SMTP_PORT", "465"))
        username = os.getenv("YUKINO_SMTP_USERNAME", "").strip()
        password = os.getenv("YUKINO_SMTP_PASSWORD", "")
        sender = os.getenv("YUKINO_SMTP_SENDER", username).strip()
        if action == "status":
            return {"configured": bool(host and username and password and sender), "host": host, "sender": sender}
        if action != "send":
            raise CapabilityError(f"未知邮件操作：{action}")
        if not confirmed:
            raise CapabilityError("发送邮件需要 confirmed=true。")
        if not all((host, username, password, sender)):
            raise CapabilityError("SMTP 尚未配置。")
        recipient = str(arguments.get("to", "")).strip()
        subject = str(arguments.get("subject", "")).strip()
        body = str(arguments.get("body", ""))
        if not recipient or "@" not in recipient or not subject:
            raise CapabilityError("收件人和主题不能为空。")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        dispatch_id = str(
            self._dispatch_metadata(arguments).get("dispatch_id", "")
        ).strip()
        if dispatch_id:
            message["Message-ID"] = f"<{dispatch_id}@agi-yukino.local>"
        message.set_content(body)
        # 作用：建立 TLS SMTP 会话并完成一次登录和邮件提交。
        # 参数：无。
        def send_once() -> None:
            with smtplib.SMTP_SSL(
                host,
                port,
                context=ssl.create_default_context(),
                timeout=20,
            ) as client:
                client.login(username, password)
                client.send_message(message)

        self._mark_provider_call_started(arguments, side_effecting=True)
        self._network_call(
            f"mail:{host}",
            send_once,
            safe_to_retry=False,
            cancel_check=cancel_check,
        )
        return {
            "sent": True,
            "to": recipient,
            "subject": subject,
            "message_id": str(message.get("Message-ID", "")),
        }

    # 作用：分析提示复杂度并管理回应策略实验记录。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    def _prompt_governance(self, action: str, arguments: dict[str, Any]) -> Any:
        if action == "status":
            return {
                "available": True,
                "quality_guard": True,
                "response_policy": True,
                "interaction_learning": True,
                "experiment_count": len(self.state.read().get("prompt_experiments", [])),
            }
        if action == "analyze":
            text = str(arguments.get("text", ""))
            constraints = len(re.findall(r"必须|不要|不能|同时|分别|格式|步骤", text))
            risk_markers = len(re.findall(r"自伤|伤人|医疗|法律|金融|密码|密钥", text))
            score = min(1.0, len(text) / 2000 + constraints * 0.08 + risk_markers * 0.18)
            level = "high" if score >= 0.65 else "medium" if score >= 0.3 else "low"
            return {
                "complexity": level,
                "score": round(score, 3),
                "characters": len(text),
                "constraints": constraints,
                "risk_markers": risk_markers,
                "recommendation": "拆分任务并启用严格审计" if level == "high" else "使用标准回应链路",
            }
        if action == "list_experiments":
            return list(self.state.read().get("prompt_experiments", []))
        if action == "record_experiment":
            name = str(arguments.get("name", "")).strip()
            if not name:
                raise CapabilityError("实验名称不能为空。")

            # 作用：持久化一条包含基线、候选和决策的提示实验。
            # 参数 state：当前读取、修改或公开的持久状态对象。
            def record(state: dict[str, Any]) -> dict[str, Any]:
                item = {
                    "id": uuid.uuid4().hex[:12],
                    "name": name,
                    "baseline": arguments.get("baseline", {}),
                    "candidate": arguments.get("candidate", {}),
                    "metrics": arguments.get("metrics", {}),
                    "decision": arguments.get("decision", "pending"),
                    "created_at": time.time(),
                }
                state.setdefault("prompt_experiments", []).append(item)
                return item

            return self.state.update(record)
        raise CapabilityError(f"未知回应治理操作：{action}")

    # 作用：分派素材上传、视觉、语音、图像和视频能力及其确认边界。
    # 参数 action：要执行或校验的能力操作标识。
    # 参数 arguments：本次能力调用或工作流步骤的参数对象。
    # 参数 confirmed：调用方是否已对当前副作用目标作出显式确认。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def _media(
        self,
        action: str,
        arguments: dict[str, Any],
        confirmed: bool,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        stickers = Path(__file__).resolve().parents[2] / "assets" / "stickers"
        if action == "list":
            return {
                "stickers": [str(path.relative_to(stickers)) for path in stickers.rglob("*") if path.is_file()]
                if stickers.exists()
                else [],
                "uploads": self.workspace.list("uploads") if self.workspace.resolve("uploads").exists() else [],
                "tts_enabled": os.getenv("TTS_ENABLED", "").lower() in {"1", "true", "yes"},
                "vision_configured": bool(os.getenv("OPENAI_API_KEY", "")),
            }
        if action == "upload":
            filename = Path(str(arguments.get("filename", "upload.bin"))).name
            encoded = str(arguments.get("base64", ""))
            if encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise CapabilityError("上传内容不是有效 Base64。") from exc
            if not raw or len(raw) > 10 * 1024 * 1024:
                raise CapabilityError("上传文件必须在 1 字节到 10 MiB 之间。")
            target = self.workspace.resolve(f"uploads/{uuid.uuid4().hex[:8]}-{filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return {"path": str(target.relative_to(self.workspace.root)), "size": len(raw)}
        if action == "analyze":
            if not confirmed:
                raise CapabilityError("视觉分析会把图片发送给已配置模型，需要 confirmed=true。")
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise CapabilityError("尚未配置视觉模型 API Key。")
            source = self.workspace.resolve(str(arguments.get("path", "")), must_exist=True)
            raw = source.read_bytes()
            if not raw or len(raw) > 10 * 1024 * 1024:
                raise CapabilityError("图片必须在 1 字节到 10 MiB 之间。")
            mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise CapabilityError("视觉分析只支持 JPEG、PNG、WebP 或 GIF。")
            from openai import OpenAI

            client_kwargs: dict[str, Any] = {"api_key": api_key}
            if os.getenv("OPENAI_BASE_URL", "").strip():
                client_kwargs["base_url"] = os.getenv("OPENAI_BASE_URL", "").strip()
            client = OpenAI(**client_kwargs)
            prompt = str(arguments.get("prompt", "请客观描述这张图片，并指出需要注意的内容。"))
            completion_arguments: dict[str, Any] = {
                "model": os.getenv(
                    "OPENAI_VISION_MODEL",
                    os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                ),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64.b64encode(raw).decode()}"
                                },
                            },
                        ],
                    }
                ],
                "timeout": 20,
            }
            dispatch_headers = self._dispatch_headers(arguments)
            if dispatch_headers:
                completion_arguments["extra_headers"] = dispatch_headers
            self._mark_provider_call_started(arguments, side_effecting=True)
            completion = self._network_call(
                "openai:vision",
                lambda: client.chat.completions.create(**completion_arguments),
                safe_to_retry=False,
                cancel_check=cancel_check,
            )
            return {"path": str(source.relative_to(self.workspace.root)), "content": completion.choices[0].message.content or ""}
        if action == "speak":
            if not confirmed:
                raise CapabilityError("语音合成会把文本发送给已配置模型，需要 confirmed=true。")
            text = str(arguments.get("text", ""))
            if not os.getenv("OPENAI_API_KEY", "").strip() or not text.strip():
                raise CapabilityError("语音合成需要 API Key 和非空文本。")
            from service_club.core.conversation.characters import get_character
            from service_club.core.conversation.postprocess import synthesize_tts_optional

            url = synthesize_tts_optional(
                text,
                get_character(str(arguments.get("character", "yukino"))),  # type: ignore[arg-type]
                str(arguments.get("emotion", "neutral")),  # type: ignore[arg-type]
                force=True,
                idempotency_key=str(
                    self._dispatch_metadata(arguments).get("idempotency_key", "")
                ),
                on_provider_call_start=lambda: self._mark_provider_call_started(
                    arguments,
                    side_effecting=True,
                ),
            )
            if not url:
                raise CapabilityError("语音合成失败，请检查模型、音色和 API 配置。")
            return {"url": url}
        if action == "generate_image":
            if not confirmed:
                raise CapabilityError("图像生成会把提示词发送给已配置模型，需要 confirmed=true。")
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            prompt = str(arguments.get("prompt", "")).strip()
            if not api_key or not prompt:
                raise CapabilityError("图像生成需要 API Key 和 prompt。")
            from openai import OpenAI

            client_kwargs: dict[str, Any] = {"api_key": api_key}
            if os.getenv("OPENAI_BASE_URL", "").strip():
                client_kwargs["base_url"] = os.getenv("OPENAI_BASE_URL", "").strip()
            image_client = OpenAI(**client_kwargs)
            image_arguments: dict[str, Any] = {
                "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"),
                "prompt": prompt,
                "size": str(arguments.get("size", "1024x1024")),
            }
            dispatch_headers = self._dispatch_headers(arguments)
            if dispatch_headers:
                image_arguments["extra_headers"] = dispatch_headers
            self._mark_provider_call_started(arguments, side_effecting=True)
            response = self._network_call(
                "openai:image",
                lambda: image_client.images.generate(**image_arguments),
                safe_to_retry=False,
                cancel_check=cancel_check,
            )
            item = response.data[0]
            if getattr(item, "b64_json", None):
                raw = base64.b64decode(item.b64_json)
            elif getattr(item, "url", None):
                image_url = str(item.url)

                # 作用：下载模型返回的图片地址并限制最大响应体。
                # 参数：无。
                def download_generated() -> bytes:
                    with urllib.request.urlopen(image_url, timeout=30) as generated:
                        return generated.read(20 * 1024 * 1024 + 1)

                hostname = urllib.parse.urlparse(image_url).hostname or "generated-image"
                raw = self._network_call(
                    f"generated-image:{hostname}",
                    download_generated,
                    safe_to_retry=True,
                    cancel_check=cancel_check,
                )
                if len(raw) > 20 * 1024 * 1024:
                    raise CapabilityError("生成图片超过 20 MiB 限制。")
            else:
                raise CapabilityError("图像模型没有返回可保存的内容。")
            generated_dir = self.data_dir / "media" / "generated"
            generated_dir.mkdir(parents=True, exist_ok=True)
            target = generated_dir / f"{uuid.uuid4().hex[:16]}.png"
            target.write_bytes(raw)
            return {"url": f"/media/generated/{target.name}", "size": len(raw)}
        if action == "generate_video":
            if not confirmed:
                raise CapabilityError("视频生成会把提示词发送给外部服务，需要 confirmed=true。")
            endpoint = os.getenv("YUKINO_VIDEO_ENDPOINT", "").strip()
            prompt = str(arguments.get("prompt", "")).strip()
            if not endpoint or not prompt:
                raise CapabilityError("视频生成需要 YUKINO_VIDEO_ENDPOINT 和 prompt。")
            request = urllib.request.Request(
                endpoint,
                data=json.dumps({"prompt": prompt, "duration": arguments.get("duration", 5)}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.getenv('YUKINO_VIDEO_API_KEY', '')}",
                    **self._dispatch_headers(arguments),
                },
                method="POST",
            )
            # 作用：向已配置视频服务提交一次不重试的生成请求。
            # 参数：无。
            def generate_video() -> bytes:
                with urllib.request.urlopen(request, timeout=240) as response:
                    return response.read(2 * 1024 * 1024 + 1)

            hostname = urllib.parse.urlparse(endpoint).hostname or "video"
            self._mark_provider_call_started(arguments, side_effecting=True)
            raw = self._network_call(
                f"video:{hostname}",
                generate_video,
                safe_to_retry=False,
                cancel_check=cancel_check,
            )
            if len(raw) > 2 * 1024 * 1024:
                raise CapabilityError("视频服务响应超过 2 MiB 限制。")
            return json.loads(raw.decode("utf-8"))
        raise CapabilityError(f"未知媒体操作：{action}")
