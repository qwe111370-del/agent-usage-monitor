"""Codex 本機日誌讀取與額度解析測試。

驗證 monitor.sources.codex_local 模組之功能：
- rate_limits_to_windows：
  - 300 分鐘轉換為 key "five_hour"、label "5 小時"。
  - 10080 分鐘轉換為 key "seven_day"、label "每週"。
  - primary 或 secondary 為 null 時視窗略過。
  - plan_type 正確解析與回傳。
- CodexLogReader：
  - 假 rollout jsonl 累計值差值換算（扣除 cached_input_tokens，轉換為 input, cache_read, output）。
  - 累計重設時負數差值視為 0。
  - info 為 null 的紀錄不計入用量。
  - latest_rate_limits 從跨檔案所有記錄中取時間戳記最新之 rate_limits。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitor.model import TokenCounts, UsageEvent
from monitor.sources.codex_local import CodexLogReader, rate_limits_to_windows


# ---------------------------------------------------------------------------
# 輔助函式：建立 Codex rollout JSONL 行
# ---------------------------------------------------------------------------


def make_codex_json(
    ts_iso: str = "2026-11-03T12:00:00Z",
    input_tokens: int = 1000,
    cached_tokens: int = 200,
    output_tokens: int = 300,
    rate_limits: dict | None = None,
    info_is_null: bool = False,
    event_type: str = "event_msg",
    payload_type: str = "token_count",
) -> str:
    """產生測試用的單行 Codex rollout 事件 JSON。"""
    payload: dict = {"type": payload_type}

    if info_is_null:
        payload["info"] = None
    else:
        payload["info"] = {
            "total_token_usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "output_tokens": output_tokens,
            }
        }

    if rate_limits is not None:
        payload["rate_limits"] = rate_limits

    return json.dumps({
        "type": event_type,
        "timestamp": ts_iso,
        "payload": payload,
    })


# ---------------------------------------------------------------------------
# rate_limits_to_windows 測試
# ---------------------------------------------------------------------------


class TestCodexRateLimitsToWindows:
    """測試 rate_limits_to_windows 額度視窗轉換純函式。"""

    def test_standard_windows_300_and_10080(self) -> None:
        """驗證 300 分鐘對應 '5 小時'、10080 分鐘對應 '每週'，並解析 plan_type。"""
        rl_data = {
            "plan_type": "plus",
            "primary": {
                "used_percent": 45.0,
                "window_minutes": 300,
                "resets_at": 1700000000.0,
            },
            "secondary": {
                "used_percent": 80.0,
                "window_minutes": 10080,
                "resets_at": 1700500000.0,
            },
        }

        plan, windows = rate_limits_to_windows(rl_data)
        assert plan == "plus", "plan_type 應解析為 'plus'"
        assert len(windows) == 2, "應有 primary 與 secondary 兩個視窗"

        w_primary = windows[0]
        assert w_primary.key == "five_hour", "300 分鐘 key 應為 five_hour"
        assert w_primary.label == "5 小時", "300 分鐘 label 應為 '5 小時'"
        assert w_primary.used_pct == 45.0
        assert w_primary.resets_at == 1700000000.0

        w_secondary = windows[1]
        assert w_secondary.key == "seven_day", "10080 分鐘 key 應為 seven_day"
        assert w_secondary.label == "每週", "10080 分鐘 label 應為 '每週'"
        assert w_secondary.used_pct == 80.0
        assert w_secondary.resets_at == 1700500000.0

    def test_null_window_skipped(self) -> None:
        """primary 或 secondary 為 null 時應自動略過該視窗。"""
        rl_primary_null = {
            "plan_type": "pro",
            "primary": None,
            "secondary": {
                "used_percent": 25.0,
                "window_minutes": 10080,
                "resets_at": 1700500000.0,
            },
        }
        plan, windows = rate_limits_to_windows(rl_primary_null)
        assert plan == "pro"
        assert len(windows) == 1, "primary 為 null 時只應回傳 secondary 視窗"
        assert windows[0].key == "seven_day"

        rl_secondary_null = {
            "plan_type": "team",
            "primary": {
                "used_percent": 10.0,
                "window_minutes": 300,
                "resets_at": 1700000000.0,
            },
            "secondary": None,
        }
        plan, windows = rate_limits_to_windows(rl_secondary_null)
        assert plan == "team"
        assert len(windows) == 1, "secondary 為 null 時只應回傳 primary 視窗"
        assert windows[0].key == "five_hour"

        rl_both_null = {"plan_type": "free", "primary": None, "secondary": None}
        plan, windows = rate_limits_to_windows(rl_both_null)
        assert plan == "free"
        assert len(windows) == 0, "兩者皆為 null 時 windows 應為空清單"

    def test_missing_plan_type(self) -> None:
        """若 rate_limits 缺少 plan_type 欄位，應回傳 None。"""
        rl_data = {
            "primary": {
                "used_percent": 15.0,
                "window_minutes": 300,
                "resets_at": 1700000000.0,
            }
        }
        plan, windows = rate_limits_to_windows(rl_data)
        assert plan is None, "未提供 plan_type 時應回傳 None"
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# CodexLogReader 測試
# ---------------------------------------------------------------------------


class TestCodexLogReader:
    """測試 CodexLogReader 累計差值計算與額度擷取。"""

    def test_cumulative_delta_and_cached_deduction(self, tmp_path: Path) -> None:
        """驗證同檔累計值差值換算，且 input 須扣除 cached_input_tokens。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        rollout_file = sessions_dir / "rollout-2026-11-03-01.jsonl"

        # 第一筆累計：input_tokens=1000（含 cached=200）, output_tokens=300
        # 換算：input = 1000 - 200 = 800, cache_read = 200, output = 300
        line1 = make_codex_json(
            ts_iso="2026-11-03T10:00:00Z",
            input_tokens=1000,
            cached_tokens=200,
            output_tokens=300,
        )

        # 第二筆累計：input_tokens=2500（含 cached=600）, output_tokens=700
        # 差值：Δinput_tokens = 1500, Δcached = 400, Δoutput = 400
        # 換算：input = 1500 - 400 = 1100, cache_read = 400, output = 400
        line2 = make_codex_json(
            ts_iso="2026-11-03T10:05:00Z",
            input_tokens=2500,
            cached_tokens=600,
            output_tokens=700,
        )

        rollout_file.write_text(f"{line1}\n{line2}\n", encoding="utf-8")

        reader = CodexLogReader(roots=[tmp_path], keep_days=8)
        now_ts = 1793707200.0
        reader.refresh(now=now_ts)

        events = reader.events
        assert len(events) == 2, "應解析出 2 筆事件"

        # 檢驗第一筆（第一筆直接用累計值）
        e1 = events[0]
        assert e1.counts.input == 800, "第一筆 input 應扣除 cached (1000 - 200 = 800)"
        assert e1.counts.cache_read == 200, "第一筆 cache_read 應為 cached (200)"
        assert e1.counts.output == 300, "第一筆 output 應為 300"
        assert e1.counts.cache_write == 0

        # 檢驗第二筆（差值換算）
        e2 = events[1]
        assert e2.counts.input == 1100, "第二筆 input 應為 Δinput - Δcached (1500 - 400 = 1100)"
        assert e2.counts.cache_read == 400, "第二筆 cache_read 應為 Δcached (400)"
        assert e2.counts.output == 400, "第二筆 output 應為 Δoutput (400)"
        assert e2.counts.cache_write == 0

    def test_cumulative_reset_negative_delta_treated_as_zero(self, tmp_path: Path) -> None:
        """累計值被重設時（新累計值小於前次），負數差值應視為 0。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        rollout_file = sessions_dir / "rollout-reset.jsonl"

        # 第一筆累計值很大
        line1 = make_codex_json(
            ts_iso="2026-11-03T10:00:00Z",
            input_tokens=5000,
            cached_tokens=1000,
            output_tokens=2000,
        )
        # 第二筆累計值重設變小（例如 session 重啟）
        line2 = make_codex_json(
            ts_iso="2026-11-03T10:05:00Z",
            input_tokens=100,
            cached_tokens=20,
            output_tokens=50,
        )

        rollout_file.write_text(f"{line1}\n{line2}\n", encoding="utf-8")

        reader = CodexLogReader(roots=[tmp_path], keep_days=8)
        now_ts = 1793707200.0
        reader.refresh(now=now_ts)

        events = reader.events
        assert len(events) == 2, "應有 2 筆事件"

        e2 = events[1]
        assert e2.counts.input == 0, "累計重設負數差值 input 應視為 0"
        assert e2.counts.output == 0, "累計重設負數差值 output 應視為 0"
        assert e2.counts.cache_read == 0, "累計重設負數差值 cache_read 應視為 0"
        assert e2.counts.cache_write == 0

    def test_null_info_does_not_count_as_usage(self, tmp_path: Path) -> None:
        """payload.info 為 null 的事件不計入用量，但仍可作為 rate_limits 來源。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        rollout_file = sessions_dir / "rollout-null-info.jsonl"

        rl = {
            "plan_type": "plus",
            "primary": {"used_percent": 30.0, "window_minutes": 300, "resets_at": 1700000000.0},
        }

        # 一筆 info 為 null 但帶有 rate_limits
        line_null_info = make_codex_json(
            ts_iso="2026-11-03T10:00:00Z",
            info_is_null=True,
            rate_limits=rl,
        )

        rollout_file.write_text(f"{line_null_info}\n", encoding="utf-8")

        reader = CodexLogReader(roots=[tmp_path], keep_days=8)
        reader.refresh(now=1793707200.0)

        # events 不應包含此筆
        assert len(reader.events) == 0, "info 為 null 的事件不應計入用量"

        # 但 latest_rate_limits 應能正確取得
        latest_rl = reader.latest_rate_limits
        assert latest_rl is not None, "info 為 null 的行仍應提供 rate_limits"
        ts, rl_result = latest_rl
        assert rl_result["plan_type"] == "plus"
        assert rl_result["primary"]["used_percent"] == 30.0

    def test_latest_rate_limits_picks_most_recent(self, tmp_path: Path) -> None:
        """跨檔案讀取時，latest_rate_limits 應挑選 timestamp 最新的一筆。"""
        sessions_dir = tmp_path / "sessions"
        archived_dir = tmp_path / "archived_sessions"
        sessions_dir.mkdir(parents=True)
        archived_dir.mkdir(parents=True)

        file1 = sessions_dir / "rollout-old.jsonl"
        file2 = archived_dir / "rollout-new.jsonl"

        rl_older = {
            "plan_type": "plus",
            "primary": {"used_percent": 20.0, "window_minutes": 300, "resets_at": 1700000000.0},
        }
        rl_newer = {
            "plan_type": "plus",
            "primary": {"used_percent": 85.0, "window_minutes": 300, "resets_at": 1700010000.0},
        }

        # file1 時間較早
        line1 = make_codex_json(ts_iso="2026-11-03T10:00:00Z", rate_limits=rl_older)
        # file2 時間較晚
        line2 = make_codex_json(ts_iso="2026-11-03T11:00:00Z", rate_limits=rl_newer)

        file1.write_text(f"{line1}\n", encoding="utf-8")
        file2.write_text(f"{line2}\n", encoding="utf-8")

        reader = CodexLogReader(roots=[tmp_path], keep_days=8)
        reader.refresh(now=1793707200.0)

        latest_rl = reader.latest_rate_limits
        assert latest_rl is not None
        ts, rl_dict = latest_rl
        assert rl_dict["primary"]["used_percent"] == 85.0, "應取得 timestamp 最新之 rate_limits (85%)"
