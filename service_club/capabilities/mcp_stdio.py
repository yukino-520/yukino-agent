from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# 作用：表示 MCP stdio 进程通信或协议调用失败。
# 参数：无。
class McpStdioError(RuntimeError):
    pass


# 作用：表示 MCP stdio 服务配置不合法或不安全。
# 参数：无。
class McpStdioConfigurationError(ValueError):
    pass


_SAFE_SERVER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_BLOCKED_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "HOME",
    "LD_LIBRARY_PATH",
    "NODE_OPTIONS",
    "PATH",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "SHELL",
    "ZDOTDIR",
}


# 作用：保存一个允许启动的 MCP stdio 服务及其资源限制。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“McpStdioServer”相关的数据结构、异常类型或服务组件。
# 字段：name：该对象中的结构化字段。、command：该对象中的结构化字段。、args：该对象中的结构化字段。、cwd：该对象中的结构化字段。、env：该对象中的结构化字段。、timeout_seconds：该对象中的结构化字段。
class McpStdioServer:
    name: str
    command: str
    args: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    timeout_seconds: float

    # 作用：返回不包含敏感环境变量的服务公开信息。
    # 参数：无。
    def public(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "env_keys": sorted(self.env),
        }


# 作用：解析并校验 MCP stdio 服务白名单、命令和环境变量配置。
# 参数 raw_servers：尚未校验的 MCP stdio 服务配置对象。
# 参数 raw_environment：允许注入 MCP 子进程的环境变量白名单配置。
# 参数 max_servers：一次允许配置的 MCP stdio 服务数量上限。
def parse_stdio_servers(
    raw_servers: object,
    raw_environment: object | None = None,
    *,
    max_servers: int = 20,
) -> dict[str, McpStdioServer]:
    if raw_servers in (None, ""):
        return {}
    if not isinstance(raw_servers, dict):
        raise McpStdioConfigurationError("stdio MCP 服务配置必须是 JSON 对象。")
    if len(raw_servers) > max_servers:
        raise McpStdioConfigurationError(f"stdio MCP 服务最多配置 {max_servers} 个。")
    environment = raw_environment if isinstance(raw_environment, dict) else {}
    result: dict[str, McpStdioServer] = {}
    for raw_name, raw_config in raw_servers.items():
        name = str(raw_name).strip()
        if not _SAFE_SERVER_NAME.fullmatch(name):
            raise McpStdioConfigurationError(f"stdio MCP 服务名格式无效：{name[:80]}")
        if not isinstance(raw_config, dict):
            raise McpStdioConfigurationError(f"stdio MCP 服务 {name} 的配置必须是对象。")
        command = str(raw_config.get("command", "")).strip()
        if (
            not command
            or len(command) > 1000
            or any(character in command for character in "\0\r\n")
        ):
            raise McpStdioConfigurationError(f"stdio MCP 服务 {name} 缺少有效 command。")
        if any(character.isspace() for character in command):
            raise McpStdioConfigurationError(
                f"stdio MCP 服务 {name} 的 command 不能包含参数；请使用 args 数组。"
            )
        raw_args = raw_config.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > 40:
            raise McpStdioConfigurationError(
                f"stdio MCP 服务 {name} 的 args 必须是不超过 40 项的数组。"
            )
        args = tuple(str(value) for value in raw_args)
        if any(
            len(value) > 4000 or any(character in value for character in "\0\r\n")
            for value in args
        ):
            raise McpStdioConfigurationError(f"stdio MCP 服务 {name} 的参数无效。")
        cwd = str(raw_config.get("cwd", "")).strip()
        if cwd:
            cwd_path = Path(cwd).expanduser()
            if not cwd_path.is_absolute():
                raise McpStdioConfigurationError(
                    f"stdio MCP 服务 {name} 的 cwd 必须是绝对路径。"
                )
            cwd = str(cwd_path.resolve())
        try:
            timeout_seconds = float(raw_config.get("timeout_seconds", 20))
        except (TypeError, ValueError) as exc:
            raise McpStdioConfigurationError(
                f"stdio MCP 服务 {name} 的 timeout_seconds 无效。"
            ) from exc
        if not 1 <= timeout_seconds <= 120:
            raise McpStdioConfigurationError(
                f"stdio MCP 服务 {name} 的超时必须在 1 到 120 秒之间。"
            )
        raw_env = environment.get(name, {})
        if raw_env in (None, ""):
            raw_env = {}
        if not isinstance(raw_env, dict) or len(raw_env) > 30:
            raise McpStdioConfigurationError(
                f"stdio MCP 服务 {name} 的环境变量必须是不超过 30 项的对象。"
            )
        env: dict[str, str] = {}
        for raw_key, raw_value in raw_env.items():
            key = str(raw_key).strip()
            value = str(raw_value)
            if not _SAFE_ENV_NAME.fullmatch(key) or key.upper() in _BLOCKED_ENV_NAMES:
                raise McpStdioConfigurationError(
                    f"stdio MCP 服务 {name} 的环境变量 {key[:128]} 不允许覆盖进程边界。"
                )
            if len(value) > 4096 or any(character in value for character in "\0\r\n"):
                raise McpStdioConfigurationError(
                    f"stdio MCP 服务 {name} 的环境变量 {key} 内容无效。"
                )
            env[key] = value
        result[name] = McpStdioServer(
            name=name,
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
    unknown_environment = sorted(set(map(str, environment)) - set(result))
    if unknown_environment:
        raise McpStdioConfigurationError(
            f"stdio MCP 环境变量引用了未配置服务：{', '.join(unknown_environment[:5])}"
        )
    return result


# 作用：异步读取子进程日志，并只保留限定字节数的尾部内容。
# 参数：无。
class _BoundedLog:
    # 作用：启动后台线程持续排空日志流，防止子进程阻塞。
    # 参数 limit：本次读取、搜索或缓冲允许返回的最大数量。
    def __init__(self, limit: int = 8192) -> None:
        self.limit = max(0, limit)
        self._value = bytearray()
        self._total_bytes = 0
        self._lock = threading.Lock()
        self._closed = False
        read_fd, write_fd = os.pipe()
        self.stream = os.fdopen(
            write_fd,
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        self._reader = os.fdopen(read_fd, "rb", buffering=0)
        self._thread = threading.Thread(
            target=self._drain,
            name="mcp-stderr-drain",
            daemon=True,
        )
        self._thread.start()

    # 作用：读取日志流并维护固定大小的尾部缓冲区。
    # 参数：无。
    def _drain(self) -> None:
        try:
            while chunk := self._reader.read(4096):
                with self._lock:
                    self._total_bytes += len(chunk)
                    remaining = max(0, self.limit - len(self._value))
                    if remaining:
                        self._value.extend(chunk[:remaining])
        finally:
            self._reader.close()

    # 作用：关闭日志读取器并短暂等待后台线程退出。
    # 参数：无。
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.stream.close()
        finally:
            self._thread.join(timeout=1)

    # 作用：返回已捕获日志的 UTF-8 容错文本。
    # 参数：无。
    @property
    # 作用：执行“value”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def value(self) -> str:
        with self._lock:
            return bytes(self._value).decode("utf-8", errors="replace")

    # 作用：返回日志流累计产生的字节数。
    # 参数：无。
    @property
    # 作用：执行“total_bytes”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes


# 作用：通过受限子进程生命周期调用本地 MCP stdio 服务。
# 参数：无。
class McpStdioClient:
    """One isolated MCP lifecycle per call using the official Python SDK."""

    MAX_RESULT_BYTES = 2 * 1024 * 1024

    # 作用：校验服务命令并在独立事件循环中完成一次 MCP 调用。
    # 参数 server：MCP 服务名称或其经过校验的配置。
    # 参数 method：MCP 或 HTTP 请求使用的方法名称。
    # 参数 params：发送给 MCP 方法的 JSON-RPC 参数对象。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 idempotency_key：确保重复请求复用同一结果的幂等键。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    def invoke(
        self,
        server: McpStdioServer,
        *,
        method: str,
        params: dict[str, Any],
        request_id: str,
        idempotency_key: str = "",
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if cancel_check is not None and cancel_check():
            raise McpStdioError("stdio MCP 调用已取消。")
        command = self._resolve_command(server.command)
        if server.cwd and not Path(server.cwd).is_dir():
            raise McpStdioError("stdio MCP 工作目录不存在。")
        try:
            result = asyncio.run(
                self._invoke_async(
                    server,
                    command=command,
                    method=method,
                    params=params,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    cancel_check=cancel_check,
                )
            )
        except McpStdioError:
            raise
        except Exception as exc:
            raise McpStdioError(
                f"stdio MCP 调用失败（{type(exc).__name__}）。"
            ) from exc
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > self.MAX_RESULT_BYTES:
            raise McpStdioError("stdio MCP 响应超过 2 MiB 限制。")
        return result

    # 作用：启动 MCP 子进程，执行调用并保证超时或取消时清理进程树。
    # 参数 server：MCP 服务名称或其经过校验的配置。
    # 参数 command：待校验或启动的无 Shell 命令及参数。
    # 参数 method：MCP 或 HTTP 请求使用的方法名称。
    # 参数 params：发送给 MCP 方法的 JSON-RPC 参数对象。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 idempotency_key：确保重复请求复用同一结果的幂等键。
    # 参数 cancel_check：返回真时中止当前长耗时操作的回调。
    async def _invoke_async(
        self,
        server: McpStdioServer,
        *,
        command: str,
        method: str,
        params: dict[str, Any],
        request_id: str,
        idempotency_key: str,
        cancel_check: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        task = asyncio.create_task(
            self._connected_call(
                server,
                command=command,
                method=method,
                params=params,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        )
        deadline = asyncio.get_running_loop().time() + server.timeout_seconds + 5
        while not task.done():
            if cancel_check is not None and cancel_check():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise McpStdioError("stdio MCP 调用已取消。")
            if asyncio.get_running_loop().time() >= deadline:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise McpStdioError("stdio MCP 调用超时，子进程已经终止。")
            await asyncio.sleep(0.05)
        return await task

    # 作用：完成 MCP 握手、工具调用和 JSON-RPC 响应校验。
    # 参数 server：MCP 服务名称或其经过校验的配置。
    # 参数 command：待校验或启动的无 Shell 命令及参数。
    # 参数 method：MCP 或 HTTP 请求使用的方法名称。
    # 参数 params：发送给 MCP 方法的 JSON-RPC 参数对象。
    # 参数 request_id：用户请求或 JSON-RPC 调用的唯一标识。
    # 参数 idempotency_key：确保重复请求复用同一结果的幂等键。
    async def _connected_call(
        self,
        server: McpStdioServer,
        *,
        command: str,
        method: str,
        params: dict[str, Any],
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            from mcp import Client, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpStdioError(
                "stdio MCP 需要安装 mcp>=2.1,<3。"
            ) from exc
        log = _BoundedLog()
        parameters = StdioServerParameters(
            command=command,
            args=list(server.args),
            env=dict(server.env) or None,
            cwd=server.cwd or None,
        )
        transport = stdio_client(parameters, errlog=log.stream)
        try:
            async with Client(
                transport,
                read_timeout_seconds=server.timeout_seconds,
                raise_exceptions=False,
            ) as client:
                meta = {
                    "agi-yukino.dev/request-id": request_id,
                    **(
                        {"agi-yukino.dev/idempotency-key": idempotency_key}
                        if idempotency_key
                        else {}
                    ),
                }
                if method == "initialize":
                    return {
                        "protocolVersion": str(client.protocol_version or ""),
                        "serverInfo": self._dump(client.server_info),
                        "capabilities": self._dump(client.server_capabilities),
                    }
                if method == "tools/list":
                    response = await client.list_tools(
                        cursor=self._optional_string(params.get("cursor")),
                        meta=meta,
                        cache_mode="bypass",
                    )
                elif method == "tools/call":
                    name = str(params.get("name", "")).strip()
                    arguments = params.get("arguments", {})
                    if not name or not isinstance(arguments, dict):
                        raise McpStdioError("tools/call 需要 name 和 arguments 对象。")
                    response = await client.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=server.timeout_seconds,
                        meta=meta,
                    )
                elif method == "resources/list":
                    response = await client.list_resources(
                        cursor=self._optional_string(params.get("cursor")),
                        meta=meta,
                        cache_mode="bypass",
                    )
                elif method == "resources/read":
                    uri = str(params.get("uri", "")).strip()
                    if not uri:
                        raise McpStdioError("resources/read 需要 uri。")
                    response = await client.read_resource(
                        uri,
                        meta=meta,
                        cache_mode="bypass",
                    )
                else:
                    raise McpStdioError("MCP 方法不在允许列表。")
                result = self._dump(response)
                if method == "tools/call" and bool(result.get("isError")):
                    raise McpStdioError("MCP 工具返回 isError=true。")
                return result
        except McpStdioError:
            raise
        except Exception as exc:
            log.close()
            stderr_note = (
                f"；server stderr 有 {log.total_bytes} 字节（内容未暴露）"
                if log.total_bytes
                else ""
            )
            raise McpStdioError(
                f"stdio MCP 生命周期失败（{type(exc).__name__}）{stderr_note}。"
            ) from exc
        finally:
            log.close()

    # 作用：只允许可解析到绝对路径且满足安全条件的可执行命令。
    # 参数 command：待校验或启动的无 Shell 命令及参数。
    @staticmethod
    # 作用：执行“resolve_command”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 command：调用方传入的command，用于本次处理。
    def _resolve_command(command: str) -> str:
        path = Path(command).expanduser()
        if path.is_absolute():
            if not path.is_file() or not os.access(path, os.X_OK):
                raise McpStdioError("stdio MCP command 不是可执行文件。")
            # Preserve virtual-environment launchers instead of resolving their
            # symlink to the base interpreter, otherwise installed MCP packages
            # and the environment's sys.path would be lost.
            return str(path)
        if "/" in command or "\\" in command:
            raise McpStdioError("stdio MCP command 只能使用绝对路径或 PATH 中的程序名。")
        resolved_command = shutil.which(command)
        if not resolved_command:
            raise McpStdioError(f"找不到 stdio MCP command：{command}")
        return str(Path(resolved_command).absolute())

    # 作用：把协议对象稳定序列化为紧凑 JSON。
    # 参数 value：待解析、清洗、转义或递归处理的输入值。
    @staticmethod
    # 作用：执行“dump”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _dump(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
            return dumped if isinstance(dumped, dict) else {"value": dumped}
        if isinstance(value, dict):
            return value
        return {"value": str(value)}

    # 作用：把可选协议字段规范化为字符串或空值。
    # 参数 value：待解析、清洗、转义或递归处理的输入值。
    @staticmethod
    # 作用：执行“optional_string”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _optional_string(value: Any) -> str | None:
        normalized = str(value).strip() if value is not None else ""
        return normalized or None
