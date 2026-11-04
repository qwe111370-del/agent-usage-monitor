"""僅允許白名單 HTTPS 查詢，禁止轉址並隔離所有敏感錯誤內容。"""

import json
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib import error, parse, request

ALLOWED_HOSTS = frozenset({"api.anthropic.com", "chatgpt.com", "daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com", "oauth2.googleapis.com"})


class HttpError(Exception):
    status: int | None
    retry_after: float | None

    def __init__(self, status: int | None = None, retry_after: float | None = None):
        self.status = status
        self.retry_after = retry_after
        # 只保留固定集合中的 OAuth 錯誤碼，不暴露伺服器原始回應。
        self.oauth_error: str | None = None
        super().__init__(f"HTTP {status}" if status is not None else "網路查詢失敗")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        result = float(value)
    except ValueError:
        try:
            result = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, result) if math.isfinite(result) else None


def _send(url: str, headers: dict[str, str], data: bytes | None = None) -> Any:
    try:
        parts = parse.urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS
                 and parts.username is None and parts.password is None
                 and parts.port in (None, 443))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("只允許白名單 HTTPS 主機") from None
    result = None
    failure = None
    try:
        req = request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
        # 不採用環境代理，避免將含憑證的請求交給額外的主機。
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        with opener.open(req, timeout=15) as response:
            if not 200 <= response.status < 300:
                failure = HttpError(response.status)
            else:
                result = json.loads(response.read())
    except error.HTTPError as exc:
        failure = HttpError(exc.code, _retry_after(exc.headers.get("Retry-After")) if exc.code == 429 else None)
        try:
            if parts.hostname == "oauth2.googleapis.com":
                obj = json.loads(exc.read(65536))
                code = obj.get("error") if isinstance(obj, dict) else None
                if code in ("invalid_client", "unauthorized_client", "invalid_grant"):
                    failure.oauth_error = code
        except Exception:
            pass
        finally:
            exc.close()
    except Exception:
        failure = HttpError()
    # 在 except 區段外丟出，避免 exception context 帶出 URL、body 或憑證。
    if failure is not None:
        raise failure from None
    return result


def get_json(url: str, headers: dict[str, str]) -> Any:
    return _send(url, headers)


def post_json(url: str, headers: dict[str, str], body: dict) -> Any:
    data = None
    try:
        data = json.dumps(body).encode("utf-8")
    except Exception:
        pass
    if data is None:
        raise ValueError("無法編碼查詢內容") from None
    return _send(url, {**headers, "Content-Type": "application/json"}, data)


def post_form(url: str, form: dict[str, str]) -> Any:
    data = None
    try:
        data = parse.urlencode(form).encode("ascii")
    except Exception:
        pass
    if data is None:
        raise ValueError("無法編碼查詢內容") from None
    return _send(url, {"Content-Type": "application/x-www-form-urlencoded"}, data)
