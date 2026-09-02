import os
from hashlib import sha256
from pathlib import Path
from typing import Callable

from openai import OpenAI

from service_club.core.conversation.characters import CharacterProfile
from service_club.core.types import EmotionLabel
from service_club.settings import settings


# 作用：清理模型回复首尾空白，形成可展示文本。
# 参数 text：待识别、切分、清理或合成语音的输入文本。
def clean_reply(text: str) -> str:
    return text.strip()


# 作用：按角色和情绪选择第一张可用表情，并在缺失时回退到中性表情。
# 参数 base_dir：角色贴纸资源所在的基础目录。
# 参数 character：本轮角色配置，用于贴纸、语音或系统提示。
# 参数 emotion：本轮结构化情绪结果或用于媒体选择的情绪标签。
def pick_sticker(
    base_dir: str | Path,
    character: CharacterProfile,
    emotion: EmotionLabel,
) -> str | None:
    base = Path(base_dir)
    for label in (emotion, "neutral"):
        directory = base / character.sticker_pack / label
        if directory.is_dir():
            files = sorted(path for path in directory.iterdir() if path.is_file())
            if files:
                return f"/assets/stickers/{character.sticker_pack}/{label}/{files[0].name}"
    return None


# 作用：在启用语音时调用 TTS Provider，并用内容摘要复用本地音频缓存。
# 参数 text：待识别、切分、清理或合成语音的输入文本。
# 参数 character：本轮角色配置，用于贴纸、语音或系统提示。
# 参数 emotion：本轮结构化情绪结果或用于媒体选择的情绪标签。
# 参数 force：是否忽略常规 TTS 开关而强制尝试合成。
# 参数 idempotency_key：用于复用相同语音生成结果的幂等键。
# 参数 on_provider_call_start：外部 TTS 请求真正开始前调用的审计回调。
def synthesize_tts_optional(
    text: str,
    character: CharacterProfile,
    emotion: EmotionLabel,
    *,
    force: bool = False,
    idempotency_key: str = "",
    on_provider_call_start: Callable[[], None] | None = None,
) -> str | None:
    if not force and os.environ.get("TTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not text.strip():
        return None
    voice = os.getenv(f"YUKINO_TTS_VOICE_{character.id.upper()}", "alloy")
    model = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    digest = sha256(f"{model}\0{voice}\0{emotion}\0{text}".encode()).hexdigest()[:24]
    cache_dir = settings.data_dir / "media" / "tts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{digest}.mp3"
    if target.exists() and target.stat().st_size:
        return f"/media/tts/{target.name}"
    client_options: dict[str, str] = {"api_key": api_key}
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        client_options["base_url"] = base_url
    try:
        request: dict[str, object] = {
            "model": model,
            "voice": voice,
            "input": text[:4000],
            "response_format": "mp3",
            "timeout": 20,
        }
        if idempotency_key:
            request["extra_headers"] = {
                "Idempotency-Key": idempotency_key[:200]
            }
        if on_provider_call_start is not None:
            on_provider_call_start()
        response = OpenAI(**client_options).audio.speech.create(**request)
        if hasattr(response, "write_to_file"):
            response.write_to_file(target)
        else:
            content = getattr(response, "content", b"")
            if not content:
                return None
            target.write_bytes(content)
    except Exception:
        target.unlink(missing_ok=True)
        return None
    return f"/media/tts/{target.name}" if target.exists() else None
