"""整合三家本機用量與官方額度，隔離錯誤並管理查詢快取與退避。"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .aggregate import local_midnight, sum_between
from .config import Config
from .fmt import format_clock
from .model import AGENT_ORDER, DISPLAY_NAMES, AgentStatus
from .secure_http import HttpError
from .sources import agy_quota, claude_quota, codex_quota
from .sources.agy_local import AgyConversationReader
from .sources.claude_local import ClaudeLogReader
from .sources.codex_local import CodexLogReader, rate_limits_to_windows

_SAFE_REASONS = frozenset({
    "找不到憑證", "找不到 Antigravity 登入憑證", "需要重新登入 agy",
    "找不到 Antigravity OAuth 設定", "Antigravity 憑證刷新失敗",
    "沒有可用的 Antigravity 額度資料", "官方未回傳額度資料",
})


def _reason(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        return f"HTTP {exc.status}" if isinstance(exc.status, int) else "網路查詢失敗"
    # 不轉述任意例外內容，避免檔案、JSON、HTTP 或第三方函式洩漏憑證。
    if type(exc) is RuntimeError and exc.args and isinstance(exc.args[0], str) and exc.args[0] in _SAFE_REASONS:
        return exc.args[0]
    return "資料查詢失敗"


class Collector:
    def __init__(self, config: Config | None = None, now: Callable[[], float] = time.time):
        self.config = config if config is not None else Config()
        self.now = now
        self.claude_home = Path(self.config.claude_home) if self.config.claude_home is not None else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        self.codex_home = Path(self.config.codex_home) if self.config.codex_home is not None else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.agy_home = Path(self.config.agy_home) if self.config.agy_home is not None else Path.home() / ".gemini/antigravity-cli"
        self._desktop_dirs = None if self.config.claude_desktop_dirs is None else [Path(p) for p in self.config.claude_desktop_dirs]
        self._readers = {
            "claude": ClaudeLogReader(self.claude_home / "projects"),
            "codex": CodexLogReader([self.codex_home / "sessions", self.codex_home / "archived_sessions"]),
            "agy": AgyConversationReader(self.agy_home / "conversations"),
        }
        self._agy_client = agy_quota.AgyQuotaClient(self.config.agy_bin)
        self._quota: dict[str, AgentStatus] = {}
        self._next_remote: dict[str, float] = {}
        self._backoff_until: dict[str, float] = {}

    def _get_desktop_dirs(self) -> list[Path]:
        if self._desktop_dirs is None:
            self._desktop_dirs = claude_quota.discover_desktop_dirs()
        return self._desktop_dirs

    def _query(self, agent: str, now: float) -> tuple[AgentStatus, Exception | None]:
        status = AgentStatus(agent, DISPLAY_NAMES[agent])
        failure = None
        try:
            if agent == "claude":
                token = claude_quota.get_access_token(self.claude_home, self._get_desktop_dirs(), now)
                if token is None:
                    raise RuntimeError("找不到憑證")
                status.windows = claude_quota.query_usage(token)
            elif agent == "codex":
                auth = codex_quota.read_auth(self.codex_home)
                if auth is None:
                    raise RuntimeError("找不到憑證")
                status.plan, status.windows = codex_quota.query_usage(*auth)
            else:
                status.windows = self._agy_client.query_usage(now)
            if not status.windows:
                raise RuntimeError("官方未回傳額度資料")
            status.quota_source = "live"
            status.quota_as_of = now
        except Exception as exc:
            failure = exc
            reason = _reason(exc)
            if agent == "claude":
                snapshot = claude_quota.read_snapshot(self._get_desktop_dirs())
                if snapshot is not None:
                    status.quota_as_of, status.windows = snapshot
                    status.quota_source = "snapshot"
                    status.notes.append(f"即時查詢失敗（{reason}），顯示桌面 App 在 {format_clock(status.quota_as_of)} 記錄的快照")
            elif agent == "codex":
                local = self._readers["codex"].latest_rate_limits
                if local is not None:
                    status.quota_as_of, limits = local
                    status.plan, status.windows = rate_limits_to_windows(limits)
                    status.quota_source = "local"
                    status.notes.append(f"即時查詢失敗（{reason}），改用本機紀錄")
            if status.quota_source == "none":
                status.notes.append(reason)
                status.error = reason
        return status, failure

    def _collect_agent(self, agent: str, now: float, force_remote: bool) -> AgentStatus:
        local_error = None
        reader = self._readers[agent]
        try:
            if agent == "agy":
                reader.refresh()
            else:
                reader.refresh(now)
        except Exception:
            local_error = "本機用量讀取失敗"
        due = agent not in self._quota or now >= self._next_remote.get(agent, 0) or force_remote
        if due and now >= self._backoff_until.get(agent, -math.inf):
            quota, failure = self._query(agent, now)
            self._quota[agent] = quota
            interval = max(1, self.config.remote_interval_s)
            delay = interval
            self._backoff_until.pop(agent, None)
            if isinstance(failure, HttpError) and failure.status == 429:
                retry = failure.retry_after
                delay = max(0, retry) if isinstance(retry, (int, float)) and math.isfinite(retry) else min(900, interval * 2)
                self._backoff_until[agent] = now + delay
            self._next_remote[agent] = now + delay
        cached = self._quota.get(agent, AgentStatus(agent, DISPLAY_NAMES[agent]))
        # 每次建立獨立結果，GUI 及過期處理都不會修改官方快取。
        status = replace(cached, windows=[replace(window) for window in cached.windows], notes=list(cached.notes))
        for window in status.windows:
            if window.resets_at is not None and window.resets_at <= now:
                window.used_pct = 0
                status.notes.append(f"{window.label} 已重置，等待新資料")
        events = reader.events
        status.tokens_today = sum_between(events, local_midnight(now), now)
        primary = next((window for window in status.windows
                        if window.window_seconds is not None and abs(window.window_seconds - 18000) <= 300
                        and window.resets_at is not None and window.resets_at > now), None)
        if primary is not None:
            start = primary.resets_at - 18000
            status.window_label = primary.label
        else:
            start = now - 18000
            status.window_label = "近 5 小時"
        status.tokens_window = sum_between(events, start, now)
        if local_error:
            status.error = local_error
        return status

    def collect(self, force_remote: bool = False) -> list[AgentStatus]:
        try:
            now = self.now()
            if not math.isfinite(now):
                raise ValueError
        except Exception:
            now = time.time()
        statuses = []
        for agent in AGENT_ORDER:
            if agent not in self.config.enabled:
                continue
            try:
                statuses.append(self._collect_agent(agent, now, force_remote))
            except Exception:
                statuses.append(AgentStatus(agent, DISPLAY_NAMES[agent], error="資料收集失敗"))
        return statuses
