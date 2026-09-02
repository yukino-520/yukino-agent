"""Deterministic execution plans and evidence requirements for Agent tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from service_club.capabilities.catalog import CAPABILITIES


# 作用：描述任务完成前必须由哪些工具及审计字段证明的一项验收要求。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“EvidenceRequirement”相关的数据结构、异常类型或服务组件。
# 字段：key：该对象中的结构化字段。、label：该对象中的结构化字段。、actions：该对象中的结构化字段。、audit_equals：该对象中的结构化字段。、side_effect：该对象中的结构化字段。
class EvidenceRequirement:
    key: str
    label: str
    actions: tuple[str, ...]
    audit_equals: dict[str, object] = field(default_factory=dict)
    side_effect: bool = False

    # 作用：将证据要求转换成可持久化和传递给安全层的结构。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "actions": list(self.actions),
            "audit_equals": dict(self.audit_equals),
            "side_effect": self.side_effect,
        }


# 作用：保存确定性执行计划以及任务完成所需的全部证据契约。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“AgentExecutionContract”相关的数据结构、异常类型或服务组件。
# 字段：plan：该对象中的结构化字段。、requirements：该对象中的结构化字段。
class AgentExecutionContract:
    plan: tuple[str, ...]
    requirements: tuple[EvidenceRequirement, ...]

    # 作用：序列化计划，并派生是否需要工具证据和副作用的标记。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "plan": list(self.plan),
            "requirements": [item.as_dict() for item in self.requirements],
            "requires_tool_evidence": bool(self.requirements),
            "has_side_effects": any(item.side_effect for item in self.requirements),
        }


# 作用：从用户原始委托、解析意图和附件构建保守的执行与验收契约。
# 参数：无。
class AgentExecutionPlanner:
    """Build a conservative contract without trusting the model's completion claim."""

    ACTION_LABELS = {
        "get_time": "读取并返回当前时间",
        "remember": "写入明确要求保留的记忆",
        "recall": "检索相关记忆",
        "forget": "删除指定记忆并核对结果",
        "clear_memory": "清理会话记忆并核对结果",
        "set_reminder": "创建提醒并核对记录",
        "list_reminders": "读取提醒列表",
        "cancel_reminder": "取消指定提醒并核对状态",
        "search_capabilities": "检索可用能力",
        "list_files": "列出授权范围内的文件",
        "read_file": "读取目标文件",
        "search_files": "在授权目录中检索文件内容",
        "analyze_code": "检查代码结构、语法与风险",
        "analyze_file": "读取并检查代码文件",
        "write_file": "创建目标文件并校验落盘结果",
        "edit_file": "准备受确认保护的文件修改",
        "confirm_file_operation": "执行已确认的文件操作并校验",
        "cancel_file_operation": "取消等待中的文件操作",
        "rollback_file_operation": "回滚文件操作并校验",
        "run_python": "在受控 Python 环境运行代码",
        "web_search": "检索最新公开网页信息",
        "web_fetch": "读取指定公开网页",
        "read_document": "解析并读取目标文档",
        "system_status": "读取设备与系统状态",
        "add_task": "创建待办并核对记录",
        "list_tasks": "读取待办列表",
        "capability_call": "调用所需扩展能力并取得真实结果",
        "confirm_capability_operation": "执行已确认的外部能力并核对结果",
        "cancel_capability_operation": "取消等待中的外部能力操作",
    }
    SIDE_EFFECT_ACTIONS = {
        "remember",
        "forget",
        "clear_memory",
        "set_reminder",
        "cancel_reminder",
        "write_file",
        "edit_file",
        "confirm_file_operation",
        "rollback_file_operation",
        "add_task",
        "confirm_capability_operation",
    }

    # 作用：合并显式意图、附件和隐含任务要求，形成去重后的计划与证据清单。
    # 参数 text：用于规划、检测或规范化的用户文本。
    # 参数 intent_steps：由确定性意图解析器产生的动作步骤。
    # 参数 attachments：已归属当前会话的附件元数据列表。
    def build(
        self,
        *,
        text: str,
        intent_steps: Iterable[object],
        attachments: list[dict[str, object]],
    ) -> AgentExecutionContract:
        requirements: list[EvidenceRequirement] = []
        for index, step in enumerate(intent_steps, 1):
            action = str(getattr(step, "action", "none"))
            if action == "none":
                continue
            requirements.append(
                EvidenceRequirement(
                    key=f"intent:{index}:{action}",
                    label=self.ACTION_LABELS.get(action, f"执行 {action}"),
                    actions=(action,),
                    side_effect=action in self.SIDE_EFFECT_ACTIONS,
                )
            )
        requirements.extend(self._attachment_requirements(attachments))
        requirements.extend(self._implicit_requirements(text, requirements))
        requirements = self._dedupe(requirements)
        plan = ["理解委托并识别必须完成的结果"]
        plan.extend(item.label for item in requirements)
        plan.append(
            "核对必要工具证据，未取得证据时明确说明未完成"
            if requirements
            else "结合上下文生成回应并完成质量检查"
        )
        return AgentExecutionContract(tuple(plan), tuple(requirements))

    # 作用：按附件类型生成读取、解析或经确认分析所需的证据要求。
    # 参数 attachments：已归属当前会话的附件元数据列表。
    def _attachment_requirements(
        self,
        attachments: list[dict[str, object]],
    ) -> list[EvidenceRequirement]:
        result: list[EvidenceRequirement] = []
        for item in attachments:
            attachment_id = str(item.get("id", ""))
            name = str(item.get("original_name", "附件"))
            kind = str(item.get("kind", "text"))
            if kind == "code":
                actions = ("analyze_file",)
                label = f"读取并检查代码附件 {name}"
                audit = {"attachment_id": attachment_id}
            elif kind == "document":
                actions = ("read_document",)
                label = f"解析文档附件 {name}"
                audit = {"attachment_id": attachment_id}
            elif kind == "image":
                actions = ("capability_call",)
                label = f"经用户确认后分析图片附件 {name}"
                audit = {
                    "attachment_id": attachment_id,
                    "capability": "media",
                    "operation": "analyze",
                }
            else:
                actions = ("read_file",)
                label = f"读取文本附件 {name}"
                audit = {"attachment_id": attachment_id}
            result.append(
                EvidenceRequirement(
                    key=f"attachment:{attachment_id}",
                    label=label,
                    actions=actions,
                    audit_equals=audit,
                    side_effect=kind == "image",
                )
            )
        return result

    # 作用：从自然语言中补出文件、邮件、媒体、MCP 和联网等隐含成果要求。
    # 参数 text：用于规划、检测或规范化的用户文本。
    # 参数 existing：已经生成的证据要求，用于避免重复推导。
    def _implicit_requirements(
        self,
        text: str,
        existing: list[EvidenceRequirement],
    ) -> list[EvidenceRequirement]:
        requirements: list[EvidenceRequirement] = []
        normalized = text.strip()
        if self._has_actions(existing, {"write_file", "edit_file"}):
            file_required = False
        else:
            file_required = bool(
                re.search(
                    r"(?:(?:创建|新建|生成|保存|写入|输出).{0,36}(?:文件|桌面|[A-Za-z0-9_-]+\.[A-Za-z0-9]+))"
                    r"|(?:(?:文件|桌面).{0,36}(?:创建|新建|生成|保存|写入|输出))",
                    normalized,
                    flags=re.I,
                )
            )
        media_generation = re.search(
            r"(?:生成|制作|画|绘制).{0,36}(?:图片|图像|插画|视频)",
            normalized,
        )
        if not self._has_actions(existing, {"remember"}) and re.search(
            r"(?:记一下|记下来|记着(?:这个|这点|这件事)|把这点记住)",
            normalized,
        ):
            requirements.append(
                EvidenceRequirement(
                    key="implicit:remember",
                    label="写入用户本轮明确要求保留的记忆",
                    actions=("remember",),
                    side_effect=True,
                )
            )
        if file_required and not media_generation:
            requirements.append(
                EvidenceRequirement(
                    key="implicit:file_output",
                    label="创建用户要求的文件并校验真实落盘结果",
                    actions=("write_file", "edit_file", "confirm_file_operation"),
                    side_effect=True,
                )
            )
        if not self._has_actions(existing, {"edit_file", "write_file"}) and re.search(
            r"(?:(?:修改|更改|替换|改动|编辑).{0,40}(?:文件|[A-Za-z0-9_./~-]+\.[A-Za-z0-9_-]+))"
            r"|(?:(?:文件|[A-Za-z0-9_./~-]+\.[A-Za-z0-9_-]+).{0,40}(?:修改|更改|替换|改成|换成))",
            normalized,
            flags=re.I,
        ):
            requirements.append(
                EvidenceRequirement(
                    key="implicit:file_edit",
                    label="准备用户要求的文件修改并等待确认",
                    actions=("edit_file", "write_file", "confirm_file_operation"),
                    side_effect=True,
                )
            )
        if re.search(r"(?:发送|发)(?:一封)?(?:邮件|email)", normalized, flags=re.I):
            requirements.append(
                self._capability_requirement("mail_send", "发送邮件并取得服务端结果", "mail", "send")
            )
        if re.search(r"(?:生成|制作|画|绘制).{0,36}(?:图片|图像|插画)", normalized):
            requirements.append(
                self._capability_requirement(
                    "image_generation",
                    "经用户确认后生成图片",
                    "media",
                    "generate_image",
                )
            )
        if re.search(r"(?:生成|制作).{0,36}视频", normalized):
            requirements.append(
                self._capability_requirement(
                    "video_generation",
                    "经用户确认后生成视频",
                    "media",
                    "generate_video",
                )
            )
        if re.search(r"(?:调用|执行|使用).{0,20}MCP|MCP.{0,20}(?:调用|执行|工具)", normalized, flags=re.I):
            requirements.append(
                EvidenceRequirement(
                    key="implicit:mcp_call",
                    label="调用指定 MCP 并取得真实返回",
                    actions=("capability_call",),
                    audit_equals={"capability": "mcp"},
                    side_effect=True,
                )
            )
        requirements.extend(self._explicit_capability_requirements(normalized))
        if not self._has_actions(existing, {"web_search", "web_fetch"}) and re.search(
            r"(?:上网|联网|搜索网页|查网页|最新(?:新闻|消息|资料|版本|价格))",
            normalized,
        ):
            requirements.append(
                EvidenceRequirement(
                    key="implicit:web_research",
                    label="检索公开网页并基于真实结果回答",
                    actions=("web_search", "web_fetch", "capability_call"),
                    audit_equals={"capability": "web"},
                )
            )
        return requirements

    # 作用：识别用户明确点名的 capability.operation 及常见插件操作。
    # 参数 text：用于规划、检测或规范化的用户文本。
    def _explicit_capability_requirements(
        self,
        text: str,
    ) -> list[EvidenceRequirement]:
        definitions = {item.id: set(item.operations) for item in CAPABILITIES}
        result: list[EvidenceRequirement] = []
        for capability, operation in re.findall(
            r"\b([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\b",
            text.lower(),
        ):
            if operation not in definitions.get(capability, set()):
                continue
            result.append(
                self._capability_requirement(
                    f"explicit_{capability}_{operation}",
                    f"调用用户明确指定的 {capability}.{operation}",
                    capability,
                    operation,
                )
            )
        plugin_operations = (
            (r"(?:注册|添加).{0,24}插件", "register"),
            (r"(?:启用|开启).{0,24}插件", "enable"),
            (r"(?:调用|运行|执行|使用).{0,24}插件", "invoke"),
        )
        exact_plugin_operation = any(
            item.audit_equals.get("capability") == "plugins" for item in result
        )
        for pattern, operation in plugin_operations:
            if not exact_plugin_operation and re.search(pattern, text, flags=re.I):
                result.append(
                    self._capability_requirement(
                        f"plugins_{operation}",
                        f"执行用户明确要求的插件 {operation} 操作",
                        "plugins",
                        operation,
                    )
                )
                break
        return result

    # 作用：构造一项绑定具体扩展能力和操作名的副作用证据要求。
    # 参数 key：当前审计字段、证据项或熔断目标的键。
    # 参数 label：证据要求面向用户的可读说明。
    # 参数 capability：被调用的扩展能力标识。
    # 参数 operation：要调用的扩展操作名称或网络执行函数。
    @staticmethod
    # 作用：执行“capability_requirement”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 key：调用方传入的key，用于本次处理。
    # 参数 label：调用方传入的label，用于本次处理。
    # 参数 capability：调用方传入的capability，用于本次处理。
    # 参数 operation：调用方传入的operation，用于本次处理。
    def _capability_requirement(
        key: str,
        label: str,
        capability: str,
        operation: str,
    ) -> EvidenceRequirement:
        return EvidenceRequirement(
            key=f"implicit:{key}",
            label=label,
            actions=("capability_call",),
            audit_equals={"capability": capability, "operation": operation},
            side_effect=True,
        )

    # 作用：判断已有证据要求是否已经覆盖指定动作集合。
    # 参数 requirements：任务执行契约中的证据要求集合。
    # 参数 actions：允许或需要匹配的动作集合。
    @staticmethod
    # 作用：执行“has_actions”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 requirements：调用方传入的requirements，用于本次处理。
    # 参数 actions：调用方传入的actions，用于本次处理。
    def _has_actions(
        requirements: list[EvidenceRequirement],
        actions: set[str],
    ) -> bool:
        return any(actions.intersection(item.actions) for item in requirements)

    # 作用：按动作和审计约束去重，避免同一成果被重复加入执行计划。
    # 参数 requirements：任务执行契约中的证据要求集合。
    @staticmethod
    # 作用：执行“dedupe”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 requirements：调用方传入的requirements，用于本次处理。
    def _dedupe(requirements: list[EvidenceRequirement]) -> list[EvidenceRequirement]:
        seen: set[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = set()
        result: list[EvidenceRequirement] = []
        for item in requirements:
            signature = (
                item.actions,
                tuple(sorted((key, str(value)) for key, value in item.audit_equals.items())),
            )
            if signature in seen:
                continue
            seen.add(signature)
            result.append(item)
        return result
