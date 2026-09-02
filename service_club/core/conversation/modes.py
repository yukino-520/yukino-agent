import re

from service_club.core.types import ConversationMode


# 作用：根据用户措辞推断日常、安静、委托、吐槽、严肃或安慰模式。
# 参数 text：待识别、切分、清理或合成语音的输入文本。
def infer_mode(text: str) -> ConversationMode:
    if re.search(r"不想说话|陪我待会|安静", text):
        return "quiet"
    if re.search(
        r"委托|帮我想想|怎么办|该怎么|怎么做|怎么开始|如何|给我建议|帮我分析|有什么办法",
        text,
    ):
        return "request"
    if re.search(r"摆烂|吐槽|随便聊|闲聊", text):
        return "banter"
    if re.search(r"计划|学习|认真|选择", text):
        return "serious"
    if re.search(r"难过|孤独|好累|安慰", text):
        return "comfort"
    return "daily"
