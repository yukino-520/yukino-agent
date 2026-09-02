import json
import re
import uuid
from dataclasses import dataclass
from typing import Any


# 作用：保存从兼容模型文本中提取出的一个 DSML 工具调用。
# 参数 id：为本次调用生成的短标识。
# 参数 name：工具名称。
# 参数 arguments：解析或修复后的工具参数字典。
# 参数 source：工具调用来源，默认为 dsml。
# 参数 repaired：原始参数是否经过容错修复。
@dataclass(frozen=True)
# 作用：定义“ExtractedToolCall”相关的数据结构、异常类型或服务组件。
# 字段：id：该对象中的结构化字段。、name：该对象中的结构化字段。、arguments：该对象中的结构化字段。、source：该对象中的结构化字段。、repaired：该对象中的结构化字段。
class ExtractedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    source: str = "dsml"
    repaired: bool = False


# 作用：定义“ToolCallExtractor”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class ToolCallExtractor:
    # 作用：创建 DSML 工具调用提取器，并设置可接受的工具白名单。
    # 参数 allowed_tools：允许从文本中提取的工具名称；为空表示不过滤。
    def __init__(self, allowed_tools: set[str] | None = None) -> None:
        self.allowed_tools = allowed_tools

    # 作用：扫描模型文本中的 tool_call 标签并解析为结构化调用。
    # 参数 text：可能包含 DSML 标签的模型输出。
    # 返回：通过工具白名单检查的调用列表。
    def extract_text(self, text: str) -> list[ExtractedToolCall]:
        calls: list[ExtractedToolCall] = []
        for match in re.finditer(
            r'<tool_call\s+name="(?P<name>[^"]+)">(?P<body>.*?)</tool_call>',
            text,
            flags=re.DOTALL,
        ):
            name = match.group("name").strip()
            if self.allowed_tools is not None and name not in self.allowed_tools:
                continue
            body = match.group("body").strip()
            arguments, repaired = self._parse_arguments(name, body)
            calls.append(
                ExtractedToolCall(
                    id=uuid.uuid4().hex[:10],
                    name=name,
                    arguments=arguments,
                    repaired=repaired,
                )
            )
        return calls

    # 作用：解析标签正文中的 JSON，并对简单键值或常见工具参数进行容错修复。
    # 参数 name：当前工具名称，用于选择特定修复规则。
    # 参数 body：tool_call 标签中的原始参数正文。
    # 返回：参数字典及是否发生修复的标记。
    def _parse_arguments(self, name: str, body: str) -> tuple[dict[str, Any], bool]:
        if not body:
            return {}, False
        try:
            return json.loads(body), False
        except json.JSONDecodeError:
            pass
        key_value = re.fullmatch(r"(?P<key>[a-zA-Z_][\w-]*)\s*=\s*(?P<value>.+)", body)
        if key_value:
            return {key_value.group("key"): key_value.group("value").strip()}, True
        if name in {"remember", "set_reminder"}:
            return {"content": body}, True
        if name == "recall":
            return {"query": body}, True
        return {}, True

    # 作用：从模型回复中删除所有 DSML 工具标签，只保留用户可见文本。
    # 参数 text：包含工具标签的原始模型输出。
    # 返回：移除标签并清理首尾空白后的文本。
    def strip_text(self, text: str) -> str:
        return re.sub(
            r'<tool_call\s+name="[^"]+">.*?</tool_call>',
            "",
            text,
            flags=re.DOTALL,
        ).strip()
