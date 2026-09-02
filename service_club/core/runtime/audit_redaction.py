from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "headers",
    "password",
    "refresh_token",
    "smtp_password",
    "access_token",
}
BULK_TEXT_KEYS = {
    "attachment",
    "base64",
    "blob",
    "body",
    "code",
    "content",
    "image",
    "new_text",
    "old_text",
    "prompt",
}
SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^&\s]+)"),
)


# 作用：递归清洗审计载荷中的密钥和大段内容，同时保留可排障的结构信息。
# 参数 value：可能包含密钥或大段正文的任意审计值。
# 参数 key：当前值所属的字段名，用于判断是否敏感。
# 参数 depth：当前递归清洗层级，用于限制审计载荷深度。
def redact_for_audit(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Keep task diagnostics useful without duplicating secrets or large payloads."""
    normalized_key = key.strip().lower().replace("-", "_")
    if _sensitive_key(normalized_key):
        return "[REDACTED]"
    if depth > 8:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_for_audit(item, key=str(item_key), depth=depth + 1)
            for item_key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [redact_for_audit(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    if isinstance(value, str):
        if normalized_key in BULK_TEXT_KEYS:
            return f"[{len(value)} characters]"
        masked = value
        for pattern in SECRET_PATTERNS:
            if pattern.pattern.startswith("(?i)"):
                masked = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", masked)
            else:
                masked = pattern.sub("[REDACTED]", masked)
        if len(masked) > 2000:
            return f"{masked[:500]}… [{len(masked)} characters]"
        return masked
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[{type(value).__name__}]"


# 作用：判断字段名是否可能承载密码、令牌或认证信息。
# 参数 key：当前值所属的字段名，用于判断是否敏感。
def _sensitive_key(key: str) -> bool:
    return (
        key in SENSITIVE_KEYS
        or key.endswith(("_password", "_secret", "_token"))
        or key.startswith("authorization_")
    )
