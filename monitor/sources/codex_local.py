"""增量讀取 Codex 累計 token，換算差值並保存最新本機額度。"""

import json
from pathlib import Path

from ..model import QuotaWindow, TokenCounts, UsageEvent
from .claude_local import _JsonlReader, _count, _number, _timestamp


def _window_label(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:g} 天"
    return f"{seconds / 3600:g} 小時"


def rate_limits_to_windows(rl: dict) -> tuple[str | None, list[QuotaWindow]]:
    if not isinstance(rl, dict):
        return None, []
    windows = []
    for name in ("primary", "secondary"):
        value = rl.get(name)
        if not isinstance(value, dict):
            continue
        minutes = _number(value.get("window_minutes"), 300 if name == "primary" else 10080)
        seconds = max(1, int(minutes * 60))
        key = "five_hour" if minutes == 300 else "seven_day" if minutes == 10080 else f"{minutes:g}m"
        label = "5 小時" if minutes == 300 else "每週" if minutes == 10080 else _window_label(seconds)
        reset = _number(value.get("resets_at"), None)
        windows.append(QuotaWindow(key, label, float(_number(value.get("used_percent"))), reset, seconds))
    plan = rl.get("plan_type")
    return plan if isinstance(plan, str) else None, windows


class CodexLogReader(_JsonlReader):
    def __init__(self, roots: list[Path], keep_days: float = 8):
        super().__init__(roots, "rollout-*.jsonl", keep_days)

    def _new_data(self) -> dict:
        return {"records": [], "previous": (0, 0, 0), "latest": None}

    def _consume(self, state: dict, line: str) -> None:
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict) or obj.get("type") != "event_msg":
                return
            payload = obj.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                return
            ts = _timestamp(obj.get("timestamp"))
            if ts is None:
                return
            rl = payload.get("rate_limits")
            if isinstance(rl, dict) and (state["latest"] is None or ts >= state["latest"][0]):
                state["latest"] = (ts, rl)
            info = payload.get("info")
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(usage, dict):
                return
            current = tuple(_count(usage.get(key)) for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
            delta = tuple(max(0, a - b) for a, b in zip(current, state["previous"]))
            state["previous"] = current
            counts = TokenCounts(input=max(0, delta[0] - delta[1]), output=delta[2], cache_read=delta[1])
            self._serial += 1
            state["records"].append((None, UsageEvent(ts, counts), self._serial))
        except (ValueError, TypeError, OverflowError, RecursionError):
            return

    @property
    def events(self) -> list[UsageEvent]:
        return sorted((record[1] for state in self._files.values() for record in state["records"]), key=lambda event: event.ts)

    @property
    def latest_rate_limits(self) -> tuple[float, dict] | None:
        values = [state["latest"] for state in self._files.values() if state["latest"] is not None]
        return max(values, key=lambda item: item[0]) if values else None
