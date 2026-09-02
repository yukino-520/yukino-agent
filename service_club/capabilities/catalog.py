from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Literal

CapabilityState = Literal["ready", "configured", "degraded"]


# 作用：描述一项产品能力的标识、操作范围、风险和可用状态。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“Capability”相关的数据结构、异常类型或服务组件。
# 字段：id：该对象中的结构化字段。、label：该对象中的结构化字段。、group：该对象中的结构化字段。、description：该对象中的结构化字段。、operations：该对象中的结构化字段。、state：该对象中的结构化字段。、risk：该对象中的结构化字段。、requires：该对象中的结构化字段。
class Capability:
    id: str
    label: str
    group: str
    description: str
    operations: tuple[str, ...]
    state: CapabilityState = "ready"
    risk: str = "low"
    requires: str = ""

    # 作用：将不可变能力描述转换为便于 API 返回的字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["operations"] = list(self.operations)
        return value


CAPABILITIES: tuple[Capability, ...] = (
    Capability("companion", "侍奉部陪伴", "陪伴", "五角色私聊、群像编排、情绪与安全回应。", ("chat",)),
    Capability("memory", "记忆与画像", "认知", "会话记忆、永久记忆、画像、关系与反思。", ("recall", "remember", "forget")),
    Capability("retrieval", "混合检索", "认知", "全文、语义、时效和重要度融合检索。", ("search",)),
    Capability("knowledge_graph", "用户知识图谱", "认知", "用会话隔离的实体与关系组织已确认用户信息。", ("observe", "list", "search", "add_entity", "add_relation")),
    Capability("insights", "学习记录", "认知", "记录、分类和追踪错误、需求与有效互动模式。", ("list", "record", "update")),
    Capability("tool_search", "能力检索", "工具", "按用户意图从能力目录中选择最相关工具。", ("search",)),
    Capability("workspace", "文件工作区", "工具", "在专属沙箱中列出、读取、写入和搜索文件。", ("list", "read", "write", "search"), risk="medium"),
    Capability("python", "Python 小实验", "工具", "经 AST 审查后在隔离子进程运行短代码。", ("run",), risk="medium"),
    Capability("web", "网页研究", "工具", "带 SSRF 防护、超时和大小限制的搜索与正文提取。", ("search", "fetch"), state="configured", requires="网络访问"),
    Capability("documents", "文档阅读", "工具", "读取文本、JSON、CSV，以及已安装解析器支持的 PDF/Office 文件。", ("read",), state="configured", requires="对应格式解析库"),
    Capability("system", "设备状态", "工具", "读取系统状态，并运行用户显式配置的无 Shell 任务。", ("snapshot", "run_task"), risk="medium"),
    Capability("hardware", "硬件感知", "工具", "读取温度、电池和设备资源；不可用硬件会优雅降级。", ("status",)),
    Capability("organizer", "笔记与日程", "主动行为", "管理笔记、待办、提醒与计划。", ("list", "add", "complete", "delete"), risk="medium"),
    Capability("workflows", "部务流程", "编排", "执行带依赖、输出引用、持久进度和人工恢复的有界能力工作流。", ("run", "enqueue", "status", "list", "resume"), risk="medium"),
    Capability("mcp", "MCP 连接", "扩展", "连接显式配置的 HTTP JSON-RPC MCP 服务。", ("list", "call"), state="configured", requires="YUKINO_MCP_ENDPOINTS"),
    Capability("plugins", "扩展清单", "扩展", "发现、控制并调用有权限声明的 HTTP 扩展。", ("list", "register", "enable", "invoke"), risk="medium"),
    Capability("channels", "消息通道", "通道", "Web、QQ、微信和通用 Webhook 统一入站契约。", ("receive",), state="configured", requires="平台凭证或 Webhook"),
    Capability("media", "多模态", "媒体", "贴纸、语音、素材上传、视觉理解与生成。", ("list", "upload", "analyze", "speak", "generate_image", "generate_video"), state="configured", requires="素材或模型凭证"),
    Capability("mail", "邮件协作", "通道", "通过显式 SMTP 配置发送确认过的邮件。", ("status", "send"), state="configured", risk="high", requires="SMTP 配置"),
    Capability("observability", "运行观测", "治理", "Doctor、追踪、降级、路由反馈和质量审计。", ("status",)),
    Capability("usage", "模型用量", "治理", "按天与模型汇总 token 和可配置估算费用。", ("summary",)),
    Capability("prompt_governance", "回应治理", "治理", "场景复杂度、实验记录、回应策略、质量守卫与互动学习。", ("status", "analyze", "list_experiments", "record_experiment")),
)


# 作用：把中英文能力描述切分为英文词和中文二元词元。
# 参数 text：待分词、分析或发送的输入文本。
def tokenize(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    words.update(chinese)
    return {token for token in words if token}


# 作用：按词法重合度和直接命中程度检索最相关的能力。
# 参数 query：能力、文件、网页或图谱检索使用的查询文本。
# 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
def search_capabilities(query: str, limit: int = 6) -> list[dict[str, object]]:
    query_tokens = tokenize(query)
    normalized_query = query.lower()
    scored: list[tuple[float, Capability]] = []
    for capability in CAPABILITIES:
        document = " ".join(
            (capability.id, capability.label, capability.group, capability.description, *capability.operations)
        )
        document_tokens = tokenize(document)
        overlap = query_tokens & document_tokens
        lexical = sum(2.0 if token in capability.label.lower() else 1.0 for token in overlap)
        coverage = len(overlap) / max(1, math.sqrt(len(query_tokens) * len(document_tokens)))
        direct_match = 5.0 if capability.id.lower() in normalized_query or capability.label.lower() in normalized_query else 0.0
        score = direct_match + lexical + coverage
        if score > 0:
            scored.append((score, capability))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return [{**capability.as_dict(), "score": round(score, 4)} for score, capability in scored[:limit]]
