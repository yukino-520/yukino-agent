from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

RUNTIME_CONFIG_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_FALLBACK_MODEL",
        "OPENAI_EMBEDDING_API_KEY",
        "OPENAI_EMBEDDING_BASE_URL",
        "OPENAI_EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_TIMEOUT_SECONDS",
        "OPENAI_TIMEOUT_SECONDS",
        "YUKINO_MODEL_MAX_CONCURRENT",
        "YUKINO_MODEL_CIRCUIT_FAILURES",
        "YUKINO_MODEL_CIRCUIT_COOLDOWN_SECONDS",
        "OPENAI_TTS_MODEL",
        "TTS_ENABLED",
        "YUKINO_TTS_VOICE_YUKINO",
        "YUKINO_TTS_VOICE_YUI",
        "YUKINO_TTS_VOICE_HACHIMAN",
        "YUKINO_TTS_VOICE_IROHA",
        "YUKINO_TTS_VOICE_SHIZUKA",
        "YUKINO_MCP_ENDPOINTS",
        "YUKINO_MCP_HEADERS",
        "YUKINO_MCP_STDIO_ENABLED",
        "YUKINO_MCP_STDIO_SERVERS",
        "YUKINO_MCP_STDIO_ENV",
        "YUKINO_MCP_ALLOW_PRIVATE_HOSTNAMES",
        "YUKINO_OUTBOUND_WEBHOOK",
        "YUKINO_OUTBOUND_WEBHOOK_SECRET",
        "YUKINO_INBOUND_WEBHOOK_SECRET",
        "YUKINO_BACKGROUND_REMINDER_DELIVERY",
        "YUKINO_SMTP_HOST",
        "YUKINO_SMTP_PORT",
        "YUKINO_SMTP_USERNAME",
        "YUKINO_SMTP_PASSWORD",
        "YUKINO_SMTP_SENDER",
        "YUKINO_AGENT_ENABLED",
        "YUKINO_AGENT_MAX_STEPS",
        "YUKINO_AGENT_WORKERS",
        "YUKINO_AGENT_QUEUE_CAPACITY",
        "YUKINO_AGENT_SESSION_QUEUE_LIMIT",
        "YUKINO_TASK_WALL_TIME_SECONDS",
        "YUKINO_TASK_MODEL_TOKEN_LIMIT",
        "YUKINO_TASK_MODEL_COST_LIMIT",
        "YUKINO_TASK_TOOL_CALL_LIMIT",
        "YUKINO_TASK_MODEL_CALL_LIMIT",
        "YUKINO_MODEL_MAX_OUTPUT_TOKENS",
        "YUKINO_INPUT_COST_PER_MILLION",
        "YUKINO_OUTPUT_COST_PER_MILLION",
        "YUKINO_COST_CURRENCY",
        "YUKINO_ALLOW_DESKTOP",
        "YUKINO_ALLOWED_WRITE_DIRS",
        "YUKINO_SYSTEM_TASKS",
        "YUKINO_TIMEZONE",
        "YUKINO_VECTOR_BACKEND",
        "YUKINO_MILVUS_URI",
        "YUKINO_MILVUS_TOKEN",
        "YUKINO_MILVUS_DATABASE",
        "YUKINO_MILVUS_COLLECTION_PREFIX",
        "YUKINO_GRAPH_BACKEND",
        "YUKINO_NEO4J_URI",
        "YUKINO_NEO4J_USERNAME",
        "YUKINO_NEO4J_PASSWORD",
        "YUKINO_NEO4J_DATABASE",
    }
)

SECRET_CONFIG_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_EMBEDDING_API_KEY",
        "YUKINO_SMTP_PASSWORD",
        "YUKINO_MCP_HEADERS",
        "YUKINO_MCP_STDIO_ENV",
        "YUKINO_OUTBOUND_WEBHOOK_SECRET",
        "YUKINO_INBOUND_WEBHOOK_SECRET",
        "YUKINO_MILVUS_TOKEN",
        "YUKINO_NEO4J_PASSWORD",
    }
)


# 作用：持久化可在运行期覆盖环境变量的配置项。
# 参数：无。
class RuntimeConfigStore:
    """Persist explicitly user-configured runtime values in a local 0600 file."""

    # 作用：确定运行配置文件位置并创建父目录。
    # 参数 path：目标文件、数据库或状态存储路径。
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # 作用：读取并规范化已保存的运行配置。
    # 参数：无。
    def read(self) -> dict[str, str]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            if not isinstance(raw, dict):
                return {}
            return {
                str(key): str(value)
                for key, value in raw.items()
                if key in RUNTIME_CONFIG_KEYS and isinstance(value, (str, int, float, bool))
            }

    # 作用：把持久化配置注入当前进程环境供后续组件读取。
    # 参数：无。
    def apply_saved(self) -> dict[str, str]:
        values = self.read()
        for key, value in values.items():
            os.environ[key] = value
        return values

    # 作用：校验配置变更，支持更新、清空并原子保存。
    # 参数 values：待规范化、写入或合并的配置值集合。
    # 参数 clear：更新时需要从持久配置中删除的键列表。
    def update(self, values: dict[str, str | None], *, clear: set[str] | None = None) -> dict[str, str]:
        clear = clear or set()
        invalid = (set(values) | clear) - RUNTIME_CONFIG_KEYS
        if invalid:
            raise ValueError(f"unsupported runtime config keys: {', '.join(sorted(invalid))}")
        with self._lock:
            persisted = self.read()
            for key in clear:
                persisted[key] = ""
                os.environ.pop(key, None)
            for key, value in values.items():
                if value is None:
                    continue
                normalized = str(value).strip()
                if normalized:
                    persisted[key] = normalized
                    os.environ[key] = normalized
                else:
                    persisted[key] = ""
                    os.environ.pop(key, None)
            self._write(persisted)
            return persisted

    # 作用：按“已保存配置优先、环境变量其次”读取有效值。
    # 参数 key：网络熔断作用域或运行配置键。
    # 参数 default：配置缺失或解析失败时采用的默认值。
    def effective(self, key: str, default: str = "") -> str:
        if key not in RUNTIME_CONFIG_KEYS:
            raise ValueError(f"unsupported runtime config key: {key}")
        return os.getenv(key, default)

    # 作用：通过临时文件原子写入运行配置。
    # 参数 values：待规范化、写入或合并的配置值集合。
    def _write(self, values: dict[str, str]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


# 作用：掩码展示密钥，仅保留少量首尾字符用于辨认。
# 参数 value：待解析、清洗、转义或递归处理的输入值。
def masked_secret(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:3]}••••{value[-4:]}"


# 作用：把 JSON 字符串解析为对象，失败时返回给定兜底值。
# 参数 raw：尚未规范化的原始配置、JSON 文本或底层连接对象。
# 参数 fallback：原始 JSON 解析失败时返回的兜底对象。
def parse_json_object(raw: str, *, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return dict(fallback or {})
    return value if isinstance(value, dict) else dict(fallback or {})
