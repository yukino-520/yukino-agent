from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# 作用：表示本机任务白名单配置的字段或参数不合法。
# 参数：无。
class SystemTaskConfigurationError(ValueError):
    pass


_TASK_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_ALLOWED_FIELDS = {"command", "cwd", "timeout_seconds", "description"}


# 作用：描述无需 Shell 展开的固定命令、工作目录和运行上限。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“SystemTaskSpec”相关的数据结构、异常类型或服务组件。
# 字段：name：该对象中的结构化字段。、command：该对象中的结构化字段。、cwd：该对象中的结构化字段。、timeout_seconds：该对象中的结构化字段。、description：该对象中的结构化字段。
class SystemTaskSpec:
    name: str
    command: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    description: str

    # 作用：返回适合展示和审计的任务配置。
    # 参数：无。
    def public(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            **({"description": self.description} if self.description else {}),
        }

    # 作用：在项目目录和 Agent 工作区之间选择任务执行目录。
    # 参数 workspace_root：Agent 默认可写工作区的根目录。
    # 参数 project_root：允许读取或执行项目资源的根目录。
    def working_directory(self, *, workspace_root: Path, project_root: Path) -> Path:
        return project_root if self.cwd == "project" else workspace_root

    # 作用：计算任务配置的稳定 SHA-256 指纹，用于确认绑定。
    # 参数：无。
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.public(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


# 作用：解析并严格校验本机任务白名单，拒绝未知字段和危险参数。
# 参数 raw：尚未规范化的原始配置、JSON 文本或底层连接对象。
# 参数 max_tasks：一次允许配置的本机任务数量上限。
def parse_system_tasks(
    raw: object,
    *,
    max_tasks: int = 20,
) -> dict[str, SystemTaskSpec]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise SystemTaskConfigurationError("本机任务配置必须是 JSON 对象。")
    if len(raw) > max_tasks:
        raise SystemTaskConfigurationError(f"本机任务最多配置 {max_tasks} 个。")
    result: dict[str, SystemTaskSpec] = {}
    for raw_name, raw_config in raw.items():
        name = str(raw_name).strip()
        if not _TASK_NAME.fullmatch(name):
            raise SystemTaskConfigurationError("本机任务名称格式无效。")
        if isinstance(raw_config, list):
            command_value = raw_config
            cwd = "workspace"
            timeout_value: object = 30
            description = ""
        elif isinstance(raw_config, dict):
            unknown = sorted(set(map(str, raw_config)) - _ALLOWED_FIELDS)
            if unknown:
                raise SystemTaskConfigurationError(
                    f"本机任务 {name} 包含未知字段：{', '.join(unknown[:5])}。"
                )
            command_value = raw_config.get("command", [])
            cwd = str(raw_config.get("cwd", "workspace")).strip().lower()
            timeout_value = raw_config.get("timeout_seconds", 30)
            description = str(raw_config.get("description", "")).strip()
        else:
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 必须是命令数组或结构化对象。"
            )
        if (
            not isinstance(command_value, list)
            or not 1 <= len(command_value) <= 40
        ):
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 的命令参数必须为 1 到 40 项。"
            )
        command = tuple(str(item) for item in command_value)
        if any(
            not item
            or len(item) > 4000
            or any(character in item for character in "\0\r\n")
            for item in command
        ):
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 的命令参数无效。"
            )
        if cwd not in {"workspace", "project"}:
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 的 cwd 只能是 workspace 或 project。"
            )
        try:
            timeout_seconds = int(timeout_value)
        except (TypeError, ValueError) as exc:
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 的 timeout_seconds 无效。"
            ) from exc
        if not 1 <= timeout_seconds <= 120:
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 的超时必须在 1 到 120 秒之间。"
            )
        if len(description) > 200 or any(
            character in description for character in "\0\r\n"
        ):
            raise SystemTaskConfigurationError(
                f"本机任务 {name} 的说明无效。"
            )
        result[name] = SystemTaskSpec(
            name=name,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            description=description,
        )
    return result
