from __future__ import annotations

import ast
import re
from typing import Any


# 作用：对代码做确定性静态检查，提取结构事实和已知高风险模式。
# 参数 code：用户提供的源码文本。
# 返回：语言、行数、函数、类、依赖和规则发现组成的字典。
def inspect_code(code: str) -> dict[str, Any]:
    """Return deterministic facts that the model can use for a grounded review."""
    source = code.strip()
    if not source:
        raise ValueError("没有找到需要分析的代码。")
    language = _detect_language(source)
    lines = source.splitlines()
    findings: list[dict[str, str]] = []
    facts: dict[str, Any] = {
        "language": language,
        "line_count": len(lines),
        "non_empty_lines": sum(bool(item.strip()) for item in lines),
    }
    if language == "python":
        try:
            tree = ast.parse(source)
            facts.update(
                {
                    "functions": [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))],
                    "classes": [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)],
                    "imports": [
                        alias.name
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                        for alias in node.names
                    ],
                }
            )
        except SyntaxError as exc:
            findings.append(
                {
                    "severity": "error",
                    "kind": "syntax",
                    "message": f"第 {exc.lineno or '?'} 行存在语法错误：{exc.msg}",
                }
            )
    patterns = (
        (r"\beval\s*\(", "high", "security", "使用 eval 处理不可信输入可能导致代码执行。"),
        (r"\bexec\s*\(", "high", "security", "使用 exec 会扩大任意代码执行风险。"),
        (r"shell\s*=\s*True", "high", "security", "shell=True 需要严格控制命令输入。"),
        (r"(?:api[_-]?key|password|secret)\s*=\s*['\"][^'\"]+", "high", "secret", "疑似把密钥或密码硬编码在源码中。"),
        (r"except\s*(?:Exception)?\s*:\s*(?:\n\s*)?pass\b", "medium", "reliability", "异常被静默吞掉，可能掩盖真实故障。"),
        (r"\bTODO\b|\bFIXME\b", "info", "maintenance", "代码中存在尚未处理的 TODO/FIXME。"),
    )
    for pattern, severity, kind, message in patterns:
        if re.search(pattern, source, flags=re.IGNORECASE | re.MULTILINE):
            findings.append({"severity": severity, "kind": kind, "message": message})
    facts["findings"] = findings
    facts["review_instruction"] = (
        "请结合用户原始代码继续做语义分析：先说明用途，再按正确性、安全性、可维护性和性能给出具体问题与修改建议；"
        "无法仅凭静态信息确认的内容必须明确说明。"
    )
    return facts


# 作用：根据各语言常见语法特征粗略识别源码语言。
# 参数 code：需要识别语言的源码文本。
# 返回：python、javascript 等语言名称，无法判断时返回 unknown。
def _detect_language(code: str) -> str:
    scores = {
        "python": len(re.findall(r"(?:^|\n)\s*(?:def |class |from \w+ import |import \w+|if __name__)", code)),
        "javascript": len(re.findall(r"\b(?:const|let|function|async function|console\.log)\b|=>", code)),
        "typescript": len(re.findall(r"\b(?:interface|type|enum)\s+\w+|:\s*(?:string|number|boolean)\b", code)),
        "java": len(re.findall(r"\b(?:public class|private |protected |static void main|System\.out)\b", code)),
        "go": len(re.findall(r"(?:^|\n)\s*(?:package |func |import \()", code)),
        "rust": len(re.findall(r"\b(?:fn main|let mut|impl|pub fn|use \w+::)\b", code)),
    }
    language, score = max(scores.items(), key=lambda item: item[1])
    return language if score else "unknown"
