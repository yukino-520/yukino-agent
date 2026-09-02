import re

from service_club.core.types import SafetyLevel

CRISIS_PATTERNS = [
    r"不想活",
    r"自杀",
    r"伤害自己",
    r"活不下去",
    r"结束生命",
]

HIGH_RISK_PATTERNS = [
    r"药",
    r"诊断",
    r"违法",
    r"报复",
    r"伤害他",
    r"借贷",
    r"投资",
]


# 作用：根据危机和高风险关键词决定本轮回复需要采用的安全等级。
# 参数 text：待识别、切分、清理或合成语音的输入文本。
def detect_safety_level(text: str) -> SafetyLevel:
    if any(re.search(pattern, text) for pattern in CRISIS_PATTERNS):
        return "crisis"
    if any(re.search(pattern, text) for pattern in HIGH_RISK_PATTERNS):
        return "high_risk"
    return "normal"
