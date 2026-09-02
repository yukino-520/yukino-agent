import re
from dataclasses import dataclass

from service_club.capabilities import CapabilityHub
from service_club.core.memory import MemoryManager
from service_club.core.memory.permanent_memory import PermanentMemoryManager
from service_club.core.tooling.tool_registry import ServiceClubToolRegistry
from service_club.core.types import ToolAction, ToolExecutionResult


# 作用：保存确定性解析得到的工具动作和单个文本参数。
# 参数 action：解析出的标准工具名称。
# 参数 argument：从用户文本中提取的主要参数。
@dataclass(frozen=True)
# 作用：定义“ParsedToolAction”相关的数据结构、异常类型或服务组件。
# 字段：action：该对象中的结构化字段。、argument：该对象中的结构化字段。
class ParsedToolAction:
    action: ToolAction
    argument: str = ""


# 作用：用正则把明确的中文委托解析成受支持的工具动作。
# 参数 text：用户的一段自然语言委托。
# 返回：解析出的动作和参数；未匹配时 action 为 none。
def parse_tool_action(text: str) -> ParsedToolAction:
    stripped = text.strip()
    rollback = re.search(
        r"回滚(?:文件操作)?(?:[:：\s]+([a-f0-9]{6,20}))?",
        stripped,
        flags=re.I,
    )
    if rollback:
        return ParsedToolAction(
            "rollback_file_operation",
            (rollback.group(1) or "").strip(),
        )
    capability_confirmation = re.search(
        r"确认(?:能力操作|外部操作)(?:[:：\s]+([a-f0-9]{6,20}))?",
        stripped,
        flags=re.I,
    )
    if capability_confirmation:
        return ParsedToolAction(
            "confirm_capability_operation",
            (capability_confirmation.group(1) or "").strip(),
        )
    capability_cancellation = re.search(
        r"取消(?:能力操作|外部操作)(?:[:：\s]+([a-f0-9]{6,20}))?",
        stripped,
        flags=re.I,
    )
    if capability_cancellation:
        return ParsedToolAction(
            "cancel_capability_operation",
            (capability_cancellation.group(1) or "").strip(),
        )
    confirmation = re.search(r"确认(?:文件操作|覆盖|修改)(?:[:：\s]+([a-f0-9]{6,20}))?", stripped, flags=re.I)
    if confirmation:
        return ParsedToolAction("confirm_file_operation", (confirmation.group(1) or "").strip())
    cancellation = re.search(r"取消(?:文件操作|覆盖|修改)(?:[:：\s]+([a-f0-9]{6,20}))?", stripped, flags=re.I)
    if cancellation:
        return ParsedToolAction("cancel_file_operation", (cancellation.group(1) or "").strip())
    code_block = re.search(r"```(?:[\w.+-]+)?\s*\n(?P<code>.*?)```", stripped, flags=re.S)
    if code_block and re.search(r"分析|检查|看看|审查|review|bug|问题", stripped, flags=re.I):
        return ParsedToolAction("analyze_code", code_block.group("code").strip())
    explicit_code = re.search(r"(?:分析|检查|审查)(?:一下)?(?:这段)?代码[:：]\s*(.+)", stripped, flags=re.I | re.S)
    if explicit_code:
        return ParsedToolAction("analyze_code", explicit_code.group(1).strip())
    code_file = re.search(r"(?:分析|检查|审查)(?:一下)?(?:这个)?(?:代码)?文件[:：]\s*(.+)", stripped, flags=re.I)
    if code_file:
        return ParsedToolAction("analyze_file", code_file.group(1).strip())
    replace_content = re.search(
        r"(?:请)?(?:把|将)?(?:内部工作区)?\s*(?P<path>[A-Za-z0-9_./~ -]+\.[A-Za-z0-9_-]+)\s*(?:的)?内容(?:改成|替换为|写成)[:：]?\s*(?P<content>[\s\S]+)",
        stripped,
        flags=re.I,
    )
    if replace_content:
        return ParsedToolAction("write_file", f"{replace_content.group('path').strip()}\n{replace_content.group('content').strip()}")
    file_write = re.search(
        r"(?:创建|新建|写入)(?:文件)?[:：]\s*(?P<path>[^\n]+)\n(?P<content>[\s\S]+)",
        stripped,
        flags=re.I,
    )
    if file_write:
        return ParsedToolAction("write_file", f"{file_write.group('path').strip()}\n{file_write.group('content')}")
    python_code = re.search(r"(?:运行|执行)\s*Python[:：]\s*(.+)", stripped, flags=re.I | re.S)
    if python_code:
        return ParsedToolAction("run_python", python_code.group(1).strip())
    document = re.search(r"(?:读取|打开|读一下)(?:文档)[:：]\s*(.+)", stripped, flags=re.I)
    if document:
        return ParsedToolAction("read_document", document.group(1).strip())
    file_read = re.search(r"(?:读取|打开)(?:文件)[:：]\s*(.+)", stripped, flags=re.I)
    if file_read:
        return ParsedToolAction("read_file", file_read.group(1).strip())
    file_search = re.search(r"(?:搜索|查找)(?:文件内容)[:：]\s*(.+)", stripped, flags=re.I)
    if file_search:
        return ParsedToolAction("search_files", file_search.group(1).strip())
    file_list = re.search(r"(?:列出|查看)(?:工作区)?文件(?:[:：]\s*(.*))?", stripped, flags=re.I)
    if file_list:
        return ParsedToolAction("list_files", (file_list.group(1) or ".").strip())
    web_url = re.search(r"(?:读取|浏览|打开)(?:网页)[:：]\s*(https?://\S+)", stripped, flags=re.I)
    if web_url:
        return ParsedToolAction("web_fetch", web_url.group(1).strip())
    web_query = re.search(r"(?:搜索网页|上网查|联网查)[:：]?\s*(.+)", stripped, flags=re.I)
    if web_query:
        return ParsedToolAction("web_search", web_query.group(1).strip())
    task = re.search(r"(?:添加待办|记个任务)[:：]?\s*(.+)", stripped)
    if task:
        return ParsedToolAction("add_task", task.group(1).strip())
    if re.search(r"(?:列出|查看|有什么)待办", stripped):
        return ParsedToolAction("list_tasks")
    if re.search(r"(?:系统|设备|硬件)(?:状态|信息)", stripped):
        return ParsedToolAction("system_status")
    capability = re.search(r"(?:查找|搜索|有哪些)(?:能力|工具)[:：]?\s*(.*)", stripped)
    if capability:
        return ParsedToolAction("search_capabilities", capability.group(1).strip() or stripped)
    if re.search(r"确认清空记忆|确认忘掉全部|清空全部记忆", stripped):
        return ParsedToolAction("clear_memory", "confirmed")
    if re.search(r"清空记忆|忘掉全部", stripped):
        return ParsedToolAction("clear_memory")
    reminder = re.search(r"(?:提醒我|帮我提醒|记得提醒我)[:：]?\s*(.+)", stripped)
    if reminder:
        return ParsedToolAction("set_reminder", stripped)
    if re.search(r"有什么提醒|提醒列表|列出提醒", stripped):
        return ParsedToolAction("list_reminders")
    cancel_reminder = re.search(r"(?:取消|删除)提醒[:：\s]*(\d+)", stripped)
    if cancel_reminder:
        return ParsedToolAction("cancel_reminder", cancel_reminder.group(1))
    remember = re.search(r"(?:帮我)?记住[:：]?\s*(.+)", stripped)
    if remember:
        return ParsedToolAction("remember", remember.group(1).strip())
    forget = re.search(r"忘记[:：]?\s*(.+)", stripped)
    if forget:
        return ParsedToolAction("forget", forget.group(1).strip())
    recall = re.search(r"(?:你还)?记得(.+?)吗", stripped)
    if recall:
        return ParsedToolAction("recall", recall.group(1).strip())
    if "回忆" in stripped:
        return ParsedToolAction("recall", stripped.replace("回忆", "").strip())
    if re.search(r"几点|日期|现在时间|当前时间|今天(?:几号|星期|日期|是几号)", stripped):
        return ParsedToolAction("get_time")
    return ParsedToolAction("none")


# 作用：定义“ToolExecutor”相关的数据结构、异常类型或服务组件。
# 参数：实例化参数由该类构造函数的类型注解和默认值定义。
class ToolExecutor:
    # 作用：把记忆和能力依赖组装成统一工具注册表。
    # 参数 memory：当前会话的关系事实存储。
    # 参数 permanent_memory：可选长期记忆管理器。
    # 参数 capabilities：可选能力中心，提供文件、网络、MCP 等扩展工具。
    def __init__(
        self,
        memory: MemoryManager,
        permanent_memory: PermanentMemoryManager | None = None,
        capabilities: CapabilityHub | None = None,
    ) -> None:
        self.memory = memory
        self.registry = ServiceClubToolRegistry(memory, permanent_memory, capabilities)

    # 作用：先解析自然语言，再执行识别出的确定性工具动作。
    # 参数 session_id：工具调用归属的会话标识。
    # 参数 text：待解析的用户文本。
    # 参数 allowed_tools：当前角色允许执行的工具白名单。
    # 返回：统一的工具执行结果。
    def execute(
        self,
        session_id: str,
        text: str,
        allowed_tools: tuple[ToolAction, ...] | None = None,
    ) -> ToolExecutionResult:
        parsed = parse_tool_action(text)
        return self.execute_parsed(session_id, parsed, allowed_tools=allowed_tools)

    # 作用：把已解析动作转换为各工具需要的参数字典，并交给注册表执行。
    # 参数 session_id：工具调用归属的会话标识。
    # 参数 parsed：已经解析完成的动作与主要参数。
    # 参数 allowed_tools：当前角色允许执行的工具白名单。
    # 返回：统一的工具执行结果。
    def execute_parsed(
        self,
        session_id: str,
        parsed: ParsedToolAction,
        allowed_tools: tuple[ToolAction, ...] | None = None,
    ) -> ToolExecutionResult:
        if parsed.action == "none":
            return ToolExecutionResult(action="none", success=True)
        if parsed.action == "get_time":
            return self.registry.execute(
                "get_time",
                {},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "remember":
            return self.registry.execute(
                "remember",
                {"content": parsed.argument},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "recall":
            return self.registry.execute(
                "recall",
                {"query": parsed.argument},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "clear_memory":
            return self.registry.execute(
                "clear_memory",
                {"confirmed": parsed.argument == "confirmed"},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "set_reminder":
            return self.registry.execute(
                "set_reminder",
                {"content": parsed.argument},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "list_reminders":
            return self.registry.execute(
                "list_reminders",
                {},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "cancel_reminder":
            return self.registry.execute(
                "cancel_reminder",
                {"reminder_id": int(parsed.argument or 0)},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "forget":
            return self.registry.execute(
                "forget",
                {"query": parsed.argument},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action == "write_file":
            path, _, content = parsed.argument.partition("\n")
            return self.registry.execute(
                "write_file",
                {"path": path, "content": content},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action in {
            "confirm_file_operation",
            "cancel_file_operation",
            "rollback_file_operation",
            "confirm_capability_operation",
            "cancel_capability_operation",
        }:
            return self.registry.execute(
                parsed.action,
                {"operation_id": parsed.argument},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action in {
            "search_capabilities",
            "list_files",
            "read_file",
            "search_files",
            "analyze_code",
            "analyze_file",
            "run_python",
            "web_search",
            "web_fetch",
            "read_document",
            "add_task",
        }:
            argument_keys = {
                "search_capabilities": "query",
                "list_files": "path",
                "read_file": "path",
                "search_files": "query",
                "analyze_code": "code",
                "analyze_file": "path",
                "run_python": "code",
                "web_search": "query",
                "web_fetch": "url",
                "read_document": "path",
                "add_task": "content",
            }
            return self.registry.execute(
                parsed.action,
                {argument_keys[parsed.action]: parsed.argument},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        if parsed.action in {"system_status", "list_tasks"}:
            return self.registry.execute(
                parsed.action,
                {},
                session_id=session_id,
                allowed_tools=allowed_tools,
            )
        return ToolExecutionResult(action="none", success=True)
