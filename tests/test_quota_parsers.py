"""官方額度回應與憑證解析測試。

驗證各 Agent 官方額度與憑證相關純函式解析邏輯：
- Claude (claude_quota)：
  - parse_usage_response：
    - utilization 原值即為百分比，不得再乘以 100。
    - null 或缺少之視窗自動略過。
    - 5 小時、每週缺少時以 limits 陣列無 scope 元素後備補上。
    - 視窗順序（5 小時、每週、其他）。
  - parse_plan_usage_history：取 sample 中 t 最大且 u 非空之記錄，resets_at 為 None。
  - decrypt_v10：使用 cryptography AESGCM 驗證正常解密，開頭前綴非 v10、壞 base64 或損毀密文丟 ValueError。
  - pick_token：依權限優先順序（同時含 inference+profile > 僅 inference > 其他），同優先級取 expiresAt 最大者，過期與空 token 排除。
- Codex (codex_quota)：
  - parse_usage_response：limit_window_seconds 分類（>= 86400 為每週，其餘為 5 小時），缺少該欄位時 primary 預設 5 小時、secondary 預設每週。
- Antigravity (agy_quota)：
  - parse_quota_summary：remainingFraction 轉換為 used_pct（clamp 0~1），5h / weekly 轉換，名稱含 gemini 的 group 排最前面且 5h 排每週前面。
  - parse_available_models：最多取 6 個模型視窗，依剩餘比例由少到多排序。
  - extract_oauth_clients：從含有隨機雜訊位元組中以 regex 擷取符合規則之假 client_id 與 client_secret 配對。
"""

from __future__ import annotations

import base64
import os
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from monitor.model import QuotaWindow
from monitor.sources.agy_quota import (
    extract_oauth_clients,
    parse_available_models,
    parse_quota_summary,
)
from monitor.sources.claude_quota import (
    decrypt_v10,
    parse_plan_usage_history,
    parse_usage_response as parse_claude_usage,
    pick_token,
)
from monitor.sources.codex_quota import (
    parse_usage_response as parse_codex_usage,
)


# ===========================================================================
# Claude 額度與憑證解析測試
# ===========================================================================


class TestClaudeQuotaParsers:
    """測試 Claude 官方額度與 token 解密/選擇。"""

    def test_parse_usage_response_utilization_not_multiplied_and_ordering(self) -> None:
        """驗證 utilization 不再乘 100，且排序為 5 小時、每週、再來是其他。"""
        resp = {
            "seven_day_sonnet": {
                "utilization": 25.0,
                "resets_at": "2026-11-10T00:00:00Z",
            },
            "five_hour": {
                "utilization": 42.5,
                "resets_at": "2026-11-03T18:00:00Z",
            },
            "seven_day": {
                "utilization": 80.0,
                "resets_at": "2026-11-10T00:00:00Z",
            },
            "seven_day_opus": {
                "utilization": 15.0,
                "resets_at": "2026-11-10T00:00:00Z",
            },
        }

        windows = parse_claude_usage(resp)
        assert len(windows) == 4, "應解析出 4 個視窗"

        # 檢驗排序：5 小時 -> 每週 -> 其他 (opus / sonnet)
        assert windows[0].key == "five_hour", "第一順位應為 5 小時視窗"
        assert windows[0].used_pct == 42.5, "utilization 已經是百分比，不得再乘 100 變成 4250"
        assert windows[0].label == "5 小時"
        assert windows[0].window_seconds == 18000

        assert windows[1].key == "seven_day", "第二順位應為每週視窗"
        assert windows[1].used_pct == 80.0
        assert windows[1].label == "每週"
        assert windows[1].window_seconds == 604800

        # 其餘視窗包含 opus 與 sonnet
        other_keys = [w.key for w in windows[2:]]
        assert "seven_day_opus" in other_keys
        assert "seven_day_sonnet" in other_keys

    def test_parse_usage_response_null_windows_skipped(self) -> None:
        """欄位為 null 或缺少時應略過該視窗。"""
        resp = {
            "five_hour": None,
            "seven_day": {
                "utilization": 60.0,
                "resets_at": "2026-11-10T00:00:00Z",
            },
        }
        windows = parse_claude_usage(resp)
        assert len(windows) == 1, "five_hour 為 null 時應略過"
        assert windows[0].key == "seven_day"

    def test_parse_usage_response_limits_array_fallback(self) -> None:
        """當 five_hour 與 seven_day 缺少但有 limits 陣列時，以無 scope 元素補上。"""
        resp = {
            "limits": [
                {
                    "kind": "session",
                    "percent": 55.0,
                    "resets_at": "2026-11-03T18:00:00Z",
                },
                {
                    "kind": "weekly_all",
                    "utilization": 70.0,
                    "resets_at": "2026-11-10T00:00:00Z",
                },
                {
                    # 有 scope 的元素應被忽略
                    "kind": "session",
                    "scope": "custom_agent",
                    "percent": 99.0,
                },
            ]
        }
        windows = parse_claude_usage(resp)
        assert len(windows) == 2, "應從 limits 陣列後備產生 2 個無 scope 視窗"

        w_session = next(w for w in windows if w.key == "five_hour")
        assert w_session.used_pct == 55.0, "session kind 應對應 5 小時，優先取 percent"

        w_weekly = next(w for w in windows if w.key == "seven_day")
        assert w_weekly.used_pct == 70.0, "weekly_all kind 應對應每週，無 percent 時取 utilization"

    def test_parse_plan_usage_history_picks_latest_non_empty(self) -> None:
        """parse_plan_usage_history 應取 t 最大且 u 非空的 sample。"""
        history_data = {
            "version": 2,
            "samples": [
                {"t": 1700000000000, "org": "org_1", "u": {"fh": 20.0, "sd": 30.0}},
                {"t": 1700000080000, "org": "org_1", "u": {}},  # u 為空，應略過
                {"t": 1700000050000, "org": "org_1", "u": {"fh": 45.0, "sd": 65.0}},
            ],
        }

        res = parse_plan_usage_history(history_data)
        assert res is not None, "存在非空 u 時不應回傳 None"
        t_sec, windows = res

        # 最大非空 t 為 1700000050000 ms = 1700000050.0 s
        assert t_sec == 1700000050.0, "時間應為最大非空 sample 之秒數 (t/1000)"
        assert len(windows) == 2, "應轉換為 5 小時與每週兩個視窗"

        w_fh = next(w for w in windows if w.key == "five_hour")
        assert w_fh.used_pct == 45.0
        assert w_fh.resets_at is None, "快照視窗之 resets_at 應為 None"

        w_sd = next(w for w in windows if w.key == "seven_day")
        assert w_sd.used_pct == 65.0
        assert w_sd.resets_at is None

    def test_parse_plan_usage_history_empty_returns_none(self) -> None:
        """若 samples 為空或全為空 u，應回傳 None。"""
        assert parse_plan_usage_history({"version": 2, "samples": []}) is None
        assert parse_plan_usage_history({"version": 2, "samples": [{"t": 100, "u": {}}]}) is None

    def test_decrypt_v10_success_and_failures(self) -> None:
        """驗證 decrypt_v10 成功解密，前綴錯誤或損毀時丟出 ValueError。"""
        key = AESGCM.generate_key(bit_length=256)
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        plaintext = b'{"token": "secret_token_12345", "expiresAt": 1799999999000}'
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        # 合法格式：ASCII "v10" + 12 bytes nonce + ciphertext_with_tag
        valid_payload = b"v10" + nonce + ciphertext
        valid_b64 = base64.b64encode(valid_payload).decode("ascii")

        # 正常解密驗證
        decrypted = decrypt_v10(key, valid_b64)
        assert decrypted == plaintext, "decrypt_v10 應正確還原明文位元組串"

        # 前綴錯誤（如 v11 或無前綴）應丟 ValueError
        wrong_prefix_payload = b"v11" + nonce + ciphertext
        wrong_prefix_b64 = base64.b64encode(wrong_prefix_payload).decode("ascii")
        with pytest.raises(ValueError):
            decrypt_v10(key, wrong_prefix_b64)

        # 無前綴（直接是 nonce + ct）應丟 ValueError
        no_prefix_b64 = base64.b64encode(nonce + ciphertext).decode("ascii")
        with pytest.raises(ValueError):
            decrypt_v10(key, no_prefix_b64)

        # Base64 格式損毀應丟 ValueError
        with pytest.raises(ValueError):
            decrypt_v10(key, "!!!not_valid_base64!!!")

        # 密文內容或 tag 遭竄改應丟 ValueError
        tampered_payload = b"v10" + nonce + ciphertext[:-1] + b"\x00"
        tampered_b64 = base64.b64encode(tampered_payload).decode("ascii")
        with pytest.raises(ValueError):
            decrypt_v10(key, tampered_b64)

    def test_pick_token_priority_and_expiration(self) -> None:
        """驗證 pick_token 優先級規則與過期/空 token 排除。"""
        now_ms = 1_700_000_000_000.0

        entries = {
            # 優先級 1：同時含 user:inference 與 user:profile（較早過期）
            "inst1:user1:url1:user:inference,user:profile": {
                "token": "tok_p1_exp_soon",
                "expiresAt": now_ms + 10_000,
            },
            # 優先級 1：同時含 user:inference 與 user:profile（較晚過期） -> 應勝出！
            "inst2:user2:url2:user:inference,user:profile": {
                "token": "tok_p1_exp_later",
                "expiresAt": now_ms + 50_000,
            },
            # 優先級 1 但已過期 -> 排除
            "inst3:user3:url3:user:inference,user:profile": {
                "token": "tok_p1_expired",
                "expiresAt": now_ms - 1_000,
            },
            # 優先級 1 但 token 為空字串 -> 排除
            "inst4:user4:url4:user:inference,user:profile": {
                "token": "",
                "expiresAt": now_ms + 999_000,
            },
            # 優先級 2：僅含 user:inference（過期時間極長，但不應贏過優先級 1）
            "inst5:user5:url5:user:inference": {
                "token": "tok_p2_longest",
                "expiresAt": now_ms + 999_999,
            },
            # 優先級 3：其他 scope
            "inst6:user6:url6:user:read": {
                "token": "tok_p3",
                "expiresAt": now_ms + 999_999,
            },
        }

        chosen = pick_token(entries, now_ms=now_ms)
        assert chosen == "tok_p1_exp_later", "應選擇優先級 1 中 expiresAt 最大的有效 token"

    def test_pick_token_fallback_to_priority_2_and_none(self) -> None:
        """當無優先級 1 時挑選優先級 2，皆無或皆過期回傳 None。"""
        now_ms = 1_700_000_000_000.0
        entries = {
            "inst:u:url:user:inference": {
                "token": "tok_p2_valid",
                "expiresAt": now_ms + 5_000,
            },
            "inst:u:url:other": {
                "token": "tok_p3",
                "expiresAt": now_ms + 20_000,
            },
        }
        assert pick_token(entries, now_ms=now_ms) == "tok_p2_valid"

        # 全過期情況
        expired_entries = {
            "inst:u:url:user:inference,user:profile": {
                "token": "tok_exp",
                "expiresAt": now_ms - 100,
            }
        }
        assert pick_token(expired_entries, now_ms=now_ms) is None


# ===========================================================================
# Codex 額度解析測試
# ===========================================================================


class TestCodexQuotaParsers:
    """測試 Codex 即時額度回應解析。"""

    def test_parse_usage_response_explicit_seconds(self) -> None:
        """驗證以 limit_window_seconds 分類：>= 86400 為每週，其餘為 5 小時。"""
        resp = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 35.0,
                    "reset_at": 1700000000.0,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 75.0,
                    "reset_at": 1700500000.0,
                    "limit_window_seconds": 604800,
                },
            },
        }

        plan, windows = parse_codex_usage(resp)
        assert plan == "plus"
        assert len(windows) == 2

        # primary (18000s < 86400s) -> 5 小時
        assert windows[0].label == "5 小時"
        assert windows[0].used_pct == 35.0
        assert windows[0].resets_at == 1700000000.0

        # secondary (604800s >= 86400s) -> 每週
        assert windows[1].label == "每週"
        assert windows[1].used_pct == 75.0
        assert windows[1].resets_at == 1700500000.0

    def test_parse_usage_response_missing_limit_window_seconds_defaults(self) -> None:
        """缺少 limit_window_seconds 時：primary 預設 5 小時，secondary 預設每週。"""
        resp = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 20.0,
                    "reset_at": 1700000000.0,
                },
                "secondary_window": {
                    "used_percent": 50.0,
                    "reset_at": 1700500000.0,
                },
            },
        }

        plan, windows = parse_codex_usage(resp)
        assert plan == "pro"
        assert len(windows) == 2
        assert windows[0].label == "5 小時", "缺少 limit_window_seconds 時 primary 應預設為 '5 小時'"
        assert windows[1].label == "每週", "缺少 limit_window_seconds 時 secondary 應預設為 '每週'"


# ===========================================================================
# Antigravity (agy) 額度解析測試
# ===========================================================================


class TestAgyQuotaParsers:
    """測試 Antigravity 額度與 OAuth 憑證抽取。"""

    def test_parse_quota_summary_remaining_fraction_conversion_and_ordering(self) -> None:
        """驗證 remainingFraction 換算成 used_pct、5h/weekly 解析、gemini 群組排前面。"""
        resp = {
            "groups": [
                {
                    "displayName": "Claude Models",
                    "buckets": [
                        {
                            "bucketId": "claude-5h",
                            "displayName": "Claude 3.5 Sonnet",
                            "window": "5h",
                            "remainingFraction": 0.8,  # used_pct = 20.0
                            "resetTime": "2026-11-03T18:00:00Z",
                        }
                    ],
                },
                {
                    "displayName": "Gemini Pro",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "displayName": "Gemini 1.5 Pro Weekly",
                            "window": "weekly",
                            "remainingFraction": 0.1,  # used_pct = 90.0
                            "resetTime": "2026-11-10T00:00:00Z",
                        },
                        {
                            "bucketId": "gemini-5h",
                            "displayName": "Gemini 1.5 Pro 5h",
                            "window": "5h",
                            "remainingFraction": 0.35,  # used_pct = 65.0
                            "resetTime": "2026-11-03T15:00:00Z",
                        },
                    ],
                },
            ]
        }

        windows = parse_quota_summary(resp)
        assert len(windows) == 3, "應解析出 3 個 bucket 視窗"

        # 排序規則：
        # 1. 含 gemini 的 group 排最前面
        # 2. 同 group 內，5 小時排在每週前面
        assert "gemini-5h" in windows[0].key, "Gemini 群組的 5h 視窗應排在第 1 順位"
        assert windows[0].window_seconds == 18000
        # remainingFraction 0.35 -> used_pct = (1 - 0.35) * 100 = 65.0
        assert abs(windows[0].used_pct - 65.0) < 1e-4

        assert "gemini-weekly" in windows[1].key, "Gemini 群組的 weekly 視窗應排在第 2 順位"
        assert windows[1].window_seconds == 604800
        # remainingFraction 0.1 -> used_pct = (1 - 0.1) * 100 = 90.0
        assert abs(windows[1].used_pct - 90.0) < 1e-4

        assert "claude-5h" in windows[2].key, "非 Gemini 群組應排在後面"
        # remainingFraction 0.8 -> used_pct = 20.0
        assert abs(windows[2].used_pct - 20.0) < 1e-4

    def test_parse_quota_summary_clamp_remaining_fraction(self) -> None:
        """remainingFraction 小於 0 或大於 1 時應 clamp 至 0~1。"""
        resp = {
            "groups": [
                {
                    "displayName": "Gemini",
                    "buckets": [
                        {
                            "bucketId": "b1",
                            "displayName": "Over",
                            "window": "5h",
                            "remainingFraction": 1.5,  # clamp to 1 -> used = 0%
                        },
                        {
                            "bucketId": "b2",
                            "displayName": "Under",
                            "window": "weekly",
                            "remainingFraction": -0.5,  # clamp to 0 -> used = 100%
                        },
                    ],
                }
            ]
        }
        windows = parse_quota_summary(resp)
        assert abs(windows[0].used_pct - 0.0) < 1e-4, "超過 1 的 remainingFraction 應 clamp 為 1 (0% 已用)"
        assert abs(windows[1].used_pct - 100.0) < 1e-4, "小於 0 的 remainingFraction 應 clamp 為 0 (100% 已用)"

    def test_parse_available_models_max_six_and_sorted_by_remaining(self) -> None:
        """驗證 parse_available_models 最多回傳 6 個，且依剩餘比例由少到多排序。"""
        # 建立 8 個模型，剩餘比例各異
        models_dict = {
            f"model_{i}": {
                "quotaInfo": {
                    "remainingFraction": frac,
                    "resetTime": "2026-11-03T18:00:00Z",
                }
            }
            for i, frac in enumerate([0.9, 0.1, 0.5, 0.3, 0.8, 0.2, 0.7, 0.4])
        }

        resp = {"models": models_dict}
        windows = parse_available_models(resp)

        assert len(windows) <= 6, "最多只能回傳 6 個模型視窗"
        assert len(windows) == 6, "模型多於 6 個時應回傳恰好 6 個"

        # 檢驗依剩餘比例由少到多排序（即 remainingFraction 升序，剩越少排越前）
        # 各 frac 升序：0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 0.8, 0.9
        # 前 6 個 remainingFraction 應為 0.1, 0.2, 0.3, 0.4, 0.5, 0.7
        # 對應 remaining_pct: 10%, 20%, 30%, 40%, 50%, 70%
        remaining_percentages = [w.remaining_pct for w in windows]
        assert remaining_percentages == sorted(remaining_percentages), "模型視窗應依剩餘比例由少到多排序"
        assert abs(remaining_percentages[0] - 10.0) < 1e-4, "最少剩餘比例 (10%) 應排第 1"

    def test_extract_oauth_clients_from_noisy_bytes(self) -> None:
        """驗證從隨機二進位雜訊中正確擷取假 client_id 與 client_secret。"""
        # 明顯為假之測試用 ID 與 Secret（符合 SPEC regex）
        fake_id1 = b"123456789012-abcdefghijklmnopqrstuvwxyz123456.apps.googleusercontent.com"
        fake_id2 = b"987654321098-zyxwvutsrqponmlkjihgfedcba654321.apps.googleusercontent.com"
        # 前綴分開寫，避免被 GitHub 機密掃描誤判成真的憑證
        fake_sec1 = b"GOCSPX-" + b"abcdefghijklmnopqrstuvwxyz12"  # GOCSPX- + 28 字元
        fake_sec2 = b"GOCSPX-" + b"1234567890abcdefghijklmnopqr"  # GOCSPX- + 28 字元

        # 組合雜訊資料，刻意混入隨機 bytes、重複出現的 ID/Secret
        noise = os.urandom(1024)
        data = (
            b"noise_head\x00\xff"
            + fake_id1
            + b"\x00mid_noise\x01"
            + fake_sec1
            + noise
            + fake_id1  # 重複出現
            + b"\x00another_noise"
            + fake_id2
            + b"\x00"
            + fake_sec2
            + fake_sec1  # 重複出現
            + b"\xfe\xff_tail"
        )

        pairs = extract_oauth_clients(data)
        assert len(pairs) > 0, "應成功擷取出配對"

        # 抽出的 client_id 與 client_secret 字串檢查
        extracted_ids = {cid for cid, _ in pairs}
        extracted_secs = {sec for _, sec in pairs}

        assert fake_id1.decode("ascii") in extracted_ids, "應成功擷取 fake_id1"
        assert fake_id2.decode("ascii") in extracted_ids, "應成功擷取 fake_id2"
        assert fake_sec1.decode("ascii") in extracted_secs, "應成功擷取 fake_sec1"
        assert fake_sec2.decode("ascii") in extracted_secs, "應成功擷取 fake_sec2"
