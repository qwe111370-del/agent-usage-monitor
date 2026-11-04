"""用量加總與時間區間計算測試。

驗證 monitor.aggregate 模組與 monitor.model.TokenCounts 的運算行為：
- sum_between 的「左閉右開」邊界（ts == start 包含、ts == end 排除、區間外排除）。
- 多個 UsageEvent 的 TokenCounts 各欄位累加正確性、空集合與迭代器支援。
- TokenCounts 的加法運算子（+）、total 與 non_cache 屬性計算。
- local_midnight 本機當日 00:00:00 epoch 秒計算。
"""

from __future__ import annotations

import datetime
from typing import Iterator

import pytest

from monitor.aggregate import local_midnight, sum_between
from monitor.model import TokenCounts, UsageEvent


# ---------------------------------------------------------------------------
# TokenCounts 運算與屬性測試
# ---------------------------------------------------------------------------


class TestTokenCounts:
    """測試 TokenCounts 資料結構之加法與屬性計算。"""

    def test_default_values(self) -> None:
        """預設建構時所有欄位應為 0。"""
        counts = TokenCounts()
        assert counts.input == 0, "預設 input 應為 0"
        assert counts.output == 0, "預設 output 應為 0"
        assert counts.cache_read == 0, "預設 cache_read 應為 0"
        assert counts.cache_write == 0, "預設 cache_write 應為 0"
        assert counts.total == 0, "預設 total 應為 0"
        assert counts.non_cache == 0, "預設 non_cache 應為 0"

    def test_properties(self) -> None:
        """驗證 total 與 non_cache 屬性運算。"""
        counts = TokenCounts(input=100, output=50, cache_read=30, cache_write=20)
        # total = input + output + cache_read + cache_write = 100 + 50 + 30 + 20 = 200
        assert counts.total == 200, "total 應包含所有 4 個欄位"
        # non_cache = input + output + cache_write = 100 + 50 + 20 = 170 (排除 cache_read)
        assert counts.non_cache == 170, "non_cache 應扣除 cache_read"

    def test_addition(self) -> None:
        """驗證兩個 TokenCounts 相加行為。"""
        a = TokenCounts(input=10, output=20, cache_read=30, cache_write=40)
        b = TokenCounts(input=1, output=2, cache_read=3, cache_write=4)
        c = a + b

        assert c.input == 11, "加法後 input 應累加"
        assert c.output == 22, "加法後 output 應累加"
        assert c.cache_read == 33, "加法後 cache_read 應累加"
        assert c.cache_write == 44, "加法後 cache_write 應累加"
        assert c.total == 110, "加法後 total 應為兩者 total 之和"


# ---------------------------------------------------------------------------
# sum_between 左閉右開邊界與加總測試
# ---------------------------------------------------------------------------


class TestSumBetween:
    """測試 sum_between 左閉右開區間過濾與加總邏輯。"""

    def test_empty_events(self) -> None:
        """事件串列為空時，應回傳全為 0 的 TokenCounts。"""
        result = sum_between([], start=1000.0, end=2000.0)
        assert result == TokenCounts(0, 0, 0, 0), "無事件時應回傳全 0 之 TokenCounts"

    def test_left_closed_right_open_boundary(self) -> None:
        """嚴格驗證 start <= ts < end 邊界條件。

        ts == start：應計入（左閉）
        ts == end：應排除（右開）
        ts < start 或 ts > end：應排除
        """
        start_ts = 1000.0
        end_ts = 2000.0

        events = [
            UsageEvent(ts=999.9, counts=TokenCounts(input=1)),      # < start (排除)
            UsageEvent(ts=1000.0, counts=TokenCounts(input=10)),    # == start (計入)
            UsageEvent(ts=1500.0, counts=TokenCounts(input=100)),   # 區間內 (計入)
            UsageEvent(ts=1999.9, counts=TokenCounts(input=1000)),  # 區間內 (計入)
            UsageEvent(ts=2000.0, counts=TokenCounts(input=10000)), # == end (排除)
            UsageEvent(ts=2000.1, counts=TokenCounts(input=100000)),# > end (排除)
        ]

        result = sum_between(events, start=start_ts, end=end_ts)

        # 預期計入：10 + 100 + 1000 = 1110
        assert result.input == 1110, "sum_between 應遵循 start <= ts < end 左閉右開規則"
        assert result.output == 0
        assert result.cache_read == 0
        assert result.cache_write == 0

    def test_all_counts_fields_accumulated(self) -> None:
        """驗證 input, output, cache_read, cache_write 各欄位均正確累積。"""
        events = [
            UsageEvent(ts=100.0, counts=TokenCounts(input=10, output=20, cache_read=30, cache_write=40)),
            UsageEvent(ts=150.0, counts=TokenCounts(input=5, output=15, cache_read=25, cache_write=35)),
        ]
        result = sum_between(events, start=50.0, end=200.0)

        assert result.input == 15, "input 欄位應正確累加"
        assert result.output == 35, "output 欄位應正確累加"
        assert result.cache_read == 55, "cache_read 欄位應正確累加"
        assert result.cache_write == 75, "cache_write 欄位應正確累加"

    def test_generator_input(self) -> None:
        """驗證 sum_between 能接受 Generator / Iterator 傳入。"""
        def event_generator() -> Iterator[UsageEvent]:
            yield UsageEvent(ts=10.0, counts=TokenCounts(input=5))
            yield UsageEvent(ts=20.0, counts=TokenCounts(input=15))
            yield UsageEvent(ts=30.0, counts=TokenCounts(input=25))

        result = sum_between(event_generator(), start=10.0, end=25.0)
        # 10.0 (計入 5), 20.0 (計入 15), 30.0 (排除)
        assert result.input == 20, "傳入 Generator 時應正常迭代加總"

    def test_inverted_range(self) -> None:
        """當 start >= end 時，沒有任何 ts 能滿足 start <= ts < end，應回傳 0。"""
        events = [UsageEvent(ts=100.0, counts=TokenCounts(input=50))]
        result = sum_between(events, start=200.0, end=100.0)
        assert result == TokenCounts(0, 0, 0, 0), "反向區間不應計入任何事件"


# ---------------------------------------------------------------------------
# local_midnight 測試
# ---------------------------------------------------------------------------


class TestLocalMidnight:
    """測試 local_midnight 本機零點時間計算。"""

    def test_local_midnight_fixed_timestamp(self) -> None:
        """驗證固定 timestamp 換算出的零點為當日本機 00:00:00。"""
        # 使用固定 epoch 秒
        now = 1700000000.0
        midnight = local_midnight(now)

        dt_now = datetime.datetime.fromtimestamp(now)
        dt_midnight = datetime.datetime.fromtimestamp(midnight)

        assert dt_midnight.year == dt_now.year, "零點年份應與原時間相同"
        assert dt_midnight.month == dt_now.month, "零點月份應與原時間相同"
        assert dt_midnight.day == dt_now.day, "零點日期應與原時間相同"
        assert dt_midnight.hour == 0, "零點之時數應為 0"
        assert dt_midnight.minute == 0, "零點之分鐘應為 0"
        assert dt_midnight.second == 0, "零點之秒數應為 0"
        assert dt_midnight.microsecond == 0, "零點之微秒應為 0"

        # midnight 必須小於等於 now，且差距小於 86400 秒（一天之內）
        assert midnight <= now, "零點時間戳記應小於等於原時間"
        assert now - midnight < 86400.0, "原時間距當天零點應小於 24 小時"
