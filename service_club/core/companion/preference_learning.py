import re
from dataclasses import dataclass


# 作用：表示从用户原话中提取出的一个可追溯偏好事实候选。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“LearnedPreference”相关的数据结构、异常类型或服务组件。
# 字段：key：该对象中的结构化字段。、value：该对象中的结构化字段。、source_text：该对象中的结构化字段。
class LearnedPreference:
    key: str
    value: str
    source_text: str


# 作用：使用保守规则提取称呼、陪伴风格和互动边界等明确偏好。
# 参数：无。
class PreferenceLearner:
    # 作用：汇总各类规则抽取结果，并为每项保留原始证据文本。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def extract(self, text: str) -> list[LearnedPreference]:
        learned: list[LearnedPreference] = []
        preferred_name = self._extract_preferred_name(text)
        if preferred_name:
            learned.append(
                LearnedPreference(
                    key="preferred_name",
                    value=preferred_name,
                    source_text=text,
                )
            )
        companion_style = self._extract_companion_style(text)
        if companion_style:
            learned.append(
                LearnedPreference(
                    key="companion_style",
                    value=companion_style,
                    source_text=text,
                )
            )
        learned.extend(self._extract_interaction_rules(text))
        return learned

    # 作用：提取用户主动指定的称呼，同时排除“别叫我”一类否定表达。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _extract_preferred_name(self, text: str) -> str:
        pattern = r"(?:以后|之后)?(?:叫我|称呼我)[:：]?\s*([^，。,.！!\s]+)"
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 2) : match.start()]
            if prefix.endswith("别") or prefix.endswith("不要"):
                continue
            return match.group(1).strip()
        return ""

    # 作用：提取用户明确喜欢的陪伴、安慰或说话方式。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _extract_companion_style(self, text: str) -> str:
        match = re.search(r"我喜欢([^，。,.！!]+?(?:陪伴|安慰|聊天|说话方式))", text)
        return match.group(1).strip() if match else ""

    # 作用：从固定措辞中提取回复长度、建议时机和禁用称呼等互动规则。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _extract_interaction_rules(self, text: str) -> list[LearnedPreference]:
        rules: list[LearnedPreference] = []
        compact = text.replace(" ", "")
        if any(token in compact for token in ("简短一点", "短一点", "别太长", "不要太长")):
            rules.append(
                LearnedPreference("response_length", "简短直接，避免长篇说教", text)
            )
        if any(
            token in compact
            for token in ("先安慰", "先陪陪我", "别急着给建议", "不要急着建议")
        ):
            rules.append(
                LearnedPreference(
                    "advice_timing", "先共情和确认感受，再征求是否需要建议", text
                )
            )
        if any(token in compact for token in ("别叫我", "不要叫我")):
            match = re.search(r"(?:别叫我|不要叫我)([^，。,.！!\s]+)", text)
            if match:
                rules.append(
                    LearnedPreference(
                        "avoid_address", f"不要称呼用户为{match.group(1).strip()}", text
                    )
                )
        return rules
