"""Application settings owned by AGI Yukino.

Only product-level environment variables are defined here.  Provider-specific
adapters may read their own keys, but domain code never imports a legacy
project's configuration module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


# 作用：集中描述服务启动所需的主机、端口和数据目录配置。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“YukinoSettings”相关的数据结构、异常类型或服务组件。
# 字段：data_dir：该对象中的结构化字段。、host：该对象中的结构化字段。、port：该对象中的结构化字段。、model_provider：该对象中的结构化字段。
class YukinoSettings:
    data_dir: Path
    host: str
    port: int
    model_provider: str

    # 作用：返回数据目录下的 SQLite 主数据库路径。
    # 参数：无。
    @property
    # 作用：执行“database_path”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def database_path(self) -> Path:
        return self.data_dir / "service_club.sqlite3"

    # 作用：返回项目内置贴纸资源目录。
    # 参数：无。
    @property
    # 作用：执行“sticker_dir”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def sticker_dir(self) -> Path:
        return PROJECT_ROOT / "assets" / "stickers"

    # 作用：从环境变量构建经过类型转换的服务配置。
    # 参数：无。
    @classmethod
    # 作用：从环境变量读取配置并构造对应对象。
    def from_env(cls) -> "YukinoSettings":
        raw_data_dir = os.getenv("YUKINO_DATA_DIR", "").strip()
        data_dir = (
            Path(raw_data_dir).expanduser().resolve()
            if raw_data_dir
            else PROJECT_ROOT / "data"
        )
        return cls(
            data_dir=data_dir,
            host=os.getenv("YUKINO_HOST", "127.0.0.1"),
            port=int(os.getenv("YUKINO_PORT", "8082")),
            model_provider=os.getenv("YUKINO_MODEL_PROVIDER", "openai-compatible"),
        )


settings = YukinoSettings.from_env()
