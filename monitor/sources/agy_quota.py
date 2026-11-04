"""查詢 Antigravity 官方額度，在記憶體刷新 OAuth 憑證與快取。

參考 CodeZeno/Claude-Code-Usage-Monitor（MIT）src/poller/claude_desktop.rs、antigravity.rs 的做法，以 Python 重寫
"""

from __future__ import annotations

import json
import mmap
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..model import QuotaWindow
from ..secure_http import HttpError, post_form, post_json
from . import win_crypto
from .claude_local import _number, _timestamp


def extract_oauth_clients(data: bytes) -> list[tuple[str, str]]:
    ids = list(dict.fromkeys(match.group().decode("ascii") for match in re.finditer(rb"\d{10,}-[a-z0-9]{20,80}\.apps\.googleusercontent\.com", data)))
    secrets = list(dict.fromkeys(match.group().decode("ascii") for match in re.finditer(rb"GOCSPX-[A-Za-z0-9_\-]{28}", data)))
    return [(client_id, secret) for client_id in ids for secret in secrets]


def parse_quota_summary(obj: dict) -> list[QuotaWindow]:
    if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
        return []
    rows = []
    for group_index, group in enumerate(obj["groups"]):
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        group_name = group.get("displayName")
        group_name = group_name if isinstance(group_name, str) else ""
        for index, bucket in enumerate(group["buckets"]):
            if not isinstance(bucket, dict):
                continue
            fraction = _number(bucket.get("remainingFraction"), None)
            if fraction is None:
                continue
            window = bucket.get("window", "")
            window = window if isinstance(window, str) else ""
            lower = window.lower()
            seconds = 18000 if lower == "5h" else 604800 if lower == "weekly" else None
            suffix = "5 小時" if lower == "5h" else "每週" if lower == "weekly" else window
            name = group_name or bucket.get("displayName") or ""
            name = name if isinstance(name, str) else ""
            value = QuotaWindow(f"{bucket.get('bucketId', '')}:{window}", f"{name} {suffix}".strip(),
                                (1 - max(0, min(1, fraction))) * 100,
                                _timestamp(bucket.get("resetTime")), seconds)
            rank = 0 if lower == "5h" else 1 if lower == "weekly" else 2
            rows.append((0 if "gemini" in group_name.lower() else 1, group_index, rank, index, value))
    return [row[-1] for row in sorted(rows, key=lambda row: row[:-1])]


def parse_available_models(obj: dict) -> list[QuotaWindow]:
    if not isinstance(obj, dict) or not isinstance(obj.get("models"), dict):
        return []
    windows = []
    for model_id, model in obj["models"].items():
        info = model.get("quotaInfo") if isinstance(model, dict) else None
        if not isinstance(info, dict):
            continue
        fraction = _number(info.get("remainingFraction"), None)
        if fraction is None:
            continue
        windows.append(QuotaWindow(model_id, model_id, (1 - max(0, min(1, fraction))) * 100,
                                   _timestamp(info.get("resetTime")), None))
    return sorted(windows, key=lambda window: window.remaining_pct)[:6]


def _binary_paths(configured: Path | None) -> list[Path]:
    values = []
    if configured is not None:
        values.append(Path(configured))
    if os.environ.get("AGY_BIN"):
        values.append(Path(os.environ["AGY_BIN"]))
    if os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
        values.extend([local / "agy/bin/agy.exe",
                       local / "Programs/Antigravity/resources/bin/language_server.exe",
                       local / "Programs/Antigravity IDE/resources/bin/language_server.exe"])
    return list(dict.fromkeys(values))


def _scan_clients(configured: Path | None) -> list[tuple[str, str]]:
    for path in _binary_paths(configured):
        try:
            with path.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size == 0:
                    continue
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    clients = extract_oauth_clients(data)
            if clients:
                return clients
        except Exception:
            continue
    return []


class AgyQuotaClient:
    """只在記憶體持有登入狀態；可替換 win_crypto 與 HTTP 函式進行離線測試。"""

    def __init__(self, agy_bin: Path | None = None):
        self.agy_bin = agy_bin
        self._access_token = ""
        self._refresh_token = ""
        self._expiry: float | None = None
        self._clients: list[tuple[str, str]] | None = None

    def _load_credential(self) -> None:
        credential = None
        try:
            blob = win_crypto.read_generic_credential("gemini:antigravity")
            if blob:
                encoding = "utf-16le" if len(blob) % 2 == 0 and all(value == 0 for value in blob[1::2]) else "utf-8"
                obj = json.loads(blob.decode(encoding))
                credential = obj.get("token") if isinstance(obj, dict) else None
                if not isinstance(credential, dict):
                    credential = None
        except Exception:
            pass
        if credential is None:
            raise RuntimeError("找不到 Antigravity 登入憑證") from None
        token, refresh = credential.get("access_token"), credential.get("refresh_token")
        self._access_token = token if isinstance(token, str) and token.strip() else ""
        self._refresh_token = refresh if isinstance(refresh, str) and refresh.strip() else ""
        self._expiry = _timestamp(credential.get("expiry"))

    def _refresh(self, now: float) -> None:
        if not self._refresh_token:
            raise RuntimeError("需要重新登入 agy")
        if self._clients is None:
            # 即使由非 GUI 呼叫，也確保大型執行檔掃描在背景執行緒完成。
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="agy-oauth") as pool:
                self._clients = pool.submit(_scan_clients, self.agy_bin).result()
        if not self._clients:
            raise RuntimeError("找不到 Antigravity OAuth 設定")
        for client_id, secret in self._clients:
            try:
                obj = post_form("https://oauth2.googleapis.com/token", {
                    "client_id": client_id, "client_secret": secret,
                    "refresh_token": self._refresh_token, "grant_type": "refresh_token",
                })
            except HttpError as exc:
                if exc.oauth_error in ("invalid_client", "unauthorized_client"):
                    continue
                if exc.oauth_error == "invalid_grant":
                    self._access_token = ""
                    self._refresh_token = ""
                    self._expiry = None
                    raise RuntimeError("需要重新登入 agy") from None
                raise
            token = obj.get("access_token") if isinstance(obj, dict) else None
            if isinstance(token, str) and token.strip():
                self._access_token = token
                self._expiry = now + max(0, _number(obj.get("expires_in"), 3600))
                return
        raise RuntimeError("Antigravity 憑證刷新失敗")

    def _query(self) -> list[QuotaWindow]:
        headers = {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json",
                   "User-Agent": "antigravity/cli/1.4.0"}
        last_error = None
        auth_error = None
        for base in ("https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com"):
            try:
                project = ""
                try:
                    obj = post_json(base + "/v1internal:loadCodeAssist", headers, {"metadata": {"ideType": "ANTIGRAVITY"}})
                    project = obj.get("cloudaicompanionProject", "") if isinstance(obj, dict) else ""
                    project = project if isinstance(project, str) else ""
                except HttpError as exc:
                    if exc.status in (401, 403, 429):
                        raise
                if project:
                    try:
                        windows = parse_quota_summary(post_json(base + "/v1internal:retrieveUserQuotaSummary", headers, {"project": project}))
                        if windows:
                            return windows
                    except HttpError as exc:
                        if exc.status in (401, 403, 429):
                            raise
                windows = parse_available_models(post_json(base + "/v1internal:fetchAvailableModels", headers, {"project": project} if project else {}))
                if windows:
                    return windows
            except HttpError as exc:
                if exc.status == 429:
                    raise
                if exc.status in (401, 403):
                    auth_error = exc
                last_error = exc
        if auth_error is not None:
            raise auth_error
        if last_error is not None:
            raise last_error
        raise RuntimeError("沒有可用的 Antigravity 額度資料")

    def query_usage(self, now: float) -> list[QuotaWindow]:
        if not self._access_token and not self._refresh_token:
            self._load_credential()
        refreshed = False
        if self._refresh_token and (not self._access_token or (self._expiry is not None and self._expiry <= now + 60)):
            self._refresh(now)
            refreshed = True
        if not self._access_token:
            raise RuntimeError("需要重新登入 agy")
        try:
            return self._query()
        except HttpError as exc:
            if exc.status not in (401, 403) or refreshed:
                raise
        # 一輪最多刷新一次，重試也只在記憶體使用新 token。
        self._refresh(now)
        return self._query()
