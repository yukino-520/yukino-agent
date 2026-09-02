import os
from typing import Any

from openai import OpenAI


# 作用：可选的 OpenAI 向量化通道；未显式配置模型时保持禁用并允许检索降级。
# 参数：无。
class OpenAIEmbeddingProvider:
    """Optional embedding channel; disabled unless a model is explicitly configured."""

    # 作用：保存客户端、模型和超时配置，并记录最近一次调用错误类型。
    # 参数 client：用于枚举集合的 Milvus 客户端实例。
    # 参数 model：生成或读取向量时使用的嵌入模型标识。
    # 参数 timeout_seconds：调用外部向量服务允许等待的超时秒数。
    def __init__(
        self,
        *,
        client: Any | None,
        model: str,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.client = client
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.last_error = ""

    # 作用：从环境变量构造向量服务，缺少密钥或模型时返回禁用实例。
    # 参数：无。
    @classmethod
    # 作用：从环境变量读取配置并构造对应对象。
    def from_env(cls) -> "OpenAIEmbeddingProvider":
        api_key = os.getenv("OPENAI_EMBEDDING_API_KEY", "") or os.getenv(
            "OPENAI_API_KEY", ""
        )
        model = os.getenv("OPENAI_EMBEDDING_MODEL", "")
        base_url = os.getenv("OPENAI_EMBEDDING_BASE_URL", "") or os.getenv(
            "OPENAI_BASE_URL", ""
        )
        if not api_key or not model:
            return cls(client=None, model=model)
        kwargs: dict[str, str] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return cls(
            client=OpenAI(**kwargs),
            model=model,
            timeout_seconds=float(os.getenv("OPENAI_EMBEDDING_TIMEOUT_SECONDS", "8")),
        )

    # 作用：判断客户端和模型是否都可用。
    # 参数：无。
    @property
    # 作用：执行“enabled”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def enabled(self) -> bool:
        return self.client is not None and bool(self.model)

    # 作用：批量生成向量并校验返回数量，异常时保留错误类型供状态观测。
    # 参数 texts：待批量生成向量的文本列表。
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.enabled:
            raise RuntimeError("embedding_provider_disabled")
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=texts,
                timeout=self.timeout_seconds,
            )
            vectors = [list(item.embedding) for item in response.data]
            if len(vectors) != len(texts):
                raise RuntimeError("embedding_count_mismatch")
            self.last_error = ""
            return vectors
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise

    # 作用：返回向量通道开关、模型名与最近错误，不暴露密钥。
    # 参数：无。
    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "last_error": self.last_error,
        }
