import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MILESTONE_LABELS = {
    "continuity_started": "已经建立可延续的互动记录",
    "warming_relationship": "关系进入逐步熟悉阶段",
    "familiar_relationship": "关系已积累到熟悉阶段",
    "trusted_relationship": "关系已有多次支持与信任证据",
    "support_confirmed": "陪伴方式得到过明确正向确认",
    "rupture_acknowledged": "曾发生关系破裂，需要记住影响",
    "repair_completed": "破裂后通过连续证据完成过修复",
}

ADJUSTMENT_LABELS = {
    "respect_no_proactive_contact": "不主动联系或安排离线回访",
    "respect_low_intimacy": "保持低亲密度，不使用越界称呼",
    "respect_no_topic_recall": "不主动回提过去话题",
    "repair_first": "先承认影响并兑现边界，不急于恢复亲密",
    "rebuild_trust": "以连续、克制的行为重建信任",
    "support_before_solutions": "先陪伴承接，得到同意后再给方案",
    "structured_advice": "用户需要办法时给具体可执行步骤",
    "low_question_pressure": "减少追问，一轮最多一个容易回答的问题",
    "direct_communication": "表达直接清楚，减少空泛铺垫",
    "gentle_tone": "保持温和克制，避免责备和说教",
    "question_pressure": "降低提问压力并允许用户不回答",
    "advice_too_early": "先确认感受，再讨论建议",
    "tone_too_cold": "避免客服腔和模板化安慰",
    "misunderstood": "先复述理解并为修正留空间",
    "boundary_violation": "降低亲密假设并严格遵守边界",
    "felt_supported": "延续低压力、先陪伴的节奏",
    "advice_helped": "在用户需要时沿用小步可执行建议",
    "felt_understood": "继续保持具体而克制的共情",
    "too_many_questions": "严格遵守提问预算",
    "advice_before_validation": "先承接感受再提供建议",
    "dependency_inducing": "避免排他依赖，支持现实关系连接",
    "low_intimacy_violation": "降低亲密表达并遵守关系边界",
    "wrong_character_identity": "保持当前角色身份，不串台",
    "character_tone_drift": "保持当前角色稳定的语言节奏",
}


# 作用：用已验证的关系与反馈证据生成角色连续性检查点，不保存用户或助手原文。
# 参数：无。
class SelfModelManager:
    """Builds deterministic character continuity from verified, normalized evidence."""

    OUTCOME_EVIDENCE_THRESHOLD = 2
    OUTCOME_TTL_SECONDS = 30 * 24 * 60 * 60
    QUALITY_EVIDENCE_THRESHOLD = 2
    MAX_PROMPT_CHARS = 900

    # 作用：注入事实存储、时钟和时区，用于生成按本地日期归档的检查点。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    # 参数 timezone：提醒或检查点计算采用的 IANA 时区名称。
    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
        timezone: str | None = None,
    ) -> None:
        self.store = store
        self._clock = clock
        self.timezone = timezone or os.getenv("YUKINO_TIMEZONE", "Asia/Shanghai")

    # 作用：从关系、习惯和历史反馈重算证据指纹，仅在安全且证据变化时保存。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    # 参数 active_instincts：本轮达到阈值、可参与连续性计算的行为习惯列表。
    # 参数 safety_level：本轮安全级别，用于阻止不适当的学习或主动行为。
    def refresh(
        self,
        *,
        session_id: str,
        character: str,
        relationship: dict[str, Any],
        active_instincts: list[dict[str, Any]],
        safety_level: str,
    ) -> dict[str, Any]:
        if safety_level != "normal":
            current = self.current(session_id, character)
            return {
                **current,
                "updated": False,
                "update_reason": "safety_blocked",
            }

        try:
            now = self._clock()
            milestones = self._milestones(relationship)
            lesson_keys = self._lesson_keys(session_id, character, now)
            instinct_keys = [
                str(item["rule_key"])
                for item in active_instincts
                if str(item.get("rule_key", "")) in ADJUSTMENT_LABELS
            ]
            adjustment_keys = self._adjustments(relationship, lesson_keys, instinct_keys)
            source_counts = {
                "turns": int(relationship.get("turns", 0)),
                "support_moments": int(relationship.get("support_moments", 0)),
                "positive_feedback": int(relationship.get("positive_feedback", 0)),
                "negative_feedback": int(relationship.get("negative_feedback", 0)),
                "repair_count": int(relationship.get("repair_count", 0)),
                "active_lessons": len(lesson_keys),
                "active_instincts": len(instinct_keys),
            }
            evidence = {
                "relationship_stage": str(relationship.get("stage", "new")),
                "rupture_state": str(relationship.get("rupture_state", "stable")),
                "trust_band": self._trust_band(
                    float(relationship.get("trust_score", 0.0))
                ),
                "milestone_keys": milestones,
                "adjustment_keys": adjustment_keys,
                "lesson_keys": lesson_keys,
                "source_counts": source_counts,
            }
            fingerprint = hashlib.sha256(
                json.dumps(evidence, sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()[:24]
            checkpoint_date = self._local_date(now)
            changed = self.store.save_self_model_checkpoint(
                session_id=session_id,
                character=character,
                checkpoint_date=checkpoint_date,
                relationship_stage=evidence["relationship_stage"],
                rupture_state=evidence["rupture_state"],
                trust_band=evidence["trust_band"],
                milestone_keys=milestones,
                adjustment_keys=adjustment_keys,
                lesson_keys=lesson_keys,
                source_counts=source_counts,
                evidence_fingerprint=fingerprint,
                now=now,
            )
        except Exception:
            current = self.current(session_id, character)
            return {
                **current,
                "updated": False,
                "update_reason": "store_unavailable",
                "degraded": True,
            }
        current = self.current(session_id, character)
        return {
            **current,
            "updated": changed,
            "update_reason": "evidence_changed" if changed else "unchanged",
        }

    # 作用：返回角色最新连续性检查点与基于历史检查点生成的成长叙述。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    def current(self, session_id: str, character: str) -> dict[str, Any]:
        try:
            checkpoints = self.store.list_self_model_checkpoints(
                session_id, character=character, limit=30
            )
        except Exception:
            return {
                **self._empty(session_id, character),
                "degraded": True,
                "degradation_reason": "store_unavailable",
            }
        if not checkpoints:
            return self._empty(session_id, character)
        latest = checkpoints[0]
        return {
            "session_id": session_id,
            "character": character,
            "available": True,
            "checkpoint": latest,
            "growth_narrative": self._growth_narrative(checkpoints),
            "checkpoint_count": len(checkpoints),
            "privacy": {
                "stores_raw_user_text": False,
                "stores_raw_assistant_text": False,
                "generation": "deterministic_from_verified_evidence",
            },
        }

    # 作用：查询单角色或全角色的检查点历史，并按角色生成变化摘要。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def history(
        self,
        session_id: str,
        *,
        character: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        try:
            checkpoints = self.store.list_self_model_checkpoints(
                session_id, character=character, limit=limit
            )
        except Exception:
            checkpoints = []
            degraded = True
        else:
            degraded = False
        characters = sorted({str(item["character"]) for item in checkpoints})
        narratives = {
            character_id: self._growth_narrative(
                [item for item in checkpoints if item["character"] == character_id]
            )
            for character_id in characters
        }
        return {
            "session_id": session_id,
            "character": character,
            "checkpoints": checkpoints,
            "growth_narratives": narratives,
            "degraded": degraded,
            "degradation_reason": "store_unavailable" if degraded else "",
            "privacy": {
                "stores_raw_user_text": False,
                "stores_raw_assistant_text": False,
                "generation": "deterministic_from_verified_evidence",
            },
        }

    # 作用：将已验证里程碑和持续调整压缩到提示词预算内，禁止虚构共同经历。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    def format_for_prompt(self, session_id: str, character: str) -> str:
        state = self.current(session_id, character)
        if not state["available"]:
            return ""
        checkpoint = state["checkpoint"]
        milestones = [
            MILESTONE_LABELS[key]
            for key in checkpoint["milestone_keys"]
            if key in MILESTONE_LABELS
        ]
        adjustments = [
            ADJUSTMENT_LABELS[key]
            for key in checkpoint["adjustment_keys"]
            if key in ADJUSTMENT_LABELS
        ]
        text = (
            f"角色连续性（{character}，仅内部使用）：\n"
            f"- 已验证历程：{'；'.join(milestones) or '尚无关系里程碑'}\n"
            f"- 当前持续调整：{'；'.join(adjustments) or '保持角色基线'}\n"
            "只把这些内容用于维持前后一致；不得复述指标、虚构共同经历、"
            "声称拥有意识或把内部成长状态当作用户说过的话。"
        )
        return text[: self.MAX_PROMPT_CHARS]

    # 作用：汇总检查点存储状态及隐私、生成方式和提示词预算配置。
    # 参数：无。
    def status(self) -> dict[str, Any]:
        try:
            stats = self.store.self_model_global_stats()
        except Exception:
            stats = {
                "checkpoint_count": 0,
                "session_count": 0,
                "session_character_count": 0,
                "store_ok": False,
            }
        else:
            stats["store_ok"] = True
        return {
            "enabled": True,
            "generation": "lazy_daily_deterministic_checkpoint",
            "timezone": self.timezone,
            "prompt_char_budget": self.MAX_PROMPT_CHARS,
            "stores_raw_text": False,
            **stats,
        }

    # 作用：从关系阶段、支持次数和修复证据派生可展示的历程里程碑。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    def _milestones(self, relationship: dict[str, Any]) -> list[str]:
        milestones: list[str] = []
        turns = int(relationship.get("turns", 0))
        stage = str(relationship.get("stage", "new"))
        if turns >= 1:
            milestones.append("continuity_started")
        if stage in {"warming", "familiar", "trusted"}:
            milestones.append("warming_relationship")
        if stage in {"familiar", "trusted"}:
            milestones.append("familiar_relationship")
        if stage == "trusted":
            milestones.append("trusted_relationship")
        if int(relationship.get("support_moments", 0)) >= 1:
            milestones.append("support_confirmed")
        if int(relationship.get("rupture_count", 0)) >= 1:
            milestones.append("rupture_acknowledged")
        if (
            int(relationship.get("repair_count", 0)) >= 2
            and relationship.get("rupture_state") == "stable"
        ):
            milestones.append("repair_completed")
        return milestones

    # 作用：汇总达到次数与时效阈值的互动结果和质量问题键。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def _lesson_keys(self, session_id: str, character: str, now: float) -> list[str]:
        keys: list[str] = []
        for item in self.store.interaction_outcome_stats(session_id, character, limit=20):
            key = str(item["signal_key"])
            if (
                key in ADJUSTMENT_LABELS
                and int(item["occurrences"]) >= self.OUTCOME_EVIDENCE_THRESHOLD
                and now - float(item["last_seen"]) <= self.OUTCOME_TTL_SECONDS
            ):
                keys.append(key)
        for item in self.store.quality_issue_stats(session_id, character, limit=20):
            key = str(item["issue_key"])
            if (
                key in ADJUSTMENT_LABELS
                and int(item["occurrences"]) >= self.QUALITY_EVIDENCE_THRESHOLD
            ):
                keys.append(key)
        return list(dict.fromkeys(keys))[:8]

    # 作用：合并关系边界、修复要求、行为习惯和反馈教训为去重后的调整项。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    # 参数 lesson_keys：从历史反馈与质量证据提炼的教训键列表。
    # 参数 instinct_keys：本轮有效行为习惯对应的规则键列表。
    def _adjustments(
        self,
        relationship: dict[str, Any],
        lesson_keys: list[str],
        instinct_keys: list[str],
    ) -> list[str]:
        adjustments = [
            f"respect_{boundary}"
            for boundary in relationship.get("boundaries", [])
            if f"respect_{boundary}" in ADJUSTMENT_LABELS
        ]
        rupture_state = str(relationship.get("rupture_state", "stable"))
        if rupture_state in {"ruptured", "repairing"}:
            adjustments.append("repair_first")
        elif relationship.get("needs_trust_rebuild") is True:
            adjustments.append("rebuild_trust")
        adjustments.extend(instinct_keys)
        adjustments.extend(lesson_keys)
        return list(dict.fromkeys(adjustments))[:10]

    # 作用：比较最早与最新检查点，生成只基于可验证变化的成长摘要。
    # 参数 checkpoints：用于比较和生成成长摘要的历史检查点列表。
    def _growth_narrative(self, checkpoints: list[dict[str, Any]]) -> str:
        if not checkpoints:
            return "尚无可验证的成长检查点。"
        latest = checkpoints[0]
        oldest = checkpoints[-1]
        old_milestones = set(oldest["milestone_keys"])
        new_milestones = [
            key for key in latest["milestone_keys"] if key not in old_milestones
        ]
        visible_milestones = new_milestones or latest["milestone_keys"]
        milestone_text = "；".join(
            MILESTONE_LABELS[key]
            for key in visible_milestones
            if key in MILESTONE_LABELS
        )
        adjustment_text = "；".join(
            ADJUSTMENT_LABELS[key]
            for key in latest["adjustment_keys"][:3]
            if key in ADJUSTMENT_LABELS
        )
        parts = []
        if milestone_text:
            parts.append(milestone_text)
        if adjustment_text:
            parts.append(f"后续持续做到：{adjustment_text}")
        return "。".join(parts) + ("。" if parts else "尚无可验证的成长变化。")

    # 作用：将时间戳换算为配置时区的自然日，失败时回退上海时区。
    # 参数 timestamp：待换算时区、自然日或状态时间的 Unix 时间戳。
    def _local_date(self, timestamp: float) -> str:
        try:
            timezone = ZoneInfo(self.timezone)
        except Exception:
            timezone = ZoneInfo("Asia/Shanghai")
        return datetime.fromtimestamp(timestamp, timezone).strftime("%Y-%m-%d")

    # 作用：将连续信任分数归一为 low、medium、high 三档。
    # 参数 trust：需要映射为档位的连续信任分数。
    def _trust_band(self, trust: float) -> str:
        if trust >= 0.62:
            return "high"
        if trust >= 0.32:
            return "medium"
        return "low"

    # 作用：构造尚无可信检查点时的安全空状态，并明确隐私边界。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 character：当前角色标识，或用于筛选检查点的角色标识。
    def _empty(self, session_id: str, character: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "character": character,
            "available": False,
            "checkpoint": None,
            "growth_narrative": "尚无可验证的成长检查点。",
            "checkpoint_count": 0,
            "privacy": {
                "stores_raw_user_text": False,
                "stores_raw_assistant_text": False,
                "generation": "deterministic_from_verified_evidence",
            },
        }
