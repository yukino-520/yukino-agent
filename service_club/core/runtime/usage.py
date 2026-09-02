from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


# 作用：以追加日志形式记录模型 token 用量，并按配置估算调用成本。
# 参数：无。
class UsageLedger:
    # 作用：准备用量日志文件及线程内串行写入锁。
    # 参数 path：用量账本日志文件路径。
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # 作用：写入一次模型调用的 token、费用和状态事件。
    # 参数 model：当前调用、检查点或健康监控对应的模型名称。
    # 参数 input_tokens：模型调用实际使用或待估价的输入 Token 数。
    # 参数 output_tokens：模型调用实际使用或待估价的输出 Token 数。
    # 参数 status：要写入、筛选或转换的执行状态。
    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        status: str = "ok",
    ) -> dict[str, Any]:
        input_rate = float(os.getenv("YUKINO_INPUT_COST_PER_MILLION", "0"))
        output_rate = float(os.getenv("YUKINO_OUTPUT_COST_PER_MILLION", "0"))
        estimated_cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        event = {
            "created_at": time.time(),
            "model": model,
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "estimated_cost": round(estimated_cost, 8),
            "currency": os.getenv("YUKINO_COST_CURRENCY", "USD"),
            "status": status,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    # 作用：读取时间范围内的近期有效用量事件，并跳过损坏日志行。
    # 参数 days：向前统计用量事件的天数。
    # 参数 limit：本次查询、资源预算或异常上下文采用的上限。
    def events(self, *, days: int = 1, limit: int = 2000) -> list[dict[str, Any]]:
        cutoff = time.time() - max(1, days) * 86400
        try:
            with self._lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result = []
        for line in lines[-limit:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(event.get("created_at", 0)) >= cutoff:
                result.append(event)
        return result

    # 作用：汇总给定天数内的调用量、token、估算费用及模型维度明细。
    # 参数 days：向前统计用量事件的天数。
    def summary(self, *, days: int = 1) -> dict[str, Any]:
        events = self.events(days=days)
        by_model: dict[str, dict[str, float | int]] = {}
        for event in events:
            model = str(event.get("model", "unknown"))
            bucket = by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0})
            bucket["calls"] += 1
            bucket["input_tokens"] += int(event.get("input_tokens", 0))
            bucket["output_tokens"] += int(event.get("output_tokens", 0))
            bucket["estimated_cost"] = round(float(bucket["estimated_cost"]) + float(event.get("estimated_cost", 0)), 8)
        return {
            "days": max(1, days),
            "calls": len(events),
            "input_tokens": sum(int(item.get("input_tokens", 0)) for item in events),
            "output_tokens": sum(int(item.get("output_tokens", 0)) for item in events),
            "estimated_cost": round(sum(float(item.get("estimated_cost", 0)) for item in events), 8),
            "currency": os.getenv("YUKINO_COST_CURRENCY", "USD"),
            "by_model": by_model,
        }
