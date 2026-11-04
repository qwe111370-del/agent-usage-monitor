"""依時間區間加總用量，並計算本機時區的每日起點。"""

from collections.abc import Iterable
from datetime import datetime

from .model import TokenCounts, UsageEvent


def sum_between(events: Iterable[UsageEvent], start: float, end: float) -> TokenCounts:
    result = TokenCounts()
    for event in events:
        if start <= event.ts < end:
            result = result + event.counts
    return result


def local_midnight(now: float) -> float:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
