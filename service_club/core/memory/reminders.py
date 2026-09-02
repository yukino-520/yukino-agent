from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# 作用：表示从中文提醒请求中解析出的内容、绝对时间、时区和重复规则。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“ParsedReminder”相关的数据结构、异常类型或服务组件。
# 字段：content：该对象中的结构化字段。、due_at：该对象中的结构化字段。、timezone：该对象中的结构化字段。、expression：该对象中的结构化字段。、recurrence：该对象中的结构化字段。
class ParsedReminder:
    content: str
    due_at: float | None
    timezone: str
    expression: str = ""
    recurrence: str = ""

    # 作用：判断提醒是否已解析出可信的到期时间。
    # 参数：无。
    @property
    # 作用：执行“scheduled”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def scheduled(self) -> bool:
        return self.due_at is not None

    # 作用：将到期时间按提醒时区格式化为 ISO 字符串，未排期则返回空值。
    # 参数：无。
    @property
    # 作用：执行“due_iso”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def due_iso(self) -> str:
        if self.due_at is None:
            return ""
        return dt.datetime.fromtimestamp(self.due_at, self.zone).isoformat()

    # 作用：解析提醒时区，配置无效时安全回退到上海时区。
    # 参数：无。
    @property
    # 作用：执行“zone”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("Asia/Shanghai")


# 作用：保守解析常见中文提醒时间，无法消除歧义时不伪造精确排期。
# 参数：无。
class ReminderScheduleParser:
    """Parse common Chinese reminder times without pretending ambiguity is exact."""

    DEFAULT_TIMEZONE = "Asia/Shanghai"
    NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百]+)"
    RELATIVE = re.compile(
        rf"(?P<number>{NUMBER})\s*(?P<unit>分钟|分|小时|钟头|天)\s*(?:之)?后"
    )
    TIME = re.compile(
        rf"(?P<period>凌晨|早上|上午|中午|下午|晚上|今晚)?\s*"
        rf"(?P<hour>{NUMBER})\s*(?P<separator>[:：点时])\s*"
        rf"(?P<minute>\d{{1,2}}|半|一刻|三刻)?\s*(?:分)?"
    )
    ISO_DATE_TIME = re.compile(
        r"(?P<year>20\d{2})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})日?"
        r"(?:[ T]|\s)*(?P<hour>\d{1,2})(?:[:：点时])(?P<minute>\d{1,2})?"
    )
    MONTH_DAY = re.compile(
        r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"
    )
    DAY_WORD = re.compile(r"大后天|后天|明天|今天|今晚")
    DAILY = re.compile(r"每天|每日")
    WEEKDAY = re.compile(
        r"(?P<prefix>每周|每星期|下周|下星期|本周|这周|周|星期)"
        r"(?P<weekday>[一二三四五六日天])"
    )
    REMINDER_WORDS = re.compile(r"(?:请|麻烦)?(?:帮我|记得)?提醒我|帮我提醒|记得提醒我")

    # 作用：初始化解析时区，非法配置回退默认时区。
    # 参数 timezone：提醒或检查点计算采用的 IANA 时区名称。
    def __init__(self, timezone: str = DEFAULT_TIMEZONE) -> None:
        try:
            self.zone = ZoneInfo(timezone)
            self.timezone = timezone
        except (ZoneInfoNotFoundError, ValueError):
            self.zone = ZoneInfo(self.DEFAULT_TIMEZONE)
            self.timezone = self.DEFAULT_TIMEZONE

    # 作用：按相对时间、绝对日期、星期和自然日规则解析提醒内容与排期。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    def parse(
        self,
        text: str,
        *,
        now: dt.datetime | None = None,
    ) -> ParsedReminder:
        cleaned = self.REMINDER_WORDS.sub(" ", text).strip(" ，,。.!！：:")
        reference = now or dt.datetime.now(self.zone)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=self.zone)
        else:
            reference = reference.astimezone(self.zone)

        relative = self.RELATIVE.search(cleaned)
        if relative:
            number = self._number(relative.group("number"))
            unit = relative.group("unit")
            delta = (
                dt.timedelta(minutes=number)
                if unit in {"分钟", "分"}
                else dt.timedelta(hours=number)
                if unit in {"小时", "钟头"}
                else dt.timedelta(days=number)
            )
            due = reference + delta
            return self._result(cleaned, relative, due)

        explicit = self.ISO_DATE_TIME.search(cleaned)
        if explicit:
            minute = int(explicit.group("minute") or 0)
            try:
                due = dt.datetime(
                    int(explicit.group("year")),
                    int(explicit.group("month")),
                    int(explicit.group("day")),
                    int(explicit.group("hour")),
                    minute,
                    tzinfo=self.zone,
                )
            except ValueError:
                return ParsedReminder(cleaned, None, self.timezone)
            if due <= reference:
                return ParsedReminder(
                    cleaned,
                    None,
                    self.timezone,
                    expression=explicit.group().strip(),
                )
            return self._result(cleaned, explicit, due)

        time_match = self.TIME.search(cleaned)
        if time_match is None:
            day_only = self.DAY_WORD.search(cleaned)
            daily_only = self.DAILY.search(cleaned)
            weekday_only = self.WEEKDAY.search(cleaned)
            if day_only is None and daily_only is None and weekday_only is None:
                return ParsedReminder(cleaned, None, self.timezone)
            if daily_only:
                due = dt.datetime.combine(reference.date(), dt.time(hour=9), tzinfo=self.zone)
                if due <= reference:
                    due += dt.timedelta(days=1)
                return self._result(cleaned, daily_only, due, recurrence="daily")
            if weekday_only:
                due = self._weekday_due(weekday_only, reference, hour=9, minute=0)
                recurrence = (
                    f"weekly:{self._weekday_number(weekday_only.group('weekday'))}"
                    if weekday_only.group("prefix").startswith("每")
                    else ""
                )
                return self._result(cleaned, weekday_only, due, recurrence=recurrence)
            due_date = reference.date() + dt.timedelta(days=self._day_offset(day_only.group()))
            default_hour = 20 if day_only.group() == "今晚" else 9
            due = dt.datetime.combine(due_date, dt.time(hour=default_hour), tzinfo=self.zone)
            if due <= reference:
                return ParsedReminder(
                    cleaned,
                    None,
                    self.timezone,
                    expression=day_only.group().strip(),
                )
            return self._result(cleaned, day_only, due)

        hour = self._number(time_match.group("hour"))
        minute = self._minute(time_match.group("minute"))
        period = time_match.group("period") or ""
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            return ParsedReminder(cleaned, None, self.timezone)
        if period in {"下午", "晚上", "今晚"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0

        prefix_text = cleaned[: time_match.start() + 1]
        day_match = self.DAY_WORD.search(prefix_text)
        month_day = self.MONTH_DAY.search(prefix_text)
        daily_match = self.DAILY.search(prefix_text)
        weekday_match = self.WEEKDAY.search(prefix_text)
        recurrence = ""
        if month_day:
            year = reference.year
            try:
                due_date = dt.date(
                    year,
                    int(month_day.group("month")),
                    int(month_day.group("day")),
                )
            except ValueError:
                return ParsedReminder(cleaned, None, self.timezone)
            due = dt.datetime.combine(due_date, dt.time(hour, minute), tzinfo=self.zone)
            if due <= reference:
                try:
                    due = due.replace(year=year + 1)
                except ValueError:
                    return ParsedReminder(cleaned, None, self.timezone)
            expression_start = month_day.start()
        elif daily_match:
            due = dt.datetime.combine(reference.date(), dt.time(hour, minute), tzinfo=self.zone)
            if due <= reference:
                due += dt.timedelta(days=1)
            expression_start = daily_match.start()
            recurrence = "daily"
        elif weekday_match:
            due = self._weekday_due(weekday_match, reference, hour=hour, minute=minute)
            expression_start = weekday_match.start()
            if weekday_match.group("prefix").startswith("每"):
                recurrence = f"weekly:{self._weekday_number(weekday_match.group('weekday'))}"
        else:
            offset = self._day_offset(day_match.group()) if day_match else 0
            due_date = reference.date() + dt.timedelta(days=offset)
            due = dt.datetime.combine(due_date, dt.time(hour, minute), tzinfo=self.zone)
            if day_match and due <= reference:
                return ParsedReminder(
                    cleaned,
                    None,
                    self.timezone,
                    expression=cleaned[day_match.start() : time_match.end()].strip(),
                )
            if not day_match and due <= reference:
                due += dt.timedelta(days=1)
            expression_start = day_match.start() if day_match else time_match.start()
        expression_end = time_match.end()
        expression = cleaned[expression_start:expression_end]
        content = self._remove_span(cleaned, expression_start, expression_end)
        return ParsedReminder(
            content=content or "提醒事项",
            due_at=due.timestamp(),
            timezone=self.timezone,
            expression=expression.strip(),
            recurrence=recurrence,
        )

    # 作用：从原文移除已识别时间片段，组装统一提醒解析结果。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 match：正则表达式匹配到的时间片段对象。
    # 参数 due：已经解析并带时区的提醒到期时间。
    # 参数 recurrence：提醒的每日或每周重复规则。
    def _result(
        self,
        text: str,
        match: re.Match[str],
        due: dt.datetime,
        *,
        recurrence: str = "",
    ) -> ParsedReminder:
        content = self._remove_span(text, match.start(), match.end())
        return ParsedReminder(
            content=content or "提醒事项",
            due_at=due.timestamp(),
            timezone=self.timezone,
            expression=match.group().strip(),
            recurrence=recurrence,
        )

    # 作用：计算本周、下周或每周指定星期的下一次合法到期时间。
    # 参数 match：正则表达式匹配到的时间片段对象。
    # 参数 reference：解析相对提醒时间使用的基准日期时间。
    # 参数 hour：提醒触发时间中的小时值。
    # 参数 minute：提醒触发时间中的分钟值。
    def _weekday_due(
        self,
        match: re.Match[str],
        reference: dt.datetime,
        *,
        hour: int,
        minute: int,
    ) -> dt.datetime:
        target = self._weekday_number(match.group("weekday"))
        prefix = match.group("prefix")
        if prefix in {"下周", "下星期"}:
            days = 7 - reference.weekday() + target
        else:
            days = (target - reference.weekday()) % 7
        due_date = reference.date() + dt.timedelta(days=days)
        due = dt.datetime.combine(due_date, dt.time(hour, minute), tzinfo=self.zone)
        if due <= reference:
            due += dt.timedelta(days=7)
        return due

    # 作用：将中文星期字符映射为 Python 的零基星期编号。
    # 参数 value：待规范化、持久化或解析的业务值。
    @staticmethod
    # 作用：执行“weekday_number”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _weekday_number(value: str) -> int:
        return {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[value]

    # 作用：删除原文中的时间表达并归一化剩余提醒正文。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    # 参数 start：待从文本移除的片段起始下标。
    # 参数 end：待从文本移除的片段结束下标。
    @staticmethod
    # 作用：执行“remove_span”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 text：待分析、记录或处理的自然语言文本。
    # 参数 start：调用方传入的start，用于本次处理。
    # 参数 end：调用方传入的end，用于本次处理。
    def _remove_span(text: str, start: int, end: int) -> str:
        value = f"{text[:start]} {text[end:]}"
        return re.sub(r"\s+", " ", value).strip(" ，,。.!！：:")

    # 作用：将阿拉伯数字或常用中文数字转换为整数。
    # 参数 value：待规范化、持久化或解析的业务值。
    @classmethod
    # 作用：执行“number”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _number(cls, value: str) -> int:
        if value.isdigit():
            return int(value)
        digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if "百" in value:
            left, _, right = value.partition("百")
            return (digits.get(left, 1) * 100) + cls._number(right or "零")
        if "十" in value:
            left, _, right = value.partition("十")
            return (digits.get(left, 1) * 10) + digits.get(right, 0)
        result = 0
        for character in value:
            result = result * 10 + digits.get(character, 0)
        return result

    # 作用：将整点、半点和刻钟表达统一换算为分钟数。
    # 参数 value：待规范化、持久化或解析的业务值。
    @classmethod
    # 作用：执行“minute”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _minute(cls, value: str | None) -> int:
        if not value:
            return 0
        if value == "半":
            return 30
        if value == "一刻":
            return 15
        if value == "三刻":
            return 45
        return cls._number(value)

    # 作用：将今天、明天、后天等自然日表达映射为天数偏移。
    # 参数 value：待规范化、持久化或解析的业务值。
    @staticmethod
    # 作用：执行“day_offset”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 value：需要转换、校验或保存的输入值。
    def _day_offset(value: str) -> int:
        return {"今天": 0, "今晚": 0, "明天": 1, "后天": 2, "大后天": 3}.get(value, 0)
