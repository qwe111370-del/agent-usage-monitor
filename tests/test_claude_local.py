"""Claude 本機日誌讀取與解析測試。

驗證 monitor.sources.claude_local 模組之功能：
- parse_line：合格 assistant 行解析、非 assistant 行排除、無 usage 排除、JSON 格式損毀處理、缺欄位預設為 0、去重鍵 (message.id, requestId) 抽取。
- ClaudeLogReader：
  - 跨檔案去重（相同的 message.id + requestId 跨檔案僅計入一次）。
  - 增量讀取（寫入兩行 refresh，再追加一行 refresh，總計 3 筆且不重複計算）。
  - 半行處理（寫入一行加未換行的半行，refresh 僅計 1 筆；補齊換行後再 refresh 正確讀入第 2 筆）。
  - 檔案變小時整檔重讀（覆寫為較短內容時，清除該檔舊事件並重新讀入新事件）。
  - 排除超過 keep_days 的過期事件。
  - events 屬性依時間戳記排序。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitor.model import TokenCounts, UsageEvent
from monitor.sources.claude_local import ClaudeLogReader, parse_line


# ---------------------------------------------------------------------------
# 輔助函式：建立 Claude JSONL 行
# ---------------------------------------------------------------------------


def make_claude_json(
    ts_iso: str = "2026-11-03T12:00:00Z",
    msg_id: str | None = "msg_01",
    req_id: str | None = "req_01",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 20,
    cache_write: int = 10,
    type_str: str = "assistant",
    has_usage: bool = True,
    usage_fields: dict | None = None,
) -> str:
    """產生測試用的單行 Claude JSON 字串。"""
    obj: dict = {"type": type_str, "timestamp": ts_iso}
    if msg_id is not None or has_usage:
        msg_obj: dict = {}
        if msg_id is not None:
            msg_obj["id"] = msg_id
        if has_usage:
            if usage_fields is not None:
                msg_obj["usage"] = usage_fields
            else:
                msg_obj["usage"] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                }
        obj["message"] = msg_obj
    if req_id is not None:
        obj["requestId"] = req_id
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# parse_line 測試
# ---------------------------------------------------------------------------


class TestClaudeParseLine:
    """測試 Claude 單行 JSON 解析。"""

    def test_valid_assistant_line(self) -> None:
        """合格的 assistant 行應成功解析出去重鍵與 UsageEvent。"""
        line = make_claude_json(
            ts_iso="2026-11-03T12:00:00Z",
            msg_id="msg_abc",
            req_id="req_xyz",
            input_tokens=100,
            output_tokens=50,
            cache_read=20,
            cache_write=10,
        )
        res = parse_line(line)
        assert res is not None, "合格行不應回傳 None"
        dedup_key, event = res

        assert dedup_key == ("msg_abc", "req_xyz"), "去重鍵應為 (message.id, requestId)"
        assert isinstance(event, UsageEvent), "應回傳 UsageEvent 物件"
        assert event.counts == TokenCounts(
            input=100,
            output=50,
            cache_read=20,
            cache_write=10,
        ), "token 數量換算不正確"
        # 驗證時間戳記正確解析（ISO 8601 到 epoch 秒）
        assert event.ts > 0, "時間戳記應轉換為大於 0 的 epoch 秒"

    def test_non_assistant_line_ignored(self) -> None:
        """非 assistant 類型（例如 user 或 tool_result）應回傳 None。"""
        user_line = make_claude_json(type_str="user")
        assert parse_line(user_line) is None, "user 類型應被忽略回傳 None"

        system_line = make_claude_json(type_str="system")
        assert parse_line(system_line) is None, "system 類型應被忽略回傳 None"

    def test_missing_usage_ignored(self) -> None:
        """若沒有 message.usage，應回傳 None。"""
        line_no_usage = make_claude_json(has_usage=False)
        assert parse_line(line_no_usage) is None, "缺少 usage 欄位時應回傳 None"

    def test_corrupted_json(self) -> None:
        """損毀的 JSON 字串應安全回傳 None，不得拋出例外。"""
        assert parse_line("not a valid json") is None, "無效字串應回傳 None"
        assert parse_line('{"type": "assistant", "incomplete": ') is None, "不完整 JSON 應回傳 None"
        assert parse_line("") is None, "空字串應回傳 None"

    def test_missing_usage_fields_default_to_zero(self) -> None:
        """usage 內部缺少部分欄位時，應視為 0。"""
        # 僅提供 input_tokens，其餘缺少
        line = make_claude_json(usage_fields={"input_tokens": 42})
        res = parse_line(line)
        assert res is not None, "部分缺少欄位仍為合法行"
        _, event = res
        assert event.counts.input == 42, "提供的 input_tokens 應正確解析"
        assert event.counts.output == 0, "缺少的 output_tokens 應預設為 0"
        assert event.counts.cache_read == 0, "缺少的 cache_read 應預設為 0"
        assert event.counts.cache_write == 0, "缺少的 cache_write 應預設為 0"

    def test_no_dedup_key_when_both_ids_missing(self) -> None:
        """當 message.id 與 requestId 都不存在時，去重鍵應為 None（不去重）。"""
        line = make_claude_json(msg_id=None, req_id=None, input_tokens=15)
        res = parse_line(line)
        assert res is not None, "無 id 行仍為合法事件"
        dedup_key, event = res
        assert dedup_key is None, "兩者皆無時去重鍵應為 None"
        assert event.counts.input == 15


# ---------------------------------------------------------------------------
# ClaudeLogReader 測試
# ---------------------------------------------------------------------------


class TestClaudeLogReader:
    """測試 ClaudeLogReader 本機日誌掃描與增量讀取。"""

    def test_cross_file_deduplication(self, tmp_path: Path) -> None:
        """驗證相同去重鍵跨檔案時只計入一次。"""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        dir_a = projects_dir / "proj_a"
        dir_b = projects_dir / "proj_b" / "subagents"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        file_a = dir_a / "chat1.jsonl"
        file_b = dir_b / "sub1.jsonl"

        # 兩檔包含相同的 (msg_dup, req_dup)
        line_dup = make_claude_json(
            ts_iso="2026-11-03T10:00:00Z",
            msg_id="msg_dup",
            req_id="req_dup",
            input_tokens=100,
        )
        line_a = make_claude_json(
            ts_iso="2026-11-03T10:01:00Z",
            msg_id="msg_unique_a",
            req_id="req_unique_a",
            input_tokens=200,
        )
        line_b = make_claude_json(
            ts_iso="2026-11-03T10:02:00Z",
            msg_id="msg_unique_b",
            req_id="req_unique_b",
            input_tokens=300,
        )

        file_a.write_text(f"{line_dup}\n{line_a}\n", encoding="utf-8")
        file_b.write_text(f"{line_dup}\n{line_b}\n", encoding="utf-8")

        reader = ClaudeLogReader(projects_dir=projects_dir, keep_days=8)
        # 固定現在時間（2026-11-03 12:00:00Z 相當於約 1793707200）
        now_ts = 1793707200.0
        reader.refresh(now=now_ts)

        # 總共應有 3 筆，重複的一筆被去重
        events = reader.events
        assert len(events) == 3, "跨檔重複鍵應被去重，總筆數應為 3"
        total_input = sum(e.counts.input for e in events)
        assert total_input == 600, "總 input 應為 100 + 200 + 300 = 600"

    def test_incremental_read(self, tmp_path: Path) -> None:
        """先寫兩行 refresh，再追加一行 refresh，總數正確且不重複計算。"""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        file_path = projects_dir / "session.jsonl"

        line1 = make_claude_json(
            ts_iso="2026-11-03T10:00:00Z",
            msg_id="m1",
            req_id="r1",
            input_tokens=10,
        )
        line2 = make_claude_json(
            ts_iso="2026-11-03T10:05:00Z",
            msg_id="m2",
            req_id="r2",
            input_tokens=20,
        )

        file_path.write_text(f"{line1}\n{line2}\n", encoding="utf-8")

        reader = ClaudeLogReader(projects_dir=projects_dir, keep_days=8)
        now_ts = 1793707200.0
        reader.refresh(now=now_ts)

        assert len(reader.events) == 2, "第一次讀取應有 2 筆事件"
        assert sum(e.counts.input for e in reader.events) == 30

        # 追加第三行
        line3 = make_claude_json(
            ts_iso="2026-11-03T10:10:00Z",
            msg_id="m3",
            req_id="r3",
            input_tokens=30,
        )
        with file_path.open("a", encoding="utf-8") as f:
            f.write(f"{line3}\n")

        reader.refresh(now=now_ts)

        # 增量讀取後應有 3 筆事件，且前 2 筆不重複累計
        assert len(reader.events) == 3, "增量追加後事件總數應為 3"
        assert sum(e.counts.input for e in reader.events) == 60, "總 input 應為 10 + 20 + 30 = 60"

    def test_partial_line_handling(self, tmp_path: Path) -> None:
        """先寫一行加未換行的半行，refresh 僅計 1 行；補完換行後再 refresh 讀入第 2 行。"""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        file_path = projects_dir / "session.jsonl"

        line1 = make_claude_json(
            ts_iso="2026-11-03T10:00:00Z",
            msg_id="m1",
            req_id="r1",
            input_tokens=10,
        )
        line2 = make_claude_json(
            ts_iso="2026-11-03T10:05:00Z",
            msg_id="m2",
            req_id="r2",
            input_tokens=20,
        )

        # 寫入第一行（含換行）以及第二行前半段（不含換行）
        split_idx = len(line2) // 2
        part1 = line2[:split_idx]
        part2 = line2[split_idx:]

        file_path.write_text(f"{line1}\n{part1}", encoding="utf-8")

        reader = ClaudeLogReader(projects_dir=projects_dir, keep_days=8)
        now_ts = 1793707200.0
        reader.refresh(now=now_ts)

        assert len(reader.events) == 1, "半行未結束前不應計入事件"
        assert reader.events[0].counts.input == 10

        # 補齊第二行並換行
        with file_path.open("a", encoding="utf-8") as f:
            f.write(f"{part2}\n")

        reader.refresh(now=now_ts)

        assert len(reader.events) == 2, "半行補齊後應成功解析為第 2 筆事件"
        assert sum(e.counts.input for e in reader.events) == 30

    def test_file_shrink_rereads_entire_file(self, tmp_path: Path) -> None:
        """檔案變小時應清空該檔舊事件並整檔重新讀取。"""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        file_path = projects_dir / "session.jsonl"

        line1 = make_claude_json(ts_iso="2026-11-03T10:00:00Z", msg_id="m1", input_tokens=10)
        line2 = make_claude_json(ts_iso="2026-11-03T10:01:00Z", msg_id="m2", input_tokens=20)
        line3 = make_claude_json(ts_iso="2026-11-03T10:02:00Z", msg_id="m3", input_tokens=30)
        file_path.write_text(f"{line1}\n{line2}\n{line3}\n", encoding="utf-8")

        reader = ClaudeLogReader(projects_dir=projects_dir, keep_days=8)
        now_ts = 1793707200.0
        reader.refresh(now=now_ts)
        assert len(reader.events) == 3, "初始應有 3 筆事件"

        # 將檔案覆寫為較短的單行（檔案大小縮小）
        line_new = make_claude_json(ts_iso="2026-11-03T11:00:00Z", msg_id="m_new", input_tokens=99)
        file_path.write_text(f"{line_new}\n", encoding="utf-8")

        reader.refresh(now=now_ts)

        # 檔案變小重讀，舊 3 筆應被清除，只剩新 1 筆
        assert len(reader.events) == 1, "檔案變小時應移除該檔舊事件並重讀"
        assert reader.events[0].counts.input == 99

    def test_events_older_than_keep_days_excluded(self, tmp_path: Path) -> None:
        """超過 keep_days 的歷史事件應自 events 中排除。"""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        file_path = projects_dir / "session.jsonl"

        # now_ts 設定為基準點
        now_ts = 1700000000.0
        # 10 天前（超過 keep_days=8）
        ts_old_iso = "2023-11-04T22:13:20Z"  # 1700000000 - 10 * 86400 = 1699136000
        # 2 天前（在 8 天內）
        ts_recent_iso = "2023-11-12T22:13:20Z"  # 1700000000 - 2 * 86400 = 1699827200

        line_old = make_claude_json(ts_iso=ts_old_iso, msg_id="m_old", input_tokens=100)
        line_recent = make_claude_json(ts_iso=ts_recent_iso, msg_id="m_recent", input_tokens=200)

        file_path.write_text(f"{line_old}\n{line_recent}\n", encoding="utf-8")

        reader = ClaudeLogReader(projects_dir=projects_dir, keep_days=8)
        reader.refresh(now=now_ts)

        assert len(reader.events) == 1, "超過 8 天的事件應被排除"
        assert reader.events[0].counts.input == 200, "僅保留 8 天內之新事件"

    def test_events_sorted_by_timestamp(self, tmp_path: Path) -> None:
        """events 屬性回傳的事件列表必須依時間戳記由小至大排序。"""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        file_path = projects_dir / "session.jsonl"

        # 刻意將時間倒序寫入
        line1 = make_claude_json(ts_iso="2026-11-03T12:00:00Z", msg_id="m1")
        line2 = make_claude_json(ts_iso="2026-11-03T10:00:00Z", msg_id="m2")
        line3 = make_claude_json(ts_iso="2026-11-03T11:00:00Z", msg_id="m3")
        file_path.write_text(f"{line1}\n{line2}\n{line3}\n", encoding="utf-8")

        reader = ClaudeLogReader(projects_dir=projects_dir, keep_days=8)
        reader.refresh(now=1793707200.0)

        ts_list = [e.ts for e in reader.events]
        assert ts_list == sorted(ts_list), "events 必須按時間由小到大排序"
