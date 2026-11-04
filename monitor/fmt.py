"""將 token 數量、重置倒數及本機時間轉成繁體中文文字。"""

import math
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


def format_tokens(n: int) -> str:
    n = max(0, n)
    if n < 10_000:
        return f"{n:,}"
    if n < 100_000_000:
        rounded = int((Decimal(n) / 10_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return f"{rounded:,} 萬"
    rounded = (Decimal(n) / 100_000_000).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    return f"{rounded:,.1f} 億"


def format_countdown(seconds: float) -> str:
    if seconds <= 0 or not math.isfinite(seconds):
        return "已重置"
    if seconds < 3600:
        return f"{max(1, math.ceil(seconds / 60))} 分"
    if seconds < 86400:
        hours, minutes = divmod(math.ceil(seconds / 60), 60)
        return f"{hours} 小時" + (f" {minutes} 分" if minutes else "")
    days, hours = divmod(int(seconds // 3600), 24)
    return f"{days} 天" + (f" {hours} 小時" if hours else "")


def format_clock(ts: float) -> str:
    value = datetime.fromtimestamp(ts)
    return value.strftime("%H:%M" if value.date() == datetime.now().date() else "%m/%d %H:%M")
