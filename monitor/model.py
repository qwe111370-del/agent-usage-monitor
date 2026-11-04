"""共用資料結構：資料來源（sources/、collector.py）與畫面（gui.py）之間只透過這些型別溝通。"""

from __future__ import annotations

from dataclasses import dataclass, field

AGENT_ORDER = ("claude", "codex", "agy")
DISPLAY_NAMES = {"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity"}


@dataclass
class TokenCounts:
    """token 用量。各家欄位統一換算成這四類，彼此不重疊。"""

    input: int = 0  # 未命中快取的輸入
    output: int = 0  # 輸出，含 reasoning／thinking
    cache_read: int = 0  # 快取讀取
    cache_write: int = 0  # 快取寫入（只有 Claude 有）

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    @property
    def non_cache(self) -> int:
        """扣掉快取讀取後的量，比較接近「實際新處理」的 token。"""
        return self.input + self.output + self.cache_write

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
        )


@dataclass(frozen=True)
class UsageEvent:
    """一次 API 呼叫（或一段時間）的用量。ts 為 epoch 秒。"""

    ts: float
    counts: TokenCounts


@dataclass
class QuotaWindow:
    """官方的一個用量額度視窗。"""

    key: str  # 例如 "five_hour"、"seven_day"、"gemini:5h"
    label: str  # 顯示用，例如「5 小時」「每週」「Gemini 5 小時」
    used_pct: float  # 已用百分比 0–100
    resets_at: float | None = None  # 重置時間（epoch 秒），未知為 None
    window_seconds: int | None = None  # 視窗長度（秒），未知為 None

    @property
    def remaining_pct(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_pct))


@dataclass
class AgentStatus:
    """一個 agent 目前的狀態，畫面上的一張卡片。"""

    agent: str  # "claude" | "codex" | "agy"
    display_name: str
    plan: str | None = None  # 方案名稱，例如 "plus"、"max"；未知為 None
    tokens_today: TokenCounts = field(default_factory=TokenCounts)  # 本機時間今天 00:00 起
    tokens_window: TokenCounts | None = None  # 目前主要額度視窗（通常是 5 小時）內的用量
    window_label: str | None = None  # tokens_window 對應的視窗名稱，例如「5 小時」
    windows: list[QuotaWindow] = field(default_factory=list)  # 官方額度，依重要性排序
    quota_source: str = "none"  # "live" 即時查詢｜"local" 本機紀錄｜"snapshot" 舊快照｜"none" 沒有資料
    quota_as_of: float | None = None  # 額度資料的時間（epoch 秒）
    notes: list[str] = field(default_factory=list)  # 給使用者看的提示（繁體中文）
    error: str | None = None  # 整張卡片的錯誤訊息（繁體中文）；有部分資料時仍應填其他欄位
