"""診斷工具：用真實資料跑一次收集並印出結果（不會印出任何憑證）。

用法：.venv\\Scripts\\python.exe tools\\diagnose.py
"""

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.collector import Collector  # noqa: E402
from monitor.config import load_config  # noqa: E402
from monitor.fmt import format_countdown, format_tokens  # noqa: E402


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    collector = Collector(load_config())
    t0 = time.perf_counter()
    statuses = collector.collect(force_remote=True)
    elapsed = time.perf_counter() - t0
    now = time.time()
    for s in statuses:
        print(f"== {s.display_name}（方案：{s.plan or '未知'}）")
        t = s.tokens_today
        print(f"  今日 {format_tokens(t.total)}（輸入 {t.input:,}、輸出 {t.output:,}、快取讀 {t.cache_read:,}、快取寫 {t.cache_write:,}）")
        if s.tokens_window is not None:
            print(f"  {s.window_label} 視窗內 {format_tokens(s.tokens_window.total)}")
        for w in s.windows:
            reset = f"{format_countdown(w.resets_at - now)}後重置" if w.resets_at else "重置時間未知"
            print(f"  [{w.key}] {w.label}：已用 {w.used_pct:.1f}%、剩 {w.remaining_pct:.1f}%，{reset}")
        as_of = datetime.fromtimestamp(s.quota_as_of).strftime("%m/%d %H:%M:%S") if s.quota_as_of else "—"
        print(f"  額度來源：{s.quota_source}（{as_of}）")
        for n in s.notes:
            print(f"  note：{n}")
        if s.error:
            print(f"  error：{s.error}")
    print(f"-- 第一次收集耗時 {elapsed:.1f} 秒")
    t0 = time.perf_counter()
    collector.collect()
    print(f"-- 第二次（增量）耗時 {time.perf_counter() - t0:.2f} 秒")


if __name__ == "__main__":
    main()
