"""格式化函式測試。

驗證 monitor.fmt 模組中數值與倒數時間的字串格式化邏輯：
- format_tokens：小於 1 萬千分位、1 萬至 1 億萬位千分位四捨五入、1 億以上小數一位、0 與負數處理。
- format_countdown：已重置（<=0）、未滿 1 小時無條件進位至分、滿 1 小時整點與非整點、滿 1 天整天與非整天。
- format_clock：本機時間今天與跨日格式。
"""

from __future__ import annotations

import datetime
import pytest

from monitor.fmt import format_tokens, format_countdown, format_clock


# ---------------------------------------------------------------------------
# format_tokens 測試
# ---------------------------------------------------------------------------


class TestFormatTokens:
    """測試 token 數量格式化。"""

    def test_zero(self) -> None:
        """0 應格式化為 '0'。"""
        assert format_tokens(0) == "0", "0 應回傳 '0'"

    def test_negative(self) -> None:
        """負數應視為 0 回傳 '0'。"""
        assert format_tokens(-1) == "0", "-1 應回傳 '0'"
        assert format_tokens(-100) == "0", "-100 應回傳 '0'"
        assert format_tokens(-999999) == "0", "較大負數應回傳 '0'"

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (1, "1"),
            (12, "12"),
            (123, "123"),
            (999, "999"),
            (1_000, "1,000"),
            (9_876, "9,876"),
            (9_999, "9,999"),
        ],
    )
    def test_under_ten_thousand(self, n: int, expected: str) -> None:
        """小於 1 萬應顯示千分位整數。"""
        assert format_tokens(n) == expected, f"{n} 格式化結果與預期不符"

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (10_000, "1 萬"),
            (14_999, "1 萬"),
            (15_000, "2 萬"),
            (24_999, "2 萬"),
            (25_000, "3 萬"),
            (99_999, "10 萬"),
            (100_000, "10 萬"),
            (1_234_567, "123 萬"),
            (15_000_000, "1,500 萬"),
            (26_396_605, "2,640 萬"),
            (99_949_999, "9,995 萬"),
        ],
    )
    def test_ten_thousand_to_one_hundred_million(self, n: int, expected: str) -> None:
        """1 萬到 1 億以下應以萬為單位取整數並四捨五入、加千分位。"""
        assert format_tokens(n) == expected, f"{n} 萬位格式化結果與預期不符"

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (100_000_000, "1.0 億"),
            (123_456_789, "1.2 億"),
            (195_000_000, "2.0 億"),
            (250_000_000, "2.5 億"),
            (1_000_000_000, "10.0 億"),
        ],
    )
    def test_over_one_hundred_million(self, n: int, expected: str) -> None:
        """1 億以上應以億為單位，保留小數一位。"""
        assert format_tokens(n) == expected, f"{n} 億位格式化結果與預期不符"


# ---------------------------------------------------------------------------
# format_countdown 測試
# ---------------------------------------------------------------------------


class TestFormatCountdown:
    """測試倒數時間格式化。"""

    @pytest.mark.parametrize("seconds", [0.0, -0.1, -1.0, -100.0])
    def test_reset_when_non_positive(self, seconds: float) -> None:
        """小於等於 0 應回傳 '已重置'。"""
        assert format_countdown(seconds) == "已重置", f"{seconds} 秒應顯示 '已重置'"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.1, "1 分"),
            (1.0, "1 分"),
            (30.0, "1 分"),
            (59.0, "1 分"),
            (60.0, "1 分"),
            (60.1, "2 分"),
            (61.0, "2 分"),
            (119.0, "2 分"),
            (120.0, "2 分"),
            (2700.0, "45 分"),
            (3540.0, "59 分"),
            (3599.0, "60 分"),
        ],
    )
    def test_under_one_hour(self, seconds: float, expected: str) -> None:
        """未滿 1 小時應無條件進位至分，最少為 '1 分'。"""
        assert format_countdown(seconds) == expected, f"{seconds} 秒未滿 1 小時格式化錯誤"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (3600.0, "1 小時"),
            (3 * 3600.0, "3 小時"),
            (10 * 3600.0, "10 小時"),
            (23 * 3600.0, "23 小時"),
        ],
    )
    def test_whole_hours(self, seconds: float, expected: str) -> None:
        """滿 1 小時但未滿 1 天且剛好整點，應顯示 'N 小時'。"""
        assert format_countdown(seconds) == expected, f"{seconds} 秒整點格式化錯誤"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (3660.0, "1 小時 1 分"),
            (2 * 3600.0 + 14 * 60.0, "2 小時 14 分"),
            (5 * 3600.0 + 30 * 60.0, "5 小時 30 分"),
            (23 * 3600.0 + 59 * 60.0, "23 小時 59 分"),
        ],
    )
    def test_hours_and_minutes(self, seconds: float, expected: str) -> None:
        """滿 1 小時但未滿 1 天且非整點，應顯示 'N 小時 M 分'。"""
        assert format_countdown(seconds) == expected, f"{seconds} 秒小時加分鐘格式化錯誤"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (86400.0, "1 天"),
            (2 * 86400.0, "2 天"),
            (7 * 86400.0, "7 天"),
        ],
    )
    def test_whole_days(self, seconds: float, expected: str) -> None:
        """滿 1 天且剛好整天，應顯示 'N 天'。"""
        assert format_countdown(seconds) == expected, f"{seconds} 秒整天格式化錯誤"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (86400.0 + 3600.0, "1 天 1 小時"),
            (3 * 86400.0 + 4 * 3600.0, "3 天 4 小時"),
            (6 * 86400.0 + 23 * 3600.0, "6 天 23 小時"),
        ],
    )
    def test_days_and_hours(self, seconds: float, expected: str) -> None:
        """滿 1 天且非整天，應顯示 'N 天 M 小時'。"""
        assert format_countdown(seconds) == expected, f"{seconds} 秒天加小時格式化錯誤"


# ---------------------------------------------------------------------------
# format_clock 測試
# ---------------------------------------------------------------------------


class TestFormatClock:
    """測試時間戳記時鐘顯示格式。"""

    def test_today_format(self) -> None:
        """若是今天的時間，應顯示 'HH:MM'。"""
        now_dt = datetime.datetime.now()
        now_ts = now_dt.timestamp()
        expected_str = now_dt.strftime("%H:%M")
        assert format_clock(now_ts) == expected_str, "當天時間應只顯示時與分 'HH:MM'"

    def test_not_today_format(self) -> None:
        """若不是今天的時間，應顯示 'MM/DD HH:MM'。"""
        past_dt = datetime.datetime.now() - datetime.timedelta(days=2)
        past_ts = past_dt.timestamp()
        expected_str = past_dt.strftime("%m/%d %H:%M")
        assert format_clock(past_ts) == expected_str, "非當天時間應顯示月日與時分 'MM/DD HH:MM'"
