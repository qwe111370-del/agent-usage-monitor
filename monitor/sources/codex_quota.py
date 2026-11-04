"""唯讀 Codex 登入資訊並查詢官方用量，保留本機額度後備介面。"""

import json
from pathlib import Path

from ..model import QuotaWindow
from ..secure_http import get_json
from .claude_local import _number
from .codex_local import _window_label


def parse_usage_response(obj: dict) -> tuple[str | None, list[QuotaWindow]]:
    if not isinstance(obj, dict):
        return None, []
    plan = obj.get("plan_type")
    rate = obj.get("rate_limit")
    windows = []
    if isinstance(rate, dict):
        for name, default in (("primary_window", 18000), ("secondary_window", 604800)):
            value = rate.get(name)
            if not isinstance(value, dict):
                continue
            seconds = max(1, int(_number(value.get("limit_window_seconds"), default)))
            weekly = seconds >= 86400
            key = "seven_day" if weekly else "five_hour"
            label = "每週" if weekly else _window_label(seconds)
            windows.append(QuotaWindow(key, label, float(_number(value.get("used_percent"))),
                                       _number(value.get("reset_at"), None), seconds))
    return plan if isinstance(plan, str) else None, windows


def read_auth(codex_home: Path) -> tuple[str, str | None] | None:
    try:
        obj = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))["tokens"]
        token, account = obj.get("access_token"), obj.get("account_id")
        if isinstance(token, str) and token.strip():
            return token, account if isinstance(account, str) and account.strip() else None
    except Exception:
        pass
    return None


def query_usage(token: str, account_id: str | None = None) -> tuple[str | None, list[QuotaWindow]]:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return parse_usage_response(get_json("https://chatgpt.com/backend-api/wham/usage", headers))
