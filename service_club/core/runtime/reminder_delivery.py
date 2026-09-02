"""Server-side reminder delivery through a configured outbound webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from service_club.core.memory import MemoryManager
from service_club.core.runtime.background_jobs import BackgroundJobStore

TRUTHY = {"1", "true", "yes", "on"}


# 作用：表示提醒 webhook 的参数、连接或响应不满足交付要求。
# 参数：无。
class ReminderDeliveryError(RuntimeError):
    pass


# 作用：扫描到期提醒并通过持久作业可靠投递到已配置 webhook。
# 参数：无。
class ReminderWebhookDispatcher:
    job_kind = "reminder.webhook"

    # 作用：绑定提醒存储、后台作业账本和可替换的 HTTP 客户端。
    # 参数 memory：提供提醒领取、续租和确认能力的记忆管理器。
    # 参数 jobs：持久后台作业仓库。
    # 参数 urlopen：实际发送 webhook 的可替换 HTTP 打开函数。
    def __init__(
        self,
        memory: MemoryManager,
        jobs: BackgroundJobStore,
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.memory = memory
        self.jobs = jobs
        self.urlopen = urlopen

    # 作用：检查后台提醒开关及 webhook URL 是否配置有效。
    # 参数：无。
    @staticmethod
    # 作用：执行“configured”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def configured() -> tuple[bool, str]:
        enabled = os.getenv("YUKINO_BACKGROUND_REMINDER_DELIVERY", "false").lower() in TRUTHY
        url = os.getenv("YUKINO_OUTBOUND_WEBHOOK", "").strip()
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        return enabled and valid, url if valid else ""

    # 作用：租约领取到期提醒并以提醒 ID 和时间作为去重键创建投递作业。
    # 参数：无。
    def scan_due(self) -> int:
        enabled, _ = self.configured()
        if not enabled:
            return 0
        reminders = self.memory.claim_due_reminders_any(limit=20, lease_seconds=600)
        created = 0
        for reminder in reminders:
            reminder_id = int(reminder["id"])
            due_at = float(reminder.get("due_at") or 0)
            job = self.jobs.enqueue(
                self.job_kind,
                {
                    "reminder_id": reminder_id,
                    "session_id": str(reminder["session_id"]),
                    "content": str(reminder["content"]),
                    "due_at": due_at,
                    "timezone": str(reminder.get("timezone") or ""),
                    "recurrence": str(reminder.get("recurrence") or ""),
                    "claim_token": str(reminder["claim_token"]),
                },
                session_id=str(reminder["session_id"]),
                dedupe_key=f"{reminder_id}:{due_at:.6f}",
                max_attempts=5,
            )
            if job.get("deduplicated") and job.get("status") in {
                "completed",
                "cancelled",
                "dead_letter",
            }:
                self.memory.release_reminder_claim(
                    str(reminder["session_id"]),
                    reminder_id,
                    str(reminder["claim_token"]),
                )
                continue
            created += int(not job.get("deduplicated"))
        return created

    # 作用：续租提醒领取权、签名发送 webhook，并在成功后确认提醒已交付。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    def deliver(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload")
        if not isinstance(payload, dict):
            raise ReminderDeliveryError("提醒派送参数损坏。")
        session_id = str(payload.get("session_id") or "")
        reminder_id = int(payload.get("reminder_id") or 0)
        claim_token = str(payload.get("claim_token") or "")
        enabled, webhook = self.configured()
        if not enabled:
            self.memory.release_reminder_claim(session_id, reminder_id, claim_token)
            return {"delivered": False, "reason": "background_delivery_disabled"}
        if not self.memory.renew_reminder_claim(
            session_id,
            reminder_id,
            claim_token,
            lease_seconds=600,
        ):
            return {"delivered": False, "reason": "reminder_claim_no_longer_owned"}

        event = {
            "event": "reminder.due",
            "event_id": str(job["id"]),
            "reminder": {
                "id": reminder_id,
                "session_id": session_id,
                "content": str(payload.get("content") or ""),
                "due_at": payload.get("due_at"),
                "timezone": str(payload.get("timezone") or ""),
                "recurrence": str(payload.get("recurrence") or ""),
            },
        }
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AGI-Yukino/1.0",
            "X-AGI-Yukino-Event": "reminder.due",
            "X-AGI-Yukino-Event-Id": str(job["id"]),
        }
        secret = os.getenv("YUKINO_OUTBOUND_WEBHOOK_SECRET", "")
        if secret:
            signature = hmac.new(
                secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-AGI-Yukino-Signature"] = f"sha256={signature}"
        request = urllib.request.Request(
            webhook,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with self.urlopen(request, timeout=12) as response:
                status = int(getattr(response, "status", 200))
                response.read(4096)
        except urllib.error.HTTPError as exc:
            raise ReminderDeliveryError(f"Webhook 返回 HTTP {exc.code}。") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            label = type(reason).__name__ if reason is not None else type(exc).__name__
            raise ReminderDeliveryError(f"Webhook 连接失败（{label}）。") from exc
        if not 200 <= status < 300:
            raise ReminderDeliveryError(f"Webhook 返回 HTTP {status}。")

        acknowledged = self.memory.acknowledge_reminder(
            session_id,
            reminder_id,
            claim_token,
        )
        return {
            "delivered": True,
            "acknowledged": acknowledged,
            "http_status": status,
            "event_id": str(job["id"]),
        }

    # 作用：作业进入死信时释放提醒领取权，避免提醒永久卡在租约中。
    # 参数 job：当前需要校验、执行或展示的后台作业记录。
    def release_dead_letter(self, job: dict[str, Any]) -> None:
        payload = job.get("payload")
        if not isinstance(payload, dict):
            return
        self.memory.release_reminder_claim(
            str(payload.get("session_id") or ""),
            int(payload.get("reminder_id") or 0),
            str(payload.get("claim_token") or ""),
        )

    # 作用：汇总 webhook 配置以及提醒作业的待处理和死信数量。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        enabled, url = self.configured()
        job_status = self.jobs.status()
        return {
            "ok": job_status["dead_letters"] == 0,
            "enabled": enabled,
            "webhook_configured": bool(url),
            "pending": job_status["pending"],
            "dead_letters": job_status["dead_letters"],
        }
