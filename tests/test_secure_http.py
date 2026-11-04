"""安全 HTTPS 請求用戶端測試。

驗證 monitor.secure_http 模組的嚴格白名單與傳輸安全性：
- 非白名單主機（例如 example.com, google.com, evil.com, 非授權子網域）一律拋出 ValueError。
- 非 HTTPS 協定（例如 http://, ftp:// 等）一律拋出 ValueError。
- get_json、post_json、post_form 三個對外介面皆受相同安全防護約束。
- 所有非法請求在發起任何實際網路連線前即被攔截。
"""

from __future__ import annotations

import socket
import pytest

from monitor.secure_http import get_json, post_form, post_json


# ---------------------------------------------------------------------------
# 非白名單與非 HTTPS 網址測試
# ---------------------------------------------------------------------------


class TestSecureHttpValidation:
    """測試主機白名單與 HTTPS 強制限制。"""

    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自動阻斷底層 socket 連線，確保測試過程中絕無任何網路請求發出。"""
        def fail_on_connect(*args, **kwargs):
            pytest.fail("安全性檢查失敗：未攔截非法請求即嘗試建立網路連線！")

        monkeypatch.setattr(socket.socket, "connect", fail_on_connect)

    @pytest.mark.parametrize(
        "invalid_url",
        [
            # 非白名單主機
            "https://example.com/api",
            "https://google.com/search",
            "https://evil-anthropic.com/v1/messages",
            "https://sub.chatgpt.com/api",
            "https://api.anthropic.com.attacker.com/test",
            "https://anthropic.com/api",  # 必須為 api.anthropic.com
            # 明確禁止之明文 HTTP 協定（即使為白名單主機）
            "http://api.anthropic.com/api/oauth/usage",
            "http://chatgpt.com/backend-api/wham/usage",
            "http://oauth2.googleapis.com/token",
            "http://cloudcode-pa.googleapis.com/v1internal",
            "http://daily-cloudcode-pa.googleapis.com/v1internal",
            # 其他非 HTTPS 協定
            "ftp://api.anthropic.com/file",
            "file:///C:/path/to/secret",
            "ws://chatgpt.com/socket",
            # 格式損毀之 URL
            "not_a_url",
            "://api.anthropic.com",
            "",
        ],
    )
    def test_get_json_rejects_non_whitelisted_or_insecure_url(self, invalid_url: str) -> None:
        """get_json 遇到非白名單或非 https 應立即拋出 ValueError。"""
        with pytest.raises(ValueError):
            get_json(invalid_url, headers={"Authorization": "Bearer test"})

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "https://example.com/post",
            "https://attacker.org/leak",
            "http://oauth2.googleapis.com/token",
            "http://cloudcode-pa.googleapis.com/v1internal",
            "ftp://cloudcode-pa.googleapis.com/resource",
        ],
    )
    def test_post_json_rejects_non_whitelisted_or_insecure_url(self, invalid_url: str) -> None:
        """post_json 遇到非白名單或非 https 應立即拋出 ValueError。"""
        with pytest.raises(ValueError):
            post_json(
                invalid_url,
                headers={"Authorization": "Bearer test"},
                body={"key": "value"},
            )

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "https://example.com/token",
            "https://phishing.com/oauth",
            "http://oauth2.googleapis.com/token",
            "http://chatgpt.com/token",
        ],
    )
    def test_post_form_rejects_non_whitelisted_or_insecure_url(self, invalid_url: str) -> None:
        """post_form 遇到非白名單或非 https 應立即拋出 ValueError。"""
        with pytest.raises(ValueError):
            post_form(
                invalid_url,
                form={"client_id": "test", "grant_type": "refresh_token"},
            )
