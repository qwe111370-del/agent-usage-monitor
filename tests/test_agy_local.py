"""Antigravity (agy) 本機 SQLite 對話資料庫與 Protobuf 解析測試。

驗證 monitor.sources.agy_local 模組之功能：
- 通用 Protobuf 解碼：
  - decode_usage：從 field 1 -> field 4 中解析 varint（field 2=input, field 3=output, field 5=cache_read, field 9=thinking），output 須為 field 3 + field 9。
  - decode_step_time：從 field 1 -> field 1 中解析 epoch 秒。
  - wire type 0, 1, 2, 5 支援與跳過未知欄位。
  - field 1、4 重複出現時取第一個。
  - 格式損毀或長度越界時安全回傳 None，不得拋出例外。
- AgyConversationReader：
  - 讀取 SQLite .db（唯讀模式）。
  - 單一對話資料庫所有記錄合成「一個」UsageEvent。
  - 事件時間取該對話中所有 step 時間之最大值；無 step 時間時退回使用資料庫檔案 mtime。
"""

from __future__ import annotations

import os
import sqlite3
import struct
from pathlib import Path

import pytest

from monitor.model import TokenCounts, UsageEvent
from monitor.sources.agy_local import (
    AgyConversationReader,
    decode_step_time,
    decode_usage,
)


# ---------------------------------------------------------------------------
# Protobuf 編碼輔助函式（測試用）
# ---------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """編碼整數為 Protobuf varint 位元組串。"""
    buf = bytearray()
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def encode_field(field_number: int, wire_type: int, payload: bytes | int) -> bytes:
    """編碼單一 Protobuf 欄位（tag + 資料）。支援 wire type 0, 1, 2, 5。"""
    tag = encode_varint((field_number << 3) | wire_type)
    if wire_type == 0:
        assert isinstance(payload, int)
        return tag + encode_varint(payload)
    elif wire_type == 1:  # 64-bit fixed
        assert isinstance(payload, int)
        return tag + struct.pack("<Q", payload)
    elif wire_type == 2:  # Length-delimited
        assert isinstance(payload, (bytes, bytearray))
        return tag + encode_varint(len(payload)) + bytes(payload)
    elif wire_type == 5:  # 32-bit fixed
        assert isinstance(payload, int)
        return tag + struct.pack("<I", payload)
    raise ValueError(f"不支援的 wire type: {wire_type}")


def make_agy_usage_blob(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 20,
    thinking_tokens: int = 10,
    extra_fixed_fields: bool = False,
) -> bytes:
    """依 SPEC 5.1 規範建立 gen_metadata.data 之 Protobuf blob。

    路徑：field 1 (message) -> field 4 (message)
    內含：
      field 2 (varint): input
      field 3 (varint): output
      field 5 (varint): cache_read
      field 9 (varint): thinking
    """
    inner = (
        encode_field(2, 0, input_tokens)
        + encode_field(3, 0, output_tokens)
        + encode_field(5, 0, cache_read)
        + encode_field(9, 0, thinking_tokens)
    )
    if extra_fixed_fields:
        # 加入 wire type 1 (fixed64) 與 wire type 5 (fixed32) 測試解析器跳過能力
        inner += encode_field(15, 1, 0x1122334455667788)
        inner += encode_field(16, 5, 0xAABBCCDD)

    outer = encode_field(4, 2, inner)
    root = encode_field(1, 2, outer)
    return root


def make_agy_step_blob(epoch_seconds: int) -> bytes:
    """依 SPEC 5.1 規範建立 steps.metadata 之 Protobuf blob。

    路徑：field 1 (message) -> field 1 (varint): epoch 秒
    """
    inner = encode_field(1, 0, epoch_seconds)
    root = encode_field(1, 2, inner)
    return root


# ---------------------------------------------------------------------------
# decode_usage 與 decode_step_time 測試
# ---------------------------------------------------------------------------


class TestAgyProtobufDecoders:
    """測試 Protobuf 解碼純函式。"""

    def test_decode_usage_standard_and_thinking_addition(self) -> None:
        """驗證 decode_usage：output 必須是 欄位 3 (output) + 欄位 9 (thinking)。"""
        # SPEC 實測範例：input=199976, output=12579, thinking=8054, cache_read=1232695
        blob = make_agy_usage_blob(
            input_tokens=199_976,
            output_tokens=12_579,
            cache_read=1_232_695,
            thinking_tokens=8_054,
        )
        counts = decode_usage(blob)
        assert counts is not None, "正常 blob 應成功解碼"
        assert counts.input == 199_976, "input 應為欄位 2"
        # output = 12579 + 8054 = 20633
        assert counts.output == 20_633, "output 應為欄位 3 + 欄位 9 (含 thinking)"
        assert counts.cache_read == 1_232_695, "cache_read 應為欄位 5"
        assert counts.cache_write == 0, "agy cache_write 應為 0"

    def test_decode_usage_with_wire_types_1_and_5(self) -> None:
        """驗證解析器能正常跳過 wire type 1 (fixed64) 與 wire type 5 (fixed32)。"""
        blob = make_agy_usage_blob(
            input_tokens=500,
            output_tokens=100,
            cache_read=50,
            thinking_tokens=25,
            extra_fixed_fields=True,
        )
        counts = decode_usage(blob)
        assert counts is not None, "含有 fixed64/fixed32 之 blob 應正常略過未知欄位"
        assert counts.input == 500
        assert counts.output == 125

    def test_decode_usage_first_field_wins_on_duplicate(self) -> None:
        """欄位 1 或 4 重複出現時，應取第一個出現的訊息。"""
        # 建立兩個 field 4：第一個 input=111，第二個 input=999
        inner1 = encode_field(2, 0, 111) + encode_field(3, 0, 10)
        inner2 = encode_field(2, 0, 999) + encode_field(3, 0, 90)
        outer = encode_field(4, 2, inner1) + encode_field(4, 2, inner2)
        root = encode_field(1, 2, outer)

        counts = decode_usage(root)
        assert counts is not None
        assert counts.input == 111, "欄位 4 重複時應取第一個"

    @pytest.mark.parametrize(
        "corrupted_blob",
        [
            b"",                                    # 空 bytes
            b"\x80",                                # 結尾截斷未完的 varint
            b"\x0a\x10\x01\x02",                    # length-delimited 長度超出實際 bytes
            b"\x0f\x01",                            # wire type 為 7（無效 wire type）
            b"\xff\xff\xff\xff\xff\xff\xff\xff",    # 隨機雜訊 bytes
        ],
    )
    def test_decode_usage_corrupted_returns_none(self, corrupted_blob: bytes) -> None:
        """損毀的 bytes 應回傳 None，且絕對不得拋出例外。"""
        try:
            res = decode_usage(corrupted_blob)
            assert res is None, "損毀 bytes 應回傳 None"
        except Exception as e:
            pytest.fail(f"decode_usage 拋出未預期例外: {e}")

    def test_decode_step_time_standard(self) -> None:
        """驗證 decode_step_time 解析 field 1 -> field 1 之 epoch 秒。"""
        blob = make_agy_step_blob(1_700_000_123)
        ts = decode_step_time(blob)
        assert ts is not None, "合法 step blob 應回傳時間戳記"
        assert ts == 1_700_000_123.0 or ts == 1_700_000_123

    @pytest.mark.parametrize(
        "corrupted_blob",
        [
            b"",
            b"\x80",
            b"\x0a\x20\x01",
            b"\x07",
        ],
    )
    def test_decode_step_time_corrupted_returns_none(self, corrupted_blob: bytes) -> None:
        """損毀的 step metadata 應回傳 None 且不丟例外。"""
        try:
            res = decode_step_time(corrupted_blob)
            assert res is None, "損毀 step blob 應回傳 None"
        except Exception as e:
            pytest.fail(f"decode_step_time 拋出未預期例外: {e}")


# ---------------------------------------------------------------------------
# AgyConversationReader 測試
# ---------------------------------------------------------------------------


class TestAgyConversationReader:
    """測試 AgyConversationReader SQLite 讀取與單一對話事件合成。"""

    def test_single_conversation_merged_into_one_event_with_max_step_time(
        self, tmp_path: Path
    ) -> None:
        """驗證整個對話合成一個 UsageEvent，時間取最大 step 時間，用量為全部加總。"""
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir(parents=True)
        db_file = conv_dir / "conv_01.db"

        # 建立 SQLite 資料表
        with sqlite3.connect(db_file) as conn:
            conn.execute("CREATE TABLE gen_metadata (data BLOB);")
            conn.execute("CREATE TABLE steps (metadata BLOB);")

            # 插入兩筆用量記錄
            # 筆 1: input=100, output=50, thinking=10, cache_read=20 -> output=60
            b1 = make_agy_usage_blob(100, 50, 20, 10)
            # 筆 2: input=200, output=30, thinking=20, cache_read=40 -> output=50
            b2 = make_agy_usage_blob(200, 30, 40, 20)
            conn.execute("INSERT INTO gen_metadata (data) VALUES (?);", (b1,))
            conn.execute("INSERT INTO gen_metadata (data) VALUES (?);", (b2,))

            # 插入兩筆 step 時間：1700001000 與 1700005000
            s1 = make_agy_step_blob(1_700_001_000)
            s2 = make_agy_step_blob(1_700_005_000)
            conn.execute("INSERT INTO steps (metadata) VALUES (?);", (s1,))
            conn.execute("INSERT INTO steps (metadata) VALUES (?);", (s2,))

        reader = AgyConversationReader(conversations_dir=conv_dir)
        reader.refresh()

        events = reader.events
        assert len(events) == 1, "一個對話 db 應合成恰好一個 UsageEvent"

        event = events[0]
        # 用量累加：input = 100 + 200 = 300, output = 60 + 50 = 110, cache_read = 20 + 40 = 60
        assert event.counts.input == 300, "對話內 input 應累加"
        assert event.counts.output == 110, "對話內 output (含 thinking) 應累加"
        assert event.counts.cache_read == 60, "對話內 cache_read 應累加"
        assert event.counts.cache_write == 0

        # 時間取最大 step 時間 1700005000
        assert event.ts == 1_700_005_000.0, "事件時間戳記應取所有 step 時間之最大值"

    def test_fallback_to_mtime_when_no_steps(self, tmp_path: Path) -> None:
        """當 steps 資料表無記錄或無合法 step 時間時，事件時間應退回使用 db 檔案的 mtime。"""
        conv_dir = tmp_path / "conversations"
        conv_dir.mkdir(parents=True)
        db_file = conv_dir / "conv_nosteps.db"

        with sqlite3.connect(db_file) as conn:
            conn.execute("CREATE TABLE gen_metadata (data BLOB);")
            conn.execute("CREATE TABLE steps (metadata BLOB);")

            # 僅插入用量，steps 為空
            b1 = make_agy_usage_blob(150, 40, 10, 5)
            conn.execute("INSERT INTO gen_metadata (data) VALUES (?);", (b1,))

        # 固定檔案 mtime
        expected_mtime = 1_700_000_999.0
        os.utime(db_file, (expected_mtime, expected_mtime))

        reader = AgyConversationReader(conversations_dir=conv_dir)
        reader.refresh()

        events = reader.events
        assert len(events) == 1
        event = events[0]
        assert abs(event.ts - expected_mtime) < 1.0, "無 step 時間時應以 db 檔案 mtime 為事件時間"
