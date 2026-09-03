"""Authentication and durable replay protection for inbound channel webhooks."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from service_club.storage.relational import RelationalBackend, configured_relational_backend


# 作用：携带 HTTP 状态码的入站通道认证或防重放异常。
# 参数：无。
class ChannelSecurityError(RuntimeError):
    # 作用：保存面向接口返回的错误信息和状态码。
    # 参数 message：异常向调用方说明的可读错误信息。
    # 参数 status_code：该安全异常应返回的 HTTP 状态码。
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


# 作用：校验 webhook 签名、时间窗和事件 ID，并持久化阻止跨重启重放。
# 参数：无。
class InboundChannelSecurity:
    """Verify signed requests and remember accepted event IDs across restarts."""

    timestamp_header = "X-AGI-Yukino-Timestamp"
    event_header = "X-AGI-Yukino-Event-Id"
    signature_header = "X-AGI-Yukino-Signature"

    # 作用：配置签名时钟与有效时间窗，并初始化防重放账本。
    # 参数 db_path：旧版路径参数，运行时不使用。
    # 参数 backend：可选的关系型存储后端；未提供时读取 PostgreSQL 配置。
    # 参数 clock：可替换的时钟函数，便于控制过期和熔断时间。
    # 参数 max_age_seconds：入站签名允许偏离当前时间的最大秒数。
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: RelationalBackend | None = None,
        clock: Callable[[], float] = time.time,
        max_age_seconds: int = 300,
    ) -> None:
        if backend is None:
            backend = configured_relational_backend()
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self.clock = clock
        self.max_age_seconds = max(30, min(int(max_age_seconds), 900))
        self.init()

    # 作用：创建已接收入站事件表和按接收时间清理所需的索引。
    # 参数：无。
    def init(self) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS inbound_channel_events (
                    channel TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    received_at {self.backend.float_type} NOT NULL,
                    PRIMARY KEY(channel, event_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_channel_events_received
                ON inbound_channel_events(received_at)
                """
            )

    # 作用：校验 HMAC、时间戳和事件格式，通过后原子登记事件 ID。
    # 参数 channel：入站消息所属的外部通道标识。
    # 参数 body：参与签名校验的原始 HTTP 请求体。
    # 参数 timestamp：参与签名且表示消息产生时间的字符串时间戳。
    # 参数 event_id：外部通道提供的唯一事件标识，用于防重放。
    # 参数 signature：调用方提交的 HMAC-SHA256 签名。
    def verify(
        self,
        *,
        channel: str,
        body: bytes,
        timestamp: str,
        event_id: str,
        signature: str,
    ) -> str:
        secret = os.getenv("YUKINO_INBOUND_WEBHOOK_SECRET", "")
        if not secret:
            raise ChannelSecurityError(
                "外部消息通道尚未配置入站签名密钥。",
                status_code=503,
            )
        if not re.fullmatch(r"\d{10,13}", timestamp):
            raise ChannelSecurityError("入站消息时间戳无效。")
        timestamp_value = int(timestamp)
        if len(timestamp) == 13:
            timestamp_value /= 1000
        now = float(self.clock())
        if abs(now - timestamp_value) > self.max_age_seconds:
            raise ChannelSecurityError("入站消息已超出 5 分钟签名时间窗。")
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", event_id):
            raise ChannelSecurityError("入站消息 event_id 无效。")
        if not re.fullmatch(r"sha256=[a-fA-F0-9]{64}", signature):
            raise ChannelSecurityError("入站消息签名格式无效。")
        expected = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature.lower(), f"sha256={expected}"):
            raise ChannelSecurityError("入站消息签名校验失败。")
        self._remember(channel, event_id, now)
        return event_id

    # 作用：清理过期记录并登记新事件；重复 ID 以冲突状态拒绝执行。
    # 参数 channel：入站消息所属的外部通道标识。
    # 参数 event_id：外部通道提供的唯一事件标识，用于防重放。
    # 参数 now：可选的当前时间覆盖值，便于原子操作和恢复测试。
    def _remember(self, channel: str, event_id: str, now: float) -> None:
        with self.backend.connect(immediate=True) as conn:
            conn.execute(
                "DELETE FROM inbound_channel_events WHERE received_at < ?",
                (now - 86_400,),
            )
            inserted = conn.execute(
                """
                INSERT INTO inbound_channel_events(channel, event_id, received_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel, event_id) DO NOTHING
                """,
                (channel, event_id, now),
            )
        if not inserted.rowcount:
            raise ChannelSecurityError(
                "入站消息 event_id 已处理，拒绝重复执行。",
                status_code=409,
            )

    # 作用：返回签名配置、时间窗、已登记事件数及存储后端信息。
    # 参数：无。
    def status(self) -> dict[str, object]:
        with self.backend.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM inbound_channel_events"
            ).fetchone()
            accepted = int(row["count"] if row else 0)
        return {
            "configured": bool(os.getenv("YUKINO_INBOUND_WEBHOOK_SECRET", "")),
            "max_age_seconds": self.max_age_seconds,
            "accepted_events": accepted,
            "signature": "HMAC-SHA256(timestamp + '.' + raw_body)",
            "backend": self.backend.name,
        }
