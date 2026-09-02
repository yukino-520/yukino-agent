import hashlib
import time
from collections.abc import Callable
from typing import Any

POSITIVE_SIGNALS = (
    "谢谢你还在",
    "谢谢你陪我",
    "这样好多了",
    "就是这种感觉",
    "你这样说我舒服",
    "你帮到我了",
)
NEGATIVE_SIGNALS = (
    "别这样说",
    "不是这种感觉",
    "这样让我不舒服",
    "你没懂我",
    "别再这样",
    "你让我更难受",
    "不要自作主张",
)
REPAIR_SIGNALS = (
    "没关系",
    "我接受你的道歉",
    "这次好多了",
    "这样就好",
    "我们继续吧",
)
BOUNDARY_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "no_proactive_contact",
        ("别主动联系我", "不要主动联系", "别主动发消息", "不要主动发消息"),
    ),
    (
        "low_intimacy",
        ("别这么亲密", "不要这么亲密", "别叫我宝贝", "不要叫我宝贝", "保持距离"),
    ),
    (
        "no_topic_recall",
        ("别再提这件事", "不要再提这件事", "别提以前", "不要提过去"),
    ),
)
BOUNDARY_RELEASE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "no_proactive_contact",
        ("你可以主动联系我", "可以主动发消息", "可以来问我"),
    ),
    (
        "low_intimacy",
        ("不用保持距离了", "可以亲近一点"),
    ),
    (
        "no_topic_recall",
        ("可以提以前的事", "可以聊过去了"),
    ),
)


# 作用：以可追溯互动证据维护信任阶段、明确边界和破裂修复状态。
# 参数：无。
class RelationshipTracker:
    """Evidence-based relationship state with rupture and gradual repair."""

    # 作用：注入关系事实存储和时钟，用于持续计算跨轮关系状态。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(self, store: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._clock = clock

    # 作用：从当前原话记录反馈、边界或修复事件，并更新持久关系事实。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def observe(self, session_id: str, text: str) -> dict[str, object]:
        now = self._clock()
        state = self._load(session_id, now)
        previous_at = state.get("last_interaction_at")
        state["last_gap_seconds"] = (
            max(0, int(now - float(previous_at))) if previous_at else 0
        )
        state["turns"] += 1
        state["last_interaction_at"] = now
        state["updated_at"] = now

        signals: list[str] = []
        boundaries = self._boundaries(text)
        for boundary in boundaries:
            if boundary not in state["boundaries"]:
                state["boundaries"].append(boundary)
                signals.append(f"boundary:{boundary}")
        released_boundaries = self._released_boundaries(text)
        for boundary in released_boundaries:
            if boundary in state["boundaries"]:
                state["boundaries"].remove(boundary)
                signals.append(f"boundary_released:{boundary}")

        if any(phrase in text for phrase in NEGATIVE_SIGNALS):
            self._apply_event(
                state, text, "negative_feedback", "explicit_negative", -0.18, -0.25, now
            )
            state["rupture_state"] = "ruptured"
            state["repair_progress"] = 0
            signals.append("negative_feedback")
        elif state["rupture_state"] in {"ruptured", "repairing"} and any(
            phrase in text for phrase in REPAIR_SIGNALS
        ):
            if self._apply_event(
                state, text, "repair", "repair_acceptance", 0.04, 0.08, now
            ):
                state["repair_progress"] += 1
                state["repair_count"] += 1
                state["rupture_state"] = (
                    "stable" if state["repair_progress"] >= 2 else "repairing"
                )
            signals.append("repair")
        elif any(phrase in text for phrase in POSITIVE_SIGNALS):
            if self._apply_event(
                state, text, "positive_feedback", "support_confirmed", 0.09, 0.15, now
            ):
                state["support_moments"] += 1
            signals.append("positive_feedback")
        else:
            self._apply_event(state, text, "interaction", "ordinary_turn", 0.015, 0.02, now)

        if boundaries:
            self._apply_event(
                state,
                text,
                "boundary",
                "+".join(boundaries),
                0.0,
                0.0,
                now,
            )
        if released_boundaries:
            self._apply_event(
                state,
                text,
                "boundary_released",
                "+".join(released_boundaries),
                0.0,
                0.03,
                now,
            )
        self._clamp(state)
        self.store.save_relationship_state(state)
        result = self._public(state)
        result["observed_signals"] = signals
        return result

    # 作用：读取当前关系状态，并结合短期情绪给出本轮互动指导。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 mental_state：当前短期情绪趋势和长期情绪惯性的汇总状态。
    def status(
        self, *, session_id: str, mental_state: dict[str, Any] | None = None
    ) -> dict[str, object]:
        state = self._load(session_id, self._clock())
        result = self._public(state)
        result["guidance"] = self._guidance(result, mental_state or {})
        return result

    # 作用：将关系阶段、信任、破裂状态和边界压缩为模型提示词。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    def format_for_prompt(self, relationship: dict[str, object]) -> str:
        return (
            f"关系阶段：{relationship['stage']}，"
            f"信任={relationship['trust_score']}，状态={relationship['rupture_state']}，"
            f"明确边界={','.join(relationship['boundaries']) or '无'}；"
            f"陪伴提示：{relationship['guidance']}"
        )

    # 作用：根据轮数、信任、支持证据和破裂状态计算关系阶段。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    def _stage(self, state: dict[str, Any]) -> str:
        turns = int(state["turns"])
        trust = float(state["trust_score"])
        support = int(state["support_moments"])
        quality = float(state["quality_score"])
        rupture_state = str(state["rupture_state"])
        if rupture_state == "ruptured":
            return "new"
        if rupture_state == "repairing":
            return "warming"
        if turns >= 8 and trust >= 0.62 and support >= 2 and quality >= 0.35:
            return "trusted"
        if turns >= 4 and trust >= 0.32 and (support >= 1 or quality >= 0.16):
            return "familiar"
        if turns >= 2 and trust >= 0.22:
            return "warming"
        return "new"

    # 作用：将关系阶段和情绪风险映射为有边界的陪伴行为建议。
    # 参数 relationship：当前已验证的关系阶段、信任和边界状态。
    # 参数 mental_state：当前短期情绪趋势和长期情绪惯性的汇总状态。
    def _guidance(
        self, relationship: dict[str, object], mental_state: dict[str, Any]
    ) -> str:
        stage = str(relationship["stage"])
        rupture_state = str(relationship["rupture_state"])
        if rupture_state == "ruptured":
            return "先承认具体影响并简短道歉，不辩解、不索取原谅；询问用户希望如何调整。"
        if rupture_state == "repairing":
            return (
                "用户愿意继续，但信任仍在修复；严格兑现边界，"
                "用连续行为恢复，不立刻恢复亲密表达。"
            )
        if relationship.get("needs_trust_rebuild") is True:
            return (
                "修复流程已经完成，但信任仍低；保持克制并持续兑现边界，"
                "暂不恢复主动联系和熟人式亲密表达。"
            )
        if mental_state.get("needs_gentle_followup") is True:
            return "用户已经连续低落，少问责，多确认陪伴和下一小步。"
        if stage == "trusted":
            return "可以更自然地引用过往偏好，但不要假装知道未记录的信息。"
        if stage == "familiar":
            return "可以稍微延续前文，语气更像熟人，但仍保持边界。"
        if stage == "warming":
            return "开始建立连续感，轻轻承接上一轮状态。"
        return "先建立安全感，少做过度亲密假设。"

    # 作用：从事实存储加载关系状态，首次会话则建立保守初始值。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def _load(self, session_id: str, now: float) -> dict[str, Any]:
        state = self.store.get_relationship_state(session_id)
        if state is not None:
            return state
        return {
            "session_id": session_id,
            "turns": 0,
            "trust_score": 0.2,
            "quality_score": 0.0,
            "positive_feedback": 0,
            "negative_feedback": 0,
            "support_moments": 0,
            "rupture_count": 0,
            "repair_count": 0,
            "repair_progress": 0,
            "rupture_state": "stable",
            "boundaries": [],
            "last_gap_seconds": 0,
            "last_interaction_at": None,
            "updated_at": now,
        }

    # 作用：以证据哈希去重写入关系事件，并只对新事件应用分数变化。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 event_type：关系事件的业务类型。
    # 参数 signal_key：可聚合的反馈、关系或互动信号键。
    # 参数 trust_delta：关系事件对信任分数的增量。
    # 参数 quality_delta：关系事件对互动质量分数的增量。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def _apply_event(
        self,
        state: dict[str, Any],
        text: str,
        event_type: str,
        signal_key: str,
        trust_delta: float,
        quality_delta: float,
        now: float,
    ) -> bool:
        evidence_hash = hashlib.sha256(
            f"{event_type}:{signal_key}:{text.strip()}".encode()
        ).hexdigest()[:24]
        inserted = self.store.record_relationship_event(
            session_id=state["session_id"],
            event_type=event_type,
            signal_key=signal_key,
            evidence_hash=evidence_hash,
            trust_delta=trust_delta,
            quality_delta=quality_delta,
            created_at=now,
        )
        if not inserted:
            return False
        state["trust_score"] += trust_delta
        state["quality_score"] += quality_delta
        if event_type == "positive_feedback":
            state["positive_feedback"] += 1
        elif event_type == "negative_feedback":
            state["negative_feedback"] += 1
            state["rupture_count"] += 1
        return True

    # 作用：识别用户当前明确设立的主动联系、亲密度和话题边界。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _boundaries(self, text: str) -> list[str]:
        return [key for key, phrases in BOUNDARY_SIGNALS if any(p in text for p in phrases)]

    # 作用：识别用户明确解除的既有关系边界。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _released_boundaries(self, text: str) -> list[str]:
        return [
            key
            for key, phrases in BOUNDARY_RELEASE_SIGNALS
            if any(phrase in text for phrase in phrases)
        ]

    # 作用：将信任与质量分数限制在约定范围，防止累计事件造成漂移。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    def _clamp(self, state: dict[str, Any]) -> None:
        state["trust_score"] = round(min(1.0, max(0.0, state["trust_score"])), 3)
        state["quality_score"] = round(min(1.0, max(-1.0, state["quality_score"])), 3)

    # 作用：生成去除内部时间字段的公开关系视图，并附带阶段与近期事件。
    # 参数 state：待读取、公开、格式化或持久化的状态对象。
    def _public(self, state: dict[str, Any]) -> dict[str, object]:
        result = {
            key: value
            for key, value in state.items()
            if key not in {"updated_at", "last_interaction_at"}
        }
        result["stage"] = self._stage(state)
        result["needs_trust_rebuild"] = (
            int(state["rupture_count"]) > 0 and float(state["trust_score"]) < 0.32
        )
        result["guidance"] = self._guidance(result, {})
        result["events"] = self.store.list_relationship_events(state["session_id"], limit=10)
        return result
