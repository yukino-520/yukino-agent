import re
from dataclasses import dataclass

from service_club.core.tooling.executor import ParsedToolAction, parse_tool_action
from service_club.core.types import ToolAction


# 作用：表示从用户复合请求中提取出的一个确定性工具步骤。
# 参数 action：确定性工具步骤要执行的动作。
# 参数 argument：确定性工具步骤携带的主要参数。
# 参数 source_text：产生当前意图步骤的原始用户文本片段。
@dataclass(frozen=True)
# 作用：定义“IntentStep”相关的数据结构、异常类型或服务组件。
# 字段：action：该对象中的结构化字段。、argument：该对象中的结构化字段。、source_text：该对象中的结构化字段。
class IntentStep:
    action: ToolAction
    argument: str = ""
    source_text: str = ""

    # 作用：将意图步骤转换为便于记录和返回客户端的追踪字典。
    # 参数：无。
    def as_trace(self) -> dict[str, str]:
        return {
            "action": self.action,
            "argument": self.argument,
            "source_text": self.source_text,
        }


# 作用：用确定性规则把复合自然语言委托拆成可直接执行和验收的工具步骤。
# 参数：无。
class IntentDecomposer:
    # 作用：将用户文本拆成多个片段，解析工具动作并合并去重为执行步骤。
    # 参数 text：待识别、切分、清理或合成语音的输入文本。
    def decompose(self, text: str) -> list[IntentStep]:
        parts = self._split(text)
        parsed_parts = [self._to_step(part, parse_tool_action(part)) for part in parts]
        steps = [step for step in parsed_parts if step.action != "none"]
        whole_step = self._to_step(text, parse_tool_action(text))
        if whole_step.action != "none":
            steps = self._merge_whole_step(whole_step, steps)
        return self._dedupe(steps)

    # 作用：按中文连接词和常见标点切分复合委托。
    # 参数 text：待识别、切分、清理或合成语音的输入文本。
    def _split(self, text: str) -> list[str]:
        parts = [
            item.strip()
            for item in re.split(r"(?:然后|并且|同时|另外|再|，|,|；|;)", text)
            if item.strip()
        ]
        return parts or [text.strip()]

    # 作用：把底层解析结果连同原始片段封装为可追踪的意图步骤。
    # 参数 source_text：产生当前意图步骤的原始用户文本片段。
    # 参数 parsed：底层工具意图解析器返回的动作和参数。
    def _to_step(self, source_text: str, parsed: ParsedToolAction) -> IntentStep:
        return IntentStep(
            action=parsed.action,
            argument=parsed.argument,
            source_text=source_text.strip(),
        )

    # 作用：在整句解析更可靠时保留整句动作，否则把它与分段动作合并。
    # 参数 whole_step：从完整用户文本直接解析出的候选意图步骤。
    # 参数 split_steps：从分段文本中解析出的候选意图步骤。
    def _merge_whole_step(
        self,
        whole_step: IntentStep,
        split_steps: list[IntentStep],
    ) -> list[IntentStep]:
        if whole_step.action in {"analyze_code", "write_file", "run_python"}:
            return [whole_step]
        if not split_steps:
            return [whole_step]
        if whole_step.action in {step.action for step in split_steps}:
            return split_steps
        return [whole_step, *split_steps]

    # 作用：按动作和参数去除重复步骤，同时保持用户原始顺序。
    # 参数 steps：待按动作和参数去重的意图步骤列表。
    def _dedupe(self, steps: list[IntentStep]) -> list[IntentStep]:
        seen: set[tuple[str, str]] = set()
        deduped: list[IntentStep] = []
        for step in steps:
            key = (step.action, step.argument)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(step)
        return deduped
