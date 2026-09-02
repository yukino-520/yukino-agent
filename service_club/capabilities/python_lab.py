from __future__ import annotations

import ast
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


# 作用：表示实验代码不符合安全策略或隔离执行失败。
# 参数：无。
class PythonLabError(ValueError):
    pass


# 作用：在语言白名单和操作系统沙箱中运行短小的 Python 实验。
# 参数：无。
class PythonLab:
    """Run small calculations with language and operating-system isolation."""

    MAX_CODE_CHARS = 12_000
    MAX_STDOUT_BYTES = 1_048_576
    MAX_STDERR_BYTES = 262_144
    FORBIDDEN_NODES = (
        ast.Import,
        ast.ImportFrom,
        ast.Global,
        ast.Nonlocal,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.Raise,
        ast.ClassDef,
        ast.Lambda,
        ast.GeneratorExp,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
    )
    FORBIDDEN_CALLS = {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "type",
        "object",
        "help",
    }
    FORBIDDEN_ATTRIBUTES = {
        "ag_frame",
        "cr_frame",
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_locals",
        "gi_code",
        "gi_frame",
        "mro",
        "modules",
        "popen",
        "system",
        "tb_frame",
    }
    SAFE_VALUE_ATTRIBUTES = {
        "add",
        "append",
        "cdf",
        "clear",
        "conjugate",
        "copy",
        "count",
        "denominator",
        "difference",
        "discard",
        "endswith",
        "extend",
        "find",
        "get",
        "index",
        "insert",
        "intercept",
        "intersection",
        "inv_cdf",
        "is_integer",
        "isalnum",
        "isalpha",
        "isdecimal",
        "isdigit",
        "islower",
        "isnumeric",
        "isspace",
        "isupper",
        "items",
        "join",
        "keys",
        "lower",
        "lstrip",
        "numerator",
        "overlap",
        "pop",
        "popitem",
        "remove",
        "replace",
        "reverse",
        "rstrip",
        "setdefault",
        "slope",
        "sort",
        "split",
        "splitlines",
        "startswith",
        "strip",
        "swapcase",
        "symmetric_difference",
        "title",
        "union",
        "update",
        "upper",
        "values",
        "zscore",
    }
    SAFE_MATH_ATTRIBUTES = {
        "acos",
        "acosh",
        "asin",
        "asinh",
        "atan",
        "atan2",
        "atanh",
        "cbrt",
        "ceil",
        "comb",
        "copysign",
        "cos",
        "cosh",
        "degrees",
        "dist",
        "e",
        "erf",
        "erfc",
        "exp",
        "exp2",
        "expm1",
        "fabs",
        "factorial",
        "floor",
        "fmod",
        "frexp",
        "fsum",
        "gamma",
        "gcd",
        "hypot",
        "inf",
        "isclose",
        "isfinite",
        "isinf",
        "isnan",
        "isqrt",
        "lcm",
        "ldexp",
        "lgamma",
        "log",
        "log10",
        "log1p",
        "log2",
        "modf",
        "nan",
        "nextafter",
        "perm",
        "pi",
        "pow",
        "prod",
        "radians",
        "remainder",
        "sin",
        "sinh",
        "sqrt",
        "sumprod",
        "tan",
        "tanh",
        "tau",
        "trunc",
        "ulp",
    }
    SAFE_STATISTICS_ATTRIBUTES = {
        "NormalDist",
        "correlation",
        "covariance",
        "fmean",
        "geometric_mean",
        "harmonic_mean",
        "linear_regression",
        "mean",
        "median",
        "median_grouped",
        "median_high",
        "median_low",
        "mode",
        "multimode",
        "pstdev",
        "pvariance",
        "quantiles",
        "stdev",
        "variance",
    }
    RUNNER = r'''
import json
import math as _math
import resource as _resource
import statistics as _statistics
import sys as _sys

class _SafeNamespace:
    __slots__ = ("_values",)
    def __init__(self, source, names):
        object.__setattr__(self, "_values", {name: getattr(source, name) for name in names})
    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError("private attributes are unavailable")
        values = object.__getattribute__(self, "_values")
        if name not in values:
            raise AttributeError(f"attribute {name!r} is unavailable")
        return values[name]

payload = json.loads(_sys.stdin.read())
_resource.setrlimit(_resource.RLIMIT_CPU, (4, 4))
_resource.setrlimit(_resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
try:
    _resource.setrlimit(_resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
except (ValueError, OSError):
    pass
try:
    _resource.setrlimit(_resource.RLIMIT_CORE, (0, 0))
except (ValueError, OSError):
    pass
try:
    _resource.setrlimit(_resource.RLIMIT_NOFILE, (16, 16))
except (ValueError, OSError):
    pass
try:
    _resource.setrlimit(_resource.RLIMIT_NPROC, (0, 0))
except (AttributeError, ValueError, OSError):
    pass
safe_builtins = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter,
    "float": float, "int": int, "len": len, "list": list, "map": map,
    "max": max, "min": min, "ord": ord, "pow": pow, "print": print,
    "range": range, "reversed": reversed, "round": round, "set": set,
    "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "zip": zip,
}
scope = {
    "__builtins__": safe_builtins,
    "math": _SafeNamespace(_math, payload["math_names"]),
    "statistics": _SafeNamespace(_statistics, payload["statistics_names"]),
}
exec(compile(payload["code"], "<yukino-lab>", "exec"), scope, scope)
'''

    # 作用：创建隔离进程使用的实验工作目录。
    # 参数 workspace：隔离 Python 实验使用的工作目录。
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    # 作用：审查代码后在资源受限子进程中执行，并截取标准输出和错误。
    # 参数 code：待安全审查或隔离执行的 Python 源码。
    # 参数 timeout_seconds：网络、代码或任务执行允许持续的最长秒数。
    def run(self, code: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        self._validate(code)
        command, sandbox = self._command()
        payload = json.dumps(
            {
                "code": code,
                "math_names": sorted(self.SAFE_MATH_ATTRIBUTES),
                "statistics_names": sorted(self.SAFE_STATISTICS_ATTRIBUTES),
            }
        )
        try:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                completed = subprocess.run(
                    command,
                    input=payload.encode("utf-8"),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=self.workspace,
                    timeout=max(0.2, min(timeout_seconds, 10.0)),
                    env={},
                    check=False,
                    start_new_session=os.name != "nt",
                )
                stdout = self._read_tail(stdout_file, self.MAX_STDOUT_BYTES)
                stderr = self._read_tail(stderr_file, self.MAX_STDERR_BYTES)
        except subprocess.TimeoutExpired as exc:
            raise PythonLabError("代码执行超时，隔离进程已经终止。") from exc
        if sandbox["required"] and completed.returncode == 71 and "sandbox_apply" in stderr:
            raise PythonLabError("系统级 Python 沙箱无法启用，已拒绝降级执行。")
        return {
            "ok": completed.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": completed.returncode,
            "sandbox": sandbox,
        }

    # 作用：报告当前沙箱模式、可用后端和被禁止的资源能力。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        mode = self._requested_mode()
        backend = self._os_backend()
        os_enforced = mode == "strict" and bool(backend)
        return {
            "ok": os_enforced,
            "operational": bool(backend) or mode == "language",
            "requested_mode": mode,
            "backend": backend or "unavailable",
            "strict_available": bool(backend),
            "fail_closed": mode != "language",
            "os_enforced": os_enforced,
            "enforcement": (
                "operating_system" if os_enforced else "language_allowlist_only"
            ),
            "network_access": False,
            "workspace_file_access": False,
            "external_file_access": False,
            "process_spawn": False,
            "language_allowlist": True,
            "max_code_chars": self.MAX_CODE_CHARS,
            "max_stdout_bytes": self.MAX_STDOUT_BYTES,
            "max_stderr_bytes": self.MAX_STDERR_BYTES,
        }

    # 作用：通过 AST 白名单拒绝导入、危险名称和对象图逃逸语法。
    # 参数 code：待安全审查或隔离执行的 Python 源码。
    def _validate(self, code: str) -> None:
        if not code.strip():
            raise PythonLabError("代码不能为空。")
        if len(code) > self.MAX_CODE_CHARS:
            raise PythonLabError("代码超过 12000 字符限制。")
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            raise PythonLabError(f"Python 语法错误：{exc.msg}") from exc
        for node in ast.walk(tree):
            if isinstance(node, self.FORBIDDEN_NODES):
                raise PythonLabError(f"不允许的语法：{type(node).__name__}")
            if isinstance(node, ast.Name) and (
                node.id.startswith("_") or node.id in self.FORBIDDEN_CALLS
            ):
                raise PythonLabError(f"不允许访问名称 {node.id}。")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "__" in node.value
            ):
                raise PythonLabError("字符串中不允许双下划线对象图访问。")
            if isinstance(node, ast.Attribute):
                self._validate_attribute(node)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in self.FORBIDDEN_CALLS
            ):
                raise PythonLabError(f"不允许调用 {node.func.id}。")

    # 作用：仅允许安全值属性及 math、statistics 白名单成员。
    # 参数 node：待检查属性访问是否合法的 AST 节点。
    def _validate_attribute(self, node: ast.Attribute) -> None:
        attribute = node.attr
        if attribute.startswith("_") or attribute in self.FORBIDDEN_ATTRIBUTES:
            raise PythonLabError(f"不允许访问属性 {attribute}。")
        if isinstance(node.value, ast.Name) and node.value.id in {"math", "statistics"}:
            allowed = (
                self.SAFE_MATH_ATTRIBUTES
                if node.value.id == "math"
                else self.SAFE_STATISTICS_ATTRIBUTES
            )
            if attribute not in allowed:
                raise PythonLabError(f"{node.value.id}.{attribute} 不在安全白名单中。")
            return
        if attribute not in self.SAFE_VALUE_ATTRIBUTES:
            raise PythonLabError(f"对象属性 {attribute} 不在安全白名单中。")

    # 作用：按安全模式构造隔离 Python 命令，严格模式不可降级执行。
    # 参数：无。
    def _command(self) -> tuple[list[str], dict[str, Any]]:
        mode = self._requested_mode()
        base_executable = Path(
            getattr(sys, "_base_executable", sys.executable)
        ).resolve()
        python_command = [str(base_executable), "-I", "-S", "-c", self.RUNNER]
        backend = self._os_backend()
        if mode == "language":
            return python_command, self._sandbox_metadata("language", required=False)
        if not backend:
            raise PythonLabError(
                "当前系统没有可用的进程级 Python 沙箱；为避免越权，代码未执行。"
            )
        if backend == "macos-sandbox-exec":
            profile = self._macos_profile(base_executable)
            return (
                ["/usr/bin/sandbox-exec", "-p", profile, *python_command],
                self._sandbox_metadata(backend, required=True),
            )
        raise PythonLabError("当前系统级 Python 沙箱后端尚未实现。")

    # 作用：读取 Python 沙箱模式，只接受严格模式或显式语言模式。
    # 参数：无。
    @staticmethod
    # 作用：执行“requested_mode”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _requested_mode() -> str:
        value = os.getenv("YUKINO_PYTHON_SANDBOX_MODE", "strict").strip().lower()
        return "language" if value == "language" else "strict"

    # 作用：探测当前系统可用的进程级沙箱后端。
    # 参数：无。
    @staticmethod
    # 作用：执行“os_backend”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def _os_backend() -> str:
        if platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").exists():
            return "macos-sandbox-exec"
        return ""

    # 作用：为 macOS sandbox-exec 生成最小文件和进程权限配置。
    # 参数 base_executable：构建沙箱策略时允许启动的 Python 基础可执行文件。
    def _macos_profile(self, base_executable: Path) -> str:
        runtime_root = base_executable.parent.parent.resolve()
        resource_executable = (
            runtime_root / "Resources/Python.app/Contents/MacOS/Python"
        )
        executable_rules = [
            f'(literal "{self._sbpl_escape(str(base_executable))}")',
        ]
        if resource_executable.exists():
            executable_rules.append(
                f'(literal "{self._sbpl_escape(str(resource_executable))}")'
            )
        return "".join(
            (
                "(version 1)",
                "(deny default)",
                '(import "system.sb")',
                f"(allow process-exec {' '.join(executable_rules)})",
                "(allow file-read-metadata)",
                f'(allow file-read* (subpath "{self._sbpl_escape(str(runtime_root))}"))',
            )
        )

    # 作用：转义写入 macOS 沙箱策略字符串的路径。
    # 参数 value：待解析、清洗、转义或递归处理的输入值。
    @staticmethod
    # 作用：执行“sbpl_escape”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _sbpl_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    # 作用：生成说明沙箱强度和资源隔离范围的审计元数据。
    # 参数 backend：所使用的存储、执行后端或后端标识。
    # 参数 required：是否要求操作系统级沙箱且禁止降级。
    @staticmethod
    # 作用：执行“sandbox_metadata”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 backend：实际执行数据库操作的后端适配器。
    # 参数 required：是否要求目标配置或能力必须存在。
    def _sandbox_metadata(backend: str, *, required: bool) -> dict[str, Any]:
        return {
            "backend": backend,
            "required": required,
            "os_enforced": required,
            "enforcement": (
                "operating_system" if required else "language_allowlist_only"
            ),
            "network_access": False,
            "workspace_file_access": False,
            "external_file_access": False,
            "process_spawn": False,
        }

    # 作用：从临时输出流末尾读取限定字节数，避免返回过量内容。
    # 参数 stream：保存子进程输出的可定位二进制流。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    @staticmethod
    # 作用：执行“read_tail”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 stream：调用方传入的stream，用于本次处理。
    # 参数 limit：返回结果的最大数量。
    def _read_tail(stream: Any, limit: int) -> str:
        stream.flush()
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read(limit).decode("utf-8", errors="replace")
