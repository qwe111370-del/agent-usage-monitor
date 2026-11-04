"""查詢 Claude 官方額度，並讀取桌面快照作為後備。

參考 CodeZeno/Claude-Code-Usage-Monitor（MIT）src/poller/claude_desktop.rs、antigravity.rs 的做法，以 Python 重寫
"""

import base64
import json
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..model import QuotaWindow
from ..secure_http import get_json
from . import win_crypto
from .claude_local import _number, _timestamp


def decrypt_v10(key: bytes, b64_value: str) -> bytes:
    result = None
    try:
        value = base64.b64decode(b64_value, validate=True)
        if value.startswith(b"v10") and len(value) >= 3 + 12 + 16:
            result = AESGCM(key).decrypt(value[3:15], value[15:], None)
    except Exception:
        pass
    if result is None:
        raise ValueError("無法解密桌面 App 憑證") from None
    return result


def pick_token(entries: dict, now_ms: float) -> str | None:
    if not isinstance(entries, dict):
        return None
    candidates = []
    for name, entry in entries.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        token = entry.get("token")
        expires = _number(entry.get("expiresAt"), 0)
        if not isinstance(token, str) or not token.strip() or expires <= now_ms:
            continue
        rank = 2 if "user:inference" in name and "user:profile" in name else 1 if "user:inference" in name else 0
        candidates.append((rank, expires, token))
    return max(candidates, key=lambda entry: (entry[0], entry[1]))[2] if candidates else None


def discover_desktop_dirs() -> list[Path]:
    result = []
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if local:
        root = Path(local) / "Claude-Data"
        try:
            matches = [p for p in root.glob("claude_*") if p.is_dir() and re.fullmatch(r"claude_[a-z0-9]{13}", p.name)]
            result.extend(sorted(matches, key=lambda p: (p.name != "claude_pzs8sxrjxfjjc", p.name)))
        except OSError:
            pass
    if roaming:
        result.append(Path(roaming) / "Claude")
    if local:
        try:
            result.extend(sorted((Path(local) / "Packages").glob("Claude_*/LocalCache/Roaming/Claude")))
        except OSError:
            pass
    return result


def get_access_token(claude_home: Path, desktop_dirs: list[Path], now: float) -> str | None:
    for directory in desktop_dirs:
        try:
            config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
            local_state = json.loads((directory / "Local State").read_text(encoding="utf-8"))
            encrypted = base64.b64decode(local_state["os_crypt"]["encrypted_key"], validate=True)
            if not encrypted.startswith(b"DPAPI"):
                continue
            key = win_crypto.dpapi_unprotect(encrypted[5:])
            for name in ("oauth:tokenCacheV2", "oauth:tokenCache"):
                value = config.get(name)
                if not isinstance(value, str) or not value.strip():
                    continue
                try:
                    token = pick_token(json.loads(decrypt_v10(key, value)), now * 1000)
                    if token:
                        return token
                except Exception:
                    continue
        except Exception:
            continue
    try:
        obj = json.loads((claude_home / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        token = obj.get("accessToken")
        if isinstance(token, str) and token.strip() and _number(obj.get("expiresAt")) > now * 1000:
            return token
    except Exception:
        pass
    return None


def parse_usage_response(obj: dict) -> list[QuotaWindow]:
    if not isinstance(obj, dict):
        return []
    windows = {}
    definitions = (("five_hour", "5 小時", 18000), ("seven_day", "每週", 604800),
                   ("seven_day_opus", "每週 Opus", 604800), ("seven_day_sonnet", "每週 Sonnet", 604800))
    for key, label, seconds in definitions:
        value = obj.get(key)
        if not isinstance(value, dict):
            continue
        percent = _number(value.get("utilization"), None)
        if percent is not None:
            windows[key] = QuotaWindow(key, label, float(percent), _timestamp(value.get("resets_at")), seconds)
    limits = obj.get("limits")
    if isinstance(limits, list):
        for value in limits:
            if not isinstance(value, dict) or value.get("scope"):
                continue
            kind = value.get("kind")
            key = "five_hour" if kind == "session" else "seven_day" if kind == "weekly_all" else None
            if key is None or key in windows:
                continue
            percent = _number(value.get("percent"), None)
            if percent is None:
                percent = _number(value.get("utilization"), None)
            if percent is not None:
                label, seconds = ("5 小時", 18000) if key == "five_hour" else ("每週", 604800)
                windows[key] = QuotaWindow(key, label, float(percent), _timestamp(value.get("resets_at")), seconds)
    return [windows[key] for key, _, _ in definitions if key in windows]


def parse_plan_usage_history(obj: dict) -> tuple[float, list[QuotaWindow]] | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("samples"), list):
        return None
    candidates = []
    for sample in obj["samples"]:
        if not isinstance(sample, dict) or not isinstance(sample.get("u"), dict) or not sample["u"]:
            continue
        ts = _number(sample.get("t"), None)
        if ts is not None:
            candidates.append((ts, sample["u"]))
    if not candidates:
        return None
    ts, usage = max(candidates, key=lambda item: item[0])
    windows = []
    for field, key, label, seconds in (("fh", "five_hour", "5 小時", 18000), ("sd", "seven_day", "每週", 604800)):
        value = _number(usage.get(field), None)
        if value is not None:
            windows.append(QuotaWindow(key, label, float(value), None, seconds))
    return ts / 1000, windows


def read_snapshot(desktop_dirs: list[Path]) -> tuple[float, list[QuotaWindow]] | None:
    snapshots = []
    for directory in desktop_dirs:
        try:
            snapshot = parse_plan_usage_history(json.loads((directory / "plan-usage-history.json").read_text(encoding="utf-8")))
            if snapshot is not None:
                snapshots.append(snapshot)
        except Exception:
            continue
    return max(snapshots, key=lambda value: value[0]) if snapshots else None


def query_usage(token: str) -> list[QuotaWindow]:
    return parse_usage_response(get_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "agent-usage-monitor/1.0",
    }))
