from dataclasses import dataclass, field
from typing import Any


# 作用：保存本轮回答在开场、建议、提问、角色扮演和安全方面的约束。
# 参数 opening_move：回答开场时应该采取的沟通动作。
# 参数 advice_mode：本轮提供建议时应遵循的方式。
# 参数 question_budget：整条回复最多允许提出的问题数。
# 参数 roleplay_intensity：本轮允许展现角色扮演的强度。
# 参数 safety_directive：回复必须优先遵守的安全处理指令。
# 参数 must_include：本轮回复必须覆盖的内容要求。
# 参数 avoid：本轮回复明确应避免的表达方式。
@dataclass(frozen=True)
# 作用：定义“ResponsePolicy”相关的数据结构、异常类型或服务组件。
# 字段：opening_move：该对象中的结构化字段。、advice_mode：该对象中的结构化字段。、question_budget：该对象中的结构化字段。、roleplay_intensity：该对象中的结构化字段。、safety_directive：该对象中的结构化字段。、must_include：该对象中的结构化字段。、avoid：该对象中的结构化字段。
class ResponsePolicy:
    opening_move: str
    advice_mode: str
    question_budget: int
    roleplay_intensity: str
    safety_directive: str
    must_include: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)

    # 作用：将响应策略转换为可记录和返回客户端的独立字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "opening_move": self.opening_move,
            "advice_mode": self.advice_mode,
            "question_budget": self.question_budget,
            "roleplay_intensity": self.roleplay_intensity,
            "safety_directive": self.safety_directive,
            "must_include": list(self.must_include),
            "avoid": list(self.avoid),
        }


# 作用：根据情绪、安全等级、会话模式和关系状态规划回答方式。
# 参数：无。
class ResponsePolicyPlanner:
    NEGATIVE_EMOTIONS = {"sad", "lonely", "anxious", "angry"}

    # 作用：生成本轮开场动作、建议方式、提问预算和必须遵守的安全规则。
    # 参数 emotion_label：当前用户的标准化情绪标签。
    # 参数 safety_level：本轮正常、高风险或危机安全等级。
    # 参数 conversation_mode：日常、委托、安慰等回应策略模式。
    # 参数 relationship：当前会话的关系阶段、边界和修复状态。
    # 参数 proactive_care：关系模块生成的主动关怀提示列表。
    def plan(
        self,
        *,
        emotion_label: str,
        safety_level: str,
        conversation_mode: str,
        relationship: dict[str, Any],
        proactive_care: list[dict[str, Any]],
    ) -> ResponsePolicy:
        if safety_level == "crisis":
            return ResponsePolicy(
                opening_move="safety_check",
                advice_mode="immediate_support",
                question_budget=1,
                roleplay_intensity="low",
                safety_directive="crisis_first",
                must_include=["现实求助", "不要独处"],
                avoid=["玩梗", "长篇分析", "淡化风险"],
            )
        if safety_level == "high_risk":
            return ResponsePolicy(
                opening_move="ground_and_validate",
                advice_mode="support_then_options",
                question_budget=1,
                roleplay_intensity="low",
                safety_directive="safety_first",
                must_include=["先确认安全"],
                avoid=["责备", "强行乐观"],
            )
        if relationship.get("rupture_state") == "ruptured":
            return ResponsePolicy(
                opening_move="repair_first",
                advice_mode="ask_preference_before_help",
                question_budget=1,
                roleplay_intensity="low",
                safety_directive="normal",
                must_include=["承认影响"],
                avoid=["辩解", "索取原谅", "恢复亲密称呼"],
            )
        if relationship.get("rupture_state") == "repairing":
            return ResponsePolicy(
                opening_move="repair_continuity",
                advice_mode="follow_user_preference",
                question_budget=1,
                roleplay_intensity="low",
                safety_directive="normal",
                avoid=["重复索取确认", "假装已经没事", "恢复亲密称呼"],
            )
        if emotion_label in self.NEGATIVE_EMOTIONS or proactive_care:
            return ResponsePolicy(
                opening_move="validate_first",
                advice_mode="small_next_step",
                question_budget=1,
                roleplay_intensity=self._roleplay_intensity(relationship),
                safety_directive="normal",
                must_include=["接住情绪"],
                avoid=["连续追问", "直接说教"],
            )
        if conversation_mode == "request":
            return ResponsePolicy(
                opening_move="clarify_goal",
                advice_mode="structured_steps",
                question_budget=2,
                roleplay_intensity=self._roleplay_intensity(relationship),
                safety_directive="normal",
                avoid=["空泛安慰"],
            )
        return ResponsePolicy(
            opening_move="natural_reply",
            advice_mode="none_unless_needed",
            question_budget=1,
            roleplay_intensity=self._roleplay_intensity(relationship),
            safety_directive="normal",
            avoid=["过度亲密"],
        )

    # 作用：把结构化策略格式化为模型可直接遵守的内部提示片段。
    # 参数 policy：本轮结构化回复策略及其安全、关系边界。
    def format_for_prompt(self, policy: ResponsePolicy) -> str:
        return (
            "回复策略："
            f"开场={policy.opening_move}；"
            f"建议方式={policy.advice_mode}；"
            f"最多提问={policy.question_budget}；"
            f"角色扮演强度={policy.roleplay_intensity}；"
            f"安全指令={policy.safety_directive}；"
            f"必须包含={','.join(policy.must_include) or '无'}；"
            f"避免={','.join(policy.avoid) or '无'}"
        )

    # 作用：根据关系阶段和修复状态限制角色扮演强度。
    # 参数 relationship：当前会话的关系阶段、边界和修复状态。
    def _roleplay_intensity(self, relationship: dict[str, Any]) -> str:
        if relationship.get("rupture_state") in {"ruptured", "repairing"}:
            return "low"
        if relationship.get("needs_trust_rebuild") is True:
            return "low"
        if "low_intimacy" in relationship.get("boundaries", []):
            return "low"
        stage = str(relationship.get("stage", "new"))
        if stage == "trusted":
            return "high"
        return "medium"


# 作用：在模型回复后执行确定性策略修正，避免违反危机处理或关系修复边界。
# 参数：无。
class ResponsePolicyEnforcer:
    CRISIS_APPENDIX = (
        "如果你现在有伤害自己的冲动，请立刻寻求现实求助：联系身边可信任的人或当地紧急服务，"
        "先不要独处，把自己放到有人能看见、能帮到你的地方。"
    )
    REPAIR_PREFIX = (
        "刚才的回应让你不舒服，是我没有接住你的感受。对不起。"
        "我不会为自己辩解，会按你希望的方式调整。"
    )

    # 作用：检查策略必需内容，并在危机或关系破裂场景补充安全文本。
    # 参数 content：待审计、修正或返回的模型回复文本。
    # 参数 policy：本轮结构化回复策略及其安全、关系边界。
    def enforce(self, content: str, policy: ResponsePolicy) -> tuple[str, dict[str, object]]:
        missing = [item for item in policy.must_include if item not in content]
        if "承认影响" in missing and self._acknowledges_impact(content):
            missing.remove("承认影响")
        audit: dict[str, object] = {
            "modified": False,
            "missing": missing,
            "safety_directive": policy.safety_directive,
        }
        if policy.safety_directive == "crisis_first" and missing:
            reinforced = f"{content.rstrip()}\n\n{self.CRISIS_APPENDIX}"
            audit["modified"] = True
            audit["reinforcement"] = "crisis_appendix"
            return reinforced, audit
        if policy.opening_move == "repair_first" and missing:
            reinforced = f"{self.REPAIR_PREFIX}\n\n{content.lstrip()}"
            audit["modified"] = True
            audit["reinforcement"] = "relationship_repair_prefix"
            return reinforced, audit
        return content, audit

    # 作用：判断回复是否同时包含道歉和对用户负面影响的承认。
    # 参数 content：待审计、修正或返回的模型回复文本。
    def _acknowledges_impact(self, content: str) -> bool:
        apology = any(phrase in content for phrase in ("对不起", "抱歉", "是我"))
        impact = any(
            phrase in content
            for phrase in ("不舒服", "难受", "没接住", "没有听懂", "让你觉得", "刚才")
        )
        return apology and impact
