from __future__ import annotations

import json
from typing import Any

from service_club.core.types import ToolExecutionResult

_SEVERITY_LABELS = {
    "error": "语法错误",
    "high": "高风险",
    "medium": "可靠性",
    "info": "维护提示",
}


# 作用：模型整理失败时，只依据已验证工具证据生成可用的降级回复。
# 参数 character_name：用于保持角色展示格式的角色名称。
# 参数 results：当前轮次已经完成的工具结果。
# 参数 degradation_reason：模型阶段的降级原因代码。
# 返回：不夸大未验证动作的证据型回复文本。
def present_degraded_tool_results(
    character_name: str,
    results: list[ToolExecutionResult],
    *,
    degradation_reason: str = "",
) -> str:
    """Turn verified local tool evidence into a useful failover response."""
    sections: list[str] = []
    for result in results:
        if not result.success or not result.content.strip():
            continue
        if result.action in {"analyze_code", "analyze_file"}:
            rendered = _present_code_analysis(result)
        else:
            rendered = result.content.strip()
        if rendered and rendered not in sections:
            sections.append(rendered)

    reason = _degradation_label(degradation_reason)
    opening = (
        f"{character_name}：模型整理阶段{reason}，但受控工具已经完成。"
        "下面只列能够由本地证据确认的结果。"
    )
    return opening if not sections else opening + "\n\n" + "\n\n".join(sections)[:6000]


# 作用：把代码分析工具的 JSON 结果整理成可读的确定性检查报告。
# 参数 result：analyze_code 或 analyze_file 的工具结果。
# 返回：代码概况、确定问题、修改建议和分析边界文本。
def _present_code_analysis(result: ToolExecutionResult) -> str:
    try:
        payload = json.loads(result.content)
    except json.JSONDecodeError:
        return result.content.strip()
    if not isinstance(payload, dict):
        return result.content.strip()

    path = ""
    analysis: Any = payload
    if result.action == "analyze_file":
        path = str(payload.get("path", "")).strip()
        analysis = payload.get("analysis", {})
    if not isinstance(analysis, dict):
        return result.content.strip()

    language = str(analysis.get("language", "unknown"))
    line_count = int(analysis.get("line_count", 0) or 0)
    functions = _string_list(analysis.get("functions"))
    classes = _string_list(analysis.get("classes"))
    imports = _string_list(analysis.get("imports"))
    findings = analysis.get("findings", [])

    scope = f"文件 `{path}`" if path else "这段代码"
    overview = [f"语言：{language}", f"共 {line_count} 行"]
    if functions:
        overview.append("函数：" + "、".join(functions))
    if classes:
        overview.append("类：" + "、".join(classes))
    if imports:
        overview.append("依赖：" + "、".join(imports))

    lines = [f"代码检查（{scope}）", "- 概况：" + "；".join(overview)]
    valid_findings = [item for item in findings if isinstance(item, dict)]
    if not valid_findings:
        lines.extend(
            (
                "- 本地规则：未发现语法错误或已知高风险模式。",
                "- 边界：这不等于代码没有逻辑、并发或业务问题；模型语义分析本轮未完成。",
            )
        )
        return "\n".join(lines)

    lines.append("- 确定问题：")
    recommendations: list[str] = []
    for index, finding in enumerate(valid_findings, start=1):
        severity = str(finding.get("severity", "info"))
        message = str(finding.get("message", "发现需要检查的问题。"))
        lines.append(
            f"  {index}. [{_SEVERITY_LABELS.get(severity, severity)}] {message}"
        )
        recommendation = _recommendation(message)
        if recommendation and recommendation not in recommendations:
            recommendations.append(recommendation)
    if recommendations:
        lines.append("- 修改建议：")
        lines.extend(
            f"  {index}. {recommendation}"
            for index, recommendation in enumerate(recommendations, start=1)
        )
    lines.append("- 边界：以上来自确定性静态检查；需要运行时或业务上下文才能确认的问题仍需进一步验证。")
    return "\n".join(lines)


# 作用：根据静态检查问题文本匹配一条可执行的修复建议。
# 参数 message：单条确定性检查发现的说明。
# 返回：对应建议；没有专用规则时返回空字符串。
def _recommendation(message: str) -> str:
    if "eval" in message:
        return "移除 `eval`；按真实输入格式使用显式数值运算、JSON 解析或受限映射，并先校验输入类型。"
    if "exec" in message:
        return "移除 `exec`，把允许执行的动作改成显式函数或命令白名单。"
    if "shell=True" in message:
        return "使用参数数组并保持 `shell=False`；如果必须调用外部程序，再对白名单和每个参数做校验。"
    if "密钥" in message or "密码" in message:
        return "把凭据移到本机密钥配置或环境变量；若该值曾经有效，应立即轮换。"
    if "静默吞掉" in message:
        return "只捕获预期异常，并选择记录、重新抛出或返回明确错误，避免无声地返回 `None`。"
    if "语法错误" in message:
        return "先修复语法错误并通过解析/编译检查，再继续判断运行逻辑。"
    if "TODO" in message or "FIXME" in message:
        return "把 TODO/FIXME 转成可追踪任务，注明完成条件和失败处理。"
    return ""


# 作用：把内部模型降级原因转换成面向用户的简短描述。
# 参数 reason：稳定的模型降级原因代码。
# 返回：等待超时、暂时不可用或未完成等标签。
def _degradation_label(reason: str) -> str:
    if "timeout" in reason:
        return "等待超时"
    if reason:
        return "暂时不可用"
    return "未完成"


# 作用：把可能来自 JSON 的列表安全转换为非空字符串列表。
# 参数 value：待检查和转换的任意值。
# 返回：清理后的字符串列表；非列表输入返回空列表。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
