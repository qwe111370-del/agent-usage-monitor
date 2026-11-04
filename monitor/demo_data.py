"""提供完全虛構的展示資料；視窗可切換懸浮模式，左鍵拖曳、雙擊還原、右鍵操作。"""

from __future__ import annotations

from .model import AgentStatus, QuotaWindow, TokenCounts


def demo_statuses(now: float) -> list[AgentStatus]:
    """產生三家假資料，涵蓋三種來源、三種用量顏色、提示與錯誤。"""
    return [
        AgentStatus(
            agent="claude", display_name="Claude Code", plan="max",
            tokens_today=TokenCounts(1_100_000, 96_605, 25_000_000, 200_000),
            tokens_window=TokenCounts(700_000, 80_000, 18_000_000, 200_000),
            window_label="5 小時",
            windows=[
                QuotaWindow("five_hour", "5 小時", 15, now + 8_040, 18_000),
                QuotaWindow("seven_day", "每週", 65, now + 273_600, 604_800),
            ],
            quota_source="live", quota_as_of=now,
            notes=["展示資料：所有用量與額度皆為虛構。"],
        ),
        AgentStatus(
            agent="codex", display_name="Codex", plan="plus",
            tokens_today=TokenCounts(180_000, 30_000, 500_000),
            tokens_window=TokenCounts(90_000, 15_000, 220_000),
            window_label="5 小時",
            windows=[
                QuotaWindow("five_hour", "5 小時", 90, now + 2_700, 18_000),
                QuotaWindow("seven_day", "每週", 65, now + 172_800, 604_800),
            ],
            quota_source="local", quota_as_of=now - 600,
            error="即時查詢失敗（展示錯誤），改用本機紀錄。",
        ),
        AgentStatus(
            agent="agy", display_name="Antigravity",
            tokens_today=TokenCounts(35_000, 15_000, 160_000),
            tokens_window=TokenCounts(20_000, 10_000, 100_000),
            window_label="5 小時",
            windows=[
                QuotaWindow("gemini:5h", "Gemini 5 小時", 15, now + 10_800, 18_000),
                QuotaWindow("gemini:weekly", "Gemini 每週", 65, None, 604_800),
                QuotaWindow("claude:5h", "Claude 5 小時", 90, now + 3_600, 18_000),
                QuotaWindow("claude:weekly", "Claude 每週", 30, now + 345_600, 604_800),
            ],
            quota_source="snapshot", quota_as_of=now - 2_400,
            notes=["展示快照已超過 30 分鐘，額度以灰色顯示。"],
        ),
    ]
