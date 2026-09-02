from dataclasses import dataclass, field
from typing import Any

from service_club.core.types import CharacterId, ChatMode, RoutingMode

MENTION_MAP: dict[str, CharacterId] = {
    "@雪乃": "yukino",
    "@雪之下": "yukino",
    "@结衣": "yui",
    "@由比滨": "yui",
    "@八幡": "hachiman",
    "@比企谷": "hachiman",
    "@一色": "iroha",
    "@彩羽": "iroha",
    "@平冢": "shizuka",
    "@老师": "shizuka",
}

CLUB_INTERJECTIONS: dict[CharacterId, list[CharacterId]] = {
    "yukino": ["yukino", "yui", "hachiman"],
    "yui": ["yui", "yukino", "hachiman"],
    "hachiman": ["hachiman", "yui", "yukino"],
    "iroha": ["iroha", "hachiman", "yui"],
    "shizuka": ["shizuka", "hachiman", "yukino"],
}


# 保存本轮主角色、参与角色、路由方式和各候选角色的评分依据。
@dataclass(frozen=True)
# 作用：定义“RoutingDecision”相关的数据结构、异常类型或服务组件。
# 字段：primary_agent：该对象中的结构化字段。、active_agents：该对象中的结构化字段。、mode：该对象中的结构化字段。、reasoning：该对象中的结构化字段。、scorecard：该对象中的结构化字段。
class RoutingDecision:
    primary_agent: CharacterId
    active_agents: list[CharacterId]
    mode: str
    reasoning: str
    scorecard: dict[str, dict[str, float | int]] = field(default_factory=dict)


# 根据点名、聊天模式和自适应评分决定由哪些角色参与本轮回复。
class ServiceClubRouter:
    # 注入历史路由反馈，用于修正自适应角色选择。
    def __init__(self, route_feedback: Any | None = None) -> None:
        self.route_feedback = route_feedback

    # 按“显式点名、私聊选择、自适应评分、手动选择”的优先级生成路由结果。
    def decide(
        self,
        *,
        text: str,
        chat_mode: ChatMode,
        selected_character: CharacterId,
        routing_mode: RoutingMode = "manual",
    ) -> RoutingDecision:
        mentioned = self._match_mention(text)
        if mentioned:
            agents = self._club_agents(mentioned) if chat_mode == "club" else [mentioned]
            return RoutingDecision(
                primary_agent=mentioned,
                active_agents=agents,
                mode="club" if chat_mode == "club" and len(agents) > 1 else "single",
                reasoning=f"mention:{mentioned}",
            )
        if chat_mode == "solo":
            return RoutingDecision(
                primary_agent=selected_character,
                active_agents=[selected_character],
                mode="single",
                reasoning=f"solo:selected:{selected_character}",
            )
        if routing_mode == "adaptive":
            primary, scorecard = self._adaptive_primary(text, selected_character)
            return RoutingDecision(
                primary_agent=primary,
                active_agents=self._club_agents(primary),
                mode="club",
                reasoning=f"club:adaptive:{primary}",
                scorecard=scorecard,
            )
        return RoutingDecision(
            primary_agent=selected_character,
            active_agents=self._club_agents(selected_character),
            mode="club",
            reasoning=f"club:selected:{selected_character}",
        )

    # 从用户文本中识别明确点名的角色。
    def _match_mention(self, text: str) -> CharacterId | None:
        for mention, character_id in MENTION_MAP.items():
            if mention in text:
                return character_id
        return None

    # 根据主角色选择最多三个适合共同发言的群像角色。
    def _club_agents(self, primary: CharacterId) -> list[CharacterId]:
        return CLUB_INTERJECTIONS.get(primary, [primary])[:3]

    # 融合语义匹配、历史成功率、延迟和用户先验选择，计算自适应主角色。
    def _adaptive_primary(
        self,
        text: str,
        selected_character: CharacterId,
    ) -> tuple[CharacterId, dict[str, dict[str, float | int]]]:
        affinities = self._semantic_affinities(text)
        scorecard: dict[str, dict[str, float | int]] = {}
        for agent_id, semantic in affinities.items():
            belief = (
                self.route_feedback.belief_score(agent_id)
                if self.route_feedback is not None
                else {"turns": 0, "expected_success": 0.5, "latency_score": 1.0}
            )
            selected_prior = 0.1 if agent_id == selected_character else 0.0
            total = (
                semantic * 0.6
                + float(belief["expected_success"]) * 0.25
                + float(belief["latency_score"]) * 0.05
                + selected_prior
            )
            scorecard[agent_id] = {
                "semantic": round(semantic, 4),
                "expected_success": belief["expected_success"],
                "latency_score": belief["latency_score"],
                "observations": belief["turns"],
                "selected_prior": selected_prior,
                "total": round(total, 4),
            }
        primary = max(scorecard, key=lambda agent_id: float(scorecard[agent_id]["total"]))
        return primary, scorecard  # type: ignore[return-value]

    # 根据情绪、分析、社交等关键词估算文本与各角色定位的语义亲和度。
    def _semantic_affinities(self, text: str) -> dict[CharacterId, float]:
        scores: dict[CharacterId, float] = {
            "yukino": 0.2,
            "yui": 0.2,
            "hachiman": 0.2,
            "iroha": 0.2,
            "shizuka": 0.2,
        }
        groups: list[tuple[tuple[str, ...], dict[CharacterId, float]]] = [
            (
                ("难过", "孤独", "寂寞", "想哭", "焦虑", "撑不住", "安慰", "陪我"),
                {"yui": 0.65, "yukino": 0.15, "hachiman": 0.1},
            ),
            (
                ("分析", "怎么办", "选择", "纠结", "计划", "原因", "拆解"),
                {"yukino": 0.65, "shizuka": 0.3, "hachiman": 0.2},
            ),
            (
                ("社交", "尴尬", "不想见人", "麻烦", "现实", "吐槽"),
                {"hachiman": 0.65, "yukino": 0.2},
            ),
            (
                ("工作", "学习", "考试", "行动", "自律", "求职", "职业"),
                {"shizuka": 0.65, "yukino": 0.3},
            ),
            (
                ("开心", "好玩", "轻松", "聊聊", "气氛", "夸我"),
                {"iroha": 0.55, "yui": 0.35},
            ),
        ]
        for keywords, boosts in groups:
            if any(keyword in text for keyword in keywords):
                for agent_id, boost in boosts.items():
                    scores[agent_id] += boost
        return {agent_id: min(score, 1.0) for agent_id, score in scores.items()}
